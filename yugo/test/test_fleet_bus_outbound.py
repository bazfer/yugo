"""
Outbound tests for v0.3c — what a bus-triggered turn puts back on the wire.

Two halves again, split by what each can prove:

  * the PARSER half is pure. `<BUS to="…">…</BUS>` is a string problem, and a
    broker adds nothing to "which characters were consumed".
  * the DISPATCH half runs against a REAL nats-server, because every claim
    here is about bytes reaching (or not reaching) a subject: an envelope
    addressed to a third party, an answer addressed to the sender, and — the
    one that cannot be faked at all — two bots auto-replying to each other
    until someone stops them. `test_two_bots_do_not_ping_pong` is a real
    ping-pong between two real adapters over a real broker; without the loop
    guard it runs until its fuse blows, which is exactly the failure the guard
    exists to prevent.

Every guard here is written against a specific mutation, named in the test's
docstring. The mutation table is in the PR body.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import fleet_bus
from test.nats_server import (  # noqa: F401 — `nats_server` is a fixture
    BOT_NAME,
    BOT_PASSWORD,
    PEER_NAME,
    PEER_PASSWORD,
    nats_server,
)

HEARTBEAT_S = 0.3
RECONNECT_WAIT_S = 0.05

# A third bot that exists in the manifest but has no credentials on the test
# broker — the `<BUS>` tag's normal case is addressing someone who is NOT the
# sender, and nothing about publishing requires the recipient to be online.
THIRD_NAME = "ohm"
# ...and one that is not in the manifest at all.
STRANGER_NAME = "stranger"

ALLOWED = frozenset({BOT_NAME, PEER_NAME, THIRD_NAME})


def _peer_envelope(**overrides) -> dict:
    envelope = {
        "envelope_version": 1,
        "id": "peer-1",
        "from": PEER_NAME,
        "to": BOT_NAME,
        "kind": "text_message",
        "ts": "2026-08-26T12:00:00.000Z",
        "payload": {"text": "hello"},
    }
    envelope.update(overrides)
    return envelope


def _encode(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


# ---------- parser: what counts as a tag (pure) ----------


def test_a_tag_is_lifted_out_of_the_reply_text():
    """The two things the parser owes its caller: the recipient plus the
    message, and a span that covers the WHOLE tag so the caller can strip it.

    Mutation this catches: a span that stops short of `</BUS>` (markup leaks
    into the answer the sender receives), or a body that keeps the markup.
    """
    reply = 'on it. <BUS to="vec">please rebase #12</BUS> anything else?'
    tags = fleet_bus.find_bus_tags(reply)

    assert len(tags) == 1
    assert tags[0].attrs == {"to": "vec"}
    assert tags[0].body == "please rebase #12"
    assert reply[tags[0].start : tags[0].end] == (
        '<BUS to="vec">please rebase #12</BUS>'
    )
    assert fleet_bus.strip_bus_tags(reply, tags) == "on it.  anything else?"


def test_tags_are_returned_in_document_order_and_all_of_them_are_stripped():
    """One turn may address more than one peer.

    Mutation this catches: a parser that returns after the first match (the
    second peer never hears from us AND their markup rides out to the first),
    and a strip that only removes one span.
    """
    reply = 'a <BUS to="vec">one</BUS> b <BUS to="ohm">two</BUS> c'
    tags = fleet_bus.find_bus_tags(reply)

    assert [tag.attrs["to"] for tag in tags] == ["vec", "ohm"]
    assert [tag.body for tag in tags] == ["one", "two"]
    assert fleet_bus.strip_bus_tags(reply, tags) == "a  b  c"


def test_an_angle_bracket_inside_a_quoted_attribute_does_not_end_the_tag():
    """The reason this parser is hand-rolled rather than a regex, recorded in
    the sibling adapter's design (fleet-bus `docs/CODEX-ADAPTER-DESIGN.md` §6):
    `<BUS[^>]*>` terminates on the first `>`, which may be inside a quoted
    value.

    What that costs is a MESSAGE. The tag below is well-formed and names a real
    peer; a scan that stops at the `>` inside `note` sees an unterminated quote,
    calls the whole tag malformed, and the peer never hears from us — while the
    audit line blames the model for markup it got right.

    Mutation this catches: replacing `_scan_open_tag` with `text.find('>')`.
    """
    reply = '<BUS to="vec" note="ship if a > b">rebase please</BUS>'
    tags = fleet_bus.find_bus_tags(reply)

    assert len(tags) == 1
    assert tags[0].attrs == {"to": "vec", "note": "ship if a > b"}
    assert tags[0].body == "rebase please"
    assert reply[tags[0].start : tags[0].end] == reply
    assert fleet_bus.strip_bus_tags(reply, tags) == ""


@pytest.mark.parametrize(
    "label,reply,cause",
    [
        ("no-closing-tag", '<BUS to="vec">where does this end', "unclosed"),
        ("open-tag-runs-off", '<BUS to="vec', "unclosed"),
        ("bare-marker", "trailing <BUS", "unclosed"),
        ("unquoted-value", "<BUS to=vec>nope</BUS>", "malformed"),
        ("spaces-around-equals", '<BUS to = "vec">nope</BUS>', "malformed"),
        ("no-equals", '<BUS "vec">nope</BUS>', "malformed"),
    ],
)
def test_a_broken_tag_is_reported_as_unpublishable_and_still_stripped(
    label, reply, cause
):
    """A tag that does not parse must not publish, must not raise, and must
    still be removed from the text — a rejected tag left in place is markup
    the sender receives as if it were prose.

    Mutation this catches: raising out of the parser (kills the turn's
    answer), and returning nothing for a broken tag (the span is never
    stripped, so the raw markup goes to the sender).
    """
    tags = fleet_bus.find_bus_tags(reply)

    assert len(tags) == 1, label
    assert tags[0].attrs is None, label
    assert tags[0].cause == cause, label
    # Whatever prose surrounded the wreckage survives; none of the markup does.
    stripped = fleet_bus.strip_bus_tags(reply, tags)
    assert "<BUS" not in stripped, label
    assert "vec" not in stripped, label


@pytest.mark.parametrize(
    "reply",
    [
        "the <BUSY> signal was high",
        "a plain sentence with no markup",
        "<bus to='vec'>lowercase is not the tag</bus>",
    ],
)
def test_text_that_only_looks_like_a_tag_is_left_alone(reply):
    """`<BUSY>` is a different word, and the tag is upper-case by contract.

    Mutation this catches: dropping the "next character must be whitespace or
    `>`" check, which turns every word starting with BUS into tag wreckage —
    and, because a broken tag is stripped, silently deletes it from the answer.
    """
    assert fleet_bus.find_bus_tags(reply) == []
    assert fleet_bus.strip_bus_tags(reply, []) == reply


def test_a_tag_with_no_attributes_parses_but_names_nobody():
    """`<BUS>` is recognised as a tag so that it is audited and stripped, and
    it resolves to no recipient so that it cannot publish. Both halves matter:
    the publish gate is what refuses it, not the parser.
    """
    tags = fleet_bus.find_bus_tags("<BUS>who is this for?</BUS>")

    assert len(tags) == 1
    assert tags[0].attrs == {}
    assert tags[0].attrs.get("to") is None


# ---------- envelope construction (pure) ----------


def test_a_non_reply_envelope_omits_in_reply_to_entirely():
    """Null is not absent: `validate_envelope` — and its TypeScript peer —
    reject a present-but-non-string `in_reply_to` as `invalid_in_reply_to`, so
    an envelope that writes the key as null is one no adapter will accept.

    Mutation this catches: `"in_reply_to": in_reply_to` written
    unconditionally in `create_request_envelope`.
    """
    envelope = fleet_bus.create_request_envelope(BOT_NAME, PEER_NAME, {"text": "hi"})

    assert "in_reply_to" not in envelope
    assert fleet_bus.validate_envelope(envelope, ALLOWED).ok

    reply = fleet_bus.create_request_envelope(
        BOT_NAME, PEER_NAME, {"text": "hi"}, in_reply_to="peer-1"
    )
    assert reply["in_reply_to"] == "peer-1"
    assert fleet_bus.validate_envelope(reply, ALLOWED).ok


def test_an_outbound_envelope_carries_the_canonical_identities():
    """`from` and `to` are subject tokens on the other side of the wire, so
    both are normalised before they are written, and the result is an envelope
    our own inbound validator accepts.
    """
    envelope = fleet_bus.create_request_envelope("YUGO", "ＶＥＣ", {"text": "hi"})

    assert envelope["from"] == BOT_NAME
    assert envelope["to"] == PEER_NAME
    assert envelope["kind"] == "text_message"
    assert envelope["envelope_version"] == 1
    assert envelope["ts"].endswith("Z")


# ---------- dispatch: what reaches the wire (real broker) ----------


class OutboundHarness:
    """A running FleetBus whose session hook returns a scripted reply."""

    def __init__(self, bus, task, audit_path: Path, injected: list, replies: dict):
        self.bus = bus
        self.task = task
        self.audit_path = audit_path
        self.injected = injected
        self.replies = replies

    def lines(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [
            json.loads(raw)
            for raw in self.audit_path.read_text(encoding="utf-8").splitlines()
            if raw.strip()
        ]

    def drops(self, reason: str) -> list[dict]:
        return [line for line in self.lines() if line.get("reason") == reason]

    async def wait_for(self, predicate, timeout: float = 20.0, what: str = "condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate(self.lines()):
                return self.lines()
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"timed out after {timeout}s waiting for {what}; audit log so far:\n"
            + "\n".join(json.dumps(line) for line in self.lines())
        )

    async def wait_for_turn(self):
        await self.wait_for(
            lambda lines: any(
                line["dir"] == "in" and "req_id" in line for line in lines
            ),
            what="a turn to run",
        )

    async def stop(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class Inbox:
    """Collects the envelopes that actually land on a set of subjects."""

    def __init__(self, client):
        self._client = client
        self.received: list[tuple[str, dict]] = []

    async def watch(self, *subjects: str) -> None:
        async def _collect(msg):
            self.received.append((msg.subject, json.loads(msg.data)))

        for subject in subjects:
            await self._client.subscribe(subject, cb=_collect)
        await self._client.flush()

    def on(self, subject: str) -> list[dict]:
        return [envelope for seen, envelope in self.received if seen == subject]

    async def wait_for(self, count: int, timeout: float = 20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.received) >= count:
                return self.received
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"timed out waiting for {count} envelope(s); saw "
            f"{[subject for subject, _ in self.received]}"
        )


@pytest.fixture
def outbound_bus(nats_server, tmp_path):  # noqa: F811 — pytest fixture injection
    """FleetBus wired to the throwaway server with a scripted session hook."""
    started: list[OutboundHarness] = []

    def _make(reply="ack") -> OutboundHarness:
        audit_path = tmp_path / f"outbound-audit-{len(started)}.jsonl"
        injected: list[dict] = []
        replies: dict = {"reply": reply}

        async def _hook(envelope, req_id):
            injected.append(envelope)
            reply = replies["reply"]
            # A callable script is how a test drives a DIFFERENT reply per
            # turn — which is what any self-triggering loop needs.
            return reply(envelope) if callable(reply) else reply

        config = fleet_bus.FleetBusConfig(
            bot_name=BOT_NAME,
            url=nats_server.url,
            user=BOT_NAME,
            password=BOT_PASSWORD,
            allowed_from=ALLOWED,
            plugin_version="0.3c-test",
            audit_log_path=str(audit_path),
            heartbeat_interval_s=HEARTBEAT_S,
            reconnect_time_wait_s=RECONNECT_WAIT_S,
        )
        bus = fleet_bus.FleetBus(
            config, fleet_bus.AuditLog(str(audit_path)), on_envelope=_hook
        )
        harness = OutboundHarness(
            bus, asyncio.create_task(bus.run()), audit_path, injected, replies
        )
        started.append(harness)
        return harness

    yield _make

    for harness in started:
        if not harness.task.done():
            harness.task.cancel()


async def _peer_client(nats_server):  # noqa: F811
    import nats

    return await nats.connect(
        servers=[nats_server.url], user=PEER_NAME, password=PEER_PASSWORD
    )


async def _connected(harness: OutboundHarness) -> None:
    await harness.wait_for(
        lambda lines: any(line.get("event") == "connected" for line in lines),
        what="connected",
    )


@pytest.mark.asyncio
async def test_a_bus_turn_answers_the_sender_automatically(
    outbound_bus, nats_server  # noqa: F811
):
    """The slice, in one test: a turn driven by an inbound envelope publishes
    its answer back to whoever sent it, with no `<BUS>` tag involved.

    The reply is an ordinary `.request` envelope carrying `in_reply_to` (SPEC
    §7 — there is no `.result` subject class), the id is fresh, and `from`/`to`
    are the two canonical identities the right way round.

    Mutation this catches: dropping the auto-reply (the sender waits forever),
    reusing the inbound id instead of minting one, and addressing the reply to
    ourselves.
    """
    harness = outbound_bus(reply="rebased, tests green")
    await _connected(harness)
    peer = await _peer_client(nats_server)
    inbox = Inbox(peer)
    try:
        await inbox.watch(f"fleet.{PEER_NAME}.request", f"fleet.{PEER_NAME}.result")
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="ask-1"))
        )
        await peer.flush()
        await inbox.wait_for(1)
        await asyncio.sleep(0.3)  # a second, unexpected publish would land here
    finally:
        await peer.close()

    assert [subject for subject, _ in inbox.received] == [f"fleet.{PEER_NAME}.request"]
    answer = inbox.on(f"fleet.{PEER_NAME}.request")[0]
    assert answer["from"] == BOT_NAME
    assert answer["to"] == PEER_NAME
    assert answer["in_reply_to"] == "ask-1"
    assert answer["id"] != "ask-1"
    assert answer["kind"] == "text_message"
    assert answer["payload"] == {"text": "rebased, tests green"}
    assert fleet_bus.validate_envelope(answer, ALLOWED).ok

    out = [line for line in harness.lines() if line["dir"] == "out"]
    reply_line = next(
        line for line in out if line["subject"] == f"fleet.{PEER_NAME}.request"
    )
    assert reply_line["id"] == answer["id"]
    assert reply_line["in_reply_to"] == "ask-1"
    turn = next(line for line in harness.lines() if line["dir"] == "in" and "req_id" in line)
    assert reply_line["req_id"] == turn["req_id"]
    await harness.stop()


@pytest.mark.asyncio
async def test_a_tag_addresses_a_third_party_while_the_sender_still_gets_an_answer(
    outbound_bus, nats_server  # noqa: F811
):
    """What the tag is FOR. `<BUS to="ohm">` is not a reply — it is this bot
    starting a conversation with someone else, so it goes out with a fresh id
    and NO `in_reply_to`, and it does not consume the sender's answer.

    The stripped body is the other half: the sender asked a question, and the
    answer they receive must not contain this bot's instructions to a third.

    Mutation this catches: stamping `in_reply_to` on the tag envelope (which,
    under the loop guard, would cost the third party their own auto-reply and
    silently end the conversation), publishing the tag to the sender instead
    of to the named peer, and skipping `strip_bus_tags` (raw markup ships).
    """
    harness = outbound_bus(
        reply='on it. <BUS to="ohm">please review PR 12</BUS> pinging Ohm now.'
    )
    await _connected(harness)
    peer = await _peer_client(nats_server)
    inbox = Inbox(peer)
    try:
        await inbox.watch(
            f"fleet.{PEER_NAME}.request", f"fleet.{THIRD_NAME}.request"
        )
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="ask-2"))
        )
        await peer.flush()
        await inbox.wait_for(2)
        await asyncio.sleep(0.3)
    finally:
        await peer.close()

    third = inbox.on(f"fleet.{THIRD_NAME}.request")
    assert len(third) == 1
    assert third[0]["to"] == THIRD_NAME
    assert third[0]["from"] == BOT_NAME
    assert third[0]["payload"] == {"text": "please review PR 12"}
    assert "in_reply_to" not in third[0]

    answer = inbox.on(f"fleet.{PEER_NAME}.request")
    assert len(answer) == 1
    assert answer[0]["in_reply_to"] == "ask-2"
    assert answer[0]["payload"] == {"text": "on it.  pinging Ohm now."}
    assert "<BUS" not in answer[0]["payload"]["text"]
    assert third[0]["id"] != answer[0]["id"]
    await harness.stop()


@pytest.mark.parametrize(
    "label,reply,expected_reason",
    [
        (
            "not-in-the-manifest",
            f'<BUS to="{STRANGER_NAME}">secrets</BUS> told them.',
            "invalid_to",
        ),
        ("no-recipient", "<BUS>who?</BUS> told them.", "invalid_to"),
        (
            "not-a-bot-name",
            '<BUS to="fleet.*">everyone</BUS> told them.',
            "invalid_to",
        ),
        (
            "unparseable-tag",
            "<BUS to=stranger>secrets</BUS> told them.",
            fleet_bus.REJECT_TAG,
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_tag_we_cannot_honour_publishes_nothing_and_says_why(
    outbound_bus, nats_server, label, reply, expected_reason  # noqa: F811
):
    """The manifest is the fleet roster, and `to` becomes a NATS subject
    token. A tag naming a bot the manifest does not list, naming nobody, or
    naming something that is not a bot name at all must not reach the wire —
    and the drop has to be visible, or "my bot ignored me" has no explanation
    anywhere.

    The control is the auto-reply: it MUST still go out in every case, so a
    dispatcher that publishes nothing at all cannot pass this test.

    Mutation this catches: dropping the `recipient not in allowed_from` half
    of the publish gate (the stranger case publishes to
    `fleet.stranger.request`), and dropping the audit line (silent loss).
    """
    harness = outbound_bus(reply=reply)
    await _connected(harness)
    peer = await _peer_client(nats_server)
    inbox = Inbox(peer)
    try:
        await inbox.watch(
            f"fleet.{PEER_NAME}.request",
            f"fleet.{STRANGER_NAME}.request",
            f"fleet.{THIRD_NAME}.request",
        )
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id=f"ask-{label}"))
        )
        await peer.flush()
        await inbox.wait_for(1)
        await asyncio.sleep(0.3)
    finally:
        await peer.close()

    published = [subject for subject, _ in inbox.received]
    assert published == [f"fleet.{PEER_NAME}.request"], (
        f"{label}: an unhonourable tag reached the wire on {published}"
    )
    assert harness.drops(expected_reason), (
        f"{label}: no {expected_reason} audit line; lines were "
        f"{[line.get('reason') for line in harness.lines()]}"
    )
    answer = inbox.on(f"fleet.{PEER_NAME}.request")[0]
    assert answer["payload"]["text"] == "told them."
    await harness.stop()


@pytest.mark.asyncio
async def test_a_broken_tag_costs_one_tag_not_the_turn_or_the_subscription(
    outbound_bus, nats_server  # noqa: F811
):
    """A tag the model got wrong is one lost message. The sender still gets an
    answer, the markup is not in it, and the NEXT envelope still drives a turn.

    Mutation this catches: letting the parse fault propagate out of
    `_publish_turn_output` — nats-py routes a raising callback to `error_cb`,
    so the bot keeps heartbeating and looking healthy while every envelope
    from then on dies the same way.
    """
    harness = outbound_bus(reply='sure thing <BUS to="ohm">unterminated')
    await _connected(harness)
    peer = await _peer_client(nats_server)
    inbox = Inbox(peer)
    try:
        await inbox.watch(
            f"fleet.{PEER_NAME}.request", f"fleet.{THIRD_NAME}.request"
        )
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="broken"))
        )
        await peer.flush()
        await inbox.wait_for(1)

        harness.replies["reply"] = "second answer"
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="after-broken"))
        )
        await peer.flush()
        await inbox.wait_for(2)
    finally:
        await peer.close()

    assert inbox.on(f"fleet.{THIRD_NAME}.request") == []
    answers = inbox.on(f"fleet.{PEER_NAME}.request")
    assert [answer["in_reply_to"] for answer in answers] == ["broken", "after-broken"]
    assert answers[0]["payload"] == {"text": "sure thing"}
    assert answers[1]["payload"] == {"text": "second answer"}
    rejected = harness.drops(fleet_bus.REJECT_TAG)
    assert [line["cause"] for line in rejected] == ["unclosed"]
    assert "<BUS" in rejected[0]["raw"]
    assert not harness.task.done()
    await harness.stop()


@pytest.mark.asyncio
async def test_a_turn_with_nothing_to_say_publishes_nothing_and_reports_nothing(
    outbound_bus, nats_server  # noqa: F811
):
    """The hook contract is `str | None`, and None means the turn produced no
    text. That is silence, not a fault: nothing goes out and nothing is
    reported as lost.

    Mutation this catches: dropping the `isinstance(reply, str)` check, which
    turns a quiet turn into a `yugo_publish_failed` line describing a TypeError
    — an error where there is no error.
    """
    harness = outbound_bus(reply=None)
    await _connected(harness)
    peer = await _peer_client(nats_server)
    inbox = Inbox(peer)
    try:
        await inbox.watch(f"fleet.{PEER_NAME}.request")
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="silent"))
        )
        await peer.flush()
        await harness.wait_for_turn()
        await asyncio.sleep(0.3)
    finally:
        await peer.close()

    assert inbox.received == []
    assert [line for line in harness.lines() if line["dir"] == "drop"] == []
    await harness.stop()


@pytest.mark.asyncio
async def test_a_tag_with_an_empty_body_sends_nobody_an_empty_message(
    outbound_bus, nats_server  # noqa: F811
):
    """A tag naming a real peer and carrying no text is a message with nothing
    in it — and on the receiving side it is not free: it drives a whole LLM
    turn over an empty payload.

    The control is the sender's answer, which must still go out.

    Mutation this catches: dropping the empty-body check (`fleet.ohm.request`
    receives `{"text": ""}`).
    """
    harness = outbound_bus(reply='nothing to pass on. <BUS to="ohm">   </BUS>')
    await _connected(harness)
    peer = await _peer_client(nats_server)
    inbox = Inbox(peer)
    try:
        await inbox.watch(
            f"fleet.{PEER_NAME}.request", f"fleet.{THIRD_NAME}.request"
        )
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="empty-body"))
        )
        await peer.flush()
        await inbox.wait_for(1)
        await asyncio.sleep(0.3)
    finally:
        await peer.close()

    assert inbox.on(f"fleet.{THIRD_NAME}.request") == []
    assert inbox.on(f"fleet.{PEER_NAME}.request")[0]["payload"] == {
        "text": "nothing to pass on."
    }
    rejected = harness.drops(fleet_bus.REJECT_TAG)
    assert [line["cause"] for line in rejected] == ["empty_body"]
    await harness.stop()


@pytest.mark.asyncio
async def test_a_turn_that_is_nothing_but_tags_sends_no_empty_answer(
    outbound_bus, nats_server  # noqa: F811
):
    """A turn that said everything it had to say to a third party has nothing
    left for the sender, and an empty `text_message` would just be one more
    envelope for them to run a turn over.

    Mutation this catches: publishing the stripped-empty reply anyway.
    """
    harness = outbound_bus(reply='  <BUS to="ohm">handled over here</BUS>  ')
    await _connected(harness)
    peer = await _peer_client(nats_server)
    inbox = Inbox(peer)
    try:
        await inbox.watch(
            f"fleet.{PEER_NAME}.request", f"fleet.{THIRD_NAME}.request"
        )
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="tags-only"))
        )
        await peer.flush()
        await inbox.wait_for(1)
        await asyncio.sleep(0.3)
    finally:
        await peer.close()

    assert [subject for subject, _ in inbox.received] == [f"fleet.{THIRD_NAME}.request"]
    suppressed = harness.drops(fleet_bus.REJECT_AUTOREPLY_SUPPRESSED)
    assert [line["cause"] for line in suppressed] == ["empty_reply"]
    await harness.stop()


# ---------- the loop guard ----------


@pytest.mark.asyncio
async def test_an_envelope_that_is_itself_a_reply_is_not_auto_answered(
    outbound_bus, nats_server  # noqa: F811
):
    """The loop guard, stated on one bot: an inbound envelope carrying
    `in_reply_to` still drives a turn — the model must see the answer it was
    waiting for — but that turn does not automatically answer back.

    The control is the second publish: the same envelope WITHOUT `in_reply_to`
    must still be answered, so a bot that has simply stopped publishing cannot
    pass this test.

    Mutation this catches: removing the `in_reply_to` check, and inverting it.
    """
    harness = outbound_bus(reply="thanks!")
    await _connected(harness)
    peer = await _peer_client(nats_server)
    inbox = Inbox(peer)
    try:
        await inbox.watch(f"fleet.{PEER_NAME}.request")
        await peer.publish(
            f"fleet.{BOT_NAME}.request",
            _encode(_peer_envelope(id="a-reply", in_reply_to="something-we-sent")),
        )
        await peer.flush()
        await harness.wait_for(
            lambda lines: any(
                line.get("reason") == fleet_bus.REJECT_AUTOREPLY_SUPPRESSED
                for line in lines
            ),
            what="the suppressed auto-reply",
        )
        assert inbox.received == [], (
            "a reply was auto-answered — two bots doing this is an infinite "
            f"exchange: {inbox.received}"
        )
        assert [envelope["id"] for envelope in harness.injected] == ["a-reply"], (
            "the turn itself must still run; the guard is about answering, "
            "not about reading"
        )

        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="a-question"))
        )
        await peer.flush()
        await inbox.wait_for(1)
    finally:
        await peer.close()

    assert [envelope["in_reply_to"] for envelope in inbox.on(f"fleet.{PEER_NAME}.request")] == [
        "a-question"
    ]
    suppressed = harness.drops(fleet_bus.REJECT_AUTOREPLY_SUPPRESSED)
    assert [line["cause"] for line in suppressed] == ["in_reply_to"]
    assert suppressed[0]["id"] == "a-reply"
    await harness.stop()


@pytest.mark.parametrize(
    "label,self_name",
    [
        ("exact", BOT_NAME),
        ("case-folded", BOT_NAME.upper()),
        ("width-folded", "\uff59\uff55\uff47\uff4f"),
    ],
)
@pytest.mark.asyncio
async def test_a_self_addressed_tag_cannot_drive_this_bot_in_circles(
    outbound_bus, nats_server, label, self_name  # noqa: F811
):
    """The other loop, and the one the `in_reply_to` guard cannot see.

    Our own name is in the manifest, so `<BUS to="<self>">` publishes onto
    `fleet.<self>.request` — the subject we are subscribed to — as a FRESH
    envelope with no `in_reply_to`. Tag envelopes omit that field by design,
    so the reply-suppression guard has nothing to key on: the bot drives
    itself, one model turn per hop, for as long as the model repeats the tag.

    Same shape as `test_two_bots_do_not_ping_pong`, and the FUSE does the same
    job: the hook stops emitting the tag after `FUSE` turns, so the mutated
    run fails on a count instead of spinning forever.

    The width- and case-folded parametrisations are not decoration. `to` is
    normalised on the way to a subject, so `<BUS to="YUGO">` reaches exactly
    the same subject as `<BUS to="yugo">`; a guard that compares the raw
    attribute rejects the obvious spelling and passes the loop through under
    the other two.

    The control is the sender's answer, which must still go out — a bot that
    has simply stopped publishing cannot pass this test.

    Mutation this catches: removing the self-address check (the turn count
    runs to the fuse), and comparing without `normalize_bot_name` (the two
    folded cases run to the fuse).
    """
    FUSE = 20
    turns: list[str] = []

    def _reply(envelope):
        turns.append(envelope["id"])
        if len(turns) > FUSE:
            return "fuse blown"
        return f'thinking. <BUS to="{self_name}">keep going</BUS>'

    harness = outbound_bus(reply=_reply)
    await _connected(harness)
    peer = await _peer_client(nats_server)
    inbox = Inbox(peer)
    try:
        await inbox.watch(f"fleet.{PEER_NAME}.request")
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id=f"seed-{label}"))
        )
        await peer.flush()
        await inbox.wait_for(1)
        # Let the loop run if it is going to; at these timings a self-driving
        # bot blows the fuse in well under a second.
        await asyncio.sleep(1.0)
    finally:
        await peer.close()

    assert turns == [f"seed-{label}"], (
        f"{label}: the bot re-triggered itself through its own tag: {turns}"
    )
    out_subjects = [line["subject"] for line in harness.lines() if line["dir"] == "out"]
    assert f"fleet.{BOT_NAME}.request" not in out_subjects, (
        f"{label}: a tag published onto our own request subject: {out_subjects}"
    )
    rejected = harness.drops(fleet_bus.REJECT_TAG)
    assert [line["cause"] for line in rejected] == ["self_addressed"], label
    assert inbox.on(f"fleet.{PEER_NAME}.request")[0]["payload"] == {"text": "thinking."}
    await harness.stop()


@pytest.mark.asyncio
async def test_two_bots_do_not_ping_pong(nats_server, tmp_path):  # noqa: F811
    """Two real adapters, one real broker, one seed envelope.

    Auto-reply on both ends is a closed loop: yugo answers vec, vec answers
    the answer, forever, at provider prices. The exchange here is the whole
    failure mode in miniature — the only thing that ends it is the guard.

    The FUSE matters as much as the assertion. Each hook stops replying once
    the pair has taken `FUSE` turns, so the mutated run FAILS on a count
    instead of hanging forever or filling the disk; a test that can only prove
    a guard by never terminating proves nothing on the day it breaks.

    Expected exchange with the guard: vec's seed -> yugo's turn -> yugo's
    answer -> vec's turn -> silence. Two turns, one published answer.

    Mutation this catches: removing the `in_reply_to` guard — the pair then
    runs to the fuse (20 turns in well under a second) and the count assertion
    fails.
    """
    FUSE = 20
    turns: list[str] = []

    def _make(name: str, password: str) -> OutboundHarness:
        audit_path = tmp_path / f"pingpong-{name}.jsonl"
        injected: list[dict] = []

        async def _hook(envelope, req_id):
            turns.append(name)
            if len(turns) > FUSE:
                # Blown fuse: stop feeding the loop so the failure is a count,
                # not a hung test.
                return None
            injected.append(envelope)
            return f"{name} acknowledges {envelope['id']}"

        config = fleet_bus.FleetBusConfig(
            bot_name=name,
            url=nats_server.url,
            user=name,
            password=password,
            allowed_from=ALLOWED,
            plugin_version="0.3c-test",
            audit_log_path=str(audit_path),
            heartbeat_interval_s=HEARTBEAT_S,
            reconnect_time_wait_s=RECONNECT_WAIT_S,
        )
        bus = fleet_bus.FleetBus(
            config, fleet_bus.AuditLog(str(audit_path)), on_envelope=_hook
        )
        return OutboundHarness(
            bus, asyncio.create_task(bus.run()), audit_path, injected, {}
        )

    yugo = _make(BOT_NAME, BOT_PASSWORD)
    vec = _make(PEER_NAME, PEER_PASSWORD)
    try:
        await _connected(yugo)
        await _connected(vec)

        # vec opens the conversation by hand — a plain question, no
        # `in_reply_to`, exactly what `bus_request(to=…)` puts on the wire.
        assert await vec.bus.publish_request(BOT_NAME, {"text": "hello yugo"})

        await yugo.wait_for_turn()
        await vec.wait_for_turn()
        # Let the loop run if it is going to: at these timings a ping-pong
        # blows the fuse in well under a second.
        await asyncio.sleep(1.0)
    finally:
        await yugo.stop()
        await vec.stop()

    assert turns == [BOT_NAME, PEER_NAME], (
        f"the exchange did not stop after one round trip: {turns}"
    )
    yugo_out = [
        line["subject"] for line in yugo.lines() if line["dir"] == "out"
    ]
    assert yugo_out.count(f"fleet.{PEER_NAME}.request") == 1
    vec_suppressed = vec.drops(fleet_bus.REJECT_AUTOREPLY_SUPPRESSED)
    assert [line["cause"] for line in vec_suppressed] == ["in_reply_to"]


# ---------- publish faults ----------


@pytest.mark.asyncio
async def test_publishing_without_a_connection_is_audited_not_raised():
    """`_nc` is None between reconnects, and a turn can finish inside that
    window. The envelope is lost either way; what must not happen is an
    exception out of a NATS callback.

    The `error` field is asserted, not just the reason. Without the explicit
    check the publish still fails — on `None.publish`, reported as an
    `AttributeError` — and "lost because the bus was down" is the single most
    common outbound fault there is. It has to read as itself in the log, not as
    a Python bug, for the same reason `auth_rejected` has its own event.

    Mutation this catches: dropping the `_nc is None` check.
    """
    audit: list[dict] = []

    class _RecordingAudit(fleet_bus.AuditLog):
        def record(self, direction, subject, **fields):
            audit.append({"dir": direction, "subject": subject, **fields})

    config = fleet_bus.FleetBusConfig(
        bot_name=BOT_NAME,
        url="nats://127.0.0.1:1",
        user=BOT_NAME,
        password="x",
        allowed_from=ALLOWED,
        plugin_version="0.3c-test",
        audit_log_path=None,
    )
    bus = fleet_bus.FleetBus(config, _RecordingAudit(None))

    assert await bus.publish_request(PEER_NAME, {"text": "into the void"}) is False
    assert audit[0]["reason"] == fleet_bus.REJECT_PUBLISH_FAILED
    assert audit[0]["subject"] == f"fleet.{PEER_NAME}.request"
    assert audit[0]["error"] == "not connected", (
        "a publish attempted with no connection must say so — an "
        f"AttributeError from `None.publish` is not a diagnosis: {audit[0]}"
    )


@pytest.mark.asyncio
async def test_a_recipient_the_model_invented_cannot_flood_the_audit_log(
    nats_server,  # noqa: F811
):
    """`to` is model-authored and bounded by nothing, and the audit sink is a
    JSONL file the tap tails. The line has to say WHICH recipient was refused
    without letting one turn write a megabyte into it.

    Mutation this catches: passing the raw `to` into the audit record.
    """
    audit: list[dict] = []

    class _RecordingAudit(fleet_bus.AuditLog):
        def record(self, direction, subject, **fields):
            audit.append({"dir": direction, "subject": subject, **fields})

    config = fleet_bus.FleetBusConfig(
        bot_name=BOT_NAME,
        url=nats_server.url,
        user=BOT_NAME,
        password=BOT_PASSWORD,
        allowed_from=ALLOWED,
        plugin_version="0.3c-test",
        audit_log_path=None,
    )
    bus = fleet_bus.FleetBus(config, _RecordingAudit(None))

    assert await bus.publish_request("z" * 100_000, {"text": "hi"}) is False
    assert audit[0]["reason"] == "invalid_to"
    assert len(audit[0]["to"]) <= fleet_bus.AUDIT_RAW_MAX_CHARS + 1


@pytest.mark.asyncio
async def test_a_fault_in_the_dispatcher_does_not_wedge_the_subscription(
    outbound_bus, nats_server, monkeypatch  # noqa: F811
):
    """The last-resort guard around the whole publish path. Everything
    per-envelope is already absorbed by `publish_request`, so reaching it means
    a fault in the dispatch code itself — and a raising NATS callback is routed
    to `error_cb`, leaving a bot that heartbeats, looks healthy, and answers
    nobody from then on.

    Mutation this catches: removing the `try` around `_publish_turn_output`.
    """
    harness = outbound_bus(reply="answer")
    await _connected(harness)

    def _explode(text):
        raise RuntimeError("parser fell over")

    monkeypatch.setattr(fleet_bus, "find_bus_tags", _explode)
    peer = await _peer_client(nats_server)
    inbox = Inbox(peer)
    try:
        await inbox.watch(f"fleet.{PEER_NAME}.request")
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="explodes"))
        )
        await peer.flush()
        await harness.wait_for(
            lambda lines: any(
                line.get("reason") == fleet_bus.REJECT_PUBLISH_FAILED
                for line in lines
            ),
            what="the dispatch fault audit line",
        )
        monkeypatch.undo()
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="after-explosion"))
        )
        await peer.flush()
        await inbox.wait_for(1)
    finally:
        await peer.close()

    answers = inbox.on(f"fleet.{PEER_NAME}.request")
    assert [answer["in_reply_to"] for answer in answers] == ["after-explosion"]
    assert "parser fell over" in harness.drops(fleet_bus.REJECT_PUBLISH_FAILED)[0]["error"]
    assert not harness.task.done()
    await harness.stop()


@pytest.mark.asyncio
async def test_an_envelope_the_validator_refuses_never_reaches_the_wire(
    outbound_bus, nats_server  # noqa: F811
):
    """SPEC §15 3c's "per envelope validation from §5": what we publish goes
    through the same validator as what we receive, so we cannot emit an
    envelope a peer would drop. An oversize payload is the case that can
    actually happen — a model can write one.

    Mutation this catches: publishing without validating (the envelope goes
    out and the peer drops it as `envelope_too_large`, with the reject line
    on THEIR disk, not ours).
    """
    harness = outbound_bus(reply="x" * (fleet_bus.DEFAULT_MAX_ENVELOPE_BYTES + 10))
    await _connected(harness)
    peer = await _peer_client(nats_server)
    inbox = Inbox(peer)
    try:
        await inbox.watch(f"fleet.{PEER_NAME}.request")
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="huge"))
        )
        await peer.flush()
        await harness.wait_for(
            lambda lines: any(
                line.get("reason") == "envelope_too_large" for line in lines
            ),
            what="the oversize reject",
        )
        await asyncio.sleep(0.3)
    finally:
        await peer.close()

    assert inbox.received == []
    await harness.stop()

"""
Connection-lifecycle tests for the fleet-bus adapter, against a REAL
nats-server process.

These are the invariants slice 3a exists to prove. Every one of them lives in
nats-py's connection state machine or the broker's subscription bookkeeping,
so a faked `connect` would assert a mock back at itself:

    * subscribe the LIVE wire subjects (NOT `fleet.<self>.inbox` — see the
      SPEC §15 erratum E-1)
    * heartbeat loopback: publish -> broker -> subscribe -> validate -> audit
    * one client and one supervisor task no matter how often start is called
    * `subscribe` called exactly once per subject ACROSS a server restart —
      nats-py replays subscriptions itself, and re-subscribing in
      `reconnected_cb` would double-deliver forever
    * a server restart produces disconnected + reconnected and NEVER reaches
      CLOSED (the `max_reconnect_attempts` regression)
    * malformed inbound is dropped with the exact fleet-bus reject code and
      does NOT wedge the subscription for the next valid envelope
    * a fault PART WAY through the subscribe run closes the half-built client
      instead of orphaning a live, subscribed one on the bus forever
    * a beat that raises costs one beat, not the supervisor
    * wrong credentials retry forever, audited under their own event code
    * shutdown from connected AND from mid-reconnect

Timings are deliberately compressed (heartbeat 0.3s instead of 30s, reconnect
wait 0.05s instead of 5s) so an outage long enough to exhaust nats-py's
default 60 reconnect attempts fits in a few seconds. `test_fleet_bus_config`
pins the PRODUCTION defaults separately, so the compression cannot hide a
default that drifted.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

import pytest

import bot as bot_module
import fleet_bus
from test.nats_server import (  # noqa: F401 — `nats_server` is a fixture
    BOT_NAME,
    BOT_PASSWORD,
    PEER_NAME,
    PEER_PASSWORD,
    nats_server,
)

pytestmark = pytest.mark.asyncio

HEARTBEAT_S = 0.3
RECONNECT_WAIT_S = 0.05
# 0.05s per attempt x 60 default attempts = 3s. An outage comfortably past
# that is what makes the max_reconnect_attempts regression visible in a test
# that finishes in seconds rather than in the >120s the real defaults need.
OUTAGE_S = 5.0
# Distinctive on purpose: the credentials test greps the audit file for it.
WRONG_PASSWORD = "not-the-password-and-must-never-be-logged"


class BusHarness:
    """A running FleetBus plus read access to the audit log it is writing."""

    def __init__(self, bus, task, audit_path: Path) -> None:
        self.bus = bus
        self.task = task
        self.audit_path = audit_path

    def lines(self) -> list[dict]:
        if not self.audit_path.exists():
            return []
        return [
            json.loads(raw)
            for raw in self.audit_path.read_text(encoding="utf-8").splitlines()
            if raw.strip()
        ]

    def events(self) -> list[str]:
        return [line["event"] for line in self.lines() if line["dir"] == "conn"]

    async def wait_for(self, predicate, timeout: float = 20.0, what: str = "condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = self.lines()
            if predicate(lines):
                return lines
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"timed out after {timeout}s waiting for {what}; audit log so far:\n"
            + "\n".join(json.dumps(line) for line in self.lines())
        )

    async def stop(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


@pytest.fixture
def bus_factory(nats_server, tmp_path):  # noqa: F811 — pytest fixture injection
    """Build a FleetBus wired to the throwaway server, and clean it up."""
    started: list[BusHarness] = []

    def _make(**config_overrides) -> BusHarness:
        audit_path = tmp_path / f"audit-{len(started)}.jsonl"
        settings = {
            "bot_name": BOT_NAME,
            "url": nats_server.url,
            "user": BOT_NAME,
            "password": BOT_PASSWORD,
            "allowed_from": frozenset({BOT_NAME, PEER_NAME}),
            "plugin_version": "0.3a-test",
            "audit_log_path": str(audit_path),
            "heartbeat_interval_s": HEARTBEAT_S,
            "reconnect_time_wait_s": RECONNECT_WAIT_S,
        }
        # Merged, not splatted alongside the defaults: a test that overrides
        # `password` (the credentials case) would otherwise be a duplicate
        # keyword argument.
        settings.update(config_overrides)
        config = fleet_bus.FleetBusConfig(**settings)
        bus = fleet_bus.FleetBus(config, fleet_bus.AuditLog(str(audit_path)))
        harness = BusHarness(bus, asyncio.create_task(bus.run()), audit_path)
        started.append(harness)
        return harness

    yield _make

    # pytest-asyncio tears the loop down once the test coroutine returns, so a
    # supervisor can only be AWAITED to a stop from inside the test — every
    # test does that itself via `harness.stop()` or `bot.close()`. This is the
    # backstop for a test that failed before it got there.
    for harness in started:
        if not harness.task.done():
            harness.task.cancel()


async def _peer_client(nats_server):  # noqa: F811
    """A second connection standing in for another fleet bot."""
    import nats

    return await nats.connect(
        servers=[nats_server.url], user=PEER_NAME, password=PEER_PASSWORD
    )


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


# ---------- subjects ----------


async def test_subscribes_the_live_wire_subjects_not_inbox(bus_factory, nats_server):  # noqa: F811
    """SPEC §15 erratum E-1.

    `fleet.<self>.inbox` does not exist until FB-1 lands, and per-user authz
    does not grant subscribe on it. nats-py answers a permissions violation by
    calling `error_cb` and RETURNING — the connection stays up. A bot that
    subscribed `.inbox` today would be connected, heartbeating, and deaf,
    while every lifecycle assertion below still passed.
    """
    harness = bus_factory()
    await harness.wait_for(
        lambda lines: any(line.get("event") == "connected" for line in lines),
        what="connected",
    )
    connected = next(line for line in harness.lines() if line.get("event") == "connected")
    assert connected["subjects"] == [
        f"fleet.{BOT_NAME}.request",
        f"fleet.{BOT_NAME}.result",
        f"fleet.{BOT_NAME}.status",
        "fleet.broadcast.>",
    ]
    assert not any("inbox" in subject for subject in connected["subjects"])
    await harness.stop()


@pytest.mark.parametrize(
    "subject_key",
    ["request", "result", "status", "broadcast"],
)
async def test_every_subscribed_subject_actually_delivers(
    bus_factory, nats_server, subject_key  # noqa: F811
):
    """Class coverage: each of the four subjects is asserted to deliver, not
    just the one the heartbeat happens to use.

    A subject that is in the `subjects` tuple but never really subscribed
    (typo, wrong wildcard) would pass the list assertion above and fail here.
    """
    harness = bus_factory()
    await harness.wait_for(
        lambda lines: any(line.get("event") == "connected" for line in lines),
        what="connected",
    )
    subject = {
        "request": f"fleet.{BOT_NAME}.request",
        "result": f"fleet.{BOT_NAME}.result",
        "status": f"fleet.{BOT_NAME}.status",
        "broadcast": "fleet.broadcast.heartbeat.vec",
    }[subject_key]

    peer = await _peer_client(nats_server)
    try:
        await peer.publish(subject, _encode(_peer_envelope(id=f"probe-{subject_key}")))
        await peer.flush()
        await harness.wait_for(
            lambda lines: any(
                line["dir"] == "in" and line.get("id") == f"probe-{subject_key}"
                for line in lines
            ),
            what=f"inbound envelope on {subject}",
        )
    finally:
        await peer.close()
    delivered = next(
        line for line in harness.lines() if line.get("id") == f"probe-{subject_key}"
    )
    assert delivered["subject"] == subject
    assert delivered["from"] == PEER_NAME
    await harness.stop()


# ---------- heartbeat ----------


async def test_heartbeat_loopback_publish_to_audit(bus_factory):
    """End-to-end with no peer on the bus at all: the adapter subscribes its
    own `.status`, publishes its heartbeat there, and `no_echo` defaults to
    False, so the beat comes back through the broker and down the normal
    decode -> validate -> audit path.

    That round trip is the whole point of the slice — it proves publish,
    subscribe, validation and audit are all really wired, not just
    constructed.
    """
    harness = bus_factory()
    await harness.wait_for(
        lambda lines: any(line["dir"] == "in" for line in lines),
        what="heartbeat loopback",
    )
    lines = harness.lines()
    out = [line for line in lines if line["dir"] == "out"]
    inbound = [line for line in lines if line["dir"] == "in"]
    assert out and inbound
    assert out[0]["subject"] == f"fleet.{BOT_NAME}.status"
    assert inbound[0]["subject"] == f"fleet.{BOT_NAME}.status"
    assert inbound[0]["kind"] == "status_heartbeat"
    assert inbound[0]["from"] == BOT_NAME
    # Same envelope, not a coincidentally-shaped one.
    assert inbound[0]["id"] == out[0]["id"]
    await harness.stop()


async def test_heartbeat_payload_on_the_wire(bus_factory, nats_server):  # noqa: F811
    """Read the beat off the broker with an independent subscriber and assert
    the payload fields the tap and coordinator consume. Reading our own audit
    log instead would only prove we wrote what we meant to write."""
    peer = await _peer_client(nats_server)
    received: list[dict] = []

    async def _collect(msg):
        received.append(json.loads(msg.data))

    await peer.subscribe(f"fleet.{BOT_NAME}.status", cb=_collect)
    await peer.flush()
    harness = bus_factory()
    try:
        deadline = time.monotonic() + 20
        while not received and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert received, "no heartbeat observed on the wire"
        envelope = received[0]
        assert envelope["envelope_version"] == 1
        assert envelope["from"] == BOT_NAME
        assert envelope["kind"] == "status_heartbeat"
        payload = envelope["payload"]
        assert payload["online"] is True
        assert payload["plugin_version"] == "0.3a-test"
        assert isinstance(payload["pid"], int)
        assert payload["process_alive_ts"] == envelope["ts"]
    finally:
        await peer.close()
        await harness.stop()


async def test_a_failed_heartbeat_publish_is_audited_and_the_loop_survives(
    monkeypatch, bus_factory  # noqa: F811
):
    """A beat that raises must cost one beat, not the bus.

    Mid-outage nats-py buffers publishes and raises once the pending buffer is
    full. Without the `try` around the publish that exception unwinds
    `_publish_heartbeat` -> `_heartbeat_loop` -> `run`, and `run` has no
    `except` on that path: the supervisor task ends, and the bot is off the
    fleet bus for the rest of the process's life while still answering Discord
    perfectly — the failure mode with no symptom.
    """
    import nats.aio.client

    real_publish = nats.aio.client.Client.publish
    failures = {"left": 2}

    async def flaky_publish(self, subject, *args, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("pending buffer full")
        return await real_publish(self, subject, *args, **kwargs)

    monkeypatch.setattr(nats.aio.client.Client, "publish", flaky_publish)

    harness = bus_factory()
    await harness.wait_for(
        lambda lines: len(
            [line for line in lines if line.get("event") == "heartbeat_failed"]
        )
        >= 2,
        what="two heartbeat_failed audit lines",
    )
    await harness.wait_for(
        lambda lines: any(line["dir"] == "in" for line in lines),
        what="heartbeat loopback resuming after the failed beats",
    )
    assert not harness.task.done(), "a failed beat ended the supervisor"
    failed = next(x for x in harness.lines() if x.get("event") == "heartbeat_failed")
    assert "pending buffer full" in failed["error"]
    assert failed["subject"] == f"fleet.{BOT_NAME}.status"
    await harness.stop()


async def test_heartbeat_repeats_on_the_configured_cadence(bus_factory):
    """Cadence, not just "at least one beat".

    Catches both directions: a dropped `await asyncio.sleep(interval)` floods
    the bus (gaps collapse to ~0), and a beat that only fires once on connect
    never produces a second gap at all.
    """
    harness = bus_factory()
    await harness.wait_for(
        lambda lines: len([x for x in lines if x["dir"] == "out"]) >= 4,
        what="four heartbeats",
    )
    await harness.stop()
    stamps = [
        datetime.fromisoformat(line["ts"].replace("Z", "+00:00")).timestamp()
        for line in harness.lines()
        if line["dir"] == "out"
    ]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(gaps) >= 3
    for gap in gaps:
        assert HEARTBEAT_S * 0.5 <= gap <= HEARTBEAT_S * 2.5, (
            f"heartbeat gaps {gaps} do not match the configured "
            f"{HEARTBEAT_S}s cadence"
        )


# ---------- start idempotency ----------


async def test_lifecycle_start_twice_yields_one_client_and_one_task(
    monkeypatch, nats_server, tmp_path  # noqa: F811
):
    """`setup_hook` is the correct start site precisely BECAUSE `on_ready`
    re-fires on gateway resume (see the `_lock` comment in bot.py). Guard the
    idempotency anyway: a second supervisor would mean two NATS clients, two
    heartbeats on the same subject and a doubled audit trail.
    """
    import nats

    connects: list[str] = []
    real_connect = nats.connect

    async def counting_connect(*args, **kwargs):
        connects.append(kwargs.get("name", "?"))
        return await real_connect(*args, **kwargs)

    monkeypatch.setattr(nats, "connect", counting_connect)

    audit_path = tmp_path / "wiring-audit.jsonl"
    manifest = tmp_path / "fleet-manifest.yaml"
    manifest.write_text(f"bot_names:\n  - {BOT_NAME}\n", encoding="utf-8")
    config = fleet_bus.FleetBusConfig(
        bot_name=BOT_NAME,
        url=nats_server.url,
        user=BOT_NAME,
        password=BOT_PASSWORD,
        allowed_from=frozenset({BOT_NAME}),
        plugin_version="0.3a-test",
        audit_log_path=str(audit_path),
        heartbeat_interval_s=HEARTBEAT_S,
        reconnect_time_wait_s=RECONNECT_WAIT_S,
    )
    monkeypatch.setattr(bot_module, "BUS_CONFIG", config)
    monkeypatch.setattr(bot_module, "_bus", None)
    monkeypatch.setattr(bot_module, "_bus_task", None)

    try:
        first = bot_module.start_bus()
        second = bot_module.start_bus()
        assert first is second
        assert bot_module._bus is not None
        # Give the single supervisor time to connect and beat a few times.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if audit_path.exists() and sum(
                1
                for raw in audit_path.read_text(encoding="utf-8").splitlines()
                if json.loads(raw)["dir"] == "out"
            ) >= 3:
                break
            await asyncio.sleep(0.05)
        assert connects == [BOT_NAME], f"expected exactly one connect, got {connects}"
    finally:
        await bot_module.stop_bus()


# ---------- reconnect ----------


async def test_server_restart_reconnects_and_never_reaches_closed(
    bus_factory, nats_server  # noqa: F811
):
    """Blocker-2 regression test.

    nats-py's default is `max_reconnect_attempts=60` at a flat 2s wait; once
    exhausted it discards the server, empties the pool and calls `close()` —
    permanently, restart-required. This test compresses the wait to 0.05s so a
    5s outage burns well past 60 attempts. With the default the audit log
    shows `closed` and never `reconnected`; with -1 (infinite) it shows
    `disconnected` then `reconnected` and the client is still open.
    """
    harness = bus_factory()
    await harness.wait_for(
        lambda lines: any(line.get("event") == "connected" for line in lines),
        what="initial connect",
    )

    nats_server.stop()
    await harness.wait_for(
        lambda lines: any(line.get("event") == "disconnected" for line in lines),
        what="disconnected audit line",
    )
    await asyncio.sleep(OUTAGE_S)
    nats_server.start()

    await harness.wait_for(
        lambda lines: any(line.get("event") == "reconnected" for line in lines),
        what="reconnected audit line",
    )
    assert "closed" not in harness.events(), (
        "connection reached CLOSED during an outage — nats-py abandoned the "
        "server, which leaves the bot bus-less until the process restarts. "
        f"events={harness.events()}"
    )
    assert harness.bus.connection is not None
    assert harness.bus.connection.is_closed is False
    await harness.stop()


async def test_subscribe_called_once_per_subject_across_a_restart(
    monkeypatch, bus_factory, nats_server  # noqa: F811
):
    """nats-py replays subscriptions itself on reconnect (it rewrites the SUB
    commands from its own `_subs` map). Re-subscribing in `reconnected_cb`
    would therefore DOUBLE-deliver every inbound envelope, permanently, and
    the only symptom would be duplicated work by the bot.

    Counted at `Client.subscribe`, so only OUR calls register — the library's
    replay writes to the transport directly and is invisible here. Heartbeat
    loopback resuming after the restart is the proof the replay happened.
    """
    import nats.aio.client

    calls: list[str] = []
    real_subscribe = nats.aio.client.Client.subscribe

    async def counting_subscribe(self, subject, *args, **kwargs):
        calls.append(subject)
        return await real_subscribe(self, subject, *args, **kwargs)

    monkeypatch.setattr(nats.aio.client.Client, "subscribe", counting_subscribe)

    harness = bus_factory()
    await harness.wait_for(
        lambda lines: any(line["dir"] == "in" for line in lines),
        what="pre-restart heartbeat loopback",
    )
    assert sorted(calls) == sorted(harness.bus.subjects)

    beats_before = len([line for line in harness.lines() if line["dir"] == "in"])
    nats_server.stop()
    await asyncio.sleep(0.5)
    nats_server.start()
    await harness.wait_for(
        lambda lines: any(line.get("event") == "reconnected" for line in lines),
        what="reconnect",
    )
    await harness.wait_for(
        lambda lines: len([x for x in lines if x["dir"] == "in"]) > beats_before + 1,
        what="heartbeat loopback resuming after reconnect",
    )
    await harness.stop()

    assert sorted(calls) == sorted(harness.bus.subjects), (
        "subscribe() was called again after reconnect — nats-py already "
        f"replays subscriptions, so this double-delivers. calls={calls}"
    )


# ---------- credentials ----------


async def test_wrong_credentials_retry_forever_audited_as_auth_rejected(bus_factory):
    """Bad password is a config fault, and it still must not be fatal.

    Failing closed on it would be worse than the noise: a token rotated a beat
    early would take the Discord lane down with the bus lane. So the retry is
    infinite by design — and infinite means ~10 attempts a second at this
    suite's compressed `reconnect_time_wait`, ~10^4 audit lines a day at the
    production 5s, into a file with no rotation.

    What makes that survivable is the code on the line. `auth_rejected` is
    greppable and means "fix the credential"; a generic `error` line is
    indistinguishable from the reconnect churn of a broker that is merely
    down, which is the one thing the operator must NOT do anything about.
    """
    harness = bus_factory(password=WRONG_PASSWORD)
    await harness.wait_for(
        lambda lines: len(
            [line for line in lines if line.get("event") == "auth_rejected"]
        )
        >= 3,
        what="repeated auth_rejected audit lines",
    )
    # Still retrying, not given up, and never authenticated.
    assert not harness.task.done()
    events = harness.events()
    assert "connected" not in events
    assert not any(line["dir"] == "out" for line in harness.lines())

    # No authz rejection may hide under the generic code — that is the whole
    # point of splitting the event.
    generic = [
        line
        for line in harness.lines()
        if line.get("event") == "error"
        and "authorization" in line["error"].lower()
    ]
    assert generic == [], f"authz failures still audited as generic errors: {generic}"

    # The credential itself must never reach the audit file: it is 0600, but
    # it is also the file that gets pasted into a channel when a bot is sick.
    assert WRONG_PASSWORD not in harness.audit_path.read_text(encoding="utf-8")
    await harness.stop()


# ---------- inbound validation on the real wire ----------


async def test_malformed_envelopes_drop_with_exact_codes_then_valid_still_arrives(
    bus_factory, nats_server  # noqa: F811
):
    """A reject must not wedge the subscription.

    nats-py routes an exception raised inside a subscription callback to
    `error_cb` and leaves the subscription alive, but an adapter that let one
    escape would still lose the drop-audit line, and one that returned early
    from the wrong place would stop consuming. So: four different rejects,
    exact reject codes, and then a valid envelope that must still land.
    """
    harness = bus_factory()
    await harness.wait_for(
        lambda lines: any(line.get("event") == "connected" for line in lines),
        what="connected",
    )
    subject = f"fleet.{BOT_NAME}.request"
    peer = await _peer_client(nats_server)
    try:
        await peer.publish(subject, b"{not json at all")
        await peer.publish(subject, _encode(_peer_envelope(envelope_version=2)))
        await peer.publish(subject, _encode(_peer_envelope(**{"from": "stranger"})))
        await peer.publish(
            subject,
            _encode(
                _peer_envelope(
                    payload={"text": "x" * (fleet_bus.DEFAULT_MAX_ENVELOPE_BYTES + 2000)}
                )
            ),
        )
        await peer.flush()
        await harness.wait_for(
            lambda lines: len([x for x in lines if x["dir"] == "drop"]) >= 4,
            what="four drop-audit lines",
        )
        # AFTER the rejects: the subscription must still be live.
        await peer.publish(subject, _encode(_peer_envelope(id="survivor")))
        await peer.flush()
        await harness.wait_for(
            lambda lines: any(line.get("id") == "survivor" for line in lines),
            what="valid envelope after the rejects",
        )
    finally:
        await peer.close()

    drops = [line for line in harness.lines() if line["dir"] == "drop"]
    assert [line["reason"] for line in drops[:4]] == [
        "malformed_json",
        "unsupported_envelope_version",
        "from_claim_rejected",
        "envelope_too_large",
    ]
    for line in drops:
        assert line["subject"] == subject
    survivor = next(line for line in harness.lines() if line.get("id") == "survivor")
    assert survivor["dir"] == "in"
    await harness.stop()


async def test_non_finite_numbers_on_the_wire_drop_as_malformed_json(
    bus_factory, nats_server  # noqa: F811
):
    """`NaN`/`Infinity`/`-Infinity` are not JSON, and `JSON.parse` throws on
    all three — so the TS adapter answers these bytes with `malformed_json`.

    Python's decoder accepts them, which made the same bytes a live inbound
    envelope here: audited as `in`, with no reject line anywhere to say the
    peer had already dropped it. This is the wire half of the pin; the
    `validate_envelope` half is in test_fleet_bus_envelope, because the
    validator is exported and callable without the decoder.

    Real broker rather than a synthetic `_on_message` call on purpose: the
    bytes have to survive publish -> broker -> subscribe, and the drop must
    not wedge the subscription for the envelope behind it.
    """
    harness = bus_factory()
    await harness.wait_for(
        lambda lines: any(line.get("event") == "connected" for line in lines),
        what="connected",
    )
    subject = f"fleet.{BOT_NAME}.request"
    peer = await _peer_client(nats_server)
    try:
        # `_encode` is `json.dumps` at its default `allow_nan=True`, which is
        # exactly how a Python peer would put these on the wire by accident.
        for identifier, value in (
            ("nan-top", float("nan")),
            ("inf-top", float("inf")),
            ("ninf-nested", {"a": [{"b": float("-inf")}]}),
        ):
            raw = _encode(_peer_envelope(id=identifier, payload=value))
            assert b"NaN" in raw or b"Infinity" in raw, raw
            await peer.publish(subject, raw)
        await peer.flush()
        await harness.wait_for(
            lambda lines: len([x for x in lines if x["dir"] == "drop"]) >= 3,
            what="three drop-audit lines",
        )
        await peer.publish(subject, _encode(_peer_envelope(id="finite-survivor")))
        await peer.flush()
        await harness.wait_for(
            lambda lines: any(line.get("id") == "finite-survivor" for line in lines),
            what="valid envelope after the rejects",
        )
    finally:
        await peer.close()

    lines = harness.lines()
    drops = [line for line in lines if line["dir"] == "drop"]
    assert [line["reason"] for line in drops] == ["malformed_json"] * 3
    # Scoped to the request subject: `no_echo` is off, so our own heartbeat
    # loops back on `fleet.<self>.status` and is audited as `in` too.
    accepted = {
        line.get("id")
        for line in lines
        if line["dir"] == "in" and line["subject"] == subject
    }
    assert accepted == {"finite-survivor"}, (
        "a non-finite envelope was accepted here that the TS adapter drops"
    )
    # Nothing the adapter wrote may be unparseable to a plain JSON reader —
    # the stream is JSONL and one bad line poisons every later consumer.
    def _reject_constant(name):
        raise AssertionError(f"audit line is not JSON: bare {name}")

    for raw in harness.audit_path.read_text(encoding="utf-8").splitlines():
        json.loads(raw, parse_constant=_reject_constant)
    await harness.stop()


async def test_inbound_envelope_is_dropped_not_injected(bus_factory, nats_server):  # noqa: F811
    """Two boundaries at once, both still live at 3b.

    `bus_factory` configures NO `on_envelope` hook, which is 3a's behaviour and
    the behaviour of any embedding that does not opt into a session: decode ->
    validate -> audit -> DROP, no LLM turn. The hook-configured routing rules
    are pinned in `test_fleet_bus_session`.

    The wire assertion below is the other half: no session means no answer.
    From 3c a hook-configured adapter auto-replies (see
    `test_fleet_bus_outbound`), and that reply is the TURN's, so an embedding
    that only wanted the wire must not suddenly start speaking for itself.
    """
    harness = bus_factory()
    await harness.wait_for(
        lambda lines: any(line.get("event") == "connected" for line in lines),
        what="connected",
    )
    peer = await _peer_client(nats_server)
    replies: list[bytes] = []

    async def _collect(msg):
        replies.append(msg.data)

    await peer.subscribe(f"fleet.{PEER_NAME}.request", cb=_collect)
    try:
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="no-reply"))
        )
        await peer.flush()
        await harness.wait_for(
            lambda lines: any(line.get("id") == "no-reply" for line in lines),
            what="inbound audit",
        )
        await asyncio.sleep(0.5)
    finally:
        await peer.close()
    assert replies == [], "an adapter with no session hook must not answer"
    # The only publishes on the wire are our own heartbeats.
    out_subjects = {line["subject"] for line in harness.lines() if line["dir"] == "out"}
    assert out_subjects == {f"fleet.{BOT_NAME}.status"}
    await harness.stop()


# ---------- shutdown ----------


async def test_bot_close_from_connected_state(monkeypatch, bus_factory, nats_server):  # noqa: F811
    """`bot.close()` must cancel the supervisor, drain the connection and let
    no exception escape into discord.py's shutdown path.

    `drain` is asserted as a call, not through an observable difference,
    because at 3a there is none: everything inbound is dropped and the only
    outbound is a heartbeat nobody waits for, so swapping `drain()` for
    `close()` in `_teardown` leaves every other assertion in this file green.
    The difference is that `drain()` unsubscribes, flushes what is pending and
    lets in-flight callbacks finish, while `close()` discards them — which is
    exactly what a `<BUS>` reply published on the way out of 3c will need.
    Pinning the mechanism now is what keeps that from being discovered in
    production two slices later.
    """
    import nats.aio.client

    drained: list = []
    real_drain = nats.aio.client.Client.drain

    async def counting_drain(self, *args, **kwargs):
        drained.append(self)
        return await real_drain(self, *args, **kwargs)

    monkeypatch.setattr(nats.aio.client.Client, "drain", counting_drain)

    harness = bus_factory()
    await harness.wait_for(
        lambda lines: any(line["dir"] == "in" for line in lines),
        what="heartbeat loopback",
    )
    monkeypatch.setattr(bot_module, "_bus", harness.bus)
    monkeypatch.setattr(bot_module, "_bus_task", harness.task)
    # Hold the client itself: `bus.connection` is nulled by teardown, so
    # asserting on it only proves we dropped our REFERENCE, not that the
    # socket was drained and closed.
    connection = harness.bus.connection

    client = bot_module.YugoBot(command_prefix="!", intents=bot_module.intents)
    await client.close()

    assert harness.task.cancelled() or harness.task.done()
    assert bot_module._bus_task is None
    assert harness.bus.connection is None
    assert connection.is_closed, "shutdown left the NATS connection open"
    assert drained == [connection], (
        "the connection was closed without being drained — pending publishes "
        "and in-flight callbacks are discarded rather than flushed"
    )


async def test_bot_close_from_reconnecting_state(monkeypatch, bus_factory, nats_server):  # noqa: F811
    """`drain()` raises `ConnectionReconnectingError` when the client is
    mid-reconnect. The shutdown path has to catch that and fall back to
    `close()`, or SIGTERM leaves the supervisor task and the socket behind and
    the container waits out its stop-grace period before SIGKILL.
    """
    harness = bus_factory()
    await harness.wait_for(
        lambda lines: any(line.get("event") == "connected" for line in lines),
        what="connected",
    )
    nats_server.stop()
    await harness.wait_for(
        lambda lines: any(line.get("event") == "disconnected" for line in lines),
        what="disconnected",
    )
    assert harness.bus.connection.is_reconnecting

    monkeypatch.setattr(bot_module, "_bus", harness.bus)
    monkeypatch.setattr(bot_module, "_bus_task", harness.task)
    connection = harness.bus.connection
    client = bot_module.YugoBot(command_prefix="!", intents=bot_module.intents)
    await asyncio.wait_for(client.close(), timeout=20)

    assert harness.task.cancelled() or harness.task.done()
    assert bot_module._bus_task is None
    assert harness.bus.connection is None
    assert connection.is_closed, "shutdown left the NATS connection open"
    # Taken through the EXPECTED branch, not the generic error handler: a
    # `drain_failed` line on every SIGTERM-during-outage is noise that would
    # mask a real drain failure the one time it matters.
    assert "drain_failed" not in harness.events(), harness.events()


async def test_closed_connection_trips_the_audit_and_the_supervisor_recovers(
    bus_factory,
):
    """Positive control for the CLOSED tripwire.

    `test_server_restart_reconnects_and_never_reaches_closed` asserts the
    ABSENCE of a `closed` line. That assertion is vacuous on its own — it also
    passes if `closed_cb` was never wired up at all. This forces a close and
    requires the line to appear, so the two tests together mean something.

    It also pins the recovery: reaching CLOSED must not be terminal for the
    supervisor, it re-enters connect.
    """
    harness = bus_factory()
    await harness.wait_for(
        lambda lines: any(line["dir"] == "in" for line in lines),
        what="first heartbeat loopback",
    )
    await harness.bus.connection.close()
    await harness.wait_for(
        lambda lines: any(line.get("event") == "closed" for line in lines),
        what="closed tripwire audit line",
    )
    await harness.wait_for(
        lambda lines: len([x for x in lines if x.get("event") == "connected"]) >= 2,
        what="supervisor reconnecting after CLOSED",
    )
    await harness.wait_for(
        lambda lines: len([x for x in lines if x["dir"] == "in"]) >= 3,
        what="heartbeat loopback resuming after CLOSED",
    )
    await harness.stop()


async def test_a_failure_after_connect_is_retried_and_leaks_no_client(
    monkeypatch, bus_factory, nats_server  # noqa: F811
):
    """The supervisor's own retry branch, and the client it must not leak.

    With `max_reconnect_attempts=-1`, nats-py's `connect()` retries a refused
    connection internally and never raises, so a plain "broker down" case does
    NOT exercise this branch (see test_fleet_bus_wiring). What does reach it is
    a fault AFTER the socket is up — a failing `subscribe`, a `flush` timeout.
    Without the retry the supervisor task would just end, leaving a bot that
    looks alive and is permanently off the bus.

    The failure is injected on the THIRD subject on purpose. Injected on the
    first, the failed attempt leaves a client with no subscriptions, and a
    leaked one is then indistinguishable from a closed one on every channel a
    test can read — same audit lines, same heartbeat, same everything. Failing
    partway through the subscribe run is what a rolling nats-server restart
    does, and it leaves a fully connected client bound to `_on_message` with
    `max_reconnect_attempts=-1`: unreachable, un-closeable by `bot.close()`,
    and reconnecting through every future outage to replay its own
    subscriptions. Today that doubles the audit trail; at 3b it is two LLM
    turns per envelope.
    """
    import nats
    import nats.aio.client

    handed_out: list = []
    real_connect = nats.connect

    async def tracking_connect(*args, **kwargs):
        client = await real_connect(*args, **kwargs)
        handed_out.append(client)
        return client

    monkeypatch.setattr(nats, "connect", tracking_connect)

    real_subscribe = nats.aio.client.Client.subscribe
    failures = {"left": 1}

    async def flaky_subscribe(self, subject, *args, **kwargs):
        if failures["left"] and subject.endswith(".status"):
            failures["left"] -= 1
            raise RuntimeError("subscribe blew up")
        return await real_subscribe(self, subject, *args, **kwargs)

    monkeypatch.setattr(nats.aio.client.Client, "subscribe", flaky_subscribe)

    harness = bus_factory()
    await harness.wait_for(
        lambda lines: any(line.get("event") == "connect_failed" for line in lines),
        what="connect_failed audit line",
    )
    await harness.wait_for(
        lambda lines: any(line["dir"] == "in" for line in lines),
        what="heartbeat loopback after the retry succeeded",
    )
    assert not harness.task.done()
    failed = next(x for x in harness.lines() if x.get("event") == "connect_failed")
    assert "subscribe blew up" in failed["error"]

    assert len(handed_out) == 2, f"expected one failed + one live connect, got {handed_out}"
    still_open = [client for client in handed_out if not client.is_closed]
    assert len(still_open) == 1, (
        "the client from the failed attempt was left open — it is still "
        "subscribed to _on_message and still reconnecting forever, and "
        "nothing holds a reference that could close it. "
        f"open={len(still_open)} of {len(handed_out)}"
    )
    assert handed_out[0].is_closed, "the orphan is the one that must be closed"

    # The symptom, not just the socket: one inbound audit line per envelope.
    peer = await _peer_client(nats_server)
    try:
        await peer.publish(
            f"fleet.{BOT_NAME}.request", _encode(_peer_envelope(id="exactly-once"))
        )
        await peer.flush()
        await harness.wait_for(
            lambda lines: any(line.get("id") == "exactly-once" for line in lines),
            what="inbound envelope after the retry",
        )
        await asyncio.sleep(0.5)  # a second delivery would land in this window
    finally:
        await peer.close()
    # Scoped to the INBOUND lane: v0.3b's session-injection lines also carry
    # the envelope id, so an unscoped count would stop meaning "one delivery"
    # the moment this harness grew an `on_envelope` hook.
    delivered = [
        line
        for line in harness.lines()
        if line.get("id") == "exactly-once" and line["dir"] == "in"
    ]
    assert len(delivered) == 1, (
        f"one published envelope produced {len(delivered)} inbound audit lines "
        "— a second subscribed client is still on the bus"
    )
    await harness.stop()


async def test_stop_bus_is_safe_when_never_started(monkeypatch):
    """SIGTERM before `setup_hook` ran, or with the bus disabled entirely."""
    monkeypatch.setattr(bot_module, "_bus", None)
    monkeypatch.setattr(bot_module, "_bus_task", None)
    await bot_module.stop_bus()
    assert bot_module._bus_task is None

"""
Boundary tests for the `messages` payload `bot.on_message` assembles and
hands to `ask_llm`.

Why a dedicated file:
    The first round of v0.2a tests fake `ask_llm` with a lambda that never
    inspects its argument. That let a mutant of `bot.on_message` — one that
    silently replaced `history.build_messages(...)` with a stateless
    `[system, user]` payload — pass the whole suite green. History was still
    recorded (into a store nobody read), and no test noticed the model was
    never seeing prior turns.

    These tests plug an ASSERTING fake into that seam. Every mutation of
    the message-assembly path in `bot.on_message` should be catchable by at
    least one test here.

Invariants asserted on EVERY payload (from PR #4 revision brief):
    1. Exactly one `role: system` message, at index 0.
    2. Every other role is in `history.ALLOWED_ROLES` ({user, assistant}).
    3. The last message has role `user`.
    4. (Store-level: retained-suffix identity after eviction — see
       `test_history.test_retained_suffix_element_wise_identical_after_eviction`.)
    5. Non-system count in the stored history is bounded by `max_turns * 2`.

Deliberately NOT asserted: alternation, even length. Providers merge
consecutive same-role turns, and v0.4 tool rounds kill alternation anyway.
Enshrining alternation was PR #4's second-order bug.
"""

from unittest.mock import MagicMock

import discord
import openai
import pytest

import bot
import history
import litellm
from litellm.types.utils import Message


# ---------- shared fakes ----------


class _FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent: list[str] = []

    def typing(self):
        return _FakeTyping()

    async def send(self, content):
        self.sent.append(content)


class _FakeAuthor:
    # Constructor kwarg so ingress-guard tests can build a bot-authored
    # author without mutating the default. Existing call sites that
    # instantiate `_FakeAuthor()` still get `bot=False` — matches the
    # v0.1 class-attribute shape they relied on.
    def __init__(self, bot: bool = False):
        self.bot = bot


class _FakeMessage:
    def __init__(self, content, channel, author=None):
        self.content = content
        self.channel = channel
        self.author = author if author is not None else _FakeAuthor()


def _make_assert_messages_fake(seen: list[list[dict]], reply: str = "OK"):
    """Boundary fake for `bot.ask_llm` — asserts invariants 1-3 on every call
    and captures the payload for the test to make contents assertions on.

    Every call MUST satisfy:
      1. `messages[0]["role"] == "system"` AND there is EXACTLY ONE system
         message in the whole payload.
      2. Every non-index-0 entry's role is in `history.ALLOWED_ROLES`.
      3. `messages[-1]["role"] == "user"`.
    """

    async def _fake(messages, thread_id):
        assert len(messages) >= 2, (
            f"expected >= 2 messages (persona + current user), got {len(messages)}"
        )
        system_count = sum(1 for m in messages if m.get("role") == "system")
        assert system_count == 1, (
            f"invariant 1: exactly one system message required; got {system_count}"
        )
        assert messages[0].get("role") == "system", (
            f"invariant 1: index 0 must be role=system; "
            f"got {messages[0].get('role')!r}"
        )
        for i, m in enumerate(messages[1:], start=1):
            role = m.get("role")
            assert role in history.ALLOWED_ROLES, (
                f"invariant 2: index {i} role must be in "
                f"{sorted(history.ALLOWED_ROLES)}; got {role!r}"
            )
        assert messages[-1].get("role") == "user", (
            f"invariant 3: last message must be role=user; "
            f"got {messages[-1].get('role')!r}"
        )
        # Snapshot a shallow copy so a later mutation of `messages` in the
        # bot doesn't retroactively edit what the test sees.
        seen.append([dict(m) for m in messages])
        return reply

    return _fake


@pytest.fixture(autouse=True)
def _clean_history():
    history._reset_for_tests()
    yield
    history._reset_for_tests()


# ---------- invariants on the assembled payload ----------


@pytest.mark.asyncio
async def test_first_turn_payload_is_persona_then_user(monkeypatch):
    """Fresh thread: assembled payload MUST be exactly [PERSONA, user_msg].

    Catches a mutant where `on_message` skips `build_messages` and sends only
    the user text, or one that inlines the wrong persona.
    """
    seen: list[list[dict]] = []
    monkeypatch.setattr(bot, "ask_llm", _make_assert_messages_fake(seen))

    channel = _FakeChannel(bot.CHANNEL_ID)
    await bot.on_message(_FakeMessage("hi", channel))

    assert len(seen) == 1
    assert seen[0] == [
        {"role": "system", "content": bot.PERSONA},
        {"role": "user", "content": "hi"},
    ]


@pytest.mark.asyncio
async def test_second_turn_payload_contains_first_turn_between_persona_and_user(
    monkeypatch,
):
    """This is the mutation-catching test.

    After one full turn, the second call's payload MUST include the recorded
    user+assistant pair between PERSONA and the new user message. A mutant
    that assembles a stateless payload (v0.1 shape) survives the invariant
    assertions inside the fake but fails THIS exact-contents check.
    """
    seen: list[list[dict]] = []
    monkeypatch.setattr(bot, "ask_llm", _make_assert_messages_fake(seen, reply="A1"))

    channel = _FakeChannel(bot.CHANNEL_ID)
    await bot.on_message(_FakeMessage("U1", channel))
    await bot.on_message(_FakeMessage("U2", channel))

    assert len(seen) == 2
    assert seen[1] == [
        {"role": "system", "content": bot.PERSONA},
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
    ]


@pytest.mark.asyncio
async def test_many_turns_never_produce_two_system_messages(monkeypatch):
    """Cycle beyond the bound — the payload must still hold exactly one
    system message. Catches a mutant that appends the persona to the store
    (would produce two systems after the first turn) or one that lets
    eviction chew into the persona slot."""
    seen: list[list[dict]] = []
    monkeypatch.setattr(bot, "ask_llm", _make_assert_messages_fake(seen, reply="A"))

    channel = _FakeChannel(bot.CHANNEL_ID)
    # Run 3x the bound to exercise several eviction cycles.
    for i in range(bot.HISTORY_MAX_TURNS * 3):
        await bot.on_message(_FakeMessage(f"U{i}", channel))

    # The fake already asserts exactly-one-system on every call; also verify
    # the eviction bound. v0.2a's store contains only user/assistant entries
    # (no system in the store — PERSONA lives outside it), so `len(stored)`
    # IS the non-system count. v0.2b will need a dedicated counter once the
    # summary lands as a role:user entry inside the store.
    stored = history._history[channel.id]
    assert len(stored) <= bot.HISTORY_MAX_TURNS * 2, (
        "invariant 5: stored entry count must be <= max_turns * 2"
    )


@pytest.mark.asyncio
async def test_payload_last_message_is_always_current_user(monkeypatch):
    """After N turns, the last message must be the CURRENT user text, not
    a leftover assistant reply or a duplicated prior user turn."""
    seen: list[list[dict]] = []
    monkeypatch.setattr(bot, "ask_llm", _make_assert_messages_fake(seen, reply="Ax"))

    channel = _FakeChannel(bot.CHANNEL_ID)
    for i in range(5):
        await bot.on_message(_FakeMessage(f"U{i}", channel))

    # Each captured payload's last message must be the corresponding user text.
    for i, payload in enumerate(seen):
        assert payload[-1] == {"role": "user", "content": f"U{i}"}


# ---------- ask_llm contract: empty provider content RAISES, never returns --


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_content", ["", "   ", "\n\t "])
async def test_ask_llm_raises_on_empty_content_never_returns_placeholder(
    monkeypatch, empty_content
):
    """`ask_llm` MUST raise `EmptyResponseError` when the provider returns
    empty/whitespace content — NEVER return a `"[no response]"` placeholder
    as if it were a real reply.

    Why this test exists: the pre-fix `ask_llm` returned the string
    `"[no response]"`, which flowed through `on_message`'s success branch and
    got recorded as an assistant turn. Every subsequent completion then
    carried that useless assistant message forward, poisoning the model's
    context. The fix moved this into an exception path so `on_message` can
    catch it and skip the record.

    Bite-check: mentally revert `ask_llm` to `return content or "[no
    response]"` — this test would then fail because no exception is raised.
    """
    # A REAL `litellm.types.utils.Message`, not a MagicMock: v0.4a's loop
    # reads `message.tool_calls`, and every attribute of a MagicMock is a
    # truthy MagicMock — the loop would see phantom tool calls and never
    # reach the empty-content branch this test is about. The real type
    # leaves `tool_calls` as None when the provider sent none.
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message = Message(
        content=empty_content, role="assistant"
    )

    async def _fake_acompletion(**_kwargs):
        return fake_response

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    with pytest.raises(bot.EmptyResponseError):
        await bot.ask_llm([{"role": "user", "content": "hi"}], bot.CHANNEL_ID)


# ---------- error paths: empty response is reported but NOT recorded ----------


@pytest.mark.asyncio
async def test_empty_provider_response_surfaced_and_not_recorded(monkeypatch):
    """When `ask_llm` raises `EmptyResponseError`, the user MUST see
    "[no response]" and history MUST stay untouched.

    Regression guard: v0.2a's original ask_llm returned the string
    "[no response]" as if it were a real reply — which flowed through the
    success branch and recorded a `role: assistant` entry with useless
    content. Every subsequent turn then carried that garbage forward.
    """

    async def _empty(_messages, _thread_id):
        raise bot.EmptyResponseError("empty from provider")

    monkeypatch.setattr(bot, "ask_llm", _empty)

    channel = _FakeChannel(bot.CHANNEL_ID)
    await bot.on_message(_FakeMessage("hi", channel))

    assert channel.sent == ["[no response]"]

    # History untouched — a fresh build sees only [system, user].
    next_msgs = history.build_messages(bot.PERSONA, channel.id, "next")
    assert next_msgs == [
        {"role": "system", "content": bot.PERSONA},
        {"role": "user", "content": "next"},
    ]


# ---------- error paths: send failure after LLM success is NOT recorded -------


@pytest.mark.asyncio
async def test_send_failure_after_llm_success_does_not_record_history(monkeypatch):
    """LLM returned a real reply, but `message.channel.send` raises
    `discord.errors.HTTPException` on the first chunk. History MUST stay
    clean — the model must not see an assistant reply the user never got.

    Regression guard for PR #4 P2 (Codex): the pre-fix `on_message` recorded
    the turn BEFORE the send loop, so a network hiccup / permission denied /
    rate-limit / partial multi-chunk failure at send time silently poisoned
    the next turn's context with a message the user never saw.

    Bite-check: against pre-fix `on_message`, `record_turn` fires inside the
    `else` branch before the send loop, so the send-time HTTPException escapes
    `on_message` unhandled AND `record_turn` was already called — the test
    fails either on the propagated HTTPException or on the `record_calls`
    assertion. Fixed code catches the HTTPException, returns without
    recording, and both assertions hold.
    """
    ask_calls: list[list[dict]] = []

    async def _fake_ask(messages, thread_id):
        ask_calls.append(messages)
        return "the model's reply"

    monkeypatch.setattr(bot, "ask_llm", _fake_ask)

    # Spy on record_turn WITHOUT calling through — if the pre-fix code slips
    # in a real record here, the _clean_history fixture would wipe it, but
    # not calling through keeps the failure signal (record_calls) clean.
    record_calls: list[tuple] = []

    def _spy_record(*args, **kwargs):
        record_calls.append((args, kwargs))

    monkeypatch.setattr(history, "record_turn", _spy_record)

    class _FailingSendChannel(_FakeChannel):
        async def send(self, content):
            resp = MagicMock()
            resp.status = 500
            resp.reason = "Server Error"
            raise discord.errors.HTTPException(resp, "boom")

    channel = _FailingSendChannel(bot.CHANNEL_ID)
    await bot.on_message(_FakeMessage("hi", channel))

    # LLM did succeed — proves this is the "LLM OK, send fails" path, not a
    # test that happens to skip both.
    assert len(ask_calls) == 1, (
        f"ask_llm must have been called exactly once; got {len(ask_calls)}"
    )

    # The P2 fix's contract: no record when send failed.
    assert record_calls == [], (
        f"record_turn must NOT be called when send fails; got {record_calls!r}"
    )

    # And the store is empty — next build sees only [system, user].
    next_msgs = history.build_messages(bot.PERSONA, channel.id, "next")
    assert next_msgs == [
        {"role": "system", "content": bot.PERSONA},
        {"role": "user", "content": "next"},
    ]


# ---------- error paths: partial multi-chunk send is NOT recorded -------------


@pytest.mark.asyncio
async def test_multi_chunk_partial_send_failure_does_not_record_history(monkeypatch):
    """A long reply chunks into multiple `channel.send()` calls. If chunk 1
    succeeds and chunk 2 raises `discord.HTTPException`, history MUST stay
    clean AND the sent list MUST contain only chunk 1 (partial delivery).

    Regression guard for a subtle B20-shaped mutation: a guard that only
    catches the FIRST send's exception (or one that records inside the send
    loop) would let partial delivery poison the next turn's context — the
    model would think the user saw a full reply that was actually truncated
    mid-sentence.

    Bite-check: mentally move `record_turn` INSIDE the send loop (before
    each `send()`) — this test would find `record_turn` called during
    chunk 1's success, before chunk 2 raises. Or restrict the outer
    try/except to only guard `send[0]` — chunk 2's HTTPException escapes
    unhandled and `record_turn` still fires.
    """
    ask_calls: list[list[dict]] = []
    long_reply = "x" * 4000  # 3 chunks at 1990/chunk

    async def _fake_ask(messages, thread_id):
        ask_calls.append(messages)
        return long_reply

    monkeypatch.setattr(bot, "ask_llm", _fake_ask)

    record_calls: list[tuple] = []

    def _spy_record(*args, **kwargs):
        record_calls.append((args, kwargs))

    monkeypatch.setattr(history, "record_turn", _spy_record)

    class _PartialFailSendChannel(_FakeChannel):
        async def send(self, content):
            # First chunk goes through; second and later raise.
            if len(self.sent) == 0:
                self.sent.append(content)
                return
            resp = MagicMock()
            resp.status = 500
            resp.reason = "Server Error"
            raise discord.errors.HTTPException(resp, "boom on chunk 2")

    channel = _PartialFailSendChannel(bot.CHANNEL_ID)
    await bot.on_message(_FakeMessage("hi", channel))

    # LLM path exercised.
    assert len(ask_calls) == 1, (
        f"ask_llm must have been called exactly once; got {len(ask_calls)}"
    )

    # Only chunk 1 landed — the second send raised. This proves we're on
    # the multi-chunk partial-failure path, not the "first send fails"
    # path already covered above.
    assert len(channel.sent) == 1, (
        f"expected exactly one chunk delivered before failure; "
        f"got {len(channel.sent)} chunks: {channel.sent!r}"
    )
    assert channel.sent[0] == long_reply[:1990]

    # The contract: partial delivery counts as failure — no record.
    assert record_calls == [], (
        f"record_turn must NOT be called when a later chunk fails; "
        f"got {record_calls!r}"
    )

    # Store is empty — next build sees only [system, user].
    next_msgs = history.build_messages(bot.PERSONA, channel.id, "next")
    assert next_msgs == [
        {"role": "system", "content": bot.PERSONA},
        {"role": "user", "content": "next"},
    ]


# ---------- error paths: provider rate-limit surfaces as [api error: ...] -----


@pytest.mark.asyncio
async def test_rate_limit_error_surfaced_as_api_error_and_not_recorded(monkeypatch):
    """A provider rate-limit MUST reach the user as `[api error: ...]`, not
    `[error: ...]`, and MUST NOT record the turn.

    Regression guard: the original branch caught `litellm.exceptions.APIError`,
    which is a DIFFERENT class from `openai.APIError`. Every real operational
    LiteLLM exception (`RateLimitError`, `AuthenticationError`, `Timeout`,
    `BadRequestError`, `ContextWindowExceededError`) inherits from
    `openai.APIError` but NOT from `litellm.exceptions.APIError`, so the
    friendlier branch matched essentially nothing `acompletion` raises — real
    rate-limits fell into the catch-all and got the generic `[error: ...]`
    label.

    Bite-check: with `except litellm.exceptions.APIError`, `openai.RateLimitError`
    is NOT a subclass of `litellm.exceptions.APIError`, so it falls through to
    the generic catch-all and the sent text starts with `[error:` — assertion
    `startswith("[api error:")` fails. With `except openai.APIError`, the
    friendlier branch matches and the assertion holds.
    """

    async def _rate_limited(_messages, _thread_id):
        resp = MagicMock()
        resp.status = 429
        resp.reason = "Too Many Requests"
        raise openai.RateLimitError("rate limited", response=resp, body=None)

    monkeypatch.setattr(bot, "ask_llm", _rate_limited)

    record_calls: list[tuple] = []

    def _spy_record(*args, **kwargs):
        record_calls.append((args, kwargs))

    monkeypatch.setattr(history, "record_turn", _spy_record)

    channel = _FakeChannel(bot.CHANNEL_ID)
    await bot.on_message(_FakeMessage("hi", channel))

    assert channel.sent, "expected an error reply to have been sent"
    assert channel.sent[0].startswith("[api error:"), (
        f"expected [api error: ...] prefix (openai.APIError branch); "
        f"got {channel.sent[0]!r}"
    )
    assert record_calls == [], (
        f"record_turn must NOT be called on a provider error; got {record_calls!r}"
    )


# ---------- ingress guards: bot-author and wrong-channel drops ----------------


@pytest.mark.asyncio
async def test_on_message_drops_bot_authored_messages(monkeypatch):
    """The `message.author.bot` guard MUST actually fire under a bot-authored
    message. Without it, a shared channel that hosts multiple bots (Vec/Ohm/
    Kat/etc. in Fernando's fleet) turns into a bot-to-bot reply loop that
    burns provider spend until a human notices.

    Bite-check: delete `if message.author.bot: return` from `on_message` —
    this test FAILS because `ask_llm` gets called (call count == 1) and the
    history store picks up an entry.
    """
    ask_calls: list[list[dict]] = []

    async def _fake_ask(messages, thread_id):
        ask_calls.append(messages)
        return "reply"

    monkeypatch.setattr(bot, "ask_llm", _fake_ask)

    channel = _FakeChannel(bot.CHANNEL_ID)
    msg = _FakeMessage("hi", channel, author=_FakeAuthor(bot=True))
    await bot.on_message(msg)

    # If the guard fired: ask_llm never called, nothing sent, store untouched.
    assert ask_calls == [], (
        f"ask_llm must NOT be called for bot-authored messages; "
        f"got {len(ask_calls)} call(s)"
    )
    assert channel.sent == [], (
        f"nothing should be sent when the bot-author guard fires; "
        f"got {channel.sent!r}"
    )
    assert history._history == {}, (
        f"history must stay empty when the bot-author guard fires; "
        f"got {history._history!r}"
    )


@pytest.mark.asyncio
async def test_on_message_drops_messages_from_other_channels(monkeypatch):
    """The `channel.id != CHANNEL_ID` filter MUST actually drop messages from
    channels the bot is not scoped to. Without it, the bot responds in any
    channel it can see, defeating the single-channel scoping SPEC §4.2 assumes.

    Bite-check: replace the guard with `if False:` (or drop it entirely) —
    this test FAILS because `ask_llm` gets called and the store picks up an
    entry keyed on the foreign channel id.
    """
    ask_calls: list[list[dict]] = []

    async def _fake_ask(messages, thread_id):
        ask_calls.append(messages)
        return "reply"

    monkeypatch.setattr(bot, "ask_llm", _fake_ask)

    # Distinct id so the != check must fire; human author to isolate this
    # test to the channel filter (bot=False is the default).
    channel = _FakeChannel(bot.CHANNEL_ID + 999)
    msg = _FakeMessage("hi", channel, author=_FakeAuthor(bot=False))
    await bot.on_message(msg)

    assert ask_calls == [], (
        f"ask_llm must NOT be called for messages from other channels; "
        f"got {len(ask_calls)} call(s)"
    )
    assert channel.sent == [], (
        f"nothing should be sent when the channel filter fires; "
        f"got {channel.sent!r}"
    )
    assert history._history == {}, (
        f"history must stay empty when the channel filter fires; "
        f"got {history._history!r}"
    )

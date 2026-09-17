"""
Tests for yugo v0.2a — per-thread rolling conversation history.

Each test targets one specific invariant so a broken implementation surfaces
as a red on the test whose name describes what broke.
"""

import pytest

import history


@pytest.fixture(autouse=True)
def _clean_history():
    """Reset the module-level history store before every test."""
    history._reset_for_tests()
    yield
    history._reset_for_tests()


# ---------- pure history primitives ----------

def test_new_thread_produces_system_plus_user_only():
    """First turn in a brand-new thread: no in-between entries, no KeyError."""
    msgs = history.build_messages("SYS", thread_id=1, user_text="hello")

    assert msgs == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hello"},
    ]
    # Would fail if build_messages threw KeyError on missing thread, or if it
    # created a phantom assistant/user pair before the first record_turn.


def test_store_bound_enforced_and_keeps_most_recent():
    """Store bound = max_turns * 2 non-system entries. 20 turns w/ max_turns=3; keep last 3."""
    tid = 42
    for i in range(20):
        history.record_turn(tid, max_turns=3, user_text=f"u{i}", assistant_text=f"a{i}")

    # Build to inspect what would be sent to the LLM next.
    msgs = history.build_messages("SYS", thread_id=tid, user_text="now")

    # [system] + max_turns*2 history entries + [current user]
    assert len(msgs) == 1 + 3 * 2 + 1
    # The retained history entries must be the last 3 turns (17, 18, 19) in
    # insertion order.
    assert msgs[1:-1] == [
        {"role": "user", "content": "u17"},
        {"role": "assistant", "content": "a17"},
        {"role": "user", "content": "u18"},
        {"role": "assistant", "content": "a18"},
        {"role": "user", "content": "u19"},
        {"role": "assistant", "content": "a19"},
    ]
    # Would fail if the bound were miscomputed (e.g. `max_turns` entries
    # instead of `max_turns * 2`), or if eviction dropped newest instead of
    # oldest, or if turn ordering weren't preserved.


def test_thread_isolation_independent_stores():
    """Two thread_ids get independent stores — no cross-talk on record."""
    history.record_turn(1, max_turns=5, user_text="u1a", assistant_text="a1a")
    history.record_turn(2, max_turns=5, user_text="u2a", assistant_text="a2a")

    msgs_1 = history.build_messages("SYS", thread_id=1, user_text="q1")
    msgs_2 = history.build_messages("SYS", thread_id=2, user_text="q2")

    # Each thread sees only its own single prior turn, not the other's.
    assert msgs_1 == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "u1a"},
        {"role": "assistant", "content": "a1a"},
        {"role": "user", "content": "q1"},
    ]
    assert msgs_2 == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "u2a"},
        {"role": "assistant", "content": "a2a"},
        {"role": "user", "content": "q2"},
    ]
    # Would fail if `_history` were a single shared store (both threads would
    # see both turns) or keyed on something other than thread_id.


def test_poisoning_one_thread_does_not_leak_to_another():
    """Filling thread A to overflow doesn't touch thread B's store."""
    for i in range(10):
        history.record_turn(1, max_turns=2, user_text=f"u{i}", assistant_text=f"a{i}")

    # Thread 2 has never been touched.
    msgs_2 = history.build_messages("SYS", thread_id=2, user_text="hi")

    assert msgs_2 == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
    ]
    # Would fail if the store were a single global list or if the bound were
    # applied across threads instead of per thread.


def test_turn_ordering_preserved_across_multiple_turns():
    """User precedes assistant in each turn; turns land in insertion order."""
    tid = 7
    history.record_turn(tid, max_turns=5, user_text="q1", assistant_text="a1")
    history.record_turn(tid, max_turns=5, user_text="q2", assistant_text="a2")
    history.record_turn(tid, max_turns=5, user_text="q3", assistant_text="a3")

    msgs = history.build_messages("SYS", thread_id=tid, user_text="q4")

    assert msgs == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "q4"},
    ]
    # Would fail if role order were swapped (assistant before user), or if
    # record_turn appended in reverse order, or if build_messages iterated
    # backwards.


def test_retained_suffix_element_wise_identical_after_eviction():
    """Invariant 4: eviction drops from the head only — surviving entries
    are the same dict objects (element-wise equal, same order) they were
    before the append that triggered eviction."""
    tid = 5
    # Fill to exactly `max_turns * 2` non-system entries so the next
    # `record_turn` triggers eviction.
    for i in range(3):
        history.record_turn(tid, max_turns=3, user_text=f"u{i}", assistant_text=f"a{i}")

    # Snapshot: the entries we EXPECT to survive after the next eviction are
    # the last four non-system entries currently in the store — i.e. turns
    # 1 and 2. (Turn 0 will be evicted to make room for the new pair.)
    snapshot = [dict(m) for m in history._history[tid][-4:]]

    history.record_turn(tid, max_turns=3, user_text="u3", assistant_text="a3")

    # After eviction + append, the store's first four entries must equal the
    # snapshot exactly (same order, unmutated), followed by the new pair.
    stored = history._history[tid]
    assert stored[:4] == snapshot
    assert stored[4:] == [
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]
    # Would fail if eviction reordered, mutated, or trimmed from the tail.


def test_record_turn_rejects_disallowed_role_defensively():
    """record_turn validates roles — a future caller (e.g. tool-loop) that
    tries to record an unsupported role gets a loud ValueError, not silent
    store corruption. The current call site never triggers this — it's a
    guard against v0.4 regressions."""
    # Poke through the public API by monkeying record_turn's construction —
    # simulate the shape a bad refactor would produce.
    import history as h

    # Directly invoke the validator via the module — the guard sits inside
    # record_turn, so we call it with a role that would be constructed after
    # a hypothetical ALLOWED_ROLES contraction (or an untested v0.4 wire).
    original = h.ALLOWED_ROLES
    try:
        h.ALLOWED_ROLES = frozenset({"assistant"})  # user no longer allowed
        with pytest.raises(ValueError, match="role 'user' not allowed"):
            h.record_turn(1, max_turns=2, user_text="u", assistant_text="a")
    finally:
        h.ALLOWED_ROLES = original
    # Would fail if record_turn skipped role validation and silently wrote
    # a disallowed role into the store.


@pytest.fixture(params=[
    pytest.param(
        {"role": "assistant", "content": "flat text"},
        id="flat",
    ),
    pytest.param(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q":"x"}'},
                },
            ],
        },
        id="nested_tool_calls",
        marks=pytest.mark.xfail(
            reason=(
                "v0.4 will introduce tool_calls (nested list of dicts). The "
                "current shallow `dict(m)` in history.build_messages shares "
                "the inner list — deepen the copy when tool_calls lands and "
                "unmark this xfail. strict=True flips to XPASS-fail the "
                "moment the copy is deepened, forcing a review of this test."
            ),
            strict=True,
        ),
    ),
])
def stored_assistant_turn(request):
    return request.param


def test_build_messages_does_not_share_dict_references_with_store(stored_assistant_turn):
    """Invariant on WHAT YUGO CONTROLS, not on what LiteLLM does: every
    payload element in the historical middle slice (indices [1:-1] — excludes
    the fresh system-persona at head and the fresh current-user at tail) is a
    DIFFERENT OBJECT from the corresponding stored dict, and any nested list
    or dict fields are ALSO distinct objects, not shared references.

    Decoupled from what LiteLLM's transform happens to mutate today or
    tomorrow: this asserts identity on the copy boundary itself. Parametrized
    over two shapes:

      - `flat`: today's `{role, content}` shape. Passes under the current
        shallow `dict(m)` because there is nothing to share.
      - `nested_tool_calls`: v0.4's shape. Fails under shallow `dict(m)`
        (the `tool_calls` list is shared) — marked xfail(strict=True) so it
        stays green today AND flips loud the moment someone deepens the
        copy without revisiting this test.
    """
    tid = 42
    # record_turn requires str content; inject the shape directly so the
    # nested_tool_calls parametrization (content=None) is representable.
    history._history[tid] = [
        {"role": "user", "content": "u"},
        stored_assistant_turn,
    ]

    payload = history.build_messages("PERSONA", tid, "current")
    stored = history._history[tid]

    # Payload shape: [system, user, assistant_turn, current_user].
    # Middle slice = [user, assistant_turn] vs stored [user, assistant_turn].
    middle = payload[1:-1]
    assert len(middle) == len(stored)
    for i, (p, s) in enumerate(zip(middle, stored)):
        assert p is not s, (
            f"index {i}: payload element is the same object as the stored "
            f"element — shallow copy failed at the outer dict"
        )
        for k, v in p.items():
            if isinstance(v, (list, dict)):
                assert v is not s.get(k), (
                    f"index {i}: nested field {k!r} is a shared object — "
                    f"shallow copy is insufficient for this shape"
                )
    # Would fail if build_messages unpacked `*prior` without copying (outer
    # `is` check trips) OR if the copy is only one level deep once nested
    # shapes like tool_calls land (nested `is` check trips).


def test_tool_role_addition_forces_deep_copy_review():
    """Tripwire on the HAZARD event, not the fix event.

    The day someone adds "tool" to ALLOWED_ROLES (v0.4 tool loop), this
    test fails LOUDLY with a fix instruction. That's the moment nested
    structures (tool_calls: list) can first reach the store, at which
    point history.build_messages()'s shallow dict(m) copy shares nested
    lists with LiteLLM's transform.

    The xfail on the nested_tool_calls parametrized case above documents
    the shape v0.4 needs to cope with; this test names the exact trigger
    event so v0.4's author sees WHY the parametrized xfail matters at the
    moment the trigger fires. Both worth having.
    """
    assert "tool" not in history.ALLOWED_ROLES, (
        "v0.4 added tool messages to ALLOWED_ROLES — build_messages "
        "uses a shallow dict copy (history.py line ~72), which shares "
        "nested tool_calls lists with the LiteLLM transform payload. "
        "Deepen the copy (copy.deepcopy or targeted deep-copy of "
        "tool_calls) before removing this tripwire."
    )


# ---------- v0.3b: the bus namespace ----------

def test_a_bus_key_can_never_equal_a_discord_key():
    """The collision argument for the whole namespace decision, asserted
    rather than assumed: Discord keys are `int` snowflakes, bus keys are
    prefixed `str`, and the two families are disjoint in Python by type.

    Mutation this catches: any bus key coerced into the Discord key space —
    `hash(name)`, an enumeration index, `int(...)` of anything. The dict-level
    check is the one that matters: it fails for a colliding key even if the
    `==` check were somehow satisfied by a lookalike type.
    """
    key = history.bus_thread_key("vec")
    assert isinstance(key, str)
    assert key.startswith(history.BUS_THREAD_PREFIX)

    history.record_turn(key, max_turns=5, user_text="bus-u", assistant_text="bus-a")
    for snowflake in (0, 1, 12345, 100000000000000002, hash("vec")):
        assert key != snowflake
        assert snowflake not in history._history, (
            f"the bus key landed in the Discord key space at {snowflake}"
        )


def test_bus_and_discord_stores_are_independent():
    """Both directions of the isolation the namespace exists to provide."""
    discord_key = 100000000000000002
    bus_key = history.bus_thread_key("vec")
    history.record_turn(discord_key, 5, "private-human-text", "human-answer")
    history.record_turn(bus_key, 5, "untrusted-peer-text", "peer-answer")

    from_bus = history.build_messages("SYS", bus_key, "next")
    from_discord = history.build_messages("SYS", discord_key, "next")

    assert "private-human-text" not in str(from_bus)
    assert "untrusted-peer-text" not in str(from_discord)
    assert len(from_bus) == len(from_discord) == 4


def test_is_compactable_excludes_bus_namespaces():
    """v0.2b's summary compaction must not run over bus stores: it would have
    an LLM summarize UNTRUSTED bot-authored content and splice the summary
    back in as `role: user`, letting injected instructions shape their own
    summary — which then OUTLIVES the FIFO eviction that would have flushed
    the raw text out of the window.

    Mutation this catches: `is_compactable` returning True unconditionally,
    or matching on `"bus" in key` (which would also exempt a future
    `"bus"`-containing Discord-side namespace by accident, and would NOT
    exempt the real prefix if it were ever renamed).
    """
    assert history.is_compactable(12345) is True
    assert history.is_compactable(history.bus_thread_key("vec")) is False
    assert history.is_compactable(history.bus_thread_key("yugo")) is False
    # A string key that is NOT a bus namespace stays compactable — the rule is
    # about the bus prefix, not about the key's type.
    assert history.is_compactable("summary-fixture") is True


def test_compaction_arrival_must_consult_is_compactable():
    """Tripwire on the HAZARD event, in the same shape as the tool-role one
    below: this fires the day v0.2b lands, not the day someone remembers.

    `is_compactable` is a rule with no caller today, which is exactly the kind
    of rule that gets skipped by the slice it was written for. Naming the
    trigger event here is what makes it binding.
    """
    compaction_symbols = sorted(
        name
        for name in dir(history)
        if ("compact" in name.lower() or "summar" in name.lower())
        and name != "is_compactable"
    )
    assert compaction_symbols == [], (
        f"history.py grew {compaction_symbols} — v0.2b compaction has arrived. "
        "It MUST skip every thread where `history.is_compactable(thread_id)` "
        "is False (bus namespaces: summarizing untrusted bot-authored content "
        "re-injects it as `role: user` and outlives eviction). Wire the check, "
        "then relax this tripwire to name the caller instead."
    )


def test_record_turn_creates_store_lazily_on_first_call():
    """build_messages alone doesn't create state; record_turn does."""
    # A pure build shouldn't leave a store behind (else error-path polls fill
    # the dict with empty lists).
    history.build_messages("SYS", thread_id=99, user_text="probe")
    assert 99 not in history._history

    history.record_turn(99, max_turns=1, user_text="u", assistant_text="a")
    assert 99 in history._history
    assert list(history._history[99]) == [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ]
    # Would fail if build_messages eagerly created stores (would leave a
    # list at thread 99 before record_turn ran), or if record_turn silently
    # dropped its arguments when the thread was previously unseen.


# ---------- integration: bot.on_message error path ----------

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
    bot = False


class _FakeMessage:
    def __init__(self, content, channel):
        self.content = content
        self.channel = channel
        self.author = _FakeAuthor()


@pytest.mark.asyncio
async def test_llm_error_does_not_append_to_history(monkeypatch):
    """When ask_llm raises, history for that thread stays empty."""
    import bot

    # `bot._lock` is module-scope now (created at import); no per-test init.

    channel = _FakeChannel(bot.CHANNEL_ID)
    message = _FakeMessage("hi", channel)

    async def _boom(_messages, _thread_id):
        raise RuntimeError("simulated provider outage")

    monkeypatch.setattr(bot, "ask_llm", _boom)

    await bot.on_message(message)

    # User saw an error string.
    assert channel.sent, "expected an error reply to be sent to the channel"
    assert channel.sent[0].startswith("[error:")

    # And crucially, history must be untouched — the next build sees a clean
    # [system, user] pair with no failed turn in between.
    next_msgs = history.build_messages("SYS", channel.id, "next")
    assert next_msgs == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "next"},
    ]
    # Would fail if bot.on_message appended unconditionally (the failed turn
    # would appear between system and next-user), or if record_turn ran
    # before the try/except sanity check.


@pytest.mark.asyncio
async def test_llm_success_does_append_to_history(monkeypatch):
    """Sanity check the positive path: successful reply IS recorded.

    Without this, a broken record_turn (e.g. always-no-op) would silently
    pass the error-path test above.
    """
    import bot

    channel = _FakeChannel(bot.CHANNEL_ID)
    message = _FakeMessage("hello", channel)

    async def _ok(_messages, _thread_id):
        return "world"

    monkeypatch.setattr(bot, "ask_llm", _ok)

    await bot.on_message(message)

    assert channel.sent == ["world"]

    # The turn should now be visible in the next build.
    next_msgs = history.build_messages(bot.PERSONA, channel.id, "again")
    assert next_msgs == [
        {"role": "system", "content": bot.PERSONA},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
        {"role": "user", "content": "again"},
    ]

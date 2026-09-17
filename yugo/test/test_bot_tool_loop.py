"""
v0.4a — the loop in `bot.ask_llm`.

`test_tools.py` covers one tool call in isolation. This file drives the loop:
what `litellm.acompletion` is called with, what accumulates between rounds,
what stops it, and what reaches (and does not reach) the history store.

The seam is `litellm.acompletion` itself, not `bot.ask_llm` — patching
`ask_llm` is what the other suites do to test their own callers, and doing it
here would test nothing. Every scripted response carries a REAL
`litellm.types.utils.Message`: on a MagicMock, `message.tool_calls` is a
truthy MagicMock, so a loop that never checked for tool calls at all would
still "pass".

Mutation table is in the PR body; each test's docstring names the mutation it
is written against.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest
from litellm.types.utils import ChatCompletionMessageToolCall, Function, Message

import bot
import history
import litellm
import tools

def _tool_call(name: str, arguments: str, call_id: str = "call_1"):
    return ChatCompletionMessageToolCall(
        id=call_id, type="function", function=Function(name=name, arguments=arguments)
    )


def _response(message: Message):
    wrapper = MagicMock()
    wrapper.choices = [MagicMock()]
    wrapper.choices[0].message = message
    return wrapper


def _text(content: str):
    return _response(Message(content=content, role="assistant"))


def _calls(*tool_calls, content=None):
    return _response(Message(content=content, role="assistant", tool_calls=list(tool_calls)))


class _ScriptedProvider:
    """Returns the scripted responses in order, recording every kwarg set.

    Runs out loudly rather than repeating the last response — a loop that
    called one more time than the test expected would otherwise be invisible.
    """

    def __init__(self, *responses, delay: float = 0.0):
        self._responses = list(responses)
        self._delay = delay
        self.kwargs: list[dict] = []

    async def __call__(self, **kwargs):
        self._record(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if not self._responses:
            raise AssertionError(
                f"provider called {len(self.kwargs)} times; the script had "
                f"{len(self.kwargs) - 1}"
            )
        return self._responses.pop(0)

    def _record(self, kwargs: dict) -> None:
        """Snapshot the payload rather than keeping the caller's reference.

        The loop appends to its working list between rounds, so a stored
        reference would show every round the FINAL state and no assertion
        about round ordering could fail.
        """
        snapshot = dict(kwargs)
        snapshot["messages"] = [dict(m) for m in kwargs["messages"]]
        self.kwargs.append(snapshot)

    @property
    def call_count(self) -> int:
        return len(self.kwargs)


class _AlwaysCalls(_ScriptedProvider):
    """Never stops asking for the probe. For the ceiling + deadline tests."""

    async def __call__(self, **kwargs):
        self._record(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        return _calls(_tool_call("loop_probe", '{"note": "again"}'))


class _AlwaysHang(_ScriptedProvider):
    """Always asks for the `hang` tool. For the handler-deadline test."""

    async def __call__(self, **kwargs):
        self._record(kwargs)
        return _calls(_tool_call("hang", "{}"))


@pytest.fixture
def probe_declared(monkeypatch, tmp_path):
    """Give the module the file-granted probe plus a temp audit file."""
    selected = {"loop_probe": tools.REGISTRY["loop_probe"]}
    audit_path = tmp_path / "tool-audit.jsonl"
    monkeypatch.setattr(bot, "TOOLS", selected)
    monkeypatch.setattr(bot, "TOOL_SCHEMAS", tools.schemas(selected))
    monkeypatch.setattr(bot, "TOOL_AUDIT", tools.ToolAuditLog(str(audit_path)))
    return audit_path


@pytest.fixture(autouse=True)
def _clean_history():
    history._reset_for_tests()
    yield
    history._reset_for_tests()


# ---------- the tools= parameter ----------


@pytest.mark.asyncio
async def test_no_granted_tools_means_no_tools_parameter_at_all(monkeypatch):
    """An absent/empty grant file must produce v0.3's exact provider call.
    `tools=[]` is not the same thing — several providers reject an empty tool
    array outright, so a bot that declared nothing would stop answering.

    Bite-check: pass `tools=TOOL_SCHEMAS` unconditionally and `"tools"` shows
    up in the kwargs with an empty list.
    """
    monkeypatch.setattr(bot, "TOOLS", {})
    monkeypatch.setattr(bot, "TOOL_SCHEMAS", [])
    provider = _ScriptedProvider(_text("plain answer"))
    monkeypatch.setattr(litellm, "acompletion", provider)

    assert await bot.ask_llm([{"role": "user", "content": "hi"}], 1) == "plain answer"
    assert provider.call_count == 1
    assert "tools" not in provider.kwargs[0], (
        f"tools= must be omitted entirely when nothing is declared; "
        f"got {provider.kwargs[0].get('tools')!r}"
    )


@pytest.mark.asyncio
async def test_declared_tools_are_sent_on_every_round(monkeypatch, probe_declared):
    """Including the round AFTER a tool result. A provider that stops seeing
    the schemas mid-turn cannot make a second call, so the model dead-ends
    with a tool it can no longer name.

    Bite-check: send `tools=` only on the first completion (hoist it out of
    the loop body) and the second call's kwargs lose it.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", '{"note": "x"}')),
        _text("done"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    await bot.ask_llm([{"role": "user", "content": "probe"}], 1)

    assert provider.call_count == 2
    for index, kwargs in enumerate(provider.kwargs):
        assert kwargs["tools"] == bot.TOOL_SCHEMAS, f"round {index} lost tools="
        assert kwargs["tools"][0]["function"]["name"] == "loop_probe"


# ---------- the round trip ----------


@pytest.mark.asyncio
async def test_a_tool_round_appends_the_call_and_its_result_then_re_enters(
    monkeypatch, probe_declared
):
    """The whole slice in one assertion: the second completion's payload is
    the first one plus the `assistant`+`tool_calls` message and the matching
    `role: tool` result, in that order.

    Bite-check: drop the `working.append(assistant_tool_call_message(...))`
    line and the second payload has an orphan `role: tool` message — which is
    also a provider 400 in production, not just a red test.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", '{"note": "hello"}')),
        _text("the probe answered"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    reply = await bot.ask_llm(
        [
            {"role": "system", "content": "PERSONA"},
            {"role": "user", "content": "probe please"},
        ],
        1,
    )

    assert reply == "the probe answered"
    assert provider.call_count == 2

    first, second = provider.kwargs[0]["messages"], provider.kwargs[1]["messages"]
    assert [m["role"] for m in first] == ["system", "user"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]

    assistant = second[2]
    assert assistant["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "loop_probe", "arguments": '{"note": "hello"}'},
        }
    ]
    assert second[3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "loop_probe ok: hello",
    }


@pytest.mark.asyncio
async def test_every_call_in_one_round_gets_its_own_result_message(
    monkeypatch, probe_declared
):
    """Providers batch parallel tool calls into one assistant message. Each
    one needs its own `role: tool` reply keyed by `tool_call_id`, or the
    provider rejects the next request for an unanswered call.

    Bite-check: run only `tool_calls[0]` and the second payload holds one
    tool message instead of two.
    """
    provider = _ScriptedProvider(
        _calls(
            _tool_call("loop_probe", '{"note": "one"}', call_id="call_a"),
            _tool_call("loop_probe", '{"note": "two"}', call_id="call_b"),
        ),
        _text("both answered"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    await bot.ask_llm([{"role": "user", "content": "probe twice"}], 1)

    second = provider.kwargs[1]["messages"]
    tool_messages = [m for m in second if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call_a", "call_b"]
    assert [m["content"] for m in tool_messages] == [
        "loop_probe ok: one",
        "loop_probe ok: two",
    ]


@pytest.mark.asyncio
async def test_several_rounds_accumulate_rather_than_replace(
    monkeypatch, probe_declared
):
    """Round 2 must still see round 1. A loop that rebuilt `working` from the
    original messages each time would keep answering, keep looking sane, and
    keep hiding what the model already learned.

    Bite-check: reset `working` to the incoming messages at the top of the
    loop and the third payload loses round 1's pair.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", '{"note": "first"}', call_id="c1")),
        _calls(_tool_call("loop_probe", '{"note": "second"}', call_id="c2")),
        _text("finished"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    await bot.ask_llm([{"role": "user", "content": "twice"}], 1)

    third = provider.kwargs[2]["messages"]
    assert [m["role"] for m in third] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
    ]
    assert [m["content"] for m in third if m["role"] == "tool"] == [
        "loop_probe ok: first",
        "loop_probe ok: second",
    ]


@pytest.mark.asyncio
async def test_assistant_text_alongside_tool_calls_is_preserved(
    monkeypatch, probe_declared
):
    """Models narrate ("let me check") while calling. Dropping that text loses
    the model's own reasoning from its context for the rest of the turn.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", "{}"), content="let me check"),
        _text("done"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    await bot.ask_llm([{"role": "user", "content": "hi"}], 1)

    assistant = provider.kwargs[1]["messages"][1]
    assert assistant["content"] == "let me check"


@pytest.mark.asyncio
async def test_an_empty_assistant_content_becomes_none_not_empty_string(
    monkeypatch, probe_declared
):
    """LiteLLM's Anthropic transform rewrites `content: ""` into a literal
    `[System: Empty message content sanitised to satisfy protocol]` text
    block — a sentence the bot never said, sitting in its own context for the
    rest of the turn. `None` produces a clean `tool_use` block. The transform
    behaviour itself is pinned in `test_litellm_transforms.py`.

    Bite-check: `content or ""` in `assistant_tool_call_message` and this
    fails on the `is None` assertion.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", "{}"), content=""),
        _text("done"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    await bot.ask_llm([{"role": "user", "content": "hi"}], 1)

    assistant = provider.kwargs[1]["messages"][1]
    assert assistant["content"] is None


@pytest.mark.asyncio
async def test_the_rebuilt_assistant_message_carries_no_litellm_extras(
    monkeypatch, probe_declared
):
    """`Message.model_dump()` emits `function_call: null` and
    `provider_specific_fields: null`. Nothing downstream wants them and some
    providers reject them on the next request, so the message is rebuilt
    field by field.

    Bite-check: `return message.model_dump()` and the key set grows.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", "{}")),
        _text("done"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    await bot.ask_llm([{"role": "user", "content": "hi"}], 1)

    assistant = provider.kwargs[1]["messages"][1]
    assert set(assistant) == {"role", "content", "tool_calls"}


# ---------- what stops the loop ----------


@pytest.mark.asyncio
async def test_a_model_that_never_stops_hits_the_round_ceiling(
    monkeypatch, probe_declared
):
    """Without the ceiling this only ends at RESPONSE_TIMEOUT — up to two
    minutes of provider spend, and on the bus lane a subscription callback
    held for the whole of it.

    Bite-check: delete the `rounds_used >= TOOL_MAX_ROUNDS` branch and this
    hangs until the deadline instead of raising.
    """
    monkeypatch.setattr(bot, "TOOL_MAX_ROUNDS", 3)
    provider = _AlwaysCalls()
    monkeypatch.setattr(litellm, "acompletion", provider)

    with pytest.raises(bot.ToolLoopLimitError, match="ceiling of 3"):
        await bot.ask_llm([{"role": "user", "content": "hi"}], 1)

    # 3 rounds of tools, plus the completion that asked for a fourth.
    assert provider.call_count == 4


@pytest.mark.asyncio
async def test_response_timeout_bounds_the_whole_turn_not_each_completion(
    monkeypatch, probe_declared
):
    """N rounds under a PER-CALL timeout is an N×RESPONSE_TIMEOUT turn. On the
    bus lane that is the slow-consumer road `_ask_bus` already refuses to walk
    for `_lock`; on the Discord lane it is a user watching a typing indicator
    for ten minutes with `RESPONSE_TIMEOUT=120` set.

    Bite-check: pass `timeout=RESPONSE_TIMEOUT` instead of `timeout=remaining`
    and the loop runs all 20 rounds and raises `ToolLoopLimitError` instead —
    a different exception AND ~20 calls instead of a handful.
    """
    monkeypatch.setattr(bot, "RESPONSE_TIMEOUT", 0.5)
    monkeypatch.setattr(bot, "TOOL_MAX_ROUNDS", 20)
    provider = _AlwaysCalls(delay=0.15)
    monkeypatch.setattr(litellm, "acompletion", provider)

    with pytest.raises(asyncio.TimeoutError):
        await bot.ask_llm([{"role": "user", "content": "hi"}], 1)

    assert provider.call_count <= 8, (
        f"the turn deadline should have stopped this in a handful of rounds; "
        f"got {provider.call_count} completions"
    )


@pytest.mark.asyncio
async def test_slow_tools_spend_the_same_budget_the_provider_does(
    monkeypatch, probe_declared
):
    """v0.4c's `http_get` against a hanging host is the case. Time spent in a
    handler is time the turn does not have left, and the harness says so
    rather than leaning on `asyncio.wait_for`'s behaviour for a non-positive
    timeout.

    Bite-check: delete the `remaining <= 0` branch — `wait_for` still raises
    `TimeoutError`, but with no message, so the `match=` fails.
    """
    monkeypatch.setattr(bot, "RESPONSE_TIMEOUT", 0.2)

    async def _slow(_args):
        await asyncio.sleep(0.4)
        return "eventually"

    slow_tool = {
        "slow": tools.Tool(name="slow", description="d", parameters={}, handler=_slow)
    }
    monkeypatch.setattr(bot, "TOOLS", slow_tool)
    monkeypatch.setattr(bot, "TOOL_SCHEMAS", tools.schemas(slow_tool))

    provider = _ScriptedProvider(_calls(_tool_call("slow", "{}")), _text("never"))
    monkeypatch.setattr(litellm, "acompletion", provider)

    with pytest.raises(asyncio.TimeoutError, match="tool round"):
        await bot.ask_llm([{"role": "user", "content": "hi"}], 1)

    assert provider.call_count == 1


@pytest.mark.asyncio
async def test_an_empty_final_answer_still_raises_empty_response_error(
    monkeypatch, probe_declared
):
    """The v0.2a contract survives a tool round: an empty completion is an
    error path, never a recorded assistant turn.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", "{}")),
        _text("   "),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    with pytest.raises(bot.EmptyResponseError):
        await bot.ask_llm([{"role": "user", "content": "hi"}], 1)


# ---------- history: the rounds stay inside the turn ----------


@pytest.mark.asyncio
async def test_tool_rounds_never_enter_the_history_store(monkeypatch, probe_declared):
    """`history.record_turn` evicts from index 0 in PAIRS. A stored tool round
    would eventually lose its `assistant`+`tool_calls` message while the
    matching `role: tool` message survived, and an orphan tool result is a
    provider 400 on the next turn. Group-aware eviction is a real design
    change and does not belong to the scaffold.

    Bite-check: record the whole `working` list instead of the final reply and
    a `tool` role appears in the store — which also trips
    `history.record_turn`'s `ALLOWED_ROLES` check.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", '{"note": "seen"}')),
        _text("final answer"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    messages = history.build_messages("PERSONA", 42, "probe please")
    reply = await bot.ask_llm(messages, 42)
    history.record_turn(42, 10, "probe please", reply)

    store = history._history[42]
    assert store == [
        {"role": "user", "content": "probe please"},
        {"role": "assistant", "content": "final answer"},
    ]
    assert "loop_probe ok" not in json.dumps(store)


@pytest.mark.asyncio
async def test_the_loop_does_not_mutate_the_list_it_was_handed(
    monkeypatch, probe_declared
):
    """`build_messages` hands back a fresh list today, but `ask_llm` appends,
    and a caller that kept a reference must not watch it grow tool rounds. The
    store-aliasing class already bit this repo once (`*prior` in place of
    `*[dict(m) for m in prior]`).

    Bite-check: drop the `[dict(m) for m in messages]` copy and `handed`
    grows from 1 entry to 3.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", "{}")),
        _text("done"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    handed = [{"role": "user", "content": "hi"}]
    await bot.ask_llm(handed, 1)

    assert handed == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_a_bus_tool_round_leaves_the_discord_store_alone(
    monkeypatch, probe_declared
):
    """SPEC §8: a bus payload is untrusted external input, and a bus turn's
    tool round must not become context for the human channel. The two lanes
    key on different namespaces (`bus:<peer>` vs the int channel id), so the
    separation is structural — this pins it against a tool round, which is
    the new thing that could have crossed it.

    Bite-check: key `_ask_bus` on `CHANNEL_ID` (or have `ask_llm` write to the
    store itself) and the Discord store stops being empty.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", '{"note": "from the bus"}')),
        _text("bus answer"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    envelope = {
        "envelope_version": 1,
        "id": "peer-1",
        "from": "vec",
        "to": "yugo",
        "kind": "text_message",
        "ts": "2026-08-26T12:00:00.000Z",
        "payload": {"text": "run the probe"},
    }
    reply = await bot._ask_bus(envelope, "nonce-1")

    assert reply == "bus answer"
    assert set(history._history) == {"bus:vec"}, (
        f"a bus turn touched a store outside its namespace: "
        f"{sorted(history._history)}"
    )
    assert [m["role"] for m in history._history["bus:vec"]] == ["user", "assistant"]

    # And the audit line names the namespace, so the provenance of a tool call
    # made on behalf of an unauthenticated peer is recoverable after the fact.
    (line,) = [
        json.loads(raw)
        for raw in probe_declared.read_text().splitlines()
        if raw
    ]
    assert line["thread"] == "bus:vec"


@pytest.mark.asyncio
async def test_a_discord_tool_round_leaves_the_bus_stores_alone(
    monkeypatch, probe_declared
):
    """The other direction, which is the easy one to miss: a human's private
    conversation must not become context for a bus reply, and the tap mirrors
    bus traffic fleet-wide.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", '{"note": "from discord"}')),
        _text("discord answer"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    messages = history.build_messages("PERSONA", bot.CHANNEL_ID, "probe")
    reply = await bot.ask_llm(messages, bot.CHANNEL_ID)
    history.record_turn(bot.CHANNEL_ID, 10, "probe", reply)

    assert set(history._history) == {bot.CHANNEL_ID}

    (line,) = [
        json.loads(raw)
        for raw in probe_declared.read_text().splitlines()
        if raw
    ]
    assert line["thread"] == str(bot.CHANNEL_ID)
    assert not line["thread"].startswith("bus:")


# ---------- the audit fires from inside the loop ----------


@pytest.mark.asyncio
async def test_the_loop_audits_every_round_with_its_round_index(
    monkeypatch, probe_declared
):
    """SPEC §9 Universal wants an audit per CALL, and the round index is what
    lets a reader reconstruct a multi-round turn from the file.

    Bite-check: pass a constant `round_index=0` and the second line's `round`
    stops distinguishing the rounds.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", '{"note": "r0"}', call_id="c1")),
        _calls(_tool_call("loop_probe", '{"note": "r1"}', call_id="c2")),
        _text("done"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    await bot.ask_llm([{"role": "user", "content": "hi"}], 7)

    lines = [
        json.loads(raw) for raw in probe_declared.read_text().splitlines() if raw
    ]
    assert [line["round"] for line in lines] == [0, 1]
    assert [line["args"]["note"] for line in lines] == ["r0", "r1"]
    assert all(line["ok"] for line in lines)


@pytest.mark.asyncio
async def test_a_hallucinated_tool_name_is_audited_and_the_turn_survives(
    monkeypatch, probe_declared
):
    """A model naming a tool nobody declared must not kill the turn, and the
    attempt must still land in the audit — "the bot tried to call
    `write_file`" is exactly the line an operator wants to find.

    Bite-check: raise on an unknown name instead of returning a tool error and
    the turn dies with an exception instead of returning `recovered`.
    """
    provider = _ScriptedProvider(
        _calls(_tool_call("write_file", '{"path": "/etc/passwd"}')),
        _text("recovered"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    assert await bot.ask_llm([{"role": "user", "content": "hi"}], 1) == "recovered"

    (line,) = [
        json.loads(raw) for raw in probe_declared.read_text().splitlines() if raw
    ]
    assert line["tool"] == "write_file"
    assert line["ok"] is False
    assert line["args"] == {"path": "/etc/passwd"}

    # The model saw the refusal and could act on it.
    tool_message = provider.kwargs[1]["messages"][2]
    assert tool_message["role"] == "tool"
    assert "unknown tool" in tool_message["content"]


@pytest.mark.asyncio
async def test_a_provider_transform_that_pops_the_system_message_cannot_reach_us(
    monkeypatch, probe_declared
):
    """LiteLLM's Anthropic and Gemini transforms MUTATE the list they are
    handed — both pop the system entry, which `test_litellm_transforms` pins
    them doing and which that file's own module comment warns about. One
    completion never noticed. A loop that handed `working` over twice would
    have the persona removed from under it somewhere after round 1, and the
    only symptom would be the bot forgetting who it is mid-turn.

    Bite-check: pass `messages=working` instead of a per-round copy and the
    second payload starts at `user` — persona gone.
    """

    class _PoppingProvider(_ScriptedProvider):
        async def __call__(self, **kwargs):
            self._record(kwargs)
            # What AnthropicConfig.transform_request does to its argument.
            kwargs["messages"].pop(0)
            return self._responses.pop(0)

    provider = _PoppingProvider(
        _calls(_tool_call("loop_probe", "{}")),
        _text("still myself"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    reply = await bot.ask_llm(
        [
            {"role": "system", "content": "PERSONA"},
            {"role": "user", "content": "probe"},
        ],
        1,
    )

    assert reply == "still myself"
    assert provider.kwargs[1]["messages"][0] == {
        "role": "system",
        "content": "PERSONA",
    }, "the persona did not survive the first round's transform"


@pytest.mark.asyncio
async def test_every_bounded_await_gets_only_the_budget_the_turn_has_left(
    monkeypatch, probe_declared
):
    """The deterministic half of the deadline claim, and the one that bites.

    `test_response_timeout_bounds_the_whole_turn_not_each_completion` above
    proves the turn TERMINATES, but the `remaining <= 0` branch alone is
    enough to make it terminate — so a `timeout=RESPONSE_TIMEOUT` mutant
    survives it (it just overshoots the deadline by one whole completion
    before the next loop iteration notices). This asserts the property
    directly: every timed await in the turn gets the budget REMAINING at that
    moment, and those values strictly shrink.

    Five awaits, and the composition is the point — three completions plus the
    two tool handlers between them. An unbounded `await tool.handler(args)`
    (the first cut of this slice) shows up here as three values, not five.

    Bite-check: `timeout=RESPONSE_TIMEOUT` on the completion and the
    completion values stop decreasing; drop the handler's `wait_for` and the
    count falls to 3.
    """
    seen: list[float] = []
    real_wait_for = asyncio.wait_for

    async def _spy(awaitable, timeout):
        seen.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", _spy)
    monkeypatch.setattr(bot, "RESPONSE_TIMEOUT", 10)

    provider = _ScriptedProvider(
        _calls(_tool_call("loop_probe", "{}", call_id="c1")),
        _calls(_tool_call("loop_probe", "{}", call_id="c2")),
        _text("done"),
        delay=0.05,
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    await bot.ask_llm([{"role": "user", "content": "hi"}], 1)

    assert len(seen) == 5, (
        f"expected 3 completions + 2 bounded handler calls; got {seen!r}"
    )
    assert seen[0] <= 10, f"first round asked for more than the turn had: {seen!r}"
    assert all(later < earlier for earlier, later in zip(seen, seen[1:])), (
        f"every timed await must get the REMAINING budget, not the full "
        f"RESPONSE_TIMEOUT; got {seen!r}"
    )


# ---------- review round: the deadline reaches the handler (blocker 1) -------


@pytest.mark.asyncio
async def test_a_hanging_tool_cannot_outlive_the_turn(monkeypatch, probe_declared):
    """The blocker, end to end at the loop level.

    `run_tool_call` now takes a required budget, but the loop is what has to
    HAND it one. Before the fix this turn ran until something outside it
    intervened: measured at >8s against `RESPONSE_TIMEOUT=1.0`, with a
    30-second handler still going. On the bus lane that is a NATS
    subscription callback held open for the handler's whole life, which is
    the `http_get`-against-a-dead-host case arriving in v0.4c.

    Bite-check: drop `timeout=deadline - time.monotonic()` from the
    `run_tool_call` call — `run_tool_call` then raises TypeError for the
    missing argument, so this cannot regress silently either.
    """
    monkeypatch.setattr(bot, "RESPONSE_TIMEOUT", 0.5)

    async def _hang(_args):
        await asyncio.sleep(30)
        return "never"

    hanging = {
        "hang": tools.Tool(name="hang", description="d", parameters={}, handler=_hang)
    }
    monkeypatch.setattr(bot, "TOOLS", hanging)
    monkeypatch.setattr(bot, "TOOL_SCHEMAS", tools.schemas(hanging))

    provider = _AlwaysHang()
    monkeypatch.setattr(litellm, "acompletion", provider)

    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await bot.ask_llm([{"role": "user", "content": "hi"}], 1)
    elapsed = time.monotonic() - started

    assert elapsed < 5, (
        f"the turn ran {elapsed:.2f}s against RESPONSE_TIMEOUT=0.5 — the "
        "handler is not bounded by the turn deadline"
    )

    # And the cut-off call is on the record.
    (line,) = [
        json.loads(raw) for raw in probe_declared.read_text().splitlines() if raw
    ]
    assert line["tool"] == "hang"
    assert line["ok"] is False
    assert "timed out" in line["result"]


@pytest.mark.asyncio
async def test_the_second_call_in_a_round_sees_what_the_first_one_spent(
    monkeypatch, probe_declared
):
    """The budget is recomputed per CALL, not per round. Two slow tools in one
    assistant message must not each get the full remaining budget.

    Bite-check: hoist `timeout=` out of the `for call in tool_calls` loop
    (compute it once per round) and the second handler gets the same budget
    the first did, so both run to completion and no timeout is audited.
    """
    monkeypatch.setattr(bot, "RESPONSE_TIMEOUT", 0.4)

    async def _slow(_args):
        await asyncio.sleep(0.3)
        return "slow ok"

    slow = {
        "slow": tools.Tool(name="slow", description="d", parameters={}, handler=_slow)
    }
    monkeypatch.setattr(bot, "TOOLS", slow)
    monkeypatch.setattr(bot, "TOOL_SCHEMAS", tools.schemas(slow))

    provider = _ScriptedProvider(
        _calls(
            _tool_call("slow", "{}", call_id="a"),
            _tool_call("slow", "{}", call_id="b"),
        ),
        _text("never reached"),
    )
    monkeypatch.setattr(litellm, "acompletion", provider)

    with pytest.raises(asyncio.TimeoutError):
        await bot.ask_llm([{"role": "user", "content": "hi"}], 1)

    lines = [
        json.loads(raw) for raw in probe_declared.read_text().splitlines() if raw
    ]
    assert len(lines) == 2, f"both calls must be audited; got {lines!r}"
    assert lines[0]["ok"] is True, "the first call had budget and should succeed"
    assert lines[1]["ok"] is False, (
        "the second call must inherit what the first left, not a fresh budget"
    )
    assert "timed out" in lines[1]["result"] or "no turn budget" in lines[1]["result"]


# ---------- re-review round: the refused ceiling batch ----------------------


class _AlwaysCallsTwice(_ScriptedProvider):
    """Never stops, and asks for TWO probes per round.

    A one-call batch cannot tell "audits the refused batch" from "audits the
    first refused call", and the ceiling is reached precisely when a model is
    emitting calls in bulk.
    """

    async def __call__(self, **kwargs):
        self._record(kwargs)
        return _calls(
            _tool_call("loop_probe", '{"note": "first"}', call_id="a"),
            _tool_call("loop_probe", '{"note": "second"}', call_id="b"),
        )


@pytest.mark.asyncio
async def test_calls_refused_at_the_round_ceiling_are_audited(
    monkeypatch, probe_declared
):
    """The ceiling raises on a batch that never reaches `run_tool_call`, so
    those calls were the only ones in the whole turn with no audit line — and
    they are the last batch a runaway model emits, which from v0.4b/4c means
    the paths and URLs an auditor most wants. Every other pre-handler refusal
    (unknown tool, bad shape, no budget) already audits.

    Bite-check: delete the `record_refused_call` loop in `bot.ask_llm` and the
    line count drops from 6 to 4 with no `refused` line at all.
    """
    monkeypatch.setattr(bot, "TOOL_MAX_ROUNDS", 2)
    provider = _AlwaysCallsTwice()
    monkeypatch.setattr(litellm, "acompletion", provider)

    with pytest.raises(bot.ToolLoopLimitError):
        await bot.ask_llm([{"role": "user", "content": "hi"}], 1)

    lines = [json.loads(x) for x in probe_declared.read_text().splitlines() if x]
    executed = [line for line in lines if "refused" not in line]
    refused = [line for line in lines if "refused" in line]

    # 2 rounds x 2 calls executed, then the batch that asked for a third.
    assert len(executed) == 4, lines
    assert len(refused) == 2, lines

    # Every refused call, not just the first: same tool, different arguments.
    assert [line["args"]["note"] for line in refused] == ["first", "second"]
    for line in refused:
        assert line["event"] == "tool_call"
        assert line["tool"] == "loop_probe"
        assert line["ok"] is False
        assert line["round"] == 2
        assert line["thread"] == "1"
        assert "ceiling of 2" in line["refused"]


@pytest.mark.asyncio
async def test_the_ceiling_still_raises_and_still_stops_the_loop(
    monkeypatch, probe_declared
):
    """CONTROL on the test above. Auditing the refused batch must not turn the
    ceiling into something the loop survives — a `record_refused_call` that
    swallowed the raise, or a loop that audited and then continued, would
    leave the previous test's assertions about executed lines intact while
    removing the only thing the ceiling exists to do.
    """
    monkeypatch.setattr(bot, "TOOL_MAX_ROUNDS", 2)
    provider = _AlwaysCallsTwice()
    monkeypatch.setattr(litellm, "acompletion", provider)

    with pytest.raises(bot.ToolLoopLimitError, match="ceiling of 2"):
        await bot.ask_llm([{"role": "user", "content": "hi"}], 1)

    # 2 tool rounds, plus the completion that asked for a third. Not more.
    assert provider.call_count == 3

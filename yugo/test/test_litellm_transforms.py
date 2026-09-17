"""
LiteLLM provider-transform verification for the v0.2b compacted-shape.

The architect's reshape rests on: a summary rendered as `role: user` in a
chronological slot MUST be preserved through LiteLLM's Anthropic + Gemini
transforms — whereas any `role: system` message (at any position) gets
hoisted to the provider's top-level `system`/`system_instruction` param and
collapsed into the persona. This is why v0.2b's summary is stored as a
`role: user` entry, not `role: system`.

These tests OBSERVE (not fabricate) the actual transform behavior at the
LiteLLM version pinned in `requirements.txt`. If the transform API drifts,
this file surfaces the drift.
"""

from __future__ import annotations

import copy


# Compacted-shape from the brief:
#   [ system=PERSONA, user=[Summary...], user=latest, ... ]
# WARNING: both transforms mutate their input `messages` list in place
# (pop the system entry). Each test MUST work off a deep copy, or a
# later test in the same run will observe a persona-stripped input.
_COMPACTED_MESSAGES = [
    {"role": "system", "content": "PERSONA content"},
    {
        "role": "user",
        "content": "[Summary of earlier conversation] we discussed X and Y.",
    },
    {"role": "user", "content": "now do Z"},
]


def _fresh_compacted() -> list[dict]:
    return copy.deepcopy(_COMPACTED_MESSAGES)


def _all_text(parts):
    """Flatten a list of {'text': str} parts to a single string for search."""
    return " ".join(p.get("text", "") for p in parts if isinstance(p, dict))


def test_anthropic_transform_hoists_persona_and_preserves_summary_as_user():
    """Anthropic transform:
      - `system` param contains ONLY the persona text.
      - `messages` still contains the summary text (as a user-role entry) in
        a chronological slot before the latest user text.
    """
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    cfg = AnthropicConfig()
    result = cfg.transform_request(
        model="claude-3-5-sonnet-latest",
        messages=_fresh_compacted(),
        optional_params={},
        litellm_params={},
        headers={},
    )

    # Top-level system MUST contain only the persona — not the summary.
    system_field = result["system"]
    # Some versions render `system` as a string; others as a list of
    # {'type': 'text', 'text': ...}. Handle both.
    if isinstance(system_field, list):
        system_text = _all_text(system_field)
    else:
        system_text = str(system_field)
    assert "PERSONA content" in system_text, (
        f"expected persona hoisted to top-level system; got {system_field!r}"
    )
    assert "Summary of earlier conversation" not in system_text, (
        f"summary MUST NOT be hoisted to system; got {system_field!r}"
    )

    # The messages array must still carry the summary text in the user side.
    msgs = result["messages"]
    assert len(msgs) >= 1
    # Anthropic merges consecutive same-role messages into one entry with
    # multi-part content; the summary + latest end up as parts on one user.
    all_user_text = ""
    for m in msgs:
        if m.get("role") == "user":
            content = m["content"]
            if isinstance(content, list):
                all_user_text += " " + _all_text(content)
            else:
                all_user_text += " " + str(content)

    assert "Summary of earlier conversation" in all_user_text, (
        "summary must survive as user-side content post-transform"
    )
    assert "now do Z" in all_user_text, (
        "latest user text must survive post-transform"
    )
    # And chronological order: summary text must appear before latest text
    # when the user content is flattened.
    assert all_user_text.index("Summary of earlier conversation") < all_user_text.index(
        "now do Z"
    ), "summary must precede latest user text in chronological order"


def test_gemini_transform_hoists_persona_and_preserves_summary_as_user():
    """Gemini transform (via `_transform_system_message` — the seam that runs
    inside `_transform_request_body` and populates `system_instruction`):

      - System messages are extracted into a separate `system_instruction`
        blob that will be attached to the request as `system_instruction`.
      - Remaining messages retain the summary user-turn in a user slot,
        chronologically before the latest user text.
    """
    from litellm.llms.vertex_ai.gemini.transformation import (
        _transform_system_message,
        _gemini_convert_messages_with_history,
    )

    system_instructions, remaining = _transform_system_message(
        supports_system_message=True, messages=_fresh_compacted()
    )

    # Persona goes to system_instruction (top-level Gemini field).
    # Shape is typically {'parts': [{'text': ...}]}.
    assert system_instructions is not None, "persona must produce a system_instruction"
    parts = system_instructions.get("parts") if isinstance(system_instructions, dict) else None
    assert parts is not None, f"unexpected system_instructions shape: {system_instructions!r}"
    system_text = _all_text(parts)
    assert "PERSONA content" in system_text
    assert "Summary of earlier conversation" not in system_text, (
        "summary MUST NOT be hoisted to Gemini system_instruction"
    )

    # Remaining messages still hold the summary as a user-role entry.
    assert any(
        m.get("role") == "user"
        and "Summary of earlier conversation" in (m.get("content") or "")
        for m in remaining
    ), (
        f"summary must survive as a user-role message in the remaining set; "
        f"got {remaining!r}"
    )

    # And the messages-to-contents converter preserves the text in a user
    # part, chronologically before the latest user text.
    contents = _gemini_convert_messages_with_history(
        messages=list(remaining), model="gemini-1.5-pro"
    )
    # Flatten every user role's parts.
    user_text = ""
    for c in contents:
        if c.get("role") == "user":
            user_text += " " + _all_text(c.get("parts", []))
    assert "Summary of earlier conversation" in user_text
    assert "now do Z" in user_text
    assert user_text.index("Summary of earlier conversation") < user_text.index("now do Z")


# ---------- negative case: mid-array role:system IS hoisted (not preserved) ---
#
# The positive tests above prove `role: user` summaries SURVIVE. The tests
# below prove the DUAL claim the whole compacted-shape design rests on:
# a mid-array `role: system` message (attacker-injected or naively spliced)
# gets HOISTED into the top-level system field regardless of position, and
# does NOT stay in the messages array as a chronological message. This
# converts an architectural claim ("hoist happens regardless of position")
# from a comment into an executable assertion — if either transform ever
# starts preserving mid-array system messages, the v0.2b design breaks and
# these tests fail loudly.

# Compacted-shape WITH an attacker-injected / mid-array system entry:
#   [ persona (system), user, MID-ARRAY SYSTEM, latest user ]
_INJECTED_MESSAGES = [
    {"role": "system", "content": "PERSONA content"},
    {"role": "user", "content": "earlier user turn"},
    {"role": "system", "content": "INJECTED mid-array system directive"},
    {"role": "user", "content": "now do Z"},
]


def _fresh_injected() -> list[dict]:
    return copy.deepcopy(_INJECTED_MESSAGES)


def test_anthropic_transform_hoists_mid_array_system_message_out_of_messages():
    """Anthropic transform: a `role: system` message at a mid-array position
    is hoisted into the top-level `system` param along with the persona —
    NOT preserved as a message entry in the returned `messages` array.

    If this claim ever breaks, v0.2b's decision to render summaries as
    `role: user` (not `role: system`) needs re-review: today a summary at
    a mid-array slot would collapse into system anyway; if hoisting stops,
    the shape rationale changes.
    """
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    cfg = AnthropicConfig()
    result = cfg.transform_request(
        model="claude-3-5-sonnet-latest",
        messages=_fresh_injected(),
        optional_params={},
        litellm_params={},
        headers={},
    )

    # No `role: system` survives inside the messages array.
    surviving_roles = [m.get("role") for m in result["messages"]]
    assert "system" not in surviving_roles, (
        f"mid-array role:system leaked into messages array; roles={surviving_roles!r}"
    )

    # And the injected system text ended up in the top-level system field
    # (hoisted, not dropped).
    system_field = result["system"]
    system_text = (
        _all_text(system_field) if isinstance(system_field, list) else str(system_field)
    )
    assert "INJECTED mid-array system directive" in system_text, (
        f"mid-array system directive must be hoisted to top-level system; "
        f"got {system_field!r}"
    )
    # And persona is still there — proves both system entries got hoisted,
    # not the second one silently replacing the first.
    assert "PERSONA content" in system_text, (
        f"persona must remain hoisted alongside injected system; "
        f"got {system_field!r}"
    )


def test_gemini_transform_hoists_mid_array_system_message_out_of_messages():
    """Gemini transform: `_transform_system_message` extracts EVERY
    `role: system` entry (persona + mid-array injection) into
    `system_instruction` — the remaining message list has NO system role.
    """
    from litellm.llms.vertex_ai.gemini.transformation import (
        _transform_system_message,
    )

    system_instructions, remaining = _transform_system_message(
        supports_system_message=True, messages=_fresh_injected()
    )

    # No system message survives in the remaining set.
    surviving_roles = [m.get("role") for m in remaining]
    assert "system" not in surviving_roles, (
        f"mid-array role:system leaked into Gemini's remaining messages; "
        f"roles={surviving_roles!r}"
    )

    # Both system entries hoisted into system_instruction.
    assert system_instructions is not None
    parts = (
        system_instructions.get("parts")
        if isinstance(system_instructions, dict)
        else None
    )
    assert parts is not None, f"unexpected system_instructions shape: {system_instructions!r}"
    system_text = _all_text(parts)
    assert "INJECTED mid-array system directive" in system_text, (
        f"mid-array system directive must be hoisted to Gemini system_instruction; "
        f"got {system_instructions!r}"
    )
    assert "PERSONA content" in system_text


# ---------- v0.4a: the tool-round message shapes -----------------------------
#
# `tools.assistant_tool_call_message` sends `content: None` — never `""` —
# when the model returned tool calls and no text, and `bot.ask_llm` hands
# LiteLLM a per-round COPY of its working list. Both choices are answers to
# observed behaviour of the pinned litellm, not preferences. These tests
# OBSERVE that behaviour so the choices stop being assertions in a comment.

_TOOL_ROUND = [
    {"role": "system", "content": "PERSONA content"},
    {"role": "user", "content": "run the probe"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "loop_probe", "arguments": '{"note": "abc"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "loop_probe ok: abc"},
]


def _anthropic_messages(messages):
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    return AnthropicConfig().transform_request(
        model="claude-3-5-sonnet-latest",
        messages=copy.deepcopy(messages),
        optional_params={},
        litellm_params={},
        headers={},
    )["messages"]


def test_anthropic_transform_renders_a_tool_round_as_tool_use_and_tool_result():
    """The round-trip shape yugo appends must survive into Anthropic's blocks:
    the assistant entry becomes a `tool_use` block keyed by the same id, and
    the `role: tool` entry becomes a `tool_result` pointing back at it.

    If this stops holding, the loop is emitting a shape the provider will
    reject and the failure surfaces here rather than in production.
    """
    messages = _anthropic_messages(_TOOL_ROUND)

    assistant = next(m for m in messages if m["role"] == "assistant")
    tool_use = [b for b in assistant["content"] if b.get("type") == "tool_use"]
    assert len(tool_use) == 1, f"expected one tool_use block; got {assistant!r}"
    assert tool_use[0]["id"] == "call_1"
    assert tool_use[0]["name"] == "loop_probe"
    assert tool_use[0]["input"] == {"note": "abc"}

    results = [
        block
        for m in messages
        if isinstance(m["content"], list)
        for block in m["content"]
        if block.get("type") == "tool_result"
    ]
    assert len(results) == 1, f"expected one tool_result block; got {messages!r}"
    assert results[0]["tool_use_id"] == "call_1"
    assert results[0]["content"] == "loop_probe ok: abc"


def test_assistant_tool_call_content_none_stays_clean_but_empty_string_does_not():
    """`content: None` produces ONLY the `tool_use` block. `content: ""`
    produces an extra text block reading `[System: Empty message content
    sanitised to satisfy protocol]` — a sentence the bot never said, in its
    own context for the rest of the turn, and one it can go on to quote.

    This is why `tools.assistant_tool_call_message` writes `None`. Observed at
    the pinned litellm version; if the sanitiser is ever removed, the second
    half of this test fails and the choice can be revisited.
    """
    clean = _anthropic_messages(_TOOL_ROUND)
    assistant = next(m for m in clean if m["role"] == "assistant")
    assert [b.get("type") for b in assistant["content"]] == ["tool_use"], (
        f"content=None must yield the tool_use block alone; got {assistant!r}"
    )

    with_empty = copy.deepcopy(_TOOL_ROUND)
    with_empty[2]["content"] = ""
    sanitised = _anthropic_messages(with_empty)
    injected = next(m for m in sanitised if m["role"] == "assistant")
    texts = [b["text"] for b in injected["content"] if b.get("type") == "text"]
    assert texts and "sanitis" in texts[0], (
        "expected the empty-content sanitiser to fire on content='' — if it "
        "no longer does, assistant_tool_call_message may stop caring which "
        f"of the two it emits; got {injected!r}"
    )

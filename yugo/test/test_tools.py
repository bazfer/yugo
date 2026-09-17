"""
v0.4a — tool declaration, one tool call, and the audit line.

Split from `test_bot_tool_loop.py` on purpose: this file exercises `tools.py`
in isolation (parse a persona, run one call, read the audit back), the other
drives the whole loop through `bot.ask_llm`. A fault in either is a different
fix.

Every fake `tool_call` here is a REAL `litellm.types.utils.
ChatCompletionMessageToolCall`, never a MagicMock. A MagicMock answers every
attribute with a truthy MagicMock, so `run_tool_call` reading `call.function.
name` off one would "work" against any implementation — including one that
never looks at the name at all.
"""

import asyncio
import json
import os
import stat
import time

import pytest
from litellm.types.utils import ChatCompletionMessageToolCall, Function

import tools


def _call(name: str, arguments, call_id: str = "call_1"):
    return ChatCompletionMessageToolCall(
        id=call_id, type="function", function=Function(name=name, arguments=arguments)
    )


def _audit_lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# A lone surrogate, built at RUNTIME rather than written as a source literal.
# Python 3.13 refuses to marshal a code object holding one, so `"\ud800"` in
# this file would compile under CI's 3.12 and fail at import inside the
# container, which runs 3.13 — a green test suite behind a container that
# cannot start. `chr()` keeps the source plain ASCII.
_LONE_SURROGATE = chr(0xD800)


# ---------- grant file declaration (SPEC §9.1) ----------


def _grant(tmp_path, content: str, mode: int = 0o644):
    path = tmp_path / "tools.yaml"
    path.write_text(content)
    path.chmod(mode)
    return path


def test_absent_file_grants_nothing_and_logs_path(tmp_path, caplog):
    path = tmp_path / "absent.yaml"
    assert tools.resolve_declared(path) == {}
    assert f"path={path.resolve()} found=False granted=0" in caplog.text


def test_empty_file_grants_nothing(tmp_path):
    assert tools.resolve_declared(_grant(tmp_path, "")) == {}


@pytest.mark.parametrize("content", ["version: 1\n", "version: 1\ntools: null\n"])
def test_tools_missing_or_null_grants_nothing(tmp_path, content):
    assert tools.resolve_declared(_grant(tmp_path, content)) == {}


@pytest.mark.parametrize("value", ["null", "{}"])
def test_valid_null_or_empty_config_grants_tool(tmp_path, value):
    selected = tools.resolve_declared(
        _grant(tmp_path, f"version: 1\ntools:\n  loop_probe: {value}\n")
    )
    assert list(selected) == ["loop_probe"]


@pytest.mark.parametrize(
    ("content", "named"),
    [
        ("version: [", "malformed YAML"),
        ("- version\n- 1\n", "root key"),
        ("tools: {}\n", "version key"),
        ("version: 2\n", "version key"),
        ("version: 1.0\n", "version key"),
        ("version: true\n", "version key"),
        ("version: 1\ntypo: true\n", "typo"),
        ("version: 1\ntools: []\n", "tools key"),
        ("version: 1\ntools:\n  Bad-Name: null\n", "Bad-Name"),
        ("version: 1\ntools:\n  loop_probe: null\n  loop_probe: {}\n", "loop_probe"),
        (
            'version: 1\ntools:\n  loop_probe: null\n  "loop_probe": {}\n',
            "loop_probe",
        ),
        ("version: 1\ntools:\n  <<: {loop_probe: null}\n", "merge key"),
        (
            "version: 1\ntools:\n  <<: {loop_probe: null}\n  loop_probe: {}\n",
            "merge key",
        ),
        (
            "version: 1\ntools: &tools\n  loop_probe: *tools\n",
            "loop_probe",
        ),
        (
            "version: 1\ntools:\n  loop_probe:\n"
            "    first: &shared {}\n    second: *shared\n",
            "loop_probe",
        ),
        ("version: 1\ntools:\n  loop_probe: yes\n", "loop_probe"),
        ("version: 1\ntools:\n  loop_probe:\n    surprise: 1\n", "surprise"),
    ],
)
def test_every_invalid_grant_aborts_naming_the_offending_key(tmp_path, content, named):
    with pytest.raises(tools.ToolDeclarationError, match=named):
        tools.resolve_declared(_grant(tmp_path, content))


def test_two_different_tool_names_survive_duplicate_detection(tmp_path):
    registry = {"alpha": tools.REGISTRY["loop_probe"], "beta": tools.REGISTRY["loop_probe"]}
    selected = tools.resolve_declared(
        _grant(tmp_path, "version: 1\ntools:\n  alpha: null\n  beta: {}\n"),
        registry=registry,
    )
    assert list(selected) == ["alpha", "beta"]


@pytest.mark.parametrize("mode", [0o664, 0o646])
def test_group_or_world_writable_file_aborts_naming_mode(tmp_path, mode):
    with pytest.raises(tools.ToolDeclarationError, match="group/world-writable"):
        tools.resolve_declared(_grant(tmp_path, "version: 1\n", mode))


def test_unsafe_python_tag_is_not_constructed(tmp_path):
    marker = tmp_path / "owned"
    path = _grant(
        tmp_path,
        "version: 1\ntools: !!python/object/apply:os.system "
        f"['touch {marker}']\n",
    )
    with pytest.raises(tools.ToolDeclarationError, match="malformed YAML"):
        tools.resolve_declared(path)
    assert not marker.exists()


def test_resolved_path_found_and_count_are_logged(tmp_path, caplog):
    path = _grant(tmp_path, "version: 1\ntools:\n  loop_probe: null\n")
    tools.resolve_declared(path)
    assert f"path={path.resolve()} found=True granted=1" in caplog.text


# ---------- the probe itself ----------


@pytest.mark.asyncio
async def test_loop_probe_echoes_its_note():
    assert await tools._loop_probe({"note": "abc"}) == "loop_probe ok: abc"


@pytest.mark.asyncio
async def test_loop_probe_with_no_arguments_still_answers():
    assert await tools._loop_probe({}) == "loop_probe ok: "


@pytest.mark.asyncio
async def test_loop_probe_rejects_a_non_string_note():
    with pytest.raises(ValueError, match="must be a string"):
        await tools._loop_probe({"note": 7})


@pytest.mark.asyncio
async def test_loop_probe_rejects_an_over_long_note():
    """The probe echoes model-authored text into an audit line whose `args`
    field is deliberately NOT truncated. Without this bound one call could
    write an arbitrarily large line.
    """
    with pytest.raises(ValueError, match="at most"):
        await tools._loop_probe({"note": "x" * (tools.PROBE_NOTE_MAX_CHARS + 1)})


# ---------- argument decoding ----------


@pytest.mark.parametrize("raw", [None, "", "   ", "{}"])
def test_the_three_spellings_of_no_arguments_all_decode_to_empty(raw):
    """Providers spell an argument-less call as `None`, `""` or `"{}"`.
    `json.loads` raises on the first two, so without this a no-argument tool
    call would fail for a reason the model cannot act on.

    Bite-check: drop the blank/None branch from `_decode_arguments` and the
    first three parameters raise `JSONDecodeError`.
    """
    assert tools._decode_arguments(raw) == {}


def test_valid_json_that_is_not_an_object_is_rejected():
    """`"[1, 2]"` parses fine and would then be handed to a handler expecting
    a mapping. Reject it where the error can still become a tool result.
    """
    with pytest.raises(ValueError, match="JSON object"):
        tools._decode_arguments("[1, 2]")


# ---------- one call, end to end ----------


@pytest.mark.asyncio
async def test_a_successful_call_returns_a_tool_message_and_audits_it(tmp_path):
    """SPEC §9 Universal: args + result summary + duration on every call.

    Bite-check: delete the `audit.record(...)` call and the audit file never
    appears; drop `duration_ms` from the fields and the key assertion fails.
    """
    path = tmp_path / "audit" / "tool-audit.jsonl"
    audit = tools.ToolAuditLog(str(path))
    selected = {"loop_probe": tools.REGISTRY["loop_probe"]}

    message = await tools.run_tool_call(
        _call("loop_probe", '{"note": "hello"}'),
        selected=selected,
        audit=audit,
        thread_id=98765,
        round_index=0,
        timeout=5,
    )

    assert message == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "loop_probe ok: hello",
    }

    (line,) = _audit_lines(path)
    assert line["event"] == "tool_call"
    assert line["tool"] == "loop_probe"
    assert line["ok"] is True
    assert line["args"] == {"note": "hello"}
    assert line["result"] == "loop_probe ok: hello"
    assert line["result_len"] == len("loop_probe ok: hello")
    assert isinstance(line["duration_ms"], (int, float))
    assert line["round"] == 0
    assert line["thread"] == "98765"
    assert line["ts"].endswith("Z")


@pytest.mark.asyncio
async def test_the_audit_names_the_bus_namespace_that_drove_the_call(tmp_path):
    """Provenance, not decoration. A tool call made during a bus turn was made
    on behalf of another bot's UNAUTHENTICATED payload (SPEC §8), and the
    audit is where an operator finds that out. The `bus:` prefix `history.
    bus_thread_key` mints is what separates it from a Discord channel id.

    Bite-check: stop passing `thread_id` through to the audit fields (or
    hardcode it) and this fails while every behavioural loop test stays green.
    """
    import history

    path = tmp_path / "tool-audit.jsonl"
    audit = tools.ToolAuditLog(str(path))
    selected = {"loop_probe": tools.REGISTRY["loop_probe"]}

    await tools.run_tool_call(
        _call("loop_probe", "{}"),
        selected=selected,
        audit=audit,
        thread_id=history.bus_thread_key("vec"),
        round_index=2,
        timeout=5,
    )

    (line,) = _audit_lines(path)
    assert line["thread"] == "bus:vec"
    assert line["round"] == 2


@pytest.mark.asyncio
async def test_an_undeclared_tool_is_refused_without_running_anything(tmp_path):
    """SPEC §9's "no auto-discovery" is about what the model can REACH, not
    just what gets advertised. `run_tool_call` resolves against the
    file-granted set, so a name the build ships but this bot was not granted
    declare is still `unknown tool`.

    Bite-check: resolve against `tools.REGISTRY` instead of `selected` and
    this fails — `loop_probe` would run without an operator grant.
    """
    path = tmp_path / "tool-audit.jsonl"
    audit = tools.ToolAuditLog(str(path))

    message = await tools.run_tool_call(
        _call("loop_probe", '{"note": "hi"}'),
        selected={},  # grant file declared nothing
        audit=audit,
        thread_id=1,
        round_index=0,
        timeout=5,
    )

    assert message["role"] == "tool"
    assert message["tool_call_id"] == "call_1"
    assert "unknown tool" in message["content"]
    assert "loop_probe ok" not in message["content"]

    (line,) = _audit_lines(path)
    assert line["ok"] is False
    assert line["tool"] == "loop_probe"


@pytest.mark.asyncio
async def test_undecodable_arguments_come_back_as_a_tool_error_not_an_exception(
    tmp_path,
):
    """Raising here would strand an `assistant` message carrying `tool_calls`
    with no matching result, which is a provider 400 on the next request.
    The model gets a readable error instead and can retry.

    Bite-check: let `json.JSONDecodeError` propagate out of `run_tool_call`
    and this raises instead of returning.
    """
    path = tmp_path / "tool-audit.jsonl"
    audit = tools.ToolAuditLog(str(path))
    selected = {"loop_probe": tools.REGISTRY["loop_probe"]}

    message = await tools.run_tool_call(
        _call("loop_probe", "{not json"),
        selected=selected,
        audit=audit,
        thread_id=1,
        round_index=0,
        timeout=5,
    )

    assert message["role"] == "tool"
    assert message["content"].startswith("[tool error:")

    (line,) = _audit_lines(path)
    assert line["ok"] is False
    # Arguments never decoded, so the raw model output is what gets recorded —
    # an auditor still sees exactly what was attempted.
    assert "args" not in line
    assert "{not json" in line["args_raw"]


@pytest.mark.asyncio
async def test_a_raising_handler_becomes_a_tool_error_and_is_audited(tmp_path):
    """v0.4b's `read_file` will raise `FileNotFoundError` as a matter of
    routine. That is a tool result, not a dead turn.
    """
    path = tmp_path / "tool-audit.jsonl"
    audit = tools.ToolAuditLog(str(path))

    async def _explode(_args):
        raise RuntimeError("handler blew up")

    selected = {
        "boom": tools.Tool(
            name="boom", description="d", parameters={}, handler=_explode
        )
    }

    message = await tools.run_tool_call(
        _call("boom", "{}"),
        selected=selected,
        audit=audit,
        thread_id=1,
        round_index=0,
        timeout=5,
    )

    assert message["content"] == "[tool error: handler blew up]"
    (line,) = _audit_lines(path)
    assert line["ok"] is False
    assert line["args"] == {}


@pytest.mark.asyncio
async def test_the_audit_truncates_the_result_but_records_its_true_length(tmp_path):
    """§9 asks for a result SUMMARY. v0.4b's `read_file` can return megabytes
    and an audit line per megabyte is an audit nobody reads. `result_len`
    keeps the truncation honest.

    Bite-check: drop the `[:AUDIT_RESULT_MAX_CHARS]` slice and the length
    assertion on `result` fails.
    """
    path = tmp_path / "tool-audit.jsonl"
    audit = tools.ToolAuditLog(str(path))
    big = "y" * 5000

    async def _big(_args):
        return big

    selected = {
        "big": tools.Tool(name="big", description="d", parameters={}, handler=_big)
    }

    message = await tools.run_tool_call(
        _call("big", "{}"),
        selected=selected,
        audit=audit,
        thread_id=1,
        round_index=0,
        timeout=5,
    )

    # The MODEL still gets the whole thing — an output-size cap is a `full`
    # mode guard per SPEC §9 and is explicitly OFF in reduced-homelab mode.
    assert message["content"] == big

    (line,) = _audit_lines(path)
    assert len(line["result"]) == tools.AUDIT_RESULT_MAX_CHARS
    assert line["result_len"] == 5000


# ---------- the audit file itself ----------


def test_the_audit_file_is_created_0600(tmp_path):
    """It holds tool arguments — paths from v0.4b, URLs from v0.4c.

    Bite-check: swap `os.open(..., 0o600)` for a plain `open(path, "a")` and
    the mode comes out 0644 under the usual umask.
    """
    path = tmp_path / "nested" / "tool-audit.jsonl"
    tools.ToolAuditLog(str(path)).record("loop_probe", ok=True)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_an_unwritable_audit_path_does_not_kill_the_call(tmp_path, capsys):
    """A full disk or a missing bind mount is not a tool fault. The line still
    reaches stdout, where the container log keeps it.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    tools.ToolAuditLog(str(blocker / "sub" / "audit.jsonl")).record(
        "loop_probe", ok=True
    )
    assert "audit write failed" in capsys.readouterr().out


def test_an_unencodable_field_degrades_to_a_parseable_line(tmp_path):
    """One raw line would poison every consumer reading this JSONL from then
    on. `float('inf')` is the reachable case: `_canonical_json` runs with
    `allow_nan=False`.
    """
    path = tmp_path / "tool-audit.jsonl"
    tools.ToolAuditLog(str(path)).record("loop_probe", ok=True, weird=float("inf"))
    (line,) = _audit_lines(path)  # parses at all == the contract
    assert line["error"] == "audit_encode_failed"
    assert line["tool"] == "loop_probe"


def test_a_pathless_audit_log_writes_to_the_logger(tmp_path):
    written: list[str] = []
    tools.ToolAuditLog(None, logger=written.append).record("loop_probe", ok=True)
    assert len(written) == 1
    assert json.loads(written[0].removeprefix("[tool] "))["tool"] == "loop_probe"


# ---------- config ----------


def test_the_default_audit_path_is_the_one_spec_5_names():
    """SPEC §5 `YUGO_TOOL_AUDIT_PATH` — `/var/lib/yugo/tool-audit.jsonl`, moved
    off the `~/.claude/` convention by SEV3 v6. Pinned because a silent change
    here would split an operator's audit trail across two files at an upgrade.
    """
    assert tools.DEFAULT_TOOL_AUDIT_PATH == "/var/lib/yugo/tool-audit.jsonl"
    assert tools.audit_path_from_env({}) == tools.DEFAULT_TOOL_AUDIT_PATH


def test_a_blank_audit_path_falls_back_to_the_default():
    """`.env.example` doubles as CI's smoke-gate env-file, where a key with no
    value is normal. Blank must not disable the audit trail.
    """
    assert (
        tools.audit_path_from_env({"YUGO_TOOL_AUDIT_PATH": "   "})
        == tools.DEFAULT_TOOL_AUDIT_PATH
    )


def test_an_explicit_audit_path_wins():
    assert tools.audit_path_from_env({"YUGO_TOOL_AUDIT_PATH": "/x/y.jsonl"}) == "/x/y.jsonl"


# ---------- tripwire for v0.4b/v0.4c ----------


def test_registry_contains_only_the_shipped_v04c_set():
    """A side-effecting tool added without its sandbox suite changes reach."""
    assert set(tools.REGISTRY) == {
        "loop_probe",
        "read_file",
        "write_file",
        "list_dir",
        "http_get",
    }


def test_http_redirect_zero_is_the_only_zero_config_allowed(tmp_path):
    selected = tools.resolve_declared(
        _grant(tmp_path, "version: 1\ntools:\n  http_get:\n    max_redirects: 0\n")
    )
    assert "http_get" in selected
    with pytest.raises(tools.ToolDeclarationError, match="max_bytes"):
        tools.resolve_declared(
            _grant(tmp_path, "version: 1\ntools:\n  http_get:\n    max_bytes: 0\n")
        )


# ---------- review round: the declared shape is enforced (blocker 3) ---------


@pytest.mark.asyncio
async def test_an_undeclared_argument_key_is_refused_before_the_handler_runs(
    tmp_path,
):
    """`loop_probe` declares `additionalProperties: false` and nothing
    enforced it. A model could send a short `note` next to an arbitrarily
    large undeclared key, the handler would succeed, and the whole object
    would land in the audit line — an unbounded audit write driven by model
    output.

    Bite-check: delete the `additionalProperties` branch from
    `_check_declared_shape` and the probe answers `loop_probe ok: fine`.
    """
    path = tmp_path / "tool-audit.jsonl"
    audit = tools.ToolAuditLog(str(path))
    selected = {"loop_probe": tools.REGISTRY["loop_probe"]}

    message = await tools.run_tool_call(
        _call("loop_probe", json.dumps({"note": "fine", "payload": "x" * 50_000})),
        selected=selected,
        audit=audit,
        thread_id=1,
        round_index=0,
        timeout=5,
    )

    assert message["content"].startswith("[tool error:")
    assert "payload" in message["content"]
    assert "loop_probe ok" not in message["content"]

    (line,) = _audit_lines(path)
    assert line["ok"] is False


@pytest.mark.asyncio
async def test_a_missing_required_argument_is_refused(tmp_path):
    """The other half of the enforced subset. `loop_probe` requires nothing,
    so this uses a tool that does — the check has to be schema-driven, not
    special-cased to the probe.
    """
    path = tmp_path / "tool-audit.jsonl"

    async def _needs(args):
        return f"got {args['must']}"

    selected = {
        "needs": tools.Tool(
            name="needs",
            description="d",
            parameters={
                "type": "object",
                "properties": {"must": {"type": "string"}},
                "required": ["must"],
            },
            handler=_needs,
        )
    }

    message = await tools.run_tool_call(
        _call("needs", "{}"),
        selected=selected,
        audit=tools.ToolAuditLog(str(path)),
        thread_id=1,
        round_index=0,
        timeout=5,
    )

    assert "requires ['must']" in message["content"]


@pytest.mark.asyncio
async def test_a_schema_that_declares_neither_keyword_is_not_second_guessed(
    tmp_path,
):
    """Control for the two above. `_check_declared_shape` enforces what a
    schema ASKS for and nothing more — a tool with `parameters={}` (which
    several tests here use) must keep accepting anything, or the "not a JSON
    Schema validator" claim in its docstring is false.
    """

    async def _anything(args):
        return f"got {sorted(args)}"

    selected = {
        "anything": tools.Tool(
            name="anything", description="d", parameters={}, handler=_anything
        )
    }

    message = await tools.run_tool_call(
        _call("anything", '{"whatever": 1, "more": 2}'),
        selected=selected,
        audit=tools.ToolAuditLog(str(tmp_path / "a.jsonl")),
        thread_id=1,
        round_index=0,
        timeout=5,
    )

    assert message["content"] == "got ['more', 'whatever']"


@pytest.mark.asyncio
async def test_oversized_arguments_are_bounded_in_the_audit_line(tmp_path):
    """Shape enforcement alone does not close the unbounded-write hole: a
    DECLARED key can still carry megabytes, and args are audited whether or
    not the handler accepts them. `args_len` keeps the cut honest.

    Bite-check: emit `args` unconditionally in `_audit_args_fields` and the
    line grows to the full 50KB.
    """
    path = tmp_path / "tool-audit.jsonl"
    big = "z" * 50_000

    async def _accepts(_args):
        return "ok"

    selected = {
        "wide": tools.Tool(
            name="wide",
            description="d",
            parameters={"type": "object", "properties": {"note": {"type": "string"}}},
            handler=_accepts,
        )
    }

    await tools.run_tool_call(
        _call("wide", json.dumps({"note": big})),
        selected=selected,
        audit=tools.ToolAuditLog(str(path)),
        thread_id=1,
        round_index=0,
        timeout=5,
    )

    (line,) = _audit_lines(path)
    assert "args" not in line, "the full object must not be written"
    assert len(line["args_truncated"]) == tools.AUDIT_ARGS_MAX_CHARS
    assert line["args_truncated_flag"] is True
    assert line["args_len"] > 50_000
    assert len(path.read_text()) < 10_000, (
        f"one audit line grew to {len(path.read_text())} bytes"
    )


# ---------- review round: non-finite constants (blocker 4) -------------------


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constants_are_refused_at_decode(literal):
    """`json.loads` accepts all three as bare literals at any depth;
    `_canonical_json` runs with `allow_nan=False` and refuses them. Accepting
    a value the strict encoder will later reject is the bug class
    `fleet_bus._json_loads` was given this same hook to close (PR #6).

    Bite-check: drop `parse_constant=_reject_json_constant` and this decodes
    to `{'x': inf}` instead of raising.
    """
    with pytest.raises(ValueError, match="non-finite"):
        tools._decode_arguments(f'{{"x": {literal}}}')


def test_non_finite_constants_are_refused_at_any_depth():
    """Python's decoder accepts them nested, so a top-level-only check would
    be a guard with a hole in it."""
    with pytest.raises(ValueError, match="non-finite"):
        tools._decode_arguments('{"a": {"b": [1, Infinity]}}')


@pytest.mark.asyncio
async def test_a_non_finite_argument_produces_a_complete_audit_record(tmp_path):
    """The failure this closes end to end: before, the value decoded fine, the
    handler ran, the encoder then refused the audit line, and the fallback
    dropped args, result, duration, round AND thread — so the one pathological
    call was the one that lost its audit detail.

    Bite-check: restore the bare `json.loads` and this line comes back with
    `error: audit_encode_failed` and no `thread` / `duration_ms` / `round`.
    """
    path = tmp_path / "tool-audit.jsonl"
    selected = {"loop_probe": tools.REGISTRY["loop_probe"]}

    message = await tools.run_tool_call(
        _call("loop_probe", '{"note": "hi", "x": Infinity}'),
        selected=selected,
        audit=tools.ToolAuditLog(str(path)),
        thread_id="bus:vec",
        round_index=1,
        timeout=5,
    )

    assert message["content"].startswith("[tool error:")
    assert "non-finite" in message["content"]

    (line,) = _audit_lines(path)
    assert "error" not in line, f"the line should encode cleanly; got {line!r}"
    assert line["ok"] is False
    assert line["thread"] == "bus:vec"
    assert line["round"] == 1
    assert "duration_ms" in line
    # Decoding never produced an object, so the model's literal text is what
    # gets recorded — an auditor still sees exactly what was attempted.
    assert "Infinity" in line["args_raw"]


def test_an_unencodable_field_keeps_every_other_field_on_the_line(tmp_path):
    """Defence in depth behind the decode fix. Whatever future field turns out
    to be unencodable, the salvage path must keep ts/tool/thread/round/ok/
    duration_ms rather than collapsing to a three-key stub.

    Bite-check: revert `record` to emitting the bare {ts, event, tool, error}
    stand-in and every assertion below the first one fails.
    """
    path = tmp_path / "tool-audit.jsonl"
    tools.ToolAuditLog(str(path)).record(
        "loop_probe",
        thread="bus:vec",
        round=2,
        ok=True,
        duration_ms=1.5,
        weird=float("inf"),
    )

    (line,) = _audit_lines(path)  # parses at all == the JSONL contract holds
    assert line["error"] == "audit_encode_failed"
    assert line["tool"] == "loop_probe"
    assert line["thread"] == "bus:vec"
    assert line["round"] == 2
    assert line["ok"] is True
    assert line["duration_ms"] == 1.5
    assert line["weird"] == repr(float("inf"))


# ---------- review round: bounded + audited tool execution (blocker 1) -------


@pytest.mark.asyncio
async def test_a_handler_that_outruns_its_budget_is_cut_off_and_audited(tmp_path):
    """SPEC §10's whole-turn bound has to reach the handler. Before this, a
    hanging tool held the turn — and on the bus lane the NATS subscription
    callback driving it — open indefinitely.

    Bite-check: `await tool.handler(args)` without `wait_for` and this hangs
    for 30s instead of returning at ~0.2s.
    """
    path = tmp_path / "tool-audit.jsonl"

    async def _hang(_args):
        await asyncio.sleep(30)
        return "never"

    selected = {
        "hang": tools.Tool(name="hang", description="d", parameters={}, handler=_hang)
    }

    started = time.monotonic()
    message = await tools.run_tool_call(
        _call("hang", "{}"),
        selected=selected,
        audit=tools.ToolAuditLog(str(path)),
        thread_id=1,
        round_index=0,
        timeout=0.2,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"the handler was not cut off; took {elapsed:.2f}s"
    assert "timed out" in message["content"]
    assert message["tool_call_id"] == "call_1"

    (line,) = _audit_lines(path)
    assert line["ok"] is False
    assert "timed out" in line["result"]
    assert line["duration_ms"] >= 150


@pytest.mark.asyncio
async def test_a_call_with_no_budget_left_fails_without_starting_the_handler(
    tmp_path,
):
    """Several tool calls can arrive in one assistant message and they share
    one turn budget. The second must not start when the first spent it — and
    it must still be audited, not silently skipped.
    """
    path = tmp_path / "tool-audit.jsonl"
    ran: list[str] = []

    async def _record(_args):
        ran.append("started")
        return "ok"

    selected = {
        "t": tools.Tool(name="t", description="d", parameters={}, handler=_record)
    }

    message = await tools.run_tool_call(
        _call("t", "{}"),
        selected=selected,
        audit=tools.ToolAuditLog(str(path)),
        thread_id=1,
        round_index=0,
        timeout=-0.5,
    )

    assert ran == [], "the handler ran with no budget left"
    assert "no turn budget left" in message["content"]
    (line,) = _audit_lines(path)
    assert line["ok"] is False


@pytest.mark.asyncio
async def test_a_cancelled_tool_call_is_still_audited(tmp_path):
    """`asyncio.CancelledError` derives from `BaseException`, so the `except
    Exception` chain does not see it. A bot shut down — or a bus supervisor
    cancelled by `stop_bus` — mid-tool-call would otherwise leave the call
    unrecorded, and an unaudited tool call is worse than a slow one.

    Bite-check: move the `audit.record(...)` out of the `finally` and back
    below the except chain; the cancellation still propagates but the audit
    file is never created.
    """
    path = tmp_path / "tool-audit.jsonl"

    async def _slow(_args):
        await asyncio.sleep(30)
        return "never"

    selected = {
        "slow": tools.Tool(name="slow", description="d", parameters={}, handler=_slow)
    }

    task = asyncio.create_task(
        tools.run_tool_call(
            _call("slow", "{}"),
            selected=selected,
            audit=tools.ToolAuditLog(str(path)),
            thread_id="bus:vec",
            round_index=0,
            timeout=30,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    (line,) = _audit_lines(path)
    assert line["ok"] is False
    assert line["thread"] == "bus:vec"
    assert "cancelled" in line["result"]


def test_run_tool_call_has_no_default_timeout():
    """The class fix for blocker 1. A default — including `None` for
    "unbounded" — lets the next caller reintroduce exactly the hole this
    round closed, and no behavioural test would notice because the default
    would look deliberate.

    Bite-check: give `timeout` any default and this fails.
    """
    import inspect

    parameter = inspect.signature(tools.run_tool_call).parameters["timeout"]
    assert parameter.default is inspect.Parameter.empty, (
        "run_tool_call.timeout must be a REQUIRED keyword — a default is how "
        "an unbounded handler await gets reintroduced"
    )
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


# ---------- re-review round: the pre-decoded argument path -------------------
#
# `parse_constant` only fires while PARSING A STRING. `_decode_arguments`
# returned an already-decoded mapping untouched, so a `NaN` inside one reached
# the handler, then `_audit_args_fields` raised inside `run_tool_call`'s
# `finally` — before `audit.record`. Result: the call ran, NO audit line was
# written at all, and `run_tool_call` raised out of a function documented
# never to raise for a tool fault, stranding an assistant `tool_calls`
# message with no matching result (a provider 400 on the next request).
#
# The path is reachable on litellm's OWN types, not just a hand-rolled stub:
# `Function` declares `arguments: str` but pydantic v2 does not validate on
# assignment, and the class deliberately exposes `__setitem__`/`setattr`. The
# tests below therefore mutate a real `ChatCompletionMessageToolCall`.


def _call_with_decoded_arguments(name: str, arguments, call_id: str = "call_1"):
    """A REAL tool call whose `arguments` is a mapping, not a string.

    Built through `Function(arguments="{}")` and then assigned, because
    `Function.__init__` json-dumps a dict it is passed at construction. The
    assignment is the production shape: unvalidated, and what survives.
    """
    call = _call(name, "{}", call_id)
    call.function.arguments = arguments
    assert isinstance(call.function.arguments, dict), (
        "litellm started validating on assignment; this whole section's "
        "premise is gone and the tests below prove nothing"
    )
    return call


def test_a_pre_decoded_mapping_with_a_non_finite_value_is_refused():
    """The blocker, at the unit.

    Bite-check: return `decoded` without the encodability check and this
    returns `{'v': nan}` instead of raising.
    """
    with pytest.raises(ValueError, match="not audit-encodable"):
        tools._decode_arguments({"v": float("nan")})


def test_a_pre_decoded_mapping_is_checked_at_any_depth():
    """A top-level-only check is the same guard-with-a-hole the string path
    already had to fix."""
    with pytest.raises(ValueError, match="not audit-encodable"):
        tools._decode_arguments({"a": {"b": [1, float("inf")]}})


def test_a_pre_decoded_mapping_holding_a_non_json_object_is_refused():
    """`NaN` is one member of the class, not the class. A mapping that never
    went through a JSON parser can hold a `set`, a `datetime` or raw bytes,
    and all of them fail the same encoder for the same reason — so the check
    is a real encode rather than a non-finite scan.

    Bite-check: narrow the check to `math.isfinite` on floats and this passes
    a `set` straight through to the same `finally` that lost the audit line.
    """
    with pytest.raises(ValueError, match="not audit-encodable"):
        tools._decode_arguments({"tags": {"a", "b"}})


def test_an_ordinary_pre_decoded_mapping_is_still_accepted():
    """CONTROL. Without it, a `_decode_arguments` that refused EVERY mapping
    would pass all three tests above — and would break the branch outright.
    The mapping path must survive; only unencodable ones must not.
    """
    assert tools._decode_arguments({"note": "hi", "n": 1}) == {"note": "hi", "n": 1}


@pytest.mark.asyncio
async def test_a_pre_decoded_non_finite_argument_still_audits_the_call(tmp_path):
    """End to end, and the assertion that actually failed before: the audit
    file did not exist.

    The `NaN` sits in the tool's OWN declared key. An undeclared one would be
    refused by `_check_declared_shape` first, and the test would then pass
    against the unfixed decoder for a reason it does not name — the vacuous
    shape that survived two mutations last round.

    Bite-check: revert `_decode_arguments` and this raises `ValueError: Out of
    range float values are not JSON compliant: nan` out of `run_tool_call`
    and writes nothing.
    """
    path = tmp_path / "tool-audit.jsonl"
    selected = {"loop_probe": tools.REGISTRY["loop_probe"]}

    message = await tools.run_tool_call(
        _call_with_decoded_arguments("loop_probe", {"note": float("nan")}),
        selected=selected,
        audit=tools.ToolAuditLog(str(path)),
        thread_id="bus:vec",
        round_index=1,
        timeout=5,
    )

    # Named, so a refusal for any OTHER reason fails here rather than passing.
    assert "not audit-encodable" in message["content"], message["content"]
    assert message["tool_call_id"] == "call_1"

    (line,) = _audit_lines(path)
    assert "error" not in line, f"the line should encode cleanly; got {line!r}"
    assert line["ok"] is False
    assert line["thread"] == "bus:vec"
    assert line["round"] == 1
    assert "duration_ms" in line
    assert "nan" in line["args_raw"].lower()


def test_audit_args_fields_cannot_raise(tmp_path):
    """The second of two locks on one door. `_decode_arguments` now refuses
    what this encoder rejects, so reaching here needs a future caller who
    skipped it — which is exactly how the first lock got installed.

    It is called while building `audit.record`'s kwargs, so ANYTHING it throws
    costs the whole line: `record`'s own per-field salvage never runs, because
    `record` is never reached.

    Bite-check: drop the try/except and this raises instead of returning.
    """
    fields = tools._audit_args_fields(None, {"v": float("nan")})
    assert "nan" in fields["args_raw"].lower()
    assert "args_unencodable" in fields
    # And the salvaged shape must itself be encodable, or the line still dies.
    tools.ToolAuditLog(str(tmp_path / "a.jsonl")).record("t", ok=False, **fields)


# ---------- re-review round: refusals audited before execution --------------


def test_a_refused_call_is_audited_with_its_arguments(tmp_path):
    """`record_refused_call` is the ceiling's audit lane. Same
    `event: tool_call` + `ok: false` shape as every other pre-handler refusal,
    plus `refused` naming why.

    Bite-check: drop `**_audit_args_fields(...)` and the line records that
    something was refused without recording WHAT — which is the whole point
    for a batch that from v0.4b/4c carries paths and URLs.
    """
    path = tmp_path / "tool-audit.jsonl"
    tools.record_refused_call(
        _call("loop_probe", '{"note": "last"}'),
        audit=tools.ToolAuditLog(str(path)),
        thread_id="bus:vec",
        round_index=7,
        reason="tool loop ceiling of 7 round(s)",
    )

    (line,) = _audit_lines(path)
    assert line["event"] == "tool_call"
    assert line["tool"] == "loop_probe"
    assert line["ok"] is False
    assert line["thread"] == "bus:vec"
    assert line["round"] == 7
    assert line["duration_ms"] == 0.0
    assert line["refused"] == "tool loop ceiling of 7 round(s)"
    assert line["args"] == {"note": "last"}


def test_a_refused_call_with_undecodable_arguments_is_still_audited(tmp_path):
    """It runs on a path that is already raising. An audit helper that let its
    own failure escape would trade the turn's real error for its own.

    Bite-check: hoist `_decode_arguments` out of its try and this raises.
    """
    path = tmp_path / "tool-audit.jsonl"
    tools.record_refused_call(
        _call("loop_probe", "{not json"),
        audit=tools.ToolAuditLog(str(path)),
        thread_id="1",
        round_index=0,
        reason="tool loop ceiling of 1 round(s)",
    )

    (line,) = _audit_lines(path)
    assert line["ok"] is False
    assert "not json" in line["args_raw"]


# ---------- re-review round: lone surrogates ("\ud800") --------------------
#
# Found by mutation, not by review. Replacing the decode-time encodability
# check with a non-finite scan left every test green, which said the check
# was under-specified: the class is "values the audit cannot write", and
# non-finite floats are one member of it.
#
# A lone surrogate is the member that needs no unusual input at all.
# `json.loads('{"note": "\\ud800"}')` is ordinary valid JSON and Python builds
# the surrogate from it; `_canonical_json` runs `ensure_ascii=False` and emits
# it happily; only `.encode("utf-8")` fails, as a `UnicodeEncodeError` — a
# `ValueError`, NOT an `OSError`. It therefore walked straight through the
# `except OSError` guarding the write, out of `record`, out of
# `run_tool_call`'s `finally`, and out of a function documented never to raise
# for a tool fault. The audit file was left CREATED AND EMPTY, which a
# consumer reads as "no tool calls happened" — strictly worse than absent.


def test_a_lone_surrogate_is_refused_at_decode():
    """Lock one. Ordinary JSON in, no malformed bytes anywhere.

    Bite-check: stop at `_canonical_json(decoded)` without `.encode("utf-8")`
    and this returns `{'note': '\\ud800'}` instead of raising.
    """
    with pytest.raises(ValueError, match="not audit-encodable"):
        tools._decode_arguments('{"note": "\\ud800"}')


@pytest.mark.asyncio
async def test_a_lone_surrogate_argument_leaves_a_written_audit_line(tmp_path):
    """The end-to-end failure: raised out of `run_tool_call`, audit file
    created with zero bytes in it.

    Bite-check: revert either lock and this fails — the decode check with a
    `UnicodeEncodeError` escaping, the writer check with an empty file.
    """
    path = tmp_path / "tool-audit.jsonl"
    selected = {"loop_probe": tools.REGISTRY["loop_probe"]}

    message = await tools.run_tool_call(
        _call("loop_probe", '{"note": "\\ud800"}'),
        selected=selected,
        audit=tools.ToolAuditLog(str(path)),
        thread_id="bus:vec",
        round_index=0,
        timeout=5,
    )

    assert "not audit-encodable" in message["content"], message["content"]
    (line,) = _audit_lines(path)
    assert line["ok"] is False
    assert line["thread"] == "bus:vec"
    assert "duration_ms" in line
    assert "ud800" in line["args_raw"]


def test_a_surrogate_reaching_the_writer_is_salvaged_not_raised(tmp_path):
    """Lock two, independent of lock one: the tool RESULT lands on the same
    line and never went through `_decode_arguments` at all, so a handler that
    echoes its input could put a surrogate there by itself.

    Bite-check: encode outside the salvage `try` (the original shape) and this
    raises `UnicodeEncodeError` and writes a zero-byte file.
    """
    path = tmp_path / "tool-audit.jsonl"
    tools.ToolAuditLog(str(path)).record(
        "loop_probe", thread="bus:vec", round=1, ok=True, duration_ms=2.0,
        result=_LONE_SURROGATE,
    )

    (line,) = _audit_lines(path)
    assert line["error"] == "audit_encode_failed"
    # Per-field salvage, not a stub: everything writable stays on the line.
    assert line["thread"] == "bus:vec"
    assert line["round"] == 1
    assert line["ok"] is True
    assert line["duration_ms"] == 2.0
    assert "ud800" in line["result"]


def test_a_surrogate_in_the_tool_name_still_writes_a_parseable_line(tmp_path):
    """`tool` is model-supplied like everything else, and it is the one field
    the last-resort fallback used to copy from the RAW entry — so a surrogate
    there defeated the fallback that exists to survive exactly this.

    The bar is not fidelity, it is a parseable stream: one unwritable line
    would otherwise poison every consumer reading this JSONL from then on.
    """
    path = tmp_path / "tool-audit.jsonl"
    tools.ToolAuditLog(str(path)).record(_LONE_SURROGATE, ok=False)

    (line,) = _audit_lines(path)  # parses at all == the assertion
    assert line["event"] == "tool_call"
    assert line["ok"] is False


def test_a_pathless_audit_log_survives_a_surrogate(tmp_path, capsys):
    """The logger branch is the other exit and it is the one used when no
    audit path is configured. `print`ing a lone surrogate to a UTF-8 stream
    raises the same way writing it does.

    Bite-check: log the pre-encode `str` and this raises instead.
    """
    tools.ToolAuditLog(None).record("loop_probe", ok=True, result=_LONE_SURROGATE)
    assert "audit_encode_failed" in capsys.readouterr().out


class _UnprintableRepr:
    """An object whose `repr` is ITSELF unwritable. Drives the last resort.

    Per-field salvage coerces an unencodable value with `repr()`, and for
    every ordinary value — including a surrogate-bearing `str`, whose repr is
    escaped and therefore ASCII — that is enough. This is the case it is not.
    """

    def __repr__(self) -> str:
        return _LONE_SURROGATE


def test_the_last_resort_line_is_written_when_even_the_repr_fails(tmp_path):
    """Without this the belt-and-braces branch is unreachable by any test and
    the claim that it works is untested assertion.

    Bite-check: drop the second `try` around `_encode_audit_line(salvaged)`
    and this raises `UnicodeEncodeError` and writes a zero-byte file.
    """
    path = tmp_path / "tool-audit.jsonl"
    tools.ToolAuditLog(str(path)).record("loop_probe", ok=False, weird=_UnprintableRepr())

    (line,) = _audit_lines(path)  # parses at all == the assertion
    assert line["error"] == "audit_encode_failed"
    assert line["tool"] == "loop_probe"


def test_the_last_resort_takes_the_tool_name_from_the_salvaged_entry(tmp_path):
    """`tool` is model-supplied like every other field, so the last resort
    copying it from the RAW entry re-imports the exact class of value that
    forced the fallback in the first place.

    Asserts the FIDELITY of the name, not merely that the line parses: a raw
    surrogate would make the last-resort encode raise, and a future
    `errors="replace"` softening of it would keep the line parseable while
    silently losing what the model actually asked for.
    """
    path = tmp_path / "tool-audit.jsonl"
    tools.ToolAuditLog(str(path)).record(_LONE_SURROGATE, ok=False, weird=_UnprintableRepr())

    (line,) = _audit_lines(path)
    assert line["tool"] == repr(_LONE_SURROGATE), (
        "the last resort should carry the salvaged, repr-coerced name; a "
        "lossy-replaced one means it re-read the raw entry"
    )


@pytest.mark.asyncio
async def test_the_turn_budget_reaches_http_get_as_its_remaining_budget(tmp_path, monkeypatch):
    """SPEC §9.3.3: `http_get`'s deadline is bounded by the turn's REMAINING
    budget, which only `run_tool_call` knows.

    `http_tools`' own suite always supplies `remaining_budget` by hand, so
    nothing there can see the handler stop forwarding it — replacing
    `_CALL_REMAINING_BUDGET.get()` with a constant leaves every one of those
    tests green while the turn budget stops bounding the socket. This is the
    only test that asserts the value crossing the boundary.
    """
    import http_tools

    captured = {}

    def fake_http_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return "{}"

    monkeypatch.setattr(http_tools, "http_get", fake_http_get)
    selected = tools.resolve_declared(
        _grant(tmp_path, "version: 1\ntools:\n  http_get: null\n")
    )
    audit = tools.ToolAuditLog(str(tmp_path / "tool-audit.jsonl"))

    message = await tools.run_tool_call(
        _call("http_get", '{"url": "https://public.example/x"}'),
        selected=selected,
        audit=audit,
        thread_id=1,
        round_index=0,
        timeout=0.25,
    )

    assert message["content"] == "{}", message["content"]
    assert captured["url"] == "https://public.example/x"
    assert captured["remaining_budget"] == 0.25


def test_http_credentials_path_must_be_outside_workspace_even_without_file_tools(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials = workspace / "credentials.yaml"
    credentials.write_text("version: 1\ncredentials: []\n")
    credentials.chmod(0o600)
    monkeypatch.setenv("YUGO_WORKSPACE_PATH", str(workspace))
    monkeypatch.setenv("YUGO_HTTP_CREDENTIALS_FILE", str(credentials))
    with pytest.raises(tools.ToolDeclarationError, match="YUGO_HTTP_CREDENTIALS_FILE"):
        tools.resolve_declared(_grant(tmp_path, "version: 1\ntools:\n  http_get: null\n"))


@pytest.mark.asyncio
async def test_http_result_metadata_reaches_audit_without_secret(tmp_path):
    from types import SimpleNamespace
    import json
    import http_tools

    async def handler(args):
        return http_tools.HttpResult(
            '{"body":"token-id"}',
            {
                "credential_id": "token-id",
                "credential_origins": ["https://api.example:443"],
                "credential_drop": None,
            },
        )

    tool = tools.Tool("http_get", "", {"type": "object"}, handler)
    call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="http_get", arguments='{"url":"https://api.example"}'),
    )
    path = tmp_path / "audit.jsonl"
    reply = await tools.run_tool_call(
        call,
        selected={"http_get": tool},
        audit=tools.ToolAuditLog(str(path)),
        thread_id="bus:deet",
        round_index=0,
        timeout=1,
    )
    line = json.loads(path.read_text())
    assert reply["content"] == '{"body":"token-id"}'
    assert line["credential_id"] == "token-id"
    assert line["credential_origins"] == ["https://api.example:443"]

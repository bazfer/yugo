"""
Source-integrity scan for bot.py and fleet_bus.py.

Class-mechanism guard, not a behavior test — belongs on its own file because it
doesn't exercise runtime, it enforces a coding rule that keeps the runtime
tests' coverage honest.
"""

import ast
import inspect
import re
import textwrap
from pathlib import Path

import bot


def _code_only(obj) -> str:
    """Source of `obj` with docstrings and comments removed.

    These guards ban a CALL, not a word. Scanning raw text makes them trip on
    the prose that explains the very hazard they exist for — which is what
    happened when v0.3b documented "never `endswith('.request')`" in the
    docstring above the equality test. `ast.unparse` drops comments, and the
    walk below drops docstrings, so what is left is only what executes.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree))



def test_no_direct_int_parse_of_env_in_bot_module():
    """Class-mechanism guard: env-var int parses must go through `_env_int`,
    which handles blank strings (that crash raw `int()`). A future direct
    `int(os.environ.get(...))` would slip past the smoke gate's blank-value
    coverage. This test watches for the hazard entering the codebase.
    """
    src = Path(bot.__file__).read_text()
    assert "int(os.environ" not in src, (
        "Parse env-vars through _env_int(name, default) — it handles blank "
        "values from .env files. A direct int() on a blank env var crashes "
        "at import and the CI smoke step under --env-file .env.example "
        "would only catch it if the specific var was tested. Prefer the "
        "helper for class-wide coverage."
    )


# ---------- fleet-bus (v0.3a) class-mechanism guards ----------


def test_no_nats_callback_resubscribes():
    """nats-py replays every subscription itself on reconnect (it rewrites the
    SUB commands from its own `_subs` map before releasing the connection).

    A `subscribe` call inside `reconnected_cb` — the intuitive thing to write —
    therefore DOUBLES delivery of every inbound envelope for the rest of the
    process's life, and the only symptom is the bot doing everything twice.
    `test_fleet_bus_lifecycle.test_subscribe_called_once_per_subject_across_a_restart`
    catches it behaviourally; this catches it at the source level, including
    in callbacks a future slice adds that no runtime test happens to exercise.
    """
    import fleet_bus

    callbacks = [
        name
        for name in dir(fleet_bus.FleetBus)
        if name.startswith("_on_") and name != "_on_message"
    ]
    assert "_on_reconnected" in callbacks, "callback set changed; update this guard"
    for name in callbacks:
        source = _code_only(getattr(fleet_bus.FleetBus, name))
        assert "subscribe(" not in source, (
            f"FleetBus.{name} calls subscribe — nats-py already replays "
            "subscriptions on reconnect, so this double-delivers"
        )


def test_fleet_bus_does_not_import_nats_at_module_scope():
    """`FLEET_BUS_ENABLED=0` must run zero NATS code, and the only version of
    that claim a test can pin hard is "the library was never imported"
    (see test_fleet_bus_wiring). A module-scope `import nats` would silently
    void that guarantee while every behavioural test stayed green.
    """
    import fleet_bus

    tree = ast.parse(Path(fleet_bus.__file__).read_text())
    for node in tree.body:  # module scope only — nested imports are the point
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any(name.split(".")[0] == "nats" for name in names), (
            f"module-scope nats import at line {node.lineno}; import it inside "
            "_lazy_nats() so a bus-disabled deployment never loads it"
        )


def test_adapter_never_subscribes_the_post_jetstream_inbox_subject():
    """SPEC §15 erratum E-1. `fleet.<self>.inbox` arrives with FB-1/FB-3 and
    does not exist on today's wire; per-user authz does not grant subscribe on
    it, and nats-py answers a permissions violation by calling `error_cb` and
    RETURNING — leaving a bot connected, heartbeating and deaf.
    """
    import fleet_bus

    config = fleet_bus.FleetBusConfig(
        bot_name="yugo",
        url="nats://127.0.0.1:1",
        user="yugo",
        password="x",
        allowed_from=frozenset({"yugo"}),
        plugin_version="0.3a",
        audit_log_path=None,
    )
    subjects = fleet_bus.FleetBus(config, fleet_bus.AuditLog(None)).subjects
    assert subjects == (
        "fleet.yugo.request",
        "fleet.yugo.result",
        "fleet.yugo.status",
        "fleet.broadcast.>",
    )


# ---------- fleet-bus (v0.3b) class-mechanism guards ----------


def test_the_bus_turn_has_no_discord_surface_in_source():
    """SPEC §8, at the source level: a bus-triggered turn replies bus-only.

    `test_fleet_bus_session.test_a_bus_turn_never_speaks_into_discord` catches
    the send behaviourally, but only through the one seam it fakes
    (`bot.get_channel`). This catches the whole class — `bot.get_partial_
    messageable`, a captured channel object, a webhook — by refusing the
    vocabulary outright in the function that owns the turn.
    """
    body = _code_only(bot._ask_bus)
    for forbidden in ("channel", "send(", "CHANNEL_ID", "discord"):
        assert forbidden not in body, (
            f"bot._ask_bus references {forbidden!r} — a bus-triggered turn "
            "replies bus-only (SPEC §8) and must not reach Discord at all"
        )


def test_the_injection_gate_is_an_equality_test_not_a_suffix_test():
    """`fleet.broadcast.>` is a subscribed wildcard, so `fleet.broadcast.
    request` is a subject any credentialed bot can publish. A
    `subject.endswith('.request')` gate would inject it and hand out the
    ambient broadcast → prompt surface SPEC §7.1 exists to close.

    `test_fleet_bus_session.test_broadcast_request_subject_cannot_smuggle_an_
    injection` catches it on the real wire; this catches the hazard entering
    the source, including in a future slice's own routing code.
    """
    import fleet_bus

    # Scanned over the WHOLE adapter, not one method: the gate has already
    # moved once (out of the injection helper 3b started with, into the
    # `_on_message` router), and a guard pinned to a method name stops
    # covering the hazard the moment it moves again.
    source = _code_only(fleet_bus.FleetBus)
    assert "== self.request_subject" in source, (
        "the request-subject gate is no longer an equality test; update this "
        "guard only if the replacement is at least as strict"
    )
    assert "endswith" not in source, (
        "match the request subject with `==`, not a suffix test — "
        "`fleet.broadcast.request` passes a suffix test"
    )


def test_audit_log_creates_the_file_with_0600_not_chmod_after():
    """Class-mechanism guard for a window a mode assertion cannot see.

    `test_fleet_bus_envelope.test_audit_file_is_0600_...` proves the file ENDS
    UP 0600. It cannot distinguish that from `open(path, 'a')` (0644 under the
    usual umask) followed by a chmod — which leaves a real, if short,
    world-readable window over a log holding envelope ids and peer identities.
    Only the source can say which one happened.
    """
    import fleet_bus

    source = inspect.getsource(fleet_bus.AuditLog.record)
    assert "os.open(" in source and "0o600" in source, (
        "create the audit log with mode 0o600 via os.open; a plain open() + "
        "chmod leaves a world-readable window"
    )
    assert re.search(r"(?<!os\.)\bopen\(", source) is None, (
        "builtin open() creates with 0666 & ~umask — use os.open with an "
        "explicit mode"
    )


def test_the_audit_never_grows_a_direction_outside_the_shared_schema():
    """The audit file is read by the tap and by whoever is debugging a sick
    bot, and `dir` is the field they filter on. The shared fleet-bus schema
    has `in` / `out` / `drop`; this adapter adds `conn` for its own connection
    lifecycle, which has no wire event to hang on.

    A fifth value is a silent observability fork — every existing filter keeps
    working and simply stops seeing the new lane. v0.3b briefly had one
    (`dir="bus"` for session turns) before the success record was folded onto
    the conforming `in` line.
    """
    import fleet_bus

    source = Path(fleet_bus.__file__).read_text()
    directions = set(re.findall(r'\.record\(\s*"([a-z_]+)"', source))
    assert directions == {"in", "out", "drop", "conn"}, (
        f"audit directions are {sorted(directions)}; the shared schema is "
        "in/out/drop plus this adapter's conn. A session turn is an `in` line "
        "carrying its req_id, not a direction of its own."
    )


# ---------- fleet-bus (v0.3d) class-mechanism guards ----------


def test_every_adapter_local_reject_code_is_harness_prefixed():
    """fleet-bus SPEC §8: a drop reason is "a §5 code or an
    implementation-specific code prefixed with the harness name". §5's twelve
    codes are the shared taxonomy the tap and the coordinator key dashboards
    off, so an unprefixed adapter-local code both squats on a name §5 may
    later want and reports a yugo-only condition as if the fleet defined it.

    Pinned at the source rather than per-code, because the hazard is the NEXT
    code somebody adds. v0.3d added three at once.
    """
    import fleet_bus

    local = {
        name: value
        for name, value in vars(fleet_bus).items()
        if name.startswith("REJECT_") and isinstance(value, str)
    }
    assert local, "the REJECT_* constant set vanished; update this guard"
    for name, code in local.items():
        assert code.startswith("yugo_"), (
            f"{name} = {code!r} is adapter-local but carries no harness "
            "prefix — fleet-bus SPEC §8 requires one, and §5 owns the "
            "unprefixed namespace"
        )


def test_outbound_baton_fields_are_never_read_off_a_tag_attribute():
    """Baton fields on anything this bot publishes come from the RECEIVED
    envelope, via `next_baton_fields`, and from nowhere else.

    `test_fleet_bus_baton.test_the_model_cannot_write_its_own_baton_fields`
    catches it behaviourally through the one attribute set that test writes.
    This catches the class: a model that could set `hops` in a tag could
    reset the ceiling on every pass, and the model's input is another bot's
    unauthenticated payload (SPEC §8), so the path is reachable from the wire.
    The sibling adapter design does exactly this (`bazfer/fleet-bus` 820e4d8,
    `docs/CODEX-ADAPTER-DESIGN.md` §6), which is what makes it a plausible
    edit for someone porting between the two.
    """
    import fleet_bus

    body = _code_only(fleet_bus.FleetBus._publish_turn_output)
    reads = re.findall(r"tag\.attrs\.get\(\s*([^)]*?)\s*\)", body)
    assert reads, "no tag.attrs read found; update this guard"
    assert set(reads) == {"'to'"}, (
        f"the tag dispatcher reads {sorted(set(reads))} off tag attributes; "
        "`to` is the whole grammar, and a baton field taken from here is "
        "model-authored"
    )
    for field in fleet_bus.BATON_FIELDS:
        assert f"'{field}'" not in body, (
            f"{field!r} is named in the tag dispatcher — baton fields come "
            "from next_baton_fields(envelope), not from the reply text"
        )



# ---------- tools (v0.4a) class-mechanism guards ----------


def test_tool_audit_log_creates_the_file_with_0600_not_chmod_after():
    """Same window the fleet-bus guard above watches, on the other audit file.

    `test_tools.test_the_audit_file_is_created_0600` proves the file ENDS UP
    0600 and cannot tell that apart from `open(path, 'a')` (0644 under the
    usual umask) followed by a chmod. From v0.4b this file holds the paths the
    agent asked for and from v0.4c the URLs, so the window is real.
    """
    import tools

    source = inspect.getsource(tools.ToolAuditLog.record)
    assert "os.open(" in source and "0o600" in source, (
        "create the tool audit log with mode 0o600 via os.open; a plain "
        "open() + chmod leaves a world-readable window"
    )
    assert re.search(r"(?<!os\.)\bopen\(", source) is None, (
        "builtin open() creates with 0666 & ~umask — use os.open with an "
        "explicit mode"
    )


def test_the_tool_audit_is_a_separate_file_from_the_bus_audit():
    """SPEC §5 gives them separate config vars and §9 gives the tool one a
    placement rule the bus one does not have (outside `write_file`'s scope).
    Collapsing them onto one path would put a file the agent must never reach
    wherever the bus audit happens to be mounted.
    """
    import fleet_bus
    import tools

    assert tools.DEFAULT_TOOL_AUDIT_PATH != fleet_bus.DEFAULT_AUDIT_LOG


def test_the_tool_loop_does_not_write_to_the_history_store():
    """v0.4a's contract: tool rounds accumulate in `ask_llm`'s per-turn
    working copy and the CALLERS record the user text plus the final reply.

    `history.record_turn` evicts from index 0 in pairs with no notion of a
    tool-call group, so a round persisted from inside the loop can lose its
    `assistant`+`tool_calls` message while the matching `role: tool` message
    survives — an orphan tool result, which is a provider 400 on the next
    turn. `test_bot_tool_loop.test_tool_rounds_never_enter_the_history_store`
    catches the behaviour; this catches the hazard entering the source at all.

    Update this guard only alongside group-aware eviction in `record_turn`,
    never to accommodate a new write.
    """
    body = _code_only(bot.ask_llm)
    assert "record_turn" not in body, (
        "bot.ask_llm writes to the history store — tool rounds must stay in "
        "the per-turn working copy until record_turn evicts in tool-call "
        "groups"
    )


def test_dockerfile_copies_every_module_bot_imports():
    """PR #4's B1, made class-wide. `docker build` does not execute bot.py, so
    a repo-local module that bot.py imports and the Dockerfile does not COPY
    builds green and then crashes the container on boot with
    ModuleNotFoundError. CI's smoke step catches it only for the modules named
    in its import line; this catches the next one automatically.
    """
    root = Path(bot.__file__).resolve().parent
    dockerfile = (root / "Dockerfile").read_text()
    tree = ast.parse(Path(bot.__file__).read_text())
    local = [
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
        if (root / f"{alias.name}.py").is_file()
    ]
    assert local, "no repo-local imports found in bot.py; update this guard"
    for name in local:
        assert f"COPY yugo/{name}.py" in dockerfile, (
            f"bot.py imports the repo-local module {name!r} but the "
            f"Dockerfile has no `COPY {name}.py` — the image builds and the "
            "container dies at boot"
        )


# ---------- tools (v0.4a review round) class-mechanism guards ----------


def test_the_tool_handler_is_never_awaited_unbounded():
    """SPEC §10's turn bound has to reach the handler, not stop at the
    provider call. An `await tool.handler(args)` holds the turn — and on the
    bus lane the NATS subscription callback driving it — open for as long as
    the handler wants, which is the `http_get`-against-a-dead-host case
    arriving in v0.4c.

    `test_tools.test_a_handler_that_outruns_its_budget_is_cut_off_and_audited`
    catches it behaviourally through the one handler that test writes. This
    catches the hazard entering the source, including in whatever dispatch a
    later slice adds.
    """
    import tools

    body = _code_only(tools.run_tool_call)
    assert "wait_for(tool.handler(" in body, (
        "the tool handler must be awaited through asyncio.wait_for with the "
        "turn's remaining budget"
    )
    assert "await tool.handler" not in body, (
        "bare `await tool.handler(...)` — a hanging tool then outlives the "
        "whole turn"
    )


def test_the_tool_call_audit_is_written_from_a_finally_block():
    """`asyncio.CancelledError` derives from `BaseException`, so an
    `except Exception` chain does not see it. With the audit written after
    that chain, a bot shut down (or a bus supervisor cancelled by `stop_bus`)
    mid-tool-call leaves the call unrecorded — and an unaudited tool call is
    worse than a slow one.

    Asserted structurally rather than by substring, because "the record call
    is still in the source" is exactly what would stay true if someone moved
    it back out of the `finally`.
    """
    import tools

    tree = ast.parse(textwrap.dedent(inspect.getsource(tools.run_tool_call)))
    audited_in_finally = any(
        isinstance(node, ast.Try)
        and any(
            isinstance(inner, ast.Attribute) and inner.attr == "record"
            for handler in node.finalbody
            for inner in ast.walk(handler)
        )
        for node in ast.walk(tree)
    )
    assert audited_in_finally, (
        "audit.record must run from a `finally` in run_tool_call so a "
        "cancelled tool call is still recorded"
    )


def test_every_json_loads_in_tools_refuses_non_finite_constants():
    """Python's decoder accepts `NaN` / `Infinity` / `-Infinity` as bare
    literals at any depth; `_canonical_json` runs with `allow_nan=False` and
    refuses them. A decoder that accepts what the encoder will reject is the
    bug `fleet_bus._json_loads` was given the same hook to close (PR #6) —
    here it cost the audit line its detail on exactly the pathological call.

    Pinned at the source because the hazard is the NEXT `json.loads` somebody
    adds, not the one already fixed.
    """
    import tools

    source = Path(tools.__file__).read_text()
    loads = re.findall(r"json\.loads\((?:[^()]|\([^()]*\))*\)", source)
    assert loads, "no json.loads call found in tools.py; update this guard"
    for call in loads:
        assert "parse_constant" in call, (
            f"{call} decodes without a parse_constant hook — it will accept "
            "NaN/Infinity, which _canonical_json then refuses to write"
        )


def test_no_http_error_construction_reports_credential_attachment():
    """SPEC §9.3.3: on an ERROR there is no result object and therefore no
    credential fields. The model gets the named error and nothing about
    attachment; the audit stays the record of which origins the credential
    reached before the failure.

    Pinned at the source because the runtime version of this test cannot fail:
    asserting the absence of a key nothing constructs passes against every
    implementation, including the wrong one. The hazard is the NEXT person to
    "improve" an error payload by adding the field the success result carries —
    which would report attachment for a request that produced no response at
    all. `audit_fields=audit()` is unaffected: the audit builds its own dict.
    """
    import http_tools

    tree = ast.parse(Path(http_tools.__file__).read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "HttpGetError"
    ]
    assert calls, "no HttpGetError construction found in http_tools.py; update this guard"
    for call in calls:
        rendered = ast.unparse(call)
        for field in ("credential_attached", "credential_id"):
            assert field not in rendered, (
                f"{rendered} carries {field!r} — an error has no result object, "
                "so there is no request whose attachment it could describe. The "
                "audit already records the origins the credential reached."
            )

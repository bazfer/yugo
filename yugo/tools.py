"""
Tool-use support for the native harness — registry, capability grants, audit.

The loop itself lives in `bot.ask_llm`; this module owns the grant boundary,
schemas, handlers, argument enforcement, and audit. Filesystem handlers are
bound only after the startup grant has opened an `openat2`-confined workspace.
`loop_probe` remains as a side-effect-free scaffold; `http_get` is v0.4c.

Declaration comes only from `$YUGO_TOOLS_FILE`. The grant is parsed once with
a safe YAML loader before the bot opens its gateway; ambiguity, permissions
that let another process rewrite it, and configuration this build cannot
enforce all abort startup. Persona prose has no authority over capabilities.

Audit, per SPEC §9 Universal ("Every tool call audited with args + result
summary + duration"): every call — including the ones that fail before a
handler runs — writes one JSONL line to `$YUGO_TOOL_AUDIT_PATH`. §9 puts that
path deliberately OUTSIDE `write_file`'s scope so the agent cannot edit its
own audit trail.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import time
import contextvars
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Awaitable, Callable, Mapping

# `_canonical_json` and `_utc_now_iso` are reached across the module boundary
# on purpose. They are the repo's single definition of "one audit line" —
# `allow_nan=False` so a non-finite float can never emit unparseable JSONL,
# and a fixed 3-fractional-digit `Z` timestamp. Re-declaring them here would
# let the two audit files drift into two timestamp formats for whoever is
# grepping both while debugging a sick bot.
import fleet_bus
import filesystem_tools
import http_tools
import yaml

# SPEC §5 (`YUGO_TOOL_AUDIT_PATH`) + §9. `/var/lib/yugo/` rather than a
# `~/.claude/` path — SEV3 v6 moved it off the Claude convention.
DEFAULT_TOOL_AUDIT_PATH = "/var/lib/yugo/tool-audit.jsonl"
DEFAULT_TOOLS_FILE = "/etc/yugo/tools.yaml"
DEFAULT_WORKSPACE_ROOT = "/var/lib/yugo/workspace"
_TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
_LOG = logging.getLogger(__name__)

# How much of a tool's return value goes into the audit line. §9 asks for a
# result SUMMARY, and an unbounded one turns a 10MB `read_file` (v0.4b) into a
# 10MB audit line.
AUDIT_RESULT_MAX_CHARS = 500

# And how much of the ARGUMENTS. This bound was missing in the first cut, on
# the reasoning that §9 asks for the args themselves and the argument an
# auditor most wants is the long one. That reasoning was wrong in one
# direction: arguments are MODEL-AUTHORED, they are audited whether or not the
# handler accepts them, and a rejected 10MB argument therefore still bought an
# unbounded write to the audit trail. Generous enough that any realistic path
# or URL survives whole; `args_len` keeps the truncation honest.
AUDIT_ARGS_MAX_CHARS = 4000

# `loop_probe`'s own bound. The probe echoes model-authored text, so without a
# cap a single call could write an arbitrarily large audit line through the
# un-truncated `args` field above.
PROBE_NOTE_MAX_CHARS = 200
PROBE_RESULT_PREFIX = "loop_probe ok: "


class ToolDeclarationError(Exception):
    """The grant file cannot be trusted as an exact capability declaration.

    Fatal at startup — see the module docstring. Carries the offending name so
    the operator does not have to guess which grant entry was rejected.
    """


@dataclass(frozen=True)
class ToolConfig:
    """One operator-set bound; a wrong ceiling is a startup security fault."""

    default: int
    ceiling: int
    minimum: int = 1


@dataclass(frozen=True)
class Tool:
    """One callable tool: its OpenAI-shaped schema plus the handler.

    `handler` takes the DECODED argument dict and returns the string that
    becomes the `role: tool` message content. It may raise — `run_tool_call`
    turns an exception into a tool-visible error result rather than killing
    the turn, so the model can react to "that file does not exist" the way it
    reacts to any other tool output.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[str]]
    config: Mapping[str, ToolConfig] = field(default_factory=dict)

    def schema(self) -> dict[str, Any]:
        """The `tools=[...]` entry LiteLLM forwards to the provider."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


async def _filesystem_unbound(args: dict[str, Any]) -> str:
    """A registry entry reaching this handler skipped startup confinement."""
    raise RuntimeError("filesystem tool was not bound to a workspace")


async def _read_file(
    args: dict[str, Any], *, workspace: filesystem_tools.Workspace, max_bytes: int
) -> str:
    return await asyncio.to_thread(workspace.read_file, args.get("path"), max_bytes)


async def _write_file(
    args: dict[str, Any], *, workspace: filesystem_tools.Workspace, max_bytes: int
) -> str:
    return await asyncio.to_thread(
        workspace.write_file, args.get("path"), args.get("content"), max_bytes
    )


async def _list_dir(
    args: dict[str, Any], *, workspace: filesystem_tools.Workspace, max_entries: int
) -> str:
    return await asyncio.to_thread(workspace.list_dir, args.get("path"), max_entries)


async def _loop_probe(args: dict[str, Any]) -> str:
    """Echo `note` back. No side effects of any kind — see module docstring.

    Rejects a non-string or over-long `note` rather than coercing, because the
    only thing this tool is for is proving the loop, and a probe that quietly
    accepts anything cannot prove that argument decoding worked.
    """
    note = args.get("note", "")
    if not isinstance(note, str):
        raise ValueError(f"note must be a string; got {type(note).__name__}")
    if len(note) > PROBE_NOTE_MAX_CHARS:
        raise ValueError(
            f"note must be at most {PROBE_NOTE_MAX_CHARS} characters; "
            f"got {len(note)}"
        )
    return f"{PROBE_RESULT_PREFIX}{note}"


_CALL_REMAINING_BUDGET: contextvars.ContextVar[float] = contextvars.ContextVar(
    "tool_call_remaining_budget"
)


async def _http_get(
    args: dict[str, Any],
    *,
    credentials: Mapping[http_tools.Origin, http_tools.Credential],
    max_bytes: int,
    timeout_ms: int,
    max_redirects: int,
) -> str:
    """Keep blocking DNS and sockets off-loop while their own deadline bites."""
    remaining = _CALL_REMAINING_BUDGET.get()
    return await asyncio.to_thread(
        http_tools.http_get,
        args.get("url"),
        max_bytes=max_bytes,
        timeout_ms=timeout_ms,
        max_redirects=max_redirects,
        remaining_budget=remaining,
        credentials=credentials,
    )


REGISTRY: Mapping[str, Tool] = {
    "loop_probe": Tool(
        name="loop_probe",
        description=(
            "Scaffold probe. Echoes `note` back verbatim and does nothing "
            "else — no filesystem, no network, no state. Present only to "
            "exercise the tool-call loop."
        ),
        parameters={
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": (
                        "Any string of at most "
                        f"{PROBE_NOTE_MAX_CHARS} characters; echoed back."
                    ),
                }
            },
            "required": [],
            "additionalProperties": False,
        },
        handler=_loop_probe,
    ),
    "read_file": Tool(
        name="read_file",
        description="Read one UTF-8 file from this bot's confined workspace.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_filesystem_unbound,
        config={"max_bytes": ToolConfig(1024 * 1024, 16 * 1024 * 1024)},
    ),
    "write_file": Tool(
        name="write_file",
        description="Overwrite one UTF-8 file in this bot's confined workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        handler=_filesystem_unbound,
        config={"max_bytes": ToolConfig(256 * 1024, 4 * 1024 * 1024)},
    ),
    "list_dir": Tool(
        name="list_dir",
        description="List one directory in this bot's confined workspace.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=_filesystem_unbound,
        config={"max_entries": ToolConfig(1000, 10000)},
    ),
    "http_get": Tool(
        name="http_get",
        description="Fetch one public HTTP(S) URL as bounded UTF-8 text.",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        handler=_filesystem_unbound,
        config={
            "max_bytes": ToolConfig(256 * 1024, 4 * 1024 * 1024),
            "timeout_ms": ToolConfig(30000, 120000),
            "max_redirects": ToolConfig(5, 10, minimum=0),
        },
    ),
}


# --- capability grant file ---


def _reject_aliases_and_merges(
    node: yaml.Node | None,
    seen: set[int] | None = None,
    key_path: str = "root",
) -> None:
    """Aliases and merges make the authored grant differ from its mapping."""
    if node is None:
        return
    seen = set() if seen is None else seen
    if id(node) in seen:
        raise ToolDeclarationError(f"alias at key {key_path!r} is forbidden")
    seen.add(id(node))
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            name = getattr(key_node, "value", "mapping")
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise ToolDeclarationError(f"merge key {name!r} is forbidden")
            child_path = f"{key_path}.{name}"
            _reject_aliases_and_merges(key_node, seen, child_path)
            _reject_aliases_and_merges(value_node, seen, child_path)
    elif isinstance(node, yaml.SequenceNode):
        for index, child in enumerate(node.value):
            _reject_aliases_and_merges(child, seen, f"{key_path}[{index}]")


class _GrantSafeLoader(yaml.SafeLoader):
    """Construct mappings without silently overwriting a capability key."""


def _construct_unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise ToolDeclarationError("merge key '<<' is forbidden")
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in result
        except TypeError as error:
            raise ToolDeclarationError(f"unhashable mapping key {key!r}") from error
        if duplicate:
            raise ToolDeclarationError(f"duplicate key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_GrantSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _parse_grant(text: str, path: Path) -> Any:
    try:
        # Aliases/merges are rejected from the safe-composed node graph, then
        # semantic duplicates are rejected by a SafeLoader mapping constructor
        # before either authored value can be discarded.
        _reject_aliases_and_merges(yaml.compose(text, Loader=yaml.SafeLoader))
        return yaml.load(text, Loader=_GrantSafeLoader)
    except ToolDeclarationError:
        raise
    except yaml.YAMLError as error:
        raise ToolDeclarationError(f"malformed YAML in {path}: {error}") from error


def _validate_protected_paths(
    workspace_path: str | os.PathLike[str],
    protected: Mapping[str, str | os.PathLike[str]],
) -> None:
    """A writable workspace containing authority or audit defeats both."""
    workspace = Path(workspace_path).expanduser().resolve(strict=False)
    for label, candidate in protected.items():
        resolved = Path(candidate).expanduser().resolve(strict=False)
        if resolved == workspace or resolved.is_relative_to(workspace):
            raise ToolDeclarationError(
                f"protected {label} path {resolved} must be outside workspace path {workspace}"
            )


def resolve_declared(
    tools_file: str | os.PathLike[str] | None = None,
    registry: Mapping[str, Tool] = REGISTRY,
) -> dict[str, Tool]:
    """Load the startup grant; ambiguity aborts before a capability is live."""
    configured = tools_file
    if configured is None:
        configured = os.environ.get("YUGO_TOOLS_FILE", "").strip() or DEFAULT_TOOLS_FILE
    path = Path(configured).expanduser().resolve(strict=False)
    found = path.exists()
    selected: dict[str, Tool] = {}
    configured_values: dict[str, dict[str, int]] = {}
    try:
        if not found:
            return selected
        mode = path.stat().st_mode
        if mode & 0o022:
            raise ToolDeclarationError(
                f"grant file {path} is group/world-writable (mode {mode & 0o777:04o})"
            )
        try:
            text = path.read_text()
        except OSError as error:
            raise ToolDeclarationError(f"cannot read grant file {path}: {error}") from error
        if not text.strip():
            return selected
        document = _parse_grant(text, path)
        if not isinstance(document, dict):
            raise ToolDeclarationError("root key must be a mapping")
        unknown_top = [key for key in document if key not in {"version", "tools"}]
        if unknown_top:
            raise ToolDeclarationError(f"unknown top-level key {unknown_top[0]!r}")
        if type(document.get("version")) is not int or document["version"] != 1:
            raise ToolDeclarationError(
                f"version key must be exactly 1; got {document.get('version')!r}"
            )
        grants = document.get("tools")
        if grants is None:
            return selected
        if not isinstance(grants, dict):
            raise ToolDeclarationError("tools key must be a mapping or null")
        for name, config in grants.items():
            if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
                raise ToolDeclarationError(f"invalid tool name key {name!r}")
            if name not in registry:
                raise ToolDeclarationError(f"unknown tool key {name!r}; known: {sorted(registry)}")
            if config is not None and not isinstance(config, dict):
                raise ToolDeclarationError(
                    f"tool key {name!r} config must be a mapping or null"
                )
            values = {}
            for key, value in (config or {}).items():
                if key not in registry[name].config:
                    raise ToolDeclarationError(
                        f"unknown config key {key!r} for tool {name!r}"
                    )
                bound = registry[name].config[key]
                if type(value) is not int:
                    raise ToolDeclarationError(
                        f"config key {key!r} for tool {name!r} must be an integer, got {value!r}"
                    )
                if value < bound.minimum:
                    raise ToolDeclarationError(
                        f"config key {key!r} for tool {name!r} must be at least {bound.minimum}, got {value}"
                    )
                if value > bound.ceiling:
                    raise ToolDeclarationError(
                        f"config key {key!r} for tool {name!r} ceiling {bound.ceiling} exceeded by {value}"
                    )
                values[key] = value
            selected[name] = registry[name]
            configured_values[name] = values
        filesystem_names = {"read_file", "write_file", "list_dir"}.intersection(selected)
        credentials_path = Path(
            os.environ.get("YUGO_HTTP_CREDENTIALS_FILE", "").strip()
            or http_tools.DEFAULT_CREDENTIALS_FILE
        ).expanduser().resolve(strict=False)
        if "http_get" in selected:
            bot_name = os.environ.get("BOT_NAME", "").strip() or "yugo"
            configured_workspace = (
                os.environ.get("YUGO_WORKSPACE_PATH", "").strip()
                or f"{DEFAULT_WORKSPACE_ROOT}/{bot_name}"
            )
            _validate_protected_paths(
                configured_workspace,
                {"YUGO_HTTP_CREDENTIALS_FILE": credentials_path},
            )
        if filesystem_names:
            bot_name = os.environ.get("BOT_NAME", "").strip() or "yugo"
            workspace_path = (
                os.environ.get("YUGO_WORKSPACE_PATH", "").strip()
                or f"{DEFAULT_WORKSPACE_ROOT}/{bot_name}"
            )
            _validate_protected_paths(
                workspace_path,
                {
                    "YUGO_TOOL_AUDIT_PATH": audit_path_from_env(),
                    "YUGO_TOOLS_FILE": path,
                    "YUGO_HTTP_CREDENTIALS_FILE": credentials_path,
                },
            )
            try:
                workspace = filesystem_tools.Workspace(workspace_path)
            except filesystem_tools.FilesystemStartupError as error:
                raise ToolDeclarationError(
                    f"filesystem tool startup failed for workspace path {workspace_path!r}: {error}"
                ) from error
            for name in filesystem_names:
                tool = selected[name]
                values = {
                    key: configured_values[name].get(key, bound.default)
                    for key, bound in tool.config.items()
                }
                function = {
                    "read_file": _read_file,
                    "write_file": _write_file,
                    "list_dir": _list_dir,
                }[name]
                selected[name] = replace(
                    tool, handler=partial(function, workspace=workspace, **values)
                )
        if "http_get" in selected:
            try:
                credentials = http_tools.load_credentials(credentials_path)
            except http_tools.HttpStartupError as error:
                raise ToolDeclarationError(f"http_get credentials key: {error}") from error
            tool = selected["http_get"]
            values = {
                key: configured_values["http_get"].get(key, bound.default)
                for key, bound in tool.config.items()
            }
            selected["http_get"] = replace(
                tool, handler=partial(_http_get, credentials=credentials, **values)
            )
        return selected
    finally:
        _LOG.warning(
            "tool grants path=%s found=%s granted=%d", path, found, len(selected)
        )


def schemas(selected: Mapping[str, Tool]) -> list[dict[str, Any]]:
    """`tools=[...]` payload for `litellm.acompletion`.

    Empty when nothing is declared. The caller must then omit the parameter
    entirely rather than sending `tools=[]` — see `bot.ask_llm`.
    """
    return [tool.schema() for tool in selected.values()]


# --- audit ---


def audit_path_from_env(env: Mapping[str, str] | None = None) -> str:
    """`$YUGO_TOOL_AUDIT_PATH`, defaulting per SPEC §5.

    Blank collapses to the default the same way every other optional key in
    `.env.example` does — a blank line in that file must not disable the
    audit trail.
    """
    source = os.environ if env is None else env
    return source.get("YUGO_TOOL_AUDIT_PATH", "").strip() or DEFAULT_TOOL_AUDIT_PATH


def _encode_audit_line(entry: Mapping[str, Any]) -> bytes:
    """One audit line, all the way to the BYTES that get written.

    The encode is part of the check, not a step after it. `_canonical_json`
    runs `ensure_ascii=False`, so a lone surrogate — which `json.loads`
    produces from an ordinary `"\\ud800"` escape, no malformed input needed —
    encodes to a `str` happily and then fails at `.encode("utf-8")`. That
    raises `UnicodeEncodeError`, which is a `ValueError` and NOT an
    `OSError`, so it used to sail through the `except OSError` around the
    write, out of `record`, out of `run_tool_call`'s `finally` and out of a
    function documented never to raise for a tool fault — leaving the audit
    file CREATED AND EMPTY, which reads to a consumer as "no tool calls
    happened".

    Strict, with no lossy spelling. A `errors="replace"` fallback was written
    first and then removed: every field of `record`'s last-resort entry is
    ASCII by construction — a generated timestamp, two literals, a
    repr-coerced tool name and `repr()` of an exception, whose own repr
    escapes the offending character — so no input reached it. An
    unreachable branch documents a guarantee nothing verifies.
    """
    text = fleet_bus._canonical_json(entry)
    return f"{text}\n".encode("utf-8")


def _encodable(value: Any) -> bool:
    """Can this value reach the audit file? Used to salvage a line per field.

    Deliberately the FULL trip — encoder plus UTF-8 — because the salvage
    path exists to pick out the field that cannot be written, and a check
    that stopped at `str` would hand the offending value straight back.
    """
    try:
        fleet_bus._canonical_json(value).encode("utf-8")
    except (TypeError, ValueError):
        return False
    return True


class ToolAuditLog:
    """Append-only JSONL sink for tool calls (SPEC §9 Universal).

    A SEPARATE file from the fleet-bus audit, with a separate schema and a
    separate config var (SPEC §5). Tool events are not bus traffic and must
    not be smuggled into the `dir` lanes the tap and the coordinator filter
    on — `test_source_scan.test_the_audit_never_grows_a_direction_outside_the
    _shared_schema` is the guard on that side of the seam.

    Every line carries `event`, `tool`, `ok` and `duration_ms`, so a consumer
    can count and time calls without parsing the variable fields.

    Mode is 0600 at CREATE time rather than chmod-after: the file holds tool
    arguments, which from v0.4b onward means paths and from v0.4c URLs, and a
    0644 window between create and chmod is a window.
    """

    def __init__(self, path: str | None, logger=print) -> None:
        self.path = path
        self._logger = logger

    def record(self, tool: str, **fields: Any) -> None:
        entry = {
            "ts": fleet_bus._utc_now_iso(),
            "event": "tool_call",
            "tool": tool,
            **fields,
        }
        try:
            blob = _encode_audit_line(entry)
        except (TypeError, ValueError) as e:
            # One unencodable line would otherwise poison every consumer that
            # reads this JSONL from then on.
            #
            # Degrading to a bare {ts, event, tool} stub was the first cut and
            # it was backwards: it threw away ok, duration_ms, round, thread
            # and the result summary on exactly the calls whose detail matters
            # most — the pathological ones. Coerce PER FIELD instead, so only
            # the offending value becomes a repr and every other field on the
            # line survives intact.
            salvaged = {
                key: value if _encodable(value) else repr(value)
                for key, value in entry.items()
            }
            salvaged["error"] = "audit_encode_failed"
            salvaged["detail"] = repr(e)
            try:
                blob = _encode_audit_line(salvaged)
            except (TypeError, ValueError):
                # Belt and braces: a value whose own repr cannot be encoded.
                # Never observed, but a poisoned JSONL stream is not a failure
                # mode worth leaving to argument. `tool` is model-supplied and
                # so is taken from the SALVAGED entry, not from the raw one —
                # the raw name is exactly the sort of value that got us here.
                blob = _encode_audit_line(
                    {
                        "ts": entry["ts"],
                        "event": "tool_call",
                        "tool": salvaged.get("tool"),
                        "error": "audit_encode_failed",
                        "detail": repr(e),
                    }
                )
        if not self.path:
            # Decoding what was just encoded, rather than reusing the `str`:
            # this is the one spelling guaranteed printable on a UTF-8 stream,
            # and a logger that raised would cost the line just as dearly as
            # a writer that did.
            self._logger(f"[tool] {blob.decode('utf-8').rstrip()}")
            return
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, blob)
            finally:
                os.close(fd)
            os.chmod(self.path, 0o600)
        except OSError as e:
            # An unwritable audit path must not take the turn down. It is a
            # full disk or a missing bind mount, not a tool fault — and the
            # line still reaches stdout, where the container log keeps it.
            self._logger(
                f"[tool] audit write failed: {e!r} · "
                f"{blob.decode('utf-8').rstrip()}"
            )


# --- one tool call ---


def _reject_json_constant(name: str) -> Any:
    """`parse_constant` hook: refuse `NaN` / `Infinity` / `-Infinity`.

    Python's decoder accepts all three as bare literals at any depth;
    `JSON.parse` does not, and neither does `_canonical_json`, which runs with
    `allow_nan=False`. Accepting a value the strict encoder will later refuse
    is the bug `fleet_bus._json_loads` was given this same hook to close (PR
    #6) — here it bit the audit trail: the argument object went into the
    record, the encoder refused the line, and the fallback dropped the very
    detail the pathological call most needed recorded.
    """
    raise ValueError(f"non-finite JSON constant in tool arguments: {name}")


def _decode_arguments(raw: Any) -> dict[str, Any]:
    """Model-authored `function.arguments` string → argument dict.

    Providers spell "no arguments" three ways — `None`, `""` and `"{}"` — and
    the first two make `json.loads` raise, so a no-argument tool call would
    fail for no reason the model could act on. Everything else that is not a
    JSON object raises, and `run_tool_call` turns that into a tool-visible
    error the model can retry against.

    Everything this returns is guaranteed encodable by `_canonical_json`, so
    the audit line for a call can always carry its own arguments. That
    guarantee is ENFORCED HERE rather than assumed, and it is enforced on
    both entry paths. `parse_constant` only fires while parsing a string;
    LiteLLM types `function.arguments` as `Any` and providers do hand back
    already-decoded mappings, and that path used to return the object
    untouched. A `NaN` inside one then survived to `_audit_args_fields`,
    whose encode raised inside the `finally` — so the call ran, `audit.record`
    was never reached, NO audit line was written at all, and `run_tool_call`
    raised out of a function documented never to raise for a tool fault,
    stranding an assistant `tool_calls` message with no matching result.

    The check is a real encode all the way to BYTES, not a non-finite scan.
    Two reasons, and the second is the one a scan would miss entirely:

    * A pre-decoded mapping can carry a `set`, a `datetime` or raw `bytes`
      just as easily as a `NaN`, and all of them fail the same encoder.
    * A LONE SURROGATE arrives through the ordinary string path. `json.loads`
      builds one from a plain `"\\ud800"` escape — valid JSON, nothing
      malformed — and `_canonical_json` emits it without complaint because it
      runs `ensure_ascii=False`. It only fails at `.encode("utf-8")`, as a
      `UnicodeEncodeError`: a `ValueError`, not an `OSError`, so it used to
      escape the audit writer's own error handling and raise out of this
      module, leaving the audit file created and EMPTY.

    Refusing here turns every one of them into an ordinary tool error with a
    complete `args_raw` line — `repr` is ASCII-safe, so the record survives
    even when the value cannot.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    decoded = (
        json.loads(raw, parse_constant=_reject_json_constant)
        if isinstance(raw, (str, bytes))
        else raw
    )
    if not isinstance(decoded, dict):
        raise ValueError(
            f"tool arguments must decode to a JSON object; got "
            f"{type(decoded).__name__}"
        )
    try:
        fleet_bus._canonical_json(decoded).encode("utf-8")
    except (TypeError, ValueError) as e:
        raise ValueError(f"tool arguments are not audit-encodable: {e}") from e
    return decoded


def _check_declared_shape(name: str, parameters: dict[str, Any], args: dict) -> None:
    """Enforce the part of a tool's declared schema that bounds its inputs.

    NOT a JSON Schema validator, and deliberately not one — this is the
    scaffold slice and pulling in `jsonschema` is a dependency decision for
    whichever of 4b/4c actually needs typed validation. What it does enforce
    is the two keywords that decide WHAT REACHES a handler and the audit line:

    * `additionalProperties: false` — the probe declares it and nothing
      enforced it, so a model could send a short `note` alongside an
      arbitrarily large undeclared key, watch the handler succeed, and put the
      whole object into the audit trail.
    * `required` — a handler that has to defend itself against missing keys
      re-derives the same check per tool.

    A schema that declares neither is enforced no further than it asks to be.
    """
    properties = parameters.get("properties") or {}
    if parameters.get("additionalProperties") is False:
        unexpected = sorted(key for key in args if key not in properties)
        if unexpected:
            raise ValueError(
                f"tool {name!r} declares no arguments beyond "
                f"{sorted(properties)}; got unexpected {unexpected}"
            )
    missing = [key for key in (parameters.get("required") or ()) if key not in args]
    if missing:
        raise ValueError(f"tool {name!r} requires {missing}")


def _audit_args_fields(function: Any, args: dict[str, Any] | None) -> dict[str, Any]:
    """The argument half of an audit line, in one of three exclusive shapes.

    * `args` — the decoded object, when its encoded form fits the bound.
    * `args_truncated` — the encoded object cut to the bound, when it does
      not, alongside `args_truncated_flag` so no reader has to infer it.
    * `args_raw` — the model's literal `arguments` string, when decoding never
      got far enough to produce an object at all.

    `args_len` (encoded length) is on every shape but the last, so "how big
    was it really" survives the cut.

    THIS FUNCTION MUST NOT RAISE. It is called from `run_tool_call`'s
    `finally` while building the kwargs for `audit.record`, so anything it
    throws happens BEFORE the record call and costs the line entirely —
    `ToolAuditLog.record` is careful to salvage an unencodable line per
    field, and none of that helps if it is never reached. `_decode_arguments`
    now refuses arguments this encoder would reject, so the guard below is
    the second of two locks on one door; it stays because the argument in
    favour of removing it is precisely the argument that lost last round.
    """
    if args is None:
        return {"args_raw": repr(getattr(function, "arguments", None))[:AUDIT_ARGS_MAX_CHARS]}
    try:
        encoded = fleet_bus._canonical_json(args)
    except (TypeError, ValueError) as e:
        return {
            "args_raw": repr(args)[:AUDIT_ARGS_MAX_CHARS],
            "args_unencodable": repr(e),
        }
    if len(encoded) <= AUDIT_ARGS_MAX_CHARS:
        return {"args": args, "args_len": len(encoded)}
    return {
        "args_truncated": encoded[:AUDIT_ARGS_MAX_CHARS],
        "args_truncated_flag": True,
        "args_len": len(encoded),
    }


async def run_tool_call(
    call: Any,
    *,
    selected: Mapping[str, Tool],
    audit: ToolAuditLog,
    thread_id: Any,
    round_index: int,
    timeout: float,
) -> dict[str, Any]:
    """Execute one `tool_calls` entry and build its `role: tool` reply.

    Never raises for a tool-level fault. An unknown name, undecodable
    arguments, a rejected argument shape, a handler exception or a handler
    that outruns `timeout` all come back as a `role: tool` message whose
    content names the error, because the model is mid-turn and can recover —
    and because raising here would strand an `assistant` message that has
    `tool_calls` with no matching result, which is a provider 400 on the next
    request, not a graceful failure.

    `selected` is the FILE-GRANTED set, not `REGISTRY`. A model that names
    a tool its grant file did not declare gets `unknown tool` even when the build
    ships that tool — SPEC §9's "no auto-discovery" is a property of what the
    model can reach, not just of what gets advertised.

    **`timeout` is REQUIRED and is the turn's remaining budget**, recomputed
    by the caller before every call. It is not optional and there is no
    "unbounded" spelling, because the first cut awaited the handler bare: a
    hanging tool held the turn — and, on the bus lane, the NATS subscription
    callback driving it — open indefinitely, which is exactly the `http_get`
    case arriving in v0.4c. Making it a required keyword is the class fix; a
    default would let the next caller reintroduce the same hole.

    Writes exactly one audit line per call, on every path INCLUDING
    cancellation. The audit is in a `finally` rather than after the `except`
    chain because `asyncio.CancelledError` derives from `BaseException`: a bot
    shut down (or a bus supervisor cancelled) mid-tool-call would otherwise
    leave the call unrecorded, and an unaudited tool call is worse than a slow
    one. The write is synchronous, so it completes even while unwinding.
    """
    function = getattr(call, "function", None)
    name = getattr(function, "name", None) or ""
    started = time.monotonic()
    args: dict[str, Any] | None = None
    ok = False
    # Overwritten on every path that reaches an outcome; survives only when
    # the coroutine is cancelled before one.
    result = "[tool error: cancelled before completion]"
    audit_extra: dict[str, Any] = {}
    budget_token = _CALL_REMAINING_BUDGET.set(timeout)
    try:
        args = _decode_arguments(getattr(function, "arguments", None))
        tool = selected.get(name)
        if tool is None:
            raise ValueError(
                f"unknown tool {name!r}; this bot's grant file declares "
                f"{sorted(selected)}"
            )
        _check_declared_shape(name, tool.parameters, args)
        if timeout <= 0:
            # An earlier call in this same round already spent the budget.
            # Said outright rather than left to `wait_for`'s handling of a
            # non-positive timeout.
            raise asyncio.TimeoutError("no turn budget left")
        result = await asyncio.wait_for(tool.handler(args), timeout)
        audit_extra = dict(getattr(result, "audit_fields", {}))
        ok = True
    except asyncio.TimeoutError as e:
        # Must precede the `Exception` clause: since 3.11 asyncio.TimeoutError
        # IS the builtin TimeoutError, which is an OSError and so an Exception.
        detail = str(e) or f"exceeded the turn's remaining {timeout:.3f}s"
        result = f"[tool error: {name} timed out — {detail}]"
    except Exception as e:  # noqa: BLE001 — every tool fault is model-visible
        audit_extra = dict(getattr(e, "audit_fields", {}))
        result = f"[tool error: {e}]"
    finally:
        _CALL_REMAINING_BUDGET.reset(budget_token)
        audit.record(
            name,
            # `str()` normalises the two key families `history` maintains — an
            # int Discord channel id and a `bus:<peer>` string — into one field
            # type, while the `bus:` prefix keeps them distinguishable. It is
            # the provenance an auditor needs: a tool call driven by a bus turn
            # was driven by another bot's unauthenticated payload (SPEC §8).
            thread=str(thread_id),
            round=round_index,
            ok=ok,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            result=result[:AUDIT_RESULT_MAX_CHARS],
            result_len=len(result),
            **audit_extra,
            **_audit_args_fields(function, args),
        )

    return {
        "role": "tool",
        "tool_call_id": getattr(call, "id", None),
        "content": result,
    }


def record_refused_call(
    call: Any,
    *,
    audit: ToolAuditLog,
    thread_id: Any,
    round_index: int,
    reason: str,
) -> None:
    """Audit a tool call the loop refuses BEFORE `run_tool_call` sees it.

    One caller today: the `TOOL_MAX_ROUNDS` ceiling in `bot.ask_llm`, which
    raises on the batch that asked for one round too many. Those calls never
    reached `run_tool_call`, so their names and arguments were the only ones
    in the whole turn that went unrecorded — and they are the last batch a
    runaway model emits, which from v0.4b/4c means the file paths and URLs an
    auditor most wants to see. Every OTHER pre-handler refusal — unknown
    tool, bad argument shape, no budget left — already audits, so the ceiling
    was the one inconsistent lane, not a deliberate exemption.

    Deliberately the same `event: tool_call` + `ok: false` shape as those
    refusals rather than a new event type: a consumer that already counts
    failed calls should count these too, and `refused` names why. There is no
    `role: tool` reply to build because the turn is ending, so this returns
    nothing — `result` carries the refusal text that would have been the
    model's.

    Never raises. It runs on a path that is already raising, and an audit
    helper that could take the real exception down with it would trade the
    turn's error for its own.
    """
    function = getattr(call, "function", None)
    try:
        args = _decode_arguments(getattr(function, "arguments", None))
    except Exception:  # noqa: BLE001 — undecodable args still get audited raw
        args = None
    try:
        audit.record(
            getattr(function, "name", None) or "",
            thread=str(thread_id),
            round=round_index,
            ok=False,
            # The handler never started; 0.0 keeps the field on every line so
            # a consumer timing calls never has to special-case its absence.
            duration_ms=0.0,
            refused=reason,
            result=f"[tool error: refused before execution — {reason}]",
            result_len=len(f"[tool error: refused before execution — {reason}]"),
            **_audit_args_fields(function, args),
        )
    except Exception as e:  # noqa: BLE001
        audit._logger(f"[tool] refusal audit failed: {e!r}")


def assistant_tool_call_message(message: Any) -> dict[str, Any]:
    """The `assistant` message carrying `tool_calls`, rebuilt field by field.

    NOT `message.model_dump()`. That emits the provider-neutral extras LiteLLM
    hangs off its `Message` type (`function_call: null`,
    `provider_specific_fields: null`) which nothing downstream wants and some
    providers reject on the next request.

    `content` is `None`, not `""`, when the model sent tool calls and no text.
    LiteLLM's Anthropic transform rewrites an empty-string assistant content
    into a literal `[System: Empty message content sanitised to satisfy
    protocol]` text block — a sentence the bot never said, permanently in its
    own context for the rest of the turn. `None` produces a clean single
    `tool_use` block. Observed at the pinned litellm version and pinned by
    `test_litellm_transforms.test_assistant_tool_call_content_none_...`.
    """
    text = getattr(message, "content", None)
    return {
        "role": "assistant",
        "content": text if text else None,
        "tool_calls": [
            {
                "id": getattr(call, "id", None),
                "type": "function",
                "function": {
                    "name": getattr(getattr(call, "function", None), "name", None),
                    "arguments": getattr(
                        getattr(call, "function", None), "arguments", None
                    ),
                },
            }
            for call in (getattr(message, "tool_calls", None) or ())
        ],
    }

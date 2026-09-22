"""
fleet-bus adapter — v0.3d: connect + subscribe + heartbeat + audit log +
session injection + outbound publish + baton participation.

Scope (SPEC §15 slices 3a/3b/3c/3d, 3a as corrected by the §15 erratum): 3a
proved the connection lifecycle, with inbound handling decode → validate →
audit → DROP. 3b replaces the DROP on ONE subject — `fleet.<self>.request`, and
only for envelopes actually addressed to us — with a call into the
`on_envelope` hook, which drives an LLM turn that replies BUS-ONLY (§8) and
never touches Discord. The envelope reaches the model as the fleet-wide
`<channel source="fleet-bus" ...>` injection frame. 3c gives that turn a way
out: the answer is published back to the sender automatically, and any `<BUS
to="...">` tag in it publishes to a THIRD party. 3d makes this adapter a
PARTICIPANT in the baton protocol rather than a conduit for it — see the
section below — and the shared in-tree envelope contract is 3e. Nothing here touches
JetStream — no durable consumers or DeliverPolicy. Envelope-id deduplication is
durable in SQLite so at-least-once relay delivery is safe across restarts.

**Baton participation (3d).** §15's one-line summary of this slice is "baton
field pass-through on inbound + outbound". Read literally that means carry the
four fields and touch nothing, and that reading is harmful: `hops` is defined
by `BATON-PROTOCOL-SPEC.md` v0.2 as "incremented on every pass", so a bot that
forwards a baton WITHOUT incrementing makes the count undercount for every
participant downstream of it. The chain then looks shorter than it is and
everyone believes a backstop exists that does not — including yugo's own
coordinator, whose §7A.3 policy tightens on `hops_gte` and whose §7A.3 note
names "a classless bot-originated hop-9 envelope" as the case the axis is
there to catch. A fabricated hop count is worse than no hop count.

So: `hops` INCREMENTS on everything this adapter publishes onward from a
received envelope, the hop-8 warning is PUBLISHED TO `origin` (the baton spec:
"a warning nobody reads is decoration"), and an inbound envelope at or past
hop 16 is REFUSED. `root_id`, `origin` and `owner` are copied unchanged, which
is where "pass-through" is the whole truth. Lifecycle state is NOT tracked
here: the baton spec puts that on the originator ("the originator tracks its
own open batons... Do not make this the bus's job"; "Not a work queue. No
persistence"), so there is no store in this module and the 600s abandonment
timer is deliberately not implemented — it is originator bookkeeping, not
adapter pass-through. The repo owner's ruling on the §15 wording is recorded
with the PR as an erratum; SPEC.md is a frozen versioned artifact and is not
edited here.

The heartbeat IS a publish (`fleet.<self>.status`); "no publish" in the 3a
line means no envelope-publish API for LLM-authored traffic. 3c is where that
API arrives, and it publishes to `fleet.<peer>.request` only — never
`.result`, which SPEC §7 removed as a subject class (see `publish_request`).

Wire semantics are a port of the reference TypeScript adapter
(`artifice-discord/src/fleet-bus.ts` v0.4.0) — same envelope shape, same
reject-code vocabulary, same audit-record shape. Divergences from that port
are called out inline, and they are only allowed to run one way: Python may
REJECT what TS accepts, never accept what TS rejects. Accepting more is how
one adapter ends up processing an envelope its peer already dropped, with no
reject line anywhere to say so. Python's own semantics make that direction
easy to write by accident, in three separate places, so each is guarded
explicitly below: `True == 1`; `fromisoformat` parses — and for a UTC offset
silently NORMALISES — shapes `Date.parse` calls NaN; and `json` both accepts
and emits `NaN`/`Infinity`/`-Infinity`, which `JSON.parse` refuses outright.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple

import yaml

# --- wire constants (must match fleet-bus.ts) ---

ENVELOPE_VERSION = 1
DEFAULT_MAX_ENVELOPE_BYTES = 1_044_480
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0

# nats-py's own default is 2s; at 5s an outage costs ~12 audit lines a minute
# instead of ~30 while still reconnecting fast enough for a fleet heartbeat.
DEFAULT_RECONNECT_TIME_WAIT_S = 5.0

# `drain()` blocks up to this long on shutdown. nats-py defaults to 30s, which
# would stall `bot.close()` (and therefore container SIGTERM → SIGKILL) behind
# a bus that is already gone.
DEFAULT_DRAIN_TIMEOUT_S = 5.0

# Every envelope 3c publishes is free-form bot-to-bot chat, which is exactly
# what the fleet-bus SPEC §3 kind `text_message` names. Its payload shape
# (`{"text": ...}`) is the one the fleet already speaks: the GATE suite's
# probes and the `bus_request` examples in the plugin-integration doc both use
# it, so a peer's renderer already knows how to display what we send.
OUTBOUND_KIND = "text_message"

# --- baton protocol (v0.3d) ---
#
# The four additive fields, in the order they are rendered on the injection
# frame. `BATON-PROTOCOL-SPEC.md` v0.2 and the fleet-bus schema
# (`schema/envelope.v1.schema.json` at `bazfer/fleet-bus` 820e4d8) agree on
# both the names and the value shapes; the constraints are enforced in
# `validate_envelope`.
#
# `root_id`, `origin` and `owner` are COPIED UNCHANGED onto anything we send
# onward. `owner` in particular is NOT set to this bot: ownership changes only
# on an explicit `baton.handoff`, and the baton spec names a forged handoff
# ("a spoofed `baton.handoff` reassigns ownership of live work") as the thing
# `owner` exists to prevent. yugo answering a question is not a handoff, and a
# `<BUS>` tag addressing a third party is not one either — it publishes
# `text_message`, not `baton.handoff`.
BATON_PASSTHROUGH_FIELDS = ("root_id", "origin", "owner")
BATON_FIELDS = BATON_PASSTHROUGH_FIELDS + ("hops",)

# Warn at 8, reject at 16 (Fernando via `BATON-PROTOCOL-SPEC.md` v0.2, and the
# same two numbers in the fleet-bus schema's `hops` description). 16 rather
# than 8 for the ceiling because a contested PR loop is two hops per round
# trip and three rounds of changes-requested is 12 hops of healthy work; 16
# still catches the real target, which is two agents acknowledging each other
# forever and reaches it in under a second.
BATON_HOPS_WARN_AT = 8
BATON_HOPS_REJECT_AT = 16

# Longest fragment of a rejected `<BUS>` tag copied into an audit line. The
# tag body is model-authored text of unbounded length and the audit sink is a
# JSONL file the tap tails; the fragment is there to identify WHICH tag was
# rejected, not to archive it.
AUDIT_RAW_MAX_CHARS = 200

DEFAULT_MANIFEST_PATH = "/vault/infra/fleet-manifest.yaml"
DEFAULT_AUDIT_LOG = "/root/.claude/fleet-bus-log.jsonl"
DEFAULT_URL = "nats://nats:4222"
DEFAULT_DEDUP_TTL_S = 8 * 24 * 60 * 60
MIN_DEDUP_TTL_S = 7 * 24 * 60 * 60
DEFAULT_DEDUP_LEASE_S = 60
DEDUP_PRUNE_EVERY = 256
DEDUP_PRUNE_LIMIT = 100
# A single bounded batch cannot keep up: at 100 deleted per 256 admitted the
# expired backlog grows ~156 rows per 256 arrivals. `prune` therefore loops
# bounded batches until the expired set is drained or this budget is spent,
# and the budget is deliberately larger than DEDUP_PRUNE_EVERY so a steady
# arrival stream loses ground on every sweep rather than gaining it.
DEDUP_PRUNE_BUDGET = 4 * DEDUP_PRUNE_EVERY
# Renewal cadence for a live owner, as a fraction of the lease. Driven by a
# MONOTONIC timer in-process: the stored lease_until_s stays wall-clock
# because it is compared across processes, and a monotonic value is not
# comparable outside the process that read it. What renewal buys is that a
# healthy owner keeps pushing its own deadline forward, so neither a slow turn
# nor a forward clock step can hand its envelope to a second worker.
DEDUP_LEASE_RENEW_RATIO = 0.4

_BOT_NAME_PATTERN = re.compile(r"[a-z0-9_-]+")

# The EXTENDED ISO-8601 shapes `Date.parse` accepts. `datetime.fromisoformat`
# accepts a strict superset of those, and this gate is what cuts it back down.
# Two different reasons a shape has to be excluded HERE rather than left to
# the parse:
#
#   * basic/compact forms (`20260826T120000`, `2026W351`, `20260826`).
#     `fromisoformat` parses them, `Date.parse` answers NaN. Excluded by
#     requiring the dashes and colons.
#   * a UTC offset whose MINUTE field is 60..99 (`+01:60`, `-0199`).
#     `fromisoformat` does not reject those, it NORMALISES them into the next
#     hour — `+01:60` becomes a valid `+02:00` — while `Date.parse` answers
#     NaN. A normalising parse looks exactly like a successful one from the
#     outside, so this is the one range check that cannot be delegated. The
#     offset HOUR is clamped to 00..23 in the same group: `fromisoformat`
#     does reject 24+ (its offset must be strictly under 24h), but stating
#     both halves of the offset range in one place is what stops the next
#     edit from reading `[+-]\d{2}:?\d{2}` as deliberate.
#
# What stays `fromisoformat`'s job: semantic validity this regex cannot
# express — month 13, day 32, minute 60, second 60, hour 24. Every one of
# those it REJECTS rather than normalises (verified field-by-field against
# `Date.parse`), which is why they are safe to delegate and offset minutes
# are not.
_ISO_EXTENDED_TS_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}"  # calendar date, dashes required
    r"[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"  # required time, colons required
    r"(?:Z|[+-](?:[01]\d|2[0-3]):?[0-5]\d)"  # required Z, or an in-range offset
)

RESERVED_BOT_NAMES = {"broadcast"}


class FleetBusConfigError(RuntimeError):
    """Bus is enabled but its configuration is unusable.

    Raised at import (startup) so the operator gets the same fatal treatment
    SPEC §10 gives a missing persona, rather than a bot that boots deaf. The
    message ALWAYS names the offending env var — an anonymous traceback in a
    restart-looping container is what this class exists to prevent.
    """


def _lazy_nats():
    """Import `nats` on first use.

    Deliberately NOT a module-level import: with `FLEET_BUS_ENABLED=0` the
    adapter must execute zero NATS code, and "the library was never even
    imported" is the only version of that claim a test can pin hard
    (`"nats" not in sys.modules`). It also keeps a bus-less deployment
    bootable if nats-py is missing from the image.
    """
    import nats

    return nats


def _utc_now_iso(now: datetime | None = None) -> str:
    """UTC timestamp in JS `Date.prototype.toISOString()` shape.

    Exactly three fractional digits + literal `Z`, because the TypeScript
    adapter and the tap both emit/parse that shape. `datetime.isoformat()`
    would give `+00:00` and either 0 or 6 fractional digits.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"


def _reject_json_constant(name: str) -> Any:
    """`parse_constant` hook: refuse `NaN` / `Infinity` / `-Infinity`."""
    raise ValueError(f"non-finite JSON constant: {name}")


def _json_loads(data: bytes | str) -> Any:
    """`json.loads` cut back to what `JSON.parse` accepts.

    Python's decoder accepts the bare literals `NaN`, `Infinity` and
    `-Infinity` at ANY depth; `JSON.parse` — which is what the TS adapter's
    `JSONCodec` runs — throws on all three. Leaving that on is the forbidden
    divergence: the same bytes are a live, audited-as-inbound envelope here
    and a `malformed_json` drop over there, with no reject line anywhere.

    It also decodes to a float that has NO JSON spelling. At 3a the audit
    record carries only `id`/`kind`/`from` — all string-validated — so such a
    value cannot reach the log today; from 3b, where payload content starts
    crossing into session and audit surfaces, it would. `_canonical_json`
    refuses to emit one either way, so both halves fail closed.
    """
    return json.loads(data, parse_constant=_reject_json_constant)


def _canonical_json(value: Any) -> str:
    """JSON in `JSON.stringify` byte-shape — no separator padding, raw UTF-8.

    Byte-length parity matters: `envelope_too_large` is a size verdict, and
    Python's default `", "` / `": "` separators plus `ensure_ascii=True`
    would make the same envelope measure larger here than on the TS side,
    so the two adapters would disagree about which envelopes are legal.

    `allow_nan=False` because the default EMITS `NaN` / `Infinity` /
    `-Infinity`, which are not JSON and which nothing outside Python's own
    decoder will read back. It raises instead, at any depth: in
    `validate_envelope` that surfaces as a `payload_not_serializable` reject,
    and in `AuditLog` it is what keeps every line of the stream parseable.
    """
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


# --- identity ---


def normalize_bot_name(value: Any) -> str | None:
    """Canonical bus identity, or None for a non-ASCII/invalid claim.

    Port of `normalizeBotName`: NFKC-fold then lowercase then match
    `^[a-z0-9_-]+$`. The NFKC fold is what stops `ｖｅｃ` (fullwidth) or `ⅴec`
    from being a distinct on-wire identity from `vec`.
    """
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).lower()
    return (
        normalized
        if _BOT_NAME_PATTERN.fullmatch(normalized) and normalized not in RESERVED_BOT_NAMES
        else None
    )


def normalize_allowlist(values: Iterable[Any]) -> set[str]:
    """Normalize a manifest `bot_names` list, rejecting invalid entries.

    Raises rather than skipping: a manifest entry that silently vanished
    from the allowlist would make that bot's envelopes drop as
    `from_claim_rejected`, which reads as a peer problem, not a config typo.
    """
    result: set[str] = set()
    for value in values:
        normalized = normalize_bot_name(value)
        if normalized is None:
            raise ValueError(f"Invalid fleet bot name: {value!r}")
        result.add(normalized)
    return result


def load_fleet_manifest_allowlist(path: str) -> set[str]:
    """Read `bot_names` out of the fleet manifest YAML."""
    manifest = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"Fleet manifest must be a YAML mapping: {path}")
    bot_names = manifest.get("bot_names")
    if not isinstance(bot_names, list) or not bot_names:
        raise ValueError(f"Fleet manifest bot_names must be a non-empty list: {path}")
    return normalize_allowlist(bot_names)


# --- envelopes ---


class EnvelopeValidationResult(NamedTuple):
    ok: bool
    envelope: dict | None = None
    error: str | None = None


def validate_envelope(
    value: Any,
    allowed_from_claims: frozenset[str] | set[str],
    max_bytes: int = DEFAULT_MAX_ENVELOPE_BYTES,
) -> EnvelopeValidationResult:
    """Validate the v1 wire envelope before it reaches any bus handler.

    Port of `validateEnvelope`. Field order and reject-code strings are
    load-bearing — the tap and the coordinator key dashboards off these codes,
    so a renamed code is a silent observability regression.

    Two deliberate divergences, both in the fail-closed direction:

    * `ts` is parsed with `datetime.fromisoformat` behind an extended-ISO
      gate, where TS uses `Date.parse` alone. `Date.parse` also accepts
      RFC-2822 and bare `YYYY` strings; this does not. Every fleet producer
      emits `toISOString()`, so the strictness only rejects timestamps
      nothing sends.
    * a non-finite float ANYWHERE in the envelope is `payload_not_serializable`
      here, because `_canonical_json` refuses to encode it. `JSON.stringify`
      does not throw on one — it writes `null` — so TS would accept such a
      value if it were ever handed one directly. It cannot arrive off the
      wire on either side (`JSON.parse` and `_json_loads` both reject the
      literals), so this only bites a caller that builds an envelope in
      process, and rejecting beats silently rewriting a number to `null`.
    """
    if not isinstance(value, dict):
        return EnvelopeValidationResult(False, error="envelope_not_object")

    # `type(...) is not int` rather than `!= ENVELOPE_VERSION`: `True == 1` in
    # Python, so a bare `!=` ACCEPTS `envelope_version: true` — which TS
    # rejects (`true !== 1`). Same envelope, two verdicts, no way to notice.
    # It also rejects a JSON `1.0`, which TS accepts (`1.0 === 1`); no encoder
    # emits that, and rejecting more is the divergence direction that fails
    # closed.
    version = value.get("envelope_version")
    if type(version) is not int or version != ENVELOPE_VERSION:
        return EnvelopeValidationResult(False, error="unsupported_envelope_version")

    identifier = value.get("id")
    if not isinstance(identifier, str) or not identifier:
        return EnvelopeValidationResult(False, error="invalid_id")

    kind = value.get("kind")
    if not isinstance(kind, str) or not kind:
        return EnvelopeValidationResult(False, error="invalid_kind")

    if not _is_parseable_ts(value.get("ts")):
        return EnvelopeValidationResult(False, error="invalid_ts")

    if "payload" not in value:
        return EnvelopeValidationResult(False, error="missing_payload")

    to = value.get("to")
    if "to" in value and to is not None and not isinstance(to, str):
        return EnvelopeValidationResult(False, error="invalid_to")

    if "in_reply_to" in value and not isinstance(value["in_reply_to"], str):
        return EnvelopeValidationResult(False, error="invalid_in_reply_to")

    from_claim = normalize_bot_name(value.get("from"))
    if from_claim is None or from_claim not in allowed_from_claims:
        return EnvelopeValidationResult(False, error="from_claim_rejected")

    try:
        encoded_bytes = len(_canonical_json(value).encode("utf-8"))
    except (TypeError, ValueError):
        return EnvelopeValidationResult(False, error="payload_not_serializable")
    if encoded_bytes > max_bytes:
        return EnvelopeValidationResult(False, error="envelope_too_large")

    baton_error = _validate_baton_fields(value, allowed_from_claims)
    if baton_error is not None:
        return EnvelopeValidationResult(False, error=baton_error)

    return EnvelopeValidationResult(True, envelope={**value, "from": from_claim})


def _validate_baton_fields(value: dict, allowed_from: frozenset[str] | set[str]) -> str | None:
    """Reject code for a present-but-malformed baton field, or None. v0.3d.

    Runs AFTER every §5 check, so an envelope that fails both reports the
    shared code the fleet already dashboards on. The codes here are all
    `yugo_`-prefixed because §5 has none for baton fields — fleet-bus SPEC §8
    is explicit that a drop reason is "a §5 code or an implementation-specific
    code prefixed with the harness name", and prefixing is what keeps these
    from colliding with a §5 addition when the baton fields land there.

    Rejecting rather than ignoring is a deliberate reading of clause 1. Clause
    1 says consumers must ignore fields they do not UNDERSTAND; from this
    slice yugo understands all four, and each one has a consequence:

    * `hops` is the ceiling. If a malformed `hops` were ignored, `hops: "16"`
      would walk straight past the backstop forever — the ceiling would be
      exactly the decoration the baton spec warns about.
    * `origin` is where the hop-8 warning is addressed. An origin that is not
      a routable bot name has nowhere to send it.
    * `root_id` and `owner` are copied onto envelopes we publish under our OWN
      `from`. Laundering a schema-invalid value under this bot's name is the
      forbidden direction of the TS-parity rule at the top of this module
      applied to the wire: accepting more than the schema does.

    Nothing on the live bus sends these fields today (the reference TS adapter
    has no baton code at all — `artifice-ia/claude-discord` a9d605e), so no
    existing producer can be broken by the strictness. `to`/`in_reply_to`
    already reject present-and-null; baton fields match that, which is why
    `is None` is not a special case below.

    Values are required to be ALREADY canonical rather than normalised into
    canonical form. `root_id` is "copied unchanged into every descendant" per
    the baton spec, and a field this module rewrites on the way through is not
    being copied unchanged — so the check refuses a non-canonical `origin`
    or `owner` instead of quietly folding it.
    """
    if "root_id" in value and (
        not isinstance(value["root_id"], str) or not value["root_id"]
    ):
        return "yugo_invalid_root_id"

    for name in ("origin", "owner"):
        if name not in value:
            continue
        claim = value[name]
        if (
            not isinstance(claim, str)
            or normalize_bot_name(claim) != claim
            or claim not in allowed_from
        ):
            return f"yugo_invalid_{name}"

    if "hops" in value:
        hops = value["hops"]
        # `type(...) is not int` for the same reason `envelope_version` uses
        # it: `True == 1` in Python, so a bare isinstance ACCEPTS `hops: true`
        # and then `true + 1` is `2` — a live counter built out of a boolean.
        # A JSON `8.0` is rejected too; the schema says integer, and no
        # encoder in the fleet emits a float there.
        if type(hops) is not int or hops < 0:
            return "yugo_invalid_hops"

    return None


def _is_parseable_ts(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if not _ISO_EXTENDED_TS_PATTERN.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def create_heartbeat_envelope(
    bot_name: str,
    plugin_version: str,
    pid: int | None = None,
    now: datetime | None = None,
    *,
    session_last_response_ts: str | None = None,
    injection_delivered_ts: str | None = None,
) -> dict:
    """Build the `status_heartbeat` envelope published to `fleet.<self>.status`.

    `session_last_response_ts` / `injection_delivered_ts` were
    present-and-null through 3a — the fields exist on the wire contract the
    tap already parses, and there was no session to report on until injection
    existed. From 3b they carry the real stamps, because that is the
    difference the tap uses to tell a bot whose LLM turn is wedged from one
    that is merely quiet: `process_alive_ts` keeps moving in both cases.
    They stay null until the first envelope is actually injected.
    """
    from_claim = normalize_bot_name(bot_name)
    if from_claim is None:
        raise ValueError(f"Invalid heartbeat bot name: {bot_name!r}")
    ts = _utc_now_iso(now)
    return {
        "envelope_version": ENVELOPE_VERSION,
        "id": str(uuid.uuid4()),
        "from": from_claim,
        "to": None,
        "kind": "status_heartbeat",
        "ts": ts,
        "payload": {
            "online": True,
            "process_alive_ts": ts,
            "session_last_response_ts": session_last_response_ts,
            "injection_delivered_ts": injection_delivered_ts,
            "pid": os.getpid() if pid is None else pid,
            "plugin_version": plugin_version,
        },
    }


def create_request_envelope(
    bot_name: str,
    to: str,
    payload: Any,
    *,
    kind: str = OUTBOUND_KIND,
    in_reply_to: str | None = None,
    baton: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Build a directed envelope for `fleet.<to>.request`.

    Same construction rules as `create_heartbeat_envelope` — canonical `from`,
    fresh UUIDv4 id, `Date.prototype.toISOString()`-shaped `ts` — because both
    ends of the wire read both kinds of envelope with the same validator.

    `in_reply_to` is OMITTED rather than emitted as null when there is none.
    Null is not "absent" here: `validate_envelope` (and its TS peer) reject a
    present-but-non-string `in_reply_to` as `invalid_in_reply_to`, so writing
    the key unconditionally would make every non-reply we send undeliverable.

    `baton` carries the four fields for THIS publish, already resolved — see
    `next_baton_fields`, which is what computes them from a received envelope.
    Passed as a dict rather than four keyword arguments so "no baton" is one
    falsy value instead of four `None`s that each have to be omitted
    individually: an envelope outside a baton chain must carry NONE of the
    fields, not four nulls (`validate_envelope` rejects a present-and-null
    baton field, exactly as it does for `in_reply_to`).

    **A key outside `BATON_FIELDS` RAISES** (Codex, PR #10), rather than being
    dropped on the floor: silently discarding a caller's key would leave them
    believing it took effect. Past that check the merge below cannot reach any
    other field.

    An unrestricted `envelope.update(baton)` — which is what this was — lets a
    caller that hands over anything wider than `next_baton_fields` (the inbound
    envelope itself is the obvious slip) overwrite `from`, `to`, `id` or
    `payload`. That is not merely untidy: `publish_request` picks the NATS
    subject from the PRE-merge recipient while `validate_envelope` only asks
    that the POST-merge `from` be allowlisted, so the two can disagree with
    everything still green — an envelope on `fleet.vec.request` claiming to be
    from a different allowlisted bot. SPEC §8 already concedes `from` is
    allowlist-checked and not cryptographically bound; a bug that lets it
    disagree with the subject undermines the one identity control the bus has.

    Raising matches how this function already treats a caller fault (see the
    two `ValueError`s above). `publish_request` catches it so its own
    never-raises contract holds, and audits `yugo_publish_failed`.

    Nothing is minted here. yugo never invents a `root_id`: the baton spec
    gives that to the originator ("Minted by the originator: `root_id = id` on
    the first message"), and a bot that mints one for a chain it did not start
    is claiming to have started it.
    """
    from_claim = normalize_bot_name(bot_name)
    if from_claim is None:
        raise ValueError(f"Invalid outbound bot name: {bot_name!r}")
    recipient = normalize_bot_name(to)
    if recipient is None:
        raise ValueError(f"Invalid outbound recipient: {to!r}")
    envelope = {
        "envelope_version": ENVELOPE_VERSION,
        "id": str(uuid.uuid4()),
        "from": from_claim,
        "to": recipient,
        "kind": kind,
        "ts": _utc_now_iso(now),
        "payload": payload,
    }
    if in_reply_to is not None:
        envelope["in_reply_to"] = in_reply_to
    if baton:
        unexpected = sorted(set(baton) - set(BATON_FIELDS))
        if unexpected:
            raise ValueError(
                f"baton carries non-baton keys {unexpected}; only "
                f"{list(BATON_FIELDS)} may be set this way — a wider merge "
                f"can overwrite from/to/id/payload after the subject is chosen"
            )
        # Safe BECAUSE of the three lines above: past them the key set is a
        # subset of BATON_FIELDS, so this update and a per-field projection
        # are provably the same write. A projection here as well would be a
        # branch no test could ever distinguish from this one.
        envelope.update(baton)
    return envelope


def next_baton_fields(envelope: dict) -> dict:
    """The baton fields carried by anything published onward from `envelope`.

    The whole of slice 3d's outbound half, in one function, and the one place
    the hop count moves.

    * `root_id`, `origin`, `owner` — copied unchanged. See
      `BATON_PASSTHROUGH_FIELDS` for why `owner` is not set to this bot.
    * `hops` — INCREMENTED. "Incremented on every pass" (baton spec). This is
      the line §15's "pass-through" summary would have omitted, and omitting
      it is what makes every downstream participant's count too low.

    An envelope carrying NO baton field at all is not part of a chain, and
    gets none back: yugo does not start batons (see `create_request_envelope`).

    An envelope carrying some baton field but no `hops` is drift — the spec
    has the originator set `hops: 0` on `baton.start`, so a chain always has
    one. Absent is read as 0 and the count starts at 1 from our pass onward.
    Undercounting from 1 is recoverable; leaving the counter absent means
    every bot after us has nothing to increment either, which is the failure
    this slice exists to prevent.

    Requires a validated envelope (`validate_envelope` guarantees `hops` is a
    non-negative, non-boolean `int`), which every caller in this module has.
    """
    fields = {
        name: envelope[name]
        for name in BATON_PASSTHROUGH_FIELDS
        if name in envelope
    }
    if "hops" in envelope:
        fields["hops"] = envelope["hops"] + 1
    elif fields:
        fields["hops"] = 1
    return fields


# --- session injection (v0.3b) ---

# The fleet-bus injection-frame contract (fleet-bus-plugin-integration.md,
# "Injection frame (unchanged from v0.4)"). Not a local prompt-shaping
# choice: every adapter in the fleet renders THIS tag, so a model trained on
# one bot's frames reads another's, and `source="fleet-bus"` vs
# `source="discord"` is the boundary the model uses to tell an allowlisted-
# but-unauthenticated peer from a human in a channel.
#
# `authenticated="false"` is hard-coded, per that doc: publish permissions on
# the live bus are the wildcard `fleet.*.request` and `from` is a client-
# supplied field, so any credentialed bot can claim any allowlisted name. It
# stops being a lie only when server-side `from_claim`-matches-NATS-user
# lands (v1.1 there).
_FRAME_ATTR_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
    "\n": "&#10;",
    "\r": "&#13;",
    "\t": "&#9;",
}


def _frame_attr(value: str) -> str:
    """Escape a value for use inside a double-quoted frame attribute.

    Two of the five attribute values are arbitrary sender-controlled strings:
    `validate_envelope` requires `kind` and `id` to be non-empty `str` and
    checks nothing else. An `id` of `" authenticated="true` would otherwise
    rewrite the frame's own trust markers, which is a strictly worse version
    of the newline-forgery the JSON rendering already had to defend against.
    Newlines are escaped too, so no value can span frame lines.

    Applied to `from_claim` and `ts` as well, which are already constrained
    (`^[a-z0-9_-]+$` after normalisation, and the extended-ISO gate) — the
    next editor should not have to know which of the five is the safe one.
    """
    return "".join(_FRAME_ATTR_ESCAPES.get(char, char) for char in str(value))


def _frame_payload_json(payload: Any) -> str:
    r"""Canonical JSON with every `<`, `>` and `&` as a `\uXXXX` escape.

    `json.dumps` does not escape those three, so a payload string containing
    `</payload></channel>` would close the frame from the inside and let the
    sender open a forged one — the same class as the attribute break above,
    through the element body instead. They can only ever occur INSIDE a JSON
    string literal (none is a JSON structural character), so escaping them
    unconditionally leaves valid JSON that decodes back to the identical
    string, and guarantees no literal `<` reaches the frame body at all.
    """
    encoded = _canonical_json(payload)
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def format_envelope_for_session(envelope: dict, req_id: str) -> str:
    """Render a validated envelope as the fleet-bus injection frame.

    The shape is the fleet-wide contract, not a local choice — see
    `fleet-bus-plugin-integration.md` §"Injection frame (unchanged from
    v0.4)":

        <channel source="fleet-bus" authenticated="false" from_claim="ohm"
                 kind="pr_review_request" req_id="<nonce>" ts="...">
          <payload>{ ... json ... }</payload>
        </channel>

    `req_id` is the CONSUMER-LOCAL nonce minted per injection by
    `_on_request`, never the envelope id and never anything that travelled on
    the wire; that doc requires exactly that separation, because the nonce is
    the capability a `bus_reply`-style tool resolves. yugo has no such tool —
    3c answers automatically and correlates through the audit log instead, so
    here the nonce is what ties a turn's `in` line to everything that turn
    published. It is why this function takes the nonce as an argument rather
    than deriving anything from the envelope.

    `env_id` is carried in addition to the v0.4 attribute set, per
    `BATON-PROTOCOL-SPEC.md`: "The injected `<channel>` tag exposes `req_id`
    but not `envelope.id`. Under this protocol it should carry `root_id`,
    `origin`, `owner` and `hops` too — otherwise an agent has to read
    `~/.claude/fleet-bus-log.jsonl` to know what baton it is holding." v0.3d
    renders those four, and ONLY when the envelope actually carries them: an
    envelope outside a baton chain renders byte-for-byte the frame 3b did, so
    the additive-field contract holds on the model-facing side too.

    They are APPENDED after the contract's seven attributes rather than
    interleaved, so the fleet-bus SPEC §7 attribute set stays an intact
    prefix of what we emit. A reader diffing our frame against the spec's
    finds an addition, not a rearrangement.

    Only the payload goes inside `<payload>`, JSON-encoded. Every value that
    crosses into the frame is escaped first (`_frame_attr`,
    `_frame_payload_json`): `kind` and `id` are arbitrary sender-controlled
    strings, and an unescaped one can rewrite the frame's own trust markers
    or close the element and open a forged frame.

    No `role: system` message is emitted around it — `history.build_messages`
    owns the single system slot (the persona), and provider transforms hoist
    any additional system message to the top-level system param regardless of
    position, which would collapse persona and frame into one blob.
    """
    attributes = [
        'source="fleet-bus"',
        'authenticated="false"',
        f'from_claim="{_frame_attr(envelope["from"])}"',
        f'kind="{_frame_attr(envelope["kind"])}"',
        f'env_id="{_frame_attr(envelope["id"])}"',
        f'req_id="{_frame_attr(req_id)}"',
        f'ts="{_frame_attr(envelope["ts"])}"',
    ]
    attributes.extend(
        f'{name}="{_frame_attr(envelope[name])}"'
        for name in BATON_FIELDS
        if name in envelope
    )
    opening = "\n         ".join(attributes)
    return (
        f"<channel {opening}>\n"
        f"  <payload>{_frame_payload_json(envelope.get('payload'))}</payload>\n"
        "</channel>"
    )


# --- outbound `<BUS>` tags (v0.3c) ---

# The grammar, in full:
#
#     <BUS to="vec">…message text…</BUS>
#
# One required attribute (`to`), the message as the element BODY. It addresses
# a THIRD party — answering the sender needs no tag at all, because a
# bus-triggered turn auto-replies (see `FleetBus._publish_turn_output`), and
# addressing THIS bot is refused outright (a self-addressed tag is a
# self-driving loop the reply guard cannot see).
#
# SPEC §15 3c names the tag and never defines it; §15 5x is the only other
# mention and shows an unrelated `class=` attribute. This shape was the repo
# owner's call when the slice was specced, and it is deliberately NOT
# the shape the sibling Python adapter design proposes for codex-container
# (`<BUS to="…" kind="…" payload="{…}" />`, fleet-bus repo
# `docs/CODEX-ADAPTER-DESIGN.md` §6 at 820e4d8) — a body carries free text
# without the model having to JSON-escape a prose paragraph into an attribute.
# The divergence is model-facing only: nothing about it reaches the wire, where
# both adapters emit the same v1 envelope. It is flagged for a fleet-wide
# ruling before 5b ports the codex adapter.
_BUS_TAG_OPEN = "<BUS"
_BUS_TAG_CLOSE = "</BUS>"

# Reject reasons 3c can write.
#
# Anything the shared taxonomy already names is spelled the way it spells it —
# a tag we cannot resolve to a publishable recipient is `invalid_to`, the
# fleet-bus SPEC §5 code, and an envelope we build and then fail to validate
# reports whatever §5 code `validate_envelope` returns. The three below have no
# §5 spelling because §5 describes the INBOUND wire and these are all upstream
# of any wire: a tag that never parsed, a publish that never left, a reply we
# chose not to send. §8 covers exactly that case — a drop reason is "a §5 code
# or an implementation-specific code prefixed with the harness name" — and the
# sibling adapter design uses the same convention with `codex_adapter_`.
# Prefixing is what keeps them from colliding with a future §5 addition.
REJECT_TAG = "yugo_bus_tag_rejected"
REJECT_PUBLISH_FAILED = "yugo_publish_failed"
REJECT_AUTOREPLY_SUPPRESSED = "yugo_autoreply_suppressed"

# v0.3d, same prefixing rule and the same reason: §5 describes the inbound
# wire and has no baton vocabulary at all. The ceiling is not a malformed
# envelope — `validate_envelope` has already passed it — it is a WELL-FORMED
# envelope we refuse to carry any further, so it cannot borrow a §5 code.
REJECT_HOPS_EXCEEDED = "yugo_baton_hops_exceeded"
REJECT_WARNING_SUPPRESSED = "yugo_baton_warning_suppressed"

# `note` on the `out` line of a hop-8 warning. Without it the warning is
# indistinguishable in the audit log from any other `text_message` this bot
# publishes, and "did the fleet ever actually warn anybody" is the first
# question anyone asks of a chain that ran to 16.
AUDIT_NOTE_HOP_WARNING = "baton_hop_warning"


class BusTag(NamedTuple):
    """One `<BUS …>…</BUS>` span found in a turn's reply text.

    `start`/`end` bound the span in the ORIGINAL text. A tag that failed to
    parse still reports its span, with `attrs`/`body` None and `cause` saying
    why: the span has to be stripped whether or not it published, or the raw
    markup rides out to the sender inside the auto-reply.
    """

    start: int
    end: int
    attrs: dict[str, str] | None
    body: str | None
    cause: str | None = None


def find_bus_tags(text: str) -> list[BusTag]:
    """Scan a turn's reply for `<BUS …>…</BUS>` spans, in document order.

    Hand-rolled rather than a regex, for the reason the sibling adapter design
    records (fleet-bus `docs/CODEX-ADAPTER-DESIGN.md` §6): a `<BUS[^>]*>` regex
    terminates on the first `>`, which may be inside a quoted attribute value,
    and the tag then both mis-parses AND survives into the emitted text. The
    open-tag scan below tracks quote state instead.

    A malformed or unterminated tag NEVER raises and never publishes — it
    comes back with `attrs=None` and a `cause`, so the caller can audit it and
    still strip it. The turn itself is already over by then; a parse fault
    must not cost the sender their answer.
    """
    tags: list[BusTag] = []
    index = 0
    while True:
        start = text.find(_BUS_TAG_OPEN, index)
        if start == -1:
            return tags
        after = start + len(_BUS_TAG_OPEN)
        # `<BUSY>` is not a `<BUS>` tag with a typo, it is a different word.
        # `>` is accepted alongside whitespace so the attribute-less `<BUS>`
        # is recognised — and rejected as `invalid_to` — rather than silently
        # left in the text as prose.
        if after < len(text) and not text[after].isspace() and text[after] != ">":
            index = after
            continue
        open_end = _scan_open_tag(text, after)
        if open_end is None:
            # Ran off the end mid-tag. Everything from `<BUS` on is tag
            # wreckage; take the span to the end of the text so it is stripped.
            tags.append(BusTag(start, len(text), None, None, "unclosed"))
            return tags
        body_end = text.find(_BUS_TAG_CLOSE, open_end + 1)
        if body_end == -1:
            tags.append(BusTag(start, len(text), None, None, "unclosed"))
            return tags
        end = body_end + len(_BUS_TAG_CLOSE)
        try:
            attrs = _parse_tag_attrs(text[after:open_end])
        except ValueError:
            tags.append(BusTag(start, end, None, None, "malformed"))
        else:
            tags.append(BusTag(start, end, attrs, text[open_end + 1 : body_end]))
        index = end


def _scan_open_tag(text: str, pos: int) -> int | None:
    """Index of the `>` closing an open tag, or None if the text runs out."""
    quote: str | None = None
    while pos < len(text):
        char = text[pos]
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == ">":
            return pos
        pos += 1
    return None


def _parse_tag_attrs(inner: str) -> dict[str, str]:
    """Parse `key="value"` / `key='value'` pairs, or raise ValueError.

    Values MUST be quoted. Unquoted attributes are the shape most likely to be
    a half-written tag, and the parser has no way to tell `to=vec now` (one
    attribute plus prose) from a recipient named `vec now`.

    Attributes other than `to` are parsed and IGNORED — `kind` and the baton
    fields are not part of this grammar, and dropping the whole message over a
    decorative attribute would lose text the model meant to send. What they
    are not is silently honoured: everything published from a tag is
    `text_message`, and its baton fields come from the RECEIVED envelope
    (`FleetBus._publish_turn_output`), never from here. A model that could
    write `hops="0"` in an attribute could reset the ceiling on every pass.
    """
    attrs: dict[str, str] = {}
    pos = 0
    while pos < len(inner):
        if inner[pos].isspace():
            pos += 1
            continue
        name_start = pos
        while pos < len(inner) and (inner[pos].isalnum() or inner[pos] in "_-"):
            pos += 1
        name = inner[name_start:pos]
        if not name:
            raise ValueError(f"expected an attribute name at offset {pos}")
        if pos >= len(inner) or inner[pos] != "=":
            raise ValueError(f"expected '=' after attribute {name!r}")
        pos += 1
        if pos >= len(inner) or inner[pos] not in "\"'":
            raise ValueError(f"attribute {name!r} must be quoted")
        quote = inner[pos]
        pos += 1
        value_start = pos
        while pos < len(inner) and inner[pos] != quote:
            pos += 1
        if pos >= len(inner):
            raise ValueError(f"attribute {name!r} has no closing {quote}")
        attrs[name] = inner[value_start:pos]
        pos += 1
    return attrs


def _audit_excerpt(value: Any) -> str:
    """A model-authored string cut to something an audit line can carry.

    Both values 3c reports back — a rejected tag's raw text and the `to` it
    claimed — are written by the LLM and bounded by nothing. The audit sink is
    a JSONL file the tap tails, so the excerpt identifies WHICH tag failed
    without letting one bad turn write a megabyte into it.
    """
    text = value if isinstance(value, str) else repr(value)
    if len(text) <= AUDIT_RAW_MAX_CHARS:
        return text
    return text[:AUDIT_RAW_MAX_CHARS] + "…"


def strip_bus_tags(text: str, tags: Iterable[BusTag]) -> str:
    """Remove every found span — parsed or not — from the reply text.

    This is what keeps raw markup out of the auto-reply. The sender asked a
    question; the answer they get should not contain this bot's instructions
    to a third one.
    """
    kept: list[str] = []
    cursor = 0
    for tag in tags:
        kept.append(text[cursor : tag.start])
        cursor = tag.end
    kept.append(text[cursor:])
    return "".join(kept)


# --- audit ---


def _encode_audit_line(entry: Mapping[str, Any]) -> bytes:
    """One audit line, all the way to the BYTES that get written.

    The UTF-8 encode is part of the check rather than a step after it, so a
    value the file cannot hold fails where the caller salvages instead of
    where the caller only expects `OSError`.
    """
    return f"{_canonical_json(entry)}\n".encode("utf-8")


def _audit_encodable(value: Any) -> bool:
    """Can this value reach the audit file — encoder AND UTF-8?"""
    try:
        _canonical_json(value).encode("utf-8")
    except (TypeError, ValueError):
        return False
    return True


class AuditLog:
    """Append-only JSONL audit sink for bus traffic.

    `record()` takes `direction` and `subject` POSITIONALLY rather than as
    part of an open `dict`, so "every audit line carries ts/dir/subject" is
    enforced by the signature instead of by reviewer attention. Connection
    lifecycle lines pass `subject=None` — there is no subject to report — but
    the key is still emitted, so a log consumer can index on it uniformly.

    `dir` lanes are the shared fleet-bus audit schema — `in` / `out` for wire
    traffic and `drop` for a rejected inbound — plus `conn` for this adapter's
    connection lifecycle, which has no wire event to attach to. A v0.3b
    session turn does NOT get a lane of its own: success is the `in` line for
    that envelope, carrying the locally minted `req_id`, and failure is
    `drop` / `injection_failed`.

    Mode is 0600 at CREATE time, not just chmod-after: the file may hold
    envelope ids and peer identities, and a 0644 window between create and
    chmod is a window.
    """

    def __init__(self, path: str | None, logger=print) -> None:
        self.path = path
        self._logger = logger

    def record(self, direction: str, subject: str | None, **fields: Any) -> None:
        entry = {"ts": _utc_now_iso(), "dir": direction, "subject": subject, **fields}
        try:
            blob = _encode_audit_line(entry)
        except (TypeError, ValueError) as e:
            # The sink is JSONL, so one unparseable line poisons every
            # consumer that reads the file from then on. A field the encoder
            # refuses (a non-finite float, an arbitrary object) therefore
            # degrades to a valid-JSON stand-in that keeps ts/dir/subject,
            # rather than being written raw — which is what `allow_nan`'s
            # default used to do — or dropped, which loses the event.
            #
            # The encode runs INSIDE this `try`, not after it. `_canonical_json`
            # is `ensure_ascii=False`, so a lone surrogate — which `json.loads`
            # builds from an ordinary `"\ud800"` escape, ASCII on the wire —
            # produces a `str` happily and only fails at `.encode("utf-8")`,
            # as a `UnicodeEncodeError`. That is a `ValueError` and NOT an
            # `OSError`, so with the encode down at the write it sailed past
            # the `except OSError` below, out of `record`, and on the inbound
            # lane out of a NATS subscription callback — leaving the audit
            # file created and EMPTY. Fixed alongside the identical shape in
            # `tools.ToolAuditLog.record`; the class is "the audit writer can
            # take down the thing it audits", not "NaN".
            #
            # `subject` is peer-supplied, so it is coerced here rather than
            # copied from the raw entry, for the same reason.
            blob = _encode_audit_line(
                {
                    "ts": entry["ts"],
                    "dir": direction,
                    "subject": subject if _audit_encodable(subject) else repr(subject),
                    "event": "audit_encode_failed",
                    "error": repr(e),
                }
            )
        if not self.path:
            # Decoding what was just encoded rather than reusing the `str`:
            # a logger that raised would cost the line as dearly as a writer
            # that did.
            self._logger(f"[fleet-bus] {blob.decode('utf-8').rstrip()}")
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
            # An unwritable audit path must not take the bus down; the bus is
            # the thing being audited, and a full disk is not a wire fault.
            self._logger(
                f"[fleet-bus] audit write failed: {e!r} · "
                f"{blob.decode('utf-8').rstrip()}"
            )


# --- config ---


async def _cancel_task(task: "asyncio.Task | None") -> None:
    """Cancel a helper task and absorb its CancelledError, nothing else."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


class DurableEnvelopeDedupStore:
    """SQLite envelope-id claims retained for 8d (7d stream age + 1d slack).

    A primary-key INSERT OR IGNORE is the concurrency arbiter. The lock
    serializes this adapter's callbacks on one connection; the UNIQUE key also
    makes competing processes deterministic.
    """

    def __init__(self, path: str, ttl_s: int = DEFAULT_DEDUP_TTL_S,
                 lease_s: int = DEFAULT_DEDUP_LEASE_S) -> None:
        if path != ":memory:" and ttl_s < MIN_DEDUP_TTL_S:
            raise ValueError("durable dedup TTL must be at least the 7-day stream max_age")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS envelope_dedup_v2 ("
            "envelope_id TEXT PRIMARY KEY, first_seen_s REAL NOT NULL, req_id TEXT NOT NULL, "
            "state TEXT NOT NULL CHECK(state IN ('pending','completed')), "
            "lease_owner TEXT NOT NULL, lease_until_s REAL NOT NULL)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS envelope_dedup_v2_first_seen ON envelope_dedup_v2(first_seen_s)")
        self._ttl_s = ttl_s
        self._lease_s = lease_s
        self._lock = threading.Lock()
        self._claims = 0

    def claim(self, envelope_id: str, req_id: str, now_s: float | None = None) -> tuple[bool, str, str | None]:
        now = datetime.now(timezone.utc).timestamp() if now_s is None else now_s
        owner = uuid.uuid4().hex
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._claims += 1
                if self._claims % DEDUP_PRUNE_EVERY == 0:
                    self.prune(now)
                self._db.execute(
                    "DELETE FROM envelope_dedup_v2 WHERE envelope_id=? AND first_seen_s < ?",
                    (envelope_id, now - self._ttl_s),
                )
                cursor = self._db.execute(
                    "INSERT OR IGNORE INTO envelope_dedup_v2 VALUES (?,?,?,'pending',?,?)",
                    (envelope_id, now, req_id, owner, now + self._lease_s),
                )
                if cursor.rowcount == 1:
                    result = (False, req_id, owner)
                else:
                    row = self._db.execute(
                        "SELECT req_id,state,lease_until_s FROM envelope_dedup_v2 WHERE envelope_id=?",
                        (envelope_id,),
                    ).fetchone()
                    if row[1] == "pending" and row[2] <= now:
                        changed = self._db.execute(
                            "UPDATE envelope_dedup_v2 SET lease_owner=?,lease_until_s=? "
                            "WHERE envelope_id=? AND state='pending' AND lease_until_s<=?",
                            (owner, now + self._lease_s, envelope_id, now),
                        ).rowcount
                        result = (False, row[0], owner) if changed else (True, row[0], None)
                    else:
                        result = (True, row[0], None)
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    @property
    def lease_s(self) -> float:
        """The lease this store issues. Callers deriving a renewal cadence MUST
        read this rather than the module default, which this store may not use."""
        return self._lease_s

    def renew(self, envelope_id: str, owner: str, now_s: float | None = None) -> bool:
        """Extend a live owner's lease. False means the lease was already lost.

        False means another consumer has taken the envelope and anything this
        turn still does is the duplicate, not the original.

        Stated honestly: no caller can act on that today. There is no cancel
        handle at this boundary, so `_renew_claim_until_done` records the loss
        and stops renewing while the turn runs to completion. That is the
        at-least-once contract working as designed, not a gap in this method —
        closing it is yugo#24. The return value exists so a future caller with
        a cancel handle has something to fence on.
        """
        now = datetime.now(timezone.utc).timestamp() if now_s is None else now_s
        with self._lock:
            return self._db.execute(
                "UPDATE envelope_dedup_v2 SET lease_until_s=? "
                "WHERE envelope_id=? AND lease_owner=? AND state='pending'",
                (now + self._lease_s, envelope_id, owner),
            ).rowcount == 1

    def complete(self, envelope_id: str, owner: str) -> bool:
        """Mark done. False means we no longer owned it, so we did NOT finish it.

        A lost owner must not be recorded as a successful completion: the
        in-memory fast paths key off this return, and promoting a stale claim
        would suppress the real owner's result.
        """
        with self._lock:
            # `AND state='pending'` matches the TypeScript port exactly. Without
            # it a same-owner double-complete returns True here and False there,
            # and the return value is now load-bearing on both sides.
            return self._db.execute(
                "UPDATE envelope_dedup_v2 SET state='completed' "
                "WHERE envelope_id=? AND lease_owner=? AND state='pending'",
                (envelope_id, owner),
            ).rowcount == 1

    def release(self, envelope_id: str, owner: str) -> bool:
        with self._lock:
            return self._db.execute(
                "DELETE FROM envelope_dedup_v2 WHERE envelope_id=? AND lease_owner=? AND state='pending'",
                (envelope_id, owner),
            ).rowcount == 1

    def prune(self, now_s: float, budget: int = DEDUP_PRUNE_BUDGET) -> int:
        """Delete expired claims in bounded batches until drained or out of budget.

        Bounded batches keep any single statement short; the loop is what makes
        cleanup able to OUTPACE ingestion. A short batch means the expired set
        is exhausted, so the loop stops without spending the rest of the budget.
        """
        cutoff = now_s - self._ttl_s
        deleted = 0
        while deleted < budget:
            batch = min(DEDUP_PRUNE_LIMIT, budget - deleted)
            n = self._db.execute(
                "DELETE FROM envelope_dedup_v2 WHERE rowid IN (SELECT rowid FROM envelope_dedup_v2 "
                "WHERE first_seen_s < ? ORDER BY first_seen_s LIMIT ?)",
                (cutoff, batch),
            ).rowcount
            deleted += n
            if n < batch:  # expired set exhausted
                break
        return deleted

    def prune_idle(self, now_s: float | None = None) -> int:
        """Sweep on a quiet lane, where no claim arrives to trigger the counter.

        Without this, a stream that goes quiet after a burst keeps its expired
        rows until the next arrival — the backlog survives precisely when
        there is most capacity to clear it.
        """
        now = datetime.now(timezone.utc).timestamp() if now_s is None else now_s
        with self._lock:
            return self.prune(now)


@dataclass(frozen=True)
class FleetBusConfig:
    bot_name: str
    url: str
    user: str
    password: str = field(repr=False)
    allowed_from: frozenset[str]
    plugin_version: str
    audit_log_path: str | None = DEFAULT_AUDIT_LOG
    max_envelope_bytes: int = DEFAULT_MAX_ENVELOPE_BYTES
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    reconnect_time_wait_s: float = DEFAULT_RECONNECT_TIME_WAIT_S
    drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S
    dedup_store_path: str | None = None
    dedup_ttl_s: int = DEFAULT_DEDUP_TTL_S


def bus_enabled(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get("FLEET_BUS_ENABLED", "0").strip() == "1"


def load_config_from_env(
    plugin_version: str,
    env: dict[str, str] | None = None,
) -> FleetBusConfig:
    """Build the bus config, or raise FleetBusConfigError naming the bad var.

    Called at import when the bus is enabled. Everything here is a CONFIG
    fault — a fault the operator can only fix by editing env or files — so it
    is fatal at startup. A NATS server that is merely unreachable is NOT a
    config fault and never reaches this function; that case degrades to
    Discord-only and retries forever (see `FleetBus.run`).
    """
    source = dict(os.environ if env is None else env)

    raw_bot_name = source.get("BOT_NAME", "").strip()
    if not raw_bot_name:
        raise FleetBusConfigError(
            "BOT_NAME is required when FLEET_BUS_ENABLED=1 — it is the fleet "
            "identity every `fleet.<self>.*` subject is built from"
        )
    bot_name = normalize_bot_name(raw_bot_name)
    if bot_name is None:
        raise FleetBusConfigError(
            f"BOT_NAME must match ^[a-z0-9_-]+$ after NFKC folding; got {raw_bot_name!r}"
        )

    user = source.get("FLEET_BUS_USER", "").strip() or bot_name
    if normalize_bot_name(user) != bot_name:
        # The NATS user IS the from-claim the server will let us publish under;
        # a mismatch means every heartbeat we send is rejected by authz.
        raise FleetBusConfigError(
            f"FLEET_BUS_USER ({user!r}) must be the same canonical fleet "
            f"identity as BOT_NAME ({bot_name!r})"
        )

    token_file = source.get("FLEET_BUS_TOKEN_FILE", "").strip()
    if not token_file:
        raise FleetBusConfigError(
            "FLEET_BUS_TOKEN_FILE is required when FLEET_BUS_ENABLED=1"
        )
    try:
        password = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise FleetBusConfigError(
            f"FLEET_BUS_TOKEN_FILE is unreadable ({token_file!r}): {e!r}"
        ) from e
    if not password:
        raise FleetBusConfigError(
            f"FLEET_BUS_TOKEN_FILE is empty ({token_file!r})"
        )

    manifest_path = source.get("FLEET_BUS_MANIFEST_PATH", "").strip() or DEFAULT_MANIFEST_PATH
    try:
        allowed_from = load_fleet_manifest_allowlist(manifest_path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise FleetBusConfigError(
            f"FLEET_BUS_MANIFEST_PATH is unusable ({manifest_path!r}): {e!r}"
        ) from e

    if bot_name not in allowed_from:
        # Our own `from` claim is validated against this allowlist like anyone
        # else's, and `no_echo` is off, so a bot missing from the manifest
        # drops its OWN heartbeat as `from_claim_rejected` — one bogus drop
        # line per interval, forever, on a bot that otherwise looks healthy.
        # README already says BOT_NAME must be listed; this is that sentence
        # made enforceable.
        raise FleetBusConfigError(
            f"BOT_NAME ({bot_name!r}) is not in the bot_names of the manifest "
            f"at FLEET_BUS_MANIFEST_PATH ({manifest_path!r}); the adapter "
            f"would reject its own heartbeat. Known: {sorted(allowed_from)}"
        )

    return FleetBusConfig(
        bot_name=bot_name,
        url=source.get("FLEET_BUS_URL", "").strip() or DEFAULT_URL,
        user=bot_name,
        password=password,
        allowed_from=frozenset(allowed_from),
        plugin_version=plugin_version,
        audit_log_path=source.get("FLEET_BUS_AUDIT_LOG", "").strip() or DEFAULT_AUDIT_LOG,
        dedup_store_path=(source.get("YUGO_DEDUP_STORE_PATH", "").strip()
                          or f"/var/lib/yugo/{bot_name}-dedup.sqlite"),
    )


# --- adapter ---


def _is_authorization_failure(error: BaseException) -> bool:
    """Is this the server refusing our credentials, rather than a wire fault?

    Both checks are load-bearing. nats-py only builds an `AuthorizationError`
    for a violation seen on an ALREADY-ESTABLISHED connection; the one a
    wrong-password bot actually hits is raised during CONNECT, as a bare
    `errors.Error("nats: 'Authorization Violation'")`, so matching the class
    alone classifies none of the lines the operator is drowning in.
    """
    nats = _lazy_nats()
    if isinstance(error, nats.errors.AuthorizationError):
        return True
    return "authorization violation" in str(error).lower()


class FleetBus:
    """Owns one NATS connection: subscribe, heartbeat, audit, inject, publish.

    `on_envelope` is the session seam (v0.3b). It is awaited with ONE
    validated envelope AND the consumer-local `req_id` minted for it, and
    returns the turn's reply text, or None. The nonce is part of the hook
    contract (as in the TS port's `FleetBusSessionEvent {envelope, reqId}`)
    because the injection frame has to carry it, and because from 3c it is the
    handle that ties a turn to every envelope that turn published in the audit
    log. It cannot be derived from the envelope. The hook is kept
    injectable rather than importing `bot` here for two reasons: this module
    must stay free of the LLM/Discord half of the process (an `import bot`
    would be circular), and a bus with no hook is the exact 3a behaviour —
    audit and drop — which every existing lifecycle test still relies on.
    """

    def __init__(
        self,
        config: FleetBusConfig,
        audit: AuditLog,
        *,
        logger=print,
        on_envelope=None,
    ) -> None:
        self._config = config
        self._audit = audit
        self._log = logger
        self._nc = None
        self._on_envelope = on_envelope
        # Reported on every heartbeat from 3b. See create_heartbeat_envelope.
        self._injection_delivered_ts: str | None = None
        self._session_last_response_ts: str | None = None
        self._dedup = DurableEnvelopeDedupStore(
            config.dedup_store_path or ":memory:", config.dedup_ttl_s
        )

    @property
    def subjects(self) -> tuple[str, ...]:
        """Subjects this adapter subscribes, in subscribe order.

        NOT `fleet.<self>.inbox`. SPEC §15's 3a line says `.inbox`; §7 says the
        adapter subscribes `.request` directly until its FB-3 flip, and §7 is
        the one that matches the live wire (and the reference TS adapter). See
        the §15 erratum. `.inbox` does not exist pre-JetStream, and per-user
        authz does not grant subscribe on it — and a nats-py permissions
        violation calls `error_cb` and RETURNS without closing the connection,
        so subscribing it would leave a bot connected, heartbeating and deaf.
        """
        name = self._config.bot_name
        return (
            f"fleet.{name}.request",
            f"fleet.{name}.result",
            f"fleet.{name}.status",
            "fleet.broadcast.>",
        )

    @property
    def status_subject(self) -> str:
        return f"fleet.{self._config.bot_name}.status"

    @property
    def request_subject(self) -> str:
        """The ONE subject whose envelopes can reach the session (v0.3b).

        Compared with `==` in `_on_message`, never `endswith(".request")`:
        `fleet.broadcast.>` is a subscribed wildcard, so a peer publishing to
        `fleet.broadcast.request` hits a suffix test and would buy itself the
        ambient broadcast→prompt injection SPEC §7.1 exists to forbid.

        Arriving here is necessary, not sufficient — `_on_request` still has
        to agree the envelope was addressed to us.
        """
        return f"fleet.{self._config.bot_name}.request"

    @property
    def connection(self):
        """The live nats client, or None. Read-only view for tests + ops."""
        return self._nc

    async def run(self) -> None:
        """Supervisor body. Returns only via cancellation.

        A NATS server that is down at boot is TRANSIENT, not fatal: the bot
        keeps serving Discord and this loop keeps retrying. With
        `max_reconnect_attempts=-1` nats-py's own `connect()` already retries
        forever internally and fires `error_cb` per attempt (that is SPEC §10's
        "audit each attempt"), so this outer loop mostly covers the paths that
        do raise, plus re-entry if the connection ever reaches CLOSED.
        """
        while True:
            try:
                await self._connect()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — every connect fault is retryable here
                self._audit.record("conn", None, event="connect_failed", error=repr(e))
                await asyncio.sleep(self._config.reconnect_time_wait_s)
                continue
            try:
                await self._heartbeat_loop()
            finally:
                await self._teardown()

    async def _connect(self) -> None:
        nats = _lazy_nats()
        self._nc = await nats.connect(
            servers=[self._config.url],
            user=self._config.user,
            password=self._config.password,
            name=self._config.bot_name,
            inbox_prefix=f"_INBOX_{self._config.bot_name}".encode("utf-8"),
            # -1 is INFINITE. nats-py's default is 60 attempts at 2s flat;
            # past that it discards the server, empties the pool and calls
            # close() — permanently. Any outage longer than ~2 minutes would
            # leave a bus-less bot until someone restarted the process.
            max_reconnect_attempts=-1,
            reconnect_time_wait=self._config.reconnect_time_wait_s,
            drain_timeout=self._config.drain_timeout_s,
            error_cb=self._on_error,
            disconnected_cb=self._on_disconnected,
            reconnected_cb=self._on_reconnected,
            closed_cb=self._on_closed,
        )
        # Everything past the assignment runs on a LIVE client, so every fault
        # from here on has to close it before it propagates. A half-built
        # client that keeps its subscriptions is unreachable after the next
        # `_connect` overwrites `self._nc` — and with
        # `max_reconnect_attempts=-1` it survives every outage, replaying its
        # own subscriptions into `_on_message` forever. That is a doubled
        # audit trail today and two LLM turns per envelope at 3b. Same shape
        # as fleet-bus.ts's `catch { this.nc = undefined; await nc.close() }`.
        try:
            # `no_echo` is deliberately left at its default (False): the adapter
            # receives its own heartbeat on `fleet.<self>.status`, which is what
            # makes publish → server → subscribe → validate → audit testable with
            # no peer on the bus at all.
            for subject in self.subjects:
                await self._nc.subscribe(subject, cb=self._on_message)
            await self._nc.flush()
        # BaseException, not Exception: SIGTERM during the `flush()` — which
        # blocks up to 10s — cancels this coroutine, and a leak on the
        # shutdown path is the one nothing ever cleans up.
        except BaseException:
            client, self._nc = self._nc, None
            try:
                await client.close()
            except Exception as e:  # noqa: BLE001 — the ORIGINAL fault is the one to report
                self._audit.record(
                    "conn", None, event="orphan_close_failed", error=repr(e)
                )
            raise
        self._audit.record(
            "conn",
            None,
            event="connected",
            url=str(self._nc.connected_url.netloc if self._nc.connected_url else self._config.url),
            subjects=list(self.subjects),
        )

    async def _heartbeat_loop(self) -> None:
        while self._nc is not None and not self._nc.is_closed:
            await self._publish_heartbeat()
            await asyncio.sleep(self._config.heartbeat_interval_s)

    async def _publish_heartbeat(self) -> None:
        if self._nc is None or self._nc.is_closed:
            return
        # Piggyback the quiet-lane sweep on the beat, matching the TypeScript
        # port. `prune` is otherwise only reached from `claim`, so a lane that
        # goes silent after a burst keeps its expired rows until the next
        # arrival. Contained: a store fault must never stop heartbeats, which
        # are this bot's liveness signal.
        try:
            self._dedup.prune_idle()
        except Exception as e:  # noqa: BLE001 — liveness outranks cleanup
            self._audit.record(
                "conn", self.status_subject, event="idle_prune_failed", error=repr(e)
            )
        envelope = create_heartbeat_envelope(
            self._config.bot_name,
            self._config.plugin_version,
            session_last_response_ts=self._session_last_response_ts,
            injection_delivered_ts=self._injection_delivered_ts,
        )
        try:
            await self._nc.publish(
                self.status_subject, _canonical_json(envelope).encode("utf-8")
            )
        except Exception as e:  # noqa: BLE001 — a failed beat must not end the loop
            # Mid-outage nats-py buffers publishes and can raise once the
            # pending buffer is full. Audit and keep beating; the reconnect
            # will flush or drop, and either way the bot stays alive.
            self._audit.record(
                "conn", self.status_subject, event="heartbeat_failed", error=repr(e)
            )
            return
        self._audit.record(
            "out", self.status_subject, id=envelope["id"], kind=envelope["kind"]
        )

    async def publish_request(
        self,
        to: Any,
        payload: Any,
        *,
        kind: str = OUTBOUND_KIND,
        in_reply_to: str | None = None,
        baton: dict | None = None,
        req_id: str | None = None,
        source_subject: str | None = None,
        audit_note: str | None = None,
    ) -> bool:
        """Publish one directed envelope to `fleet.<to>.request`. v0.3c/3d.

        Returns whether it reached the wire. Never raises: a peer we cannot
        publish to is one lost message, not a dead subscription — the caller is
        a NATS callback, and every path out of here has to leave it running.

        `.request` for replies too, not `.result`. SPEC §7 removed `.result` as
        a subject class ("Replies travel as ordinary `.request` envelopes
        carrying an `in_reply_to` field"), and the wire agrees with the SPEC:
        `_on_message` here injects `.request` and only `.request`, and the
        reference TS adapter's `onResult` is a stub that logs the subject and
        returns (`artifice-ia/claude-discord` a9d605e,
        `src/fleet-bus.ts:302-305`). A reply published to `.result` would be
        delivered, validated and ignored by every adapter in the fleet. Note
        the fleet-bus SPEC §6 still describes `.result` as the reply lane; that
        conflict is reported with the PR, and yugo's own SPEC wins here.

        THREE gates, in this order, and the first is the one that matters:

        * the recipient must normalise AND be in the manifest allowlist. The
          same list that decides whose `from` we accept decides who we will
          talk to — it is the fleet roster, and `to` is a subject token, so an
          unchecked one both invents traffic for a bot that may not exist and
          writes model-authored text straight into a NATS subject.
        * the envelope must pass `validate_envelope`, the same function the
          inbound path uses. That is SPEC §15 3c's "per envelope validation
          from §5" — the fleet-bus SPEC's §5, which is the reject-code
          taxonomy. It is what stops us emitting an envelope a peer would drop:
          an oversize payload, or one that cannot be encoded at all. From 3d it
          also covers the `baton` dict, so a caller cannot hand this API a
          malformed baton field and have us put it on the wire under our name.
        * the connection must be live. `_nc` is None between reconnects.

        `baton` (v0.3d) is the already-resolved field set for this publish —
        `next_baton_fields` computes it, and the hop count in it is ALREADY
        incremented. This method does not increment: one publish, one hop, and
        a method that both took a baton and mutated it would double-count
        whenever a caller published twice from one received envelope, which is
        exactly what a turn carrying a `<BUS>` tag does.

        `audit_note` lands as `note` on this publish's audit line, whichever
        line it turns out to be. It exists for the one publish that is not
        conversation — the hop-8 warning — and it is a machine token, not
        prose.
        """
        # Applied to EVERY line this method can write, not only the `out` one.
        # A hop-8 warning that was refused before it left is the case an
        # operator most needs to find, and it is the case where the note would
        # be missing if it only rode the success path.
        note = {} if audit_note is None else {"note": audit_note}

        recipient = normalize_bot_name(to)
        if recipient is None or recipient not in self._config.allowed_from:
            self._audit.record(
                "drop",
                source_subject,
                reason="invalid_to",
                req_id=req_id,
                **note,
                **{"to": _audit_excerpt(to)},
            )
            return False

        # Chosen from the recipient this method NORMALISED and allowlisted,
        # before the envelope exists. Nothing built below may change who this
        # envelope claims to be from or to — see `create_request_envelope`.
        subject = f"fleet.{recipient}.request"
        try:
            envelope = create_request_envelope(
                self._config.bot_name,
                recipient,
                payload,
                kind=kind,
                in_reply_to=in_reply_to,
                baton=baton,
            )
        except ValueError as e:
            # A caller fault, not a wire fault — but this method is called from
            # a NATS callback and promises never to raise, so it becomes an
            # audited refusal. `yugo_publish_failed` is the existing code for
            # "the envelope never left"; no new one is invented for it.
            self._audit.record(
                "drop",
                subject,
                reason=REJECT_PUBLISH_FAILED,
                req_id=req_id,
                error=repr(e),
                **note,
            )
            return False
        result = validate_envelope(
            envelope, self._config.allowed_from, self._config.max_envelope_bytes
        )
        if not result.ok:
            self._audit.record(
                "drop",
                subject,
                reason=result.error,
                id=envelope["id"],
                req_id=req_id,
                **note,
            )
            return False

        if self._nc is None or self._nc.is_closed:
            self._audit.record(
                "drop",
                subject,
                reason=REJECT_PUBLISH_FAILED,
                id=envelope["id"],
                req_id=req_id,
                error="not connected",
                **note,
            )
            return False
        try:
            await self._nc.publish(
                subject, _canonical_json(envelope).encode("utf-8")
            )
        except Exception as e:  # noqa: BLE001 — one lost envelope, not a dead bus
            self._audit.record(
                "drop",
                subject,
                reason=REJECT_PUBLISH_FAILED,
                id=envelope["id"],
                req_id=req_id,
                error=repr(e),
                **note,
            )
            return False
        fields = {"to": recipient}
        if in_reply_to is not None:
            fields["in_reply_to"] = in_reply_to
        if "hops" in envelope:
            # The count as PUBLISHED. `hops` is the only baton field this
            # adapter changes, so it is the only one worth a column: without
            # it the audit log cannot answer "where did the chain get to",
            # which is the question the ceiling and the warning both exist for.
            fields["hops"] = envelope["hops"]
        self._audit.record(
            "out",
            subject,
            id=envelope["id"],
            kind=envelope["kind"],
            req_id=req_id,
            **note,
            **fields,
        )
        return True

    async def _on_message(self, msg) -> None:
        """Inbound path. v0.3b: decode → validate → audit → inject-or-drop.

        A raising callback does not kill the subscription in nats-py (the
        exception routes to `error_cb`), but it would still cost us the audit
        line, so every reject is an explicit return, not an exception.
        """
        try:
            decoded = _json_loads(msg.data)
        except ValueError:
            # All three decode faults are ValueError subclasses — bad syntax
            # (JSONDecodeError), non-UTF-8 bytes (UnicodeDecodeError) and a
            # non-finite constant (`_reject_json_constant`) — and `JSON.parse`
            # throws on each, so they share the one reject code.
            self._audit.record("drop", msg.subject, reason="malformed_json")
            return
        result = validate_envelope(
            decoded, self._config.allowed_from, self._config.max_envelope_bytes
        )
        if not result.ok:
            self._audit.record("drop", msg.subject, reason=result.error)
            return
        envelope = result.envelope
        if msg.subject == self.request_subject:
            # The directed lane owns its own audit lines: a `.request`
            # envelope is `in` only once it has been delivered somewhere, so
            # the record can carry the req_id the TS port puts on it, and a
            # recipient mismatch is a `drop` with no contradictory `in` line
            # in front of it.
            await self._on_request(msg.subject, envelope)
            return
        self._audit.record(
            "in",
            msg.subject,
            id=envelope["id"],
            kind=envelope["kind"],
            **{"from": envelope["from"]},
        )

    async def _on_request(self, subject: str, envelope: dict) -> None:
        """The directed lane: validate the recipient, then drive one turn.

        Port of `fleet-bus.ts`'s `onRequest`. Reached ONLY for
        `fleet.<self>.request` — the routing decision is in `_on_message`, and
        it is an equality test rather than a `.request` suffix test because
        `fleet.broadcast.>` is a subscribed wildcard, so `fleet.broadcast.
        request` is a subject any credentialed bot can publish. The three
        subjects that never arrive here, each for its own reason:

        * `fleet.broadcast.>` — SPEC §7.1, verbatim: broadcast payloads are
          "NEVER session-injected as LLM instruction content". They are
          structured signals; v0.4 exposes them through a `fleet_status` tool
          the LLM queries on demand. Injecting them is the
          compromise-one-bot-and-prompt-the-whole-fleet surface.
        * `fleet.<self>.status` — presence, not conversation, and `no_echo` is
          off by design (the heartbeat loopback is what makes publish →
          subscribe → validate → audit testable with no peer on the bus). An
          injectable `.status` would therefore hand our OWN heartbeat to the
          LLM once per interval, forever, at provider prices.
        * `fleet.<self>.result` — replies now travel as ordinary `.request`
          envelopes carrying `in_reply_to` (§7, `.result` removed in v5). The
          subject stays subscribed for backward-compat and un-injected.
        """
        # RECIPIENT. The subject is where the envelope was DELIVERED, not who
        # it was addressed to, and the two are independent: publish
        # permissions on the live bus are the wildcard `fleet.*.request`, so
        # any credentialed bot can drop an envelope addressed to someone else
        # onto our subject and — without this check — have it enter our
        # conversation. Parity with the TS port, which normalises `to` against
        # its own bot name and audits `recipient_mismatch` (added after the
        # v0.4.0 tag this module was ported from; see the PR discussion).
        #
        # A MISSING or null `to` fails the same way, deliberately. On a
        # directed subject the recipient is not optional — `bus_request(to,
        # kind, payload)` always sets it — so an envelope without one has no
        # stated recipient at all, and "no recipient" must not be the one
        # spelling that reaches every session on the bus.
        recipient = normalize_bot_name(envelope.get("to"))
        if recipient != self._config.bot_name:
            self._audit.record(
                "drop",
                subject,
                reason="recipient_mismatch",
                id=envelope["id"],
                **{"from": envelope["from"], "to": envelope.get("to")},
            )
            return

        # THE HOP CEILING (v0.3d). Sixteen, per `BATON-PROTOCOL-SPEC.md`, and
        # the check is `>=` because "reject at 16" makes 16 the first refused
        # value, not the last accepted one.
        #
        # Placed here — after the recipient check, before EVERYTHING else —
        # because a refused baton must cost nothing: no nonce, no turn, no
        # warning (a warning is itself a hop, and this chain has no hops left),
        # no auto-reply. It is not a malformed envelope; it is a well-formed
        # one that has travelled too far, so it does not borrow a §5 code.
        #
        # This is the FLEET-WIDE backstop and it does not replace either 3c
        # guard, which both stay. The baton spec's own reasoning about the cap
        # and the abandonment timeout — "Both, not either" — applies with more
        # force here: the 3c guards fire in ONE hop where this fires in
        # sixteen, and on the self-addressed-tag loop each turn emits both a
        # tag and an auto-reply, so the branch doubles per turn. Sixteen hops
        # of that is not sixteen turns.
        hops = envelope.get("hops")
        if isinstance(hops, int) and hops >= BATON_HOPS_REJECT_AT:
            self._audit.record(
                "drop",
                subject,
                reason=REJECT_HOPS_EXCEEDED,
                id=envelope["id"],
                hops=hops,
                **{"from": envelope["from"]},
            )
            return

        # Consumer-local nonce, like the TS port's `randomBytes(16)`. NOT the
        # envelope id: the nonce identifies what processing did with the
        # stable wire id and is persisted by the durable de-dup store. The
        # frame contract also requires `req_id !== envelope.id`. From 3c it is
        # also the correlation key: every envelope this turn publishes carries
        # it on its `out` line — and from 3d that includes the hop-8 warning,
        # which is why the nonce is minted BEFORE the session hook is checked
        # rather than inside the branch that has one.
        req_id = uuid.uuid4().hex
        try:
            duplicate, original_req_id, claim_owner = self._dedup.claim(envelope["id"], req_id)
        except Exception as e:  # store fault drops this delivery, subscription stays live
            self._audit.record("drop", subject, reason="yugo_dedup_store_failed", id=envelope["id"], error=repr(e))
            return
        if duplicate:
            self._audit.record(
                "drop", subject, reason="yugo_duplicate_envelope",
                id=envelope["id"], req_id=original_req_id,
            )
            return
        req_id = original_req_id

        if isinstance(hops, int) and hops >= BATON_HOPS_WARN_AT:
            await self._warn_origin(subject, envelope, req_id)

        if self._on_envelope is None:
            # No session configured — 3a's behaviour, and the behaviour of any
            # embedding that only wants the wire. Accepted and audited, with
            # no req_id: the nonce above exists for what a TURN publishes, and
            # there is no turn.
            self._audit.record(
                "in",
                subject,
                id=envelope["id"],
                kind=envelope["kind"],
                **{"from": envelope["from"]},
            )
            self._complete_claim(subject, envelope, req_id, claim_owner)
            return

        self._injection_delivered_ts = _utc_now_iso()
        # Renew while the turn runs. Without this a turn longer than the lease
        # is handed to a second consumer WHILE THE FIRST IS STILL EXECUTING —
        # the one duplication window that is actually closable here. The
        # post-effect/pre-commit window is not closable at this boundary and is
        # documented as at-least-once; see SPEC and yugo#24.
        renewer = asyncio.create_task(
            self._renew_claim_until_done(subject, envelope, req_id, claim_owner)
        )
        try:
            reply = await self._on_envelope(envelope, req_id)
        # CancelledError is a BaseException and deliberately NOT caught: a
        # turn interrupted by shutdown is not a failed injection, and
        # swallowing the cancel here would stall `drain()` behind an LLM call.
        except BaseException as e:
            await _cancel_task(renewer)
            self._release_claim(subject, envelope, req_id, claim_owner)
            if isinstance(e, asyncio.CancelledError):
                raise
            # Same reject code the TS peer writes when its `injectIntoSession`
            # rejects. The bus stays up and the subscription stays live: this
            # envelope is lost, the next one is not.
            self._audit.record(
                "drop",
                subject,
                reason="injection_failed",
                id=envelope["id"],
                req_id=req_id,
                error=repr(e),
            )
            return
        # The claim is NOT completed here. Completing before the reply is on
        # the wire means a crash in this window leaves a `completed` row with
        # no answer sent, and the redelivery is then suppressed for the full
        # TTL — turning the documented at-least-once duplicate into a
        # permanently LOST response, which is strictly worse. The lease stays
        # held (and renewed) across publishing, and completion happens after.
        self._session_last_response_ts = _utc_now_iso()
        # Success is `dir="in"` carrying envelope identity AND the nonce —
        # the shape the TS port records from `injectIntoSession`. The audit
        # schema has three traffic lanes (`in`/`out`/`drop`) plus `conn` for
        # lifecycle; a session turn does not get a fourth.
        self._audit.record(
            "in",
            subject,
            id=envelope["id"],
            kind=envelope["kind"],
            req_id=req_id,
            reply_chars=len(reply) if isinstance(reply, str) else None,
            **{"from": envelope["from"]},
        )
        # The turn is recorded before anything is published: the `in` line
        # says a turn HAPPENED, and it stays true whether or not the answer
        # made it onto the wire. There is no Discord path from here at all,
        # per §8 (a bus-triggered turn is a bus-only reply).
        try:
            await self._publish_turn_output(subject, envelope, req_id, reply)
        except Exception as e:  # noqa: BLE001 — the subscription outlives one turn
            # `publish_request` already absorbs every per-envelope fault, so
            # reaching here means a fault in the parse-and-dispatch code
            # itself. It still must not kill the callback: nats-py would route
            # the exception to `error_cb` and this bot would go on looking
            # healthy while every subsequent envelope died the same way.
            self._audit.record(
                "drop",
                subject,
                reason=REJECT_PUBLISH_FAILED,
                id=envelope["id"],
                req_id=req_id,
                error=repr(e),
            )
        finally:
            # Outbound processing is over, one way or the other: stop renewing
            # and settle the claim. A publish fault still completes — the turn
            # ran and its side effects happened, so replaying it would burn a
            # second model call for an answer the peer may already have.
            await _cancel_task(renewer)
            self._complete_claim(subject, envelope, req_id, claim_owner)

    def _complete_claim(self, subject: str, envelope: dict, req_id: str, owner: str) -> bool:
        """Promote a claim, surviving a store fault and reporting owner loss.

        Every durable operation on this path is contained per message. A
        SQLite fault here must not reject the handler and end the lane — that
        was the whole point of the claim-fault guard, and completion is the
        same class of risk.
        """
        try:
            won = self._dedup.complete(envelope["id"], owner)
        except Exception as e:
            self._audit.record(
                "drop", subject, reason="yugo_dedup_complete_failed",
                id=envelope["id"], req_id=req_id, error=repr(e),
            )
            return False
        if not won:
            # Not an error path for THIS delivery — the work is done. It does
            # mean another consumer holds the claim, so the caller must not
            # record this as the authoritative completion.
            self._audit.record(
                "drop", subject, reason="yugo_dedup_owner_lost",
                id=envelope["id"], req_id=req_id,
            )
        return won

    def _release_claim(self, subject: str, envelope: dict, req_id: str, owner: str) -> bool:
        try:
            return self._dedup.release(envelope["id"], owner)
        except Exception as e:
            self._audit.record(
                "drop", subject, reason="yugo_dedup_release_failed",
                id=envelope["id"], req_id=req_id, error=repr(e),
            )
            return False

    async def _renew_claim_until_done(
        self, subject: str, envelope: dict, req_id: str, owner: str,
    ) -> None:
        """Hold the lease for as long as this turn is actually running.

        Cadence comes off the event loop's MONOTONIC clock, so a wall-clock
        step cannot stretch or skip the interval. The stored deadline stays
        wall-clock because competing consumers compare it across processes.
        Cancelled by the caller on both exits.
        """
        # The STORE's lease, not the module default — they are the same by
        # default but a test or embedder can hand us a store with its own.
        interval = max(0.05, self._dedup.lease_s * DEDUP_LEASE_RENEW_RATIO)
        while True:
            await asyncio.sleep(interval)
            try:
                held = self._dedup.renew(envelope["id"], owner)
            except Exception as e:
                self._audit.record(
                    "drop", subject, reason="yugo_dedup_renew_failed",
                    id=envelope["id"], req_id=req_id, error=repr(e),
                )
                return
            if not held:
                # Lease taken by another consumer. Nothing to cancel the turn
                # with at this boundary; record it so the duplicate is visible
                # rather than silent.
                self._audit.record(
                    "drop", subject, reason="yugo_dedup_lease_lost",
                    id=envelope["id"], req_id=req_id,
                )
                return

    async def _warn_origin(self, subject: str, envelope: dict, req_id: str) -> None:
        """Tell `origin` that its baton has passed hop 8. v0.3d.

        `BATON-PROTOCOL-SPEC.md`: "The hop-8 warning is addressed to `origin`,
        not written to a log. A warning nobody reads is decoration, and it
        gives the originator a chance to kill the chain before 16." So this
        PUBLISHES. The audit line is a side effect of publishing, not the
        warning itself.

        Sent for EVERY received envelope at or above the threshold, not only
        the one that crosses it exactly. A threshold that fires on `== 8` is
        defeated by any peer that increments by two or skips a value, and
        "warn at 8" then silently becomes "warn never". The ceiling bounds the
        volume for us: a chain cannot produce more than eight of these.

        Shape decisions, each of which is a loop the naive version has:

        * **`kind` stays `text_message`.** The fleet-bus §3 baton kinds are a
          closed list of six and none of them is a warning; §3 does permit
          project-specific kinds, but inventing a fleet-wide lifecycle kind
          unilaterally is drift, and prose is what a warning is FOR. It lands
          in the originator's session as something a human or a model reads.
          Flagged with the PR for a fleet ruling.
        * **`in_reply_to` is set to the triggering envelope's id.** 3c omits
          it on `<BUS>` tag envelopes on purpose — "stamping it as a reply
          would both misattribute it and cost the third party their own
          auto-reply". Here that cost is the POINT and the trade-off resolves
          the other way: a warning does not want an answer, and acknowledging
          warnings is how you get the loop the warning is about. Against any
          adapter carrying 3c's reply-suppression this makes the warning
          terminal. Against one that has no such guard the ceiling is what
          bounds it, which is the ceiling's job.
        * **Never sent when `origin` is this bot.** We ARE the originator;
          telling ourselves means publishing onto `fleet.<self>.request`,
          which is the self-driving loop 3c's self-addressed-tag guard exists
          to stop — and that guard only covers the tag path, so it cannot see
          this one. It is also what stops the warning recursing: every
          participant addresses the SAME `origin`, so the recursion terminates
          at the first bot for which `origin` is itself.

        Known edge, stated rather than special-cased: the warning is a pass
        like any other, so one triggered at hop 15 goes out at 16 and a
        compliant `origin` refuses it. Exempting it from the increment would
        put a non-incrementing envelope on the wire, which is the whole defect
        this slice exists to remove. Warnings therefore land for received hop
        counts 8..14.
        """
        origin = envelope.get("origin")
        if not origin:
            self._audit.record(
                "drop",
                subject,
                reason=REJECT_WARNING_SUPPRESSED,
                id=envelope["id"],
                req_id=req_id,
                cause="no_origin",
                hops=envelope.get("hops"),
            )
            return
        if normalize_bot_name(origin) == self._config.bot_name:
            self._audit.record(
                "drop",
                subject,
                reason=REJECT_WARNING_SUPPRESSED,
                id=envelope["id"],
                req_id=req_id,
                cause="self_origin",
                hops=envelope.get("hops"),
            )
            return
        baton = next_baton_fields(envelope)
        await self.publish_request(
            origin,
            {
                "text": (
                    f"baton hop warning: root_id={envelope.get('root_id')} "
                    f"reached hop {baton['hops']} at {self._config.bot_name} "
                    f"(warn at {BATON_HOPS_WARN_AT}, refused at "
                    f"{BATON_HOPS_REJECT_AT}). Current owner: "
                    f"{envelope.get('owner')}. You started this chain; kill it "
                    "if it is not going anywhere."
                )
            },
            in_reply_to=envelope["id"],
            baton=baton,
            req_id=req_id,
            source_subject=subject,
            audit_note=AUDIT_NOTE_HOP_WARNING,
        )

    async def _publish_turn_output(
        self, subject: str, envelope: dict, req_id: str, reply: Any
    ) -> None:
        """Send a completed bus turn: third-party `<BUS>` tags, then the reply.

        TWO different outbound acts, and keeping them separate is the whole
        design:

        * every `<BUS to="…">…</BUS>` tag publishes a NEW envelope to that
          peer — fresh id, no `in_reply_to`. It is not an answer to anything;
          it is this bot addressing a third party, and stamping it as a reply
          would both misattribute it and (see the guard below) cost the third
          party their own auto-reply. THIRD party is enforced, not merely
          documented: a tag naming this bot is refused before publish, because
          the very field that makes it not-a-reply is the one the loop guard
          reads.
        * whatever text is left after the tags are stripped goes back to the
          SENDER as an ordinary `.request` envelope with `in_reply_to` set to
          the inbound envelope's id (§7). The model does not have to ask for
          this and cannot suppress it — a bus-triggered turn answers.

        **The loop guard: an envelope that carries `in_reply_to` is not
        auto-answered.** Auto-reply on both ends of a pair is a closed loop —
        A asks, B answers, A answers the answer, forever, at provider prices,
        and the fleet's own baton spec names this exact failure ("two agents
        acknowledging each other forever; that reaches 16 in under a second").
        The cap it names is the `hops` ceiling in `_on_request`, and 3d added
        it WITHOUT retiring this guard or the self-addressed-tag one. They are
        not alternatives: this fires in ONE hop where the ceiling fires in
        sixteen, and the self-tag loop branches — each turn emits both a tag
        and an auto-reply — so its cost doubles per turn rather than growing
        by one. Sixteen hops of a doubling branch is a bill, not a backstop.
        The baton spec reaches the same conclusion about its own two
        mechanisms: "Both, not either."

        Note no peer implements the other side of this — the reference TS
        adapter cannot publish at all (`request`/`publishReply` both throw
        `not implemented`; `artifice-ia/claude-discord` a9d605e,
        `src/fleet-bus.ts:259-267`), so there is no peer behaviour to match and
        yugo is the first adapter that can loop.

        **Baton fields ride on BOTH lanes (v0.3d)**, resolved once from the
        received envelope by `next_baton_fields`: same `root_id`, `origin` and
        `owner`, and `hops` one higher than we received. A tag publish and the
        auto-reply are two passes of the same baton, so they carry the same
        incremented count — the chain branches, and both branches are one hop
        further along than the envelope that caused them.

        Baton fields are deliberately NOT readable from `<BUS>` tag
        attributes. `_parse_tag_attrs` parses and ignores everything but `to`,
        and that stays true here: a model that could write `hops="0"` could
        reset the ceiling on every pass, which is a strictly worse version of
        the undercount this slice exists to fix, and one that a compromised
        or merely confused peer's payload could talk it into. This diverges
        from the sibling adapter design (`bazfer/fleet-bus` 820e4d8,
        `docs/CODEX-ADAPTER-DESIGN.md` §6 lines 596-616), which lifts baton
        fields — `hops` included — straight off tag attributes. That document
        is a design draft, not shipped code ("Status: design draft, not yet
        implemented"), and the divergence is flagged with the PR.

        Suppressions are audited rather than silent. "My bot received it, ran a
        turn and said nothing" is otherwise unexplainable from the log.
        """
        if not isinstance(reply, str):
            # The hook contract is `str | None`; None is a turn with nothing
            # to say. Anything else is a caller bug, not an envelope.
            return

        baton = next_baton_fields(envelope)
        tags = find_bus_tags(reply)
        for tag in tags:
            if tag.attrs is None:
                self._audit.record(
                    "drop",
                    subject,
                    reason=REJECT_TAG,
                    req_id=req_id,
                    cause=tag.cause,
                    raw=_audit_excerpt(reply[tag.start : tag.end]),
                )
                continue
            body = (tag.body or "").strip()
            if not body:
                self._audit.record(
                    "drop",
                    subject,
                    reason=REJECT_TAG,
                    req_id=req_id,
                    cause="empty_body",
                    raw=_audit_excerpt(reply[tag.start : tag.end]),
                )
                continue
            # A tag MAY NOT address this bot. The grammar says third party,
            # and here that stops being documentation: our own name is in the
            # manifest, so `<BUS to="<self>">` would otherwise publish onto
            # `fleet.<self>.request` — our own subscribed subject — as a fresh
            # envelope with NO `in_reply_to`. That is precisely the field the
            # loop guard below keys on, and tag envelopes omit it BY DESIGN,
            # so the suppression cannot cover this path at all: the bot would
            # drive itself, one model turn per hop, for as long as the model
            # kept repeating the tag.
            #
            # Refused HERE and not in `publish_request`, deliberately. A bot
            # sending itself an envelope is only nonsense in the tag grammar;
            # the publish API is the general one (self-addressed probes,
            # future scheduled work), and banning it there would be a policy
            # written into the wrong layer.
            #
            # Normalised before the comparison, or `<BUS to="YUGO">` and
            # `<BUS to="ｙｕｇｏ">` walk straight past a guard that only
            # rejects the exact spelling.
            if normalize_bot_name(tag.attrs.get("to")) == self._config.bot_name:
                self._audit.record(
                    "drop",
                    subject,
                    reason=REJECT_TAG,
                    req_id=req_id,
                    cause="self_addressed",
                    raw=_audit_excerpt(reply[tag.start : tag.end]),
                )
                continue
            await self.publish_request(
                tag.attrs.get("to"),
                {"text": body},
                baton=baton,
                req_id=req_id,
                source_subject=subject,
            )

        if envelope.get("in_reply_to") is not None:
            self._audit.record(
                "drop",
                subject,
                reason=REJECT_AUTOREPLY_SUPPRESSED,
                id=envelope["id"],
                req_id=req_id,
                cause="in_reply_to",
            )
            return

        answer = strip_bus_tags(reply, tags).strip()
        if not answer:
            # A turn whose entire output was tags has already said everything
            # it had to say, to the peers it named.
            self._audit.record(
                "drop",
                subject,
                reason=REJECT_AUTOREPLY_SUPPRESSED,
                id=envelope["id"],
                req_id=req_id,
                cause="empty_reply",
            )
            return
        await self.publish_request(
            envelope["from"],
            {"text": answer},
            in_reply_to=envelope["id"],
            baton=baton,
            req_id=req_id,
            source_subject=subject,
        )

    async def _teardown(self) -> None:
        nats = _lazy_nats()
        nc, self._nc = self._nc, None
        if nc is None or nc.is_closed:
            return
        try:
            await nc.drain()
        except nats.errors.ConnectionReconnectingError:
            # drain() refuses mid-reconnect; close() is the only way out and
            # there is nothing to flush anyway — the socket is already gone.
            await nc.close()
        except Exception as e:  # noqa: BLE001 — shutdown must not raise
            self._audit.record("conn", None, event="drain_failed", error=repr(e))
            try:
                await nc.close()
            except Exception:  # noqa: BLE001
                pass

    # --- nats callbacks ---
    #
    # NOTHING here re-subscribes. nats-py replays every subscription itself on
    # reconnect (it rewrites the SUB commands from `_subs`), so a re-subscribe
    # in `_on_reconnected` would double-deliver every inbound envelope for the
    # rest of the process's life. See test_source_scan.

    async def _on_error(self, e) -> None:
        # Bad credentials are a CONFIG fault the operator has to fix, but
        # failing closed on them would be worse than the noise: a token
        # rotated a beat early would take the bot off Discord too. So it keeps
        # retrying forever — and at the production `reconnect_time_wait` of 5s
        # that is ~10⁴ audit lines a day into an unrotated file. Giving the
        # authz case its OWN event is what makes it greppable, and what stops
        # "the bus is loud" from meaning "read 10,000 identical error lines to
        # find out why".
        event = "auth_rejected" if _is_authorization_failure(e) else "error"
        self._audit.record("conn", None, event=event, error=repr(e))

    async def _on_disconnected(self) -> None:
        self._audit.record("conn", None, event="disconnected")

    async def _on_reconnected(self) -> None:
        self._audit.record("conn", None, event="reconnected")

    async def _on_closed(self) -> None:
        # Tripwire. With max_reconnect_attempts=-1 this should be reachable
        # only from our own shutdown path. If it shows up in an audit log
        # without a shutdown next to it, nats-py abandoned the server and the
        # bot is bus-less until restart.
        self._audit.record(
            "conn",
            None,
            event="closed",
            note="nats connection CLOSED — no further reconnect will be attempted",
        )

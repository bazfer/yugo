"""
Wire-contract tests for fleet_bus: identity folding, envelope validation,
heartbeat shape, audit-record shape.

These are the pure functions — no NATS, no loop. The connection lifecycle is
in test_fleet_bus_lifecycle.py (real nats-server) and the bot.py wiring is in
test_fleet_bus_wiring.py.

Why this file exists as its own thing: the reject-code vocabulary is a
CROSS-LANGUAGE contract. The tap and (from v0.6) the coordinator index on
these exact strings, and the TypeScript adapter
(`artifice-discord/src/fleet-bus.ts`) is the other end of it. A renamed code
is not a refactor, it is a silent observability regression on every dashboard
that counts drops by reason.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import fleet_bus


# A lone surrogate, built at RUNTIME rather than written as a source literal.
# Python 3.13 refuses to marshal a code object holding one, so `"\ud800"` in
# this file would compile under CI's 3.12 and fail at import inside the
# container, which runs 3.13 — a green test suite behind a container that
# cannot start. `chr()` keeps the source plain ASCII.
_LONE_SURROGATE = chr(0xD800)


ALLOWED = frozenset({"yugo", "vec", "ohm"})


def _envelope(**overrides) -> dict:
    """A minimal VALID v1 envelope. Every rejection test mutates one field."""
    base = {
        "envelope_version": 1,
        "id": "abc-123",
        "from": "vec",
        "to": "yugo",
        "kind": "text_message",
        "ts": "2026-08-26T12:00:00.000Z",
        "payload": {"text": "hi"},
    }
    base.update(overrides)
    return base


# ---------- identity folding ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("vec", "vec"),
        ("VEC", "vec"),
        ("deet-01", "deet-01"),
        ("deet_01", "deet_01"),
        # NFKC folds fullwidth + Kelvin-sign lookalikes onto their ASCII form,
        # so a homoglyph `from` claim cannot masquerade as a second identity.
        ("ＶＥＣ", "vec"),
        ("Åat", None),  # Angstrom sign folds to 'Å', still not [a-z0-9_-]
        ("has space", None),
        ("has.dot", None),
        ("", None),
        ("UPPER.CASE", None),
        ("broadcast", None),
        (None, None),
        (5, None),
        (["vec"], None),
    ],
)
def test_normalize_bot_name(raw, expected):
    assert fleet_bus.normalize_bot_name(raw) == expected


def test_normalize_allowlist_raises_on_invalid_entry():
    """A typo'd manifest entry must be LOUD. Skipping it silently would drop
    that bot's envelopes as `from_claim_rejected`, which reads like a peer
    fault rather than a one-character config fault."""
    with pytest.raises(ValueError, match="Invalid fleet bot name"):
        fleet_bus.normalize_allowlist(["vec", "not a name"])


# ---------- manifest ----------


def _write_manifest(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fleet-manifest.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_manifest_allowlist_normalizes(tmp_path):
    path = _write_manifest(tmp_path, "bot_names:\n  - VEC\n  - yugo\n")
    assert fleet_bus.load_fleet_manifest_allowlist(path) == {"vec", "yugo"}


@pytest.mark.parametrize(
    "body,match",
    [
        ("bot_names: []\n", "non-empty list"),
        ("bot_names:\n", "non-empty list"),
        ("bot_names: vec\n", "non-empty list"),
        ("- vec\n- yugo\n", "YAML mapping"),
        ("just a string\n", "YAML mapping"),
    ],
)
def test_manifest_shapes_that_must_raise(tmp_path, body, match):
    """An empty or wrong-shaped manifest must raise, never yield an empty
    allowlist. An empty allowlist rejects EVERY envelope including our own
    heartbeat loopback — a bot that looks healthy and receives nothing."""
    path = _write_manifest(tmp_path, body)
    with pytest.raises(ValueError, match=match):
        fleet_bus.load_fleet_manifest_allowlist(path)


# ---------- envelope validation: the reject-code vocabulary ----------
#
# Table-driven across EVERY code in the TS port, not a sample. The companion
# `test_no_reject_code_enters_fleet_bus_without_a_row_in_the_table_above`
# below pins the set itself, so adding a code without a row here (or
# dropping one) fails.

REJECT_CASES = {
    "envelope_not_object": [None, "a string", 5, ["not", "a", "map"]],
    "unsupported_envelope_version": [
        _envelope(envelope_version=2),
        _envelope(envelope_version=0),
        _envelope(envelope_version="1"),
        # `True == 1` in Python, so a plain `!= 1` ACCEPTS this envelope while
        # the TS port rejects it (`true !== 1`) — the one divergence direction
        # the module docstring forbids, because the accepting side processes an
        # envelope its peer already dropped and neither logs a reason.
        _envelope(envelope_version=True),
        {k: v for k, v in _envelope().items() if k != "envelope_version"},
    ],
    "invalid_id": [
        _envelope(id=""),
        _envelope(id=None),
        _envelope(id=7),
        {k: v for k, v in _envelope().items() if k != "id"},
    ],
    "invalid_kind": [
        _envelope(kind=""),
        _envelope(kind=None),
        _envelope(kind={"a": 1}),
        {k: v for k, v in _envelope().items() if k != "kind"},
    ],
    "invalid_ts": [
        _envelope(ts=""),
        _envelope(ts="not-a-date"),
        _envelope(ts=1756209600),
        # Basic (`20260826T120000`) and week-date (`2026W351`) ISO forms:
        # `datetime.fromisoformat` parses both, `Date.parse` answers NaN for
        # both. Accepting them here is Python being LOOSER than the port.
        _envelope(ts="20260826T120000"),
        _envelope(ts="2026W351"),
        _envelope(ts="20260826"),
        # A date alone and a time without an offset are parseable by Python,
        # but the fleet contract requires both a time and an explicit offset.
        _envelope(ts="2026-08-26"),
        _envelope(ts="2026-08-26T12:34:56"),
        # A UTC offset with a minute field of 60. `fromisoformat` does not
        # reject it, it NORMALISES it (`+01:60` -> `+02:00`), so a gate that
        # trusted the parse accepted a timestamp `Date.parse` answers with
        # NaN. All four punctuation/sign spellings, because the regex admits
        # the colon-less form too and fixing only the one that was reported
        # would leave three doors open.
        _envelope(ts="2026-08-26T12:34:56+01:60"),
        _envelope(ts="2026-08-26T12:34:56-01:60"),
        _envelope(ts="2026-08-26T12:34:56+0160"),
        _envelope(ts="2026-08-26T12:34:56-0160"),
        {k: v for k, v in _envelope().items() if k != "ts"},
    ],
    "missing_payload": [{k: v for k, v in _envelope().items() if k != "payload"}],
    "invalid_to": [_envelope(to=5), _envelope(to=["yugo"])],
    "invalid_in_reply_to": [
        _envelope(in_reply_to=5),
        # Present-but-null is NOT the same as absent: TS checks `!== undefined`,
        # so a JSON `null` reaches the typeof test and fails there too.
        _envelope(in_reply_to=None),
    ],
    "from_claim_rejected": [
        _envelope(**{"from": "stranger"}),
        _envelope(**{"from": ""}),
        _envelope(**{"from": None}),
        _envelope(**{"from": "has space"}),
        {k: v for k, v in _envelope().items() if k != "from"},
    ],
    "payload_not_serializable": [
        _envelope(payload=object()),
        # Non-finite floats. Python's encoder spells them `NaN`/`Infinity`,
        # which is not JSON and which nothing but Python's own decoder reads
        # back — so an envelope carrying one was validated as GOOD here and
        # audited as inbound. Nested cases are here because the check has to
        # be the whole-envelope encode, not an `isinstance` on `payload`.
        _envelope(payload=float("nan")),
        _envelope(payload=float("inf")),
        _envelope(payload=float("-inf")),
        _envelope(payload={"reading": float("nan")}),
        _envelope(payload=[1, 2, float("inf")]),
        _envelope(payload={"a": {"b": [{"c": float("-inf")}]}}),
    ],
    "envelope_too_large": [
        _envelope(payload={"text": "x" * fleet_bus.DEFAULT_MAX_ENVELOPE_BYTES})
    ],
}


@pytest.mark.parametrize(
    "expected_code,candidate",
    [(code, c) for code, cases in REJECT_CASES.items() for c in cases],
    ids=[
        f"{code}-{i}"
        for code, cases in REJECT_CASES.items()
        for i in range(len(cases))
    ],
)
def test_validate_envelope_reject_codes(expected_code, candidate):
    result = fleet_bus.validate_envelope(candidate, ALLOWED)
    assert result.ok is False
    assert result.envelope is None
    assert result.error == expected_code


def test_no_reject_code_enters_fleet_bus_without_a_row_in_the_table_above():
    """Class-mechanism guard: the SET of reject codes is the contract.

    The table above proves each code fires for the inputs we thought of. This
    proves nobody added a twelfth code, renamed one, or deleted one without
    the table noticing — the failure mode a per-code test cannot see.

    Scope, stated honestly: this reads `fleet_bus.py` and compares it against
    the table in THIS file. It never opens `fleet-bus.ts`, so it cannot see
    drift that happens on the TypeScript side — a code renamed there stays
    green here. It was named `..._matches_ts_port`, which claimed exactly the
    check it does not perform. The cross-language pin is the vendored-schema
    hash gate in slice 3e; a test that parsed the plugin's `.ts` out of a
    developer's plugin cache would skip in CI, where the file does not exist,
    and a silently-skipped contract test reads identically to a passing one.
    """
    # Three codes the VALIDATOR never returns, so none has a row in the
    # table — all three belong to the delivery path, and all three exist on
    # the TypeScript side:
    #   `malformed_json`    — `onRequest`'s decode `catch`
    #   `injection_failed`  — `injectIntoSession(...).catch(...)`
    #   `recipient_mismatch`— `envelope.to` vs local bot name, added to the
    #                         plugin after the 0.4.0 tag this module was
    #                         ported from (fleet-bus-plugin-integration.md
    #                         changelog, 2026-08-24 v0.8: "Ohm caught envelope-
    #                         recipient validation gap on PR #20 ... Vec fixed
    #                         at 8dec624, direct requests now reject/audit
    #                         mismatches as `recipient_mismatch`").
    expected = set(REJECT_CASES) | {
        "malformed_json",
        "injection_failed",
        "recipient_mismatch",
    }
    source = Path(fleet_bus.__file__).read_text(encoding="utf-8")
    found = set(re.findall(r'error="([a-z_]+)"', source))
    found |= set(re.findall(r'reason="([a-z_]+)"', source))
    assert found == expected, (
        "the reject codes in fleet_bus.py no longer match the table in this "
        "file. Both sides are a port of fleet-bus.ts `validateEnvelope` + "
        "`onRequest`, so a code that belongs here needs a row above AND the "
        "same string on the TypeScript side. "
        f"missing={expected - found} unexpected={found - expected}"
    )


@pytest.mark.parametrize(
    "candidate",
    [
        _envelope(),
        _envelope(to=None),
        {k: v for k, v in _envelope().items() if k != "to"},
        _envelope(in_reply_to="root-1"),
        _envelope(**{"from": "VEC"}),
        _envelope(ts="2026-08-26T12:00:00+00:00"),
        # The extended-ISO gate in front of `fromisoformat` must not tighten
        # past `Date.parse`: these are shapes the TS port accepts.
        _envelope(ts="2026-08-26T12:00:00Z"),
        _envelope(ts="2026-08-26T12:00:00-05:00"),
        _envelope(payload=None),
        _envelope(payload=[]),
        # Exactly at the byte bound: `>` not `>=`, same as the TS port.
        _envelope(
            payload={"text": "x" * (fleet_bus.DEFAULT_MAX_ENVELOPE_BYTES - 200)}
        ),
    ],
)
def test_validate_envelope_accepts(candidate):
    result = fleet_bus.validate_envelope(candidate, ALLOWED)
    assert result.ok is True, f"unexpected reject: {result.error}"
    assert result.envelope is not None


# ---------- timestamp gate ----------
#
# The rows in REJECT_CASES / test_validate_envelope_accepts pin the INSTANCES.
# The three below pin the CLASS: the reason a timestamp is allowed to be
# delegated to `fromisoformat` at all is that `fromisoformat` REJECTS what it
# cannot represent. Wherever it silently rewrites instead, delegation accepts
# a string `Date.parse` calls NaN — the divergence direction the module
# docstring forbids. UTC offset minutes were the one place that was true.

_TS_FIELDS = re.compile(
    r"(?P<Y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"(?:[T ](?P<h>\d{2}):(?P<mi>\d{2})(?::(?P<s>\d{2})(?:\.\d+)?)?"
    r"(?:Z|(?P<sign>[+-])(?P<ohh>\d{2}):?(?P<omm>\d{2}))?)?"
)


def _swept_timestamps():
    """Every offset spelling the module's regex can admit, plus the shapes it
    is crossed with on the wire. Written out HERE rather than derived from
    `fleet_bus._ISO_EXTENDED_TS_PATTERN`, so a loosened pattern cannot loosen
    the sweep along with itself."""
    for sign in "+-":
        for ohh in range(100):
            for omm in range(100):
                for colon in (":", ""):
                    yield f"2026-08-26T12:34:56{sign}{ohh:02d}{colon}{omm:02d}"
    for tail in ("", "Z", "+00:00", "-05:00", "+0530", "+23:59", "+01:60", "-0175"):
        for body in (
            "2026-08-26T12:34",
            "2026-08-26T12:34:56",
            "2026-08-26T12:34:56.789",
            "2026-08-26 12:34:56",
        ):
            yield body + tail
    for value in range(100):
        yield f"2026-{value:02d}-15T12:34:56Z"
        yield f"2026-08-{value:02d}T12:34:56Z"
        yield f"2026-08-26T{value:02d}:34:56Z"
        yield f"2026-08-26T12:{value:02d}:56Z"
        yield f"2026-08-26T12:34:{value:02d}Z"


def test_no_accepted_timestamp_was_silently_rewritten_by_the_parse():
    """Class mechanism: acceptance must never come from a NORMALISING parse.

    For every timestamp the gate accepts, each numeric field read straight out
    of the STRING must equal the corresponding field of the parsed datetime.
    `fromisoformat("...+01:60")` succeeds and hands back `+02:00`; from the
    outside that is indistinguishable from a clean parse, which is exactly how
    a `Date.parse`-NaN string became a valid envelope here. Any future field
    where the parse rewrites rather than rejects fails this, whatever the
    regex happens to say.

    Scope, stated honestly: this proves the gate never accepts on the back of
    a rewrite. It does NOT compare against `Date.parse` — that needs node, and
    a contract test that skips where node is missing reads identically to a
    passing one. The `Date.parse` differential is run by hand against the same
    corpus and reported on the PR.
    """
    checked = 0
    for value in _swept_timestamps():
        if not fleet_bus._is_parseable_ts(value):
            continue
        checked += 1
        fields = _TS_FIELDS.fullmatch(value)
        assert fields is not None, f"accepted a shape this test cannot read: {value}"
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        assert (parsed.year, parsed.month, parsed.day) == (
            int(fields["Y"]),
            int(fields["mo"]),
            int(fields["d"]),
        ), f"date rewritten by the parse: {value} -> {parsed.isoformat()}"
        if fields["h"] is not None:
            assert (parsed.hour, parsed.minute) == (
                int(fields["h"]),
                int(fields["mi"]),
            ), f"time rewritten by the parse: {value} -> {parsed.isoformat()}"
            assert parsed.second == int(fields["s"] or 0)
        if fields["sign"] is not None:
            offset_minutes = parsed.utcoffset() // timedelta(minutes=1)
            sign = -1 if fields["sign"] == "-" else 1
            assert divmod(abs(offset_minutes), 60) == (
                int(fields["ohh"]),
                int(fields["omm"]),
            ) and (offset_minutes == 0 or (offset_minutes > 0) == (sign > 0)), (
                f"UTC offset rewritten by the parse: {value} -> "
                f"{parsed.isoformat()}"
            )
    # Guard the guard: a sweep that accepted nothing would pass vacuously.
    assert checked > 2000, f"only {checked} timestamps accepted; sweep went stale"


@pytest.mark.parametrize("sign", ["+", "-"])
@pytest.mark.parametrize("colon", [":", ""])
def test_utc_offset_minutes_outside_00_59_are_rejected(sign, colon):
    """The named instance, swept over its whole range and both spellings.

    Discriminating: this is the half of the offset range `fromisoformat` does
    NOT enforce, so it fails the moment the pattern's `[0-5]\\d` is relaxed
    back to `\\d{2}`.
    """
    for minute in range(100):
        value = f"2026-08-26T12:34:56{sign}01{colon}{minute:02d}"
        assert fleet_bus._is_parseable_ts(value) is (minute <= 59), value


@pytest.mark.parametrize("sign", ["+", "-"])
@pytest.mark.parametrize("colon", [":", ""])
def test_utc_offset_hours_outside_00_23_are_rejected_by_the_pattern(sign, colon):
    """Defence in depth, asserted at the layer that is actually load-bearing.

    Hours 24+ are already rejected downstream — `fromisoformat` requires an
    offset strictly under 24h — so a behavioural `_is_parseable_ts` assertion
    would stay green with the pattern's hour clamp deleted, i.e. it could not
    fail for the reason it names. This asserts the PATTERN instead, which is
    where the clamp lives and where a relaxation is visible.
    """
    for hour in range(100):
        value = f"2026-08-26T12:34:56{sign}{hour:02d}{colon}30"
        matched = fleet_bus._ISO_EXTENDED_TS_PATTERN.fullmatch(value) is not None
        assert matched is (hour <= 23), value


def test_canonical_json_matches_json_stringify_byte_shape():
    """Asserted against a LITERAL, not against `_canonical_json` itself.

    Measuring the encoder with the encoder is the trap: a version that
    reintroduced Python's default `", "` / `": "` padding (or `ensure_ascii`)
    would keep every self-referential size assertion green while making this
    adapter disagree with the TypeScript one about which envelopes are legal.
    """
    value = {"a": 1, "b": "x", "c": None, "d": [1, 2], "e": "ñ", "f": True}
    assert fleet_bus._canonical_json(value) == '{"a":1,"b":"x","c":null,"d":[1,2],"e":"ñ","f":true}'


# ---------- non-finite numbers ----------
#
# `NaN`, `Infinity` and `-Infinity` are not JSON. Python's codec is alone in
# both accepting them on decode and emitting them on encode; `JSON.parse`
# throws on all three. Left alone, the same bytes are a live envelope here and
# a `malformed_json` drop on the TS side — the divergence direction the module
# docstring forbids. Both directions are pinned, at BOTH entry points — the wire
# path in test_fleet_bus_lifecycle, and `validate_envelope` here, because the
# validator is exported and used independently of the decoder.


def _strict_loads(raw: str | bytes):
    """`json.loads` restricted to real JSON, written out here rather than
    borrowed from `fleet_bus` — a decoder measured with itself would follow
    the implementation wherever it went."""

    def reject(name):
        raise AssertionError(f"not JSON: bare {name} literal")

    return json.loads(raw, parse_constant=reject)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"payload":NaN}',
        b'{"payload":Infinity}',
        b'{"payload":-Infinity}',
        # Nested at depth, and inside an array — `parse_constant` fires
        # wherever the literal appears, which a top-level-only guard would not.
        b'{"payload":{"reading":NaN}}',
        b'{"payload":[1,2,Infinity]}',
        b'{"payload":{"a":{"b":[{"c":-Infinity}]}}}',
    ],
)
def test_json_decode_refuses_the_constants_json_parse_throws_on(raw):
    with pytest.raises(ValueError):
        fleet_bus._json_loads(raw)


def test_json_decode_still_accepts_ordinary_json():
    """Control. Without it the test above is satisfied by a decoder that
    rejects everything."""
    assert fleet_bus._json_loads(b'{"payload":{"a":[1,2.5,null,true],"b":"x"}}') == {
        "payload": {"a": [1, 2.5, None, True], "b": "x"}
    }


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        {"reading": float("nan")},
        [1, 2, float("inf")],
        {"a": {"b": [{"c": float("-inf")}]}},
        # A non-finite used as a mapping KEY: Python coerces it to the string
        # "NaN" on the way out, which is a different code path from the value
        # encoder.
        {float("nan"): 1},
    ],
)
def test_canonical_json_refuses_to_emit_a_non_finite(value):
    with pytest.raises(ValueError):
        fleet_bus._canonical_json(value)


def test_audit_stream_stays_parseable_when_a_field_cannot_be_encoded(tmp_path):
    """The guarantee at the sink: the log is JSONL, so one line spelling `NaN`
    breaks every consumer that reads the file from then on.

    Scope, stated honestly: no v0.3a call site passes a non-finite — the
    inbound record carries `id`/`kind`/`from`, all string-validated — so this
    is not reachable from today's wire. It becomes reachable at 3b, when
    payload content starts crossing into audit surfaces. The pin is here now
    because `record()` is the single choke point every future caller goes
    through.

    An unencodable field degrades to a valid-JSON stand-in that keeps
    ts/dir/subject. It is neither written raw nor dropped: losing the event
    would hide the fault, and the bus must not go down over an audit field
    (same rule as an unwritable path).
    """
    path = tmp_path / "audit.jsonl"
    audit = fleet_bus.AuditLog(str(path))
    audit.record("in", "fleet.yugo.request", id="ok", kind="text_message")
    audit.record("in", "fleet.yugo.request", id="bad", reading=float("nan"))
    audit.record("in", "fleet.yugo.request", id="after", kind="text_message")

    raw_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 3, "the unencodable record must not vanish"
    lines = [_strict_loads(raw) for raw in raw_lines]
    for line in lines:
        assert {"ts", "dir", "subject"} <= set(line), line
        assert line["dir"] == "in"
        assert line["subject"] == "fleet.yugo.request"
    assert lines[1]["event"] == "audit_encode_failed"
    # The sink keeps working afterwards.
    assert lines[2]["id"] == "after"


def test_a_lone_surrogate_does_not_take_the_audit_writer_down(tmp_path):
    """Same choke point, a different member of the same class — and the one
    that needed no unusual input at all.

    `_canonical_json` runs `ensure_ascii=False`, so a lone surrogate (which
    `json.loads` builds from an ordinary `"\\ud800"` escape, plain ASCII on
    the wire) produces a `str` without complaint and fails only at
    `.encode("utf-8")` — as a `UnicodeEncodeError`, which is a `ValueError`
    and NOT an `OSError`. With the encode down at the write it walked past the
    `except OSError`, out of `record`, and on the inbound lane out of a NATS
    subscription callback, leaving the audit file created and EMPTY.

    `validate_envelope` already rejects such an envelope as
    `payload_not_serializable`, so the wire path is guarded; `record` is not
    only reached from there, and an audit writer that can take down the thing
    it audits is the defect regardless of which caller reaches it first.

    Bite-check: move the encode back below the `except` and this raises
    `UnicodeEncodeError` with a zero-byte file behind it.
    """
    path = tmp_path / "audit.jsonl"
    audit = fleet_bus.AuditLog(str(path))
    audit.record("in", "fleet.yugo.request", payload=json.loads('{"t": "\\ud800"}'))
    audit.record("in", "fleet.yugo.request", id="after", kind="text_message")

    raw_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 2, "the unwritable record must not vanish"
    lines = [_strict_loads(raw) for raw in raw_lines]
    assert lines[0]["event"] == "audit_encode_failed"
    assert {"ts", "dir", "subject"} <= set(lines[0])
    assert lines[1]["id"] == "after", "the sink keeps working afterwards"


def test_a_surrogate_in_the_subject_still_writes_a_parseable_line(tmp_path):
    """`subject` is peer-supplied, and the stand-in used to copy it from the
    raw entry — re-importing the exact value that forced the fallback.

    Bite-check: pass `subject` through uncoerced and this raises.
    """
    path = tmp_path / "audit.jsonl"
    fleet_bus.AuditLog(str(path)).record("in", _LONE_SURROGATE, reading=float("nan"))

    (line,) = [_strict_loads(raw) for raw in path.read_text(encoding="utf-8").splitlines()]
    assert line["event"] == "audit_encode_failed"
    assert line["subject"] == repr(_LONE_SURROGATE)


def test_a_pathless_bus_audit_survives_a_surrogate(capsys):
    """The logger branch is the other exit: printing a lone surrogate to a
    UTF-8 stream raises the same way writing it does.
    """
    fleet_bus.AuditLog(None).record(
        "in", "fleet.yugo.request", payload=json.loads('{"t": "\\ud800"}')
    )
    assert "audit_encode_failed" in capsys.readouterr().out


def test_size_bound_is_exact_and_measured_in_canonical_bytes():
    """Boundary pin: an envelope of exactly max_bytes passes, +1 fails.

    The target size is computed with a `json.dumps` call written out HERE, not
    by calling `fleet_bus._canonical_json` — otherwise the test would follow
    the implementation wherever it went (see the mutation table: that is
    exactly how an earlier revision of this test survived a separators
    mutation).
    """
    def stringify_bytes(value) -> int:
        return len(
            json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )

    overhead = stringify_bytes(_envelope(payload={"text": ""}))
    exact = _envelope(payload={"text": "y" * (500 - overhead)})
    assert stringify_bytes(exact) == 500
    assert fleet_bus.validate_envelope(exact, ALLOWED, max_bytes=500).ok is True
    assert fleet_bus.validate_envelope(exact, ALLOWED, max_bytes=499).error == (
        "envelope_too_large"
    )


@pytest.mark.parametrize(
    "candidate,expected_code",
    [
        # Each of these violates SEVERAL rules at once. The code returned must
        # be the FIRST one in the TypeScript check order, because that is what
        # the tap's drop-reason counters were calibrated against — a reordered
        # port silently reclassifies a whole population of drops.
        ({"envelope_version": 2, "from": "stranger"}, "unsupported_envelope_version"),
        (_envelope(id="", kind="", ts="nope"), "invalid_id"),
        (_envelope(kind="", ts="nope", **{"from": "stranger"}), "invalid_kind"),
        (_envelope(ts="nope", **{"from": "stranger"}), "invalid_ts"),
        (
            {k: v for k, v in _envelope(**{"from": "stranger"}).items() if k != "payload"},
            "missing_payload",
        ),
        (_envelope(to=5, in_reply_to=5, **{"from": "stranger"}), "invalid_to"),
        (_envelope(in_reply_to=5, **{"from": "stranger"}), "invalid_in_reply_to"),
        (
            _envelope(
                payload={"t": "x" * fleet_bus.DEFAULT_MAX_ENVELOPE_BYTES},
                **{"from": "stranger"},
            ),
            "from_claim_rejected",
        ),
    ],
)
def test_reject_code_precedence_matches_the_ts_check_order(candidate, expected_code):
    assert fleet_bus.validate_envelope(candidate, ALLOWED).error == expected_code


def test_validated_envelope_carries_normalized_from():
    """The handler must see the folded identity, not the raw claim — otherwise
    downstream comparisons against manifest names miss on case alone."""
    result = fleet_bus.validate_envelope(_envelope(**{"from": "VEC"}), ALLOWED)
    assert result.envelope["from"] == "vec"


def test_validate_envelope_does_not_mutate_input():
    candidate = _envelope(**{"from": "VEC"})
    fleet_bus.validate_envelope(candidate, ALLOWED)
    assert candidate["from"] == "VEC"


# ---------- heartbeat ----------


def test_heartbeat_envelope_shape():
    envelope = fleet_bus.create_heartbeat_envelope("yugo", "0.3a", pid=4242)
    assert envelope["envelope_version"] == 1
    assert envelope["from"] == "yugo"
    assert envelope["to"] is None
    assert envelope["kind"] == "status_heartbeat"
    payload = envelope["payload"]
    # The four fields the tap/coordinator actually read. Subset assertion, so
    # 3b adding session fields does not break this.
    assert payload["online"] is True
    assert payload["pid"] == 4242
    assert payload["plugin_version"] == "0.3a"
    assert payload["process_alive_ts"] == envelope["ts"]
    # Present-and-null at 3a: there is no session to report until 3b.
    assert payload["session_last_response_ts"] is None
    assert payload["injection_delivered_ts"] is None


def test_heartbeat_envelope_validates_against_own_allowlist():
    """The heartbeat travels the same validation path as any peer envelope
    (loopback on `fleet.<self>.status`). If it did not validate, the adapter
    would drop its own beat and the end-to-end proof would be vacuous."""
    envelope = fleet_bus.create_heartbeat_envelope("yugo", "0.3a")
    assert fleet_bus.validate_envelope(envelope, ALLOWED).ok is True


def test_heartbeat_ids_are_unique():
    ids = {fleet_bus.create_heartbeat_envelope("yugo", "0.3a")["id"] for _ in range(50)}
    assert len(ids) == 50


def test_heartbeat_ts_is_js_toisostring_shape():
    """Millisecond precision + literal `Z`, matching TS `toISOString()`.
    `datetime.isoformat()` emits `+00:00` and 0-or-6 fractional digits, which
    the TS side's `Date.parse` tolerates but the tap's string compares do not."""
    envelope = fleet_bus.create_heartbeat_envelope("yugo", "0.3a")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", envelope["ts"])


def test_heartbeat_rejects_invalid_bot_name():
    with pytest.raises(ValueError, match="Invalid heartbeat bot name"):
        fleet_bus.create_heartbeat_envelope("not a name", "0.3a")


# ---------- audit log ----------


def test_audit_file_is_0600_even_under_a_permissive_umask(tmp_path):
    """SPEC-adjacent security bound: the audit log holds envelope ids and peer
    identities. Created via `os.open(..., 0o600)` so there is no world-readable
    window between create and chmod.

    umask is forced to 0 — under the developer default (022) a plain
    `open(path, 'a')` yields 0644 and would look "fine" on inspection while
    still being wrong.
    """
    previous = os.umask(0)
    try:
        path = tmp_path / "nested" / "audit.jsonl"
        audit = fleet_bus.AuditLog(str(path))
        audit.record("out", "fleet.yugo.status", id="x")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"audit log mode {oct(mode)}"
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    finally:
        os.umask(previous)


def test_audit_reapplies_0600_to_a_preexisting_loose_file(tmp_path):
    """A log left 0644 by an earlier build (or a bind mount) must be tightened
    on first write, not inherited."""
    path = tmp_path / "audit.jsonl"
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o644)
    fleet_bus.AuditLog(str(path)).record("in", "fleet.yugo.request", id="x")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_audit_lines_always_carry_ts_dir_subject(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = fleet_bus.AuditLog(str(path))
    audit.record("out", "fleet.yugo.status", id="a", kind="status_heartbeat")
    audit.record("drop", "fleet.yugo.request", reason="malformed_json")
    audit.record("conn", None, event="disconnected")
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3
    for line in lines:
        assert {"ts", "dir", "subject"} <= set(line), line
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", line["ts"])
    # Connection lifecycle has no subject — the key is still emitted (null) so
    # a log consumer can index every line on the same shape.
    assert lines[2]["subject"] is None
    assert [line["dir"] for line in lines] == ["out", "drop", "conn"]


def test_audit_appends_rather_than_truncating(tmp_path):
    path = tmp_path / "audit.jsonl"
    for i in range(3):
        fleet_bus.AuditLog(str(path)).record("out", "fleet.yugo.status", id=str(i))
    ids = [json.loads(x)["id"] for x in path.read_text(encoding="utf-8").splitlines()]
    assert ids == ["0", "1", "2"]


def test_audit_write_failure_does_not_raise(tmp_path):
    """The bus is the thing being audited. A full disk or a read-only mount is
    not a wire fault and must not take the connection down."""
    unwritable = tmp_path / "ro"
    unwritable.mkdir()
    os.chmod(unwritable, 0o500)
    logged: list[str] = []
    try:
        audit = fleet_bus.AuditLog(str(unwritable / "audit.jsonl"), logger=logged.append)
        audit.record("out", "fleet.yugo.status", id="x")
    finally:
        os.chmod(unwritable, 0o700)
    assert len(logged) == 1
    assert "audit write failed" in logged[0]


def test_audit_without_path_falls_back_to_logger(tmp_path):
    logged: list[str] = []
    fleet_bus.AuditLog(None, logger=logged.append).record("conn", None, event="connected")
    assert len(logged) == 1
    assert json.loads(logged[0].removeprefix("[fleet-bus] "))["event"] == "connected"

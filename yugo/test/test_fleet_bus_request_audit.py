"""Characterize `_on_request` plus the helper audit flows it awaits.

The table deliberately runs real `_warn_origin` and ordinary
`_publish_turn_output` paths against a scripted NATS seam. Only the exceptional
"output dispatcher itself raised" path replaces `_publish_turn_output`, because
its catch exists for a helper-contract violation that `publish_request` normally
absorbs. Parser/tag sub-branches remain covered by their focused tests; this
module covers each helper OUTCOME and every ordering cross-product with the
inbound terminal branches.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

import fleet_bus

SUBJECT = "fleet.yugo.request"
WARN_BATON = {
    "hops": fleet_bus.BATON_HOPS_WARN_AT,
    "root_id": "root-1",
    "origin": "vec",
    "owner": "vec",
}


class RecordingAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, direction, subject, **fields) -> None:
        self.records.append({"dir": direction, "subject": subject, **fields})


class ScriptedNats:
    is_closed = False

    def __init__(self, outcomes: tuple[str, ...]) -> None:
        self.outcomes = list(outcomes)

    async def publish(self, subject, payload) -> None:
        outcome = self.outcomes.pop(0)
        if outcome == "fail":
            raise RuntimeError("scripted publish failure")


@dataclass(frozen=True)
class Case:
    name: str
    overrides: dict[str, Any]
    hook: str
    publishes: tuple[str, ...] = ()
    output_raises: bool = False
    expected: tuple[tuple[str, str | None, str | None], ...] = ()


def _envelope(**overrides) -> dict[str, Any]:
    value = {
        "envelope_version": 1,
        "id": "received-1",
        "from": "vec",
        "to": "yugo",
        "kind": "text_message",
        "ts": "2026-08-30T12:00:00.000Z",
        "payload": {"text": "hello"},
    }
    value.update(overrides)
    return value


def _record(direction, reason=None, note=None):
    return (direction, reason, note)


BASE_CASES = (
    Case(
        "recipient mismatch",
        {"to": "other"},
        "none",
        expected=(_record("drop", "recipient_mismatch"),),
    ),
    Case(
        "baton hop ceiling",
        {"hops": fleet_bus.BATON_HOPS_REJECT_AT},
        "none",
        expected=(_record("drop", fleet_bus.REJECT_HOPS_EXCEEDED),),
    ),
    Case("no session hook", {}, "none", expected=(_record("in"),)),
    Case(
        "session hook raises",
        {},
        "raises",
        expected=(_record("drop", "injection_failed"),),
    ),
    Case("session hook returns None", {}, "none_reply", expected=(_record("in"),)),
    Case(
        "session hook returns text and reply publishes",
        {},
        "text",
        publishes=("ok",),
        expected=(_record("in"), _record("out")),
    ),
    Case(
        "session hook returns text and reply publish is refused",
        {},
        "text",
        publishes=("fail",),
        expected=(_record("in"), _record("drop", fleet_bus.REJECT_PUBLISH_FAILED)),
    ),
    Case(
        "reply dispatcher raises after injection",
        {},
        "text",
        output_raises=True,
        expected=(_record("in"), _record("drop", fleet_bus.REJECT_PUBLISH_FAILED)),
    ),
    Case(
        "hop warning suppressed without origin",
        {"hops": fleet_bus.BATON_HOPS_WARN_AT},
        "none",
        expected=(
            _record("drop", fleet_bus.REJECT_WARNING_SUPPRESSED),
            _record("in"),
        ),
    ),
    Case(
        "hop warning suppressed for self origin",
        {"hops": fleet_bus.BATON_HOPS_WARN_AT, "origin": "yugo"},
        "none",
        expected=(
            _record("drop", fleet_bus.REJECT_WARNING_SUPPRESSED),
            _record("in"),
        ),
    ),
)


def _warning_record(outcome: str):
    return (
        _record("out", note=fleet_bus.AUDIT_NOTE_HOP_WARNING)
        if outcome == "ok"
        else _record(
            "drop",
            fleet_bus.REJECT_PUBLISH_FAILED,
            fleet_bus.AUDIT_NOTE_HOP_WARNING,
        )
    )


def _valid_origin_cases() -> tuple[Case, ...]:
    cases = []
    for warning in ("ok", "fail"):
        prefix = (_warning_record(warning),)
        cases.extend(
            (
                Case(
                    f"valid origin warning {warning}, no session hook",
                    WARN_BATON,
                    "none",
                    publishes=(warning,),
                    expected=prefix + (_record("in"),),
                ),
                Case(
                    f"valid origin warning {warning}, hook raises",
                    WARN_BATON,
                    "raises",
                    publishes=(warning,),
                    expected=prefix + (_record("drop", "injection_failed"),),
                ),
                Case(
                    f"valid origin warning {warning}, hook returns None",
                    WARN_BATON,
                    "none_reply",
                    publishes=(warning,),
                    expected=prefix + (_record("in"),),
                ),
                Case(
                    f"valid origin warning {warning}, dispatcher raises",
                    WARN_BATON,
                    "text",
                    publishes=(warning,),
                    output_raises=True,
                    expected=prefix
                    + (
                        _record("in"),
                        _record("drop", fleet_bus.REJECT_PUBLISH_FAILED),
                    ),
                ),
            )
        )
        for reply in ("ok", "fail"):
            reply_record = (
                _record("out")
                if reply == "ok"
                else _record("drop", fleet_bus.REJECT_PUBLISH_FAILED)
            )
            cases.append(
                Case(
                    f"valid origin warning {warning}, reply publish {reply}",
                    WARN_BATON,
                    "text",
                    publishes=(warning, reply),
                    expected=prefix + (_record("in"), reply_record),
                )
            )
    return tuple(cases)


CASES = BASE_CASES + _valid_origin_cases()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_every_on_request_terminal_path_has_its_exact_current_audit_set(
    case, monkeypatch
):
    """A missing, extra, or reordered record is an audit-contract change."""
    audit = RecordingAudit()

    if case.hook == "none":
        hook = None
    elif case.hook == "raises":
        async def hook(envelope, req_id):
            raise RuntimeError("injection broke")
    elif case.hook == "none_reply":
        async def hook(envelope, req_id):
            return None
    else:
        async def hook(envelope, req_id):
            return "ack"

    config = fleet_bus.FleetBusConfig(
        bot_name="yugo",
        url="nats://unused",
        user="yugo",
        password="unused",
        allowed_from=frozenset({"yugo", "vec"}),
        plugin_version="request-audit-test",
        audit_log_path=None,
    )
    bus = fleet_bus.FleetBus(config, audit, on_envelope=hook)
    if case.publishes:
        bus._nc = ScriptedNats(case.publishes)

    if case.output_raises:
        async def output_failure(subject, envelope, req_id, reply):
            raise RuntimeError("reply dispatcher broke")

        monkeypatch.setattr(bus, "_publish_turn_output", output_failure)

    await bus._on_request(SUBJECT, _envelope(**case.overrides))

    actual = tuple(
        (line["dir"], line.get("reason"), line.get("note"))
        for line in audit.records
    )
    assert actual == case.expected
    if case.publishes:
        assert bus._nc.outcomes == [], "a scripted publish outcome was not exercised"

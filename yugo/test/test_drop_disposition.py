import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fleet_bus import FleetBus
from drop_disposition import DROP_DISPOSITION, DropReason


def harness():
    bus = object.__new__(FleetBus)
    bus._config = SimpleNamespace(bot_name="kat", allowed_from={"deet", "kat"})
    bus._audit = Mock()
    bus._log = Mock()
    bus._nc = SimpleNamespace(publish=AsyncMock())
    bus._drop_counts = dict.fromkeys(["terminal", "transient", "throttled", "duplicate"], 0)
    bus._notice_tasks = set()
    bus._notifying_contexts = {}
    return bus


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,reason,count", [
    ("text_message", DropReason.INJECTION_FAILED, 1),
    ("drop_notice", DropReason.INVALID_ID, 0),
    ("text_message", DropReason.YUGO_DUPLICATE_ENVELOPE, 0),
])
async def test_notice_disposition(kind, reason, count):
    bus = harness()
    bus._record_drop("fleet.kat.request", reason=reason,
                     context={"id": "original", "from": "deet", "kind": kind})
    await asyncio.gather(*bus._notice_tasks)
    assert bus._nc.publish.await_count == count
    disposition = DROP_DISPOSITION[reason]
    assert bus.bus_status()["drop_counts"][disposition] == 1
    assert bus._audit.record.call_args.kwargs["disposition"] == disposition
    if count:
        subject, data = bus._nc.publish.call_args.args
        assert subject == "fleet.deet.request"
        notice = json.loads(data)
        assert notice["kind"] == "drop_notice"
        assert notice["payload"] == {"envelope_id": "original", "reason": reason.value,
                                     "disposition": "transient", "subject": "fleet.kat.request"}


def test_exhaustive_and_no_manifest_codes():
    assert set(DROP_DISPOSITION) == set(DropReason)
    for code in ["fleet_manifest_loaded", "fleet_manifest_load_failed", "fleet_manifest_reload_failed"]:
        assert code not in DROP_DISPOSITION


@pytest.mark.asyncio
async def test_notice_publish_failure_does_not_recurse():
    bus = harness()
    bus._nc.publish.side_effect = RuntimeError("offline")
    bus._record_drop("fleet.kat.request", reason=DropReason.INJECTION_FAILED,
                     context={"id": "original", "from": "deet", "kind": "text_message"})
    await asyncio.gather(*bus._notice_tasks)
    assert bus._nc.publish.await_count == 1
    assert bus._log.call_count == 1


@pytest.mark.asyncio
async def test_real_injection_failure_notifies_once_and_releases_claim():
    bus = harness()
    bus._on_envelope = AsyncMock(side_effect=RuntimeError("inject failed"))
    bus._dedup = SimpleNamespace(claim=Mock(return_value=(False, "req", "owner")),
                                 release=Mock(return_value=True), lease_s=100)
    await bus._on_request("fleet.kat.request", {"id": "original", "from": "deet",
                                              "to": "kat", "kind": "text_message"})
    await asyncio.gather(*bus._notice_tasks)
    assert bus._nc.publish.await_count == 1
    notice = json.loads(bus._nc.publish.call_args.args[1])
    assert notice["payload"]["disposition"] == "transient"
    assert notice["payload"]["reason"] == "injection_failed"
    bus._dedup.release.assert_called_once_with("original", "owner")


@pytest.mark.asyncio
async def test_real_invalid_notice_validation_does_not_notify():
    bus = harness()
    bus._config.max_envelope_bytes = 10000
    # Intentionally invalid (missing id), but with a resolvable sender and notice kind.
    msg = SimpleNamespace(subject="fleet.kat.request", data=json.dumps({
        "envelope_version": 1, "kind": "drop_notice", "from": "deet"}).encode())
    await bus._on_message(msg)
    await asyncio.gather(*bus._notice_tasks)
    bus._nc.publish.assert_not_awaited()
    assert bus.bus_status()["drop_counts"]["terminal"] == 1


@pytest.mark.asyncio
async def test_publish_failure_retains_original_sender_context():
    bus = harness()
    bus._config.max_envelope_bytes = 10000
    bus._nc.is_closed = False
    # First publish (the answer) fails; the diagnostic publish can still succeed.
    bus._nc.publish.side_effect = [RuntimeError("answer failed"), None]
    original = {"id": "original", "from": "deet", "kind": "text_message"}
    ok = await bus.publish_request("deet", {"text": "answer"},
                                   source_subject="fleet.kat.request", source_envelope=original)
    assert not ok
    await asyncio.gather(*bus._notice_tasks)
    assert bus._nc.publish.await_count == 2
    notice = json.loads(bus._nc.publish.call_args.args[1])
    assert notice["payload"]["envelope_id"] == "original"
    assert notice["payload"]["disposition"] == "transient"


def test_drop_audits_cannot_bypass_the_classifying_helper():
    import ast
    from pathlib import Path
    import fleet_bus
    tree = ast.parse(Path(fleet_bus.__file__).read_text())
    drop_writers = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(function):
            if (isinstance(call, ast.Call) and ast.unparse(call.func) == "self._audit.record"
                    and call.args and isinstance(call.args[0], ast.Constant)
                    and call.args[0].value == "drop"):
                drop_writers.append(function.name)
    assert drop_writers == ["_record_drop"]

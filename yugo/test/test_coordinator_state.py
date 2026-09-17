"""Coordinator sqlite schema and durable reconciliation controls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import sqlite3

import pytest
from nats.js.api import DeliverPolicy
from nats.js.errors import NotFoundError

import coordinator
import coordinator_state as state

STREAM = "FLEET_REQUEST"
DURABLE = "yugo-coordinator-request"


def take_lock(monkeypatch, path):
    monkeypatch.setattr(coordinator, "detect_filesystem", lambda _: "ext4")
    return coordinator.acquire_instance_lock(path)


def migrate_lock(lock):
    state.migrate(lock.connection, lock.state_preflight)
    return lock.connection


class FakeJS:
    def __init__(self, consumer=None, when=None):
        self.consumer = consumer
        self.when = when or datetime.now(timezone.utc)
        self.stream = SimpleNamespace(config=SimpleNamespace(name=STREAM), created=self.when)

    async def consumer_info(self, stream, durable):
        if self.consumer is None:
            raise NotFoundError()
        return self.consumer

    async def stream_info(self, stream):
        return self.stream


class FakeRelay:
    def __init__(self, js, *, create=True):
        self._js = js
        self.create = create
        self.opens = 0

    async def open(self):
        self.opens += 1
        if self._js.consumer is None and self.create:
            self._js.consumer = live_consumer(self._js.when)


def live_consumer(created, *, policy=DeliverPolicy.NEW, subject="fleet.*.request"):
    return SimpleNamespace(
        created=created,
        config=SimpleNamespace(deliver_policy=policy, filter_subject=subject),
    )


def test_fresh_file_initializes_schema_version(monkeypatch, tmp_path):
    lock = take_lock(monkeypatch, tmp_path / "state.sqlite")
    try:
        db = migrate_lock(lock)
        assert db.execute("PRAGMA user_version").fetchone()[0] == state.SCHEMA_VERSION
        assert state.marker(db) is None
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        assert tables == {"coordinator_instance", "coordinator_relay_marker"}
    finally:
        lock.release()


def test_created_marker_requires_both_timestamps_and_finalize_is_one_update(monkeypatch, tmp_path):
    lock = take_lock(monkeypatch, tmp_path / "state.sqlite")
    try:
        db = migrate_lock(lock)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO coordinator_relay_marker "
                "(id,state,stream_name,durable_name) VALUES (1,'created',?,?)",
                (STREAM, DURABLE),
            )
        state.write_intent(db, STREAM, DURABLE)
        statements = []
        db.set_trace_callback(statements.append)
        now = datetime.now(timezone.utc)
        state.finalize(db, STREAM, DURABLE, now, now)
        db.set_trace_callback(None)
        assert len([sql for sql in statements if sql.startswith("UPDATE coordinator_relay_marker")]) == 1
        assert state.marker(db)[0] == "created"
    finally:
        lock.release()


def test_legacy_file_migrates_and_keeps_instance_row_with_upgrade_intent(monkeypatch, tmp_path):
    path = tmp_path / "state.sqlite"
    db = sqlite3.connect(path)
    db.execute(state.INSTANCE_DDL)
    db.execute("INSERT INTO coordinator_instance VALUES (1, 123, 'before')")
    db.commit(); db.close()
    lock = take_lock(monkeypatch, path)
    try:
        assert lock.previous_instance == (123, "before")
        db = migrate_lock(lock)
        assert db.execute("SELECT id FROM coordinator_instance").fetchone() == (1,)
        assert state.marker(db)[:3] == ("intent", STREAM, DURABLE)
    finally:
        lock.release()


def test_newer_schema_aborts_before_lock_write_with_version_reason(monkeypatch, tmp_path):
    path = tmp_path / "state.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE coordinator_instance(unrelated TEXT)")
    db.execute("PRAGMA user_version=99"); db.close()
    with pytest.raises(state.CoordinatorStateError, match="coordinator_state_version_newer"):
        take_lock(monkeypatch, path)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA table_info(coordinator_instance)").fetchone()[1] == "unrelated"
    db.close()


def test_foreign_version_zero_database_is_refused(monkeypatch, tmp_path):
    path = tmp_path / "state.sqlite"
    db = sqlite3.connect(path); db.execute("CREATE TABLE foreign_data(x)"); db.commit(); db.close()
    with pytest.raises(state.CoordinatorStateError, match="coordinator_state_foreign"):
        take_lock(monkeypatch, path)


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_point", ["before_create", "after_create"])
async def test_intent_crash_residue_recovers_without_second_consumer(monkeypatch, tmp_path, crash_point):
    lock = take_lock(monkeypatch, tmp_path / "state.sqlite")
    try:
        db = migrate_lock(lock)
        when = datetime.now(timezone.utc)
        js = FakeJS(None, when)
        class CrashRelay(FakeRelay):
            async def open(self):
                self.opens += 1
                if crash_point == "after_create":
                    self._js.consumer = live_consumer(when)
                raise RuntimeError("simulated crash")
        with pytest.raises(RuntimeError, match="simulated crash"):
            await state.reconcile(db, CrashRelay(js))
        assert state.marker(db)[0] == "intent"
        relay = FakeRelay(js)
        await state.reconcile(db, relay)
        assert relay.opens == 1  # create when absent; bind when already created
        assert state.marker(db)[0] == "created"
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_upgrade_intent_finalizes_existing_durable(monkeypatch, tmp_path):
    path = tmp_path / "state.sqlite"
    old = sqlite3.connect(path); old.execute(state.INSTANCE_DDL)
    old.execute("INSERT INTO coordinator_instance VALUES (1,1,'old')"); old.commit(); old.close()
    lock = take_lock(monkeypatch, path)
    try:
        db = migrate_lock(lock)
        when = datetime.now(timezone.utc)
        relay = FakeRelay(FakeJS(live_consumer(when), when))
        await state.reconcile(db, relay)
        assert relay.opens == 1
        assert state.marker(db)[0] == "created"
    finally:
        lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("marker_state,durable,reason", [
    ("created", False, "coordinator_durable_deleted"),
    (None, True, "coordinator_durable_foreign"),
])
async def test_marker_presence_mismatch_aborts(monkeypatch, tmp_path, marker_state, durable, reason):
    lock = take_lock(monkeypatch, tmp_path / "state.sqlite")
    try:
        db = migrate_lock(lock)
        when = datetime.now(timezone.utc)
        if marker_state:
            state.write_intent(db, STREAM, DURABLE)
            state.finalize(db, STREAM, DURABLE, when, when)
        relay = FakeRelay(FakeJS(live_consumer(when) if durable else None, when))
        with pytest.raises(state.CoordinatorStateError, match=reason) as caught:
            await state.reconcile(db, relay)
        assert state.RESET_COMMAND in str(caught.value)
        assert relay.opens == 0
    finally:
        lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["generation", "policy", "filter"])
async def test_created_marker_rejects_generation_or_configuration_drift(monkeypatch, tmp_path, drift):
    lock = take_lock(monkeypatch, tmp_path / "state.sqlite")
    try:
        db = migrate_lock(lock)
        stored = datetime.now(timezone.utc) - (timedelta(seconds=5) if drift == "generation" else timedelta())
        live = datetime.now(timezone.utc)
        state.write_intent(db, STREAM, DURABLE); state.finalize(db, STREAM, DURABLE, stored, live)
        policy = DeliverPolicy.ALL if drift == "policy" else DeliverPolicy.NEW
        subject = "fleet.other.request" if drift == "filter" else "fleet.*.request"
        relay = FakeRelay(FakeJS(live_consumer(live, policy=policy, subject=subject), live))
        with pytest.raises(state.CoordinatorStateError):
            await state.reconcile(db, relay)
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_stream_created_comparison_is_exact(monkeypatch, tmp_path):
    lock = take_lock(monkeypatch, tmp_path / "state.sqlite")
    try:
        db = migrate_lock(lock); live = datetime.now(timezone.utc)
        state.write_intent(db, STREAM, DURABLE)
        state.finalize(db, STREAM, DURABLE, live, live - timedelta(microseconds=1))
        with pytest.raises(state.CoordinatorStateError, match="coordinator_durable_regenerated"):
            await state.reconcile(db, FakeRelay(FakeJS(live_consumer(live), live)))
    finally:
        lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("delta,aborts", [(timedelta(microseconds=200), False), (timedelta(seconds=5), True)])
async def test_consumer_created_uses_one_second_tolerance(monkeypatch, tmp_path, delta, aborts):
    lock = take_lock(monkeypatch, tmp_path / "state.sqlite")
    try:
        db = migrate_lock(lock); live = datetime.now(timezone.utc)
        state.write_intent(db, STREAM, DURABLE); state.finalize(db, STREAM, DURABLE, live - delta, live)
        operation = state.reconcile(db, FakeRelay(FakeJS(live_consumer(live), live)))
        if aborts:
            with pytest.raises(state.CoordinatorStateError, match="coordinator_durable_regenerated"):
                await operation
        else:
            await operation
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_accept_reset_restamps_generation_and_lock_excludes_second_process(monkeypatch, tmp_path):
    path = tmp_path / "state.sqlite"; lock = take_lock(monkeypatch, path)
    try:
        db = migrate_lock(lock); old = datetime.now(timezone.utc) - timedelta(seconds=10)
        state.write_intent(db, STREAM, DURABLE); state.finalize(db, STREAM, DURABLE, old, old)
        with pytest.raises(coordinator.CoordinatorLockError) as caught:
            take_lock(monkeypatch, path)
        assert caught.value.reason == "coordinator_already_running"
        live = datetime.now(timezone.utc); js = FakeJS(live_consumer(live), live)
        await state.accept_reset(db, js)
        assert state.marker(db)[3:] == (live.isoformat(), live.isoformat())
        await state.reconcile(db, FakeRelay(js))
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_accept_reset_clears_deleted_durable_marker_and_next_start_succeeds(monkeypatch, tmp_path):
    lock = take_lock(monkeypatch, tmp_path / "state.sqlite")
    try:
        db = migrate_lock(lock); old = datetime.now(timezone.utc) - timedelta(seconds=10)
        state.write_intent(db, STREAM, DURABLE); state.finalize(db, STREAM, DURABLE, old, old)
        js = FakeJS(None)

        with pytest.raises(state.CoordinatorStateError, match="--acknowledge-traffic-gap"):
            await state.accept_reset(db, js)
        assert state.marker(db)[0] == "created"

        assert await state.accept_reset(db, js, acknowledge_traffic_gap=True) is True
        assert state.marker(db) is None

        relay = FakeRelay(js)
        await state.reconcile(db, relay)
        assert relay.opens == 1
        assert state.marker(db)[0] == "created"
        assert js.consumer.config.deliver_policy == DeliverPolicy.NEW
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_durable_reappearing_after_clear_is_foreign(monkeypatch, tmp_path):
    lock = take_lock(monkeypatch, tmp_path / "state.sqlite")
    try:
        db = migrate_lock(lock); when = datetime.now(timezone.utc)
        state.write_intent(db, STREAM, DURABLE); state.finalize(db, STREAM, DURABLE, when, when)
        js = FakeJS(None, when)
        await state.accept_reset(db, js, acknowledge_traffic_gap=True)
        js.consumer = live_consumer(when)

        with pytest.raises(state.CoordinatorStateError, match="coordinator_durable_foreign"):
            await state.reconcile(db, FakeRelay(js))
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_reconcile_reloads_and_rejects_policy_swap_during_open(monkeypatch, tmp_path):
    lock = take_lock(monkeypatch, tmp_path / "state.sqlite")
    try:
        db = migrate_lock(lock); when = datetime.now(timezone.utc)
        state.write_intent(db, STREAM, DURABLE); state.finalize(db, STREAM, DURABLE, when, when)
        js = FakeJS(live_consumer(when), when)

        class SwapRelay(FakeRelay):
            async def open(self):
                self.opens += 1
                self._js.consumer = live_consumer(when, policy=DeliverPolicy.ALL)

        with pytest.raises(state.CoordinatorStateError, match="coordinator_durable_policy_drift"):
            await state.reconcile(db, SwapRelay(js))
    finally:
        lock.release()

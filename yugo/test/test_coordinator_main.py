"""Coordinator entrypoint ordering, supervision and live-shaped NATS wiring."""

from __future__ import annotations

import asyncio
from datetime import datetime
import os
import signal
import sqlite3
import time
from pathlib import Path

import nats
import pytest

import coordinator_main
from coordinator_main import CoordinatorStartupError, run_coordinator
import coordinator
from coordinator import CoordinatorLockError, acquire_instance_lock
from test.nats_server import NatsServer, _free_port, nats_server_binary


class FakeLock:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


def _env(tmp_path, url, password="coord-test"):
    token = tmp_path / f"token-{password}"
    token.write_text(password)
    return {
        "YUGO_MODE": "coordinator",
        "FLEET_BUS_URL": url,
        "FLEET_BUS_USER": "coordinator",
        "FLEET_BUS_TOKEN_FILE": str(token),
        "TASK_STATE_PATH": str(tmp_path / "state.sqlite"),
        "COORDINATOR_HEARTBEAT_MS": "20",
    }


def _flat_server(tmp_path):
    binary = nats_server_binary()
    if binary is None:
        if os.environ.get("CI"):
            pytest.fail("CI must run coordinator entrypoint broker tests")
        pytest.skip("no nats-server binary")
    port = _free_port()
    source = (Path(__file__).parent / "fixtures/nats-coordinator-flat.conf").read_text()
    source = source.replace("COORDINATOR_PASSWORD", "coord-test")
    source = source.replace("PROVISIONER_PASSWORD", "provision-test")
    source = source.replace("SENDER_PASSWORD", "sender-test")
    config = tmp_path / "flat.conf"
    config.write_text(f'port: {port}\njetstream {{store_dir: "{tmp_path / "js"}"}}\n{source}')
    return NatsServer(binary, config, port)


@pytest.mark.asyncio
async def test_invalid_config_fails_before_lock_or_network(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _path: called.append("lock"))
    monkeypatch.setattr(coordinator_main, "connect_nats", lambda *_a, **_k: called.append("connect"))
    with pytest.raises(Exception, match="FLEET_BUS_USER"):
        await run_coordinator({"YUGO_MODE": "coordinator", "FLEET_BUS_URL": "nats://x"})
    assert called == []
    assert not (tmp_path / "state.sqlite").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("traffic_gap", [False, True])
async def test_accept_reset_logs_all_audit_fields(tmp_path, monkeypatch, traffic_gap):
    class ResetLock(FakeLock):
        connection = object()
        state_preflight = object()

    class Connection:
        def jetstream(self):
            return object()

        async def close(self):
            pass

    lock = ResetLock()
    lines = []
    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _path: lock)
    monkeypatch.setattr(coordinator_main.coordinator_state, "migrate", lambda *_args: None)
    monkeypatch.setattr(coordinator_main, "connect_nats", lambda *_args: asyncio.sleep(0, result=Connection()))
    monkeypatch.setattr(coordinator_main, "_operator_identity", lambda _env: "test-operator")
    monkeypatch.setattr(
        coordinator_main.coordinator_state,
        "accept_reset",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=traffic_gap),
    )

    assert await coordinator_main.run_accept_durable_reset(
        _env(tmp_path, "nats://unused"),
        acknowledge_traffic_gap=traffic_gap,
        logger=lines.append,
    ) == 0
    assert len(lines) == 1
    fields = dict(part.split("=", 1) for part in lines[0].split() if "=" in part)
    assert fields["operator"] == "test-operator"
    assert datetime.fromisoformat(fields["timestamp"]).tzinfo is not None
    assert "acknowledged_risk=" in lines[0]
    if traffic_gap:
        assert "operator accepted the traffic gap" in lines[0]
        assert "NEW-only durable" in lines[0]
    else:
        assert "acknowledged_risk=none" in lines[0]
    assert lock.released


def test_main_routes_explicit_traffic_gap_acknowledgement(monkeypatch):
    calls = []

    async def reset(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(coordinator_main, "run_accept_durable_reset", reset)
    assert coordinator_main.main(["accept-durable-reset", "--acknowledge-traffic-gap"]) == 0
    assert calls == [{"acknowledge_traffic_gap": True}]


def test_operator_identity_falls_back_without_failing(monkeypatch):
    monkeypatch.setattr(coordinator_main.os, "getlogin", lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(coordinator_main.getpass, "getuser", lambda: (_ for _ in ()).throw(KeyError()))
    assert coordinator_main._operator_identity({"SUDO_USER": "deploy", "USER": "ignored"}) == "deploy"
    monkeypatch.setattr(coordinator_main.os, "getuid", lambda: 1234)
    assert coordinator_main._operator_identity({}) == "uid:1234"


def test_operator_identity_survives_a_raising_getuid(monkeypatch):
    """SPEC §6.4.2: never fail the command on an unresolvable identity.

    getuid(2) cannot fail on Linux, so this path is unreachable in production.
    It is guarded anyway because the consequence of a raise here is a marker
    mutation with no audit record, not a crash the operator can see.
    """
    monkeypatch.setattr(coordinator_main.os, "getlogin", lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(coordinator_main.getpass, "getuser", lambda: (_ for _ in ()).throw(KeyError()))
    monkeypatch.setattr(coordinator_main.os, "getuid", lambda: (_ for _ in ()).throw(OSError()))
    assert coordinator_main._operator_identity({}) == "uid:unknown"


@pytest.mark.asyncio
async def test_audit_identity_is_resolved_before_the_marker_is_mutated(tmp_path, monkeypatch):
    """The ordering IS the guard, so assert the ordering.

    If identity resolution is ever moved back after accept_reset, a failure in
    it clears the marker and then dies before recording who did it — a silent
    destructive action, which is what §6.4 exists to prevent.
    """
    class ResetLock(FakeLock):
        connection = object()
        state_preflight = object()

    class Connection:
        def jetstream(self):
            return object()

        async def close(self):
            pass

    order = []
    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _path: ResetLock())
    monkeypatch.setattr(coordinator_main.coordinator_state, "migrate", lambda *_args: None)
    monkeypatch.setattr(coordinator_main, "connect_nats", lambda *_args: asyncio.sleep(0, result=Connection()))

    def identity(_env):
        order.append("identity")
        return "test-operator"

    def accept_reset(*_args, **_kwargs):
        order.append("mutate")
        return asyncio.sleep(0, result=False)

    monkeypatch.setattr(coordinator_main, "_operator_identity", identity)
    monkeypatch.setattr(coordinator_main.coordinator_state, "accept_reset", accept_reset)

    assert await coordinator_main.run_accept_durable_reset(
        _env(tmp_path, "nats://unused"), logger=lambda _line: None
    ) == 0
    assert order == ["identity", "mutate"]


@pytest.mark.asyncio
async def test_real_second_instance_refuses_before_connect_or_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setattr(coordinator, "detect_filesystem", lambda _path: "ext4")
    first = acquire_instance_lock(tmp_path / "state.sqlite")
    calls = []

    async def forbidden_connect(*_a, **_k):
        calls.append("connect")
        pytest.fail("second coordinator connected before lock refusal")

    monkeypatch.setattr(coordinator_main, "connect_nats", forbidden_connect)
    try:
        with pytest.raises(CoordinatorLockError, match="another coordinator"):
            await run_coordinator(_env(tmp_path, "nats://unused"))
        assert calls == []  # no connection means no heartbeat can be emitted
    finally:
        first.release()
    replacement = acquire_instance_lock(tmp_path / "state.sqlite")
    replacement.release()


@pytest.mark.asyncio
async def test_connect_auth_callback_signals_without_raising(monkeypatch, tmp_path):
    lock = FakeLock()
    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _path: lock)

    async def auth_loop(_config, *, error_cb, **_kwargs):
        await error_cb(Exception("Authorization Violation"))
        await asyncio.Future()

    monkeypatch.setattr(coordinator_main, "connect_nats", auth_loop)
    started = time.monotonic()
    with pytest.raises(CoordinatorStartupError, match="authorization failed during connect"):
        await run_coordinator(
            _env(tmp_path, "nats://unused"),
            logger=lambda _line: (_ for _ in ()).throw(RuntimeError("logger failed")),
        )
    assert time.monotonic() - started < 0.2
    assert lock.released


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["repr", "classifier", "signal", "cancelled_logger"])
async def test_error_callback_never_raises_and_fails_closed(monkeypatch, tmp_path, fault):
    lock = FakeLock()
    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _path: lock)
    callback_raised = []

    class BadError(Exception):
        def __str__(self):
            return "Authorization Violation"
        def __repr__(self):
            if fault == "repr":
                raise RuntimeError("bad repr")
            return "BadError()"

    if fault == "classifier":
        monkeypatch.setattr(coordinator_main.fleet_bus, "_is_authorization_failure",
                            lambda _error: (_ for _ in ()).throw(RuntimeError("classifier")))
    if fault == "signal":
        real_event = asyncio.Event
        count = 0
        class BrokenSignalEvent:
            def __new__(cls):
                nonlocal count
                count += 1
                event = real_event()
                if count == 2:
                    event.set = lambda: (_ for _ in ()).throw(RuntimeError("signal"))
                return event
        monkeypatch.setattr(coordinator_main.asyncio, "Event", BrokenSignalEvent)

    async def auth_loop(_config, *, error_cb, **_kwargs):
        try:
            await error_cb(BadError())
        except BaseException as error:
            callback_raised.append(error)
        await asyncio.Future()

    monkeypatch.setattr(coordinator_main, "connect_nats", auth_loop)
    logger = (lambda _line: (_ for _ in ()).throw(asyncio.CancelledError())) \
        if fault == "cancelled_logger" else (lambda _line: None)
    with pytest.raises(CoordinatorStartupError, match="authorization failed during connect"):
        await asyncio.wait_for(run_coordinator(_env(tmp_path, "nats://unused"), logger=logger), 1)
    assert callback_raised == []
    assert lock.released


@pytest.mark.asyncio
async def test_reconnect_error_callback_fault_never_raises_and_stops_entrypoint(monkeypatch, tmp_path):
    callback_raised = []

    class Connection:
        async def close(self): pass

    async def connect(*_args, error_cb, **_kwargs):
        async def reconnect_error():
            await asyncio.sleep(.01)
            try:
                await error_cb(Exception("reconnect error"))
            except BaseException as error:
                callback_raised.append(error)
        asyncio.create_task(reconnect_error())
        return Connection()

    class Worker:
        def __init__(self, *_args, **_kwargs): pass
        async def open(self): pass
        async def run(self): await asyncio.Future()

    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _: FakeLock())
    monkeypatch.setattr(coordinator_main, "connect_nats", connect)
    monkeypatch.setattr(coordinator_main, "CoordinatorRelay", Worker)
    monkeypatch.setattr(coordinator_main, "CoordinatorHeartbeat", Worker)
    monkeypatch.setattr(coordinator_main.fleet_bus, "_is_authorization_failure",
                        lambda _: (_ for _ in ()).throw(RuntimeError("classifier")))
    with pytest.raises(CoordinatorStartupError, match="authorization failed"):
        await asyncio.wait_for(run_coordinator(_env(tmp_path, "nats://unused"), logger=lambda _: None), 1)
    assert callback_raised == []


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["stop", "auth"])
async def test_simultaneous_connect_completion_is_closed_before_early_exit(monkeypatch, tmp_path, trigger):
    handlers = {}
    loop = asyncio.get_running_loop()
    lock = FakeLock()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: handlers.__setitem__(sig, cb))
    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _path: lock)

    class Connection:
        closed = False
        async def close(self):
            self.closed = True
    connection = Connection()

    async def connect(*_args, error_cb, **_kwargs):
        if trigger == "stop":
            handlers[signal.SIGTERM]()
        else:
            await error_cb(Exception("Authorization Violation"))
        return connection

    monkeypatch.setattr(coordinator_main, "connect_nats", connect)
    if trigger == "stop":
        assert await run_coordinator(_env(tmp_path, "nats://unused"), logger=lambda _: None) == 0
    else:
        with pytest.raises(CoordinatorStartupError):
            await run_coordinator(_env(tmp_path, "nats://unused"), logger=lambda _: None)
    assert connection.closed
    assert lock.released


@pytest.mark.asyncio
async def test_initial_broker_outage_has_no_wall_clock_timeout_and_is_killable(monkeypatch, tmp_path):
    lock = FakeLock()
    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _path: lock)
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: handlers.__setitem__(sig, cb))

    async def unavailable_forever(*_args, **_kwargs):
        await asyncio.Future()

    monkeypatch.setattr(coordinator_main, "connect_nats", unavailable_forever)
    task = asyncio.create_task(run_coordinator(_env(tmp_path, "nats://down"), logger=lambda _line: None))
    await asyncio.sleep(0.1)
    assert not task.done()
    handlers[signal.SIGTERM]()
    assert await asyncio.wait_for(task, timeout=1) == 0
    assert lock.released


@pytest.mark.asyncio
async def test_sigterm_after_real_lock_before_migration_releases_and_exits_zero(monkeypatch, tmp_path):
    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: handlers.__setitem__(sig, cb))
    monkeypatch.setattr(coordinator, "detect_filesystem", lambda _: "ext4")
    real_acquire = coordinator.acquire_instance_lock

    def acquire_then_stop(path):
        lock = real_acquire(path)
        handlers[signal.SIGTERM]()
        return lock

    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", acquire_then_stop)
    monkeypatch.setattr(
        coordinator_main, "connect_nats",
        lambda *_a, **_k: pytest.fail("shutdown-before-migration must not connect"),
    )
    env = _env(tmp_path, "nats://unused")
    assert await run_coordinator(env, logger=lambda _: None) == 0
    db = sqlite3.connect(env["TASK_STATE_PATH"])
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    db.close()
    replacement = real_acquire(env["TASK_STATE_PATH"])
    replacement.release()


@pytest.mark.asyncio
async def test_entrypoint_uses_flat_stanza_and_refuses_stream_delete(tmp_path, monkeypatch):
    server = _flat_server(tmp_path)
    server.start()
    provisioner = coordinator = None
    try:
        provisioner = await nats.connect(server.url, user="provisioner", password="provision-test")
        js = provisioner.jetstream()
        await js.add_stream(name="FLEET_REQUEST", subjects=["fleet.*.request"])
        await js.add_stream(name="FLEET_INBOX", subjects=["fleet.*.inbox"])
        handlers = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: handlers.__setitem__(sig, cb))
        real_connect = coordinator_main.connect_nats
        connected = asyncio.Queue()
        async def observed_connect(*args, **kwargs):
            nc = await real_connect(*args, **kwargs)
            await connected.put(nc)
            return nc
        monkeypatch.setattr(coordinator_main, "connect_nats", observed_connect)
        task = asyncio.create_task(run_coordinator(_env(tmp_path, server.url), logger=lambda _: None))
        coordinator = await asyncio.wait_for(connected.get(), 1)
        deadline = loop.time() + 1
        while True:
            try:
                await js.consumer_info("FLEET_REQUEST", "yugo-coordinator-request")
                break
            except Exception:
                if loop.time() > deadline: raise
                await asyncio.sleep(.01)
        await coordinator.publish("$JS.API.STREAM.DELETE.FLEET_REQUEST", b"{}")
        await coordinator.flush()
        await asyncio.sleep(.05)
        assert (await js.stream_info("FLEET_REQUEST")).config.name == "FLEET_REQUEST"
        handlers[signal.SIGTERM]()
        assert await asyncio.wait_for(task, 1) == 0
        state_db = sqlite3.connect(tmp_path / "state.sqlite")
        assert state_db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert state_db.execute(
            "SELECT state FROM coordinator_relay_marker WHERE id=1"
        ).fetchone() == ("created",)
        state_db.close()
    finally:
        for client in (coordinator, provisioner):
            if client is not None:
                await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_terminal_task_failure_is_observed_cancels_sibling_and_cleans_up(monkeypatch, tmp_path):
    lock = FakeLock()
    sibling_cancelled = asyncio.Event()

    class Connection:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    connection = Connection()

    async def fake_connect(*_args, **_kwargs):
        return connection

    class Relay:
        def __init__(self, *_args, **_kwargs):
            pass

        async def open(self):
            return None

        async def run(self):
            raise RuntimeError("terminal relay failure")

    class Heartbeat:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _path: lock)
    monkeypatch.setattr(coordinator_main, "connect_nats", fake_connect)
    monkeypatch.setattr(coordinator_main, "CoordinatorRelay", Relay)
    monkeypatch.setattr(coordinator_main, "CoordinatorHeartbeat", Heartbeat)
    with pytest.raises(CoordinatorStartupError, match="relay task failed"):
        await run_coordinator(_env(tmp_path, "nats://unused"), logger=lambda _line: None)
    assert sibling_cancelled.is_set()
    assert connection.closed
    assert lock.released


@pytest.mark.asyncio
@pytest.mark.parametrize("close_raises", [False, True])
async def test_shutdown_finishes_workers_then_closes_nats_then_releases_lock(
    monkeypatch, tmp_path, close_raises
):
    trace = []
    handlers = {}
    loop = asyncio.get_running_loop()

    class Lock:
        def release(self):
            trace.append("lock.release")

    class Connection:
        async def close(self):
            trace.append("nc.close")
            if close_raises:
                raise RuntimeError("close failed")

    class Worker:
        def __init__(self, *_args, **_kwargs): pass
        async def open(self): pass
        async def run(self):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                trace.append("worker.cancelled")
                raise

    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: handlers.__setitem__(sig, cb))
    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _: Lock())
    monkeypatch.setattr(coordinator_main, "connect_nats", lambda *_a, **_k: asyncio.sleep(0, result=Connection()))
    monkeypatch.setattr(coordinator_main, "CoordinatorRelay", Worker)
    monkeypatch.setattr(coordinator_main, "CoordinatorHeartbeat", Worker)
    task = asyncio.create_task(run_coordinator(_env(tmp_path, "nats://unused"), logger=lambda _: None))
    while signal.SIGTERM not in handlers:
        await asyncio.sleep(0)
    await asyncio.sleep(.01)
    handlers[signal.SIGTERM]()
    assert await asyncio.wait_for(task, 1) == 0
    assert trace.count("worker.cancelled") == 2
    last_worker = max(i for i, item in enumerate(trace) if item == "worker.cancelled")
    assert last_worker < trace.index("nc.close") < trace.index("lock.release")


@pytest.mark.asyncio
async def test_entrypoint_executes_no_sql_after_workers_begin(monkeypatch, tmp_path):
    handlers = {}
    loop = asyncio.get_running_loop()
    statements_after_start = []
    armed = False
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: handlers.__setitem__(sig, cb))
    monkeypatch.setattr(coordinator, "detect_filesystem", lambda _: "ext4")

    class Connection:
        async def close(self): pass

    class Worker:
        def __init__(self, *_args, **_kwargs): pass
        async def open(self): pass
        async def run(self):
            nonlocal armed
            armed = True
            await asyncio.Future()

    async def reconcile_then_tripwire(connection, relay):
        await relay.open()
        connection.set_trace_callback(
            lambda sql: statements_after_start.append(sql) if armed else None
        )

    monkeypatch.setattr(coordinator_main, "connect_nats", lambda *_a, **_k: asyncio.sleep(0, result=Connection()))
    monkeypatch.setattr(coordinator_main, "CoordinatorRelay", Worker)
    monkeypatch.setattr(coordinator_main, "CoordinatorHeartbeat", Worker)
    monkeypatch.setattr(coordinator_main.coordinator_state, "reconcile", reconcile_then_tripwire)
    task = asyncio.create_task(run_coordinator(_env(tmp_path, "nats://unused"), logger=lambda _: None))
    while not armed:
        await asyncio.sleep(0)
    await asyncio.sleep(.01)
    handlers[signal.SIGTERM]()
    assert await asyncio.wait_for(task, 1) == 0
    assert statements_after_start == []



@pytest.mark.asyncio
async def test_entrypoint_forwards_after_long_broker_outage_and_sigterm_cleans_up(tmp_path, monkeypatch):
    server = _flat_server(tmp_path)
    server.start()
    provisioner = sender = observer = None
    task = None
    lock = FakeLock()
    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _path: lock)
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb: handlers.__setitem__(sig, cb))
    try:
        provisioner = await nats.connect(server.url, user="provisioner", password="provision-test")
        js = provisioner.jetstream()
        await js.add_stream(name="FLEET_REQUEST", subjects=["fleet.*.request"])
        await js.add_stream(name="FLEET_INBOX", subjects=["fleet.*.inbox"])
        task = asyncio.create_task(run_coordinator(
            _env(tmp_path, server.url), logger=lambda _line: None,
            connect_options={"reconnect_time_wait": 0.1},
        ))
        deadline = loop.time() + 2
        while True:
            try:
                before = await js.consumer_info("FLEET_REQUEST", "yugo-coordinator-request")
                break
            except Exception:
                if loop.time() > deadline:
                    raise
                await asyncio.sleep(0.02)

        server.stop()
        await asyncio.sleep(2.0)  # > (finite mutant attempts 5 + 1) * 0.1s
        assert not task.done()
        server.start()
        await asyncio.sleep(0.3)
        assert not task.done()
        provisioner = await nats.connect(server.url, user="provisioner", password="provision-test")
        js = provisioner.jetstream()
        after = await js.consumer_info("FLEET_REQUEST", "yugo-coordinator-request")
        assert after.name == before.name
        sender = await nats.connect(server.url, user="sender", password="sender-test")
        observer = await nats.connect(server.url, user="provisioner", password="provision-test")
        seen = asyncio.Event()

        async def on_inbox(_message):
            seen.set()

        await observer.subscribe("fleet.sender.inbox", cb=on_inbox)
        await observer.flush()
        await sender.publish("fleet.sender.request", b"after-outage")
        await sender.flush()
        await asyncio.wait_for(seen.wait(), 2)
        handlers[signal.SIGTERM]()
        assert await asyncio.wait_for(task, 2) == 0
        assert lock.released
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for client in (sender, observer, provisioner):
            if client is not None:
                await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_wrong_token_at_boot_exits_quickly_and_releases_lock(tmp_path, monkeypatch):
    server = _flat_server(tmp_path)
    server.start()
    lock = FakeLock()
    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _path: lock)
    started = time.monotonic()
    try:
        with pytest.raises(CoordinatorStartupError, match="authorization failed during connect"):
            await asyncio.wait_for(run_coordinator(
                _env(tmp_path, server.url, password="wrong-token"), logger=lambda _line: None,
                connect_options={"reconnect_time_wait": 0.1},
            ), timeout=1)
        assert time.monotonic() - started < 0.2
        assert lock.released
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_token_rotation_on_restart_exits_quickly_and_releases_lock(tmp_path, monkeypatch):
    server = _flat_server(tmp_path)
    server.start()
    lock = FakeLock()
    monkeypatch.setattr(coordinator_main, "acquire_instance_lock", lambda _path: lock)
    task = None
    provisioner = None
    try:
        provisioner = await nats.connect(server.url, user="provisioner", password="provision-test")
        js = provisioner.jetstream()
        await js.add_stream(name="FLEET_REQUEST", subjects=["fleet.*.request"])
        await js.add_stream(name="FLEET_INBOX", subjects=["fleet.*.inbox"])
        task = asyncio.create_task(run_coordinator(
            _env(tmp_path, server.url), logger=lambda _line: None,
            connect_options={"reconnect_time_wait": 0.1},
        ))
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            try:
                await js.consumer_info("FLEET_REQUEST", "yugo-coordinator-request")
                break
            except Exception:
                if asyncio.get_running_loop().time() > deadline:
                    raise
                await asyncio.sleep(0.02)
        await provisioner.close()
        provisioner = None
        server.stop()
        config_path = server._config_path
        config_path.write_text(config_path.read_text().replace("coord-test", "rotated-token"))
        started = time.monotonic()
        server.start()
        with pytest.raises(CoordinatorStartupError):
            await asyncio.wait_for(task, timeout=1)
        assert time.monotonic() - started < 0.2
        assert lock.released
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if provisioner is not None:
            await provisioner.close()
        server.stop()

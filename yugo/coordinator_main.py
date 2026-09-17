"""Standalone coordinator process entrypoint (v0.6b-1)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import getpass
import os
import signal
import sys

import fleet_bus
import coordinator_state
from coordinator import (
    CoordinatorHeartbeat,
    CoordinatorRelay,
    acquire_instance_lock,
    connect_nats,
    load_heartbeat_ms,
    load_nats_config,
    resolve_mode,
)
from version import YUGO_VERSION


class CoordinatorStartupError(RuntimeError):
    pass


async def run_coordinator(environ=None, *, logger=print, connect_options=None) -> int:
    """Start, supervise and cleanly stop relay + heartbeat."""
    env = os.environ if environ is None else environ
    if resolve_mode(env) != "coordinator":
        raise CoordinatorStartupError("coordinator entrypoint requires YUGO_MODE=coordinator")

    # Pure config first: a typo must not create/lock state on disk.
    nats_config = load_nats_config(env)
    heartbeat_ms = load_heartbeat_ms(env)
    state_path = (env.get("TASK_STATE_PATH") or "").strip()

    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except (NotImplementedError, RuntimeError):
            pass

    lock = None
    nc = None
    tasks: dict[asyncio.Task, str] = {}
    bootstrap_tasks: set[asyncio.Task] = set()
    auth_failed = asyncio.Event()
    closed = asyncio.Event()

    def safe_log(message):
        try:
            logger(message)
        except BaseException:
            # In particular, error_cb must never throw into nats-py's
            # reconnect task. Logging failure cannot be allowed to wedge it.
            pass

    auth_detected = False
    wake_task = None

    def safe_error_repr(error):
        try:
            return repr(error)
        except BaseException:
            return f"<{type(error).__name__}: repr failed>"

    async def error_cb(error):
        # Never raise from a nats callback. On reconnect that kills nats-py's
        # background reconnect task and leaves status wedged at RECONNECTING.
        nonlocal auth_detected
        try:
            is_auth = fleet_bus._is_authorization_failure(error)
        except BaseException as classifier_error:
            # Fail closed. If auth classification itself is broken, continuing
            # can recreate the infinite silent CONNECT/RECONNECT wedge.
            is_auth = True
            safe_log(f"coordinator NATS error classification failed: {safe_error_repr(classifier_error)}")
        if is_auth:
            auth_detected = True
            safe_log(f"coordinator NATS authorization failed: {safe_error_repr(error)}")
            signal_failed = False
            try:
                auth_failed.set()
            except BaseException as signal_error:
                signal_failed = True
                safe_log(f"coordinator auth event signal failed: {safe_error_repr(signal_error)}")
            # Fallback wake-up for a fault-injected/broken Event.set. The
            # entrypoint checks auth_detected before interpreting cancellation.
            if signal_failed and wake_task is not None and not wake_task.done():
                wake_task.cancel()
        else:
            safe_log(f"coordinator NATS error: {safe_error_repr(error)}")

    async def disconnected_cb():
        safe_log("coordinator NATS disconnected; waiting for nats-py reconnect")

    async def reconnected_cb():
        safe_log("coordinator NATS reconnected")

    async def closed_cb():
        safe_log("coordinator NATS connection closed")
        closed.set()

    try:
        # Lock before any network operation: a rejected second instance must
        # never connect, heartbeat, or forward.
        lock = acquire_instance_lock(state_path)
        # Give an already-delivered shutdown signal a cancellation point before
        # synchronous migration work begins.
        await asyncio.sleep(0)
        if shutdown.is_set():
            return 0
        # Some unit tests replace the lock with a lifecycle-only test double;
        # production InstanceLock always owns the state connection.
        state_connection = getattr(lock, "connection", None)
        if getattr(lock, "previous_instance", None) is not None:
            safe_log(
                f"coordinator previous instance pid={lock.previous_instance[0]} "
                f"acquired_at={lock.previous_instance[1]}"
            )
        if state_connection is not None:
            coordinator_state.migrate(state_connection, lock.state_preflight)

        connect_task = asyncio.create_task(connect_nats(
            nats_config,
            error_cb=error_cb,
            disconnected_cb=disconnected_cb,
            reconnected_cb=reconnected_cb,
            closed_cb=closed_cb,
            **(connect_options or {}),
        ), name="coordinator-connect")
        wake_task = connect_task
        auth_task = asyncio.create_task(auth_failed.wait(), name="coordinator-auth-watch-connect")
        stop_task = asyncio.create_task(shutdown.wait(), name="coordinator-stop-connect")
        bootstrap_tasks = {connect_task, auth_task, stop_task}
        done, _ = await asyncio.wait(
            (connect_task, auth_task, stop_task), return_when=asyncio.FIRST_COMPLETED
        )
        # Recover a successfully-created connection before either early-exit
        # branch. connect and stop/auth can become ready in the same loop turn.
        if connect_task.done() and not connect_task.cancelled() and connect_task.exception() is None:
            nc = connect_task.result()
        if auth_detected:
            raise CoordinatorStartupError("coordinator NATS authorization failed during connect")
        if stop_task in done and shutdown.is_set():
            return 0
        if nc is None:
            nc = await connect_task
        for task in (auth_task, stop_task):
            task.cancel()
        await asyncio.gather(auth_task, stop_task, return_exceptions=True)
        bootstrap_tasks.clear()

        relay = CoordinatorRelay(nc, logger=safe_log)
        # Reconcile after NATS connects but before either long-lived task starts.
        if state_connection is not None:
            await coordinator_state.reconcile(state_connection, relay)
        else:
            await relay.open()
        heartbeat = CoordinatorHeartbeat(
            nc, version=YUGO_VERSION, interval_ms=heartbeat_ms, logger=safe_log
        )
        relay_task = asyncio.create_task(relay.run(), name="coordinator-relay")
        heartbeat_task = asyncio.create_task(heartbeat.run(), name="coordinator-heartbeat")
        auth_watch = asyncio.create_task(auth_failed.wait(), name="coordinator-auth-watch")
        closed_watch = asyncio.create_task(closed.wait(), name="coordinator-closed-watch")
        shutdown_watch = asyncio.create_task(shutdown.wait(), name="coordinator-shutdown-watch")
        tasks = {
            relay_task: "relay",
            heartbeat_task: "heartbeat",
            auth_watch: "authorization watcher",
            closed_watch: "connection watcher",
            shutdown_watch: "shutdown watcher",
        }
        wake_task = auth_watch
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if auth_detected:
            raise CoordinatorStartupError("coordinator NATS authorization failed")
        if closed_watch in done and closed.is_set():
            raise CoordinatorStartupError("coordinator NATS connection closed terminally")
        workers = [task for task in (relay_task, heartbeat_task) if task in done]
        if workers:
            winner = workers[0]
        elif shutdown_watch in done and shutdown.is_set():
            return 0
        else:
            winner = next(iter(done))
        try:
            error = winner.exception()
        except asyncio.CancelledError:
            raise
        if error is not None:
            safe_log(f"coordinator {tasks[winner]} task failed: {error!r}")
            raise CoordinatorStartupError(f"coordinator {tasks[winner]} task failed") from error
        raise CoordinatorStartupError(f"coordinator {tasks[winner]} task exited unexpectedly")
    finally:
        # Cancellation closes the work window before releasing singleton
        # ownership. close() is bounded/idempotent where drain() is not.
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in bootstrap_tasks:
            if not task.done():
                task.cancel()
        if bootstrap_tasks:
            await asyncio.gather(*bootstrap_tasks, return_exceptions=True)
        if nc is not None:
            try:
                await nc.close()
            except Exception as error:
                safe_log(f"coordinator NATS close failed: {error!r}")
        if lock is not None:
            lock.release()


def _operator_identity(environ) -> str:
    """Best-effort operator name for the reset audit line. NEVER raises.

    SPEC v0.6b-2 §6.4.2: the command must not fail on an unresolvable
    identity. The final fallback used a bare `os.getuid()`, which is the one
    expression here that was not guarded — so an identity failure could escape
    AFTER the marker had already been mutated, leaving a destructive action
    with no audit record. That is the exact outcome §6.4 exists to prevent.

    Unreachable on Linux, where getuid(2) always succeeds, but the guard costs
    nothing and the failure mode is silent data loss rather than a crash.
    """
    try:
        identity = os.getlogin()
        if identity:
            return identity
    except Exception:
        pass
    try:
        identity = getpass.getuser()
        if identity:
            return identity
    except Exception:
        pass
    from_env = environ.get("SUDO_USER") or environ.get("USER")
    if from_env:
        return from_env
    try:
        return f"uid:{os.getuid()}"
    except Exception:
        return "uid:unknown"


async def run_accept_durable_reset(
    environ=None, *, acknowledge_traffic_gap: bool = False, logger=print
) -> int:
    env = os.environ if environ is None else environ
    if resolve_mode(env) != "coordinator":
        raise CoordinatorStartupError("accept-durable-reset requires YUGO_MODE=coordinator")
    config = load_nats_config(env)
    state_path = (env.get("TASK_STATE_PATH") or "").strip()
    lock = nc = None
    try:
        lock = acquire_instance_lock(state_path)
        coordinator_state.migrate(lock.connection, lock.state_preflight)
        nc = await connect_nats(config)
        # Resolve the audit fields BEFORE the mutation, not after. If identity
        # resolution ever fails, the command must abort with the marker intact
        # rather than clear it and then die before recording who did so.
        audit = (
            f"operator={_operator_identity(env)} "
            f"timestamp={datetime.now(timezone.utc).isoformat()}"
        )
        traffic_gap = await coordinator_state.accept_reset(
            lock.connection,
            nc.jetstream(),
            acknowledge_traffic_gap=acknowledge_traffic_gap,
        )
        if traffic_gap:
            logger(
                f"coordinator durable marker cleared: {audit} "
                "acknowledged_risk=operator accepted the traffic gap while the durable was absent; "
                "next start will create a NEW-only durable"
            )
        else:
            logger(
                f"coordinator durable marker accepted and re-stamped from live generation: {audit} "
                "acknowledged_risk=none (generation re-stamp only)"
            )
        return 0
    finally:
        try:
            if nc is not None:
                await nc.close()
        finally:
            if lock is not None:
                lock.release()


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if args == ["accept-durable-reset"]:
            return asyncio.run(run_accept_durable_reset())
        if args == ["accept-durable-reset", "--acknowledge-traffic-gap"]:
            return asyncio.run(run_accept_durable_reset(acknowledge_traffic_gap=True))
        if args:
            raise CoordinatorStartupError(f"unknown coordinator command: {' '.join(args)}")
        return asyncio.run(run_coordinator())
    except Exception as error:
        print(f"coordinator startup failed: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

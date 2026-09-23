"""Durable envelope-id deduplication controls."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import asyncio
from datetime import datetime, timezone

import fleet_bus
import pytest


def test_dedup_survives_restart_reports_original_req_id_and_expires(tmp_path):
    path = tmp_path / "dedup.sqlite"
    first = fleet_bus.DurableEnvelopeDedupStore(str(path), ttl_s=fleet_bus.DEFAULT_DEDUP_TTL_S)
    initial = first.claim("env-1", "req-original", now_s=1000)
    assert initial[:2] == (False, "req-original")
    first.complete("env-1", initial[2])

    restarted = fleet_bus.DurableEnvelopeDedupStore(str(path), ttl_s=fleet_bus.DEFAULT_DEDUP_TTL_S)
    assert restarted.claim("env-1", "req-new", now_s=1050)[:2] == (True, "req-original")
    assert restarted.prune(fleet_bus.DEFAULT_DEDUP_TTL_S + 1001) == 1
    assert restarted.claim("env-1", "req-after-ttl", now_s=fleet_bus.DEFAULT_DEDUP_TTL_S + 1001)[:2] == (False, "req-after-ttl")


def test_concurrent_claim_has_exactly_one_winner(tmp_path):
    path = tmp_path / "dedup.sqlite"
    stores = [fleet_bus.DurableEnvelopeDedupStore(str(path)) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda item: item[1].claim("raced", item[0]), zip(("a", "b"), stores)))
    assert sum(not claim[0] for claim in claims) == 1
    assert sum(claim[0] for claim in claims) == 1
    assert claims[0][1] == claims[1][1]


def test_pending_lease_recovers_and_prune_uses_index_with_bounded_batch(tmp_path):
    path = tmp_path / "dedup.sqlite"
    crashed = fleet_bus.DurableEnvelopeDedupStore(str(path), ttl_s=fleet_bus.DEFAULT_DEDUP_TTL_S, lease_s=2)
    assert crashed.claim("pending", "original", now_s=100)[:2] == (False, "original")
    restarted = fleet_bus.DurableEnvelopeDedupStore(str(path), ttl_s=fleet_bus.DEFAULT_DEDUP_TTL_S, lease_s=2)
    assert restarted.claim("pending", "replacement", now_s=103)[:2] == (False, "original")
    plan = restarted._db.execute(
        "EXPLAIN QUERY PLAN SELECT rowid FROM envelope_dedup_v2 "
        "WHERE first_seen_s < ? ORDER BY first_seen_s LIMIT ?", (0, fleet_bus.DEDUP_PRUNE_LIMIT)
    ).fetchall()
    assert "envelope_dedup_v2_first_seen" in repr(plan)
    for index in range(101):
        claim = restarted.claim(f"old-{index}", f"req-{index}", now_s=0)
        restarted.complete(f"old-{index}", claim[2])
    prune_now = fleet_bus.DEFAULT_DEDUP_TTL_S + 100
    # Batches stay bounded, but the sweep LOOPS until the expired set is
    # drained. A single 100-row batch is what let the backlog grow ~156 rows
    # per 256 arrivals; draining in one sweep is the fix.
    assert restarted.prune(prune_now) == 101
    assert restarted.prune(prune_now) == 0


def test_prune_outpaces_a_steady_arrival_stream(tmp_path):
    """Ohm's reproduction, inverted into an invariant.

    1,024 unique claims with synthetic time advancing past the TTL between
    them retained 624 rows under the single-batch prune, when only the newest
    should be live. The cleanup must now keep the table bounded rather than
    growing with the stream.
    """
    # `:memory:` deliberately bypasses the seven-day floor, which is the only
    # way to advance synthetic time across many TTL windows in a unit test.
    ttl = 100
    store = fleet_bus.DurableEnvelopeDedupStore(":memory:", ttl_s=ttl)
    for index in range(1024):
        # Arrivals span many TTL windows, so by the end almost everything
        # written is expired — the steady-stream shape from the review.
        claim = store.claim(f"stream-{index}", f"req-{index}", now_s=float(index))
        store.complete(f"stream-{index}", claim[2])
    final_now = 1024.0
    store.prune(final_now)
    live = store._db.execute("SELECT COUNT(*) FROM envelope_dedup_v2").fetchone()[0]
    expired = store._db.execute(
        "SELECT COUNT(*) FROM envelope_dedup_v2 WHERE first_seen_s < ?", (final_now - ttl,)
    ).fetchone()[0]
    assert expired == 0, f"{expired} expired rows survived the sweep"
    assert live <= ttl + 1, (
        f"{live} rows retained for a {ttl}s TTL window — the table grew with the "
        "stream instead of being bounded by it"
    )


def test_prune_idle_sweeps_a_quiet_lane(tmp_path):
    """A stream that goes quiet after a burst must still shed its backlog."""
    path = tmp_path / "dedup.sqlite"
    store = fleet_bus.DurableEnvelopeDedupStore(str(path), ttl_s=fleet_bus.DEFAULT_DEDUP_TTL_S)
    for index in range(50):
        claim = store.claim(f"burst-{index}", f"req-{index}", now_s=0)
        store.complete(f"burst-{index}", claim[2])
    # No further claims arrive, so the every-N-claims trigger never fires.
    assert store.prune_idle(fleet_bus.DEFAULT_DEDUP_TTL_S + 100) == 50
    assert store._db.execute("SELECT COUNT(*) FROM envelope_dedup_v2").fetchone()[0] == 0


def test_renew_holds_a_live_owner_past_the_original_expiry(tmp_path):
    """The testable half of the P1: a still-running worker is not overlapped.

    This does NOT assert exactly-once execution. Under the at-least-once
    contract a crash after a side effect but before completion may duplicate;
    what must never happen is a SECOND worker starting while the first is
    alive and still executing.
    """
    path = tmp_path / "dedup.sqlite"
    owner_store = fleet_bus.DurableEnvelopeDedupStore(str(path), lease_s=2)
    rival = fleet_bus.DurableEnvelopeDedupStore(str(path), lease_s=2)
    duplicate, _, owner = owner_store.claim("long-turn", "original", now_s=100)
    assert duplicate is False

    # The turn outlives the original 2s lease, renewing as it goes.
    assert owner_store.renew("long-turn", owner, now_s=101) is True
    assert rival.claim("long-turn", "rival", now_s=102.5)[0] is True, "a live owner was overlapped"
    assert owner_store.renew("long-turn", owner, now_s=103) is True
    assert rival.claim("long-turn", "rival", now_s=104.5)[0] is True, "a live owner was overlapped"

    # Owner dies. Recovery still works — that is the at-least-once half.
    stolen = rival.claim("long-turn", "rival", now_s=200)
    assert stolen[0] is False, "a dead owner's envelope was never recovered"
    # And the original owner is now fenced: its completion must not land.
    assert owner_store.complete("long-turn", owner) is False


def test_complete_and_release_report_owner_loss(tmp_path):
    path = tmp_path / "dedup.sqlite"
    store = fleet_bus.DurableEnvelopeDedupStore(str(path), lease_s=2)
    _, _, owner = store.claim("env", "req", now_s=100)
    assert store.complete("env", owner) is True
    # The SAME owner completing twice. This is what `AND state='pending'`
    # buys, and without it the two ports disagree: True here, False in
    # TypeScript. The owner predicate alone never exercises the clause.
    assert store.complete("env", owner) is False
    assert store.complete("env", "someone-else") is False
    assert store.release("env", "someone-else") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_adapter_failure_or_cancellation_releases_pending_for_restart_retry(tmp_path, failure):
    path = tmp_path / "dedup.sqlite"
    config = fleet_bus.FleetBusConfig(
        bot_name="vec", url="nats://unused", user="vec", password="x",
        allowed_from=frozenset({"vec", "kat"}), plugin_version="test",
        audit_log_path=None, dedup_store_path=str(path),
    )
    wire = {
        "envelope_version": 1, "id": f"retry-{type(failure).__name__}",
        "from": "kat", "to": "vec", "kind": "text_message",
        "ts": datetime.now(timezone.utc).isoformat(), "payload": {},
    }

    async def fail(_envelope, _req_id):
        raise failure

    first = fleet_bus.FleetBus(config, fleet_bus.AuditLog(None), on_envelope=fail)
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await first._on_request("fleet.vec.request", wire)
    else:
        await first._on_request("fleet.vec.request", wire)

    delivered = []
    async def succeed(_envelope, req_id):
        delivered.append(req_id)
    restarted = fleet_bus.FleetBus(config, fleet_bus.AuditLog(None), on_envelope=succeed)
    await restarted._on_request("fleet.vec.request", wire)
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_a_slow_turn_keeps_a_rival_fenced_for_its_whole_duration(tmp_path):
    """The test the P1 actually demands: fencing through the BUS, not the store.

    The previous round's renewal tests called `store.renew()` directly, so the
    entire renewal wiring could be deleted from `FleetBus` with both suites
    still green. This drives a real turn that outlives its lease several times
    over and asserts a second consumer is refused THROUGHOUT, then admitted
    once the owner is gone.
    """
    path = tmp_path / "dedup.sqlite"
    config = fleet_bus.FleetBusConfig(
        bot_name="vec", url="nats://unused", user="vec", password="x",
        allowed_from=frozenset({"vec", "kat"}), plugin_version="test",
        audit_log_path=None, dedup_store_path=str(path),
    )
    wire = {
        "envelope_version": 1, "id": "slow-turn", "from": "kat", "to": "vec",
        "kind": "text_message", "ts": datetime.now(timezone.utc).isoformat(),
        "payload": {},
    }
    rival = fleet_bus.DurableEnvelopeDedupStore(str(path), lease_s=1.0)
    observed: list[bool] = []

    async def slow(_envelope, _req_id):
        # Six lease-lengths of work. Without renewal the lease lapses after the
        # first and the rival takes the envelope mid-turn. A 1s lease gives
        # 600ms of headroom between a renewal tick and the probe; at 200ms the
        # margin was 120ms, which a stalled event loop on a shared CI runner
        # can eat — a fencing test that flakes reads as "renewal is broken".
        for _ in range(6):
            await asyncio.sleep(1.0)
            observed.append(rival.claim("slow-turn", "rival")[0])
        return None

    bus = fleet_bus.FleetBus(config, fleet_bus.AuditLog(None), on_envelope=slow)
    # Short lease so the turn genuinely outlives it; the bus derives its
    # renewal cadence from the store, so this is all that needs setting.
    bus._dedup = fleet_bus.DurableEnvelopeDedupStore(str(path), lease_s=1.0)
    await bus._on_request("fleet.vec.request", wire)

    assert observed, "the turn never ran"
    assert all(observed), (
        "a rival consumer claimed the envelope while the owner was still "
        f"executing — renewal is not wired into the turn: {observed}"
    )
    # Owner is done and the claim settled, so the wire id stays deduped.
    assert rival.claim("slow-turn", "late")[0] is True


@pytest.mark.asyncio
async def test_a_completion_store_fault_does_not_reject_the_handler(tmp_path):
    """Containment for completion, not just for claim.

    `claimDedup`'s fault guard was covered; `complete`/`release` were not, so
    removing their try/except left the suite green apart from a source-text
    regex. A SQLite fault at completion must be audited and swallowed.
    """
    path = tmp_path / "dedup.sqlite"
    config = fleet_bus.FleetBusConfig(
        bot_name="vec", url="nats://unused", user="vec", password="x",
        allowed_from=frozenset({"vec", "kat"}), plugin_version="test",
        audit_log_path=None, dedup_store_path=str(path),
    )
    wire = {
        "envelope_version": 1, "id": "complete-fault", "from": "kat", "to": "vec",
        "kind": "text_message", "ts": datetime.now(timezone.utc).isoformat(),
        "payload": {},
    }

    class FailingComplete(fleet_bus.DurableEnvelopeDedupStore):
        def complete(self, envelope_id: str, owner: str) -> bool:
            raise sqlite3.OperationalError("database is locked")

    async def ok(_envelope, _req_id):
        return None

    bus = fleet_bus.FleetBus(config, fleet_bus.AuditLog(None), on_envelope=ok)
    bus._dedup = FailingComplete(str(path))
    # Must not raise: a rejected handler is what ends the lane.
    await bus._on_request("fleet.vec.request", wire)


@pytest.mark.asyncio
async def test_the_claim_is_still_pending_while_the_reply_is_published(tmp_path):
    """Completion must happen AFTER outbound processing, not before.

    Completing first means a crash between the commit and the publish leaves a
    `completed` row with no answer sent, suppressing redelivery for the full
    TTL — the documented at-least-once duplicate becomes a permanently lost
    response. The real failure is a process death, so the observable assertion
    is the row's state at publish time rather than an injected exception.
    """
    path = tmp_path / "dedup.sqlite"
    config = fleet_bus.FleetBusConfig(
        bot_name="vec", url="nats://unused", user="vec", password="x",
        allowed_from=frozenset({"vec", "kat"}), plugin_version="test",
        audit_log_path=None, dedup_store_path=str(path),
    )
    wire = {
        "envelope_version": 1, "id": "publish-window", "from": "kat", "to": "vec",
        "kind": "text_message", "ts": datetime.now(timezone.utc).isoformat(),
        "payload": {},
    }
    state_at_publish: list[str] = []

    async def reply(_envelope, _req_id):
        return "an answer"

    bus = fleet_bus.FleetBus(config, fleet_bus.AuditLog(None), on_envelope=reply)

    async def spy(*_args, **_kwargs):
        row = bus._dedup._db.execute(
            "SELECT state FROM envelope_dedup_v2 WHERE envelope_id=?", ("publish-window",)
        ).fetchone()
        state_at_publish.append(row[0])
        return True  # the real helper returns delivery success; None reads as failure

    bus._publish_turn_output = spy
    await bus._on_request("fleet.vec.request", wire)

    assert state_at_publish == ["pending"], (
        "the claim was completed before the reply was published — a crash in "
        "that window loses the answer permanently"
    )
    settled = bus._dedup._db.execute(
        "SELECT state FROM envelope_dedup_v2 WHERE envelope_id=?", ("publish-window",)
    ).fetchone()
    assert settled[0] == "completed", "the claim was never settled after publishing"


@pytest.mark.asyncio
async def test_the_heartbeat_sweeps_expired_claims(tmp_path):
    """The idle prune must be WIRED, not merely implemented.

    The store-level `prune_idle` test calls the store directly, so deleting
    the call from the heartbeat left both suites green — the same shape as the
    renewal gap found the round before.
    """
    path = tmp_path / "dedup.sqlite"
    config = fleet_bus.FleetBusConfig(
        bot_name="vec", url="nats://unused", user="vec", password="x",
        allowed_from=frozenset({"vec", "kat"}), plugin_version="test",
        audit_log_path=None, dedup_store_path=str(path),
    )
    bus = fleet_bus.FleetBus(config, fleet_bus.AuditLog(None))
    bus._dedup = fleet_bus.DurableEnvelopeDedupStore(":memory:", ttl_s=10)
    for index in range(20):
        claim = bus._dedup.claim(f"stale-{index}", f"req-{index}", now_s=0)
        bus._dedup.complete(f"stale-{index}", claim[2])
    assert bus._dedup._db.execute("SELECT COUNT(*) FROM envelope_dedup_v2").fetchone()[0] == 20
    # Drive the BEAT, not the helper. Calling `_sweep_expired_claims` directly
    # tests the method and leaves the wiring unproven — which is exactly how
    # the previous round's renewal fix passed while being disconnected.
    await bus._publish_heartbeat()
    assert bus._dedup._db.execute("SELECT COUNT(*) FROM envelope_dedup_v2").fetchone()[0] == 0


def test_a_sweep_fault_never_stops_the_heartbeat(tmp_path):
    """Liveness outranks cleanup: a store fault must be audited, not raised."""
    config = fleet_bus.FleetBusConfig(
        bot_name="vec", url="nats://unused", user="vec", password="x",
        allowed_from=frozenset({"vec", "kat"}), plugin_version="test",
        audit_log_path=None, dedup_store_path=":memory:",
    )
    bus = fleet_bus.FleetBus(config, fleet_bus.AuditLog(None))

    class ExplodingPrune(fleet_bus.DurableEnvelopeDedupStore):
        def prune_idle(self, now_s=None):
            raise sqlite3.OperationalError("database is locked")

    bus._dedup = ExplodingPrune(":memory:")
    assert bus._sweep_expired_claims() == 0  # must not raise


def _bus_config(path, **overrides):
    base = dict(
        bot_name="vec", url="nats://unused", user="vec", password="x",
        allowed_from=frozenset({"vec", "kat"}), plugin_version="test",
        audit_log_path=None, dedup_store_path=str(path),
    )
    base.update(overrides)
    return fleet_bus.FleetBusConfig(**base)


def _wire(envelope_id):
    return {
        "envelope_version": 1, "id": envelope_id, "from": "kat", "to": "vec",
        "kind": "text_message", "ts": datetime.now(timezone.utc).isoformat(),
        "payload": {},
    }


@pytest.mark.asyncio
async def test_cancellation_during_publish_leaves_the_claim_recoverable(tmp_path):
    """A cancel mid-publish must NOT stamp `completed`.

    Moving completion after `_publish_turn_output` was meant to stop a crash
    in that window from losing the reply. A `finally` that completes
    unconditionally recreates exactly that tombstone, reached by an orderly
    shutdown instead of a hard kill: the answer never went out, yet the row
    says done and redelivery is suppressed for the whole TTL. Under the
    at-least-once contract a repeated effect is permitted; a lost response is
    not.
    """
    path = tmp_path / "dedup.sqlite"
    bus = fleet_bus.FleetBus(
        _bus_config(path), fleet_bus.AuditLog(None),
        on_envelope=lambda _e, _r: asyncio.sleep(0, result="an answer"),
    )
    publishing = asyncio.Event()

    async def block_before_sending(*_args, **_kwargs):
        publishing.set()
        await asyncio.sleep(3600)  # nothing has gone on the wire yet

    bus._publish_turn_output = block_before_sending
    task = asyncio.create_task(bus._on_request("fleet.vec.request", _wire("cancelled-publish")))
    await asyncio.wait_for(publishing.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = bus._dedup._db.execute(
        "SELECT state FROM envelope_dedup_v2 WHERE envelope_id=?", ("cancelled-publish",)
    ).fetchone()
    assert row is None or row[0] != "completed", (
        "a cancel before the reply was sent stamped the claim completed — the "
        "response is lost and redelivery is suppressed for the full TTL"
    )
    rival = fleet_bus.DurableEnvelopeDedupStore(str(path))
    assert rival.claim("cancelled-publish", "retry")[0] is False, (
        "the envelope is not recoverable after an interrupted publish"
    )


@pytest.mark.asyncio
async def test_a_publish_failure_leaves_the_claim_recoverable(tmp_path):
    """Same rule for an ordinary outbound fault, not only cancellation.

    The previous version completed deliberately here, reasoning that replaying
    burns a second model call. That trades a LOST answer for a saved call,
    which is backwards under a contract that accepts duplicates and not losses.
    """
    path = tmp_path / "dedup.sqlite"
    bus = fleet_bus.FleetBus(
        _bus_config(path), fleet_bus.AuditLog(None),
        on_envelope=lambda _e, _r: asyncio.sleep(0, result="an answer"),
    )

    async def explode(*_args, **_kwargs):
        raise RuntimeError("publish dispatch blew up")

    bus._publish_turn_output = explode
    await bus._on_request("fleet.vec.request", _wire("failed-publish"))

    rival = fleet_bus.DurableEnvelopeDedupStore(str(path))
    assert rival.claim("failed-publish", "retry")[0] is False, (
        "an answer that never reached the wire was stamped completed"
    )


@pytest.mark.asyncio
async def test_cancellation_during_the_hop_warning_does_not_orphan_the_renewer(tmp_path):
    """The renewer must die with the turn, wherever the turn is interrupted.

    Renewal starts before `_warn_origin` so the claim is covered across that
    publish. If the await sits outside the cleanup scope, a cancel there leaks
    the task, which then extends an abandoned pending claim for as long as the
    process lives — nothing else can ever recover that envelope.
    """
    path = tmp_path / "dedup.sqlite"
    bus = fleet_bus.FleetBus(_bus_config(path), fleet_bus.AuditLog(None), on_envelope=None)
    warning = asyncio.Event()

    async def block_warning(*_args, **_kwargs):
        warning.set()
        await asyncio.sleep(3600)

    bus._warn_origin = block_warning
    wire = _wire("cancelled-warning")
    wire["hops"] = fleet_bus.BATON_HOPS_WARN_AT

    before = len(asyncio.all_tasks())
    task = asyncio.create_task(bus._on_request("fleet.vec.request", wire))
    await asyncio.wait_for(warning.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    leaked = [t for t in asyncio.all_tasks() if not t.done() and "_renew_claim_until_done" in repr(t)]
    assert not leaked, f"renewal task outlived the cancelled turn: {leaked}"
    assert len(asyncio.all_tasks()) <= before + 1
    rival = fleet_bus.DurableEnvelopeDedupStore(str(path))
    assert rival.claim("cancelled-warning", "retry")[0] is False, (
        "the envelope is not recoverable after a cancel during the hop warning"
    )


@pytest.mark.asyncio
async def test_a_second_cancel_during_cleanup_still_settles_the_claim(tmp_path):
    """The settle decision must not sit behind an await.

    A cancel delivered while the cleanup path is suspended raises straight out
    of the `finally`, skipping the decision and leaving the claim `pending`
    with an unrenewed lease — recoverable only once that lease lapses, instead
    of immediately. `asyncio.shield` does NOT prevent this: it protects the
    awaited task, not the awaiting coroutine, and its extra scheduling hop is
    where the second cancel lands. Ordering is the fix.
    """
    path = tmp_path / "dedup.sqlite"
    bus = fleet_bus.FleetBus(
        _bus_config(path), fleet_bus.AuditLog(None),
        on_envelope=lambda _e, _r: asyncio.sleep(0, result="an answer"),
    )
    publishing = asyncio.Event()

    async def block_before_sending(*_args, **_kwargs):
        publishing.set()
        await asyncio.sleep(3600)

    bus._publish_turn_output = block_before_sending
    task = asyncio.create_task(bus._on_request("fleet.vec.request", _wire("double-cancel")))
    await asyncio.wait_for(publishing.wait(), timeout=5)

    # Two cancels: the first interrupts the publish, the second arrives while
    # cleanup is running — the window the shield was wrongly believed to close.
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = bus._dedup._db.execute(
        "SELECT state FROM envelope_dedup_v2 WHERE envelope_id=?", ("double-cancel",)
    ).fetchone()
    assert row is None, (
        f"the settle decision was skipped — row left as {row} with an unrenewed "
        "lease, recoverable only after it lapses instead of immediately"
    )
    rival = fleet_bus.DurableEnvelopeDedupStore(str(path))
    assert rival.claim("double-cancel", "retry")[0] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["transport_error", "disconnected"])
async def test_a_transport_publish_failure_leaves_the_claim_recoverable(tmp_path, mode):
    """Failure at the REAL NATS boundary, not a throwing dispatcher stub.

    `publish_request` catches transport errors and returns False rather than
    raising, and returns False when disconnected. Watching only for an
    exception escaping `_publish_turn_output` therefore missed genuine
    delivery failures entirely: the claim was stamped `completed`, zero
    replies shipped, and the envelope stayed suppressed for the whole TTL.

    The previous publish-failure test replaced `_publish_turn_output` with a
    raising stub, which bypasses exactly the failure conversion that needed
    exercising. This one fails at `nc.publish` with the real helpers in place.
    """
    path = tmp_path / "dedup.sqlite"
    bus = fleet_bus.FleetBus(
        _bus_config(path), fleet_bus.AuditLog(None),
        on_envelope=lambda _e, _r: asyncio.sleep(0, result="an answer"),
    )

    class FakeNats:
        def __init__(self, closed):
            self.is_closed = closed
            self.publishes = 0

        async def publish(self, *_args, **_kwargs):
            self.publishes += 1
            raise OSError("transport failed")

    bus._nc = FakeNats(closed=(mode == "disconnected"))
    await bus._on_request("fleet.vec.request", _wire(f"transport-{mode}"))

    row = bus._dedup._db.execute(
        "SELECT state FROM envelope_dedup_v2 WHERE envelope_id=?", (f"transport-{mode}",)
    ).fetchone()
    assert row is None or row[0] != "completed", (
        f"{mode}: the claim was stamped completed although no answer reached the wire"
    )
    rival = fleet_bus.DurableEnvelopeDedupStore(str(path))
    assert rival.claim(f"transport-{mode}", "retry")[0] is False, (
        f"{mode}: an undelivered answer is not recoverable — suppressed for the full TTL"
    )

"""Durable envelope-id deduplication controls."""

from concurrent.futures import ThreadPoolExecutor
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
    # The budget is the only ceiling, and it is deliberately above the
    # every-N-claims trigger so a steady stream loses ground on each sweep.
    assert fleet_bus.DEDUP_PRUNE_BUDGET > fleet_bus.DEDUP_PRUNE_EVERY


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

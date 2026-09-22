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
    assert restarted.prune(prune_now) == 100
    assert restarted.prune(prune_now) <= 1


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

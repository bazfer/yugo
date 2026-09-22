"""Durable envelope-id deduplication controls."""

from concurrent.futures import ThreadPoolExecutor

import fleet_bus


def test_dedup_survives_restart_reports_original_req_id_and_expires(tmp_path):
    path = tmp_path / "dedup.sqlite"
    first = fleet_bus.DurableEnvelopeDedupStore(str(path), ttl_s=100)
    assert first.claim("env-1", "req-original", now_s=1000) == (False, "req-original")

    restarted = fleet_bus.DurableEnvelopeDedupStore(str(path), ttl_s=100)
    assert restarted.claim("env-1", "req-new", now_s=1050) == (True, "req-original")
    assert restarted.claim("env-1", "req-after-ttl", now_s=1101) == (False, "req-after-ttl")


def test_concurrent_claim_has_exactly_one_winner(tmp_path):
    path = tmp_path / "dedup.sqlite"
    stores = [fleet_bus.DurableEnvelopeDedupStore(str(path)) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda item: item[1].claim("raced", item[0]), zip(("a", "b"), stores)))
    assert sum(not duplicate for duplicate, _req_id in claims) == 1
    assert sum(duplicate for duplicate, _req_id in claims) == 1
    assert claims[0][1] == claims[1][1]

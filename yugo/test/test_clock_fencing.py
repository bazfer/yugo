"""SPEC-26 protocol cases. Clock injection affects the actual named API."""
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
import dedup_store as d
import fleet_bus as f
import dedup_verification as v
from dedup_admin import provision

BOOT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def clock(monkeypatch):
    state = [100_000]
    monkeypatch.setattr(d.time, "monotonic_ns", lambda: state[0] * 1_000_000)
    return state


@pytest.fixture
def store(clock):
    return f.DurableEnvelopeDedupStore(":memory:", lease_s=60)


def row(store):
    return store._db.execute("SELECT * FROM envelope_dedup_v2 WHERE envelope_id='e'").fetchone()


def test_1_wall_forward_before_renewal_refuses(store):
    initial = store.claim("e", "original", 100)
    assert store.claim("e", "rival", 1000)[0] is True
    assert row(store)[4] == initial[2]


def test_2_same_boot_monotonic_expiry(store, clock):
    a = store.claim("e", "original", 100)
    clock[0] += 60_000
    b = store.claim("e", "rival", 100)
    assert b[0] is False and b[2] != a[2] and b[1] == "original"
    assert row(store)[6:] == (v.read_boot_id(), clock[0] + 60_000)


def test_3_different_boot_immediate_takeover(store):
    store.claim("e", "original", 100)
    store._db.execute("UPDATE envelope_dedup_v2 SET lease_boot_id=?", (OTHER,))
    assert store.claim("e", "rival", 100)[0] is False
    assert row(store)[6] == v.read_boot_id()


def test_4_legacy_row_wall_predicate_and_upgrade(store):
    store._db.execute("INSERT INTO envelope_dedup_v2 VALUES ('e',100,'original','pending','old',160,'',0)")
    assert store.claim("e", "rival", 159)[0] is True
    assert store.claim("e", "rival", 160)[0] is False
    assert row(store)[6:] == (v.read_boot_id(), 160_000)


def test_5_backward_wall_does_not_extend_hold(store, clock):
    store.claim("e", "original", 100)
    clock[0] += 60_000
    assert store.claim("e", "rival", -1000)[0] is False


@pytest.mark.parametrize("path", ["target", "periodic", "idle", "direct"])
def test_6_pending_exempt_every_ttl_path(store, path):
    a = store.claim("e", "original", 100)
    done = store.claim("done", "done", 100)
    store.complete("done", done[2])
    future = f.DEFAULT_DEDUP_TTL_S * 10
    if path == "target":
        assert store.claim("e", "rival", future)[0] is True
    elif path == "periodic":
        store._claims = d.DEDUP_PRUNE_EVERY - 1
        store.claim("trigger", "trigger", future)
    elif path == "idle":
        assert store.prune_idle(future) == 1
    else:
        assert store.prune(future) == 1
    assert row(store)[4] == a[2]


def test_7_serialized_renewal_then_takeover_and_reverse(tmp_path, monkeypatch, clock):
    path = str(tmp_path / "db")
    record = path + ".record"
    evidence = dict(device_path="fixture", mount_point="unresolved", fstype="unresolved",
                    mount_id_source="unresolved", backing="local-virtual", determined_by="fixture",
                    inspected_at="2026-09-24T19:00:00Z")
    provision(path, record, "python", "test", evidence, [dict(process="test", user="test", path=path, method="fixture")])
    monkeypatch.setenv("YUGO_DEDUP_VERIFICATION_RECORD", record)
    a, b = f.DurableEnvelopeDedupStore(path), f.DurableEnvelopeDedupStore(path)
    first = a.claim("e", "original", 100)
    clock[0] += 59_000
    assert a.renew("e", first[2], 159)
    clock[0] += 2000
    assert b.claim("e", "rival", 161)[0] is True
    clock[0] += 60_000
    second = b.claim("e", "rival", 221)
    assert second[0] is False
    assert a.renew("e", first[2], 221) is False


# Frozen Release-1 statement: release 2 deliberately refuses six-column startup.
# Mutation tests alter this fixture for the historical compatibility assertion;
# current production named INSERT coverage is the wider-table probe below.
R1_INSERT = "INSERT OR IGNORE INTO envelope_dedup_v2 (envelope_id,first_seen_s,req_id,state,lease_owner,lease_until_s) VALUES (?,?,?,'pending',?,?)"


@pytest.mark.parametrize("columns", [6, 8])
def test_8_release1_named_insert_six_and_eight(columns):
    db = sqlite3.connect(":memory:")
    v.create_schema(db, "python")
    if columns == 6:
        db.execute("ALTER TABLE envelope_dedup_v2 DROP COLUMN lease_until_mono_ms")
        db.execute("ALTER TABLE envelope_dedup_v2 DROP COLUMN lease_boot_id")
    assert db.execute(R1_INSERT, ("e", 100, "req", "owner", 160)).rowcount == 1


def test_8_current_insert_named_columns_wider_table_issue35(store):
    # Deliberately extend AFTER verified startup to discriminate named vs
    # positional INSERT; this is not permission for a live production migration.
    store._db.execute("ALTER TABLE envelope_dedup_v2 ADD COLUMN future TEXT DEFAULT ''")
    assert store.claim("e", "req", 100)[0] is False


def test_10_stop_all_accessors_cutover_required_deleted_equals_never_seen(store):
    store.claim("e", "req", 100)
    # Release 1's old prune SQL really deletes pending, despite a live mono lease.
    store._db.execute("DELETE FROM envelope_dedup_v2 WHERE first_seen_s < ?", (1000,))
    absent = store._db.execute("SELECT * FROM envelope_dedup_v2 WHERE envelope_id='e'").fetchall()
    never = store._db.execute("SELECT * FROM envelope_dedup_v2 WHERE envelope_id='never'").fetchall()
    assert json.dumps(absent).encode() == json.dumps(never).encode() == b"[]"
    # Demonstration, not a runtime detection guarantee.
    assert store.claim("e", "again", 1000)[0] is False


def test_11_stop_all_accessors_cutover_required_stale_metadata_matches_legitimate(store, clock):
    store.claim("e", "req", 100)
    original = row(store)
    store._db.execute("UPDATE envelope_dedup_v2 SET lease_owner='replacement',lease_until_s=1060 WHERE envelope_id='e' AND lease_until_s<=1000")
    stale = row(store)
    legitimate = (*original[:4], "replacement", 1060, *original[6:])
    assert stale == legitimate
    store._validate_metadata(stale[6], stale[7])
    assert store.claim("e", "rival", 1001)[0] is True
    # No generation/owner binding exists in this protocol. Not universal proof:
    # this concrete stale row matches the chosen legitimate row in EVERY column.
    assert len(stale) == 8


@pytest.mark.parametrize("boot,deadline", [(OTHER, 0), (OTHER, -1), (OTHER, "bad"),
                                          (OTHER, 1.5), ("garbage", 160000)])
def test_11a_malformed_new_format_refuses_reports(store, boot, deadline):
    store.claim("e", "req", 100)
    store._db.execute("UPDATE envelope_dedup_v2 SET lease_boot_id=?,lease_until_mono_ms=?", (boot, deadline))
    before = row(store)
    with pytest.raises(v.VerificationError, match="malformed"):
        store.claim("e", "rival", 1000)
    assert row(store) == before


def test_12_rollback_release1_functions_but_loses_safety(store):
    store.claim("e", "req", 100)
    # Exact old takeover shape: schema compatibility is NOT protocol safety.
    changed = store._db.execute(
        "UPDATE envelope_dedup_v2 SET lease_owner=?,lease_until_s=? WHERE envelope_id=? AND state='pending' AND lease_until_s<=?",
        ("old-rival", 1060, "e", 1000)).rowcount
    assert changed == 1
    assert row(store)[4] == "old-rival"
    assert row(store)[7] == 160000  # Previous owner's metadata survives.


def test_14_boot_failure_live_new_row_and_fresh_refuse(store, monkeypatch):
    store.claim("e", "req", 100)
    before = row(store)
    def unreadable():
        raise OSError("boot unreadable")
    monkeypatch.setattr(d, "read_boot_id", unreadable)
    for envelope in ("e", "fresh"):
        with pytest.raises(OSError, match="boot unreadable"):
            store.claim(envelope, "rival", 1000)
    assert row(store) == before
    assert store._db.execute("SELECT COUNT(*) FROM envelope_dedup_v2").fetchone()[0] == 1


def test_commit_failure_never_authorizes_and_rolls_back(store):
    real = store._db
    class FailCommit:
        def execute(self, sql, *args):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("commit failure")
            return real.execute(sql, *args)
        def __getattr__(self, key):
            return getattr(real, key)
    store._db = FailCommit()
    with pytest.raises(sqlite3.OperationalError, match="commit failure"):
        store.claim("e", "req", 100)
    assert row(store) is None


def test_zero_row_insert_never_authorizes(store):
    store._db.execute("CREATE TRIGGER prevent_insert BEFORE INSERT ON envelope_dedup_v2 BEGIN SELECT RAISE(IGNORE); END")
    with pytest.raises(v.VerificationError, match="zero rows"):
        store.claim("e", "req", 100)


def test_zero_row_takeover_never_authorizes(store, clock):
    store.claim("e", "req", 100)
    before = row(store)
    clock[0] += 60000
    store._db.execute("CREATE TRIGGER prevent_update BEFORE UPDATE ON envelope_dedup_v2 BEGIN SELECT RAISE(IGNORE); END")
    assert store.claim("e", "rival", 160)[0] is True
    assert row(store) == before

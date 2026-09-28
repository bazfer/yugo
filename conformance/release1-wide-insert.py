"""Issue #35 historical Release-1 regression; run with PYTHONPATH=<R1>/yugo.

Uses that checkout's real claim implementation, not a copied SQL statement.
Release 2's current eight-column writer has a separate wider-table regression.
"""
import sqlite3
import tempfile
from pathlib import Path
from fleet_bus import DurableEnvelopeDedupStore

with tempfile.TemporaryDirectory(prefix="yugo-r1-wide-") as directory:
    path = str(Path(directory) / "dedup.sqlite")
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE envelope_dedup_v2 (
            envelope_id TEXT PRIMARY KEY, first_seen_s REAL NOT NULL,
            req_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','completed')),
            lease_owner TEXT NOT NULL, lease_until_s REAL NOT NULL)""")
        db.execute("ALTER TABLE envelope_dedup_v2 ADD COLUMN future TEXT DEFAULT ''")
    store = DurableEnvelopeDedupStore(path)
    try:
        duplicate, req_id, owner = store.claim("wide", "original", 100)
        assert not duplicate and req_id == "original" and owner
        assert store._db.execute("SELECT future FROM envelope_dedup_v2").fetchone() == ("",)
    finally:
        store._db.close()
print("PASS: Release-1 claim accepts a seven-column table")

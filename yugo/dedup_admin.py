"""Explicit operator-only provisioning and offline migration; never imported by a consumer."""
from __future__ import annotations

from contextlib import closing
import argparse
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from dedup_verification import (
    SCHEMAS, VerificationError, create_schema, fingerprint, read_boot_id,
    require, resolve_uuids, validate_record,
)


def open_existing(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    canonical = Path(path).resolve(strict=True)
    return sqlite3.connect(canonical.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                           uri=True, isolation_level=None)


def migrate(store: str, backup: str, port: str) -> None:
    """Caller must stop and prevent restart of ALL accessors before invocation."""
    require(port in SCHEMAS, "explicit supported port required")
    with closing(open_existing(store)) as db:
        old = {"table": "envelope_dedup_v2", "columns": SCHEMAS[port]["columns"][:6]}
        require(fingerprint(db) == old, "migration requires exactly the old six-column schema; an eight-column store needs record-only attestation")
        # Reserve exclusively: never overwrite a previous backup.
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        dest = sqlite3.connect(backup)
        try:
            db.backup(dest)
        finally:
            dest.close()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("ALTER TABLE envelope_dedup_v2 ADD COLUMN lease_boot_id TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE envelope_dedup_v2 ADD COLUMN lease_until_mono_ms INTEGER NOT NULL DEFAULT 0")
            require(fingerprint(db) == SCHEMAS[port], "migration schema mismatch")
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise


def provision(store: str, record_path: str, port: str, attested_by: str,
              evidence: dict, inventory: list[dict], *, record_only: bool = False) -> dict:
    """Operator supplies inspected evidence, never a consumer assertion."""
    require(port in SCHEMAS, "explicit supported port required")
    resolved_store = Path(store).resolve()
    resolved_record = Path(record_path).resolve()
    require(str(resolved_record) not in {str(resolved_store), str(resolved_store) + "-wal", str(resolved_store) + "-shm"},
            "record must not overwrite store or SQLite sidecars")
    db = None
    try:
        if record_only:
            db = open_existing(store, readonly=True)
        else:
            # O_EXCL protects existing stores, including files at symlink targets.
            fd = os.open(store, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            db = open_existing(store)
            require(db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal", "WAL unavailable")
            db.execute("BEGIN IMMEDIATE")
            try:
                create_schema(db, port)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        require(fingerprint(db) == SCHEMAS[port], "record-only/provision requires full supported schema")
        require(db.execute("PRAGMA journal_mode").fetchone()[0] == "wal", "WAL inactive")
        canonical = str(Path(store).resolve(strict=True))
        stat = os.stat(canonical)
        try:
            uuids = sorted(resolve_uuids(stat.st_dev))
            resolution = "resolved via /dev/disk/by-uuid" if uuids else "no by-uuid target matches st_dev"
        except OSError as exc:
            uuids = []
            resolution = f"by-uuid unavailable: {exc}"
        device = ({"binding": "uuid", "fs_uuid": uuids[0]} if uuids else {
            "binding": "devno", "major": os.major(stat.st_dev), "minor": os.minor(stat.st_dev),
            "attested_boot_id": read_boot_id(),
        })
        record = {
            "record_version": 1, "canonical_path": canonical, "device": device,
            "inode": str(stat.st_ino), "port": port, "schema_fingerprint": fingerprint(db),
            "storage_evidence": {**evidence, "uuid_resolution": resolution},
            "participant_inventory": inventory, "attested_by": attested_by,
            "attested_at": datetime.now(timezone.utc).isoformat(),
        }
        validate_record(record)
        # Atomic record publication, never partially overwrite a valid record.
        import tempfile
        target = Path(record_path)
        with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, prefix=target.name + ".", delete=False) as out:
            temporary = out.name
            try:
                json.dump(record, out, indent=2)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            except BaseException:
                os.unlink(temporary)
                raise
        try:
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return record
    finally:
        if db is not None:
            db.close()


def run() -> None:
    parser = argparse.ArgumentParser(prog="yugo")
    dedup = parser.add_subparsers(dest="group", required=True).add_parser("dedup")
    commands = dedup.add_subparsers(dest="command", required=True)
    provision_parser = commands.add_parser("provision")
    provision_parser.add_argument("--store", required=True)
    provision_parser.add_argument("--record", required=True)
    provision_parser.add_argument("--port", choices=tuple(SCHEMAS), required=True)
    provision_parser.add_argument("--record-only", action="store_true")
    provision_parser.add_argument("--attested-by", required=True)
    migration = commands.add_parser("migrate")
    migration.add_argument("--store", required=True)
    migration.add_argument("--backup", required=True)
    migration.add_argument("--port", choices=tuple(SCHEMAS), required=True)
    args = parser.parse_args()
    print("ALL accessors must be stopped and prevented from restarting. This command cannot prove that.")
    require(input("Type STOPPED after verifying the per-file inventory: ") == "STOPPED", "accessor shutdown not confirmed")
    if args.command == "migrate":
        migrate(args.store, args.backup, args.port)
        print("Migrated with WAL-aware backup. Re-attest using yugo dedup provision --record-only before startup.")
        return
    # These are inspection aids, NOT automatic locality verdicts. Containers may
    # hide the backing devices and other accessors; the operator must trace host
    # mounts, effective configuration, loaded modules and restartable processes.
    for command in (["findmnt", "-T", args.store], ["lsblk", "-o", "NAME,TYPE,TRAN"],
                    ["lsof", args.store]):
        print("$ " + " ".join(command), flush=True)
        try:
            subprocess.run(command, check=False)
        except OSError as exc:
            print(f"Inspection unavailable here: {exc}; obtain host-side evidence.")
    print("Trace container mounts to host backing storage. Inspect running processes, loaded modules,")
    print("effective configuration and open files; include restartable/prune accessors.")
    print("Enter storage_evidence JSON (device_path, mount_point, fstype, mount_id_source, backing,")
    print("determined_by, inspected_at). If mount ID is unresolved, say unresolved; descriptive only.")
    evidence = json.loads(input())
    print("Enter nonempty participant_inventory JSON array: [{process,user,path,method}, ...]")
    inventory = json.loads(input())
    require(input("Type ATTEST to attest this inspected evidence: ") == "ATTEST", "operator did not attest")
    record = provision(args.store, args.record, args.port, args.attested_by,
                       evidence, inventory, record_only=args.record_only)
    print(f"Record written ({record['device']['binding']} binding). This is attestation, not proof of locality.")


def main() -> None:
    """The ONLY entry point, so the two callers cannot diverge on error output.

    `bin/yugo` imports and calls this directly, which used to bypass the
    handler below when it lived under `if __name__ == "__main__"` — an operator
    who mistyped the storage evidence got a traceback instead of the sentence
    naming the fault, on the error path of a command startup now requires
    (yugo#56).
    """
    try:
        run()
    except (VerificationError, OSError, ValueError, sqlite3.Error) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()

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
from typing import NamedTuple

from dedup_verification import (
    SCHEMAS, VerificationError, create_schema, fingerprint, read_boot_id,
    operator_evidence, refuse, require, resolve_uuids, validate_operator_inputs,
    validate_record,
)


# The three FIXED-NAME auxiliary files SQLite derives from a store path.
# `-journal` is here because a store that cannot do WAL falls back to it, and
# these rules must hold for all three rather than the two WAL mode uses. Not an
# exhaustive list of what SQLite can put beside a database: a transaction
# spanning attached databases also writes a `-mj<random>` super-journal, which
# has no fixed name and which provisioning cannot produce, since it opens one
# database and attaches none.
SIDECARS = ("-wal", "-shm", "-journal")

# What `create_schema` puts in a fresh store, so "nothing here but the empty
# store this command would have created" can be CHECKED rather than inferred.
# TYPE AND NAME, because a name alone is not an identity: dropping the index and
# creating a populated TABLE under its name passed a name-only allowlist and the
# file was then offered for deletion (Ohm, PR #65). SQLite's own `sqlite_%`
# objects are excluded, and it refuses that prefix to `CREATE` — "object name
# reserved for internal use" — so the exclusion cannot hide an operator's table.
# `PRAGMA writable_schema` can still forge one; nothing here defends against a
# hostile store, and §8 does not claim to.
PROVISIONED_OBJECTS = frozenset({("table", "envelope_dedup_v2"),
                                 ("index", "envelope_dedup_v2_first_seen")})


def open_existing(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    canonical = Path(path).resolve(strict=True)
    return sqlite3.connect(canonical.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                           uri=True, isolation_level=None)


def occupancy(db: sqlite3.Connection) -> str | None:
    """None when the file holds nothing but the empty store provisioning creates.

    Otherwise a phrase naming what else is in it. Two widenings, both because the
    narrower check was carrying a broader claim (Ohm, PR #65): `fingerprint`
    reads `PRAGMA table_info(envelope_dedup_v2)` alone, so a file can match it and
    hold the operator's own populated tables beside it; and matching `sqlite_master`
    on NAME alone let a populated table wear the expected index's name. The row
    count is read through the same `mode=ro` handle, which recovers an
    uncheckpointed `-wal` and so counts rows that live only there — verified,
    because a count that silently skipped them would understate the file again.

    What this establishes, exactly: no schema object is present that could hold
    operator data, and the dedup table is empty. NOT that the DDL is byte-identical
    to `create_schema`'s — an allowlisted index could be over another column. That
    is deliberate, since the claim the caller builds on this is about data.
    """
    extra = sorted(f"{kind} {name}" for kind, name
                   in db.execute("SELECT type, name FROM sqlite_master")
                   if not name.startswith("sqlite_") and (kind, name) not in PROVISIONED_OBJECTS)
    if extra:
        return "the file also holds " + ", ".join(extra)
    rows = db.execute("SELECT COUNT(*) FROM envelope_dedup_v2").fetchone()[0]
    return f"it already holds {rows} dedup row(s)" if rows else None


def beside(store: str) -> list[str]:
    """The sidecar paths that exist beside `store` AT THE MOMENT THIS IS CALLED.

    Not "the sidecars the operator had": reading a WAL database through a
    `mode=ro` handle creates `-wal` and `-shm`, and a read-only handle cannot
    remove them on close — measured. `survey` below snapshots this before it opens
    anything, precisely so the two can be told apart.
    """
    return [store + suffix for suffix in SIDECARS if os.path.lexists(store + suffix)]


class Artifact(NamedTuple):
    """One on-disk path the retry found, and what this command established about it."""
    path: str
    kind: str
    detail: str


# How an artifact is classified. Deletion advice is DERIVED from these rather than
# assembled per branch, because seven findings on this change were one shape: a
# claim in the advice reaching past the check behind it. Per branch, the author of
# each new branch had to re-derive the safety argument and twice got it wrong — a
# cold `-journal` SQLite never read, and this command's own `-wal`. With one
# classifier the argument is made once and the advice cannot outrun it.
ACCOUNTED = "accounted"            # contents established to hold nothing to lose
APPEARED_HERE = "appeared here"    # absent when this command started looking
UNACCOUNTED = "unaccounted"        # has bytes, predates this run, contents unestablished


def classify_store(store: str, port: str) -> Artifact:
    """The main database file. The only artifact whose contents are ever read."""
    try:
        if os.path.getsize(store) == 0:
            return Artifact(store, ACCOUNTED, "zero bytes, holding no database at all")
        with closing(open_existing(store, readonly=True)) as db:
            if fingerprint(db) != SCHEMAS[port]:
                return Artifact(store, UNACCOUNTED,
                                f"not a {port}-port store with the supported schema, so "
                                "--record-only would refuse it too")
            occupied = occupancy(db)
    except (OSError, sqlite3.Error) as exc:
        return Artifact(store, UNACCOUNTED, f"a file this command could not inspect ({exc})")
    if occupied is not None:
        return Artifact(store, UNACCOUNTED, f"a store with the supported schema, but {occupied}")
    return Artifact(store, ACCOUNTED, "a store with the supported schema, no dedup rows and no "
                                      "schema object provisioning did not create")


def classify_sidecar(path: str, preexisting: frozenset[str]) -> Artifact:
    """A sidecar. Its CONTENTS are never established, so it is only ever cleared
    by being empty or by being this command's own.

    The store's row count says nothing about these files, and the tempting claim
    that it does was the seventh finding here: SQLite ignores a cold or truncated
    `-journal`, and a `-wal` whose header does not match the database it sits
    beside, so a file can hold bytes that no read of the store ever touched.
    """
    if path not in preexisting:
        # Deliberately not "this command created it". What was checked is that it
        # was absent when this command started looking and is here now, and the
        # only thing that ran in between is this command's own read. The
        # accessor-shutdown attestation is what makes those the same statement,
        # and an attestation is not a mechanism — so the wording stays at what the
        # snapshot establishes.
        return Artifact(path, APPEARED_HERE,
                        "absent when this command started looking, so it appeared during this "
                        "command's own read of the store")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return Artifact(path, UNACCOUNTED, f"a sidecar this command could not stat ({exc})")
    if size == 0:
        return Artifact(path, ACCOUNTED, "a zero-byte sidecar")
    return Artifact(path, UNACCOUNTED,
                    f"{size} bytes that predate this command, which has not read them")


def survey(store: str, port: str) -> list[Artifact]:
    """The store and every `SIDECARS` path beside it that exists, each classified.

    Not every file SQLite could conceivably have put there: a `-mj<random>`
    super-journal has no fixed name and is not enumerated. Nothing here claims
    otherwise, and the advice below speaks only of the paths it lists.
    """
    # Snapshot BEFORE anything opens the store, which is what makes
    # CREATED_HERE distinguishable from UNACCOUNTED at all.
    preexisting = frozenset(beside(store))
    found = [classify_store(store, port)]
    # Read the sidecars AFTER the inspection, because the inspection adds to them
    # and the operator has to clear those too.
    found += [classify_sidecar(path, preexisting) for path in beside(store)]
    return found


def describe_obstruction(store: str, port: str) -> tuple[str, str]:
    """What the retry found at `store`, and the recovery for it (yugo#60).

    O_EXCL failing used to surface as a bare `File exists`, which reads as a new
    and unrelated fault — and the file may well be the residue of the operator's
    own previous attempt at this same command. Both halves are derived from
    `survey`: the situation lists what was found, and deletion is offered only
    when nothing found is UNACCOUNTED.
    """
    found = survey(store, port)
    unaccounted = [item for item in found if item.kind == UNACCOUNTED]
    situation = "; ".join(f"{item.path} is {item.detail}" for item in found)
    record_only = ("re-run with --record-only, the one mode that touches an existing store, which "
                   "refuses unless that store is exactly the supported schema")
    if unaccounted:
        return (situation,
                "deletion is NOT offered here: " + ", ".join(item.path for item in unaccounted)
                + " holds content this command has not accounted for. Re-attest in place instead — "
                + record_only)
    return (situation,
            f"either {record_only}; or delete "
            + ", ".join(item.path for item in found)
            + " — every path listed was either found to hold nothing, or appeared during this "
              "command's own read — and re-run this command unchanged")



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
    # Publishing the record is an `os.replace`, so a `--record` aimed at the store
    # or any of its sidecars would put the record where a file this command never
    # created belongs. The family, not the two members WAL mode happens to use —
    # `-journal` was missing here, found while sweeping the discard for the same
    # shape (Codex, PR #65).
    require(str(resolved_record) not in {str(resolved_store)}
            | {str(resolved_store) + suffix for suffix in SIDECARS},
            "record must not overwrite store or SQLite sidecars")
    # Refuse what the operator typed BEFORE creating anything (yugo#60). The
    # record's other fields are read off the store and cannot be checked yet, so
    # this covers the refusals an operator actually hits on a first attempt —
    # mistyped evidence, a bad `backing`, a malformed inventory, and the two
    # --record shapes checked just below. Each used to leave an O_EXCL store
    # behind, and the retry then failed on the path instead of succeeding.
    validate_operator_inputs(operator_evidence(evidence), inventory, attested_by)
    require(resolved_record.parent.is_dir(), f"record directory {resolved_record.parent} must exist")
    # `os.replace` onto a directory fails after the store is already created, and
    # the shape of the path needs no store to judge, so this moves up here too.
    # Every case moved out of the discard is a case the discard cannot get wrong
    # (Ohm, PR #65). Permissions on that directory are NOT pre-flighted —
    # `os.access` lies under ACLs and for root — so a write failure there is still
    # a post-create refusal, and the discard handles it. Replacing an existing
    # record FILE stays allowed: that is re-attestation, and the publication is
    # deliberately atomic.
    require(not resolved_record.is_dir(), f"--record {resolved_record} is a directory, not a file")
    created = None
    created_identity = None
    db = None
    try:
        if record_only:
            db = open_existing(store, readonly=True)
        else:
            # A sidecar can predate this command where O_EXCL proves the main path
            # does not: a `<store>-wal` left behind by a database since lost is
            # exactly that, and such a WAL can hold committed transactions the
            # main file never got. SQLite DELETES it the instant it opens the empty
            # store this command would create. The trigger is nameable, so a reader
            # can re-derive this instead of trusting it: `sqlite3.connect()` leaves
            # the orphan intact, and the NEXT line's `PRAGMA journal_mode=WAL`
            # deletes it (SQLite 3.45.1 here, 3.46.1 in review — independently
            # measured both times). Not a documented SQLite guarantee, which argues
            # for refusing rather than for relying on it; either way the deletion
            # happens before any cleanup could run, so the refusal comes before the
            # create. Gated on the store being absent, because a live store with
            # its own WAL is the O_EXCL case below and must get that message
            # instead (Codex, PR #65).
            if not os.path.lexists(store):
                for suffix in SIDECARS:
                    sidecar = str(resolved_store) + suffix
                    require(not os.path.lexists(sidecar),
                            f"{sidecar} exists but {resolved_store} does not, so that path holds a "
                            f"SQLite sidecar with no database beside it — a partly deleted or partly "
                            f"restored store. Provisioning here would create an empty store and "
                            f"SQLite would consume that file on opening it, and a `-wal` in that "
                            f"state can be the only copy of committed transactions. Recover it, or "
                            f"move it aside deliberately, and then")
            # O_EXCL protects existing stores, including files at symlink targets.
            try:
                fd = os.open(store, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                situation, recovery = describe_obstruction(str(resolved_store), port)
                refuse(f"a store already exists at {resolved_store}, so this run created nothing "
                       f"and changed nothing; {situation}; to recover, {recovery}")
            os.close(fd)
            created = str(resolved_store)
            # What the discard below may remove, recorded rather than assumed:
            # the one path O_EXCL just made, pinned by identity. It deliberately
            # does NOT list the sidecars — closing the last handle is measured to
            # remove the `-wal` and `-shm` this run created, so an unlink loop
            # here would add nothing but an ownership guess from "absent a moment
            # ago", and that guess is how the first version came to delete an
            # orphaned WAL. (SQLite is no judge of ownership either: it deletes
            # an orphaned WAL just as readily, which is why the pre-flight above
            # refuses rather than relying on anything downstream.)
            created_identity = os.stat(created)
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
    except BaseException:
        # Belt to the pre-flight's braces: what reaches here is whatever the
        # pre-flight could not judge without a store — no WAL, unreadable boot
        # identity, a failed schema commit, a stat or open that errored, an IO
        # failure writing the record. Illustrative, not exhaustive, and the point
        # is the `except BaseException`: none of them may leave a store. Closing
        # the handle
        # removes the `-wal` and `-shm` this run created; this removes exactly one
        # more path, and only while that path still holds the file O_EXCL made. The
        # first version assumed O_EXCL spoke for the sidecars too and deleted a
        # WAL that predated the command (Codex, PR #65), so nothing here is
        # inferred from a file merely having been absent a moment ago.
        if created is not None:
            if db is not None:
                db.close()
                db = None
            try:
                now = os.stat(created)
                if (now.st_dev, now.st_ino) == (created_identity.st_dev, created_identity.st_ino):
                    os.unlink(created)
            except OSError:
                pass        # whatever survives, the retry's own message names it
        raise
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

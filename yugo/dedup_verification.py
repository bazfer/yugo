"""Operator-attested storage verification with startup drift detection.

Not proof of locality, continuous enforcement, or a security boundary. Consumers
never create/repair records or file-backed stores. See SPEC-26 §8.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

BOOT_PATH = "/proc/sys/kernel/random/boot_id"
TIMENS_PATH = "/proc/self/timens_offsets"
UUID_DIRECTORY = "/dev/disk/by-uuid"
PORT = "python"
PROVISION_HINT = "run yugo dedup provision"
BOOT_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.ASCII)
SCHEMAS = {
    port: {"table": "envelope_dedup_v2", "columns": [
        {"name": "envelope_id", "declared_type": "TEXT"},
        {"name": f"first_seen_{unit}", "declared_type": kind},
        {"name": "req_id", "declared_type": "TEXT"},
        {"name": "state", "declared_type": "TEXT"},
        {"name": "lease_owner", "declared_type": "TEXT"},
        {"name": f"lease_until_{unit}", "declared_type": kind},
        {"name": "lease_boot_id", "declared_type": "TEXT"},
        {"name": "lease_until_mono_ms", "declared_type": "INTEGER"},
    ]} for port, unit, kind in [("python", "s", "REAL"), ("typescript", "ms", "INTEGER")]
}


class VerificationError(RuntimeError):
    pass


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise VerificationError(f"dedup verification refused: {reason}; {PROVISION_HINT}")


def valid_boot_id(value: Any) -> bool:
    return isinstance(value, str) and BOOT_RE.fullmatch(value) is not None


def read_boot_id() -> str:
    value = Path(BOOT_PATH).read_text().strip()
    require(valid_boot_id(value), "invalid local boot identity")
    return value


def check_clock_domain() -> None:
    lines = Path(TIMENS_PATH).read_text().splitlines()
    parsed: dict[str, tuple[int, int]] = {}
    for line in lines:
        match = re.fullmatch(r"(monotonic|boottime)\s+(-?[0-9]+)\s+(-?[0-9]+)\s*", line, re.ASCII)
        require(match is not None, "unparseable timens_offsets line")
        assert match is not None
        key = match[1]
        require(key not in parsed, "duplicate timens_offsets clock")
        parsed[key] = (int(match[2]), int(match[3]))
    require(set(parsed) == {"monotonic", "boottime"}, "incomplete timens_offsets")
    require(parsed["monotonic"] == (0, 0), "non-zero monotonic offset")


def exact_fields(value: Any, fields: set[str], where: str) -> None:
    require(type(value) is dict and set(value) == fields, f"invalid {where} fields")


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def timestamp(value: Any) -> bool:
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})", value
    ) is None:
        return False
    try:
        datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def validate_record(record: Any) -> dict:
    exact_fields(record, {"record_version", "canonical_path", "device", "inode", "port",
                         "schema_fingerprint", "storage_evidence", "participant_inventory",
                         "attested_by", "attested_at"}, "record")
    require(type(record["record_version"]) is int and record["record_version"] == 1, "record version")
    require(isinstance(record["canonical_path"], str) and os.path.isabs(record["canonical_path"]), "canonical path")
    inode = record["inode"]
    require(isinstance(inode, str) and re.fullmatch(r"[0-9]+", inode, re.ASCII) is not None, "inode decimal string")
    require(int(inode) <= (1 << 64) - 1, "inode out of range")
    require(record["port"] in ("python", "typescript"), "record port")
    device = record["device"]
    require(type(device) is dict, "device object")
    if device.get("binding") == "uuid":
        exact_fields(device, {"binding", "fs_uuid"}, "UUID device")
        require(nonempty(device["fs_uuid"]), "filesystem UUID")
    else:
        exact_fields(device, {"binding", "major", "minor", "attested_boot_id"}, "devno device")
        require(device["binding"] == "devno", "device binding")
        require(all(type(device[k]) is int and 0 <= device[k] <= 0xffffffff for k in ("major", "minor")), "device integers")
        require(valid_boot_id(device["attested_boot_id"]), "attested boot identity")
    fingerprint = record["schema_fingerprint"]
    exact_fields(fingerprint, {"table", "columns"}, "fingerprint")
    require(fingerprint["table"] == "envelope_dedup_v2" and type(fingerprint["columns"]) is list, "fingerprint shape")
    for column in fingerprint["columns"]:
        exact_fields(column, {"name", "declared_type"}, "column")
        require(nonempty(column["name"]) and nonempty(column["declared_type"]), "column strings")
    evidence = record["storage_evidence"]
    exact_fields(evidence, {"device_path", "mount_point", "fstype", "mount_id_source", "backing",
                            "determined_by", "uuid_resolution", "inspected_at"}, "storage evidence")
    require(all(nonempty(v) for v in evidence.values()), "storage evidence strings")
    require(evidence["backing"] in ("local-block", "local-virtual"), "network or unknown backing")
    require(timestamp(evidence["inspected_at"]), "inspection timestamp")
    inventory = record["participant_inventory"]
    require(type(inventory) is list and bool(inventory), "participant inventory")
    for participant in inventory:
        exact_fields(participant, {"process", "user", "path", "method"}, "participant")
        require(all(nonempty(v) for v in participant.values()), "participant strings")
    require(nonempty(record["attested_by"]) and timestamp(record["attested_at"]), "attestation")
    return record


def load_record(path: str) -> dict:
    def reject_constant(value: str) -> None:
        raise VerificationError(f"invalid JSON constant: {value}")
    return validate_record(json.loads(Path(path).read_text(), parse_constant=reject_constant))


def resolve_uuids(device: int) -> list[str]:
    """No mountinfo/path matching. Kernel-resolved st_dev vs target st_rdev."""
    matches = []
    for entry in Path(UUID_DIRECTORY).iterdir():
        try:
            if entry.stat().st_rdev == device:
                matches.append(entry.name)
        except OSError:
            continue
    return matches


def check_device(record: dict, stat: os.stat_result) -> None:
    device = record["device"]
    if device["binding"] == "uuid":
        try:
            uuids = resolve_uuids(stat.st_dev)
        except OSError:
            uuids = []
        require(device["fs_uuid"].lower() in [v.lower() for v in uuids], "unresolved or changed filesystem UUID")
    else:
        require((device["major"], device["minor"]) == (os.major(stat.st_dev), os.minor(stat.st_dev)), "device mismatch")
        require(device["attested_boot_id"] == read_boot_id(), "devno attestation is from another boot")


def fingerprint(db: sqlite3.Connection) -> dict:
    return {"table": "envelope_dedup_v2", "columns": [
        {"name": row[1], "declared_type": row[2]}
        for row in db.execute("PRAGMA table_info(envelope_dedup_v2)")
    ]}


def check_schema(db: sqlite3.Connection, record: dict, port: str) -> None:
    live = fingerprint(db)
    require(live == record["schema_fingerprint"], "live/record schema mismatch")
    require(live == SCHEMAS[port], "unsupported schema for running port")


def create_schema(db: sqlite3.Connection, port: str) -> None:
    """Provisioning / in-memory only; never called on file-backed startup."""
    unit, kind = ("s", "REAL") if port == "python" else ("ms", "INTEGER")
    db.execute(f"""CREATE TABLE envelope_dedup_v2 (
        envelope_id TEXT PRIMARY KEY, first_seen_{unit} {kind} NOT NULL,
        req_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','completed')),
        lease_owner TEXT NOT NULL, lease_until_{unit} {kind} NOT NULL,
        lease_boot_id TEXT NOT NULL DEFAULT '',
        lease_until_mono_ms INTEGER NOT NULL DEFAULT 0)""")
    db.execute(f"CREATE INDEX envelope_dedup_v2_first_seen ON envelope_dedup_v2(first_seen_{unit})")


def open_verified_store(path: str, record_path: str | None = None) -> sqlite3.Connection:
    """Return the sole verified RW handle; close it on every post-open failure."""
    db = None
    try:
        check_clock_domain()
        # Probe before opening any database, including :memory:. A process
        # whose native clock is broken must never advertise healthy startup.
        mono_ns = time.monotonic_ns()
        require(type(mono_ns) is int and mono_ns >= 0
                and mono_ns // 1_000_000 <= 2**53 - 1,
                "invalid startup monotonic clock")
        if path == ":memory:":
            read_boot_id()
            db = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
            create_schema(db, PORT)
            return db
        record_path = record_path or os.environ.get("YUGO_DEDUP_VERIFICATION_RECORD")
        require(bool(record_path), "missing verification record")
        record = load_record(record_path)
        require(record["port"] == PORT, "port mismatch")
        canonical = str(Path(path).resolve(strict=True))
        require(canonical == record["canonical_path"], "canonical path mismatch")
        check_device(record, os.stat(path))
        before = os.stat(path)
        require(before.st_ino == int(record["inode"]), "inode mismatch")
        # URI quoting is supplied by as_uri; mode=rw excludes SQLITE_OPEN_CREATE.
        db = sqlite3.connect(Path(canonical).as_uri() + "?mode=rw", uri=True,
                             isolation_level=None, check_same_thread=False)
        after = os.stat(path)
        require((after.st_dev, after.st_ino) == (before.st_dev, before.st_ino), "store changed while opening")
        require(after.st_ino == int(record["inode"]), "post-open inode mismatch")
        check_device(record, after)
        require(db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal", "WAL inactive")
        read_boot_id()
        check_schema(db, record, PORT)
        db.execute("PRAGMA busy_timeout=5000")
        return db
    except Exception as exc:
        if db is not None:
            db.close()
        if isinstance(exc, VerificationError):
            raise
        raise VerificationError(f"dedup verification refused: {exc}; {PROVISION_HINT}") from exc

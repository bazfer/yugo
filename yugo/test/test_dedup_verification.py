"""SPEC-26 §8 gates. No test attestation is evidence about a production store."""
import copy
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

import dedup_verification as v
import dedup_admin as admin

BOOT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    boot = tmp_path / "boot"
    clock = tmp_path / "clock"
    boot.write_text(BOOT)
    clock.write_text("monotonic 0 0\nboottime 0 0\n")
    monkeypatch.setattr(v, "BOOT_PATH", str(boot))
    monkeypatch.setattr(v, "TIMENS_PATH", str(clock))
    monkeypatch.setattr(v, "UUID_DIRECTORY", str(tmp_path / "no-uuid"))
    monkeypatch.delenv("YUGO_DEDUP_VERIFICATION_RECORD", raising=False)
    store, record = tmp_path / "store.sqlite", tmp_path / "record.json"
    evidence = dict(device_path="operator-inspected test fixture", mount_point="unresolved",
                    fstype="unresolved", mount_id_source="unresolved", backing="local-virtual",
                    determined_by="test fixture only", inspected_at="2026-09-24T19:00:00Z")
    inventory = [dict(process="test", user="test", path=str(store), method="fixture")]
    data = admin.provision(str(store), str(record), "python", "test", evidence, inventory)
    return SimpleNamespace(store=store, record=record, data=data, clock=clock, boot=boot,
                           evidence=evidence, inventory=inventory)


def write(e):
    e.record.write_text(json.dumps(e.data))


def open_store(e):
    return v.open_verified_store(str(e.store), str(e.record))


def test_valid_record_one_rw_handle_and_descriptive_fields_not_compared(environment):
    e = environment
    e.data["storage_evidence"].update(mount_point="/not/the/mount", fstype="descriptive-only")
    write(e)
    db = open_store(e)
    assert v.fingerprint(db) == v.SCHEMAS["python"]
    db.close()


def test_17_nonzero_monotonic_refuses(environment):
    e = environment
    e.clock.write_text("monotonic 0 1\nboottime 0 0\n")
    with pytest.raises(v.VerificationError, match="non-zero"):
        open_store(e)


@pytest.mark.parametrize("contents", [None, "monotonic garbage 0\nboottime 0 0\n",
                                     "monotonic 0 0\nboottime 0 nope\n",
                                     "monotonic 0 0\n", "monotonic 0 0\nmonotonic 0 0\n"])
def test_18_absent_or_unparseable_clock_refuses(environment, contents):
    e = environment
    if contents is None:
        e.clock.unlink()
    else:
        e.clock.write_text(contents)
    with pytest.raises(v.VerificationError):
        open_store(e)


@pytest.mark.parametrize("change", ["missing", "malformed", "version", "unknown"])
def test_19_record_failures(environment, change):
    e = environment
    if change == "missing":
        e.record.unlink()
    elif change == "malformed":
        e.record.write_text("{")
    else:
        e.data["record_version" if change == "version" else "extra"] = 2
        write(e)
    with pytest.raises(v.VerificationError):
        open_store(e)


def test_20_inode_above_2pow53_exact_and_one_off_refuses(environment, monkeypatch):
    e = environment
    high = 2**53 + 1
    e.data["inode"] = str(high)
    write(e)
    original = os.stat
    actual = original(e.store)
    def stat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if str(path) == str(e.store):
            return SimpleNamespace(st_ino=high, st_dev=actual.st_dev, st_mode=result.st_mode)
        return result
    monkeypatch.setattr(v.os, "stat", stat)
    assert v.load_record(str(e.record))["inode"] == str(high)
    open_store(e).close()
    e.data["inode"] = str(high + 1)
    write(e)
    with pytest.raises(v.VerificationError, match="inode mismatch"):
        open_store(e)


def test_21_port_mismatch_refuses(environment):
    e = environment
    e.data["port"] = "typescript"
    write(e)
    with pytest.raises(v.VerificationError, match="port mismatch"):
        open_store(e)


def test_22_absent_store_never_created_even_at_open_race(environment, monkeypatch):
    e = environment
    connect = sqlite3.connect
    def remove_then_connect(path, *args, **kwargs):
        e.store.unlink()
        return connect(path, *args, **kwargs)
    monkeypatch.setattr(v.sqlite3, "connect", remove_then_connect)
    with pytest.raises(v.VerificationError):
        open_store(e)
    assert not e.store.exists()


def test_23_postopen_replacement_refuses_and_closes(environment, monkeypatch):
    e = environment
    connect = sqlite3.connect
    handles = []
    def replace_after_open(*args, **kwargs):
        db = connect(*args, **kwargs)
        handles.append(db)
        e.store.rename(e.store.with_suffix(".old"))
        shutil.copyfile(e.store.with_suffix(".old"), e.store)
        return db
    monkeypatch.setattr(v.sqlite3, "connect", replace_after_open)
    with pytest.raises(v.VerificationError, match="changed while opening"):
        open_store(e)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        handles[0].execute("SELECT 1")


@pytest.mark.parametrize("backing", ["network", "unknown"])
def test_24_network_unknown_refuses(environment, backing):
    e = environment
    e.data["storage_evidence"]["backing"] = backing
    write(e)
    with pytest.raises(v.VerificationError, match="backing"):
        open_store(e)


def make_six(e):
    db = sqlite3.connect(e.store, isolation_level=None)
    db.execute("ALTER TABLE envelope_dedup_v2 DROP COLUMN lease_until_mono_ms")
    db.execute("ALTER TABLE envelope_dedup_v2 DROP COLUMN lease_boot_id")
    db.execute("INSERT INTO envelope_dedup_v2 VALUES ('env',1,'req','pending','owner',60)")
    return db


def test_25_six_column_live_eight_column_record_refuses(environment):
    e = environment
    make_six(e).close()
    with pytest.raises(v.VerificationError, match="live/record"):
        open_store(e)


def test_26_interrupted_migration_atomic_preserves_rows(environment, monkeypatch):
    e = environment
    make_six(e).close()
    original = admin.open_existing
    class Interrupted:
        def __init__(self):
            self.db = original(str(e.store))
        def execute(self, sql, *args):
            if "ADD COLUMN lease_until_mono_ms" in sql:
                raise RuntimeError("interrupted")
            return self.db.execute(sql, *args)
        def __getattr__(self, name):
            return getattr(self.db, name)
    monkeypatch.setattr(admin, "open_existing", lambda *a, **k: Interrupted())
    with pytest.raises(RuntimeError, match="interrupted"):
        admin.migrate(str(e.store), str(e.store) + ".backup", "python")
    with original(str(e.store)) as db:
        assert len(v.fingerprint(db)["columns"]) == 6
        assert db.execute("SELECT * FROM envelope_dedup_v2").fetchone() == ("env", 1, "req", "pending", "owner", 60)
    with pytest.raises(v.VerificationError):
        open_store(e)


def test_27_memory_exempts_only_file_checks(environment):
    v.open_verified_store(":memory:").close()
    environment.clock.write_text("monotonic 1 0\nboottime 0 0\n")
    with pytest.raises(v.VerificationError, match="non-zero"):
        v.open_verified_store(":memory:")


def test_28_devno_reboot_same_numbers_refuses(environment):
    e = environment
    e.data["device"]["attested_boot_id"] = OTHER
    write(e)
    with pytest.raises(v.VerificationError, match="another boot"):
        open_store(e)


def test_28_uuid_resolution_failure_never_downgrades(environment):
    e = environment
    e.data["device"] = dict(binding="uuid", fs_uuid="not-resolvable")
    write(e)
    with pytest.raises(v.VerificationError, match="UUID"):
        open_store(e)


@pytest.mark.parametrize("device", [dict(binding="uuid", fs_uuid="abc", major=8),
                                   dict(binding="devno", major="8:1", minor=1, attested_boot_id=BOOT)])
def test_28_mixed_or_string_device_refuses(environment, device):
    e = environment
    e.data["device"] = device
    write(e)
    with pytest.raises(v.VerificationError):
        open_store(e)


def test_29a_mountinfo_representation_is_never_read(environment, monkeypatch):
    read = Path.read_text
    def no_mountinfo(path, *args, **kwargs):
        assert re.fullmatch(r"/proc/(?:self|thread-self|[0-9]+)/mountinfo", str(path)) is None
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", no_mountinfo)
    open_store(environment).close()


def test_29b_resolved_device_change_refuses(environment, monkeypatch):
    e = environment
    original = os.stat
    def overmount(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if str(path) == str(e.store):
            return SimpleNamespace(st_ino=result.st_ino, st_dev=os.makedev(999, 999), st_mode=result.st_mode)
        return result
    monkeypatch.setattr(v.os, "stat", overmount)
    with pytest.raises(v.VerificationError, match="device mismatch"):
        open_store(e)


def test_30_six_live_matching_six_record_still_refuses(environment):
    e = environment
    db = make_six(e)
    e.data["schema_fingerprint"] = v.fingerprint(db)
    db.close()
    write(e)
    with pytest.raises(v.VerificationError, match="unsupported schema"):
        open_store(e)


def test_31_record_only_half_migration_writes_nothing(environment):
    e = environment
    db = make_six(e)
    db.execute("ALTER TABLE envelope_dedup_v2 ADD COLUMN lease_boot_id TEXT NOT NULL DEFAULT ''")
    db.close()
    before = e.record.read_bytes()
    with pytest.raises(v.VerificationError, match="full supported"):
        admin.provision(str(e.store), str(e.record), "python", "test", e.evidence, e.inventory, record_only=True)
    assert e.record.read_bytes() == before


def test_32_wal_backup_preserves_uncheckpointed_rows_main_copy_loses(environment):
    e = environment
    db = make_six(e)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("INSERT INTO envelope_dedup_v2 VALUES ('wal-only',2,'req','pending','owner',60)")
    main_copy = str(e.store) + ".unsafe-copy"
    shutil.copyfile(e.store, main_copy)
    backup = str(e.store) + ".safe-backup"
    admin.migrate(str(e.store), backup, "python")
    with sqlite3.connect(backup) as restored:
        assert restored.execute("SELECT COUNT(*) FROM envelope_dedup_v2 WHERE envelope_id='wal-only'").fetchone()[0] == 1
    with sqlite3.connect(main_copy) as unsafe:
        assert unsafe.execute("SELECT COUNT(*) FROM envelope_dedup_v2 WHERE envelope_id='wal-only'").fetchone()[0] == 0
    db.close()


def test_missing_boot_still_refuses_memory(environment):
    environment.boot.unlink()
    with pytest.raises(v.VerificationError):
        v.open_verified_store(":memory:")


def test_uuid_binding_uses_rdev_and_is_case_insensitive(environment, monkeypatch):
    e = environment
    e.data["device"] = {"binding": "uuid", "fs_uuid": "ABcd"}
    write(e)
    monkeypatch.setattr(v, "resolve_uuids", lambda dev: ["abcd"])
    open_store(e).close()


def test_record_must_not_overwrite_store(environment):
    e = environment
    with pytest.raises(v.VerificationError, match="overwrite"):
        admin.provision(str(e.store), str(e.store), "python", "test", e.evidence, e.inventory, record_only=True)


@pytest.mark.parametrize("memory", [False, True])
@pytest.mark.parametrize("reading", [None, -1, True, 1.5, (2**53) * 1_000_000])
def test_startup_clock_failure_refuses_before_database_open(environment, monkeypatch, memory, reading):
    import fleet_bus
    e = environment
    monkeypatch.setenv("YUGO_DEDUP_VERIFICATION_RECORD", str(e.record))
    before = e.store.read_bytes()

    def clock():
        if reading is None:
            raise OSError("native monotonic clock unavailable")
        return reading

    def forbidden_open(*args, **kwargs):
        pytest.fail("startup opened SQLite before validating the clock")

    monkeypatch.setattr(v.time, "monotonic_ns", clock)
    monkeypatch.setattr(v.sqlite3, "connect", forbidden_open)
    with pytest.raises(fleet_bus.FleetBusConfigError, match="monotonic clock"):
        fleet_bus.DurableEnvelopeDedupStore(":memory:" if memory else str(e.store))
    assert e.store.read_bytes() == before


@pytest.mark.parametrize("reading", [0, (2**53 - 1) * 1_000_000])
def test_startup_clock_accepts_valid_boundaries(environment, monkeypatch, reading):
    monkeypatch.setattr(v.time, "monotonic_ns", lambda: reading)
    db = v.open_verified_store(":memory:")
    db.close()


# ---------- the operator CLI's error path (yugo#56) ----------

_CLI = Path(__file__).resolve().parents[2] / "bin" / "yugo"


def _run_cli(*args: str, stdin: str) -> subprocess.CompletedProcess:
    """Run `bin/yugo` as the operator does — a real process, not an import."""
    return subprocess.run([sys.executable, str(_CLI), *args],
                          input=stdin, capture_output=True, text=True)


def test_the_cli_reports_a_bad_attestation_as_a_message_not_a_traceback(tmp_path):
    """`bin/yugo` calls `dedup_admin.main()` directly, so a handler under
    `if __name__ == "__main__"` never ran for the only entry point operators
    use: a mistyped `backing` produced a full traceback. Provisioning is a
    mandatory step before a bus-enabled bot with a file-backed store starts, so
    this is the error path of a required command at the moment the input is
    most likely wrong.

    The absence of the traceback is the load-bearing assertion. A traceback's
    last line carries the message too, and it exits 1 all the same, so neither
    the message nor the exit code can tell the two apart on its own.
    """
    evidence = dict(device_path="/dev/nfs", mount_point="/var/lib/yugo", fstype="nfs4",
                    mount_id_source="unresolved", backing="nfs",   # not local-block/local-virtual
                    determined_by="test fixture only", inspected_at="2026-09-29T00:00:00Z")
    store, record = tmp_path / "store.sqlite", tmp_path / "record.json"
    inventory = [dict(process="test", user="test", path=str(store), method="fixture")]
    result = _run_cli("dedup", "provision", "--port", "python", "--attested-by", "test",
                      "--store", str(store), "--record", str(record),
                      stdin="STOPPED\n" + json.dumps(evidence) + "\n" + json.dumps(inventory) + "\nATTEST\n")

    assert "Traceback (most recent call last)" not in result.stderr + result.stdout, (
        f"the CLI raised through bin/yugo instead of reporting; stderr={result.stderr!r}"
    )
    # Exact and last: `lsof` writes its own warnings to this stream, so the
    # requirement is that the CLI's own last word is the one readable sentence.
    assert result.stderr.strip().splitlines()[-1] == (
        "dedup verification refused: network or unknown backing; run yugo dedup provision"
    ), f"stderr does not end with the refusal as one line; stderr={result.stderr!r}"
    assert result.returncode == 1, f"expected exit 1; got {result.returncode}"
    assert not record.exists(), "a refused attestation must not write a record"

"""SPEC-26 §8 gates. No test attestation is evidence about a production store."""
from contextlib import closing
import copy
import hashlib
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


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_record_must_not_overwrite_any_sqlite_sidecar(environment, suffix):
    """Publishing the record is an `os.replace`, so `--record` pointed at a
    sidecar overwrites a file provisioning never created. `-journal` was absent
    from this guard until the PR #65 sweep."""
    e = environment
    sidecar = Path(str(e.store) + suffix)
    sidecar.write_bytes(b"operator-owned bytes this command did not create")
    with pytest.raises(v.VerificationError, match="overwrite"):
        admin.provision(str(e.store), str(sidecar), "python", "test", e.evidence, e.inventory,
                        record_only=True)
    assert sidecar.read_bytes() == b"operator-owned bytes this command did not create"


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


# ---------- a refused provisioning leaves no store behind (yugo#60) ----------
#
# `provision` used to create the store with O_CREAT|O_EXCL before the record was
# validated, so input it refused still left the file on disk. The operator's
# retry of the corrected command then failed for an unrelated reason — the path
# exists — and that second message said nothing about the first. Since #51 this
# is the retry path of a command a bus-enabled bot cannot start without.


def _provision(store, record, evidence, inventory, attested_by="test", **kwargs):
    return admin.provision(str(store), str(record), "python", attested_by,
                           evidence, inventory, **kwargs)


def _residue(store):
    """Every path provisioning could have created for this store.

    `-journal` included: it was missing here, so "left nothing behind" quietly
    never checked it (Ohm, PR #65).
    """
    return [Path(str(store) + suffix) for suffix in ("", *admin.SIDECARS)]


@pytest.mark.parametrize("mutate, reason", [
    (lambda ev, inv: ev.update(backing="nfs"), "network or unknown backing"),
    (lambda ev, inv: ev.update(inspected_at="2026-09-29 00:00:00"), "inspection timestamp"),
    (lambda ev, inv: ev.update(mount_point="   "), "storage evidence strings"),
    (lambda ev, inv: ev.pop("fstype"), "invalid storage evidence fields"),
    (lambda ev, inv: ev.update(local=True), "invalid storage evidence fields"),
    (lambda ev, inv: inv.clear(), "participant inventory"),
    (lambda ev, inv: inv[0].pop("method"), "invalid participant fields"),
    (lambda ev, inv: inv[0].update(user=""), "participant strings"),
], ids=["network_backing", "unoffset_timestamp", "blank_evidence_value",
        "missing_evidence_key", "extra_evidence_key", "empty_inventory",
        "missing_participant_key", "blank_participant_value"])
def test_60_refused_operator_input_creates_no_store(environment, tmp_path, mutate, reason):
    """Each of these is a refusal the operator hits on the FIRST attempt, and
    none of them may leave a store for the second attempt to trip over."""
    e = environment
    store, record = tmp_path / "next-bot.sqlite", tmp_path / "next-bot.record.json"
    evidence, inventory = copy.deepcopy(e.evidence), copy.deepcopy(e.inventory)
    mutate(evidence, inventory)
    with pytest.raises(v.VerificationError, match=re.escape(reason)):
        _provision(store, record, evidence, inventory)
    for path in _residue(store):
        assert not path.exists(), f"a refused provisioning left {path.name} behind (yugo#60)"
    assert not record.exists(), "a refused provisioning must not write a record"


def test_60_a_blank_attestation_creates_no_store(environment, tmp_path):
    e = environment
    store, record = tmp_path / "next-bot.sqlite", tmp_path / "next-bot.record.json"
    with pytest.raises(v.VerificationError, match="attestation"):
        _provision(store, record, e.evidence, e.inventory, attested_by="  ")
    for path in _residue(store):
        assert not path.exists(), f"a refused provisioning left {path.name} behind (yugo#60)"


def test_60_an_unwritable_record_directory_creates_no_store(environment, tmp_path):
    """The other operator typo that used to leave a store: a `--record` path
    whose directory does not exist. The store was created, then the record
    write failed on the missing directory."""
    e = environment
    store = tmp_path / "next-bot.sqlite"
    record = tmp_path / "no-such-directory" / "next-bot.record.json"
    with pytest.raises(v.VerificationError, match="record directory"):
        _provision(store, record, e.evidence, e.inventory)
    for path in _residue(store):
        assert not path.exists(), f"a refused provisioning left {path.name} behind (yugo#60)"


def test_60_bad_operator_input_is_refused_before_anything_is_created(environment, tmp_path, monkeypatch):
    """The ordering, not only the outcome.

    The tests above are satisfied by a cleanup-on-failure path as well as by
    validating first, and a cleanup path leaves a window: a kill between the
    create and the cleanup puts the operator back in yugo#60. This asserts that
    for input provisioning can judge on its own, nothing is created at all.
    """
    e = environment
    store, record = tmp_path / "next-bot.sqlite", tmp_path / "next-bot.record.json"

    def forbidden_create(*args, **kwargs):
        pytest.fail("provisioning created a file before validating the operator's input")

    monkeypatch.setattr(admin.os, "open", forbidden_create)
    with pytest.raises(v.VerificationError, match="network or unknown backing"):
        _provision(store, record, {**e.evidence, "backing": "nfs"}, e.inventory)


@pytest.mark.parametrize("evidence", [[], "local-block", None, 7], ids=["list", "str", "none", "int"])
def test_60_evidence_that_is_not_an_object_is_refused_as_a_message(environment, tmp_path, evidence):
    """`json.loads` at the prompt happily returns a list, and the old ordering
    created the store and then raised `TypeError` out of `{**evidence}` — past
    the handler main() installs, so the operator got a traceback (yugo#56)."""
    e = environment
    store, record = tmp_path / "next-bot.sqlite", tmp_path / "next-bot.record.json"
    with pytest.raises(v.VerificationError, match="invalid storage evidence fields"):
        _provision(store, record, evidence, e.inventory)
    for path in _residue(store):
        assert not path.exists(), f"a refused provisioning left {path.name} behind (yugo#60)"


def test_60_evidence_carrying_the_spec_example_uuid_resolution_still_provisions(environment, tmp_path):
    """SPEC-26 §8.3.3 prints `uuid_resolution` inside the `storage_evidence`
    block, so an operator who copies it passes eight keys. Provisioning has
    always overwritten that one from the live filesystem, and the pre-flight
    added for yugo#60 must not turn a working call into a refusal."""
    e = environment
    store, record = tmp_path / "next-bot.sqlite", tmp_path / "next-bot.record.json"
    inventory = [dict(process="test", user="test", path=str(store), method="fixture")]
    data = _provision(store, record, {**e.evidence, "uuid_resolution": "copied from the spec"},
                      inventory)
    assert data["storage_evidence"]["uuid_resolution"] != "copied from the spec", (
        "the operator's uuid_resolution reached the record; it is read off the filesystem")
    assert v.load_record(str(record)) == data


def test_60_an_environmental_refusal_after_the_store_exists_removes_it(environment, tmp_path):
    """Not every refusal can be pre-flighted. The device binding is read off the
    store, so a `devno` binding needs the boot identity and the store has to
    exist before that code runs. A refusal there must still leave nothing
    behind, which the pre-flight cannot do and the discard must.

    `-wal` and `-shm` do exist by the time this refuses — measured — and it is
    closing the handle that removes them, not an unlink in the discard. That is
    why the discard lists only the store: an unlink loop there would add nothing
    but a guess that "absent a moment ago" means "mine".
    """
    e = environment
    e.boot.unlink()            # the fixture resolves no UUID, so this is the devno branch
    store, record = tmp_path / "next-bot.sqlite", tmp_path / "next-bot.record.json"
    with pytest.raises(FileNotFoundError):
        _provision(store, record, e.evidence, e.inventory)
    for path in _residue(store):
        assert not path.exists(), f"a refused provisioning left {path.name} behind (yugo#60)"
    assert not record.exists(), "a refused provisioning must not write a record"


def test_60_a_successful_provisioning_is_unchanged(environment, tmp_path):
    """The guard on the fix above: validating first must not stop a good
    provisioning from producing a store and a record that startup accepts."""
    e = environment
    store, record = tmp_path / "next-bot.sqlite", tmp_path / "next-bot.record.json"
    inventory = [dict(process="test", user="test", path=str(store), method="fixture")]
    data = _provision(store, record, e.evidence, inventory)
    assert store.exists() and record.exists()
    assert v.load_record(str(record)) == data
    assert data["canonical_path"] == str(store.resolve())
    db = v.open_verified_store(str(store), str(record))
    assert v.fingerprint(db) == v.SCHEMAS["python"]
    db.close()


# A store can still be left behind by a kill or a failure the pre-flight cannot
# anticipate. The retry must then name THAT situation and its recovery, rather
# than reporting a bare "file exists" that reads as a new and unrelated fault.


def test_60_a_zero_byte_leftover_names_the_situation_and_the_recovery(environment, tmp_path):
    """What a kill between the O_EXCL create and the schema write leaves."""
    e = environment
    store, record = tmp_path / "killed.sqlite", tmp_path / "killed.record.json"
    os.close(os.open(store, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    with pytest.raises(v.VerificationError) as caught:
        _provision(store, record, e.evidence, e.inventory)
    message = str(caught.value)
    assert "already exists" in message and str(store) in message, message
    assert "zero bytes" in message, f"the message does not name the situation: {message}"
    assert "delete" in message, f"the message does not name the recovery: {message}"
    assert store.exists(), "the refusal must not remove a file this run did not create"
    assert not record.exists()


def test_60_an_empty_store_leftover_names_record_only_and_that_recovery_works(environment, tmp_path):
    """What a provisioning refused or killed AFTER creating the store leaves:
    the supported schema, no rows, and no record. The message must name
    `--record-only`, and `--record-only` must actually recover from it."""
    e = environment
    store, record = tmp_path / "half.sqlite", tmp_path / "half.record.json"
    _provision(store, record, e.evidence, e.inventory)
    record.unlink()      # the record never got published
    with pytest.raises(v.VerificationError) as caught:
        _provision(store, record, e.evidence, e.inventory)
    message = str(caught.value)
    assert "already exists" in message and str(store) in message, message
    assert "no dedup rows" in message, f"the message does not name the situation: {message}"
    assert "--record-only" in message, f"the message does not name the recovery: {message}"
    assert "delete" in message, f"the message does not offer the other recovery: {message}"
    assert store.exists(), "the refusal must not remove a file this run did not create"
    # The named recovery has to be a real one, not a plausible sentence.
    data = _provision(store, record, e.evidence, e.inventory, record_only=True)
    assert v.load_record(str(record)) == data


def test_60_a_populated_store_refuses_and_is_never_offered_for_deletion(environment):
    """The guard that stops the fix from becoming "delete whatever is in the
    way". A second provisioning over a live store must refuse, must keep every
    row, and must not suggest deleting it."""
    e = environment
    with closing(admin.open_existing(str(e.store))) as db:
        db.execute("INSERT INTO envelope_dedup_v2 VALUES "
                   "('live-envelope',1.0,'req','completed','owner',0.0,'',0)")
    with pytest.raises(v.VerificationError) as caught:
        _provision(e.store, e.record, e.evidence, e.inventory)
    message = str(caught.value)
    assert "already exists" in message and str(e.store) in message, message
    assert "1 dedup row" in message, f"the message does not name the situation: {message}"
    assert "--record-only" in message, f"the message does not name the recovery: {message}"
    assert "delete" not in message, f"a populated store must never be offered for deletion: {message}"
    with closing(admin.open_existing(str(e.store), readonly=True)) as db:
        assert db.execute("SELECT COUNT(*) FROM envelope_dedup_v2").fetchone()[0] == 1


@pytest.mark.parametrize("rows", [0, 1], ids=["empty_extra_table", "populated_extra_table"])
def test_60_a_store_carrying_other_tables_is_never_offered_for_deletion(environment, rows):
    """`fingerprint` reads `PRAGMA table_info(envelope_dedup_v2)` and nothing
    else, so a file can match the supported schema, have an empty dedup table,
    and still hold an operator's own tables beside it. The first version of this
    advice said "it holds no rows" about exactly that file (Ohm, PR #65) — advice
    to delete someone's data, on a check that proved something narrower than
    what the sentence claimed.
    """
    e = environment
    with closing(admin.open_existing(str(e.store))) as db:
        db.execute("CREATE TABLE operator_data(payload TEXT)")
        for _ in range(rows):
            db.execute("INSERT INTO operator_data VALUES ('must survive')")
    with pytest.raises(v.VerificationError) as caught:
        _provision(e.store, e.record, e.evidence, e.inventory)
    message = str(caught.value)
    assert "already exists" in message and str(e.store) in message, message
    assert "operator_data" in message, f"the message does not name what else is there: {message}"
    assert "delete" not in message, (
        f"a store carrying tables this command did not create was offered for deletion: {message}")
    assert "--record-only" in message, f"the message does not name the recovery: {message}"
    with closing(admin.open_existing(str(e.store), readonly=True)) as db:
        assert db.execute("SELECT COUNT(*) FROM operator_data").fetchone()[0] == rows
        assert db.execute("SELECT COUNT(*) FROM envelope_dedup_v2").fetchone()[0] == 0


def test_60_a_populated_table_wearing_the_index_name_is_never_offered_for_deletion(environment):
    """A name is not an identity. Dropping the expected INDEX and creating a
    populated TABLE under its name matched a name-only allowlist, and the file
    was offered for deletion (Ohm, PR #65) — the same destructive advice the
    allowlist was added to prevent, one dimension over.
    """
    e = environment
    with closing(admin.open_existing(str(e.store))) as db:
        db.execute("DROP INDEX envelope_dedup_v2_first_seen")
        db.execute("CREATE TABLE envelope_dedup_v2_first_seen(payload TEXT)")
        db.execute("INSERT INTO envelope_dedup_v2_first_seen VALUES ('valuable operator data')")
        # The upstream gate still passes, which is what makes this reachable.
        assert v.fingerprint(db) == v.SCHEMAS["python"]
    with pytest.raises(v.VerificationError) as caught:
        _provision(e.store, e.record, e.evidence, e.inventory)
    message = str(caught.value)
    assert "table envelope_dedup_v2_first_seen" in message, (
        f"the message does not name the object or its type: {message}")
    assert "delete" not in message, (
        f"a populated table wearing an expected index's name was read as expected: {message}")
    with closing(admin.open_existing(str(e.store), readonly=True)) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM envelope_dedup_v2_first_seen").fetchone()[0] == 1


def test_60_rows_living_only_in_an_uncheckpointed_wal_block_deletion_advice(environment, tmp_path):
    """The row count is read through a `mode=ro` handle. If that handle could not
    see an uncheckpointed `-wal`, "no dedup rows" would be a claim broader than
    the check once more — and the file offered for deletion would have rows in it.

    It does see them: SQLite recovers the WAL read-only. Pinned here so a future
    change to how the obstruction is opened cannot quietly lose it.
    """
    e = environment
    store, record = tmp_path / "wal-only.sqlite", tmp_path / "wal-only.record.json"
    _provision(store, record, e.evidence, e.inventory)
    _uncheckpointed_wal_beside(store, rows=1)
    record.unlink()

    # The fixture's own claim, asserted before anything is asserted about the
    # code: the row must be in the WAL and NOT in the main file. Without this the
    # test below passes against an inspection opened `immutable=1`, which ignores
    # the WAL completely (Ohm, PR #65).
    assert _rows_in_a_main_only_copy(store, tmp_path / "main-only.sqlite") == 0, (
        "the fixture checkpointed the row into the main file, so this test would "
        "pass against an inspection that never reads the WAL")

    with pytest.raises(v.VerificationError) as caught:
        _provision(store, record, e.evidence, e.inventory)
    message = str(caught.value)
    assert "1 dedup row" in message, f"the WAL's row was not counted: {message}"
    assert "delete" not in message, (
        f"a store whose rows live in the WAL was offered for deletion: {message}")


def _uncheckpointed_wal_beside(store, rows):
    """Leave a genuinely PRE-CHECKPOINT store on disk: `rows` live rows that exist
    only in the `-wal`, beside a main file that does not contain them.

    The main file has to be restored as well as the WAL. Closing the last writable
    handle checkpoints, and an earlier version of this helper restored only the
    WAL — so the row was in the main file, and the test built on it passed against
    an inspection that ignored the WAL entirely. Ohm proved that by opening with
    `immutable=1` and watching the test still pass (PR #65). A fixture that does
    not produce the state its name claims makes every assertion above it vacuous.
    """
    main_before = Path(store).read_bytes()
    db = admin.open_existing(str(store))
    db.execute("PRAGMA wal_autocheckpoint=0")
    for index in range(max(rows, 1)):
        db.execute("INSERT INTO envelope_dedup_v2 VALUES "
                   f"('wal-row-{index}',1.0,'req','completed','owner',0.0,'',0)")
    if not rows:
        db.execute("DELETE FROM envelope_dedup_v2")   # a -wal with content, no live rows
    kept = Path(str(store) + "-wal").read_bytes()
    db.close()                                        # checkpoints into the main file...
    Path(store).write_bytes(main_before)              # ...so undo that,
    Path(str(store) + "-wal").write_bytes(kept)       # and put the WAL back
    Path(str(store) + "-shm").unlink(missing_ok=True)
    return kept


def _rows_in_a_main_only_copy_of_bytes(main_bytes, destination):
    """Rows in a database reconstructed from the main file's bytes alone."""
    Path(destination).write_bytes(main_bytes)
    with closing(sqlite3.connect(destination)) as db:
        try:
            return db.execute("SELECT COUNT(*) FROM envelope_dedup_v2").fetchone()[0]
        except sqlite3.DatabaseError:
            return 0        # not a database at all, so it holds no dedup rows


def _rows_in_a_main_only_copy(store, destination):
    """Rows visible in a copy of the MAIN file alone, no sidecars.

    The independent check that a row said to live in the WAL really does live
    there — the same distinction SPEC-26 §8.6 step 3 draws about backups.
    """
    shutil.copyfile(store, destination)
    with closing(sqlite3.connect(destination)) as db:
        try:
            return db.execute("SELECT COUNT(*) FROM envelope_dedup_v2").fetchone()[0]
        except sqlite3.DatabaseError:
            return 0        # not a database at all, so it holds no dedup rows


def test_60_a_zero_byte_store_with_a_wal_beside_it_is_not_offered_for_deletion(environment, tmp_path):
    """Found by my own claim-vs-check pass, not by a reviewer, and it is the
    yugo#60 shape one level up: the check was `getsize(main) == 0`, and the advice
    built on it was "delete it and re-run unchanged" — which asserts the retry
    works. It does not. Deleting the main file orphans the `-wal`, and the orphan
    pre-flight then refuses, so the operator meets a second refusal caused by the
    first instruction. The `-wal` can also be the only copy of its rows.
    """
    e = environment
    store, record = tmp_path / "truncated.sqlite", tmp_path / "truncated.record.json"
    _provision(store, record, e.evidence, e.inventory)
    kept = _uncheckpointed_wal_beside(store, rows=1)
    with open(store, "wb") as main:
        main.truncate(0)                              # a truncated or half-restored store
    record.unlink()
    wal = Path(str(store) + "-wal")

    with pytest.raises(v.VerificationError) as caught:
        _provision(store, record, e.evidence, e.inventory)
    message = str(caught.value)
    assert wal.read_bytes() == kept, "the WAL beside the empty store was destroyed"
    assert str(wal) in message, f"the message does not name the sidecar: {message}"
    assert "zero bytes" in message, message
    assert f"delete {store}" not in message, (
        f"deleting the empty file alone was offered, which orphans the WAL: {message}")


def test_60_deletion_advice_names_the_sidecars_the_inspection_itself_created(environment, tmp_path):
    """Reading the store to check it leaves a `-wal` and `-shm` behind that a
    read-only handle cannot remove. Advice naming the store alone sends the
    operator into the orphan-sidecar refusal with files this command created."""
    e = environment
    store, record = tmp_path / "half.sqlite", tmp_path / "half.record.json"
    _provision(store, record, e.evidence, e.inventory)
    record.unlink()
    assert not [p for p in _residue(store) if p.exists() and p != store], (
        "fixture started with sidecars, so this would not test the created-here case")

    with pytest.raises(v.VerificationError) as caught:
        _provision(store, record, e.evidence, e.inventory)
    message = str(caught.value)
    assert "appeared during this command's own read of the store" in message, message
    for sidecar in (Path(str(store) + "-wal"), Path(str(store) + "-shm")):
        assert sidecar.exists(), f"{sidecar.name} was expected from the read-only inspection"
        assert str(sidecar) in message, (
            f"the deletion advice would orphan {sidecar.name} without naming it: {message}")


@pytest.mark.parametrize("suffix, content, preserved, label", [
    ("-journal", b"\0" * 8 + b"preserved sidecar bytes" * 100, True, "cold rollback journal"),
    ("-wal", None, True, "uncheckpointed WAL"),
    # `-shm` is a derived index SQLite rewrites and which holds no persistent
    # data, so its bytes are NOT asserted — only that a file predating the
    # command is never cleared by a claim about the store's rows.
    ("-shm", b"\1" * 32768, False, "shared-memory file with bytes in it"),
], ids=["cold_journal", "live_wal", "shm_with_bytes"])
def test_60_a_sidecar_this_command_did_not_read_is_never_offered_for_deletion(
        environment, tmp_path, suffix, content, preserved, label):
    """Finding 7, generalised. The store's row count establishes what SQLite
    read; it says nothing about a file SQLite ignored. A cold or truncated
    `-journal`, and a `-wal` whose header does not match the database beside it,
    are both invisible to that count — so the advice must not claim to cover them.

    The previous version of this advice named a pre-existing journal for deletion
    while asserting "nothing in them is unaccounted for, since the row count above
    was read through them" (Ohm, PR #65). The classifier now never makes a claim
    about a sidecar's contents at all: it is cleared by being empty or by being
    this command's own, and by nothing else.
    """
    e = environment
    store, record = tmp_path / "with-sidecar.sqlite", tmp_path / "with-sidecar.record.json"
    _provision(store, record, e.evidence, e.inventory)
    sidecar = Path(str(store) + suffix)
    kept = _uncheckpointed_wal_beside(store, rows=0) if content is None else content
    if content is not None:
        sidecar.write_bytes(content)
    record.unlink()

    with pytest.raises(v.VerificationError) as caught:
        _provision(store, record, e.evidence, e.inventory)
    message = str(caught.value)
    if preserved:
        assert sidecar.read_bytes() == kept, f"the {label} was modified or destroyed"
    assert "predate this command, which has not read them" in message, (
        f"the message does not say the {label} is unaccounted for: {message}")
    assert str(sidecar) in message, f"the message does not name the {label}: {message}"
    assert "or delete " not in message, (
        f"a {label} this command never read was offered for deletion: {message}")
    assert "--record-only" in message, f"the message withholds the recovery: {message}"


def _clean_leftover(e, store, record):
    """A store a provisioning created and was then killed before publishing."""
    _provision(store, record, e.evidence, e.inventory)
    record.unlink()


def _zero_byte_leftover(e, store, record):
    """A store killed between the O_EXCL create and the schema write."""
    os.close(os.open(store, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))


def _clean_leftover_with_an_empty_sidecar(e, store, record):
    """The same, with a zero-byte sidecar beside it — nothing in it to lose."""
    _clean_leftover(e, store, record)
    Path(str(store) + "-journal").touch()


@pytest.mark.parametrize("setup", [
    _clean_leftover, _zero_byte_leftover, _clean_leftover_with_an_empty_sidecar,
], ids=["supported_empty_store", "zero_byte_file", "empty_sidecar_beside_it"])
def test_60_the_deletion_advice_is_executed_and_nothing_with_rows_is_deleted(
        environment, tmp_path, setup):
    """Every branch that offers deletion, with BOTH halves asserted.

    Ohm's correction, and it is the limit of the earlier version of this test:
    *a successful retry proves recovery works, not that deleting every named
    artifact was safe.* So the retry succeeding is only the first assertion. The
    second — missing until now — reconstructs what was on disk BEFORE the command
    ran and requires that nothing holding rows, and no non-empty file that
    predated the command, was among the paths the advice named.
    """
    e = environment
    store, record = tmp_path / "leftover.sqlite", tmp_path / "leftover.record.json"
    setup(e, store, record)
    before = {str(path): path.read_bytes() for path in _residue(store) if path.exists()}

    with pytest.raises(v.VerificationError) as caught:
        _provision(store, record, e.evidence, e.inventory)
    recovery = str(caught.value).split("to recover, ", 1)[1]
    assert "or delete " in recovery, f"this branch is expected to offer deletion: {recovery}"
    # Split on the delimiters the message itself uses, not on whitespace: a path
    # with a space in it would defeat a token parser (Ohm, PR #65).
    named = recovery.split("or delete ", 1)[1].split(" — ", 1)[0].split(", ")

    # ---- half one: doing exactly what it said leaves a working store ----
    for path in named:
        Path(path).unlink()
    leftover = [path.name for path in _residue(store) if path.exists()]
    assert not leftover, f"the advice named fewer paths than were there: {leftover}"
    data = _provision(store, record, e.evidence, e.inventory)
    assert v.load_record(str(record)) == data

    # ---- half two: nothing deleted held anything ----
    for path in named:
        if path not in before:
            continue                       # this command's own artifact
        if path == str(store):
            rows = _rows_in_a_main_only_copy_of_bytes(
                before[path], tmp_path / "pre-state.sqlite")
            assert rows == 0, f"the advice named a store holding {rows} dedup row(s)"
        else:
            assert before[path] == b"", (
                f"the advice named {Path(path).name}, which predated the command with "
                f"{len(before[path])} bytes in it")


def test_60_deletion_is_offered_only_for_a_file_holding_nothing_else(environment, tmp_path):
    """The other side of the control above: the advice must still be given where
    it is true, or the fix is just "never offer deletion"."""
    e = environment
    store, record = tmp_path / "half.sqlite", tmp_path / "half.record.json"
    _provision(store, record, e.evidence, e.inventory)
    record.unlink()
    with pytest.raises(v.VerificationError) as caught:
        _provision(store, record, e.evidence, e.inventory)
    message = str(caught.value)
    assert "no schema object provisioning did not create" in message, message
    assert f"delete {store}" in message, f"the message withholds the true recovery: {message}"


def test_60_a_record_path_that_is_a_directory_creates_no_store(environment, tmp_path):
    """`os.replace` onto a directory fails after the store exists, so this is an
    operator-input refusal that belongs before the create rather than in the
    discard (Ohm, PR #65)."""
    e = environment
    store = tmp_path / "next-bot.sqlite"
    record = tmp_path / "a-directory-not-a-file"
    record.mkdir()
    with pytest.raises(v.VerificationError, match="is a directory"):
        _provision(store, record, e.evidence, e.inventory)
    for path in _residue(store):
        assert not path.exists(), f"a refused provisioning left {path.name} behind (yugo#60)"
    assert record.is_dir(), "the refusal must not disturb the directory it named"


def test_60_a_pre_migration_store_in_the_way_is_never_offered_for_deletion(environment):
    """A six-column store is what every pre-Release-2 deployment has, and the
    operator who meets this needs `yugo dedup migrate`, not a fresh provision.
    It has rows, so the message must not read as permission to clear the path."""
    e = environment
    make_six(e).close()
    with pytest.raises(v.VerificationError) as caught:
        _provision(e.store, e.record, e.evidence, e.inventory)
    message = str(caught.value)
    assert "already exists" in message and str(e.store) in message, message
    assert "not a python-port store with the supported schema" in message, (
        f"the message does not name the situation: {message}")
    assert "delete" not in message, (
        f"a store whose contents this command cannot account for must never be "
        f"offered for deletion: {message}")
    with closing(admin.open_existing(str(e.store), readonly=True)) as db:
        assert db.execute("SELECT COUNT(*) FROM envelope_dedup_v2").fetchone()[0] == 1


def _orphaned_wal(tmp_path):
    """A `<store>-wal` holding committed rows whose main database was lost.

    Not synthetic: this is the state an operator is in after deleting or
    overwriting the main file while the WAL was uncheckpointed, and the reason
    SPEC-26 §8.6 step 3 insists a backup is WAL-aware.
    """
    store = tmp_path / "recovered.sqlite"
    db = sqlite3.connect(store, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("CREATE TABLE envelope_dedup_v2 (envelope_id TEXT)")
    db.execute("INSERT INTO envelope_dedup_v2 VALUES ('committed-but-uncheckpointed')")
    kept = Path(str(store) + "-wal").read_bytes()
    db.close()
    store.unlink()
    Path(str(store) + "-wal").write_bytes(kept)
    for stray in (str(store) + "-shm",):
        Path(stray).unlink(missing_ok=True)
    return store, kept


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_60_a_sidecar_that_predates_the_run_is_preserved(environment, tmp_path, suffix):
    """O_EXCL proves the MAIN path is new; it says nothing about the sidecars.

    The first version of the yugo#60 cleanup assumed otherwise and deleted a
    pre-existing WAL on a refused provisioning (Codex, PR #65). A careful
    cleanup cannot fix that on its own: SQLite deletes an orphaned WAL the
    instant it opens the empty store this command would create, so the refusal
    has to come before anything is created.
    """
    e = environment
    store = tmp_path / "recovered.sqlite"
    sidecar = Path(str(store) + suffix)
    sidecar.write_bytes(b"operator-owned bytes this command did not create")
    digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    record = tmp_path / "recovered.record.json"

    try:
        _provision(store, record, e.evidence, e.inventory)
        refusal = None
    except v.VerificationError as exc:
        refusal = exc

    # The data assertion first and unconditionally. It has to hold whatever the
    # command decided to do, and asserting the refusal first would hide it: with
    # the pre-flight removed this provisioning SUCCEEDS and eats the WAL on the
    # way, so a `pytest.raises` wrapper never reaches this line.
    assert sidecar.exists(), f"provisioning destroyed a pre-existing {suffix}"
    assert hashlib.sha256(sidecar.read_bytes()).hexdigest() == digest, (
        f"provisioning rewrote a pre-existing {suffix}")

    assert refusal is not None, f"a {suffix} with no database beside it must refuse"
    message = str(refusal)
    assert str(sidecar) in message, f"the refusal does not name the sidecar: {message}"
    assert "move it aside" in message, f"the refusal does not name the recovery: {message}"
    assert not store.exists(), "the refusal must not create the store either"
    assert not record.exists()


def test_60_an_orphaned_wal_with_real_rows_survives_a_refused_provisioning(environment, tmp_path):
    """The same guarantee against the artefact SQLite itself produces, rather
    than a file of arbitrary bytes: a WAL with committed rows and no main
    database. This is the loss Codex's P1 describes, end to end."""
    e = environment
    store, kept = _orphaned_wal(tmp_path)
    wal = Path(str(store) + "-wal")
    try:
        _provision(store, tmp_path / "recovered.record.json", e.evidence, e.inventory)
        refusal = None
    except v.VerificationError as exc:
        refusal = exc
    assert wal.exists(), "the orphaned WAL was deleted outright"
    assert wal.read_bytes() == kept, "the committed rows in the orphaned WAL were destroyed"
    assert refusal is not None and str(wal) in str(refusal), (
        f"an orphaned WAL must refuse and be named; got {refusal!r}")
    assert not store.exists()


def test_60_a_live_store_with_its_own_wal_still_gets_the_store_exists_message(environment):
    """The sidecar refusal must not shadow the one above it. A store that is
    open and has an uncheckpointed WAL is the normal case, not a damaged one."""
    e = environment
    with closing(admin.open_existing(str(e.store))) as db:
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO envelope_dedup_v2 VALUES "
                   "('live-envelope',1.0,'req','completed','owner',0.0,'',0)")
        assert Path(str(e.store) + "-wal").exists(), "fixture did not produce a WAL to test with"
        with pytest.raises(v.VerificationError) as caught:
            _provision(e.store, e.record, e.evidence, e.inventory)
    message = str(caught.value)
    assert "a store already exists at" in message, message
    assert "move it aside" not in message, (
        f"a live store's own WAL was reported as an orphaned sidecar: {message}")


def test_60_the_discard_leaves_a_file_that_replaced_the_store_alone(environment, tmp_path, monkeypatch):
    """The discard removes the store only while the path still holds the file
    O_EXCL made. Recorded identity, not an assumption that a path still means
    what it meant — the same class of assumption that cost the WAL above."""
    e = environment
    store, record = tmp_path / "next-bot.sqlite", tmp_path / "next-bot.record.json"
    intruder = b"a different file, at the same path, that this run did not create"

    def swap_then_fail():
        store.unlink()
        store.write_bytes(intruder)
        raise OSError("boot identity unavailable")

    monkeypatch.setattr(admin, "read_boot_id", swap_then_fail)
    with pytest.raises(OSError, match="boot identity unavailable"):
        _provision(store, record, e.evidence, e.inventory)
    assert store.read_bytes() == intruder, "the discard removed a file it did not create"


def test_60_the_operator_retry_sequence_through_the_cli(tmp_path):
    """The whole bug as the operator meets it: a mistyped `backing`, then the
    same command with it corrected. Run through `bin/yugo` as a real process,
    because the import path is not the one an operator uses."""
    store, record = tmp_path / "store.sqlite", tmp_path / "record.json"
    evidence = dict(device_path="operator-inspected test fixture", mount_point="unresolved",
                    fstype="unresolved", mount_id_source="unresolved", backing="local-virtual",
                    determined_by="test fixture only", inspected_at="2026-09-29T00:00:00Z")
    inventory = [dict(process="test", user="test", path=str(store), method="fixture")]

    def attempt(backing):
        return _run_cli("dedup", "provision", "--port", "python", "--attested-by", "test",
                        "--store", str(store), "--record", str(record),
                        stdin="STOPPED\n" + json.dumps({**evidence, "backing": backing})
                              + "\n" + json.dumps(inventory) + "\nATTEST\n")

    refused = attempt("nfs")
    assert refused.returncode == 1, refused.stderr
    assert not store.exists(), (
        "the refused first attempt left its store behind, so the retry below "
        f"fails on the path instead of succeeding (yugo#60); stderr={refused.stderr!r}"
    )

    retried = attempt("local-virtual")
    assert retried.returncode == 0, (
        f"the corrected retry did not succeed; stderr={retried.stderr!r}"
    )
    assert store.exists() and record.exists()
    assert json.loads(record.read_text())["storage_evidence"]["backing"] == "local-virtual"

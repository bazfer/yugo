"""Coordinator single-instance lock (v0.6a).

The tests that matter here are the refusals. A lock that grants is trivially
observable; a lock that fails to EXCLUDE looks identical to a working one until
two coordinators corrupt the same state file.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordinator as _coordinator  # noqa: E402
from coordinator import (  # noqa: E402
    UNRELIABLE_LOCK_FILESYSTEMS,
    CoordinatorLockError,
    acquire_instance_lock,
    is_unreliable_filesystem,
    resolve_mode,
)


@pytest.fixture(autouse=True)
def _neutral_filesystem(monkeypatch, request):
    """Pin the filesystem answer for every test that is not ABOUT the policy.

    pytest's tmp_path can itself sit on overlayfs — routinely so in a
    container — and then the lock refuses before the behaviour under test runs.
    Seven tests would fail on a developer's machine for a reason that has
    nothing to do with the lock, and the failure would read as a broken lock.
    Tests that exercise the policy opt out via the `filesystem_policy` mark.
    """
    if request.node.get_closest_marker("filesystem_policy"):
        return
    monkeypatch.setattr("coordinator.detect_filesystem", lambda _: "ext4")


def test_first_acquirer_gets_the_lock(tmp_path):
    lock = acquire_instance_lock(tmp_path / "state.sqlite")
    assert lock.path.exists()
    lock.release()


def test_second_acquirer_in_the_same_process_is_refused(tmp_path):
    state = tmp_path / "state.sqlite"
    first = acquire_instance_lock(state)
    try:
        with pytest.raises(CoordinatorLockError) as excinfo:
            acquire_instance_lock(state)
        assert excinfo.value.reason == "coordinator_already_running"
    finally:
        first.release()


def _try_acquire(state_path: str, queue) -> None:
    """Child-process acquirer. Reports the reason code, never the exception."""
    try:
        lock = acquire_instance_lock(state_path)
    except CoordinatorLockError as error:
        queue.put(("refused", error.reason))
    else:
        queue.put(("acquired", None))
        lock.release()


def _acquire_and_die(state_path: str, marker_path: str) -> None:
    """Acquire and then DIE holding it — no release, no clean shutdown.

    `os._exit` skips interpreter cleanup, atexit and destructors, so nothing
    closes the connection politely. That is the whole point: the lock must be
    released by the kernel because the process ended, not by any code we wrote.

    Signals through a FILE rather than a multiprocessing.Queue: a Queue is
    flushed by a background feeder thread that `os._exit` does not wait for, so
    the message can be lost by the very abruptness this test needs.
    """
    lock = acquire_instance_lock(state_path)  # noqa: F841 — held deliberately
    Path(marker_path).write_text(str(os.getpid()))
    os._exit(0)


def test_a_second_OS_PROCESS_is_refused(tmp_path):
    """The same-process test above can pass on nothing but sqlite's own
    connection bookkeeping. Exclusion between two real processes is the actual
    claim, and it is the one that matters at deploy time — two containers, not
    two threads."""
    state = tmp_path / "state.sqlite"
    held = acquire_instance_lock(state)
    try:
        queue = multiprocessing.Queue()
        child = multiprocessing.Process(target=_try_acquire, args=(str(state), queue))
        child.start()
        child.join(timeout=30)
        outcome, reason = queue.get(timeout=5)
        assert outcome == "refused", "a second coordinator PROCESS acquired the lock"
        assert reason == "coordinator_already_running"
    finally:
        held.release()


def test_the_lock_releases_when_the_holder_dies_without_releasing(tmp_path):
    """POSIX locks die with the process, which is why this is a lock and not a
    lock FILE. A coordinator that is SIGKILLed leaves nothing to reap.

    The child must NOT call release(). An earlier version of this test used the
    helper that releases before exiting, so it proved ordinary release and would
    have passed against a cleanup-dependent lock that goes stale after SIGKILL —
    exactly the regression it claims to cover."""
    state = tmp_path / "state.sqlite"
    marker = tmp_path / "child-acquired"
    child = multiprocessing.Process(target=_acquire_and_die, args=(str(state), str(marker)))
    child.start()
    child.join(timeout=30)
    assert child.exitcode == 0
    assert marker.exists(), "the child never acquired, so this proves nothing about release"
    # The child is gone; its lock must be gone with it.
    lock = acquire_instance_lock(state)
    lock.release()


def test_the_lock_records_who_holds_it(tmp_path):
    """A human looking at the state file should learn the owner without
    reading this module."""
    state = tmp_path / "state.sqlite"
    lock = acquire_instance_lock(state)
    try:
        rows = lock.connection.execute("SELECT pid FROM coordinator_instance WHERE id = 1").fetchall()
        assert rows == [(os.getpid(),)]
    finally:
        lock.release()


def test_release_lets_the_next_coordinator_start(tmp_path):
    state = tmp_path / "state.sqlite"
    acquire_instance_lock(state).release()
    second = acquire_instance_lock(state)
    second.release()


@pytest.mark.filesystem_policy
def test_an_unreliable_filesystem_refuses_rather_than_warns(tmp_path, monkeypatch):
    """On NFS or overlayfs the lock does not reliably exclude, and the failure
    is SILENT — both coordinators start. A warning at boot is seen once and
    never again; a refusal is seen every time until it is fixed."""
    state = tmp_path / "state.sqlite"
    monkeypatch.setattr("coordinator.detect_filesystem", lambda _: "nfs")
    with pytest.raises(CoordinatorLockError) as excinfo:
        acquire_instance_lock(state)
    assert excinfo.value.reason == "coordinator_state_fs_unreliable"
    assert "nfs" in str(excinfo.value)
    # And the escape hatch works, for an operator who knows better.
    lock = acquire_instance_lock(state, allow_unreliable_fs=True)
    lock.release()


@pytest.mark.filesystem_policy
@pytest.mark.parametrize("filesystem", sorted(UNRELIABLE_LOCK_FILESYSTEMS))
def test_every_listed_filesystem_is_actually_refused(tmp_path, monkeypatch, filesystem):
    """The set is only a guard if every member of it is enforced. A name added
    to the frozenset and not honoured reads as protection that is not there."""
    monkeypatch.setattr("coordinator.detect_filesystem", lambda _: filesystem)
    with pytest.raises(CoordinatorLockError) as excinfo:
        acquire_instance_lock(tmp_path / f"{filesystem}.sqlite")
    assert excinfo.value.reason == "coordinator_state_fs_unreliable"


@pytest.mark.filesystem_policy
def test_an_unknown_filesystem_is_refused_by_default(tmp_path, monkeypatch):
    """Reversed from an earlier revision, which let unknown start.

    The guarantee this lock exists to provide is exclusion. A filesystem we
    could not identify — unsupported platform, unreadable mount table, a gap in
    the parser — is one where that guarantee cannot be established, so starting
    anyway contradicts the same sentence that made an unreliable filesystem an
    abort rather than a warning. The operator override is the way through, and
    it is a deliberate act rather than a default."""
    monkeypatch.setattr("coordinator.detect_filesystem", lambda _: None)
    with pytest.raises(CoordinatorLockError) as excinfo:
        acquire_instance_lock(tmp_path / "unknown.sqlite")
    assert excinfo.value.reason == "coordinator_state_fs_unreliable"
    lock = acquire_instance_lock(tmp_path / "unknown.sqlite", allow_unreliable_fs=True)
    assert lock.filesystem is None
    lock.release()


def test_a_genuine_write_fault_is_not_reported_as_another_coordinator(tmp_path, monkeypatch):
    """'database is locked' means a second coordinator. Every other sqlite
    failure means something else, and reporting it as a running coordinator
    sends an operator hunting a process that does not exist."""
    class BrokenConnection:
        # sqlite3.Connection is an immutable C type, so the fault is injected
        # by standing in for the connection rather than patching its method.
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            pass

    monkeypatch.setattr("coordinator.sqlite3.connect", lambda *a, **k: BrokenConnection())
    with pytest.raises(CoordinatorLockError) as excinfo:
        acquire_instance_lock(tmp_path / "broken.sqlite")
    assert excinfo.value.reason == "coordinator_state_unwritable"


def test_an_uncreatable_state_directory_refuses_with_its_own_reason(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    with pytest.raises(CoordinatorLockError) as excinfo:
        acquire_instance_lock(blocker / "nested" / "state.sqlite")
    assert excinfo.value.reason == "coordinator_state_dir_unwritable"


# --- YUGO_MODE -------------------------------------------------------------

def test_mode_defaults_to_bot_when_unset_or_blank():
    assert resolve_mode({}) == "bot"
    assert resolve_mode({"YUGO_MODE": ""}) == "bot"
    assert resolve_mode({"YUGO_MODE": "   "}) == "bot"


def test_mode_accepts_both_roles_case_insensitively():
    assert resolve_mode({"YUGO_MODE": "coordinator"}) == "coordinator"
    assert resolve_mode({"YUGO_MODE": "  COORDINATOR "}) == "coordinator"
    assert resolve_mode({"YUGO_MODE": "Bot"}) == "bot"


def test_an_unrecognised_mode_aborts_rather_than_falling_back_to_bot():
    """'coord' is an operator who meant 'coordinator'. Starting a bot instead
    gives them a process that looks healthy, heartbeats, and forwards nothing
    — the same silent-downgrade failure SANDBOX_MODE=full refuses in v0.4e."""
    with pytest.raises(CoordinatorLockError) as excinfo:
        resolve_mode({"YUGO_MODE": "coord"})
    assert excinfo.value.reason == "coordinator_mode_invalid"


# --- the filesystem families /proc/mounts actually reports -----------------

@pytest.mark.filesystem_policy
@pytest.mark.parametrize("name", ["fuse.sshfs", "fuse.s3fs", "fuseblk", "FUSE.sshfs", "nfs4", "overlay"])
def test_fuse_and_nfs_subtypes_are_refused_by_family(tmp_path, monkeypatch, name):
    """Linux reports a FUSE mount by its SUBTYPE — `fuse.sshfs`, `fuseblk` —
    never the bare string `fuse`. An exact-membership test misses every real
    FUSE mount, which is to say it misses precisely what the set exists for."""
    monkeypatch.setattr("coordinator.detect_filesystem", lambda _: name)
    with pytest.raises(CoordinatorLockError) as excinfo:
        acquire_instance_lock(tmp_path / "state.sqlite")
    assert excinfo.value.reason == "coordinator_state_fs_unreliable"


@pytest.mark.parametrize("name", ["ext4", "xfs", "btrfs", "apfs", "tmpfs", "EXT4"])
def test_local_filesystems_are_not_refused(tmp_path, monkeypatch, name):
    """The prefix matching must not over-reach. A guard that refuses ext4 is a
    coordinator that never starts."""
    monkeypatch.setattr("coordinator.detect_filesystem", lambda _: name)
    acquire_instance_lock(tmp_path / f"{name}.sqlite").release()


def test_is_unreliable_treats_unknown_as_unreliable():
    assert is_unreliable_filesystem(None) is True


# --- the raw path, before Path() swallows it -------------------------------

@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_a_blank_state_path_is_refused_by_its_own_reason(blank):
    """`Path("")` is `Path(".")` — truthy, and a real directory. A check made
    after conversion can never fire: an unset TASK_STATE_PATH would open the
    working directory and fail with an unrelated sqlite error, and "   " would
    successfully lock a database named three spaces."""
    with pytest.raises(CoordinatorLockError) as excinfo:
        acquire_instance_lock(blank)
    assert excinfo.value.reason == "coordinator_state_path_empty"


def test_a_state_path_is_trimmed_rather_than_taken_literally(tmp_path):
    padded = f"  {tmp_path / 'state.sqlite'}  "
    lock = acquire_instance_lock(padded)
    try:
        assert lock.path == tmp_path / "state.sqlite"
        assert lock.path.exists()
    finally:
        lock.release()


# --- the filesystem inspected is the one sqlite opens ----------------------

@pytest.mark.filesystem_policy
def test_detection_follows_a_symlinked_state_file(tmp_path, monkeypatch):
    """A state path that is a symlink lives on a different filesystem from its
    directory entry, and it is the file sqlite opens that has to hold the lock.
    Inspecting only the parent lets an NFS-backed database through a local
    directory — and falsely rejects a local database under an overlay parent."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_db = real_dir / "state.sqlite"
    real_db.write_bytes(b"")
    link = tmp_path / "link.sqlite"
    link.symlink_to(real_db)

    seen: list[str] = []
    real_statvfs = os.statvfs

    def recording_statvfs(target):
        seen.append(str(target))
        return real_statvfs(target)

    monkeypatch.setattr("coordinator.os.statvfs", recording_statvfs)
    monkeypatch.setattr("coordinator.detect_filesystem", lambda p: __import__("coordinator").detect_filesystem.__wrapped__(p) if False else None)
    # Call the real detector directly: the point is WHICH path it inspects.
    import coordinator as coord
    monkeypatch.undo()
    monkeypatch.setattr("coordinator.os.statvfs", recording_statvfs)
    coord.detect_filesystem(link)
    assert any(str(real_db) in entry for entry in seen), (
        f"detection inspected {seen}, not the symlink target sqlite actually opens"
    )


# --- the mount table the kernel actually writes ----------------------------

@pytest.mark.filesystem_policy
def test_a_mount_point_containing_a_space_is_matched_not_skipped(tmp_path, monkeypatch):
    """The kernel escapes a space in a mount point as `\\040`. Comparing a
    decoded path against the raw token never matches, so the specific mount is
    skipped, `/` wins the longest-prefix contest by default, and an NFS-backed
    state directory is reported as the ROOT filesystem's type.

    The refusal is then bypassed by nothing more exotic than a space in a
    directory name — which is why this is the fixture, not a unit test on the
    unescaper alone."""
    monkeypatch.setattr(
        "coordinator._mount_table",
        lambda: [("/", "ext4", None), ("/mnt/state store", "nfs4", None)],
    )
    assert coordinator_module().detect_filesystem(Path("/mnt/state store/db.sqlite")) == "nfs4"


@pytest.mark.filesystem_policy
def test_the_longest_matching_mount_point_wins(monkeypatch):
    monkeypatch.setattr(
        "coordinator._mount_table",
        lambda: [("/", "ext4", None), ("/mnt", "xfs", None), ("/mnt/deep/state", "nfs4", None)],
    )
    assert coordinator_module().detect_filesystem(Path("/mnt/deep/state/db.sqlite")) == "nfs4"
    assert coordinator_module().detect_filesystem(Path("/mnt/other/db.sqlite")) == "xfs"


@pytest.mark.filesystem_policy
def test_an_unreadable_mount_table_yields_unknown_which_now_refuses(tmp_path, monkeypatch):
    """The two halves of Ohm's second blocker meeting: detection fails, and the
    failure is a refusal rather than a silent start."""
    monkeypatch.setattr("coordinator._mount_table", list)
    with pytest.raises(CoordinatorLockError) as excinfo:
        acquire_instance_lock(tmp_path / "state.sqlite")
    assert excinfo.value.reason == "coordinator_state_fs_unreliable"


@pytest.mark.parametrize(
    "raw,decoded",
    [
        (r"/mnt/state\040store", "/mnt/state store"),
        (r"/mnt/tab\011here", "/mnt/tab\there"),
        (r"/mnt/nl\012here", "/mnt/nl\nhere"),
        (r"/mnt/back\134slash", "/mnt/back\\slash"),
        ("/mnt/plain", "/mnt/plain"),
        (r"/mnt/not\09escape", r"/mnt/not\09escape"),
    ],
)
def test_mount_field_escapes_decode(raw, decoded):
    from coordinator import _unescape_mount_field
    assert _unescape_mount_field(raw) == decoded


def coordinator_module():
    """The module itself, for tests that call detect_filesystem directly."""
    return _coordinator


# --- a mount point is not a mount identity ---------------------------------

@pytest.mark.filesystem_policy
def test_a_stacked_mount_is_resolved_by_device_not_by_first_appearance(tmp_path, monkeypatch):
    """Linux allows two mounts on the SAME path; only the topmost is visible,
    and mountinfo carries major:minor precisely because the path cannot tell
    them apart. Picking the first path match returns the HIDDEN mount — and if
    the hidden one is ext4 while the visible overmount is NFS, that is a
    confidently safe answer that bypasses the refusal.

    Confidently wrong is worse than unknown here, because unknown now refuses.
    """
    target = tmp_path / "state.sqlite"
    target.write_bytes(b"")
    real_dev = os.stat(target).st_dev
    visible = f"{os.major(real_dev)}:{os.minor(real_dev)}"

    monkeypatch.setattr(
        "coordinator._mount_table",
        lambda: [
            # A device that cannot collide with the real one — an earlier
            # version used "8:1", which IS this host's root device, so the
            # hidden entry matched too and the test proved nothing.
            (str(tmp_path), "ext4", "99:99"),      # hidden, listed FIRST
            (str(tmp_path), "nfs4", visible),      # the overmount actually backing the file
        ],
    )
    assert coordinator_module().detect_filesystem(target) == "nfs4"
    with pytest.raises(CoordinatorLockError) as excinfo:
        acquire_instance_lock(target)
    assert excinfo.value.reason == "coordinator_state_fs_unreliable"


@pytest.mark.filesystem_policy
def test_a_known_device_claimed_by_no_mount_is_unknown_rather_than_guessed(tmp_path, monkeypatch):
    """We know the device and no entry claims it, so the table is incomplete or
    stale. Falling back to path matching would answer from the wrong mount."""
    target = tmp_path / "state.sqlite"
    target.write_bytes(b"")
    monkeypatch.setattr(
        "coordinator._mount_table",
        lambda: [("/", "ext4", "99:99"), (str(tmp_path), "ext4", "99:98")],
    )
    assert coordinator_module().detect_filesystem(target) is None


@pytest.mark.filesystem_policy
def test_path_only_tables_refuse_to_break_an_equal_specificity_tie(monkeypatch):
    """/proc/mounts carries no device, so ties are unresolvable there. Two
    equally specific mounts disagreeing about the filesystem is exactly what a
    path cannot decide — answering anyway is the guess this guard rejects."""
    monkeypatch.setattr(
        "coordinator._mount_table",
        lambda: [("/state", "ext4", None), ("/state", "nfs4", None)],
    )
    assert coordinator_module().detect_filesystem(Path("/state/db.sqlite")) is None


@pytest.mark.filesystem_policy
def test_an_equal_specificity_tie_that_agrees_is_not_ambiguous(monkeypatch):
    """Two entries naming the same filesystem are not a conflict; refusing
    those would reject ordinary bind mounts of one filesystem."""
    monkeypatch.setattr(
        "coordinator._mount_table",
        lambda: [("/state", "ext4", None), ("/state", "ext4", None)],
    )
    assert coordinator_module().detect_filesystem(Path("/state/db.sqlite")) == "ext4"


@pytest.mark.filesystem_policy
def test_same_device_equal_specificity_disagreement_is_unknown(tmp_path, monkeypatch):
    """The device narrows the candidates; it does not always decide among them.
    Two entries on the same device, equally specific, naming different
    filesystems is still unresolvable — and the device branch has to apply the
    same rule as the path-only fallback rather than quietly taking the first.

    Added because the mutation for this rule did not fail anything: I had
    written the behaviour with no test behind it.
    """
    target = tmp_path / "state.sqlite"
    target.write_bytes(b"")
    dev = os.stat(target).st_dev
    same = f"{os.major(dev)}:{os.minor(dev)}"
    monkeypatch.setattr(
        "coordinator._mount_table",
        lambda: [(str(tmp_path), "ext4", same), (str(tmp_path), "nfs4", same)],
    )
    assert coordinator_module().detect_filesystem(target) is None


@pytest.mark.filesystem_policy
def test_same_device_equal_specificity_agreement_still_answers(tmp_path, monkeypatch):
    """Agreement is not ambiguity — bind mounts of one filesystem appear twice
    and must still resolve, or the guard refuses ordinary setups."""
    target = tmp_path / "state.sqlite"
    target.write_bytes(b"")
    dev = os.stat(target).st_dev
    same = f"{os.major(dev)}:{os.minor(dev)}"
    monkeypatch.setattr(
        "coordinator._mount_table",
        lambda: [(str(tmp_path), "ext4", same), (str(tmp_path), "ext4", same)],
    )
    assert coordinator_module().detect_filesystem(target) == "ext4"

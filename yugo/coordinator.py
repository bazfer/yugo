"""Coordinator single-instance lock (v0.6a).

The coordinator owns `TASK_STATE_PATH` — baton, task and HITL state. Two
coordinators against one state file is not a degraded mode, it is two
processes both believing they are the authority: both forward the same
envelope, both answer the same hold, and the sqlite file interleaves their
writes. SPEC §15's 6a line therefore says the second process refuses to start.

The lock is a sqlite connection held open for the life of the process, not a
lock file. That choice is load-bearing:

* A lock FILE has to be cleaned up, and a coordinator that is SIGKILLed or
  whose host loses power cannot clean anything up. The next start then finds a
  stale file and either refuses forever or ignores it — and "ignore a stale
  lock" is indistinguishable from "ignore a live one".
* A POSIX lock dies with the process that holds it. A crashed coordinator
  releases immediately, with no reaper, no TTL and no heuristic about whether
  the previous owner is really gone.

`PRAGMA locking_mode = EXCLUSIVE` plus one write takes the file lock and keeps
it until the connection closes. Read-only probing is not enough: sqlite defers
acquiring the exclusive lock until it actually writes.
"""

from __future__ import annotations

import asyncio
import errno
import inspect
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

# Filesystems where a POSIX advisory lock does not reliably exclude a second
# process. On NFS the lock depends on a working lock daemon, and on overlayfs
# the guarantee depends on the underlying layer and the kernel version. In both
# cases the failure is SILENT: both coordinators start, and nothing says so.
#
# We refuse rather than warn. The whole point of this lock is that a second
# coordinator cannot run, and a warning on a filesystem that cannot enforce it
# is the same as no lock while looking like one. An operator who sees the
# refusal moves TASK_STATE_PATH to a local disk in a minute; an operator who
# sees a warning at boot never sees it again.
UNRELIABLE_LOCK_FILESYSTEMS = frozenset({"nfs", "nfs4", "smb", "smb2", "cifs", "overlay", "overlayfs", "fuse", "fuseblk"})

# /proc/mounts reports a FUSE implementation by its SUBTYPE — `fuse.sshfs`,
# `fuse.s3fs`, `fuseblk` — not the bare string `fuse`. An exact-membership test
# therefore misses every real FUSE mount and passes exactly the filesystems the
# set exists to refuse. Families are matched by prefix for that reason.
_UNRELIABLE_PREFIXES = ("fuse", "nfs", "smb", "cifs", "overlay")


def is_unreliable_filesystem(name: str | None) -> bool:
    """True when `name` names a filesystem whose advisory locks do not exclude.

    None is NOT unreliable — see `detect_filesystem`. Unknown means unknown.
    """
    if name is None:
        # Unknown is not safe. The guarantee this lock exists to provide is
        # exclusion, and a filesystem we could not identify is one where we
        # cannot establish it. Starting anyway would contradict the same
        # sentence that made an unreliable filesystem an abort rather than a
        # warning. The operator override is the documented way through.
        return True
    normalized = name.strip().lower()
    if normalized in UNRELIABLE_LOCK_FILESYSTEMS:
        return True
    return normalized.startswith(_UNRELIABLE_PREFIXES)

# From <linux/magic.h>. statfs f_type is the reliable answer where /proc/mounts
# can disagree with reality — a bind mount, a container's rewritten mount table.
_FS_MAGIC = {
    0x6969: "nfs",
    0x517B: "smb",
    0xFF534D42: "cifs",
    0x794C7630: "overlayfs",
    0x65735546: "fuse",
}


class CoordinatorLockError(RuntimeError):
    """Startup refusal. Carries `reason` so callers audit a code, not a string."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


COORDINATOR_NATS_USER = "coordinator"
REQUEST_STREAM = "FLEET_REQUEST"
REQUEST_FILTER = "fleet.*.request"
REQUEST_DURABLE = "yugo-coordinator-request"
FORWARD_NAK_DELAY_S = 5.0
COORDINATOR_STATUS_SUBJECT = "fleet.coordinator.status"
DEFAULT_HEARTBEAT_MS = 5_000
TASK_ERROR_DELAY_S = 0.05


class CoordinatorNatsError(RuntimeError):
    """Fatal coordinator NATS configuration/connection error."""


def load_heartbeat_ms(environ: dict[str, str] | None = None) -> int:
    """Read the positive coordinator heartbeat interval (default 5000ms)."""
    env = os.environ if environ is None else environ
    raw = (env.get("COORDINATOR_HEARTBEAT_MS") or str(DEFAULT_HEARTBEAT_MS)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise CoordinatorNatsError(f"COORDINATOR_HEARTBEAT_MS must be a positive integer; got {raw!r}") from error
    if value <= 0:
        raise CoordinatorNatsError(f"COORDINATOR_HEARTBEAT_MS must be positive; got {value}")
    return value


async def _guarded_task_step(
    label: str,
    operation,
    logger,
    *,
    quiet=(),
    terminal=(),
    error_delay_s: float = TASK_ERROR_DELAY_S,
):
    """Run one repeatable task step without letting a transient fault kill it.

    All long-lived coordinator/tap loops use this one guard. Cancellation is
    control flow and always propagates. Recoverable dependency failures are
    logged, briefly delayed, and reported so the next iteration can proceed;
    terminal failures are logged and re-raised for external supervision.
    """
    try:
        return True, await operation()
    except asyncio.CancelledError:
        raise
    except terminal as error:
        logger(f"{label} (terminal): {error!r}")
        raise
    except quiet:
        return False, None
    except Exception as error:
        logger(f"{label}: {error!r}")
        # A persistent buffer/transport fault must not turn the owning task
        # into a CPU/log hot spin. Reconnection remains nats-py's job.
        await asyncio.sleep(error_delay_s)
        return False, None


@dataclass(frozen=True)
class CoordinatorNatsConfig:
    """Dedicated broker identity used by the coordinator.

    The fixed username is intentional. Accepting an arbitrary user here makes
    it easy to boot the coordinator with the console credential (which cannot
    forward) or a bot credential (which must never publish to ``.inbox``).
    """

    url: str
    password: str = field(repr=False)
    user: str = COORDINATOR_NATS_USER


def load_nats_config(environ: dict[str, str] | None = None) -> CoordinatorNatsConfig:
    """Load the coordinator's dedicated NATS identity, failing closed."""
    env = os.environ if environ is None else environ
    url = (env.get("FLEET_BUS_URL") or "").strip()
    user = (env.get("FLEET_BUS_USER") or "").strip()
    token_file = (env.get("FLEET_BUS_TOKEN_FILE") or "").strip()
    if not url:
        raise CoordinatorNatsError("FLEET_BUS_URL is required in coordinator mode")
    if user != COORDINATOR_NATS_USER:
        raise CoordinatorNatsError(
            f"FLEET_BUS_USER must be {COORDINATOR_NATS_USER!r} in coordinator mode; got {user!r}"
        )
    if not token_file:
        raise CoordinatorNatsError("FLEET_BUS_TOKEN_FILE is required in coordinator mode")
    try:
        password = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise CoordinatorNatsError(f"FLEET_BUS_TOKEN_FILE is unreadable ({token_file!r}): {error}") from error
    if not password:
        raise CoordinatorNatsError(f"FLEET_BUS_TOKEN_FILE is empty ({token_file!r})")
    return CoordinatorNatsConfig(url=url, user=user, password=password)


async def connect_nats(config: CoordinatorNatsConfig, *, connect=None, **options):
    """Open the coordinator connection without starting the forward loop.

    Injection of ``connect`` keeps configuration tests broker-independent. The
    import stays lazy so bot mode retains its existing optional-bus behaviour.
    """
    if config.user != COORDINATOR_NATS_USER:
        raise CoordinatorNatsError("refusing to connect the coordinator with a non-coordinator NATS identity")
    if connect is None:
        import nats

        connect = nats.connect
    try:
        # The coordinator becomes a hard SPOF once adapters migrate. nats-py's
        # default gives up after 60 reconnect attempts; a long broker outage
        # must not leave a healthy-looking but permanently disconnected relay.
        options.setdefault("max_reconnect_attempts", -1)
        result = connect(
            servers=[config.url],
            user=config.user,
            password=config.password,
            name="yugo-coordinator",
            inbox_prefix=b"_INBOX_coordinator",
            **options,
        )
        return await result if inspect.isawaitable(result) else result
    except Exception as error:
        raise CoordinatorNatsError(f"coordinator NATS connection failed: {error}") from error


class CoordinatorRelay:
    """Durable request -> inbox relay, with no policy or de-duplication yet.

    This v0.6a loop is intentionally unopinionated: it preserves the envelope
    bytes and derives the recipient from the stream subject. Successful inbox
    PubAck happens before request ack. A transient publish failure gets a
    delayed nak, so the request remains pending for redelivery.

    FLEET_REQUEST and FLEET_INBOX are limits-retention streams. An unacked
    message can still disappear when its 7-day max_age expires; neither this
    loop nor later holding code may treat ack state as extending max_age. The
    configured 7d >> 15min hold relation is therefore a deployment invariant,
    not a promise made by this consumer.
    """

    def __init__(self, connection, *, nak_delay_s: float = FORWARD_NAK_DELAY_S, logger=print) -> None:
        self._nc = connection
        self._js = connection.jetstream()
        self._nak_delay_s = nak_delay_s
        self._log = logger
        self._subscription = None

    @property
    def subscription(self):
        return self._subscription

    async def open(self):
        """Bind/create the sole durable pull consumer.

        DeliverPolicy.NEW is load-bearing for first deployment: historical
        pre-interposition traffic must not suddenly replay. FB-4 adds the
        persisted marker + de-dup store needed to distinguish a later deleted
        durable and safely recreate that case with DeliverPolicy.ALL.
        """
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

        self._subscription = await self._js.pull_subscribe(
            REQUEST_FILTER,
            durable=REQUEST_DURABLE,
            stream=REQUEST_STREAM,
            config=ConsumerConfig(
                durable_name=REQUEST_DURABLE,
                filter_subject=REQUEST_FILTER,
                deliver_policy=DeliverPolicy.NEW,
                ack_policy=AckPolicy.EXPLICIT,
            ),
        )
        return self._subscription

    @staticmethod
    def inbox_subject(request_subject: str) -> str:
        tokens = request_subject.split(".")
        if len(tokens) != 3 or tokens[0] != "fleet" or tokens[2] != "request" or not tokens[1]:
            raise ValueError(f"unexpected FLEET_REQUEST subject: {request_subject!r}")
        return f"fleet.{tokens[1]}.inbox"

    async def forward(self, message) -> None:
        """Forward one delivery, ack-after-PubAck or delayed-nak on failure."""
        from nats.errors import ConnectionClosedError

        destination = self.inbox_subject(message.subject)
        published, _ = await _guarded_task_step(
            f"coordinator forward failed {message.subject} -> {destination}",
            lambda: self._js.publish(destination, message.data),
            self._log,
            terminal=(ConnectionClosedError,),
        )
        if not published:
            await _guarded_task_step(
                f"coordinator delayed nak failed for {message.subject}",
                lambda: message.nak(delay=self._nak_delay_s),
                self._log,
                terminal=(ConnectionClosedError,),
            )
            return
        await _guarded_task_step(
            f"coordinator ack failed for {message.subject}",
            message.ack,
            self._log,
            terminal=(ConnectionClosedError,),
        )

    async def run(self) -> None:
        """Pull and forward forever. Startup/wiring into bot.py is v0.6b."""
        from nats.errors import ConnectionClosedError, TimeoutError as NatsTimeoutError

        subscription = self._subscription or await self.open()
        while True:
            fetched, messages = await _guarded_task_step(
                "coordinator fetch failed",
                lambda: subscription.fetch(batch=1, timeout=1),
                self._log,
                quiet=(asyncio.TimeoutError, NatsTimeoutError),
                terminal=(ConnectionClosedError,),
            )
            if not fetched:
                continue
            for message in messages:
                await self.forward(message)


class CoordinatorHeartbeat:
    """Emit coordinator liveness over plain core NATS, never JetStream APIs."""

    def __init__(
        self,
        connection,
        *,
        version: str,
        interval_ms: int = DEFAULT_HEARTBEAT_MS,
        logger=print,
    ) -> None:
        if interval_ms <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._nc = connection
        self.interval_ms = interval_ms
        self._version = version
        self._log = logger

    async def emit_once(self) -> dict:
        # Reuse the fleet wire shape, but publish through the core connection.
        # fleet.*.status is deliberately absent from every JetStream stream.
        from fleet_bus import create_heartbeat_envelope

        envelope = create_heartbeat_envelope("coordinator", self._version)
        data = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
        await self._nc.publish(COORDINATOR_STATUS_SUBJECT, data)
        await self._nc.flush()
        return envelope

    async def run(self) -> None:
        from nats.errors import ConnectionClosedError

        while True:
            await _guarded_task_step(
                "coordinator heartbeat failed",
                self.emit_once,
                self._log,
                terminal=(ConnectionClosedError,),
            )
            await asyncio.sleep(self.interval_ms / 1000)


class TapHeartbeatMonitor:
    """Tap-side coordinator-silence detector.

    Only valid coordinator status heartbeats reset the timer. Forwarded
    envelope counts are intentionally absent: the relay is at-least-once, so a
    duplicate forward is expected crash recovery, not an alert condition.
    """

    def __init__(
        self,
        *,
        heartbeat_ms: int,
        coordinator_channel_id: str,
        alert,
        paging_channel_id: str | None = None,
        clock=time.monotonic,
        connection=None,
        logger=print,
    ) -> None:
        if heartbeat_ms <= 0:
            raise ValueError("heartbeat interval must be positive")
        if not str(coordinator_channel_id).strip():
            raise ValueError("COORDINATOR_CHANNEL_ID is required")
        self.heartbeat_ms = heartbeat_ms
        self._channels = (str(coordinator_channel_id),) + (
            (str(paging_channel_id),) if paging_channel_id else ()
        )
        self._alert = alert
        self._clock = clock
        self._connection = connection
        self._log = logger
        self._last_seen = clock()
        self._alerted = False

    async def observe(self, message) -> bool:
        """Accept only the authenticated coordinator heartbeat wire shape."""
        if message.subject != COORDINATOR_STATUS_SUBJECT:
            return False
        try:
            envelope = json.loads(message.data)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        from fleet_bus import validate_envelope

        validation = validate_envelope(envelope, {"coordinator"})
        if not validation.ok or envelope.get("kind") != "status_heartbeat":
            return False
        self._last_seen = self._clock()
        self._alerted = False
        return True

    async def check(self) -> bool:
        """Alert once per outage when silence is strictly greater than 3x."""
        silent_ms = (self._clock() - self._last_seen) * 1000
        if silent_ms <= 3 * self.heartbeat_ms or self._alerted:
            return False
        event = {
            "kind": "coordinator_heartbeat_silent",
            "subject": COORDINATOR_STATUS_SUBJECT,
            "silent_ms": int(silent_ms),
            "threshold_ms": 3 * self.heartbeat_ms,
        }
        # Latch BEFORE yielding to alert I/O. A heartbeat may arrive while a
        # channel callback is in flight; observe() then clears this latch for
        # the new generation. The stale alert completion must not set it back
        # and suppress the next outage indefinitely.
        self._alerted = True
        for channel_id in self._channels:
            async def deliver(channel_id=channel_id):
                result = self._alert(channel_id, event)
                if inspect.isawaitable(result):
                    await result

            await _guarded_task_step(
                f"tap coordinator alert failed for channel {channel_id}",
                deliver,
                self._log,
            )
        return True

    async def run(self) -> None:
        await self._subscribe()
        while True:
            await asyncio.sleep(self.heartbeat_ms / 2000)
            await _guarded_task_step("tap heartbeat check failed", self.check, self._log)

    async def _subscribe(self) -> None:
        # Kept as a separate seam because tap owns the console connection.
        if self._connection is None:
            raise RuntimeError("tap monitor connection is not configured")
        await self._connection.subscribe(COORDINATOR_STATUS_SUBJECT, cb=self.observe)
        await self._connection.flush()


def _unescape_mount_field(field: str) -> str:
    """Decode the octal escapes the kernel writes into mount-table fields.

    A mount point containing a space is written `/mnt/state\\040store`. Comparing
    a decoded filesystem path against that raw token never matches, so the
    specific mount is skipped and `/` wins the longest-prefix contest by
    default — reporting the ROOT filesystem's type for a path that is really on
    NFS. The refusal is then bypassed by nothing more exotic than a space in a
    directory name. Space, tab, newline and backslash are the four the kernel
    escapes.
    """
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 3 < len(field) and field[i + 1:i + 4].isdigit():
            try:
                out.append(chr(int(field[i + 1:i + 4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(field[i])
        i += 1
    return "".join(out)


def _mount_table() -> list[tuple[str, str, str | None]]:
    """(mount point, filesystem type, "major:minor") triples, decoded.

    Empty when unreadable. Prefers `/proc/self/mountinfo`, which is
    namespace-correct — inside a container `/proc/mounts` can describe the
    host's view — and which is also the only one of the two that carries a
    DEVICE identity. `/proc/mounts` is the fallback and supplies `None` there.

    The device matters because a mount point is not a unique mount identity.
    Linux permits stacked mounts on the same path, and only the topmost is
    visible; mountinfo exposes mount and device IDs precisely because the path
    alone cannot tell them apart.
    """
    entries: list[tuple[str, str, str | None]] = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            for line in handle:
                # 0=mount id 1=parent 2=major:minor 3=root 4=mount point
                # ... " - " fstype source options
                head, _, tail = line.partition(" - ")
                head_fields, tail_fields = head.split(), tail.split()
                if len(head_fields) < 5 or not tail_fields:
                    continue
                entries.append((_unescape_mount_field(head_fields[4]), tail_fields[0], head_fields[2]))
        if entries:
            return entries
    except OSError:
        pass
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 3:
                    continue
                entries.append((_unescape_mount_field(fields[1]), fields[2], None))
    except OSError:
        return []
    return entries


def detect_filesystem(path: Path) -> str | None:
    """Best-effort filesystem name for the directory holding `path`.

    Returns None when the platform cannot answer. None means UNKNOWN, and
    `is_unreliable_filesystem` treats unknown as unreliable: a filesystem we
    could not identify is one where the exclusion guarantee cannot be
    established, and starting anyway would contradict the reasoning that made
    an unreliable filesystem an abort rather than a warning. A platform with no
    readable mount table therefore needs the explicit operator override.
    """
    # Inspect the FILE when it exists, and follow symlinks: a state path that is
    # a symlink or a file-level bind mount lives on a different filesystem from
    # its directory entry, and it is the file sqlite opens that has to hold the
    # lock. Falling back to the parent is only for a database not yet created.
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    target = resolved if resolved.exists() else resolved.parent
    if str(target) == "":
        target = Path(".")
    # statvfs does not expose f_type portably — some Linux builds do, most do
    # not — so it is an optimisation, never the gate. A statvfs that fails
    # (the target does not exist yet, EACCES on a parent) must NOT skip the
    # mount-table lookup: that ordering reported "unknown" for every path whose
    # database had not been created, which is every first start.
    try:
        f_type = getattr(os.statvfs(target), "f_type", None)
    except OSError:
        f_type = None
    if f_type is not None and f_type in _FS_MAGIC:
        return _FS_MAGIC[f_type]
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target

    # The target's OWN device, which names the mount actually backing it. Two
    # mountinfo entries can share a mount point — Linux stacks mounts, and only
    # the topmost is visible — so path matching alone can return the hidden
    # entry. If the hidden one is ext4 and the visible overmount is NFS, a
    # path-only answer is confidently wrong, which is worse than unknown now
    # that unknown refuses.
    device = None
    try:
        stat_result = os.stat(target)
        device = f"{os.major(stat_result.st_dev)}:{os.minor(stat_result.st_dev)}"
    except OSError:
        device = None

    table = _mount_table()
    if device is not None and any(entry_device is not None for _, _, entry_device in table):
        candidates = [
            (mount_point, fs_type)
            for mount_point, fs_type, entry_device in table
            if entry_device == device
        ]
        if candidates:
            # Path specificity decides only WITHIN the matching device, where
            # every candidate really does back this file.
            longest = max(len(mount_point) for mount_point, _ in candidates)
            types = {fs for mount_point, fs in candidates if len(mount_point) == longest}
            # Two entries equally specific on the same device but disagreeing
            # about the filesystem cannot be told apart here either. Same rule
            # as the path-only fallback: unknown refuses, a guess would not.
            return types.pop() if len(types) == 1 else None
        # The device is known and no mount claims it. Guessing from paths here
        # is exactly the confident-wrong answer this branch exists to avoid.
        return None

    # Path-only fallback: /proc/mounts, or a target we could not stat.
    best_len, best_fs, ambiguous = -1, None, False
    for mount_point, fs_type, _ in table:
        if str(resolved) == mount_point or str(resolved).startswith(mount_point.rstrip("/") + "/"):
            if len(mount_point) > best_len:
                best_len, best_fs, ambiguous = len(mount_point), fs_type, False
            elif len(mount_point) == best_len and fs_type != best_fs:
                # Two equally specific mounts disagreeing about the filesystem
                # is precisely the case a path cannot resolve. Unknown refuses;
                # picking one would not.
                ambiguous = True
    return None if ambiguous else best_fs


@dataclass
class InstanceLock:
    """A held coordinator lock. Release by closing, or by letting the process die."""

    path: Path
    connection: sqlite3.Connection
    filesystem: str | None
    state_preflight: object | None = None
    previous_instance: tuple | None = None

    def release(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:
            pass


def acquire_instance_lock(state_path: str | os.PathLike[str], *, allow_unreliable_fs: bool = False) -> InstanceLock:
    """Take the exclusive coordinator lock on `state_path`, or refuse to start.

    `allow_unreliable_fs` exists for operators who know their storage better
    than this check does. It is deliberately a keyword argument on this
    function rather than an env var: turning it on should be a decision
    somebody wrote down, not a value inherited from a Compose file.
    """
    # Validate the RAW string first. Path("") is Path("."), which is truthy and
    # a real directory, so a check made after conversion can never fire — an
    # unset TASK_STATE_PATH would open the working directory and report an
    # unrelated sqlite error. Whitespace is worse: "   " locks a database whose
    # filename is three spaces, and reports success.
    raw = os.fsdecode(state_path).strip() if not isinstance(state_path, str) else state_path.strip()
    if not raw:
        raise CoordinatorLockError("coordinator_state_path_empty", "TASK_STATE_PATH is required in coordinator mode")
    path = Path(raw)

    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise CoordinatorLockError(
            "coordinator_state_dir_unwritable",
            f"cannot create the directory for TASK_STATE_PATH {path}: {error}",
        ) from error

    filesystem = detect_filesystem(path)
    if not allow_unreliable_fs and is_unreliable_filesystem(filesystem):
        raise CoordinatorLockError(
            "coordinator_state_fs_unreliable",
            f"TASK_STATE_PATH {path} is on {filesystem or 'an unidentifiable filesystem'}, where an exclusive lock may not "
            "reliably exclude a second coordinator. Move it to a local filesystem "
            "(ext4/xfs/btrfs/apfs). Two coordinators on one state file is silent corruption, "
            "not a degraded mode.",
        )

    try:
        connection = sqlite3.connect(str(path), timeout=0.0, isolation_level=None)
    except sqlite3.Error as error:
        raise CoordinatorLockError("coordinator_state_unopenable", f"cannot open {path}: {error}") from error

    try:
        connection.execute("PRAGMA locking_mode = EXCLUSIVE")
        # Version/foreign-file checks must precede the first schema write: an
        # older binary must report the version mismatch, not fail against a
        # newer coordinator_instance shape with a generic sqlite error.
        import coordinator_state
        state_preflight = coordinator_state.preflight(connection)
        previous_instance = None
        if state_preflight.legacy_6b1 or state_preflight.version == coordinator_state.SCHEMA_VERSION:
            try:
                previous_instance = connection.execute(
                    "SELECT pid, acquired_at FROM coordinator_instance WHERE id=1"
                ).fetchone()
            except sqlite3.OperationalError:
                previous_instance = None
        # The PRAGMA alone changes nothing: sqlite takes the exclusive lock on
        # the first WRITE and holds it until the connection closes.
        connection.execute(coordinator_state.INSTANCE_DDL)
        connection.execute(
            "INSERT INTO coordinator_instance (id, pid, acquired_at) VALUES (1, ?, datetime('now'))"
            " ON CONFLICT(id) DO UPDATE SET pid = excluded.pid, acquired_at = excluded.acquired_at",
            (os.getpid(),),
        )
    except sqlite3.OperationalError as error:
        connection.close()
        # "database is locked" is the expected refusal, and it is the whole
        # feature. Anything else is a genuine fault and must not be reported as
        # "another coordinator is running" — that sends an operator hunting a
        # process that does not exist.
        if "locked" in str(error).lower() or "busy" in str(error).lower():
            raise CoordinatorLockError(
                "coordinator_already_running",
                f"another coordinator holds the lock on {path}. A second coordinator "
                "would forward the same envelopes twice and answer the same holds twice.",
            ) from error
        raise CoordinatorLockError("coordinator_state_unwritable", f"cannot write {path}: {error}") from error
    except sqlite3.Error as error:
        connection.close()
        raise CoordinatorLockError("coordinator_state_unwritable", f"cannot write {path}: {error}") from error
    except BaseException:
        connection.close()
        raise

    return InstanceLock(
        path=path,
        connection=connection,
        filesystem=filesystem,
        state_preflight=state_preflight,
        previous_instance=previous_instance,
    )


def resolve_mode(environ: dict[str, str] | None = None) -> str:
    """Resolve YUGO_MODE. Blank or unset is `bot` — the pre-v0.6 behaviour.

    An unrecognised value aborts rather than falling back to `bot`: an operator
    who writes YUGO_MODE=coord meant to run a coordinator, and silently
    starting a bot instead gives them a process that looks healthy, heartbeats,
    and forwards nothing. Same reasoning as SANDBOX_MODE in v0.4e.
    """
    env = os.environ if environ is None else environ
    raw = (env.get("YUGO_MODE") or "").strip().lower()
    if not raw:
        return "bot"
    if raw in ("bot", "coordinator"):
        return raw
    raise CoordinatorLockError(
        "coordinator_mode_invalid",
        f"YUGO_MODE must be 'bot' or 'coordinator'; got {raw!r}",
    )

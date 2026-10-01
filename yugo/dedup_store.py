"""Durable SQLite envelope-id dedup store — the claim/lease arbiter.

Split out of `fleet_bus.py` so a sibling harness can vendor the claim store
without the 2,500-line bus adapter (codex-container#21 D1). Everything here is
stdlib plus `dedup_verification`, and it must STAY that way: an import back into
`fleet_bus` would put the whole adapter — nats-py, yaml, the envelope contract —
back into the dependency closure and defeat the split.

`FleetBusConfigError` therefore lives HERE rather than in `fleet_bus.py`, which
re-exports it. The name reads oddly in this file, and the alternative reads
worse: a store-local error class would change which exception the already-shipped
startup gate raises, and a third shared module would make the vendorable unit
three files instead of two. The class, its name and its messages are unchanged,
so `except fleet_bus.FleetBusConfigError` keeps working on both sides.

The schema, the verification record and the monotonic lease fencing are SPEC-26
(`docs/SPEC-26-clock-step-lease-fencing.md`) and are LIVE on deployed bots with
real claim state; this module was extracted verbatim.
"""

from __future__ import annotations

import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dedup_verification import open_verified_store, read_boot_id, valid_boot_id, VerificationError


class FleetBusConfigError(RuntimeError):
    """Bus is enabled but its configuration is unusable.

    Raised at import (startup) so the operator gets the same fatal treatment
    SPEC §10 gives a missing persona, rather than a bot that boots deaf. The
    message ALWAYS names the offending env var — an anonymous traceback in a
    restart-looping container is what this class exists to prevent.
    """


DEFAULT_DEDUP_TTL_S = 8 * 24 * 60 * 60
MIN_DEDUP_TTL_S = 7 * 24 * 60 * 60
DEFAULT_DEDUP_LEASE_S = 60
DEDUP_PRUNE_EVERY = 256
DEDUP_PRUNE_LIMIT = 100
# A single bounded batch cannot keep up: at 100 deleted per 256 admitted the
# expired backlog grows ~156 rows per 256 arrivals. `prune` therefore loops
# bounded batches until the expired set is drained or this budget is spent,
# and the budget is deliberately larger than DEDUP_PRUNE_EVERY so a steady
# arrival stream loses ground on every sweep rather than gaining it.
DEDUP_PRUNE_BUDGET = 4 * DEDUP_PRUNE_EVERY
# Renewal cadence for a live owner, as a fraction of the lease. Driven by a
# MONOTONIC timer in-process: the stored lease_until_s stays wall-clock
# because it is compared across processes, and a monotonic value is not
# comparable outside the process that read it.
#
# What renewal buys: a turn that simply outlives its lease is no longer handed
# to a second worker while the first is still executing.
#
# What it does NOT buy, stated plainly because the opposite was claimed here
# before: it does not defend against a forward wall-clock step. The stored
# deadline is wall-clock, so a jump forward makes a live claim instantly
# expired and a rival can take it before the owner's next renewal tick, with
# the owner's callback still running. A monotonic CADENCE does not change the
# wall-clock PREDICATE that admits the competitor. Closing that needs a real
# clock domain — boot id plus monotonic deadlines, with reboot recovery — and
# is tracked as yugo#26. Until then a clock-step-induced overlap is an
# accepted duplicate under the at-least-once contract.
#
# And a clock step is NOT the only way a lease is lost: a renewal failure, a
# store fault, or an event-loop stall longer than the lease produce the same
# takeover. Owner fencing must not be described as if yugo#26 were its only
# prerequisite.
DEDUP_LEASE_RENEW_RATIO = 0.4


def _check_dedup_path_usable(path: str) -> None:
    """Raise FleetBusConfigError naming the path, or return.

    The parent directory must EXIST; this no longer creates it. A directory
    conjured on demand turns a wrong deployment path into a brand-new empty
    claim database, which is the silent-second-store failure of #29.

    The store FILE may legitimately be absent — SPEC §14 reads its absence as
    initial creation (`DeliverPolicy=New`) rather than as a fault — so absence
    is not checked here. Only a file that exists and cannot be written, or
    cannot be read as SQLite, is a fault.
    """
    parent = Path(path).parent
    if not parent.is_dir():
        raise FleetBusConfigError(
            f"durable dedup store path {path!r} is unusable: parent directory "
            f"{str(parent)!r} does not exist. Create it as part of deployment; "
            f"the store does not create it."
        )
    if not os.access(parent, os.W_OK):
        raise FleetBusConfigError(
            f"durable dedup store path {path!r} is unusable: parent directory "
            f"{str(parent)!r} is not writable"
        )
    target = Path(path)
    if target.exists() and not os.access(target, os.W_OK):
        raise FleetBusConfigError(
            f"durable dedup store path {path!r} is unusable: the file exists "
            f"and is not writable"
        )


class DurableEnvelopeDedupStore:
    """SQLite envelope-id claims retained for 8d (7d stream age + 1d slack).

    A primary-key INSERT OR IGNORE is the concurrency arbiter. The lock
    serializes this adapter's callbacks on one connection; the UNIQUE key also
    makes competing processes deterministic.
    """

    def __init__(self, path: str, ttl_s: int = DEFAULT_DEDUP_TTL_S,
                 lease_s: int = DEFAULT_DEDUP_LEASE_S) -> None:
        if not path.strip():
            raise FleetBusConfigError(
                "durable dedup store path is required and has no default — pass a "
                "stable, bot-specific SQLite path, or ':memory:' for a test that "
                "wants no durability"
            )
        if path != ":memory:" and ttl_s < MIN_DEDUP_TTL_S:
            raise ValueError("durable dedup TTL must be at least the 7-day stream max_age")
        if not math.isfinite(lease_s) or lease_s <= 0:
            raise ValueError("dedup lease must be positive and finite")
        if path != ":memory:":
            _check_dedup_path_usable(path)
        try:
            self._db = open_verified_store(path)
        except Exception as exc:
            raise FleetBusConfigError(
                f"durable dedup store at {path!r} is unusable: {exc}"
            ) from exc
        self._ttl_s = ttl_s
        self._lease_s = lease_s
        self._lock = threading.Lock()
        self._claims = 0

    @staticmethod
    def _validate_metadata(boot: object, deadline: object) -> None:
        if boot == "":
            return  # The supported legacy discriminator, not malformed.
        if not valid_boot_id(boot) or type(deadline) is not int or not 0 < deadline <= 2**53 - 1:
            raise VerificationError("malformed new-format lease metadata")

    def _lease_clock(self) -> tuple[str, int, int]:
        boot = read_boot_id()  # Failure never authorizes a fresh claim or takeover.
        mono = time.monotonic_ns() // 1_000_000
        deadline = mono + math.ceil(self._lease_s * 1000)
        if not 0 <= mono < deadline <= 2**53 - 1:
            raise VerificationError("invalid monotonic lease clock")
        return boot, mono, deadline

    def claim(self, envelope_id: str, req_id: str, now_s: float | None = None) -> tuple[bool, str, str | None]:
        now = datetime.now(timezone.utc).timestamp() if now_s is None else now_s
        owner = uuid.uuid4().hex
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                boot, mono, deadline = self._lease_clock()
                self._claims += 1
                if self._claims % DEDUP_PRUNE_EVERY == 0:
                    self.prune(now)
                self._db.execute(
                    "DELETE FROM envelope_dedup_v2 WHERE envelope_id=? AND state='completed' AND first_seen_s < ?",
                    (envelope_id, now - self._ttl_s),
                )
                cursor = self._db.execute(
                    "INSERT OR IGNORE INTO envelope_dedup_v2 (envelope_id,first_seen_s,req_id,state,lease_owner,lease_until_s,lease_boot_id,lease_until_mono_ms) VALUES (?,?,?,'pending',?,?,?,?)",
                    (envelope_id, now, req_id, owner, now + self._lease_s, boot, deadline),
                )
                if cursor.rowcount == 1:
                    result = (False, req_id, owner)
                elif cursor.rowcount != 0:
                    raise VerificationError("fresh claim affected an unexpected number of rows")
                else:
                    row = self._db.execute(
                        "SELECT req_id,state,lease_until_s,lease_owner,lease_boot_id,lease_until_mono_ms FROM envelope_dedup_v2 WHERE envelope_id=?",
                        (envelope_id,),
                    ).fetchone()
                    if row is None:
                        raise VerificationError("fresh claim inserted zero rows without an existing claim")
                    eligible = False
                    if row[1] == "pending":
                        self._validate_metadata(row[4], row[5])
                        eligible = row[2] <= now if row[4] == "" else (row[4] != boot or row[5] <= mono)
                    if eligible:
                        # All ownership metadata changes in ONE conditional write.
                        # BEGIN IMMEDIATE serializes readers/writers; the snapshot
                        # predicates also prevent authorizing a zero-row update.
                        changed = self._db.execute(
                            "UPDATE envelope_dedup_v2 SET lease_owner=?,lease_until_s=?,lease_boot_id=?,lease_until_mono_ms=? "
                            "WHERE envelope_id=? AND state='pending' AND lease_owner=? "
                            "AND lease_until_s=? AND lease_boot_id=? AND lease_until_mono_ms=?",
                            (owner, now + self._lease_s, boot, deadline, envelope_id, row[3], row[2], row[4], row[5]),
                        ).rowcount
                        result = (False, row[0], owner) if changed == 1 else (True, row[0], None)
                    else:
                        result = (True, row[0], None)
                self._db.execute("COMMIT")  # Execution is authorized only AFTER commit.
                return result
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    @property
    def lease_s(self) -> float:
        """The lease this store issues. Callers deriving a renewal cadence MUST
        read this rather than the module default, which this store may not use."""
        return self._lease_s

    def renew(self, envelope_id: str, owner: str, now_s: float | None = None) -> bool:
        """Extend a live owner's lease. False means the lease was already lost.

        False means another consumer has taken the envelope and anything this
        turn still does is the duplicate, not the original.

        Stated honestly: no caller can act on that today. There is no cancel
        handle at this boundary, so `_renew_claim_until_done` records the loss
        and stops renewing while the turn runs to completion. That is the
        at-least-once contract working as designed, not a gap in this method —
        closing it is yugo#24. The return value exists so a future caller with
        a cancel handle has something to fence on.
        """
        now = datetime.now(timezone.utc).timestamp() if now_s is None else now_s
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                boot, _, deadline = self._lease_clock()
                row = self._db.execute(
                    "SELECT lease_boot_id,lease_until_mono_ms FROM envelope_dedup_v2 "
                    "WHERE envelope_id=? AND lease_owner=? AND state='pending'",
                    (envelope_id, owner),
                ).fetchone()
                changed = 0
                if row is not None:
                    self._validate_metadata(*row)
                    if row[0] in ("", boot):
                        changed = self._db.execute(
                            "UPDATE envelope_dedup_v2 SET lease_until_s=?,lease_boot_id=?,lease_until_mono_ms=? "
                            "WHERE envelope_id=? AND lease_owner=? AND state='pending'",
                            (now + self._lease_s, boot, deadline, envelope_id, owner),
                        ).rowcount
                self._db.execute("COMMIT")
                return changed == 1
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def complete(self, envelope_id: str, owner: str) -> bool:
        """Mark done. False means we no longer owned it, so we did NOT finish it.

        A lost owner must not be recorded as a successful completion: the
        in-memory fast paths key off this return, and promoting a stale claim
        would suppress the real owner's result.
        """
        with self._lock:
            # `AND state='pending'` matches the TypeScript port exactly. Without
            # it a same-owner double-complete returns True here and False there,
            # and the return value is now load-bearing on both sides.
            return self._db.execute(
                "UPDATE envelope_dedup_v2 SET state='completed' "
                "WHERE envelope_id=? AND lease_owner=? AND state='pending'",
                (envelope_id, owner),
            ).rowcount == 1

    def release(self, envelope_id: str, owner: str) -> bool:
        with self._lock:
            return self._db.execute(
                "DELETE FROM envelope_dedup_v2 WHERE envelope_id=? AND lease_owner=? AND state='pending'",
                (envelope_id, owner),
            ).rowcount == 1

    def prune(self, now_s: float, budget: int = DEDUP_PRUNE_BUDGET) -> int:
        """Delete expired claims in bounded batches until drained or out of budget.

        Bounded batches keep any single statement short; the loop is what makes
        cleanup able to OUTPACE ingestion. A short batch means the expired set
        is exhausted, so the loop stops without spending the rest of the budget.
        """
        cutoff = now_s - self._ttl_s
        deleted = 0
        while deleted < budget:
            batch = min(DEDUP_PRUNE_LIMIT, budget - deleted)
            n = self._db.execute(
                "DELETE FROM envelope_dedup_v2 WHERE rowid IN (SELECT rowid FROM envelope_dedup_v2 "
                "WHERE state='completed' AND first_seen_s < ? ORDER BY first_seen_s LIMIT ?)",
                (cutoff, batch),
            ).rowcount
            deleted += n
            if n < batch:  # expired set exhausted
                break
        return deleted

    def prune_idle(self, now_s: float | None = None) -> int:
        """Sweep on a quiet lane, where no claim arrives to trigger the counter.

        Without this, a stream that goes quiet after a burst keeps its expired
        rows until the next arrival — the backlog survives precisely when
        there is most capacity to clear it.
        """
        now = datetime.now(timezone.utc).timestamp() if now_s is None else now_s
        with self._lock:
            return self.prune(now)

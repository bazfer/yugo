---
title: "yugo #26 — clock-step-safe lease fencing"
status: APPROVED for implementation — v3.2, Release 1 MERGED, Release 2 gated (2026-09-24)
updated: 2026-09-24
issue: https://github.com/bazfer/yugo/issues/26
---

# #26 — Clock-step-safe lease fencing

**Consolidated from three design rounds.** v1 and v2 were both rejected by Ohm;
this is v3 with his findings folded in. Build from THIS, not from the issue
comments — they contain two superseded designs and reading them in order is a
good way to implement a rejected one.

## The defect

`envelope_dedup_v2.lease_until_ms` / `lease_until_s` is a **wall-clock** deadline.
A forward step of the host clock makes every live claim instantly expired, and a
rival's `claim()` takes the expired-pending row through the recovery branch while
the original owner's callback is still executing.

**Renewal does not close this**, and #25 originally claimed it did. The renewal
*cadence* is timer-driven, so a clock step cannot stretch or skip the interval —
but the cadence is not the predicate. The competitor is admitted by
`lease_until <= now`, which is pure wall-clock. A 60 s deadline can be expired the
instant after an NTP correction, well before the owner's next 24 s tick.

## Scope note — what this is and is not

The settled contract, as approved in SPEC §6.4, is **deduplicated admission;
effects may repeat; eventual execution is not guaranteed.** A wrongful takeover
produces a repeated effect, which that contract already permits. (The earlier
phrasing here, "at-least-once effects with live-owner fencing", promised more
than the system delivers and was superseded by §6.4 in PR #30.) So this is a **quality-of-implementation** fix, not a violation of a
promise. Worth doing; not worth doing before #22, #28 or #23. (Deet's view,
stated 2026-09-24; Fernando chose to proceed anyway, which is his call.)

## 1. Supported coordination domain — PRECONDITIONS

Read off the code, not assumed:

- **TypeScript** defaults to `~/.claude/fleet-bus-dedup-<botName>.sqlite`
  (`src/fleet-bus.ts:903`). Per bot, per home directory.
- **Python** defaults to **`/var/lib/yugo/<bot_name>-dedup.sqlite`**
  (`yugo/fleet_bus.py:1377-1378`, via `load_config_from_env`, which `bot.py:152`
  uses), overridable with `YUGO_DEDUP_STORE_PATH`. **File-backed, per bot.**

  **CORRECTED in v3.2** after Codex and Ohm both flagged it. v3.1 said Python
  "defaults to `:memory:` — i.e. no cross-process coordination at all", citing
  `fleet_bus.py:1433`. That line is the **constructor fallback** for a config with
  no path set; it is not the production startup path, which always supplies one.
  I read one line without following the call path. The practical consequence is
  the opposite of what v3.1 claimed: **every Python bot is a real file accessor
  with a live locality question**, so `:memory:` is a rare configuration rather
  than the norm, and the `:memory:` exception below applies to far fewer
  deployments than v3.1 implied.

- **The two ports do not share a schema, so they can never share a file.**
  TypeScript stores `first_seen_ms` / `lease_until_ms` as INTEGER milliseconds
  (`src/fleet-bus.ts:133-137`); Python stores `first_seen_s` / `lease_until_s` as
  REAL seconds (`yugo/fleet_bus.py:1142-1144`). Different names and different
  units. This narrows the cutover: "all accessors of this file" is per bot **and**
  per port.

- SQLite WAL is enabled, and WAL participants must share a host.

**Therefore: processes sharing one SQLite file on one host. Never cross-host.**

Contention is either **sequential** (a process dies, a replacement finds the
expired row — the dominant real case) or **concurrent** (two `primary` instances
of one bot on one host, which `publish-only` mode exists to prevent).

**Four conditions. Violating any one FAILS CONSUMER STARTUP — it does not fall
back to legacy behaviour.** Corrected in v3.2 at Ohm's direction, 2026-09-24: the
original "falls back to legacy" wording contradicted §2, which forbids arbitrating
an existing new-format row by wall clock. A per-row fallback would require a
consumer that cannot trust its own clock domain to nevertheless classify every row
correctly before touching it. Making this a startup condition removes the decision
rather than arbitrating it.

A consumer that cannot establish these refuses to consume at all. Non-consuming
functionality may remain available **provided it neither acquires claims nor
mutates lease metadata.** The `:memory:` store is the standing exception to the
file-locality and WAL checks (see §2) — no file, no sharing, nothing to verify.

The four conditions:

1. One host-local SQLite database. Not a network mount, not a replicated copy.
2. All participants read the same boot-ID source:
   `/proc/sys/kernel/random/boot_id`.
3. All participants share one monotonic clock domain — **no time namespace
   between them.** Linux time namespaces can offset `CLOCK_MONOTONIC` even on one
   host, and these bots are containers, so this is a live concern rather than a
   theoretical one.
4. Clock API named explicitly, not "monotonic milliseconds":
   **TS `process.hrtime.bigint()`**, **Python `time.monotonic_ns()`**. Both are
   `CLOCK_MONOTONIC`; both survive a wall-clock step; neither is process-relative.

### Measured on the live deployment, 2026-09-24

```
host boot_id  eb1d102e-5f57-4148-a3d6-bbfdcbd937d9
vec  boot_id  eb1d102e-5f57-4148-a3d6-bbfdcbd937d9
ohm  boot_id  eb1d102e-5f57-4148-a3d6-bbfdcbd937d9

CLOCK_MONOTONIC (/proc/uptime), sampled sequentially
host 3112047.18   vec 3112047.29   ohm 3112047.36
```

Containers read the host's `boot_id` and share its monotonic clock; the ~0.2 s
spread is three sequential `docker exec` calls, not an offset. **No time namespace
in use.** Re-runnable: `cat /proc/sys/kernel/random/boot_id` and
`awk '{print $1}' /proc/uptime`, on the host and inside each container.

## 2. Mechanism

```sql
ALTER TABLE envelope_dedup_v2 ADD COLUMN lease_boot_id TEXT NOT NULL DEFAULT '';
ALTER TABLE envelope_dedup_v2 ADD COLUMN lease_until_mono_ms INTEGER NOT NULL DEFAULT 0;
```

Takeover decision on a `pending` row:

| Row's `lease_boot_id` | Meaning | Decision |
|---|---|---|
| equals mine | same boot, same monotonic domain | compare `lease_until_mono_ms` against my monotonic clock — **authoritative, immune to wall-clock steps** |
| differs, non-empty | the host rebooted; that process **cannot** exist | **take over immediately** — faster than today |
| empty string | legacy row, pre-upgrade writer | fall back to the **existing wall-clock predicate** |

**No wall-clock path exists for any row a current writer produced.** A boot-id
mismatch is a death certificate, not an unknown — which is precisely what v1
wrongly argued was impossible.

**Boot-ID read failure — CORRECTED in v3.1 after Ohm's fourth review.**

"Degrade to legacy" was too loose. It permitted this, with no fabricated mismatch
and the same duplicate-execution risk:

1. A owns a new-format pending row with an **unexpired** monotonic lease.
2. B cannot read its boot ID and falls back to the wall-clock predicate.
3. Wall time steps forward.
4. **B takes over A's live claim.**

The distinction I missed is **legacy ROW versus legacy READER**. Degrading the row
format is safe; degrading the reader's capability is not. Explicit rules:

| Situation | Behaviour |
|---|---|
| Existing **legacy** row (empty `lease_boot_id`) | legacy wall-clock predicate permitted, with the documented vulnerability |
| Existing **new-format** row + local boot ID unavailable | **REFUSE takeover. Surface an error.** Do not clear its metadata. Do not use wall time. |
| Boot identity unavailable at startup | **Fail consumer startup** (Ohm's stated preference for Release 2). Finding only legacy rows at startup does not guarantee the database stays legacy afterwards. Non-consuming functionality may remain available **provided it neither acquires claims nor mutates lease metadata.** |

A fabricated mismatch remains the one failure mode that would make this worse than
the bug it fixes — but so is a fabricated *downgrade*.

**Two boundary cases, made explicit at Ohm's direction on approval:**

- **Missing local boot identity must also prevent acquiring a FRESH new-protocol
  claim**, not only a takeover. Otherwise a boot-blind consumer mints claims it
  cannot later defend.
- **Non-empty row boot ID + missing or invalid monotonic deadline is MALFORMED
  METADATA, not a legacy row.** Refuse takeover and report it. Treating it as
  legacy is exactly the downgrade this section exists to prevent.
- **An EMPTY `lease_boot_id` is the explicitly supported legacy discriminator, not
  corruption.** Malformed-metadata refusal applies to **new-format** rows only.
  Added at Ohm's direction, 2026-09-24.
- **An invalid non-empty boot ID must NEVER be counted as a reboot mismatch.** A
  mismatch authorises immediate takeover, so reading an unparseable value as
  "different boot" manufactures exactly the fabricated mismatch this design treats
  as its worst failure mode. Invalid means malformed: refuse and report.

A legacy row's documented vulnerability stays acceptable during migration.
Deliberately downgrading an existing new-format row does not.

**Verify, don't infer:** `PRAGMA journal_mode=WAL` can leave the previous mode
unchanged. Check the returned value; if WAL is not active on a file-backed store
the same-host inference is unsupported and **consumer startup fails** per §1. It
does not fall back to the legacy predicate. (`:memory:` is exempt — see below.)

**But WAL success is not a locality detector.** SQLite documents same-host
operation as a *requirement*, not something it enforces. So for this rollout:
**record the actual database mount and backing storage for each participating
bot, and reject known network-backed stores.** A generic filesystem classifier is
useful defence-in-depth, not proof of the topology.

**`:memory:` is treated separately** from the file-backed WAL requirement. No
file, no sharing, no WAL locality question, so the startup checks in §1 have
nothing to verify and are skipped. **It is NOT the Python default** — v3.1 said so
and was wrong (see §1). A `:memory:` store means a single process with no
coordination at all, which is a deliberate configuration, not the norm.

**Invariant, independent of the above:** on **every** ownership change of a
new-protocol claim, **atomically replace owner, boot ID and monotonic deadline
together** in one statement. "Invalidate" must not be read as leaving the row
temporarily or permanently legacy — that would reintroduce the downgrade hole
above. A takeover that cannot write all three must not take over at all.

**One conditional UPDATE setting all three fields, inside the existing
transaction.** That excludes a committed partial ownership change.

**Claim success requires exactly ONE affected row AND a successful commit.** Zero
affected rows, or any write or commit failure, **must not authorize execution.**

**The complete-metadata requirement applies to fresh INSERTs too**, not only
takeovers.

## 3. Release plan — two releases, and the order is load-bearing

### Release 1 — named-column INSERT, alone

Both ports currently execute, with **no column list**:

```sql
INSERT OR IGNORE INTO envelope_dedup_v2 VALUES (?,?,?,?,?,?)
```

Adding a seventh column makes this fail on six values **regardless of DEFAULT**.
Ohm reproduced it: `table envelope_dedup_v2 has 7 columns but 6 values were supplied`.

**The column names differ per port — one example each, not one shared statement.**
Corrected in v3.2; v3.1 showed the TypeScript names for both, which would have been
wrong if copied into the Python port. PR #32 already implemented both correctly.

TypeScript (INTEGER milliseconds):

```sql
INSERT OR IGNORE INTO envelope_dedup_v2
  (envelope_id, first_seen_ms, req_id, state, lease_owner, lease_until_ms)
VALUES (?,?,?,'pending',?,?)
```

Python (REAL seconds):

```sql
INSERT OR IGNORE INTO envelope_dedup_v2
  (envelope_id, first_seen_s, req_id, state, lease_owner, lease_until_s)
VALUES (?,?,?,'pending',?,?)
```

**This ships first, on its own, in both ports, and nothing else.** A named-column
INSERT is forward-compatible with any number of later DEFAULTed columns.

### Release 2 — columns + predicate

Only after Release 1 has reached **every accessor of a given file**.

**Per-file protocol cutover: ALL ACTUAL FILE ACCESSORS must be STOPPED before
Release 2 operates on that file** — including prune jobs and restartable old
instances. The default filename alone does **not** establish that inventory;
enumerate real accessors. Not optional — Ohm's finding B proves an old participant
can damage new-format rows two ways:

1. It still prunes pending rows by wall-clock, so a forward step lets it **delete a
   new-format live claim**.
2. An old takeover updates `lease_owner` and the wall deadline but leaves the
   monotonic columns untouched, so a new participant reads the **previous** owner's
   deadline as authoritative for the **current** owner.

**Cheap here:** one file per bot, normally one consuming process, so "all accessors
of this file" is "this bot" and the cutover is **stop, upgrade, start**.

**Two traps, written down rather than discovered:**
- A **rolling** restart that briefly overlaps old and new re-opens both paths above.
  Must be stop-then-start, never overlap.
- **Rollback to Release 1 is structurally compatible but silently loses the safety
  property.** The schema still works; the protection does not. Do not describe it
  as an unqualified safe rollback.

## 4. Pruning — corrected scope

**Pending rows become ineligible for EVERY TTL-delete path**, not just the periodic
one:

- `claim()`'s targeted cleanup
- periodic `prune`
- `prune_idle`

Completed-row TTL is unchanged.

**Stated cost:** abandoned pending rows accumulate when an owner dies without
release and nothing retries that envelope. Bounded by the envelope-id space, not by
time. If that proves to matter the answer is a **liveness-gated reaper, not a
clock-gated one** — separate issue, not this one.

## 5. Recovery bounds — narrowed to what is true

- **Reboot** → eligible immediately, *on the next claim attempt*. Nothing
  proactively reclaims.
- **Same-boot crash** → eligible after the remaining lease, measured in the
  monotonic domain.
- **Wall-clock steps** → no effect on **pending-lease arbitration between
  new-protocol participants**.

**Not** "no effect on a non-legacy row" — completed-row TTL remains
wall-clock-sensitive, so a step still moves when a completed row is pruned.

## 6. Tests — sixteen, each mutation-verified

Remove the guard, watch the named test go red. A green suite proves the tests pass,
never that they test anything.

1. **The case that currently passes and must not:** claim, step the clock forward
   past the deadline, rival claims **before the next renewal tick** → refused.
2. Same-boot monotonic expiry → takeover succeeds.
3. Different `boot_id` → immediate takeover.
4. Empty `boot_id` (legacy row) → wall-clock behaviour, unchanged.
5. Backward clock step does not extend a hold.
6. **A forward clock step does not prune a live pending row.**
7. Takeover loses to a renewal under a **serialized transaction ordering** —
    two writers, each in its own `BEGIN IMMEDIATE`, committed in a defined order.
    Reworded in v3.2 on Ohm's non-blocking note: "a renewal interleaved between
    read and write" describes a renewal committing inside another writer's
    `BEGIN IMMEDIATE`, which SQLite's write lock makes impossible. Test the
    orderings that can actually occur, or the test asserts against a fiction.
8. Named-column INSERT works against both the 6-column and 8-column schema.
9. Both ports, same behaviour.
10. **Mixed-version deletion — an executable DEMONSTRATION, not a detection
    guarantee.** Reproduce an old participant deleting a new-format live claim,
    then assert the concrete observable outcome: the resulting absent row is
    **byte-for-byte the state of a never-seen envelope**, and the protocol carries
    no field that distinguishes the two. Do not assert detection or prevention —
    neither is possible here.
11. **Stale-metadata takeover — same shape.** Reproduce an old takeover leaving
    monotonic columns behind, then assert that the resulting row is
    **syntactically well-formed and indistinguishable from a live claim under a
    valid lease** by any discriminator this protocol defines.

    **Why these two assert the negative.** Corrected 2026-09-24 after Vec caught
    the error and Ohm ruled. My first disposition required these tests to assert
    that new code *detects* the damage. It cannot: a deleted row leaves nothing to
    inspect, and stale takeover metadata parses cleanly. That disposition claimed a
    capability the protocol does not have. Tests 10 and 11 instead **demonstrate
    the unsupported-overlap hazard**, which is why the stop-all-accessors cutover
    is the **required mitigation for this protocol** — not because "no code fix
    exists" (a future protocol could add a discriminator and enforce it), but
    because *this* protocol defines none. Name the cutover in each test name so a
    later reader does not "fix" the test by adding runtime prevention this design
    cannot provide.

    Note the limit of what a test can claim: these assert that the produced state
    matches a specific legitimate state and that no discriminator exists, which is
    checkable. They do not and cannot prove universal indistinguishability.

11a. **Malformed-metadata refusal stays a SEPARATE test** from the two above — it
    covers the detectable case (non-empty boot ID with a missing or invalid
    monotonic deadline on a new-format row) and asserts a real runtime guarantee:
    refuse and report. Empty `lease_boot_id` must pass as legacy, not refuse.
12. **Rollback:** Release 1 against a migrated file still functions, with the
    safety property absent and that absence asserted.
13. **Cross-port clock agreement:** TS and Python read the same monotonic domain
    and agree on expiry for one row.
14. **Boot-ID read failure on a NEW-format row refuses takeover** — the precise
    case: unreadable boot ID, then a forward wall-clock step, against an unexpired
    monotonic claim. Must refuse, not fall through to wall-clock.
15. Cross-port clock agreement asserts against **actual `process.hrtime.bigint()`
    and `time.monotonic_ns()` values** — `/proc/uptime` was supporting evidence for
    the design, not verification of the chosen APIs' shared epoch.

## 7. The load-bearing assumption — RESOLVED

Ohm's verdict: **host-local storage is accepted as an explicit, verified deployment
precondition.** It does not need proving for every conceivable deployment. But it
must be *recorded and checked* per bot rather than inferred from WAL succeeding —
see §2. That converts an unproven assumption into a stated precondition with a
check behind it, which is the honest version.

## Review history

- **v1** — local-monotonic observation cache. **Rejected, six counts.** Mixed-fleet
  safety claim false; migration broke the existing INSERT; observation identity
  insufficient (seq reuse); cross-host premise wrong; prune paths unaddressed;
  recovery bound unestablished.
- **v2** — boot_id + monotonic, observation cache abandoned. **Rejected.** Mixed-
  version damage not confined to legacy rows; schema compatibility ≠ protocol
  compatibility.
- **v3** — clock/boot domain measured and stated as preconditions; per-file
  cutover specified; pruning scope corrected; recovery narrowed. **Rejected on one
  count:** boot-ID read failure downgraded arbitration of an existing monotonic
  claim, allowing a forward step to steal a live claim with no fabricated mismatch.
- **v3.1** — this document. Boot-read fallback corrected (legacy ROW vs legacy
  READER); ownership change made atomic across all three fields; storage locality
  recorded per bot rather than inferred from WAL; accessor inventory made explicit;
  `:memory:` separated; cross-port test bound to the real clock APIs.
  **Ohm: "The architecture holds with those rules."** No cache redesign, no third
  release.
- **v3.1 APPROVED 2026-09-24** — *"APPROVE — v3.1 design, for implementation. The
  boot-read downgrade blocker is closed."* Design approval only, explicitly **not**
  code or release approval. Three boundary cases added on approval: fresh-claim
  acquisition blocked without boot identity; malformed metadata distinguished from
  legacy; claim success requires one affected row plus successful commit.
  **Release gates retained: per-file accessor shutdown and verified locality.**
- **v3.2 — 2026-09-24, after Release 1 shipped.** Two contradictions in v3.1 found
  by Vec during implementation, both mine, both ruled on by Ohm:
  1. §1's "violating any one disables the new predicate and falls back to legacy"
     contradicted §2's rule that an existing new-format row must refuse rather than
     use wall time. §1 was written before the boot-read correction and never
     propagated backwards. **Resolved: precondition failure fails consumer
     startup**, with the `:memory:` exception preserved. Both legacy-fallback
     statements removed, including the separate WAL paragraph.
  2. Tests 10–11 required detection the protocol cannot perform — a deleted row
     leaves nothing to inspect, stale takeover metadata parses cleanly. **Resolved:
     they reproduce the hazard and assert concrete observable outcomes**, with
     malformed-metadata refusal split into its own test. Two wording constraints
     from Ohm folded in: empty `lease_boot_id` is the supported legacy
     discriminator, and an invalid non-empty boot ID is never a reboot mismatch.
  Scope note updated to the approved §6.4 contract wording.
  **Release 1 (named-column INSERTs) merged as PR #32, Ohm-approved at `b5d7f15`,
  all CI green. Release 2 remains gated on rollout of Release 1.**
- **v3.2b — 2026-09-24, same day.** Two further corrections, found by Codex and
  confirmed by Ohm on the v3.2 push, both errors of mine:
  1. **Python's production dedup store is file-backed**, not `:memory:`. v3.1 cited
     a constructor fallback as the production default without following the call
     path through `load_config_from_env`. This materially widens the locality
     question: every Python bot is a real file accessor.
  2. **Per-port SQL examples.** The single displayed statement used TypeScript's
     `_ms` columns; Python's are `_s` and REAL-typed. Both ports now shown
     separately, and the schema divergence is stated as a precondition, since it
     means the two ports can never share a dedup file.
  Test 7 reworded per Ohm's non-blocking note: serialized transaction orderings,
  not a renewal committing inside another writer's `BEGIN IMMEDIATE`.

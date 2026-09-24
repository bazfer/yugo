---
title: "yugo #26 — clock-step-safe lease fencing"
status: v3.3 — Release 1 MERGED (undeployed); Release 2 gated; §8 pending review (2026-09-24)
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

  **Read this as a prohibition, not an impossibility** (Ohm, v3.2c): the ports
  **must not** share a file. Nothing stops a misconfiguration pointing both at one
  path, and the failure would be silent-ish rather than loud — which is one more
  reason the accessor inventory in §3 is enumerated from reality rather than
  inferred from the default filename.

- SQLite WAL is enabled, and WAL participants must share a host.

**Therefore: processes sharing one SQLite file on one host. Never cross-host.**

Contention is either **sequential** (a process dies, a replacement finds the
expired row — the dominant real case) or **concurrent** (two `primary` instances
of one bot on one host, which `publish-only` mode exists to prevent).

**Four conditions. Violating any one FAILS CONSUMER STARTUP — it does not fall
back to legacy behaviour.** **The evidence each condition is checked against is
specified in §8** — added after Vec correctly refused to implement this section
without it.
 Corrected in v3.2 at Ohm's direction, 2026-09-24: the
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
3. All participants share one monotonic clock domain — **each reads a zero
   `monotonic` offset**, per §8.1. Linux time namespaces can offset
   `CLOCK_MONOTONIC` even on one host, and these bots are containers, so this is a
   live concern rather than a theoretical one.

   **Reconciled in v3.3 at Ohm's direction.** This condition previously read "no
   time namespace between them", which a zero offset does not prove: a process can
   sit in a *distinct* time namespace that happens to carry a zero offset. The
   requirement is offset equality, not namespace identity — and offset equality is
   the property that actually matters, since two participants each unoffset from
   the initial clock read the same `CLOCK_MONOTONIC` regardless of which namespace
   object they belong to. Stating it as namespace identity claimed more than the
   check delivers and more than the design needs.
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

**`:memory:` is treated separately** from the file-backed WAL requirement.
**Only the file-locality and WAL checks are skipped** — there is no file, no
sharing, and nothing to verify. **Boot-identity and the applicable
monotonic-clock checks still apply, and a fresh claim still requires complete
new-protocol metadata.**

Narrowed in v3.2c at Ohm's direction. My v3.2b wording said "the startup checks
in §1 have nothing to verify and are skipped", which exempted far more than
intended: an in-memory store still mints claims that must be defensible, so a
consumer that cannot establish boot identity must still fail startup regardless
of where its rows live.

**`:memory:` is NOT the Python default** — v3.1 said so and was wrong (see §1). It
means a single process with no cross-process coordination, which is a deliberate
configuration, not the norm.

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
enumerate real accessors.

**How to enumerate — measure, do not infer** (Ohm, v3.2c). Trace **running
processes, loaded modules, effective configuration, and open database files**, and
follow each container path **through its mounts to host backing storage**. Locality
is not exclusively a container-level question.

Specifically, none of the following is evidence that a store does not exist:
a missing default directory, a package that will not import in the image's default
interpreter, or an unset environment variable. A path override, a `:memory:`
configuration, a different deployed adapter, or a mount namespace each explain an
absent path while a real accessor is running. *(I made the missing-directory
inference during the first inventory pass and Ohm corrected it. The constructor
does `mkdir` its parent — but that only tells you what happens when the constructor
runs, not whether it ran.)* Not optional — Ohm's finding B proves an old participant
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

## 6. Tests — sixteen on the lease protocol (below) plus sixteen on the startup interface (§8.7), each mutation-verified

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
- **v3.2c — 2026-09-24.** One contradiction introduced by the v3.2b fix itself and
  caught by Ohm on re-review: the `:memory:` paragraph said the §1 startup checks
  "are skipped", exempting boot-identity and monotonic-clock checks that still
  apply. Narrowed to the file-locality and WAL checks only. Also restated the
  port-schema divergence as a prohibition rather than an impossibility — a
  misconfiguration can still point both ports at one path.

## 8. Deployment verification record — the startup input §1 was missing

Added 2026-09-24 after **Vec refused to implement §1 without it**, and correctly:
§1 makes locality and clock domain *startup preconditions that fail startup when
unmet*, while §2 states the system cannot detect them itself. That is only
coherent if something supplies the evidence. I specified the check and never
specified its input.

Revised once already, after Ohm found four implementation blockers in the first
draft — including a validation sequence that queried SQLite before opening it.
Each is marked below where it applies.

### 8.0 What this is, stated before the mechanism

**Operator-attested deployment verification with drift detection.** It is **not**:

- proof of storage locality,
- protection against a privileged actor changing the environment,
- continuous enforcement — every check below happens at startup.

Any wording that upgrades it beyond that is wrong.

### 8.1 The clock domain needs no attestation — it is measurable

`/proc/self/timens_offsets` reports this process's offsets from the initial time
namespace's clocks:

```
monotonic           0         0
boottime            0         0
```

Rules:

- **Non-zero `monotonic` offset → REFUSE consumption.** This consumer's
  `CLOCK_MONOTONIC` is displaced from the initial namespace's and its deadlines
  are not comparable with a participant reading zero.
- **File absent** (kernel built without `CONFIG_TIME_NS`) **→ REFUSE.** The domain
  cannot be established, which §1 already fails startup for.
- **Parse strictly, in the consuming process.** Read it in the process that will
  do the consuming — not a wrapper, not an entrypoint script, not a health check.
  A parse that does not yield an exact integer pair per line refuses; do not
  treat an unparseable line as zero.

**What a zero offset does and does not establish** (Ohm, v3.3): it establishes
*offset equality with the initial namespace*, **not namespace identity**. A
process can occupy a distinct time namespace carrying a zero offset. That is
fine, and it is why §1 now states the condition as offset equality: two
participants each unoffset from the initial clock read the same
`CLOCK_MONOTONIC`, whichever namespace object they belong to.

**Transitivity is the whole argument.** Each participant independently verifying a
zero offset establishes that all verifying participants share one monotonic
domain, through the initial namespace as the common reference. No participant
learns anything about any other, and no cross-participant coordination exists.

**The offset must not change afterwards.** A startup check is not continuous
enforcement (§8.0). A consumer's time namespace must not be altered while it runs;
that is an operational requirement and is not enforced.

**Measured on this fleet, 2026-09-24:** host, the `vec` container and the
`yugo-coordinator` container all read `monotonic 0 0` / `boottime 0 0`. Docker
does not create time namespaces without `--time-offset`.

Consequence: **the record carries no clock binding.** It attests storage topology
only.

### 8.2 Record input

- **Configuration:** `YUGO_DEDUP_VERIFICATION_RECORD` in both ports — an explicit
  path to an operator-provisioned file.
- **A consumer MUST NOT create, refresh, or repair its own record.** Self-
  attestation is not attestation. There is no "accept current values on mismatch"
  path, no `--force`, and no first-run auto-generation.
- Unset while a file-backed store is configured → **refuse consumption.**

### 8.3 Record schema — concrete types

**Blocker 4 (Ohm): the first draft gave a field list, not a schema.** JSON object;
every field required unless marked optional; unknown top-level fields refuse
rather than being ignored.

| Field | Type | Validation |
|---|---|---|
| `record_version` | integer | Exactly `1`. Any other value refuses. Not a string. |
| `canonical_path` | string | Absolute. Compared to `realpath()` of the configured store, byte-for-byte. |
| `device` | object | Tagged union on `binding`. See §8.3.1 and §8.3.1a. |
| `inode` | **string** | Decimal digits only. **A string because inodes are unsigned 64-bit and JSON numbers lose precision above 2^53.** Compared as a big integer, never as a float. |
| `port` | string | Closed enum: exactly `"typescript"` or `"python"`. Compared with exact string equality against the running port's own constant — never inferred, never matched case-insensitively. A mismatch refuses, because the two schemas differ (§1). |
| `schema_fingerprint` | object | See §8.3.2. |
| `storage_evidence` | object | See §8.3.3. Nested, not a boolean. |
| `participant_inventory` | array of objects | Non-empty. Each: `{process, user, path, method}` — `method` states how it was observed, per §3. |
| `attested_by` | string | Non-empty. |
| `attested_at` | string | RFC 3339 with offset. |

#### 8.3.1 `device` — and the UUID problem

**Blocker 1 (Ohm): `/proc/self/mountinfo` contains no filesystem UUID.** True, and
the first draft asserted otherwise. Its fields are mount ID, parent ID,
`major:minor`, root, mount point, options, optional fields, separator, filesystem
type, source, super options. No UUID anywhere.

**The resolver takes no path-matching step at all.** Corrected in v3.4 after Ohm
rejected the previous rule.

v3.3 selected a mount by finding the longest mountinfo mount point matching the
store's path, resolving ties by taking the last entry in file order. **That rule
is invalid.** Mount visibility follows the mount-ID/parent-ID topology — an
ancestor overmount hides its descendants — and file order does not encode it. A
resolver built on line order gives a different answer when the entries are
reordered, which is a bug waiting on a kernel version bump. Test 29 enshrined the
wrong rule and is replaced below.

The fix is not a topology-aware path resolver. It is to stop resolving by path:

1. **`stat()` the store file. `st_dev` IS the governing device**, by definition —
   the kernel resolved the path already, including every overmount, and reports
   the device the file actually lives on. Take `major(st_dev)` and `minor(st_dev)`.
2. **Reverse-resolve a UUID:** for each entry in `/dev/disk/by-uuid/`, `stat()` its
   target and compare `st_rdev` to that device. A match yields the UUID.

Two `stat()` calls. No mountinfo parsing, no longest-prefix rule, no tie-break, no
escape decoding, and **no dependence on entry order** — the order-independence
Ohm asked for is structural rather than tested-for.

**Verified on this host, 2026-09-24:** the store's `st_dev` is 2049 → major 8,
minor 1; `/dev/disk/by-uuid/038e83cc-8b2a-4b94-a1a8-1bae5c97779a` has `st_rdev`
major 8, minor 1. Exact match.

**`mount_point` and `fstype` move out of the compared binding** and into
`storage_evidence` as descriptive fields (§8.3.3). They are useful to a human
reading a record and they are **never compared at startup**, so no topology
question can affect the verdict. Where the kernel offers `statx()` with
`STATX_MNT_ID` (Linux 5.8+), provisioning uses the returned mount ID to select the
mountinfo entry by **exact mount-ID match** — unique and order-independent — and
otherwise records them as unresolved. Neither outcome changes a startup decision.

**Dependencies and support limits of the UUID half, which are real:**

- `/dev/disk/by-uuid` is populated by udev. It is frequently **absent inside
  containers**, and absent on systems not running udev.
- Stacked and virtual filesystems — LVM, btrfs subvolumes, overlayfs, ZFS — may
  present a device with no `by-uuid` entry, or one that does not identify the
  underlying storage.
- A tmpfs or other non-persistent filesystem has no UUID by design.

Therefore the record carries **two binding strengths**, and says which it has:

| `device.binding` | Contents | Reboot behaviour |
|---|---|---|
| `"uuid"` (**strong**, preferred) | `fs_uuid` | Stable. **No re-attestation after reboot.** |
| `"devno"` (**weak**, fallback) | `major`, `minor`, **`attested_boot_id`** | **Requires re-attestation after reboot — and that requirement is ENFORCED, not documented.** See below. |

**Blocker 1 (Ohm, v3): `devno` could not detect a reboot.** Device numbers often
come back identical, so a weak record would silently pass and nobody would
re-attest. The rule existed only in prose.

A `"devno"` record therefore carries **`attested_boot_id`**, read from
`/proc/sys/kernel/random/boot_id` at provisioning time. Startup compares it to the
current boot ID; **a mismatch refuses** and the error names the provisioning
command. This is the one place boot identity enters the record, and it is doing a
different job from §2's row metadata — there it discriminates takeover, here it
detects that a weak binding's assumptions may no longer hold.

**A `"uuid"` record is NEVER downgraded to `"devno"`.** If a record declares
`"uuid"` and resolution fails at startup, that **refuses** — it does not fall back.
Silent downgrade would convert a strong binding into a weak one at exactly the
moment the environment changed underneath it, which is when the binding matters
most.

The provisioning command attempts `"uuid"` first and falls back to `"devno"` only
when the resolver fails, recording *why* in `storage_evidence.uuid_resolution`. It
never silently downgrades.

This is the honest version of the trade I argued for in the first draft. I claimed
UUID binding avoided a re-attestation treadmill; that holds **only where a UUID is
resolvable**, and Ohm was right that I had not specified how it would be.

#### 8.3.1a `device` variant types

A tagged union on `binding`; validated as a whole, with **no field from the other
variant permitted**.

`binding: "uuid"` —

| Field | Type | Validation |
|---|---|---|
| `binding` | string | Exactly `"uuid"`. |
| `fs_uuid` | string | Non-empty; compared case-insensitively, since `by-uuid` casing varies by filesystem. **The only compared field in this variant.** |

`binding: "devno"` —

| Field | Type | Validation |
|---|---|---|
| `binding` | string | Exactly `"devno"`. |
| `major`, `minor` | integer | Non-negative. Integers, not a `"8:1"` string — that form invites sloppy parsing. Compared against `major(st_dev)` / `minor(st_dev)` of the store. |
| `attested_boot_id` | string | The boot ID at provisioning time. Mismatch at startup refuses. |

Any other `binding` value refuses. A `"uuid"` record whose UUID cannot be resolved
at startup refuses rather than falling back to a device-number comparison.

#### 8.3.2 `schema_fingerprint`

`{columns: [{name, declared_type}], table: "envelope_dedup_v2"}` — ordered, taken
from `PRAGMA table_info`. Compared by exact ordered equality.

#### 8.3.3 `storage_evidence`

Nested and specific. `local=true` is a claim, not evidence.

```json
{
  "device_path": "/dev/sda1",
  "mount_point": "/",
  "fstype": "ext4",
  "mount_id_source": "statx STATX_MNT_ID",
  "backing": "local-block",
  "determined_by": "lsblk -o NAME,TYPE,TRAN + findmnt -T <path>",
  "uuid_resolution": "resolved via /dev/disk/by-uuid",
  "inspected_at": "2026-09-24T19:00:00-05:00"
}
```

`backing` is a closed enum: `local-block`, `local-virtual`, `network`, `unknown`.
**`network` and `unknown` refuse** — §1 rejects known network-backed stores, and an
uninspected store is not an inspected one.

### 8.4 Startup validation sequence — ordered, fail-closed

**Blocker 2 (Ohm): the first draft's step 6 queried `PRAGMA journal_mode` before
step 7 opened the database.** Impossible as written. Corrected — identity is
established before opening, and everything requiring a connection happens on one
handle after it:

1. **Clock domain** per §8.1.
2. **Load the record.** Missing, unreadable, malformed, unknown `record_version`,
   or carrying an unknown field → refuse.
3. `realpath()` the configured store; compare to `canonical_path`.
4. `stat()` the store and resolve the device per §8.3.1 — two `stat()` calls, no
   path matching; compare against `device` according to its `binding`.
5. `stat()` the store; compare `inode`. **Retain this value.**
6. **Open the database with `SQLITE_OPEN_READWRITE` and WITHOUT
   `SQLITE_OPEN_CREATE`.** One handle, used for everything below. No creation, no
   migration, no schema bootstrap on this path — a store that does not exist
   refuses (§8.5) rather than being created.
7. **Re-`stat()` the path and compare against both the record and the value
   retained in step 5.** Any change → close and refuse.
8. Read back `PRAGMA journal_mode`; WAL must be active (§2).
9. Verify boot identity is readable (§2).
10. **Three-way schema agreement.** Compare `PRAGMA table_info` against
    `schema_fingerprint` **and against the port's own compiled-in constant for the
    supported schema**. All three must agree; any disagreement refuses.

    **Blocker 3 (Ohm, v3), and the sharpest of the four.** The first draft compared
    the live schema only to the record. A six-column database with a matching
    six-column record agreed with itself and **passed**, letting Release 2 code run
    against a pre-migration store — the exact condition Release 2 exists to avoid.
    A record attests what an operator saw; it cannot attest what the running code
    requires. The port's constant is the authority on that, and the record is
    checked against it, not consulted in its place.
11. Only now admit envelopes.

**Step 7 narrows a TOCTOU window; it does not close it.** `stat(path)` describes
the object at a pathname, not the object SQLite opened. Between steps 5 and 6 the
path can be replaced, and an actor who restores the original inode defeats the
comparison entirely. **Storage topology must remain unchanged during operation —
an operational requirement, not an enforced one. Do not document step 7 as a
security control.**

### 8.5 Provisioning and lifecycle

**Vec's boundary case: first startup has no database file, so there is no inode to
bind.** Provisioning is therefore two-phase and operator-driven:

- **Phase A — `yugo dedup provision`.** Its own command precisely so it cannot
  happen implicitly during startup. Arguments:

  | Argument | Meaning |
  |---|---|
  | `--store <path>` | The database. Required. |
  | `--record <path>` | Where to write the record. Required. |
  | `--port typescript\|python` | **Required, never inferred.** It is written into `port` and decides which schema is created. Making the operator state it keeps a Python operator from provisioning a TypeScript-shaped store. |
  | `--record-only` | Do not create or modify the database; attest the one already there. Used by §8.6 step 5 after a migration, and it is the ONLY mode that touches an existing store. |
  | `--attested-by <name>` | Written to `attested_by`. Required. |

  Without `--record-only` it creates the store with the **full supported schema for
  `--port`** and no claims, runs the §8.3.3 inspection and the §3 accessor
  enumeration, resolves the device binding, then writes the record.

  **`--record-only` MUST reject a partial schema** (Ohm). If the store it is asked
  to attest does not match the port's compiled-in supported schema exactly, it
  refuses and writes nothing. Otherwise it would mint a record blessing a
  half-migrated store, and step 10's three-way check would then be comparing two
  agreeing wrong answers against the one right one.
- **Phase B — run.** Startup validates §8.4 and never writes the record.

| Situation | Disposition |
|---|---|
| No store file, no record | Refuse. Error names the provisioning command. |
| No store file, record present | Refuse — a record cannot bind a nonexistent inode. Re-provision. |
| **Store restored from backup, or recreated** | **Re-attestation REQUIRED, operationally. Restoration is NOT reliably detectable.** An in-place restore can preserve the inode and every binding, and then passes every check in §8.4. The first draft promised automatic detection here; it cannot deliver it. Codex found this independently. |
| Container recreated, same bind-mounted store | Bindings unchanged → passes. |
| **Reboot, `binding: "uuid"`** | **No manual re-attestation while the bindings still match.** Takeover eligibility follows successful startup validation like any other claim. |
| **Reboot, `binding: "devno"`** | Device numbers may have changed → **re-attestation required.** |
| Store moved to another filesystem | Device mismatch → refuse. Re-provision. |
| Accessor set changed | Record is stale — the inventory is part of the evidence. Re-provision. |
| **Release 2 column migration** | See §8.6. |

**`:memory:` keeps its narrowly scoped exception (§2):** no record is required —
no file, no filesystem, no inode. The §8.1 clock check and the boot-identity check
**still apply**, and a fresh claim still requires complete new-protocol metadata.

### 8.6 Migrating an existing store to the Release 2 schema

**Blocker 3 (Ohm): unspecified, and a six-column store cannot match an
eight-column fingerprint.** So every existing store fails step 10 the moment
Release 2 ships. That is correct behaviour and a complete outage if nobody wrote
down the path through it.

Ordered, and it is the §3 cutover with two steps added:

1. **Stop every accessor of this file** (§3), and prevent restart. For the
   TypeScript stores that means no Claude Code session for that user may start
   until step 6 — the launch path is a user action, not a service.
2. **Confirm stopped**, by the §3 method, not by assumption.
3. **Back up the store — including committed WAL state.** **Blocker 4 (Ohm):
   copying the main database file alone loses transactions that are committed but
   not yet checkpointed, and WAL mode means that is the normal steady state.** Use
   SQLite's [backup API](https://sqlite.org/backup.html), or checkpoint and close
   the database and **verify the checkpoint succeeded** before copying. A blind
   `cp` of the `.sqlite` file is not a backup of a WAL database. Copying the
   `-wal` and `-shm` files alongside it is not a substitute — it is a copy of a
   torn state unless the database is closed.
4. **Migrate in ONE transaction:** `BEGIN`, every
   `ALTER TABLE envelope_dedup_v2 ADD COLUMN`, `COMMIT`. Each column is `DEFAULT`ed
   so existing rows become well-formed **legacy** rows — empty `lease_boot_id`,
   which §2 defines as the supported legacy discriminator. **No row is deleted,
   rewritten or re-owned.**

   SQLite has transactional DDL, so this makes the schema change atomic and
   removes the half-migrated state the first draft had to write recovery prose
   for. Ohm's preference, and it is strictly better than sequencing the `ALTER`s.
5. **Re-attest.** The fingerprint has changed and the inode has not, so the
   operator re-runs provisioning in record-only mode against the migrated store.
6. **Start Release 2**, which validates §8.4 against the new record.

**Partial failure.** With step 4 in one transaction an interruption rolls back to
the original six-column schema, which then **refuses at step 10** against the
port's constant — a clean, retryable state rather than a half-migrated one. The
operator re-runs the migration.

**Crash after `COMMIT` but before attestation** (Ohm, non-blocking) leaves a
correctly migrated eight-column store with a stale six-column record. Recovery is
**record-only re-attestation — do NOT rerun the `ALTER`s**, which would fail
against columns that already exist and might tempt someone into a destructive
"clean slate" instead. Step 10's three-way check catches this state: live schema
and the port's constant agree, the record disagrees.

Should a store nonetheless be found matching neither the old nor the supported
schema, it **refuses**, and recovery is operator-driven: complete the migration
and re-attest, or restore the step-3 backup. **Never auto-repair a schema
mismatch** — that is the "accept current values" path §8.2 forbids, wearing a
different hat, and it is how this would get added later by someone fixing an
outage at 3am.

**Rollback after migration** is §3's rollback: structurally compatible, silently
without the safety property. The migrated store still works under Release 1
because the added columns are `DEFAULT`ed. Say so, and never call it a safe
rollback unqualified.

### 8.7 Tests for the startup interface

**Ohm: the existing lease tests do not cover these gates.** Added to §6, same
standard — delete the guard, watch the named test go red.

17. Non-zero `monotonic` offset refuses consumption.
18. Absent `timens_offsets` refuses; an unparseable line refuses and is **not**
    treated as zero.
19. Missing record refuses; malformed refuses; unknown `record_version` refuses;
    an unknown top-level field refuses.
20. `inode` round-trips a value above 2^53 without precision loss, and a
    one-off inode refuses.
21. `port` mismatch refuses — a Python consumer against a `"typescript"` record.
22. Store absent refuses and never creates the file, proving
    `SQLITE_OPEN_CREATE` is genuinely unset.
23. Inode replaced between the step-5 `stat()` and the step-7 re-`stat()` →
    refuses and closes the handle.
24. `storage_evidence.backing` of `network` or `unknown` refuses.
25. A six-column store against an eight-column fingerprint refuses at step 10 —
    the pre-migration state of every existing deployment.
26. A migration interrupted between two `ALTER`s refuses, and **no row is lost**.
27. `:memory:` runs with no record, and still refuses on a non-zero clock offset.
28. **Binding-mode failures**, one test each:
    - a `"devno"` record whose `attested_boot_id` differs from the current boot ID
      refuses, **even when `major:minor` is unchanged** — the case that made the
      reboot rule unenforced;
    - a `"uuid"` record whose UUID cannot be resolved refuses and does **not**
      fall back to comparing device numbers;
    - a `device` object mixing fields from both variants refuses;
    - `major:minor` supplied as the string `"8:1"` refuses.
29. **Device resolution is order-independent by construction — but an overmount
    that actually changes the resolved object must REFUSE.** Two halves, and
    Ohm's clarification is that conflating them would be a defect:

    a. **Representation changes nothing.** Reordering `/proc/self/mountinfo`
       entries does not change the verdict, because resolution reads `st_dev` and
       never parses mountinfo on the startup path. Fixture: the same topology,
       entries reordered.
    b. **A real change refuses.** An overmount that makes the configured path
       resolve to a different device or inode **fails step 4 or step 5 against the
       existing record**. Fixture: mount something over the store's directory so
       the path now resolves elsewhere; startup must refuse, not adapt.

    The first half says the verdict does not depend on how the kernel happens to
    print its mount table. The second says the verdict absolutely does depend on
    what the path resolves to. A test suite asserting only (a) could be satisfied
    by code that ignores the environment entirely.

    **This replaces the v3.3 test, which asserted that the last matching mountinfo
    entry governs — an invalid rule that would have locked the bug in rather than
    catching it.**
30. **Three-way schema check:** a six-column store with a matching six-column
    record still **refuses**, because the port's constant expects eight. This is
    the test that would have caught the hole in §8 v2.
31. `--record-only` against a half-migrated store refuses and writes no record.
32. **WAL-aware backup:** a store with committed-but-uncheckpointed transactions,
    backed up per §8.6 step 3, restores with those rows present — and a
    main-file-only copy is shown to lose them.

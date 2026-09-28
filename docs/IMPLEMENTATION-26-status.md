# Release 2 checkpoint — 2026-09-28, unfinished / not deployable

- Main through feca116 is integrated; `bun x tsc --noEmit` passes.
- TypeScript source suites (`bun test`): 245 passed, 2 skipped, 0 failed.
  Behavioral suites inject a test clock. The real-clock test is explicitly
  host-only (`YUGO_QUALIFIED_CLOCK_TEST=1`, set by the host probe script).
  The startup clock probe is the exception: `18a` makes `readMonotonicMs`
  throw and asserts no `Database` is constructed, so the ordering the
  `:170` comment claims is now enforced in both ports.
- Python full test directory (`pytest test/ -q -rs`, with and without
  `FLEET_BUS_ENABLED=0`): 946 passed, 0 skipped, 1 xfailed, 51 subtests
  passed. This workstation run, unprivileged and with every optional
  dependency present, skipped nothing. Environments missing integration
  dependencies do skip; a skip is never an assertion that the skipped
  coverage passed.
- Both Release-1 ports passed the seven-column regression, failed after
  actually removing their named INSERT lists, then passed after restoration.
  Current TS writer's nine-column test also went red on mutation and green
  on restoration. See EVIDENCE-35-wide-insert.md.
- Qualified host actual-clock gate previously passed; see
  EVIDENCE-26-qualified-host-clock.md. ABI C comment now names host-only
  qualification, not a nonexistent CI image.
- Fixed startup refusal tests accidentally going through auto-provisioning
  wrappers. Removed resulting untracked whitespace-named SQLite artifacts
  and sidecars; none staged.

Remaining: complete adapter-error and remaining conformance coverage, run
skipped broker/permission integration coverage in suitable environments,
CI/documentation reconciliation and review. The Release-1 named-INSERT
mutation gate and the native-clock runner regression are automated as of
`9473108` and `5fd5bfd`; the current TS writer's nine-column mutation is
still a manual proof.
No release PR opened; no production store migrated. Clock guard unchanged.

---

## Historical checkpoint (superseded)

# Release 2 implementation checkpoint — BLOCKED, not deployable

2026-09-26. Implementation branch rebased on main at `70068a2`.
The authoritative spec is unchanged by this note.

## Runtime premise disproved

SPEC-26 §1 names `process.hrtime.bigint()` and asserts it is not process-relative.
On **Bun 1.3.0**, the runtime pinned in repository CI, this assertion is false in
the measured environment. Same boot ID, zero monotonic time-namespace offsets:

- An established Bun process measured 1,222,953,938 ns before starting a child.
- Its child measured 15,904,165 ns.
- The parent measured 1,249,433,603 ns after the child exited.
- Python `time.monotonic_ns()` measured approximately 3,361,441,636,923,919 ns.
- Node's named API measured approximately the same host monotonic value as Python.

The cross-port test deliberately uses the actual APIs, not `/proc/uptime` and
not mocked values. It fails, as it must.

This is not merely cross-port interoperability (the ports must not share files).
The same-port executable reproduction in
`conformance/lease-clock-bun-repro.ts` shows an older Bun process taking a
younger Bun process's 200 ms lease **10 ms after acquisition while the original
owner is still alive**. No wall-clock step is needed.

Run from the repository root with Bun 1.3.0:

```sh
bun conformance/lease-clock-bun-repro.ts
PYTHON=python3 bun test src/clock-fencing.test.ts
```

The Python interpreter needs the adapter's dependencies (at least PyYAML for this
probe). Both processes use the same verified, test-provisioned SQLite file.
The reproduction creates only temporary test stores.

**Stopped for a ruling.** Do not replace the named clock, invent a conversion, or
weaken the actual-API assertion without an amended spec/runtime contract.

## Current checkpoint

- Both ports implement verified startup, complete-metadata monotonic claims and
  renewal, and completed-only TTL cleanup.
- Operator provisioning and atomic offline migration are implemented.
- Existing TS suite: 177 passed, 1 skipped.
- Python dedup + protocol + verification tests: 79 passed.
- New TS protocol/startup suite: 51 passed, 1 failed (actual-clock-domain test).
- The test-29 mountinfo assertion now matches proc paths, not tmpdir substrings.
- Release-1 six/eight-column compatibility fixtures and current-writer
  wider-schema INSERT regression tests are present for #35.

This is **unfinished WIP**, not release-ready code. Mutation verification, further
startup/provisioning review, CI wiring, documentation and full-suite validation
remain. No release PR has been opened and no production store has been migrated.

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

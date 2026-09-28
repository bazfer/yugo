# Release 2 qualified-host clock evidence

## Operator-reported run — 2026-09-28

Deet supplied this result over the fleet bus (message
`cbcb404a-2350-4fb5-a5c3-8165c6a307fe`). Vec did not execute this host run.

Revision: `a354f53ae3f54e59c09b49cdfcf11fe99985784d`.

Command, from the checkout root:

```sh
BUN=/home/deet/.bun/bin/bun PYTHON=python3 \
  bash conformance/run-qualified-clock.sh
```

Reported result: exit **0**, **1 pass**, **51 filtered out**, **0 fail**,
**9 expect() calls**, and final output:

```text
PASS: native ABI and actual-clock cross-port bracketing/live/expired checks
```

Reported environment: norstar (deet-01), unprivileged user deet, Linux x86_64,
Ubuntu GLIBC 2.39-0ubuntu8.9, Bun 1.3.12 at
`/home/deet/.bun/bin/bun`. Deet attests that this is the same executable used
by the live TypeScript plugin server.

The script reported self mount namespace `mnt:[4026531841]`; its unprivileged
PID 1 namespace read was unavailable. Deet separately read PID 1's namespace
with privilege and reported the same value. The qualification script itself
was not run with sudo. These are operator placement attestations, not
portable namespace-identity checks enforced by the adapter.

## What this establishes

- The host compiler accepted the C **compile-time** assertions against its
  headers: CLOCK_MONOTONIC is 1, time_t is 64-bit, and timespec has the asserted
  size, alignment and field offsets. The executable printed glibc 2.39.
- The selected test used the actual FFI clock and Python monotonic clock,
  checking cross-process bracketing and agreement on live/expired leases.
  Each port had its own in-memory store.

The C program does **not** dynamically discover the numeric clock identifier:
its output summarizes assertions compiled against the host headers. The
subsequent actual-clock test supplies behavioral evidence.

## Limits / remaining gates

This is one selected test, not a full conformance or release qualification
claim. No production database was migrated by this run.

Since this evidence was written, two gates landed in CI (`9473108`,
`5fd5bfd`). `native-clock-regression` runs the ABI assertion and the
actual-clock cross-process test on an `ubuntu-24.04` runner — runner
regression evidence, NOT deployment-host qualification, which still requires
the host probe recorded above. `release1-wide-insert` automates issue #35's
seven-column INSERT mutation proof through
`conformance/check-release1-mutations.py`.

Still outstanding: remaining conformance coverage, Release-2 mutation coverage
beyond the named-INSERT and startup-clock proofs, and review.

## Operator-reported rerun — 2026-09-28

Deet reported another successful run in fleet-bus message
`c3863a9f-e3ef-42f4-ae9f-c7309a3d9354`, at revision
`291dfc87b63d4c3fbddb057b40f856c73b33418b`, using the same command and
qualified-host environment described above.

Result: exit 0, 1 pass, 51 filtered out, 0 fail, 9 assertions, final PASS line.
This rerun includes the script's explicit `YUGO_QUALIFIED_CLOCK_TEST=1` setting.
Deet separately checked that omitting the variable visibly skips the test
(0 pass, 1 skip, 0 fail). Vec did not independently execute this host run.

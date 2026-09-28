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
claim. Integration/typecheck repairs, remaining conformance coverage, issue
#35's actual seven-column INSERT mutation proof, full-suite validation and
review remain outstanding. No production database was migrated by this run.

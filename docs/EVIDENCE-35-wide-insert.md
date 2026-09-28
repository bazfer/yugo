# Issue #35: actual Release-1 seven-column INSERT mutation

2026-09-28. The runnable regression is
`conformance/release1-wide-insert.py`. It creates the six-column table, executes
`ALTER TABLE ... ADD COLUMN future`, then calls the **real Python Release-1
DurableEnvelopeDedupStore.claim**, loaded from a separate checkout at
`feca116`. It does not execute a copied INSERT as a substitute for claim.

The Release-2 writer has eight columns and deliberately refuses a seven-column
startup schema; its existing current-writer wider-table test is a separate
nine-column regression. The seven-column historical proof must therefore use
the Release-1 implementation.

## Executed, not inferred

Using a detached worktree at `feca116`:

```sh
PYTHONPATH=/tmp/vec-yugo-r1-proof/yugo /tmp/vec-yugo-venv/bin/python   conformance/release1-wide-insert.py
```

1. Original production implementation: exit 0,
   `PASS: Release-1 claim accepts a seven-column table`.
2. Changed the INSERT in that worktree's `yugo/fleet_bus.py` from
   `INSERT OR IGNORE INTO envelope_dedup_v2 (envelope_id,first_seen_s,req_id,state,lease_owner,lease_until_s) VALUES`
   to `INSERT OR IGNORE INTO envelope_dedup_v2 VALUES`.
3. Same command: exit 1, from the real `claim` implementation:
   `sqlite3.OperationalError: table envelope_dedup_v2 has 7 columns but 6 values were supplied`.
4. Restored `yugo/fleet_bus.py`; same command passed again (exit 0).
5. Worktree status was clean after restoration. The main working tree was also
   inspected; unrelated blank-path fixture SQLite artifacts were removed, not
   staged. Startup refusal tests now bypass auto-provisioning fixtures.

The TypeScript counterpart, `conformance/release1-wide-insert.ts`, was also
executed against the same Release-1 worktree:

```sh
RELEASE1_CHECKOUT=/tmp/vec-yugo-r1-proof bun conformance/release1-wide-insert.ts
```

The worktree temporarily used a node_modules symlink to the implementation
checkout. Original named INSERT: pass (exit 0). Removed the six-column list
from the actual TypeScript writer: exit 1 with
`SQLiteError: table envelope_dedup_v2 has 7 columns but 6 values were supplied`.
Restored source: pass (exit 0). Removed the dependency symlink; worktree clean.

The **current Release-2 TypeScript writer** was also mutated by removing its
eight-column INSERT list. Its existing wider-table regression failed with
`table envelope_dedup_v2 has 9 columns but 8 values were supplied`; restoration
passed (1 pass, 0 fail). Source was restored and git status inspected.

The historical proofs are now automated CI gates. The `release1-wide-insert`
job (`.github/workflows/ci.yml`, wired in `9473108` and `5fd5bfd`) runs both
ports' seven-column regressions against a pinned `feca116` checkout, then runs
`conformance/check-release1-mutations.py`, which reverts each port's real named
INSERT, requires the exact
`table envelope_dedup_v2 has 7 columns but 6 values were supplied` failure, and
restores the sources. That script was executed end-to-end before it landed.

Still NOT automated: the current Release-2 TypeScript writer's nine-column
mutation. Its wider-table regression runs in CI through `bun test src`, but no
job mutates that writer — the red half above remains a manual proof. Remaining
Release-2 mutation coverage is still pending.

## Independent operator reproduction — 2026-09-28

In fleet-bus message `c3863a9f-e3ef-42f4-ae9f-c7309a3d9354`, Deet reported
independently reproducing the Python historical proof with a separate detached
worktree at feca116 and a separate venv: original passed (exit 0), removing the
real writer's column list failed from claim() with the exact seven-column/six-
value SQLite error (exit 1), and restoration passed (exit 0), leaving a clean
worktree. This is operator-reported corroboration, not another Vec-local run.

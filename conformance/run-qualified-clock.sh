#!/usr/bin/env bash
# Run on the qualified HOST, not in a container. No production stores touched.
set -euo pipefail
cd "$(dirname "$0")/.."
BUN="${BUN:-/home/deet/.bun/bin/bun}"
PYTHON="${PYTHON:-python3}"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

printf 'revision: '; git rev-parse HEAD
printf 'runtime: '; "$BUN" --version
uname -sm
getconf GNU_LIBC_VERSION
printf 'self mount namespace: '; readlink /proc/self/ns/mnt
# Diagnostic only: namespace inode numbers are not portable host identities.
# Host placement is operator-attested; do not elevate the qualification run.
if init_ns="$(readlink /proc/1/ns/mnt 2>/dev/null)"; then
  printf 'init mount namespace: %s\n' "$init_ns"
else
  printf 'init mount namespace: unreadable unprivileged; diagnostic skipped (host placement requires operator attestation)\n'
fi
"${CC:-cc}" -std=c11 -D_POSIX_C_SOURCE=200809L conformance/clock-abi.c -o "$scratch/clock-abi"
"$scratch/clock-abi"
"$PYTHON" -c 'import yaml'
"$BUN" install --frozen-lockfile
# No clock spy is installed by this selected test. Python uses its real clock;
# each port owns a separate in-memory database.
YUGO_QUALIFIED_CLOCK_TEST=1 PYTHON="$PYTHON" "$BUN" test src/clock-fencing.test.ts \
  --test-name-pattern '^13 and 15 cross-port expiry agrees using actual named clock APIs$'
# SPEC-26 §6 items 15a and 15b. Both spawn their own Bun processes against their
# own temporary stores; no clock spy and no production store is involved.
YUGO_QUALIFIED_CLOCK_TEST=1 PYTHON="$PYTHON" "$BUN" test src/clock-fencing-two-process.test.ts
YUGO_QUALIFIED_CLOCK_TEST=1 "$BUN" test src/clock-fault-injection.test.ts
printf 'PASS: native ABI and actual-clock cross-port bracketing/live/expired checks,\n'
printf '      two-process lease regression and post-startup clock-failure matrix\n'

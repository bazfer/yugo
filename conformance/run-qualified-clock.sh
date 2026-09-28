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
printf 'init mount namespace: '; readlink /proc/1/ns/mnt
"${CC:-cc}" -std=c11 -D_POSIX_C_SOURCE=200809L conformance/clock-abi.c -o "$scratch/clock-abi"
"$scratch/clock-abi"
"$PYTHON" -c 'import yaml'
"$BUN" install --frozen-lockfile
# No clock spy is installed by this selected test. Python uses its real clock;
# each port owns a separate in-memory database.
PYTHON="$PYTHON" "$BUN" test src/clock-fencing.test.ts \
  --test-name-pattern '^13 and 15 cross-port expiry agrees using actual named clock APIs$'
printf 'PASS: native ABI and actual-clock cross-port bracketing/live/expired checks\n'

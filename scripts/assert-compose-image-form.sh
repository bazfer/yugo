#!/usr/bin/env bash
# Assert that each given compose.yml uses image: form, not build:.
#
# A compose file with build: context: /path/to/host/checkout will silently
# reproduce whatever stale commit that checkout is at. This is FINDING 2:
# /home/luna/repos/yugo2 was at 3ee65d1b and 97 commits behind main when
# docker compose up -d --build was last run, and it reported success.
#
# Hard-fails on any file that is missing, unreadable, or contains a build:
# directive.

set -euo pipefail

fail=0
for file in "$@"; do
  if [[ ! -e "$file" ]]; then
    echo "ERROR: ${file}: not found" >&2
    fail=1
    continue
  fi
  if [[ ! -r "$file" ]]; then
    echo "ERROR: ${file}: permission denied" >&2
    fail=1
    continue
  fi
  if grep -q '^[[:space:]]*build:' "$file"; then
    echo "ERROR: ${file} contains a build: directive — it will build from a host checkout." >&2
    echo "       That checkout may be stale. Use image: form and build from a fresh tarball." >&2
    fail=1
  fi
done

[[ $fail -eq 0 ]] || exit 1

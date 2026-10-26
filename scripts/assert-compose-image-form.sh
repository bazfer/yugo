#!/usr/bin/env bash
# Assert that each given compose.yml uses image: form, not build:.
#
# A compose file with build: context: /path/to/host/checkout will silently
# reproduce whatever stale commit that checkout is at. This is FINDING 2:
# /home/luna/repos/yugo2 was at 3ee65d1b and 97 commits behind main when
# docker compose up -d --build was last run, and it reported success.
#
# Hard-fails on any file that is missing, unreadable, or contains a build:
# directive (checked via PyYAML structural parse — flow-map YAML
# {build: {context: ...}} bypasses a grep of '^[[:space:]]*build:').

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
  result="$(python3 - "$file" << 'PYEOF'
import sys, yaml
path = sys.argv[1]
try:
    with open(path) as f:
        doc = yaml.safe_load(f)
except Exception as e:
    print("ERROR: " + str(e))
    sys.exit(1)
services = (doc or {}).get("services") or {}
for svc_name, svc in services.items():
    if not isinstance(svc, dict):
        continue
    if "build" in svc:
        print("ERROR: service '{}' has a build: directive — it will build from a host checkout.".format(svc_name))
        sys.exit(1)
PYEOF
)" && py_exit=0 || py_exit=$?
  if [[ $py_exit -ne 0 ]]; then
    echo "ERROR: ${file}: ${result}" >&2
    echo "       Use image: form and build from a fresh tarball." >&2
    fail=1
  fi
done

[[ $fail -eq 0 ]] || exit 1

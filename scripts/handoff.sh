#!/usr/bin/env bash
# Latest-head handoff for bazfer/yugo.
#
# After every deploy (success or failure, if: always()), checks if HEAD of
# main has moved past the run's commit. If so, dispatches a fresh
# workflow_dispatch run so no commit is left without a deploy attempt.
#
# Required env:
#   EXPECTED_SHA     the SHA this run deployed (github.sha)
#   GH_TOKEN         token for fetching HEAD — github.token in the pipeline,
#                    PAT with contents:read for manual or cron use
#   DISPATCH_TOKEN   automatic job token for dispatch; pass ${{ github.token }}
#                    explicitly at the call site (actions:write required)

set -euo pipefail

: "${GH_TOKEN:?GH_TOKEN must be set — pipeline uses github.token}"
: "${EXPECTED_SHA:?EXPECTED_SHA must be set}"

REPO="${REPO:-bazfer/yugo}"
WORKFLOW_ID="${WORKFLOW_ID:-deploy.yml}"

HEAD="$(curl -fsSL \
  -H "Authorization: Bearer ${GH_TOKEN}" \
  "https://api.github.com/repos/${REPO}/commits/main" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["sha"])')"

if [[ "$HEAD" == "$EXPECTED_SHA" ]]; then
  echo "==> No handoff needed: HEAD is still ${HEAD:0:12}"
  exit 0
fi

echo "==> HEAD moved to ${HEAD:0:12} during deploy of ${EXPECTED_SHA:0:12}" >&2
echo "==> Dispatching handoff run for HEAD..." >&2

: "${DISPATCH_TOKEN:?DISPATCH_TOKEN must be set — pass \${{ github.token }} (actions:write)}"

curl -fsSL -X POST \
  -H "Authorization: Bearer ${DISPATCH_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW_ID}/dispatches" \
  -d '{"ref": "main"}'

echo "==> Handoff dispatched for ${HEAD:0:12}"

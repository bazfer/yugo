#!/usr/bin/env bash
# Assert every yugo container is running at the expected SHA.
# For chis: also assert yugo2-src/PINNED.md and git HEAD agree with that SHA.
#
# Exit 1 if any container is stopped, at wrong image, wrong label,
# or (chis) has a mismatched source snapshot. The gate is intentionally
# sharp: a mismatch is never a warning.
#
# Required env:
#   EXPECTED_SHA   the SHA every container must be at
#   BOTS           space-separated list (e.g. "yugo-coordinator chis ion")
#                  Default: "yugo-coordinator chis ion"
#                  Note: coordinator's container name is yugo-coordinator,
#                  not coordinator — docker inspect uses the container name.
#
# Optional env:
#   YUGO2_SRC_DIR  override yugo2-src path (testing)

set -euo pipefail

: "${EXPECTED_SHA:?EXPECTED_SHA must be set}"
BOTS="${BOTS:-yugo-coordinator chis ion}"
YUGO2_SRC_DIR="${YUGO2_SRC_DIR:-/home/luna/bots/chis/yugo2-src}"

fail=0

for BOT in $BOTS; do
  running="$(docker inspect "$BOT" --format '{{.State.Running}}' 2>/dev/null || echo "false")"
  if [[ "$running" != "true" ]]; then
    echo "DRIFT: ${BOT} is not running" >&2
    fail=1
    continue
  fi

  label="$(docker inspect "$BOT" \
    --format '{{index .Config.Labels "codex.commit"}}' 2>/dev/null || true)"
  if [[ "$label" != "$EXPECTED_SHA" ]]; then
    echo "DRIFT: ${BOT} codex.commit label is '${label:0:12}' (expected ${EXPECTED_SHA:0:12})" >&2
    fail=1
  fi

  # chis: source snapshot must match the deployed commit in two ways:
  #   PINNED.md — written atomically by deploy.sh
  #   git HEAD  — the actual checkout (PINNED.md alone is a write; git HEAD confirms it)
  if [[ "$BOT" == "chis" ]]; then
    pinned_sha="$(grep -oP '^SHA: \K\S+' "${YUGO2_SRC_DIR}/PINNED.md" 2>/dev/null || true)"
    if [[ "$pinned_sha" != "$EXPECTED_SHA" ]]; then
      echo "DRIFT: chis yugo2-src PINNED.md SHA is '${pinned_sha:0:12}' (expected ${EXPECTED_SHA:0:12})" >&2
      echo "       Image and source snapshot have diverged — chis is running code he cannot read." >&2
      fail=1
    fi

    head_sha="$(git \
      -c "safe.directory=${YUGO2_SRC_DIR}" \
      -C "${YUGO2_SRC_DIR}" \
      rev-parse HEAD 2>/dev/null || true)"
    if [[ "$head_sha" != "$EXPECTED_SHA" ]]; then
      echo "DRIFT: chis yugo2-src git HEAD is '${head_sha:0:12}' (expected ${EXPECTED_SHA:0:12})" >&2
      echo "       git HEAD and deployed SHA have diverged." >&2
      fail=1
    fi
  fi
done

if [[ $fail -ne 0 ]]; then
  echo "==> Drift gate FAILED — not all containers are at ${EXPECTED_SHA:0:12}" >&2
  exit 1
fi

echo "==> Drift gate passed: ${BOTS} all at ${EXPECTED_SHA:0:12}"

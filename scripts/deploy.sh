#!/usr/bin/env bash
# Deploy a yugo container (coordinator, chis, or ion).
#
# Usage: deploy.sh <bot-name> [commit-ref]
#
# Fetches a fresh tarball from GitHub at the exact commit SHA. FINDING 2:
# /home/luna/repos/yugo2 and /home/luna/yugo are stale host checkouts.
# Building from either silently reproduces the 97-commit gap. This script
# never touches a host checkout — it always fetches from the API.
#
# Requires GH_TOKEN. github.token in the Actions pipeline (contents:read on
# bazfer/yugo already granted by the workflow permissions block). For manual
# or cron use, set a PAT with contents:read on bazfer/yugo.
#
# Requires sudoers rules for compose restart and git/tee operations.
# See PR description for the exact rules required before running.
#
# Environment overrides (testing):
#   YUGO_COMPOSE_COORDINATOR / _CHIS / _ION   override compose file path
#   YUGO_SUDO_COORDINATOR / _CHIS / _ION       override sudo user ('none' = no sudo)
#   YUGO_YUGO2_SRC                             override yugo2-src path

set -euo pipefail

BOT="${1:?usage: deploy.sh <bot-name> [commit-ref]}"
REPO="bazfer/yugo"

: "${GH_TOKEN:?GH_TOKEN must be set — pipeline uses github.token; for manual use set a PAT with contents:read on bazfer/yugo}"

# Compose files run from the repo checkout (-f <path>), not from host directories.
# All env_file and volume paths in the compose files are absolute host paths.
case "$BOT" in
  coordinator)
    IMAGE_NAME="yugo-coordinator"
    COMPOSE_FILE="${YUGO_COMPOSE_COORDINATOR:-./bots/coordinator/compose.yml}"
    # Coordinator state (/var/lib/yugo-coordinator) is root:root 0700.
    # Must run compose as root so the docker daemon can access it.
    SUDO_USER="${YUGO_SUDO_COORDINATOR:-root}"
    ;;
  chis)
    IMAGE_NAME="chis-bot"
    COMPOSE_FILE="${YUGO_COMPOSE_CHIS:-./bots/chis/compose.yml}"
    # chis .env is owned by luna. Compose client must run as luna to read it.
    SUDO_USER="${YUGO_SUDO_CHIS:-luna}"
    YUGO2_SRC_DIR="${YUGO_YUGO2_SRC:-/home/luna/bots/chis/yugo2-src}"
    ;;
  ion)
    IMAGE_NAME="ion-bot"
    COMPOSE_FILE="${YUGO_COMPOSE_ION:-./bots/ion/compose.yml}"
    # /home/deet/bots/ion is drwx------ deet deet. Must run as deet.
    SUDO_USER="${YUGO_SUDO_ION:-deet}"
    ;;
  *)
    echo "ERROR: unknown bot '${BOT}' — expected: coordinator, chis, or ion" >&2
    exit 1
    ;;
esac

# ── First-cutover guard (purely local — runs before any network I/O) ──────────
# Compose cannot adopt a running container it did not start. If the container
# exists but carries no compose project label, it was started outside compose.
# The operator must stop and remove the old container manually once.

PREV_IMAGE_ID="$(docker inspect "$BOT" --format '{{.Image}}' 2>/dev/null || true)"
PREV_COMMIT="$(docker inspect "$BOT" \
  --format '{{index .Config.Labels "codex.commit"}}' 2>/dev/null || true)"
COMPOSE_PROJECT="$(docker inspect "$BOT" \
  --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)"

if [[ -n "$PREV_IMAGE_ID" && -z "$COMPOSE_PROJECT" ]]; then
  echo "ERROR: ${BOT} is running but was not started by docker compose." >&2
  echo "       Outgoing image: ${PREV_IMAGE_ID:0:12} (${PREV_COMMIT:-unlabeled})" >&2
  echo "       Before the first compose-managed deploy, record that image ref" >&2
  echo "       as your rollback and then run:" >&2
  echo "         docker stop ${BOT} && docker rm ${BOT}" >&2
  echo "       Then re-run the workflow. This is a one-time step per container." >&2
  exit 1
fi

# ── run_compose <image-tag> [compose args...] ─────────────────────────────────
# Runs docker compose -f $COMPOSE_FILE with CODEX_IMAGE_TAG set.
# Sudo model: 'none' = no sudo (test override), 'root' = sudo without -u
# (coordinator), anything else = sudo -u <user>.

run_compose() {
  local image_tag="$1"; shift
  if [[ "${SUDO_USER}" == "none" ]]; then
    YUGO_IMAGE_TAG="$image_tag" docker compose -f "$COMPOSE_FILE" "$@"
  elif [[ "${SUDO_USER}" == "root" ]]; then
    sudo env YUGO_IMAGE_TAG="$image_tag" docker compose -f "$COMPOSE_FILE" "$@"
  else
    sudo -u "$SUDO_USER" env YUGO_IMAGE_TAG="$image_tag" docker compose -f "$COMPOSE_FILE" "$@"
  fi
}

# ── Resolve commit ────────────────────────────────────────────────────────────

resolve_to_sha() {
  curl -fsSL \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    "https://api.github.com/repos/${REPO}/commits/${1}" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["sha"])'
}

echo "==> Resolving ${2:-main} ..."
COMMIT="$(resolve_to_sha "${2:-main}")"
echo "==> Commit: ${COMMIT}"

# ── Snapshot current state for rollback ──────────────────────────────────────

ROLLBACK_TAG="${IMAGE_NAME}:rollback"
WORK_DIR=""

if [[ -n "$PREV_IMAGE_ID" ]]; then
  docker tag "$PREV_IMAGE_ID" "$ROLLBACK_TAG"
  echo "==> Rollback snapshot saved: ${PREV_COMMIT:-unlabeled} (${PREV_IMAGE_ID:0:12})"
else
  echo "==> Warning: no running container found — rollback will not be available"
fi

# ── Rollback ──────────────────────────────────────────────────────────────────

rollback() {
  local exit_code="${1:-$?}"
  trap - ERR
  echo "" >&2
  echo "ERROR: deploy failed (exit ${exit_code}) — rolling back ${BOT}..." >&2
  if [[ -z "${PREV_IMAGE_ID:-}" ]]; then
    echo "ERROR: no rollback image available (bot was not running before deploy)" >&2
    exit 1
  fi
  local rollback_image_tag
  if [[ -n "${PREV_COMMIT:-}" ]]; then
    rollback_image_tag="$PREV_COMMIT"
    docker tag "$ROLLBACK_TAG" "${IMAGE_NAME}:${PREV_COMMIT}" || {
      echo "ERROR: rollback retag failed — image preserved as ${ROLLBACK_TAG}" >&2
      exit 1
    }
  else
    rollback_image_tag="rollback"
  fi
  echo "==> Restarting ${BOT} from rollback image..." >&2
  run_compose "$rollback_image_tag" up -d --no-build 2>&1 || {
    echo "ERROR: rollback compose up failed — image preserved as ${ROLLBACK_TAG}" >&2
    exit 1
  }
  echo "ROLLBACK COMPLETE: ${BOT} restored to ${PREV_COMMIT:-unlabeled}" >&2
  exit 1
}

die() {
  echo "ERROR: $1" >&2
  rollback 1
}

trap rollback ERR

# ── Fetch source (never from a host checkout) ─────────────────────────────────
# Always fetch a fresh tarball from GitHub. /home/luna/repos/yugo2 and
# /home/luna/yugo are stale host checkouts — building from either would
# silently reproduce the gap (FINDING 2).

WORK_DIR="$(mktemp -d)"
trap '[[ -n "$WORK_DIR" ]] && rm -rf "$WORK_DIR"' EXIT

echo "==> Fetching ${REPO}@${COMMIT:0:12}..."
curl -fsSL \
  -H "Authorization: Bearer ${GH_TOKEN}" \
  "https://api.github.com/repos/${REPO}/tarball/${COMMIT}" \
  | tar -xz --strip-components=1 -C "$WORK_DIR"

# ── Build ─────────────────────────────────────────────────────────────────────

IIDFILE="$(mktemp)"
echo "==> Building ${IMAGE_NAME}:${COMMIT:0:12}..."
DOCKER_BUILDKIT=1 docker build \
  --iidfile "$IIDFILE" \
  --label "codex.commit=${COMMIT}" \
  -t "${IMAGE_NAME}:${COMMIT}" \
  -t "${IMAGE_NAME}:latest" \
  -f "${WORK_DIR}/yugo/Dockerfile" \
  "$WORK_DIR"
BUILT_IMAGE_ID="$(cat "$IIDFILE")"
rm -f "$IIDFILE"

TAGGED_ID="$(docker inspect "${IMAGE_NAME}:${COMMIT}" --format '{{.Id}}')"
if [[ "$TAGGED_ID" != "$BUILT_IMAGE_ID" ]]; then
  die "image ID mismatch immediately after tagging"
fi

# ── chis: update yugo2-src snapshot ──────────────────────────────────────────
# The source snapshot must track the deployed commit so chis reads the code
# he runs (FINDING 3). Move both atomically before restart, or fail.
#
# Note: if yugo2-src's git remote still points at bazfer/yugo2 (old repo),
# the checkout will fail because $COMMIT is from bazfer/yugo. Update the
# remote first: sudo -u luna git -C <dir> remote set-url origin <new-url>
# This is a one-time migration step; see PR description.

if [[ "$BOT" == "chis" ]]; then
  echo "==> Updating yugo2-src to ${COMMIT:0:12}..."
  sudo -u "${SUDO_USER}" git \
    -c "safe.directory=${YUGO2_SRC_DIR}" \
    -C "${YUGO2_SRC_DIR}" fetch origin 2>&1
  sudo -u "${SUDO_USER}" git \
    -c "safe.directory=${YUGO2_SRC_DIR}" \
    -C "${YUGO2_SRC_DIR}" checkout "${COMMIT}" 2>&1
  printf 'SHA: %s\nDate: %s\nUpdated-by: deploy.sh\n' \
    "${COMMIT}" "$(date -u +%Y-%m-%d)" \
    | sudo -u "${SUDO_USER}" tee "${YUGO2_SRC_DIR}/PINNED.md" > /dev/null
  echo "==> yugo2-src pinned to ${COMMIT:0:12}"
fi

# ── Restart ───────────────────────────────────────────────────────────────────

echo "==> Starting ${BOT}..."
run_compose "$COMMIT" up -d --no-build

sleep 2

# ── Verify ────────────────────────────────────────────────────────────────────

running="$(docker inspect "$BOT" --format '{{.State.Running}}' 2>/dev/null || echo "false")"
if [[ "$running" != "true" ]]; then
  die "${BOT} is not running after deploy"
fi

container_image_id="$(docker inspect "$BOT" --format '{{.Image}}')"
if [[ "$container_image_id" != "$BUILT_IMAGE_ID" ]]; then
  die "container is running wrong image
       expected (built): ${BUILT_IMAGE_ID:0:12}
       got (running):    ${container_image_id:0:12}"
fi

deployed_commit="$(docker inspect "$BOT" \
  --format '{{index .Config.Labels "codex.commit"}}' 2>/dev/null || true)"
if [[ "$deployed_commit" != "$COMMIT" ]]; then
  die "codex.commit label mismatch
       expected: ${COMMIT:0:12}
       got:      ${deployed_commit:0:12}"
fi

# ── Cleanup ───────────────────────────────────────────────────────────────────

docker rmi "$ROLLBACK_TAG" 2>/dev/null || true
echo "==> ${BOT} running at ${COMMIT:0:12}"

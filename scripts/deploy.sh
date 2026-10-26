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
# Requires sudoers rules for compose restart and git/tee/cat operations.
# These grants are ABSENT as of 2026-10-26 (kat only has /usr/bin/apt-get).
# All three deploys fail-closed until they are added.
#
# WARNING: a sudoers entry is only as narrow as the most capable thing on it.
# "Capable" means: can it start another program? env, sh, bash, find, vi, git,
# awk, perl, python and docker can all launch arbitrary commands and must NEVER
# appear as unrestricted grants. Grant only exact binary paths with pinned args.
#
# /usr/bin/docker cannot be meaningfully narrowed — docker as root is
# root-equivalent by design (bind-mount of / reaches everything). The deploy
# genuinely needs compose, so this grant is intentionally broad. State it
# plainly so nobody later mistakes it for a scoped entry.
#
# /usr/bin/git cannot be safely argument-restricted in sudoers: checkout
# requires a variable SHA argument, and git runs arbitrary shell through
# -c alias.x='!sh' x on any unrestricted call. Use a wrapper script owned
# by luna (mode 0755) that validates the argument vector before delegating:
#
#   /home/luna/bots/chis/bin/yugo-git-chis:
#   #!/usr/bin/env bash
#   set -euo pipefail
#   SRC=/home/luna/bots/chis/yugo2-src
#   SAFE="-c safe.directory=${SRC} -C ${SRC}"
#   case "${1:-}" in
#     rev-parse) [[ $# -eq 2 && "$2" == "HEAD" ]] || exit 1
#                exec /usr/bin/git ${SAFE} rev-parse HEAD ;;
#     checkout)  [[ $# -eq 2 && "$2" =~ ^[0-9a-f]{7,40}$ ]] || exit 1
#                exec /usr/bin/git ${SAFE} checkout "$2" ;;
#     fetch)     [[ $# -eq 1 ]] || exit 1
#                exec /usr/bin/git ${SAFE} fetch origin ;;
#     *)         echo "denied: $*" >&2; exit 1 ;;
#   esac
#
# NOTE: deploy.sh currently calls /usr/bin/git directly via run_as_owner.
# Before Fernando adds the grants below, deploy.sh must be updated to call
# the wrapper instead — tracked as a separate code change. Adding the current
# git calls to sudoers without the wrapper would restore an unrestricted grant.
#
# Exact entries (paths confirmed on Norstar):
#
#   Defaults:kat env_keep += "YUGO_IMAGE_TAG"
#   kat ALL=(root) NOPASSWD: /usr/bin/docker
#   kat ALL=(luna) NOPASSWD: /usr/bin/docker, /home/luna/bots/chis/bin/yugo-git-chis, /usr/bin/tee /home/luna/bots/chis/yugo2-src/PINNED.md, /usr/bin/cat /home/luna/bots/chis/yugo2-src/PINNED.md
#   kat ALL=(deet) NOPASSWD: /usr/bin/docker
#
# Environment overrides (testing):
#   YUGO_COMPOSE_COORDINATOR / _CHIS / _ION   override compose file path
#   YUGO_SUDO_COORDINATOR / _CHIS / _ION       override sudo user ('none' = no sudo)
#   YUGO_YUGO2_SRC                             override yugo2-src path
#   YUGO_BUILD_IMAGE_ID                        skip tarball fetch and docker build;
#                                              use this value as BUILT_IMAGE_ID
#   YUGO_COMMIT_OVERRIDE                       skip resolve_to_sha; use this SHA directly

set -euo pipefail

BOT="${1:?usage: deploy.sh <bot-name> [commit-ref]}"
REPO="bazfer/yugo"

: "${GH_TOKEN:?GH_TOKEN must be set — pipeline uses github.token; for manual use set a PAT with contents:read on bazfer/yugo}"

# Compose files run from the repo checkout (-f <path>), not from host directories.
# All env_file and volume paths in the compose files are absolute host paths.
# CONTAINER_NAME is the actual docker container name — may differ from BOT.
case "$BOT" in
  coordinator)
    IMAGE_NAME="yugo-coordinator"
    CONTAINER_NAME="yugo-coordinator"
    COMPOSE_FILE="${YUGO_COMPOSE_COORDINATOR:-./bots/coordinator/compose.yml}"
    # Coordinator state (/var/lib/yugo-coordinator) is root:root 0700.
    # Must run compose as root so the docker daemon can access it.
    SUDO_USER="${YUGO_SUDO_COORDINATOR:-root}"
    ;;
  chis)
    IMAGE_NAME="chis-bot"
    CONTAINER_NAME="chis"
    COMPOSE_FILE="${YUGO_COMPOSE_CHIS:-./bots/chis/compose.yml}"
    # chis .env is owned by luna. Compose client must run as luna to read it.
    SUDO_USER="${YUGO_SUDO_CHIS:-luna}"
    YUGO2_SRC_DIR="${YUGO_YUGO2_SRC:-/home/luna/bots/chis/yugo2-src}"
    ;;
  ion)
    IMAGE_NAME="ion-bot"
    CONTAINER_NAME="ion"
    COMPOSE_FILE="${YUGO_COMPOSE_ION:-./bots/ion/compose.yml}"
    # /home/deet/bots/ion is drwx------ deet deet. Must run as deet.
    SUDO_USER="${YUGO_SUDO_ION:-deet}"
    ;;
  *)
    echo "ERROR: unknown bot '${BOT}' — expected: coordinator, chis, or ion" >&2
    exit 1
    ;;
esac

# ── run_compose <image-tag> [compose args...] ─────────────────────────────────
# Runs docker compose -f $COMPOSE_FILE with YUGO_IMAGE_TAG set.
# Sudo model: 'none' = no sudo (test override), 'root' = sudo without -u
# (coordinator), anything else = sudo -u <user>.

run_compose() {
  local image_tag="$1"; shift
  # Export YUGO_IMAGE_TAG so sudoers env_keep passes it through to docker compose.
  # Never use `env` in the sudo call — it grants unrestricted command execution.
  export YUGO_IMAGE_TAG="$image_tag"
  if [[ "${SUDO_USER}" == "none" ]]; then
    docker compose -f "$COMPOSE_FILE" "$@"
  elif [[ "${SUDO_USER}" == "root" ]]; then
    sudo docker compose -f "$COMPOSE_FILE" "$@"
  else
    sudo -u "$SUDO_USER" docker compose -f "$COMPOSE_FILE" "$@"
  fi
}

# run_as_owner: run a command as the bot owner, respecting SUDO_USER.
# 'none' = no sudo (test mode), 'root' = sudo, else sudo -u <user>.
run_as_owner() {
  if [[ "${SUDO_USER}" == "none" ]]; then
    "$@"
  elif [[ "${SUDO_USER}" == "root" ]]; then
    sudo "$@"
  else
    sudo -u "$SUDO_USER" "$@"
  fi
}

# ── First-cutover guard (purely local — runs before any network I/O) ──────────
# Compose cannot adopt a running container it did not start. If the container
# exists but carries no compose project label, it was started outside compose.
# The operator must stop and remove the old container manually once.

IMAGE_CAPTURED=0
PREV_IMAGE_ID=""
_docker_inspect_err="$(mktemp)"
if _img="$(docker inspect "$CONTAINER_NAME" --format '{{.Image}}' 2>"$_docker_inspect_err")"; then
  PREV_IMAGE_ID="$_img"
  IMAGE_CAPTURED=1
else
  _err="$(cat "$_docker_inspect_err")"
  # Pattern matched against docker's actual stderr — must be re-verified on Docker upgrade.
  # Docker 29.x writes: "error: no such object: <name>" (all lowercase).
  if ! echo "$_err" | grep -qi "no such object"; then
    echo "ERROR: docker inspect ${CONTAINER_NAME} --format {{.Image}} failed: ${_err}" >&2
    rm -f "$_docker_inspect_err"
    exit 1
  fi
  # Container does not exist — legitimate first deploy; IMAGE_CAPTURED stays 0.
fi
rm -f "$_docker_inspect_err"
PREV_COMMIT="$(docker inspect "$CONTAINER_NAME" \
  --format '{{index .Config.Labels "codex.commit"}}' 2>/dev/null || true)"
COMPOSE_PROJECT="$(docker inspect "$CONTAINER_NAME" \
  --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)"

if [[ "$IMAGE_CAPTURED" == "1" && -z "$COMPOSE_PROJECT" ]]; then
  echo "ERROR: ${CONTAINER_NAME} is running but was not started by docker compose." >&2
  echo "       Outgoing image: ${PREV_IMAGE_ID:0:12} (${PREV_COMMIT:-unlabeled})" >&2
  echo "       Before the first compose-managed deploy, record that image ref" >&2
  echo "       as your rollback and then run:" >&2
  echo "         docker stop ${CONTAINER_NAME} && docker rm ${CONTAINER_NAME}" >&2
  echo "       Then re-run the workflow. This is a one-time step per container." >&2
  exit 1
fi

# ── Resolve commit ────────────────────────────────────────────────────────────

resolve_to_sha() {
  curl -fsSL \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    "https://api.github.com/repos/${REPO}/commits/${1}" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["sha"])'
}

if [[ -n "${YUGO_COMMIT_OVERRIDE:-}" ]]; then
  COMMIT="${YUGO_COMMIT_OVERRIDE}"
else
  echo "==> Resolving ${2:-main} ..."
  COMMIT="$(resolve_to_sha "${2:-main}")"
fi
echo "==> Commit: ${COMMIT}"

# ── Idempotency check ─────────────────────────────────────────────────────────
# If the container is already running at the target SHA, skip the rebuild.
# This is informational: same-SHA redeployment is safe but unnecessary.

if [[ "${PREV_COMMIT:-}" == "$COMMIT" ]]; then
  echo "==> ${CONTAINER_NAME} already at ${COMMIT:0:12} — skipping rebuild"
  exit 0
fi

# ── Snapshot current state for rollback ──────────────────────────────────────

ROLLBACK_TAG="${IMAGE_NAME}:rollback"
WORK_DIR=""
# chis rollback state — populated before yugo2-src is modified
PREV_YUGO2_SHA=""
PREV_PINNED_MD=""
PINNED_CAPTURED=0

if [[ "$IMAGE_CAPTURED" == "1" ]]; then
  docker tag "$PREV_IMAGE_ID" "$ROLLBACK_TAG"
  echo "==> Rollback snapshot saved: ${PREV_COMMIT:-unlabeled} (${PREV_IMAGE_ID:0:12})"
else
  echo "==> Warning: no running container found — rollback will not be available"
fi

# ── Rollback ──────────────────────────────────────────────────────────────────

rollback() {
  set +e
  trap - ERR
  echo "" >&2
  echo "ERROR: deploy failed — rolling back ${BOT}..." >&2
  if [[ "$IMAGE_CAPTURED" != "1" ]]; then
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

  # Verify the container is actually running at the old image before declaring success.
  sleep 1
  local restored_running restored_image
  restored_running="$(docker inspect "${CONTAINER_NAME}" --format '{{.State.Running}}' 2>/dev/null || echo "false")"
  restored_image="$(docker inspect "${CONTAINER_NAME}" --format '{{.Image}}' 2>/dev/null || true)"
  if [[ "$restored_running" != "true" ]]; then
    echo "ERROR: rollback compose returned 0 but ${CONTAINER_NAME} is not running — manual intervention required" >&2
    exit 1
  fi
  if [[ "$restored_image" != "$PREV_IMAGE_ID" ]]; then
    echo "ERROR: rollback shows wrong image ${restored_image:0:12} (expected ${PREV_IMAGE_ID:0:12}) — manual intervention required" >&2
    exit 1
  fi

  # chis: restore yugo2-src to its pre-deploy state.
  # PREV_YUGO2_SHA is empty only when rollback fires before the snapshot section
  # (e.g. build failed) — the source was not mutated in that case, so skip.
  # If set, full restore with verification. Both halves are checked; a mismatch is manual.
  if [[ "$BOT" == "chis" ]]; then
  if [[ -z "${PREV_YUGO2_SHA:-}" ]]; then
    echo "==> yugo2-src not mutated before failure — no source restore needed" >&2
  else
    if ! run_as_owner git \
        -c "safe.directory=${YUGO2_SRC_DIR}" \
        -C "${YUGO2_SRC_DIR}" \
        checkout "${PREV_YUGO2_SHA}" 2>/dev/null >&2; then
      echo "ERROR: yugo2-src checkout restore to ${PREV_YUGO2_SHA:0:12} failed — manual intervention required" >&2
      exit 1
    fi
    if [[ "$PINNED_CAPTURED" == "1" ]]; then
      if ! printf '%s' "${PREV_PINNED_MD}" | run_as_owner tee "${YUGO2_SRC_DIR}/PINNED.md" > /dev/null 2>/dev/null; then
        echo "ERROR: PINNED.md restore failed — manual intervention required" >&2
        exit 1
      fi
    fi
    # Verify by reading back — a write returning 0 is not the same as the right value being there.
    local restored_head
    restored_head="$(run_as_owner git -c "safe.directory=${YUGO2_SRC_DIR}" -C "${YUGO2_SRC_DIR}" rev-parse HEAD 2>/dev/null || true)"
    if [[ "$restored_head" != "$PREV_YUGO2_SHA" ]]; then
      echo "ERROR: yugo2-src rollback failed — git HEAD is ${restored_head:0:12}, expected ${PREV_YUGO2_SHA:0:12} — manual intervention required" >&2
      exit 1
    fi
    if [[ "$PINNED_CAPTURED" == "1" ]]; then
      local restored_pinned
      restored_pinned="$(run_as_owner cat "${YUGO2_SRC_DIR}/PINNED.md" 2>/dev/null)" || {
        echo "ERROR: PINNED.md rollback failed — cannot read back restored content — manual intervention required" >&2
        exit 1
      }
      if [[ "$restored_pinned" != "$PREV_PINNED_MD" ]]; then
        echo "ERROR: PINNED.md rollback failed — content mismatch — manual intervention required" >&2
        exit 1
      fi
    fi
  fi
  fi

  echo "ROLLBACK COMPLETE: ${BOT} restored to ${PREV_COMMIT:-unlabeled}" >&2
  exit 1
}

die() {
  echo "ERROR: $1" >&2
  rollback
}

trap rollback ERR

# ── Fetch source OR use test-mode image ID ────────────────────────────────────
# In test mode (YUGO_BUILD_IMAGE_ID set), skip tarball fetch and docker build.
# In production, always fetch a fresh tarball from GitHub — never from a host
# checkout (/home/luna/repos/yugo2 was 97 commits stale; FINDING 2).

if [[ -n "${YUGO_BUILD_IMAGE_ID:-}" ]]; then
  BUILT_IMAGE_ID="${YUGO_BUILD_IMAGE_ID}"
  echo "==> (test mode) using supplied image ID ${BUILT_IMAGE_ID:0:12}"
else
  WORK_DIR="$(mktemp -d)"
  trap '[[ -n "$WORK_DIR" ]] && rm -rf "$WORK_DIR"' EXIT

  echo "==> Fetching ${REPO}@${COMMIT:0:12}..."
  curl -fsSL \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    "https://api.github.com/repos/${REPO}/tarball/${COMMIT}" \
    | tar -xz --strip-components=1 -C "$WORK_DIR" \
    || die "tarball fetch failed for ${COMMIT:0:12}"

  IIDFILE="$(mktemp)"
  echo "==> Building ${IMAGE_NAME}:${COMMIT:0:12}..."
  DOCKER_BUILDKIT=1 docker build \
    --iidfile "$IIDFILE" \
    --label "codex.commit=${COMMIT}" \
    -t "${IMAGE_NAME}:${COMMIT}" \
    -t "${IMAGE_NAME}:latest" \
    -f "${WORK_DIR}/yugo/Dockerfile" \
    "$WORK_DIR" \
    || die "docker build failed"
  BUILT_IMAGE_ID="$(cat "$IIDFILE")"
  rm -f "$IIDFILE"

  TAGGED_ID="$(docker inspect "${IMAGE_NAME}:${COMMIT}" --format '{{.Id}}')"
  if [[ "$TAGGED_ID" != "$BUILT_IMAGE_ID" ]]; then
    die "image ID mismatch immediately after tagging"
  fi
fi

# ── chis: capture prev state, then update yugo2-src snapshot ─────────────────
# The source snapshot must track the deployed commit (FINDING 3).
# Capture BEFORE modifying so rollback can restore.
#
# One-time prerequisite: yugo2-src must point at bazfer/yugo (not bazfer/yugo2):
#   sudo -u luna git -C /home/luna/bots/chis/yugo2-src remote set-url origin \
#     https://github.com/bazfer/yugo.git

if [[ "$BOT" == "chis" ]]; then
  PREV_YUGO2_SHA="$(run_as_owner git \
    -c "safe.directory=${YUGO2_SRC_DIR}" \
    -C "${YUGO2_SRC_DIR}" rev-parse HEAD 2>/dev/null)" || {
    echo "ERROR: yugo2-src: failed to read current HEAD — cannot deploy without rollback capability" >&2
    exit 1
  }
  [[ -n "$PREV_YUGO2_SHA" ]] || {
    echo "ERROR: yugo2-src: rev-parse returned empty — cannot deploy" >&2
    exit 1
  }
  PREV_PINNED_MD="$(run_as_owner cat "${YUGO2_SRC_DIR}/PINNED.md" 2>/dev/null)" && PINNED_CAPTURED=1 || {
    echo "ERROR: yugo2-src: failed to read PINNED.md — cannot deploy without rollback capability" >&2
    exit 1
  }

  echo "==> Updating yugo2-src to ${COMMIT:0:12}..."
  run_as_owner git \
    -c "safe.directory=${YUGO2_SRC_DIR}" \
    -C "${YUGO2_SRC_DIR}" fetch origin \
    || die "yugo2-src fetch failed (check remote: sudo -u luna git -C ${YUGO2_SRC_DIR} remote -v)"
  run_as_owner git \
    -c "safe.directory=${YUGO2_SRC_DIR}" \
    -C "${YUGO2_SRC_DIR}" checkout "${COMMIT}" \
    || die "yugo2-src checkout ${COMMIT:0:12} failed"
  printf 'SHA: %s\nDate: %s\nUpdated-by: deploy.sh\n' \
    "${COMMIT}" "$(date -u +%Y-%m-%d)" \
    | run_as_owner tee "${YUGO2_SRC_DIR}/PINNED.md" > /dev/null
  echo "==> yugo2-src pinned to ${COMMIT:0:12}"
fi

# ── Restart ───────────────────────────────────────────────────────────────────

echo "==> Starting ${CONTAINER_NAME}..."
run_compose "$COMMIT" up -d --no-build || die "compose up failed for ${BOT}"

sleep 2

# ── Verify ────────────────────────────────────────────────────────────────────

running="$(docker inspect "$CONTAINER_NAME" --format '{{.State.Running}}' 2>/dev/null || echo "false")"
if [[ "$running" != "true" ]]; then
  die "${CONTAINER_NAME} is not running after deploy"
fi

container_image_id="$(docker inspect "$CONTAINER_NAME" --format '{{.Image}}')"
if [[ "$container_image_id" != "$BUILT_IMAGE_ID" ]]; then
  die "container is running wrong image
       expected (built): ${BUILT_IMAGE_ID:0:12}
       got (running):    ${container_image_id:0:12}"
fi

deployed_commit="$(docker inspect "$CONTAINER_NAME" \
  --format '{{index .Config.Labels "codex.commit"}}' 2>/dev/null || true)"
if [[ "$deployed_commit" != "$COMMIT" ]]; then
  die "codex.commit label mismatch
       expected: ${COMMIT:0:12}
       got:      ${deployed_commit:0:12}"
fi

# ── Cleanup ───────────────────────────────────────────────────────────────────

docker rmi "$ROLLBACK_TAG" 2>/dev/null || true
echo "==> ${BOT} running at ${COMMIT:0:12}"

#!/usr/bin/env bash
# Deploy the fleet-bus tap, and REFUSE to report success unless it is verified.
#
# WHY THIS IS A SCRIPT. DEPLOY.md described this procedure in prose. Three reviews found
# five instructions in it that did not do what they said, including a deploy that was a
# no-op and a rollback that rolled nothing back. Prose cannot fail, so nothing caught
# them. This file executes, so a wrong step is a failed run.
#
# Four checks in prose are four checks nobody runs. These four refuse.
set -euo pipefail

SRC="${FLEET_BUS_SRC:-/home/luna/yugo}"
SUB="tools/fleet-bus"
COMPOSE="$SRC/$SUB/compose.yml"
NAME="${FLEET_BUS_CONTAINER:-fleet-bus-tap}"
OWNER="${FLEET_BUS_OWNER:-luna}"
MIRROR_CONFIRMED=0
ROLLBACK_REF=""
# Records which process the operator verified, so confirming check 4 does not require
# deploying again. Outside the checkout, and not a secret.
STATE="${FLEET_BUS_STATE:-/home/luna/fleet-bus/.tap-deploy-state}"

die() { printf '  FAIL: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }
# Run as the owning user -- but only escalate when we are not already them. Prefixing
# sudo unconditionally broke the NORMAL case: luna running it got
# "user luna is not allowed to execute ... as luna". Found on first real use.
as_owner() {
  if [ "$(id -un)" = "$OWNER" ]; then "$@"; else sudo -u "$OWNER" "$@"; fi
}

usage() {
  cat <<USAGE
usage: deploy.sh [--rollback <ref>] [--mirror-confirmed]

  --rollback <ref>     revert the SOURCE to <ref> and recreate. A retained container
                       preserves its configuration, not its revision, so rolling back
                       means moving the source.
  --mirror-confirmed   you have READ #fleet-bus and seen new traffic. Without this the
                       script exits non-zero: checks 1 to 3 cannot tell whether the tap
                       posts anything.

                       Passed ALONE, it confirms the deployment already made and deploys
                       nothing. It refuses if the container restarted since, because then
                       you verified a different process.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --rollback) ROLLBACK_REF="${2:-}"; [ -n "$ROLLBACK_REF" ] || die "--rollback needs a ref"; shift 2 ;;
    --mirror-confirmed) MIRROR_CONFIRMED=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

[ -f "$COMPOSE" ] || die "no compose file at $COMPOSE"

# --mirror-confirmed alone: close out the deployment already made. Re-running the whole
# deploy to record one manual check meant redeploying to verify a deployment, which also
# restarted the tap a second time and reset its status dedupe. Found on first real use.
if [ "$MIRROR_CONFIRMED" -eq 1 ] && [ -z "$ROLLBACK_REF" ]; then
  # The absence of the state file must REFUSE, not fall through. Guarding the branch on
  # [ -f "$STATE" ] meant a confirm with nothing pending skipped this block, ran a full
  # deployment, and then printed "verified" and exited 0 -- an unrequested deploy AND a
  # false check-4 pass, from a flag whose whole purpose is to record a manual check.
  # (Kat, PR 70.)
  [ -f "$STATE" ] || die "no pending deployment to confirm. This flag records a check; it does not deploy. Run deploy.sh first."
  RECORDED=$(cut -d" " -f1 < "$STATE")
  RECORDED_REF=$(cut -d" " -f2 < "$STATE")
  NOW_STARTED=$(as_owner docker inspect "$NAME" --format "{{.State.StartedAt}}" 2>/dev/null || true)
  [ -n "$NOW_STARTED" ] || die "no running container to confirm"
  if [ "$NOW_STARTED" != "$RECORDED" ]; then
    die "the container restarted since the deployment you are confirming ($NOW_STARTED vs $RECORDED). Deploy again."
  fi
  as_owner rm -f "$STATE"
  note "check 4: mirroring confirmed by the operator"
  note "deployment of ${RECORDED_REF} verified"
  exit 0
fi

# ---------------------------------------------------------------- source and deps
PULLED_AT_EPOCH=$(date -u +%s)
BEFORE=$(as_owner git -C "$SRC" rev-parse HEAD)

if [ -n "$ROLLBACK_REF" ]; then
  note "rolling the SOURCE back to $ROLLBACK_REF"
  as_owner git -C "$SRC" checkout --quiet "$ROLLBACK_REF"
else
  note "pulling"
  as_owner git -C "$SRC" pull --quiet
fi
AFTER=$(as_owner git -C "$SRC" rev-parse HEAD)
note "source: ${BEFORE:0:7} -> ${AFTER:0:7}"

# node_modules is gitignored, so a lockfile change does not arrive with the source. Both
# directions matter: a rollback can need OLDER packages than are installed.
if ! as_owner git -C "$SRC" diff --quiet "$BEFORE" "$AFTER" -- "$SUB/bun.lock"; then
  note "bun.lock moved, installing"
  as_owner docker run --rm -v "$SRC/$SUB:/app" -w /app \
    oven/bun:latest bun install --frozen-lockfile
else
  note "bun.lock unchanged, skipping install"
fi

# ---------------------------------------------------------------- recreate
# No container is preserved. Renaming it does not retain it: compose identifies
# containers by project and service labels, so --force-recreate removes the renamed one
# as well (verified). And a retained container would preserve its configuration rather
# than its revision, because the source is a shared bind mount. The fallback is $BEFORE,
# printed below, and nothing else.
if as_owner docker inspect "$NAME" >/dev/null 2>&1; then
  as_owner docker rm -f "$NAME" >/dev/null
fi

# --force-recreate is REQUIRED. The source is a bind mount, so new code does not change
# the service definition, and compose would report the service up to date while the
# running process kept the code it loaded at start.
note "recreating"
as_owner docker compose -f "$COMPOSE" up -d --force-recreate >/dev/null

# ---------------------------------------------------------------- the four checks
note "check 1: running"
sleep 6
STATUS=$(as_owner docker inspect "$NAME" --format '{{.State.Status}}')
[ "$STATUS" = "running" ] || die "check 1: state is $STATUS"
RESTARTS=$(as_owner docker inspect "$NAME" --format '{{.RestartCount}}')
[ "$RESTARTS" = "0" ] || die "check 1: $RESTARTS restarts, it is crash-looping"

note "check 2: connected and subscribed"
LOGS=$(as_owner docker logs "$NAME" 2>&1 || true)
printf '%s' "$LOGS" | grep -q 'connected to' || die "check 2: never connected"
printf '%s' "$LOGS" | grep -q 'subscribed to' || die "check 2: never subscribed"

# Check 3 is the only one that tells a real deployment from a no-op. It compares two
# TIMESTAMPS. It deliberately ignores the revision: a bind mount makes the checkout new
# whether or not the process restarted, which is the trap this check exists to catch.
note "check 3: the process restarted after the source moved"
STARTED=$(as_owner docker inspect "$NAME" --format '{{.State.StartedAt}}')
STARTED_EPOCH=$(date -u -d "$STARTED" +%s)
if [ "$STARTED_EPOCH" -lt "$PULLED_AT_EPOCH" ]; then
  die "check 3: started at $STARTED, BEFORE the source moved. The process is running old code."
fi
note "        started $((STARTED_EPOCH - PULLED_AT_EPOCH))s after the source moved"

# Check 4 cannot be automated from here: it needs reading Discord. So the script refuses
# to claim success instead of quietly reporting three of four.
if [ "$MIRROR_CONFIRMED" -eq 0 ]; then
  printf '%s %s\n' "$STARTED" "${AFTER:0:7}" | as_owner tee "$STATE" >/dev/null
  cat >&2 <<MSG

  checks 1 to 3 PASSED. check 4 is NOT done.

  Checks 1 to 3 prove the tap runs the new code. They do NOT prove it posts anything.
  A reply from a bus recipient does not prove it either: that is bus delivery, which
  works whether or not the tap runs. The log does not prove it: the tap logs only on
  failure, so a quiet log is consistent with a tap that posts nothing.

  Send any bus message, READ #fleet-bus, then run:

      deploy.sh --mirror-confirmed

  That confirms THIS deployment and deploys nothing.

  To roll back: deploy.sh --rollback ${BEFORE:0:7}
MSG
  exit 2
fi

note "check 4: mirroring confirmed by the operator"
note "deployed ${AFTER:0:7}, verified. Fallback ref: ${BEFORE:0:7}"

#!/usr/bin/env bash
# Does deploy.sh REJECT a deployment that silently did nothing?
#
# Watching the good path work is not verification. The defect this guards is a deploy
# that reports success while the running process keeps old code -- which is what the
# prose procedure did, and what no reviewer caught by reading it.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0; FAIL=0
ok()   { printf '  pass  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }

CN="deploytest-$$"
WORK="$(mktemp -d)"
cleanup() { docker rm -f "$CN" >/dev/null 2>&1; docker rm -f "$CN-"* >/dev/null 2>&1; rm -rf "$WORK"; }
trap cleanup EXIT

# A fake deployment: a git repo whose "tap" prints what check 2 looks for.
mkdir -p "$WORK/tools/fleet-bus"
cd "$WORK"
git init -q . && git config user.email t@t && git config user.name t
cat > tools/fleet-bus/app.js <<'APP'
console.log('connected to nats://fake')
console.log('subscribed to fleet.>')
setInterval(() => {}, 1000)
APP
cat > tools/fleet-bus/compose.yml <<CMP
services:
  tap:
    container_name: $CN
    image: oven/bun:latest
    command: bun app.js
    working_dir: /app
    volumes:
      - $WORK/tools/fleet-bus:/app:ro
CMP
touch tools/fleet-bus/bun.lock
git add -A && git commit -qm one
echo "// two" >> tools/fleet-bus/app.js && git add -A && git commit -qm two

export FLEET_BUS_SRC="$WORK" FLEET_BUS_CONTAINER="$CN" FLEET_BUS_OWNER="$(id -un)"
# Must be set for EVERY case, not just the state ones: the default path lives in the
# owner's home and an unconfirmed deploy writes there, which failed the first case
# with exit 1 instead of 2.
export FLEET_BUS_STATE="$WORK/state"

# ---- the good path: checks 1-3 pass, and it still REFUSES to claim success ----
OUT=$("$HERE/deploy.sh" --rollback HEAD 2>&1); RC=$?
if [ "$RC" -eq 2 ]; then ok "it exits 2 when the mirror is unconfirmed, rather than reporting success"
else bad "expected exit 2 for unconfirmed mirror, got $RC"; fi
printf '%s' "$OUT" | grep -q 'check 3' && ok "check 3 ran" || bad "check 3 did not run"

OUT=$("$HERE/deploy.sh" --rollback HEAD --mirror-confirmed 2>&1); RC=$?
if [ "$RC" -eq 0 ]; then ok "it reports success once the mirror is confirmed"
else bad "good path failed: $RC"; printf '%s\n' "$OUT" | tail -3; fi

# ---- the fallback must be a REF, and it must be reported ----
if printf '%s' "$OUT" | grep -qi 'fallback ref'; then
  ok "it reports the fallback ref, which is the only thing that recovers the old code"
else
  bad "no fallback ref reported: nothing tells the operator what to roll back to"
fi

# ---- THE REJECTION: a deploy that does not restart the process ----
# Mutate the script so it skips the recreate entirely, which is precisely what plain
# "compose up -d" does against a bind mount. Check 3 must catch it.
# Skip the removal AND --force-recreate, so the container keeps its name and its original
# process: exactly the state plain "compose up -d" leaves against a bind mount. An
# earlier version of this test removed the container and so never reached check 3 at all.
# Disable BOTH the removal and --force-recreate, which is the procedure DEPLOY.md
# described before this PR: pull, then plain "compose up -d". Compose reports the
# service up to date and the old process keeps running. The script's own "docker rm -f"
# makes that impossible, so the mutation has to remove it too, otherwise the test proves
# nothing about check 3.
sed -e 's/up -d --force-recreate/up -d/' \
    -e 's/^  as_owner docker rm -f/  true #/' \
    "$HERE/deploy.sh" > "$WORK/noop.sh"
chmod +x "$WORK/noop.sh"
OUT=$("$WORK/noop.sh" --rollback HEAD --mirror-confirmed 2>&1); RC=$?
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q 'running old code'; then
  ok "check 3 REJECTS a deploy that left the old process running"
else
  bad "check 3 passed a no-op deploy (rc=$RC) -- the enforcement does nothing"
  printf '%s\n' "$OUT" | tail -4
fi

# ---- the owner must be able to run it ----
# It always prefixed "sudo -u $OWNER", so luna running it got "user luna is not allowed
# to execute ... as luna". The NORMAL case was broken and no test covered it, because
# every test ran as a user who was not the owner.
OUT=$(FLEET_BUS_OWNER="$(id -un)" "$HERE/deploy.sh" --rollback HEAD --mirror-confirmed 2>&1); RC=$?
if [ "$RC" -eq 0 ] && ! printf '%s' "$OUT" | grep -q 'not allowed to execute'; then
  ok "the owning user can run it without sudo escalating to itself"
else
  bad "the owner cannot run it (rc=$RC)"
  printf '%s\n' "$OUT" | grep -i 'not allowed' | head -1
fi

# ---- check 4 must be confirmable WITHOUT deploying again ----
# Recording one manual check required re-running the whole deploy, which restarted the
# tap a second time and reset its status dedupe: deploying in order to verify a
# deployment.
OUT=$(FLEET_BUS_OWNER="$(id -un)" "$HERE/deploy.sh" --rollback HEAD 2>&1); RC=$?
if [ "$RC" -eq 2 ] && [ -f "$FLEET_BUS_STATE" ]; then
  ok "an unconfirmed deployment records which process was checked"
else
  bad "no state recorded (rc=$RC), so check 4 cannot be confirmed later"
fi

BEFORE_START=$(docker inspect "$CN" --format '{{.State.StartedAt}}' 2>/dev/null)
OUT=$(FLEET_BUS_OWNER="$(id -un)" "$HERE/deploy.sh" --mirror-confirmed 2>&1); RC=$?
AFTER_START=$(docker inspect "$CN" --format '{{.State.StartedAt}}' 2>/dev/null)
if [ "$RC" -eq 0 ] && [ "$BEFORE_START" = "$AFTER_START" ]; then
  ok "confirming check 4 deploys NOTHING: the container did not restart"
else
  bad "confirming check 4 restarted the container or failed (rc=$RC)"
fi

# ---- and it must REFUSE when there is nothing pending ----
# The case my previous test did NOT cover. Test 8 passed only because the test before it
# always wrote the state file, so the absent-state path never fired. With nothing pending,
# the flag used to skip the confirm branch, run a FULL DEPLOYMENT, and then report
# "verified" and exit 0. (Kat, PR 70.)
rm -f "$FLEET_BUS_STATE"
START_BEFORE=$(docker inspect "$CN" --format '{{.State.StartedAt}}' 2>/dev/null)
OUT=$(FLEET_BUS_OWNER="$(id -un)" "$HERE/deploy.sh" --mirror-confirmed 2>&1); RC=$?
START_AFTER=$(docker inspect "$CN" --format '{{.State.StartedAt}}' 2>/dev/null)
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q 'nothing pending\|no pending'; then
  ok "it REFUSES to confirm when no deployment is pending"
else
  bad "it accepted a confirm with nothing pending (rc=$RC)"
fi
# WEAKER THAN IT LOOKS, and said so rather than trusted. The fixture repo has no remote,
# so under the mutated guard the fall-through dies at "git pull" instead of deploying.
# This assertion therefore passes whether the deploy was REFUSED or merely FAILED. The
# case above is the one that discriminates: it checks for the refusal message and was
# verified against the mutation. In production, where the pull succeeds, this one matters.
if [ "$START_BEFORE" = "$START_AFTER" ]; then
  ok "and that refusal deployed NOTHING (weak here: see comment)"
else
  bad "a confirm with nothing pending triggered a deployment"
fi

# ---- and it must REFUSE to confirm a different process ----
FLEET_BUS_OWNER="$(id -un)" "$HERE/deploy.sh" --rollback HEAD >/dev/null 2>&1 || true
docker restart "$CN" >/dev/null 2>&1
sleep 3
OUT=$(FLEET_BUS_OWNER="$(id -un)" "$HERE/deploy.sh" --mirror-confirmed 2>&1); RC=$?
if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q 'restarted since'; then
  ok "it REFUSES to confirm a deployment whose process has since restarted"
else
  bad "it confirmed a process the operator never checked (rc=$RC)"
fi

# ---- A FAILED DEPLOY MUST LEAVE THE SERVICE RUNNING ----
# The outage on 2026-10-04. The script removed the container and then recreated it, so a
# failure in between left nothing running, with no error anyone saw and no rollback. The
# detector cannot report it, because the detector IS the tap.
FLEET_BUS_OWNER="$(id -un)" "$HERE/deploy.sh" --rollback HEAD --mirror-confirmed >/dev/null 2>&1 || true
if docker inspect "$CN" >/dev/null 2>&1; then
  ok "a container is running before the failure case"

  # Break compose so the recreate cannot succeed, leaving only the removal to do damage.
  cp "$WORK/tools/fleet-bus/compose.yml" "$WORK/compose.good"
  printf 'services:\n  tap:\n    image: [this is not valid yaml\n' > "$WORK/tools/fleet-bus/compose.yml"

  OUT=$(FLEET_BUS_OWNER="$(id -un)" "$HERE/deploy.sh" --rollback HEAD 2>&1); RC=$?
  cp "$WORK/compose.good" "$WORK/tools/fleet-bus/compose.yml"

  if [ "$RC" -ne 0 ]; then
    ok "a broken compose file makes the deploy FAIL rather than report success"
  else
    bad "the deploy reported success with a broken compose file (rc=$RC)"
  fi

  if docker inspect "$CN" >/dev/null 2>&1; then
    ok "and the container SURVIVES a failed deploy, rather than being left deleted"
  else
    bad "a failed deploy destroyed the running container -- this is the 2026-10-04 outage"
  fi
else
  bad "no container to run the failure case against"
fi

printf '\n  %d pass, %d fail\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

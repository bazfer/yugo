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

printf '\n  %d pass, %d fail\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

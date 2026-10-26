#!/usr/bin/env bash
# Integration tests for the bazfer/yugo deploy pipeline.
#
# Mutation-verified: every guard in this file is proven by removing the fix
# and watching the test go RED. Pass-path alone is insufficient.
#
# Requires: python3, docker (for docker stub tests), PyYAML (pyyaml>=6.0).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "PASS  $1"; PASS_COUNT=$((PASS_COUNT+1)); }
fail() { echo "FAIL  $1: $2"; FAIL_COUNT=$((FAIL_COUNT+1)); }

# ── Preflight: assert-compose-image-form.sh ──────────────────────────────────

# Acceptance 3: workflow refuses a build from a stale host checkout.
# assert-compose-image-form.sh is the gate — it runs in the preflight step
# before any deploy. Mutation: remove the build: check → test goes RED.

test_preflight_rejects_build_form() {
  local name="test_preflight_rejects_build_form"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  cat > "$tmpdir/compose.yml" << 'EOF'
services:
  bot:
    build:
      context: /home/luna/repos/yugo2
      dockerfile: yugo/Dockerfile
    container_name: chis
EOF
  local exit_code=0
  bash "${SCRIPT_DIR}/scripts/assert-compose-image-form.sh" "$tmpdir/compose.yml" 2>/dev/null \
    || exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    pass "$name"
  else
    fail "$name" "assert-compose-image-form.sh did not reject a compose file with build: context: /home/luna/repos/yugo2"
  fi
}

test_preflight_rejects_any_build_form() {
  local name="test_preflight_rejects_any_build_form"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  cat > "$tmpdir/compose.yml" << 'EOF'
services:
  bot:
    build: .
    container_name: coordinator
EOF
  local exit_code=0
  bash "${SCRIPT_DIR}/scripts/assert-compose-image-form.sh" "$tmpdir/compose.yml" 2>/dev/null \
    || exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    pass "$name"
  else
    fail "$name" "assert-compose-image-form.sh did not reject a compose file with build: ."
  fi
}

test_preflight_passes_image_form() {
  local name="test_preflight_passes_image_form"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  cat > "$tmpdir/compose.yml" << 'EOF'
services:
  bot:
    image: chis-bot:${CODEX_IMAGE_TAG:-latest}
    container_name: chis
EOF
  if bash "${SCRIPT_DIR}/scripts/assert-compose-image-form.sh" "$tmpdir/compose.yml" 2>/dev/null; then
    pass "$name"
  else
    fail "$name" "assert-compose-image-form.sh rejected a valid image: form compose file"
  fi
}

test_preflight_rejects_missing_file() {
  local name="test_preflight_rejects_missing_file"
  local exit_code=0
  bash "${SCRIPT_DIR}/scripts/assert-compose-image-form.sh" "/nonexistent/compose.yml" 2>/dev/null \
    || exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    pass "$name"
  else
    fail "$name" "assert-compose-image-form.sh did not fail on a missing file"
  fi
}

# ── deploy.sh: GH_TOKEN guard ─────────────────────────────────────────────────

test_deploy_fails_without_gh_token() {
  local name="test_deploy_fails_without_gh_token"
  local exit_code=0
  local output
  output="$(GH_TOKEN="" bash "${SCRIPT_DIR}/scripts/deploy.sh" coordinator 2>&1)" \
    || exit_code=$?
  if [[ $exit_code -ne 0 ]] && echo "$output" | grep -q "GH_TOKEN"; then
    pass "$name"
  else
    fail "$name" "expected non-zero exit with GH_TOKEN error; got exit=${exit_code}"
  fi
}

test_deploy_fails_on_unknown_bot() {
  local name="test_deploy_fails_on_unknown_bot"
  local exit_code=0
  local output
  output="$(GH_TOKEN="fake" bash "${SCRIPT_DIR}/scripts/deploy.sh" fakebot 2>&1)" \
    || exit_code=$?
  if [[ $exit_code -ne 0 ]] && echo "$output" | grep -q "unknown bot"; then
    pass "$name"
  else
    fail "$name" "expected non-zero exit with unknown bot error; got exit=${exit_code}, output=${output}"
  fi
}

# ── deploy.sh: first-cutover guard ───────────────────────────────────────────
# Compose cannot adopt a running container it did not start. The script
# must refuse and tell the operator to stop/rm the old container first.
# Mutation: remove the COMPOSE_PROJECT check → test goes RED.

test_deploy_refuses_unmanaged_container() {
  local name="test_deploy_refuses_unmanaged_container"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  cat > "$tmpdir/compose.yml" << 'EOF'
services:
  coordinator:
    image: yugo-coordinator:${CODEX_IMAGE_TAG:-latest}
    container_name: coordinator-test-unmanaged
EOF

  # Stub docker: simulate a running container with NO compose project label.
  cat > "$tmpdir/docker" << 'STUBEOF'
#!/usr/bin/env bash
args=("$@")
for i in "${!args[@]}"; do
  if [[ "${args[$i]}" == "--format" ]]; then
    fmt="${args[$((i+1))]}"
    if [[ "$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    if [[ "$fmt" == *"Image"* && "$fmt" != *"Labels"* ]]; then echo "sha256:abc123"; exit 0; fi
    if [[ "$fmt" == *"codex.commit"* ]]; then echo ""; exit 0; fi
    # compose project label — empty = not compose-managed
    if [[ "$fmt" == *"com.docker.compose.project"* ]]; then echo ""; exit 0; fi
    echo ""; exit 0
  fi
done
exit 0
STUBEOF
  chmod +x "$tmpdir/docker"

  local exit_code=0
  local output
  output="$(PATH="${tmpdir}:${PATH}" \
    GH_TOKEN="fake-token" \
    YUGO_COMPOSE_COORDINATOR="$tmpdir/compose.yml" \
    YUGO_SUDO_COORDINATOR="none" \
    bash "${SCRIPT_DIR}/scripts/deploy.sh" coordinator 2>&1)" || exit_code=$?

  if [[ $exit_code -ne 0 ]] && echo "$output" | grep -qi "stop.*rm\|first compose-managed"; then
    pass "$name"
  else
    fail "$name" "expected refusal for unmanaged container; got exit=${exit_code}, output=${output}"
  fi
}

# ── drift-check.sh ────────────────────────────────────────────────────────────

# Acceptance 2: drift gate must go RED against a stale container.
# Mutation: change label check from != to == → test goes RED.

test_drift_check_fails_on_stale_label() {
  local name="test_drift_check_fails_on_stale_label"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local deployed_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  local stale_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

  # Docker stub: coordinator running at stale_sha label.
  cat > "$tmpdir/docker" << STUBEOF
#!/usr/bin/env bash
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    if [[ "\$fmt" == *"codex.commit"* ]]; then echo "${stale_sha}"; exit 0; fi
  fi
done
exit 0
STUBEOF
  chmod +x "$tmpdir/docker"

  local exit_code=0
  PATH="${tmpdir}:${PATH}" EXPECTED_SHA="$deployed_sha" BOTS="coordinator" \
    bash "${SCRIPT_DIR}/scripts/drift-check.sh" 2>/dev/null || exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    pass "$name"
  else
    fail "$name" "drift-check passed when container label (${stale_sha:0:8}) != deployed SHA (${deployed_sha:0:8})"
  fi
}

test_drift_check_fails_on_stopped_container() {
  local name="test_drift_check_fails_on_stopped_container"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  cat > "$tmpdir/docker" << 'STUBEOF'
#!/usr/bin/env bash
for arg in "$@"; do
  if [[ "$arg" == *"State.Running"* ]]; then echo "false"; exit 0; fi
done
exit 0
STUBEOF
  chmod +x "$tmpdir/docker"

  local exit_code=0
  PATH="${tmpdir}:${PATH}" EXPECTED_SHA="aaaa" BOTS="coordinator" \
    bash "${SCRIPT_DIR}/scripts/drift-check.sh" 2>/dev/null || exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    pass "$name"
  else
    fail "$name" "drift-check passed for a stopped container"
  fi
}

# Acceptance 4: drift gate fails when chis yugo2-src is left behind.
# Mutation: remove the yugo2-src PINNED.md check → test goes RED.

test_drift_check_fails_on_yugo2src_mismatch() {
  local name="test_drift_check_fails_on_yugo2src_mismatch"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local deployed_sha="cccccccccccccccccccccccccccccccccccccccc"
  local stale_pin="dddddddddddddddddddddddddddddddddddddddd"

  # PINNED.md at old SHA
  printf 'SHA: %s\nDate: 2026-09-18\n' "$stale_pin" > "$tmpdir/PINNED.md"

  cat > "$tmpdir/docker" << STUBEOF
#!/usr/bin/env bash
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    if [[ "\$fmt" == *"codex.commit"* ]]; then echo "${deployed_sha}"; exit 0; fi
  fi
done
exit 0
STUBEOF
  chmod +x "$tmpdir/docker"

  local exit_code=0
  PATH="${tmpdir}:${PATH}" \
    EXPECTED_SHA="$deployed_sha" \
    BOTS="chis" \
    YUGO2_SRC_DIR="$tmpdir" \
    bash "${SCRIPT_DIR}/scripts/drift-check.sh" 2>/dev/null || exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    pass "$name"
  else
    fail "$name" "drift-check passed when yugo2-src PINNED.md (${stale_pin:0:8}) != deployed SHA (${deployed_sha:0:8})"
  fi
}

test_drift_check_passes_when_all_match() {
  local name="test_drift_check_passes_when_all_match"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local sha="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  printf 'SHA: %s\nDate: 2026-10-24\n' "$sha" > "$tmpdir/PINNED.md"

  cat > "$tmpdir/docker" << STUBEOF
#!/usr/bin/env bash
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    if [[ "\$fmt" == *"codex.commit"* ]]; then echo "${sha}"; exit 0; fi
  fi
done
exit 0
STUBEOF
  chmod +x "$tmpdir/docker"

  # git stub: HEAD matches expected SHA
  cat > "$tmpdir/git" << STUBEOF
#!/usr/bin/env bash
for a in "\$@"; do
  if [[ "\$a" == "rev-parse" ]]; then echo "${sha}"; exit 0; fi
done
exit 0
STUBEOF
  chmod +x "$tmpdir/git"

  local exit_code=0
  PATH="${tmpdir}:${PATH}" \
    EXPECTED_SHA="$sha" \
    BOTS="chis" \
    YUGO2_SRC_DIR="$tmpdir" \
    bash "${SCRIPT_DIR}/scripts/drift-check.sh" 2>/dev/null || exit_code=$?
  if [[ $exit_code -eq 0 ]]; then
    pass "$name"
  else
    fail "$name" "drift-check failed when everything matched"
  fi
}

# ── Structural: workflow YAML (parsed, not grepped) ───────────────────────────

# Parsed assertion: GH_TOKEN uses github.token, not a secrets reference.
# Mutation: change to secrets.SOMETHING → test goes RED.

test_workflow_uses_automatic_gh_token() {
  local name="test_workflow_uses_automatic_gh_token"
  local workflow="${SCRIPT_DIR}/.github/workflows/deploy.yml"
  local pyfile msg
  pyfile="$(mktemp)"
  cat > "$pyfile" << 'PYEOF'
import sys
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not available — install it; a missing guard is a broken guard")
    sys.exit(1)
path = sys.argv[1]
with open(path) as f:
    doc = yaml.safe_load(f)
deploy_env = ((doc.get("jobs") or {}).get("deploy") or {}).get("env") or {}
val = str(deploy_env.get("GH_TOKEN", ""))
expected = "${{ github.token }}"
if "secrets." in val:
    print("ERROR: GH_TOKEN uses secrets reference: " + repr(val))
    sys.exit(1)
if val != expected:
    print("ERROR: GH_TOKEN is " + repr(val) + ", expected " + repr(expected))
    sys.exit(1)
PYEOF
  if msg="$(python3 "$pyfile" "$workflow" 2>&1)"; then
    rm -f "$pyfile"; pass "$name"
  else
    rm -f "$pyfile"; fail "$name" "$msg"
  fi
}

# Parsed assertion: permissions.actions == 'write' and permissions.contents == 'read'.
# Mutation 1: remove actions:write, leave comment → RED.
# Mutation 2: remove actions:write, inline comment on contents line → RED.
# Mutation 3: remove from permissions:, add decoy in "jobs": env block → RED.

test_workflow_has_actions_write_permission() {
  local name="test_workflow_has_actions_write_permission"
  local workflow="${SCRIPT_DIR}/.github/workflows/deploy.yml"
  local pyfile msg
  pyfile="$(mktemp)"
  cat > "$pyfile" << 'PYEOF'
import sys
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not available — install it; a missing guard is a broken guard")
    sys.exit(1)
path = sys.argv[1]
with open(path) as f:
    doc = yaml.safe_load(f)
perms = doc.get("permissions") or {}
if not isinstance(perms, dict):
    print("ERROR: permissions is not a mapping")
    sys.exit(1)
if perms.get("actions") != "write":
    print("ERROR: permissions.actions == " + repr(perms.get("actions")) + ", expected 'write'. A comment or job env entry is not the permission.")
    sys.exit(1)
if perms.get("contents") != "read":
    print("ERROR: permissions.contents == " + repr(perms.get("contents")) + ", expected 'read'")
    sys.exit(1)
PYEOF
  if msg="$(python3 "$pyfile" "$workflow" 2>&1)"; then
    rm -f "$pyfile"; pass "$name"
  else
    rm -f "$pyfile"; fail "$name" "$msg"
  fi
}

# Structural: handoff step must run on failure too.
# Mutation: remove 'if: always()' → test goes RED.

test_workflow_handoff_always_runs() {
  local name="test_workflow_handoff_always_runs"
  if grep -A3 "Handoff — dispatch for HEAD if moved" \
      "${SCRIPT_DIR}/.github/workflows/deploy.yml" | grep -q "if: always()"; then
    pass "$name"
  else
    fail "$name" "handoff step missing 'if: always()' — will not run on deploy failure"
  fi
}

# ── handoff.sh ────────────────────────────────────────────────────────────────

test_handoff_fails_when_dispatch_fails() {
  local name="test_handoff_fails_when_dispatch_fails"
  local deployed_sha="dddddddddddddddddddddddddddddddddddddddd"
  local head_sha="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  local dir
  dir="$(mktemp -d)"
  trap "rm -rf '${dir}'" RETURN

  cat > "${dir}/curl" << STUBEOF
#!/usr/bin/env bash
for arg in "\$@"; do
  if echo "\$arg" | grep -q "dispatches"; then exit 22; fi
  if echo "\$arg" | grep -q "commits/main"; then echo '{"sha": "${head_sha}"}'; exit 0; fi
done
exit 1
STUBEOF
  chmod +x "${dir}/curl"

  local exit_code=0
  PATH="${dir}:${PATH}" \
    EXPECTED_SHA="$deployed_sha" \
    GH_TOKEN="fake-token" \
    DISPATCH_TOKEN="fake-dispatch-token" \
    REPO="bazfer/yugo" \
    WORKFLOW_ID="deploy.yml" \
    bash "${SCRIPT_DIR}/scripts/handoff.sh" 2>/dev/null || exit_code=$?
  if [[ $exit_code -eq 0 ]]; then
    fail "$name" "handoff.sh exited 0 on dispatch failure — must be visible, not silenced"
  else
    pass "$name"
  fi
}

test_handoff_no_dispatch_when_head_unchanged() {
  local name="test_handoff_no_dispatch_when_head_unchanged"
  local sha="ffffffffffffffffffffffffffffffffffffffff"
  local dir
  dir="$(mktemp -d)"
  trap "rm -rf '${dir}'" RETURN

  cat > "${dir}/curl" << STUBEOF
#!/usr/bin/env bash
if echo "\$@" | grep -q "commits/main"; then echo '{"sha": "${sha}"}'; exit 0; fi
echo "unexpected curl call" >&2; exit 1
STUBEOF
  chmod +x "${dir}/curl"

  local exit_code=0
  PATH="${dir}:${PATH}" \
    EXPECTED_SHA="$sha" \
    GH_TOKEN="fake-token" \
    REPO="bazfer/yugo" \
    WORKFLOW_ID="deploy.yml" \
    bash "${SCRIPT_DIR}/scripts/handoff.sh" 2>/dev/null || exit_code=$?
  if [[ $exit_code -eq 0 ]]; then
    pass "$name"
  else
    fail "$name" "handoff.sh failed when no handoff was needed (exit=${exit_code})"
  fi
}

# ── deploy.sh: compose up is actually invoked ─────────────────────────────────
# Mutation: replace run_compose with a no-op → test goes RED.

test_compose_up_is_invoked() {
  local name="test_compose_up_is_invoked"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local commit="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  local build_id="sha256:testbuildid000000000000000000000000000000000000000000000000000000"
  local state="${tmpdir}/container_state"
  local compose_log="${tmpdir}/compose_calls"

  cat > "${tmpdir}/compose.yml" << 'EOF'
services:
  coordinator:
    image: yugo-coordinator:${YUGO_IMAGE_TAG:-latest}
    container_name: yugo-coordinator
EOF

  cat > "${tmpdir}/docker" << STUBEOF
#!/usr/bin/env bash
STATE="${state}"
COMPOSE_LOG="${compose_log}"
BUILD_ID="${build_id}"
COMMIT_SHA="${commit}"
if [[ "\${1:-}" == "compose" ]]; then
  for a in "\$@"; do
    [[ "\$a" == "up" ]] || continue
    echo "called" >> "\${COMPOSE_LOG}"
    touch "\${STATE}"
    exit 0
  done
  exit 0
fi
[[ "\${1:-}" == "rmi" || "\${1:-}" == "tag" ]] && exit 0
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    if [[ -f "\${STATE}" ]]; then
      if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
      if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${COMMIT_SHA}"; exit 0; fi
      if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${BUILD_ID}"; exit 0; fi
      if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "yugo"; exit 0; fi
    else
      echo "error: no such object: yugo-coordinator" >&2; exit 1
    fi
  fi
done
exit 0
STUBEOF
  chmod +x "${tmpdir}/docker"

  local exit_code=0
  PATH="${tmpdir}:${PATH}" \
    GH_TOKEN="fake" \
    YUGO_COMMIT_OVERRIDE="$commit" \
    YUGO_BUILD_IMAGE_ID="$build_id" \
    YUGO_COMPOSE_COORDINATOR="${tmpdir}/compose.yml" \
    YUGO_SUDO_COORDINATOR="none" \
    bash "${SCRIPT_DIR}/scripts/deploy.sh" coordinator 2>&1 || exit_code=$?

  if [[ $exit_code -eq 0 ]] && [[ -s "$compose_log" ]]; then
    pass "$name"
  else
    fail "$name" "compose up not called or deploy failed (exit=${exit_code})"
  fi
}

# ── deploy.sh: compose failure triggers rollback ──────────────────────────────
# MUTATION: Replace the body of rollback() with `echo "ROLLBACK COMPLETE" >&2; exit 1`
# Expected colour: RED — no compose up is called, STATE file never created, state
# assertion fails. A string match on "ROLLBACK" would stay GREEN; this test does not.

test_compose_failure_triggers_rollback() {
  local name="test_compose_failure_triggers_rollback"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local commit="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  local prev_commit="cccccccccccccccccccccccccccccccccccccccc"
  local build_id="sha256:newbuild000000000000000000000000000000000000000000000000000000000"
  local prev_id="sha256:oldbuild000000000000000000000000000000000000000000000000000000000"
  local call_count="${tmpdir}/compose_calls"
  local state="${tmpdir}/container_state"

  cat > "${tmpdir}/compose.yml" << 'EOF'
services:
  coordinator:
    image: yugo-coordinator:${YUGO_IMAGE_TAG:-latest}
    container_name: yugo-coordinator
EOF

  cat > "${tmpdir}/docker" << STUBEOF
#!/usr/bin/env bash
CALLS="${call_count}"
STATE="${state}"
PREV_ID="${prev_id}"
PREV_COMMIT_SHA="${prev_commit}"
if [[ "\${1:-}" == "compose" ]]; then
  for a in "\$@"; do
    [[ "\$a" == "up" ]] || continue
    count=\$(cat "\${CALLS}" 2>/dev/null || echo 0)
    echo \$((count+1)) > "\${CALLS}"
    if [[ \$((count+1)) -eq 1 ]]; then exit 1; fi  # first up fails
    touch "\${STATE}"
    exit 0
  done
  exit 0
fi
[[ "\${1:-}" == "tag" || "\${1:-}" == "rmi" ]] && exit 0
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    if [[ -f "\${STATE}" ]]; then
      if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
      if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${PREV_ID}"; exit 0; fi
      if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${PREV_COMMIT_SHA}"; exit 0; fi
      if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "yugo"; exit 0; fi
    else
      if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${PREV_ID}"; exit 0; fi
      if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${PREV_COMMIT_SHA}"; exit 0; fi
      if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "yugo"; exit 0; fi
      if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    fi
    echo ""; exit 0
  fi
done
exit 0
STUBEOF
  chmod +x "${tmpdir}/docker"

  local exit_code=0
  local output
  output="$(PATH="${tmpdir}:${PATH}" \
    GH_TOKEN="fake" \
    YUGO_COMMIT_OVERRIDE="$commit" \
    YUGO_BUILD_IMAGE_ID="$build_id" \
    YUGO_COMPOSE_COORDINATOR="${tmpdir}/compose.yml" \
    YUGO_SUDO_COORDINATOR="none" \
    bash "${SCRIPT_DIR}/scripts/deploy.sh" coordinator 2>&1)" || exit_code=$?

  # Assert restored STATE, not output strings:
  # 1. Script exited nonzero (deploy reported failure)
  # 2. STATE file exists — rollback's compose up ran (stub only sets it on 2nd up call)
  # 3. Container is "running" with the prev image ID (from stub when STATE exists)
  local final_image=""
  if [[ -f "${state}" ]]; then
    # Read image from stub: when STATE exists, stub returns PREV_ID for .Image queries
    final_image="$(PATH="${tmpdir}:${PATH}" docker inspect yugo-coordinator \
      --format '{{.Image}}' 2>/dev/null || true)"
  fi
  if [[ $exit_code -ne 0 ]] \
      && [[ -f "${state}" ]] \
      && [[ "$final_image" == "$prev_id" ]]; then
    pass "$name"
  else
    fail "$name" "rollback state wrong: exit=${exit_code}, state_file=$(ls "${state}" 2>/dev/null || echo MISSING), final_image=${final_image:-NONE} (want prev_id=${prev_id:0:16})"
  fi
}

# ── deploy.sh: chis deploy updates yugo2-src ──────────────────────────────────
# Mutation: delete the chis yugo2-src update block → test goes RED.

test_chis_deploy_updates_yugo2_src() {
  local name="test_chis_deploy_updates_yugo2_src"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local commit="dddddddddddddddddddddddddddddddddddddddd"
  local build_id="sha256:chisbuild00000000000000000000000000000000000000000000000000000000"
  local yugo2_src="${tmpdir}/yugo2-src"
  local state="${tmpdir}/container_state"
  local git_log="${tmpdir}/git_calls"
  mkdir -p "$yugo2_src"
  # PINNED.md must exist so snapshot preflight does not abort the deploy.
  printf 'SHA: prevsha0000000000000000000000000000000000\nDate: 2026-01-01\n' \
    > "$yugo2_src/PINNED.md"

  cat > "${tmpdir}/compose.yml" << 'EOF'
services:
  bot:
    image: chis-bot:${YUGO_IMAGE_TAG:-latest}
    container_name: chis
EOF

  # Loud-fail bare git stub: catches any bypass of YUGO_GIT_CHIS
  cat > "${tmpdir}/git" << 'STUBEOF'
#!/usr/bin/env bash
echo "ERROR: bare git called directly — must use yugo-git-chis wrapper" >&2
exit 1
STUBEOF
  chmod +x "${tmpdir}/git"

  # yugo-git-chis wrapper stub: logs calls; returns stable HEAD for rev-parse
  cat > "${tmpdir}/yugo-git-chis" << STUBEOF
#!/usr/bin/env bash
echo "\$*" >> "${git_log}"
case "\${1:-}" in
  rev-parse) echo "prevsha0000000000000000000000000000000000" ;;
  checkout)  exit 0 ;;
  fetch)     exit 0 ;;
  *)         exit 1 ;;
esac
STUBEOF
  chmod +x "${tmpdir}/yugo-git-chis"

  cat > "${tmpdir}/docker" << STUBEOF
#!/usr/bin/env bash
STATE="${state}"
BUILD_ID="${build_id}"
COMMIT_SHA="${commit}"
if [[ "\${1:-}" == "compose" ]]; then
  for a in "\$@"; do
    [[ "\$a" == "up" ]] || continue
    touch "\${STATE}"
    exit 0
  done
  exit 0
fi
[[ "\${1:-}" == "rmi" || "\${1:-}" == "tag" ]] && exit 0
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    if [[ -f "\${STATE}" ]]; then
      if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
      if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${COMMIT_SHA}"; exit 0; fi
      if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${BUILD_ID}"; exit 0; fi
      if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "chis"; exit 0; fi
    else
      echo "error: no such object: chis" >&2; exit 1
    fi
  fi
done
exit 0
STUBEOF
  chmod +x "${tmpdir}/docker"

  local exit_code=0
  PATH="${tmpdir}:${PATH}" \
    GH_TOKEN="fake" \
    YUGO_COMMIT_OVERRIDE="$commit" \
    YUGO_BUILD_IMAGE_ID="$build_id" \
    YUGO_COMPOSE_CHIS="${tmpdir}/compose.yml" \
    YUGO_SUDO_CHIS="none" \
    YUGO_YUGO2_SRC="$yugo2_src" \
    YUGO_GIT_CHIS="${tmpdir}/yugo-git-chis" \
    bash "${SCRIPT_DIR}/scripts/deploy.sh" chis 2>&1 || exit_code=$?

  local pinned_sha=""
  pinned_sha="$(grep -oP '^SHA: \K\S+' "${yugo2_src}/PINNED.md" 2>/dev/null || true)"

  if [[ $exit_code -eq 0 ]] \
      && grep -q "checkout" "${git_log}" 2>/dev/null \
      && [[ "$pinned_sha" == "$commit" ]]; then
    pass "$name"
  else
    fail "$name" "expected exit=0, git checkout called, PINNED.md updated; got exit=${exit_code}, pinned=${pinned_sha}, git_log=$(cat "$git_log" 2>/dev/null || echo EMPTY)"
  fi
}

# ── drift-check.sh: fails on git HEAD mismatch ────────────────────────────────
# Mutation: remove the git rev-parse HEAD check → test goes RED.

test_drift_check_fails_on_git_head_mismatch() {
  local name="test_drift_check_fails_on_git_head_mismatch"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local expected_sha="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
  local head_sha="ffffffffffffffffffffffffffffffffffffffff"

  # PINNED.md matches, but git HEAD does not
  printf 'SHA: %s\nDate: 2026-10-24\n' "$expected_sha" > "$tmpdir/PINNED.md"

  cat > "$tmpdir/git" << STUBEOF
#!/usr/bin/env bash
for a in "\$@"; do
  if [[ "\$a" == "rev-parse" ]]; then echo "${head_sha}"; exit 0; fi
done
exit 0
STUBEOF
  chmod +x "$tmpdir/git"

  cat > "$tmpdir/docker" << STUBEOF
#!/usr/bin/env bash
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    if [[ "\$fmt" == *"codex.commit"* ]]; then echo "${expected_sha}"; exit 0; fi
    echo ""; exit 0
  fi
done
exit 0
STUBEOF
  chmod +x "$tmpdir/docker"

  local exit_code=0
  PATH="${tmpdir}:${PATH}" \
    EXPECTED_SHA="$expected_sha" \
    BOTS="chis" \
    YUGO2_SRC_DIR="$tmpdir" \
    bash "${SCRIPT_DIR}/scripts/drift-check.sh" 2>/dev/null || exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    pass "$name"
  else
    fail "$name" "drift-check passed when git HEAD (${head_sha:0:8}) != EXPECTED_SHA (${expected_sha:0:8})"
  fi
}

# ── deploy.sh: chis rollback restores yugo2-src ──────────────────────────────
# MUTATION: Delete the chis restore-and-verify block from rollback() in
#           scripts/deploy.sh (the "if [[ "$BOT" == "chis" ...fi" block before
#           ROLLBACK COMPLETE, lines ~199-235)
# Expected colour: RED — git HEAD stays at deploy SHA and PINNED.md has new
#                  content because the rollback never ran the checkout.
#
# Also pins: ROLLBACK COMPLETE must be announced only on verified success.
# MUTATION: Delete the `echo "ROLLBACK COMPLETE: ..."` line from rollback()
# Expected colour: RED — ROLLBACK COMPLETE absent from output

test_chis_rollback_restores_source() {
  local name="test_chis_rollback_restores_source"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local prev_commit="prevsha000000000000000000000000000000000"
  local prev_id="sha256:oldbuild0000000000000000000000000000000000000000000000000000000000"
  local build_id="sha256:newbuild1000000000000000000000000000000000000000000000000000000000"
  local state="${tmpdir}/container_state"
  local call_count="${tmpdir}/compose_call_count"
  echo 0 > "$call_count"

  # Real git repo: two commits so git checkout can move HEAD back to pre-deploy state.
  local yugo2_src="${tmpdir}/yugo2-src"
  mkdir -p "$yugo2_src"
  git -C "$yugo2_src" init -q 2>/dev/null || git -C "$yugo2_src" init 2>/dev/null
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit --allow-empty -m "pre-deploy" -q 2>/dev/null
  > "$yugo2_src/PINNED.md"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T add PINNED.md 2>/dev/null
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit -m "add pinned" -q 2>/dev/null
  local prev_sha prev_pinned_content
  prev_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  prev_pinned_content="$(cat "$yugo2_src/PINNED.md")"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit --allow-empty -m "deploy-target" -q 2>/dev/null
  local deploy_sha
  deploy_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" checkout "$prev_sha" -q 2>/dev/null  # set starting state

  cat > "${tmpdir}/compose.yml" << 'EOF'
services:
  bot:
    image: chis-bot:${YUGO_IMAGE_TAG:?}
    container_name: chis
EOF

  # Loud-fail bare git stub: catches any bypass of YUGO_GIT_CHIS
  cat > "${tmpdir}/git" << 'STUBEOF'
#!/usr/bin/env bash
echo "ERROR: bare git called directly — must use yugo-git-chis wrapper" >&2
exit 1
STUBEOF
  chmod +x "${tmpdir}/git"

  # yugo-git-chis wrapper stub: no-op fetch; real git for checkout and rev-parse
  cat > "${tmpdir}/yugo-git-chis" << STUBEOF
#!/usr/bin/env bash
SRC="${yugo2_src}"
case "\${1:-}" in
  fetch)     exit 0 ;;
  rev-parse) exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" rev-parse HEAD ;;
  checkout)  exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" checkout "\${2}" ;;
  *)         exit 1 ;;
esac
STUBEOF
  chmod +x "${tmpdir}/yugo-git-chis"

  # docker stub: first compose up fails (deploy triggers rollback); second succeeds (rollback)
  cat > "${tmpdir}/docker" << STUBEOF
#!/usr/bin/env bash
CALLS="${call_count}"
STATE="${state}"
PREV_ID="${prev_id}"
PREV_COMMIT_SHA="${prev_commit}"
if [[ "\${1:-}" == "compose" ]]; then
  for a in "\$@"; do
    [[ "\$a" == "up" ]] || continue
    count=\$(cat "\${CALLS}" 2>/dev/null || echo 0)
    echo \$((count+1)) > "\${CALLS}"
    if [[ \$((count+1)) -eq 1 ]]; then exit 1; fi
    touch "\${STATE}"
    exit 0
  done
  exit 0
fi
[[ "\${1:-}" == "tag" || "\${1:-}" == "rmi" ]] && exit 0
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    if [[ -f "\${STATE}" ]]; then
      if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
      if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${PREV_ID}"; exit 0; fi
      if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${PREV_COMMIT_SHA}"; exit 0; fi
      if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "chis"; exit 0; fi
    else
      if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${PREV_ID}"; exit 0; fi
      if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${PREV_COMMIT_SHA}"; exit 0; fi
      if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "chis"; exit 0; fi
      if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    fi
    echo ""; exit 0
  fi
done
exit 0
STUBEOF
  chmod +x "${tmpdir}/docker"

  local exit_code=0
  local output
  output="$(PATH="${tmpdir}:${PATH}" \
    GH_TOKEN="fake" \
    YUGO_COMMIT_OVERRIDE="$deploy_sha" \
    YUGO_BUILD_IMAGE_ID="$build_id" \
    YUGO_COMPOSE_CHIS="${tmpdir}/compose.yml" \
    YUGO_SUDO_CHIS="none" \
    YUGO_YUGO2_SRC="$yugo2_src" \
    YUGO_GIT_CHIS="${tmpdir}/yugo-git-chis" \
    bash "${SCRIPT_DIR}/scripts/deploy.sh" chis 2>&1)" || exit_code=$?

  local actual_head actual_pinned
  actual_head="$(git -C "$yugo2_src" rev-parse HEAD 2>/dev/null || true)"
  actual_pinned="$(cat "$yugo2_src/PINNED.md" 2>/dev/null || true)"

  if [[ $exit_code -ne 0 ]] \
      && [[ "$actual_head" == "$prev_sha" ]] \
      && [[ "$actual_pinned" == "$prev_pinned_content" ]] \
      && echo "$output" | grep -q "ROLLBACK COMPLETE"; then
    pass "$name"
  else
    fail "$name" "chis rollback source restore wrong: exit=${exit_code}, head=${actual_head:0:12} (want ${prev_sha:0:12}), pinned_match=$([[ "$actual_pinned" == "$prev_pinned_content" ]] && echo yes || echo no), rollback_complete=$(echo "$output" | grep -c "ROLLBACK COMPLETE" || echo 0)"
  fi
}

# ── deploy.sh: chis rollback must not claim success when restore fails ────────
# PINS: checkout exits nonzero → rollback fails with "manual intervention required".
#       Assertion is satisfied by either the immediate hard-fail or the read-back path;
#       the silent-checkout test above pins the read-back layer specifically.
# MUTATION: Change the git checkout || { ... exit 1 } back to || echo WARNING
# Expected colour: RED — "manual intervention required" absent from output

test_chis_rollback_fails_on_restore_error() {
  local name="test_chis_rollback_fails_on_restore_error"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local prev_commit="prevsha000000000000000000000000000000000"
  local prev_id="sha256:oldbuild0000000000000000000000000000000000000000000000000000000000"
  local build_id="sha256:newbuild1000000000000000000000000000000000000000000000000000000000"
  local state="${tmpdir}/container_state"
  local call_count="${tmpdir}/compose_call_count"
  local checkout_count="${tmpdir}/checkout_count"
  echo 0 > "$call_count"
  echo 0 > "$checkout_count"

  local yugo2_src="${tmpdir}/yugo2-src"
  mkdir -p "$yugo2_src"
  git -C "$yugo2_src" init -q 2>/dev/null || git -C "$yugo2_src" init 2>/dev/null
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit --allow-empty -m "pre-deploy" -q 2>/dev/null
  printf 'SHA: pre\nDate: 2026-01-01\n' > "$yugo2_src/PINNED.md"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T add PINNED.md 2>/dev/null
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit -m "add pinned" -q 2>/dev/null
  local prev_sha
  prev_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit --allow-empty -m "deploy-target" -q 2>/dev/null
  local deploy_sha
  deploy_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" checkout "$prev_sha" -q 2>/dev/null

  cat > "${tmpdir}/compose.yml" << 'EOF'
services:
  bot:
    image: chis-bot:${YUGO_IMAGE_TAG:?}
    container_name: chis
EOF

  # Loud-fail bare git stub: catches any bypass of YUGO_GIT_CHIS
  cat > "${tmpdir}/git" << 'STUBEOF'
#!/usr/bin/env bash
echo "ERROR: bare git called directly — must use yugo-git-chis wrapper" >&2
exit 1
STUBEOF
  chmod +x "${tmpdir}/git"

  # yugo-git-chis wrapper stub: no-op fetch; first checkout passes, second fails
  cat > "${tmpdir}/yugo-git-chis" << STUBEOF
#!/usr/bin/env bash
CKCOUNT="${checkout_count}"
SRC="${yugo2_src}"
case "\${1:-}" in
  fetch) exit 0 ;;
  rev-parse) exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" rev-parse HEAD ;;
  checkout)
    count=\$(cat "\${CKCOUNT}" 2>/dev/null || echo 0)
    echo \$((count+1)) > "\${CKCOUNT}"
    if [[ \$((count+1)) -ge 2 ]]; then exit 1; fi
    exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" checkout "\${2}"
    ;;
  *) exit 1 ;;
esac
STUBEOF
  chmod +x "${tmpdir}/yugo-git-chis"

  cat > "${tmpdir}/docker" << STUBEOF
#!/usr/bin/env bash
CALLS="${call_count}"
STATE="${state}"
PREV_ID="${prev_id}"
PREV_COMMIT_SHA="${prev_commit}"
if [[ "\${1:-}" == "compose" ]]; then
  for a in "\$@"; do
    [[ "\$a" == "up" ]] || continue
    count=\$(cat "\${CALLS}" 2>/dev/null || echo 0)
    echo \$((count+1)) > "\${CALLS}"
    if [[ \$((count+1)) -eq 1 ]]; then exit 1; fi
    touch "\${STATE}"
    exit 0
  done
  exit 0
fi
[[ "\${1:-}" == "tag" || "\${1:-}" == "rmi" ]] && exit 0
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    if [[ -f "\${STATE}" ]]; then
      if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
      if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${PREV_ID}"; exit 0; fi
      if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${PREV_COMMIT_SHA}"; exit 0; fi
      if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "chis"; exit 0; fi
    else
      if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${PREV_ID}"; exit 0; fi
      if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${PREV_COMMIT_SHA}"; exit 0; fi
      if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "chis"; exit 0; fi
      if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    fi
    echo ""; exit 0
  fi
done
exit 0
STUBEOF
  chmod +x "${tmpdir}/docker"

  local exit_code=0
  local output
  output="$(PATH="${tmpdir}:${PATH}" \
    GH_TOKEN="fake" \
    YUGO_COMMIT_OVERRIDE="$deploy_sha" \
    YUGO_BUILD_IMAGE_ID="$build_id" \
    YUGO_COMPOSE_CHIS="${tmpdir}/compose.yml" \
    YUGO_SUDO_CHIS="none" \
    YUGO_YUGO2_SRC="$yugo2_src" \
    YUGO_GIT_CHIS="${tmpdir}/yugo-git-chis" \
    bash "${SCRIPT_DIR}/scripts/deploy.sh" chis 2>&1)" || exit_code=$?

  if [[ $exit_code -ne 0 ]] && echo "$output" | grep -qi "manual intervention required"; then
    pass "$name"
  else
    fail "$name" "expected exit!=0 and 'manual intervention required' in output; got exit=${exit_code}, output=${output}"
  fi
}

# ── deploy.sh: chis rollback detects silent checkout (reads back actual state) ──
# PINS: read-back layer — catches a checkout that exits 0 but leaves the wrong HEAD
# (an explicit checkout failure is also caught by this test, but the test above pins that path)
# MUTATION: Delete only the "# Verify by reading back" block from rollback() in
#           scripts/deploy.sh — the restored_head/restored_pinned comparisons and
#           their exit-1 blocks, NOT the restore commands themselves.
# Expected colour: RED — checkout is a no-op stub (exits 0, HEAD stays at deploy
#                  SHA). Without the read-back, the script reaches ROLLBACK COMPLETE
#                  despite wrong HEAD, so exit_code becomes 0 and the assertion fails.

test_chis_rollback_detects_silent_checkout_failure() {
  local name="test_chis_rollback_detects_silent_checkout_failure"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local prev_commit="prevsha000000000000000000000000000000000"
  local prev_id="sha256:oldbuild0000000000000000000000000000000000000000000000000000000000"
  local build_id="sha256:newbuild1000000000000000000000000000000000000000000000000000000000"
  local state="${tmpdir}/container_state"
  local call_count="${tmpdir}/compose_call_count"
  echo 0 > "$call_count"

  # Real git repo with two commits; HEAD starts at prev_sha (pre-deploy state).
  local yugo2_src="${tmpdir}/yugo2-src"
  mkdir -p "$yugo2_src"
  git -C "$yugo2_src" init -q 2>/dev/null || git -C "$yugo2_src" init 2>/dev/null
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit --allow-empty -m "pre-deploy" -q 2>/dev/null
  printf 'SHA: pre\nDate: 2026-01-01\n' > "$yugo2_src/PINNED.md"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T add PINNED.md 2>/dev/null
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit -m "add pinned" -q 2>/dev/null
  local prev_sha
  prev_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit --allow-empty -m "deploy-target" -q 2>/dev/null
  local deploy_sha
  deploy_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" checkout "$prev_sha" -q 2>/dev/null  # set starting state

  cat > "${tmpdir}/compose.yml" << 'EOF'
services:
  bot:
    image: chis-bot:${YUGO_IMAGE_TAG:?}
    container_name: chis
EOF

  # Loud-fail bare git stub: catches any bypass of YUGO_GIT_CHIS
  cat > "${tmpdir}/git" << 'STUBEOF'
#!/usr/bin/env bash
echo "ERROR: bare git called directly — must use yugo-git-chis wrapper" >&2
exit 1
STUBEOF
  chmod +x "${tmpdir}/git"

  # yugo-git-chis wrapper stub: no-op fetch; rollback checkout (to prev_sha) is silent no-op.
  # Deploy checkout (to deploy_sha) passes through so HEAD actually moves.
  # The read-back finds deploy_sha != PREV_YUGO2_SHA and must exit 1.
  cat > "${tmpdir}/yugo-git-chis" << STUBEOF
#!/usr/bin/env bash
SRC="${yugo2_src}"
case "\${1:-}" in
  fetch) exit 0 ;;
  rev-parse) exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" rev-parse HEAD ;;
  checkout)
    if [[ "\${2:-}" == "${prev_sha}" ]]; then
      exit 0  # rollback checkout: exits 0 but does NOT move HEAD (silent failure)
    fi
    exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" checkout "\${2}"
    ;;
  *) exit 1 ;;
esac
STUBEOF
  chmod +x "${tmpdir}/yugo-git-chis"

  # docker stub: first compose up fails (triggers rollback); second succeeds.
  cat > "${tmpdir}/docker" << STUBEOF
#!/usr/bin/env bash
CALLS="${call_count}"
STATE="${state}"
PREV_ID="${prev_id}"
PREV_COMMIT_SHA="${prev_commit}"
if [[ "\${1:-}" == "compose" ]]; then
  for a in "\$@"; do
    [[ "\$a" == "up" ]] || continue
    count=\$(cat "\${CALLS}" 2>/dev/null || echo 0)
    echo \$((count+1)) > "\${CALLS}"
    if [[ \$((count+1)) -eq 1 ]]; then exit 1; fi
    touch "\${STATE}"
    exit 0
  done
  exit 0
fi
[[ "\${1:-}" == "tag" || "\${1:-}" == "rmi" ]] && exit 0
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    if [[ -f "\${STATE}" ]]; then
      if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
      if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${PREV_ID}"; exit 0; fi
      if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${PREV_COMMIT_SHA}"; exit 0; fi
      if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "chis"; exit 0; fi
    else
      if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${PREV_ID}"; exit 0; fi
      if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${PREV_COMMIT_SHA}"; exit 0; fi
      if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "chis"; exit 0; fi
      if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    fi
    echo ""; exit 0
  fi
done
exit 0
STUBEOF
  chmod +x "${tmpdir}/docker"

  local exit_code=0
  local output
  output="$(PATH="${tmpdir}:${PATH}" \
    GH_TOKEN="fake" \
    YUGO_COMMIT_OVERRIDE="$deploy_sha" \
    YUGO_BUILD_IMAGE_ID="$build_id" \
    YUGO_COMPOSE_CHIS="${tmpdir}/compose.yml" \
    YUGO_SUDO_CHIS="none" \
    YUGO_YUGO2_SRC="$yugo2_src" \
    YUGO_GIT_CHIS="${tmpdir}/yugo-git-chis" \
    bash "${SCRIPT_DIR}/scripts/deploy.sh" chis 2>&1)" || exit_code=$?

  # Checkout stub exits 0 but HEAD stays at deploy_sha.
  # The read-back catches the mismatch and must exit 1 with "manual intervention required".
  if [[ $exit_code -ne 0 ]] && echo "$output" | grep -qi "manual intervention required"; then
    pass "$name"
  else
    fail "$name" "expected exit!=0 and 'manual intervention required' in output (read-back should catch HEAD mismatch); got exit=${exit_code}, output=${output}"
  fi
}

# ── deploy.sh: deploy refuses when yugo2-src snapshot cannot be captured ─────
# MUTATION: Replace all three `exit 1` in the snapshot section of deploy.sh
#           (PREV_YUGO2_SHA capture, [[ -n ]] guard, and PINNED.md capture)
#           with `:`, leaving the echoes intact.
# Expected colour: RED — deploy proceeds to mutation phase; git checkout appears
#                  in call log and actual_head moves to deploy_sha.
# A string match on the error message would stay GREEN (echo is preserved);
# this test asserts STATE (actual_head, call logs) so it does not.

test_chis_deploy_refuses_when_snapshot_fails() {
  local name="test_chis_deploy_refuses_when_snapshot_fails"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local prev_commit="prevsha000000000000000000000000000000000"
  local prev_id="sha256:oldbuild0000000000000000000000000000000000000000000000000000000000"
  local build_id="sha256:newbuild1000000000000000000000000000000000000000000000000000000000"
  local git_log="${tmpdir}/git_calls"
  local compose_log="${tmpdir}/compose_calls"
  touch "$git_log" "$compose_log"

  # Real git repo: two commits so actual_head is meaningful evidence.
  # With the hard-fail intact: HEAD stays at prev_sha (deploy refused before checkout).
  # With the mutation (exit 1 → :): deploy proceeds, checkout moves HEAD to deploy_sha.
  local yugo2_src="${tmpdir}/yugo2-src"
  mkdir -p "$yugo2_src"
  git -C "$yugo2_src" init -q 2>/dev/null || git -C "$yugo2_src" init 2>/dev/null
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit --allow-empty -m "pre-deploy" -q 2>/dev/null
  printf 'SHA: pre\nDate: 2026-01-01\n' > "$yugo2_src/PINNED.md"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T add PINNED.md 2>/dev/null
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit -m "add pinned" -q 2>/dev/null
  local prev_sha
  prev_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit --allow-empty -m "deploy-target" -q 2>/dev/null
  local deploy_sha
  deploy_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" checkout "$prev_sha" -q 2>/dev/null  # HEAD at pre-deploy state

  cat > "${tmpdir}/compose.yml" << 'EOF'
services:
  bot:
    image: chis-bot:${YUGO_IMAGE_TAG:?}
    container_name: chis
EOF

  # Loud-fail bare git stub: catches any bypass of YUGO_GIT_CHIS
  cat > "${tmpdir}/git" << 'STUBEOF'
#!/usr/bin/env bash
echo "ERROR: bare git called directly — must use yugo-git-chis wrapper" >&2
exit 1
STUBEOF
  chmod +x "${tmpdir}/git"

  # yugo-git-chis wrapper stub: rev-parse fails (forces snapshot abort); no-op fetch; checkout logs + real git.
  cat > "${tmpdir}/yugo-git-chis" << STUBEOF
#!/usr/bin/env bash
echo "\$*" >> "${git_log}"
SRC="${yugo2_src}"
case "\${1:-}" in
  rev-parse) exit 1 ;;
  fetch) exit 0 ;;
  checkout) exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" checkout "\${2}" ;;
  *) exit 1 ;;
esac
STUBEOF
  chmod +x "${tmpdir}/yugo-git-chis"

  # docker stub: container exists, compose-managed; logs compose up calls.
  cat > "${tmpdir}/docker" << STUBEOF
#!/usr/bin/env bash
PREV_ID="${prev_id}"
PREV_COMMIT_SHA="${prev_commit}"
COMPOSE_LOG="${compose_log}"
if [[ "\${1:-}" == "compose" ]]; then
  for a in "\$@"; do
    if [[ "\$a" == "up" ]]; then echo "compose up" >> "\${COMPOSE_LOG}"; exit 0; fi
  done
  exit 0
fi
[[ "\${1:-}" == "tag" || "\${1:-}" == "rmi" ]] && exit 0
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${PREV_ID}"; exit 0; fi
    if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${PREV_COMMIT_SHA}"; exit 0; fi
    if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "chis"; exit 0; fi
    echo ""; exit 0
  fi
done
exit 0
STUBEOF
  chmod +x "${tmpdir}/docker"

  local exit_code=0
  local output
  output="$(PATH="${tmpdir}:${PATH}" \
    GH_TOKEN="fake" \
    YUGO_COMMIT_OVERRIDE="$deploy_sha" \
    YUGO_BUILD_IMAGE_ID="$build_id" \
    YUGO_COMPOSE_CHIS="${tmpdir}/compose.yml" \
    YUGO_SUDO_CHIS="none" \
    YUGO_YUGO2_SRC="$yugo2_src" \
    YUGO_GIT_CHIS="${tmpdir}/yugo-git-chis" \
    bash "${SCRIPT_DIR}/scripts/deploy.sh" chis 2>&1)" || exit_code=$?

  local actual_head
  actual_head="$(git -C "$yugo2_src" rev-parse HEAD 2>/dev/null || echo "no-git")"

  # Primary assertions — state, not exit code or message text:
  # 1. git HEAD must still be at prev_sha — no mutation occurred
  # 2. No git checkout in call log — deploy never reached the mutation phase
  # 3. No docker compose up logged — deploy never reached restart
  # Exit code is supporting evidence but not load-bearing: a script that swallows the
  # hard-fail and then crashes elsewhere still exits nonzero.
  if [[ $exit_code -ne 0 ]] \
      && [[ "$actual_head" == "$prev_sha" ]] \
      && ! grep -q "checkout" "${git_log}" 2>/dev/null \
      && ! grep -q "compose up" "${compose_log}" 2>/dev/null; then
    pass "$name"
  else
    fail "$name" "expected refuse before mutation: exit=${exit_code}, actual_head=${actual_head:0:12} (want ${prev_sha:0:12}), git_checkout=$(grep -c "checkout" "${git_log}" 2>/dev/null || echo 0), compose_up=$(grep -c "compose up" "${compose_log}" 2>/dev/null || echo 0)"
  fi
}

# ── deploy.sh: chis rollback fails when PINNED.md read-back errors ────────────
# PINS: the hard-fail on `restored_pinned=$(cat ...) || { exit 1 }` at the
#       read-back in rollback(). PINNED_CAPTURED=1 so the cat is reached.
#       cat succeeds on the first call (capture snapshot), fails on the second
#       call (read-back verify), which is the unsafe case: an empty captured
#       snapshot would have matched a failed read's empty result without this guard.
# MUTATION: Change `|| { echo "ERROR: ... manual intervention required" >&2; exit 1 }`
#           at the restored_pinned assignment back to `|| true`
# Expected colour: RED — cat failure is swallowed; empty matches empty captured
#                  snapshot; ROLLBACK COMPLETE fires despite unreadable file.

test_chis_rollback_fails_when_pinned_readback_fails() {
  local name="test_chis_rollback_fails_when_pinned_readback_fails"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local prev_commit="prevsha000000000000000000000000000000000"
  local prev_id="sha256:oldbuild0000000000000000000000000000000000000000000000000000000000"
  local build_id="sha256:newbuild1000000000000000000000000000000000000000000000000000000000"
  local state="${tmpdir}/container_state"
  local call_count="${tmpdir}/compose_call_count"
  local cat_pinned_count="${tmpdir}/cat_pinned_count"
  echo 0 > "$call_count"
  echo 0 > "$cat_pinned_count"

  # Real git repo: two commits so deploy can advance HEAD; rollback can request it back.
  local yugo2_src="${tmpdir}/yugo2-src"
  mkdir -p "$yugo2_src"
  git -C "$yugo2_src" init -q 2>/dev/null || git -C "$yugo2_src" init 2>/dev/null
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit --allow-empty -m "pre-deploy" -q 2>/dev/null
  # Empty PINNED.md — worst case: failed read-back yields "" == "" without the guard
  > "$yugo2_src/PINNED.md"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T add PINNED.md 2>/dev/null
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit -m "add pinned" -q 2>/dev/null
  local prev_sha
  prev_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=T \
    commit --allow-empty -m "deploy-target" -q 2>/dev/null
  local deploy_sha
  deploy_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" checkout "$prev_sha" -q 2>/dev/null  # set starting state

  cat > "${tmpdir}/compose.yml" << 'EOF'
services:
  bot:
    image: chis-bot:${YUGO_IMAGE_TAG:?}
    container_name: chis
EOF

  # cat stub: intercepts reads of PINNED.md; counts them.
  # Call 1 (capture snapshot): succeeds, prints empty.
  # Call 2 (read-back verify): exits 1 — simulates unreadable file after restore.
  # All other cat calls pass through to /bin/cat.
  cat > "${tmpdir}/cat" << STUBEOF
#!/usr/bin/env bash
COUNT_FILE="${cat_pinned_count}"
PINNED_PATH="${yugo2_src}/PINNED.md"
for a in "\$@"; do
  if [[ "\$a" == "\${PINNED_PATH}" ]]; then
    count=\$(cat "\${COUNT_FILE}" 2>/dev/null || echo 0)
    echo \$((count+1)) > "\${COUNT_FILE}"
    if [[ \$((count+1)) -ge 2 ]]; then
      echo "cat: \${PINNED_PATH}: simulated read failure" >&2
      exit 1
    fi
    exit 0  # first call: empty PINNED.md, exits 0
  fi
done
exec /bin/cat "\$@"
STUBEOF
  chmod +x "${tmpdir}/cat"

  # Loud-fail bare git stub: catches any bypass of YUGO_GIT_CHIS
  cat > "${tmpdir}/git" << 'STUBEOF'
#!/usr/bin/env bash
echo "ERROR: bare git called directly — must use yugo-git-chis wrapper" >&2
exit 1
STUBEOF
  chmod +x "${tmpdir}/git"

  # yugo-git-chis wrapper stub: no-op fetch; real git for checkout and rev-parse
  cat > "${tmpdir}/yugo-git-chis" << STUBEOF
#!/usr/bin/env bash
SRC="${yugo2_src}"
case "\${1:-}" in
  fetch)     exit 0 ;;
  rev-parse) exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" rev-parse HEAD ;;
  checkout)  exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" checkout "\${2}" ;;
  *)         exit 1 ;;
esac
STUBEOF
  chmod +x "${tmpdir}/yugo-git-chis"

  # docker stub: first compose up fails (triggers rollback); second succeeds (rollback image restore)
  cat > "${tmpdir}/docker" << STUBEOF
#!/usr/bin/env bash
CALLS="${call_count}"
STATE="${state}"
PREV_ID="${prev_id}"
PREV_COMMIT_SHA="${prev_commit}"
if [[ "\${1:-}" == "compose" ]]; then
  for a in "\$@"; do
    [[ "\$a" == "up" ]] || continue
    count=\$(cat "\${CALLS}" 2>/dev/null || echo 0)
    echo \$((count+1)) > "\${CALLS}"
    if [[ \$((count+1)) -eq 1 ]]; then exit 1; fi
    touch "\${STATE}"
    exit 0
  done
  exit 0
fi
[[ "\${1:-}" == "tag" || "\${1:-}" == "rmi" ]] && exit 0
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    if [[ -f "\${STATE}" ]]; then
      if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
      if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${PREV_ID}"; exit 0; fi
      if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${PREV_COMMIT_SHA}"; exit 0; fi
      if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "chis"; exit 0; fi
    else
      if [[ "\$fmt" == *".Image"* && "\$fmt" != *"Labels"* ]]; then echo "\${PREV_ID}"; exit 0; fi
      if [[ "\$fmt" == *"codex.commit"* ]]; then echo "\${PREV_COMMIT_SHA}"; exit 0; fi
      if [[ "\$fmt" == *"com.docker.compose.project"* ]]; then echo "chis"; exit 0; fi
      if [[ "\$fmt" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    fi
    echo ""; exit 0
  fi
done
exit 0
STUBEOF
  chmod +x "${tmpdir}/docker"

  local exit_code=0
  local output
  output="$(PATH="${tmpdir}:${PATH}" \
    GH_TOKEN="fake" \
    YUGO_COMMIT_OVERRIDE="$deploy_sha" \
    YUGO_BUILD_IMAGE_ID="$build_id" \
    YUGO_COMPOSE_CHIS="${tmpdir}/compose.yml" \
    YUGO_SUDO_CHIS="none" \
    YUGO_YUGO2_SRC="$yugo2_src" \
    YUGO_GIT_CHIS="${tmpdir}/yugo-git-chis" \
    bash "${SCRIPT_DIR}/scripts/deploy.sh" chis 2>&1)" || exit_code=$?

  # PINNED.md read-back fails → must exit nonzero with "manual intervention required".
  # ROLLBACK COMPLETE must be absent — announcing success when the file is unreadable is wrong.
  if [[ $exit_code -ne 0 ]] \
      && echo "$output" | grep -qi "manual intervention required" \
      && ! echo "$output" | grep -q "ROLLBACK COMPLETE"; then
    pass "$name"
  else
    fail "$name" "expected exit!=0, 'manual intervention required', no 'ROLLBACK COMPLETE'; got exit=${exit_code}, rollback_complete=$(echo "$output" | grep -c "ROLLBACK COMPLETE" || echo 0), output=${output}"
  fi
}

# ── deploy.sh: docker inspect daemon error refuses before mutating ─────────────
# MUTATION: Restore `|| true` on the PREV_IMAGE_ID capture (replace the
#           IMAGE_CAPTURED block with the original single-line capture).
# Expected colour: RED — daemon error is swallowed, IMAGE_CAPTURED stays unset,
#                  `set -u` fires on the first-cutover guard with an unbound-variable
#                  error rather than the specific "docker inspect ... failed" message
#                  this test requires. The error-message assertion is the discriminator.
#
# Docker stub: the initial inspect (--format {{.Image}}) exits 1 with a daemon error
# that does NOT contain "no such object". All other calls succeed.
# Assertions:
#   1. exit_code != 0
#   2. output contains "docker inspect.*failed" — the specific guard message
#   3. no compose up in call log

test_chis_deploy_refuses_on_docker_inspect_failure() {
  # DISCRIMINATING SETUP: real git repo with PINNED.md so snapshot succeeds.
  # Without a viable source, a neutered inspection-error guard would still be
  # stopped by the snapshot guard — making the test credit the wrong mechanism.
  # With a real repo, the only thing that can stop the deploy is the inspection-error exit.
  # The discriminating assertion is: compose up was never called.
  local name="test_chis_deploy_refuses_on_docker_inspect_failure"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local build_id="sha256:chisbuild00000000000000000000000000000000000000000000000000000000"
  local compose_log="${tmpdir}/compose_calls"
  local yugo2_src="${tmpdir}/yugo2-src"
  touch "$compose_log"

  # Real git repo: two commits — HEAD at prev_sha, deploy_sha one ahead
  mkdir -p "$yugo2_src"
  git -C "$yugo2_src" init -q
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=t commit --allow-empty -m "a"
  local prev_sha; prev_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  printf 'SHA: %s\n' "$prev_sha" > "$yugo2_src/PINNED.md"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=t add PINNED.md
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=t commit -m "pin"
  prev_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=t commit --allow-empty -m "b"
  local deploy_sha; deploy_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" checkout -q "$prev_sha"

  cat > "${tmpdir}/compose.yml" << 'EOF'
services:
  bot:
    image: chis-bot:${YUGO_IMAGE_TAG:-latest}
    container_name: chis
EOF

  # Docker stub: ALL inspects fail with daemon error (not "no such object").
  # compose and tag/rmi calls succeed.
  cat > "${tmpdir}/docker" << STUBEOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "compose" ]]; then
  for a in "\$@"; do
    if [[ "\$a" == "up" ]]; then echo "compose up" >> "${compose_log}"; exit 0; fi
  done
  exit 0
fi
[[ "\${1:-}" == "tag" || "\${1:-}" == "rmi" ]] && exit 0
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    echo "Error: docker daemon timeout" >&2
    exit 1
  fi
done
exit 0
STUBEOF
  chmod +x "${tmpdir}/docker"

  # Loud-fail bare git stub: catches any bypass of YUGO_GIT_CHIS
  cat > "${tmpdir}/git" << 'STUBEOF'
#!/usr/bin/env bash
echo "ERROR: bare git called directly — must use yugo-git-chis wrapper" >&2
exit 1
STUBEOF
  chmod +x "${tmpdir}/git"

  # yugo-git-chis wrapper stub: no-op fetch; real git for checkout and rev-parse
  cat > "${tmpdir}/yugo-git-chis" << STUBEOF
#!/usr/bin/env bash
SRC="${yugo2_src}"
case "\${1:-}" in
  fetch)     exit 0 ;;
  rev-parse) exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" rev-parse HEAD ;;
  checkout)  exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" checkout "\${2}" ;;
  *)         exit 1 ;;
esac
STUBEOF
  chmod +x "${tmpdir}/yugo-git-chis"

  local exit_code=0
  local output
  output="$(PATH="${tmpdir}:${PATH}" \
    GH_TOKEN="fake" \
    YUGO_COMMIT_OVERRIDE="$deploy_sha" \
    YUGO_BUILD_IMAGE_ID="$build_id" \
    YUGO_COMPOSE_CHIS="${tmpdir}/compose.yml" \
    YUGO_SUDO_CHIS="none" \
    YUGO_YUGO2_SRC="$yugo2_src" \
    YUGO_GIT_CHIS="${tmpdir}/yugo-git-chis" \
    bash "${SCRIPT_DIR}/scripts/deploy.sh" chis 2>&1)" || exit_code=$?

  local compose_reached=0
  grep -q "compose up" "${compose_log}" 2>/dev/null && compose_reached=1 || true
  local actual_head; actual_head="$(git -C "$yugo2_src" rev-parse HEAD 2>/dev/null || echo "no-git")"

  # With the guard intact: deploy refuses at inspection-error → no compose up, HEAD unmoved.
  # With the mutation (exit 1 → :): snapshot succeeds, compose up runs → compose_reached=1 (RED).
  if [[ $exit_code -ne 0 ]] \
      && echo "$output" | grep -q "docker inspect.*failed" \
      && [[ $compose_reached -eq 0 ]] \
      && [[ "$actual_head" == "$prev_sha" ]]; then
    pass "$name"
  else
    fail "$name" "expected exit!=0, 'docker inspect.*failed', no compose, HEAD at prev; got exit=${exit_code}, compose=${compose_reached}, head=${actual_head:0:12} (want ${prev_sha:0:12})"
  fi
}

# MUTATION CONTROL for test_chis_deploy_refuses_on_docker_inspect_failure:
# Stub emits docker's real lowercase "error: no such object: chis" text →
# deploy must treat this as absent-container (not daemon-error) and proceed.
# Expected colour: GREEN (first deploy allowed); mutant (grep without -i): RED.
#
# Stub is stateful: pre-compose-up all inspects simulate absent container
# (Image exits 1 "no such object", others return ""). Post-compose-up the
# container is running at the deployed commit. This prevents the idempotency
# check (PREV_COMMIT == COMMIT) from short-circuiting before compose up runs.
test_chis_first_deploy_allowed_on_no_such_object() {
  local name="test_chis_first_deploy_allowed_on_no_such_object"
  local tmpdir
  tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN

  local build_id="sha256:chisfirst00000000000000000000000000000000000000000000000000000000"
  local compose_log="${tmpdir}/compose_calls"
  local yugo2_src="${tmpdir}/yugo2-src"
  touch "$compose_log"

  # Real git repo: two commits — HEAD at prev_sha, deploy_sha one ahead
  mkdir -p "$yugo2_src"
  git -C "$yugo2_src" init -q
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=t commit --allow-empty -m "a"
  local prev_sha; prev_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  printf 'SHA: %s\n' "$prev_sha" > "$yugo2_src/PINNED.md"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=t add PINNED.md
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=t commit -m "pin"
  prev_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" -c user.email=t@t.com -c user.name=t commit --allow-empty -m "b"
  local deploy_sha; deploy_sha="$(git -C "$yugo2_src" rev-parse HEAD)"
  git -C "$yugo2_src" checkout -q "$prev_sha"
  # commit = deploy_sha: the actual SHA that deploy.sh will checkout in yugo2-src
  local commit="$deploy_sha"

  cat > "${tmpdir}/compose.yml" << 'EOF'
services:
  bot:
    image: chis-bot:${YUGO_IMAGE_TAG:?}
    container_name: chis
EOF

  # Stateful docker stub: pre-compose-up, no container exists.
  # After compose up writes to compose_log, container is running at commit.
  cat > "${tmpdir}/docker" << STUBEOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "compose" ]]; then
  for a in "\$@"; do
    if [[ "\$a" == "up" ]]; then echo "compose up" >> "${compose_log}"; fi
  done
  exit 0
fi
[[ "\${1:-}" == "tag" || "\${1:-}" == "rmi" ]] && exit 0
args=("\$@")
for i in "\${!args[@]}"; do
  if [[ "\${args[\$i]}" == "--format" ]]; then
    fmt="\${args[\$((\$i+1))]}"
    # Pre-deploy: no container. Post-deploy (compose up ran): container live.
    if grep -q "compose up" "${compose_log}" 2>/dev/null; then
      case "\$fmt" in
        *".Image"*)   echo "${build_id}" ;;
        *"State.Running"*) echo "true" ;;
        *"codex.commit"*) echo "${commit}" ;;
        *) echo "" ;;
      esac
    else
      case "\$fmt" in
        *".Image"*)   echo "error: no such object: chis" >&2; exit 1 ;;
        *) echo "" ;;
      esac
    fi
    exit 0
  fi
done
exit 0
STUBEOF
  chmod +x "${tmpdir}/docker"

  # Loud-fail bare git stub: catches any bypass of YUGO_GIT_CHIS
  cat > "${tmpdir}/git" << 'STUBEOF'
#!/usr/bin/env bash
echo "ERROR: bare git called directly — must use yugo-git-chis wrapper" >&2
exit 1
STUBEOF
  chmod +x "${tmpdir}/git"

  # yugo-git-chis wrapper stub: no-op fetch; real git for checkout and rev-parse
  cat > "${tmpdir}/yugo-git-chis" << STUBEOF
#!/usr/bin/env bash
SRC="${yugo2_src}"
case "\${1:-}" in
  fetch)     exit 0 ;;
  rev-parse) exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" rev-parse HEAD ;;
  checkout)  exec /usr/bin/git -c "safe.directory=\${SRC}" -C "\${SRC}" checkout "\${2}" ;;
  *)         exit 1 ;;
esac
STUBEOF
  chmod +x "${tmpdir}/yugo-git-chis"

  local exit_code=0
  PATH="${tmpdir}:${PATH}" \
    GH_TOKEN="fake" \
    YUGO_COMMIT_OVERRIDE="$commit" \
    YUGO_BUILD_IMAGE_ID="$build_id" \
    YUGO_COMPOSE_CHIS="${tmpdir}/compose.yml" \
    YUGO_SUDO_CHIS="none" \
    YUGO_YUGO2_SRC="$yugo2_src" \
    YUGO_GIT_CHIS="${tmpdir}/yugo-git-chis" \
    bash "${SCRIPT_DIR}/scripts/deploy.sh" chis 2>/dev/null || exit_code=$?

  local compose_reached=0
  grep -q "compose up" "${compose_log}" 2>/dev/null && compose_reached=1 || true
  if [[ $exit_code -eq 0 && $compose_reached -eq 1 ]]; then
    pass "$name"
  else
    fail "$name" "expected exit=0 and compose up reached; got exit=${exit_code}, compose_reached=${compose_reached}"
  fi
}

# Run all tests
test_preflight_rejects_build_form
test_preflight_rejects_any_build_form
test_preflight_passes_image_form
test_preflight_rejects_missing_file
test_deploy_fails_without_gh_token
test_deploy_fails_on_unknown_bot
test_deploy_refuses_unmanaged_container
test_drift_check_fails_on_stale_label
test_drift_check_fails_on_stopped_container
test_drift_check_fails_on_yugo2src_mismatch
test_drift_check_passes_when_all_match
test_workflow_uses_automatic_gh_token
test_workflow_has_actions_write_permission
test_workflow_handoff_always_runs
test_handoff_fails_when_dispatch_fails
test_handoff_no_dispatch_when_head_unchanged
test_compose_up_is_invoked
test_compose_failure_triggers_rollback
test_chis_deploy_updates_yugo2_src
test_drift_check_fails_on_git_head_mismatch
test_chis_rollback_restores_source
test_chis_rollback_fails_on_restore_error
test_chis_rollback_detects_silent_checkout_failure
test_chis_deploy_refuses_when_snapshot_fails
test_chis_rollback_fails_when_pinned_readback_fails
test_chis_deploy_refuses_on_docker_inspect_failure
test_chis_first_deploy_allowed_on_no_such_object

# ── yugo-git-chis: wrapper argument-validation controls ───────────────────────
# Each test calls the wrapper directly and asserts it exits nonzero with "denied"
# in stderr. These pin the validation layer independently of deploy.sh integration.

test_yugo_git_chis_rejects_non_hex_sha() {
  local name="test_yugo_git_chis_rejects_non_hex_sha"
  local tmpdir; tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN
  mkdir -p "${tmpdir}/src"
  local output exit_code=0
  output="$(YUGO2_SRC_DIR="${tmpdir}/src" \
    bash "${SCRIPT_DIR}/scripts/yugo-git-chis" checkout "not-a-hex-sha" 2>&1)" || exit_code=$?
  if [[ $exit_code -ne 0 ]] && echo "$output" | grep -q "denied"; then
    pass "$name"
  else
    fail "$name" "expected exit!=0 and 'denied'; got exit=${exit_code}, output=${output}"
  fi
}

test_yugo_git_chis_rejects_extra_checkout_arg() {
  local name="test_yugo_git_chis_rejects_extra_checkout_arg"
  local tmpdir; tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN
  mkdir -p "${tmpdir}/src"
  local output exit_code=0
  output="$(YUGO2_SRC_DIR="${tmpdir}/src" \
    bash "${SCRIPT_DIR}/scripts/yugo-git-chis" checkout "abc1234" "extra-arg" 2>&1)" || exit_code=$?
  if [[ $exit_code -ne 0 ]] && echo "$output" | grep -q "denied"; then
    pass "$name"
  else
    fail "$name" "expected exit!=0 and 'denied'; got exit=${exit_code}, output=${output}"
  fi
}

test_yugo_git_chis_rejects_rev_parse_with_arg() {
  local name="test_yugo_git_chis_rejects_rev_parse_with_arg"
  local tmpdir; tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN
  mkdir -p "${tmpdir}/src"
  local output exit_code=0
  output="$(YUGO2_SRC_DIR="${tmpdir}/src" \
    bash "${SCRIPT_DIR}/scripts/yugo-git-chis" rev-parse HEAD 2>&1)" || exit_code=$?
  if [[ $exit_code -ne 0 ]] && echo "$output" | grep -q "denied"; then
    pass "$name"
  else
    fail "$name" "expected exit!=0 and 'denied'; got exit=${exit_code}, output=${output}"
  fi
}

test_yugo_git_chis_rejects_fetch_with_arg() {
  local name="test_yugo_git_chis_rejects_fetch_with_arg"
  local tmpdir; tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN
  mkdir -p "${tmpdir}/src"
  local output exit_code=0
  output="$(YUGO2_SRC_DIR="${tmpdir}/src" \
    bash "${SCRIPT_DIR}/scripts/yugo-git-chis" fetch origin 2>&1)" || exit_code=$?
  if [[ $exit_code -ne 0 ]] && echo "$output" | grep -q "denied"; then
    pass "$name"
  else
    fail "$name" "expected exit!=0 and 'denied'; got exit=${exit_code}, output=${output}"
  fi
}

test_yugo_git_chis_rejects_unknown_subcommand() {
  local name="test_yugo_git_chis_rejects_unknown_subcommand"
  local tmpdir; tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN
  mkdir -p "${tmpdir}/src"
  local output exit_code=0
  output="$(YUGO2_SRC_DIR="${tmpdir}/src" \
    bash "${SCRIPT_DIR}/scripts/yugo-git-chis" push 2>&1)" || exit_code=$?
  if [[ $exit_code -ne 0 ]] && echo "$output" | grep -q "denied"; then
    pass "$name"
  else
    fail "$name" "expected exit!=0 and 'denied'; got exit=${exit_code}, output=${output}"
  fi
}

test_yugo_git_chis_rejects_non_hex_sha
test_yugo_git_chis_rejects_extra_checkout_arg
test_yugo_git_chis_rejects_rev_parse_with_arg
test_yugo_git_chis_rejects_fetch_with_arg
test_yugo_git_chis_rejects_unknown_subcommand

# ── scripts/yugo-git-chis: passing-path coverage ─────────────────────────────
# These tests invoke the SHIPPED wrapper against a real git repository.
# MUTATION CONTROL: replace scripts/yugo-git-chis with a deny-everything script
# and all three must go RED — proving the real wrapper is under test, not a stub.

test_yugo_git_chis_rev_parse_returns_head() {
  local name="test_yugo_git_chis_rev_parse_returns_head"
  local tmpdir; tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN
  local src="${tmpdir}/src"
  git -C "$tmpdir" init -q src
  git -C "$src" -c user.email=t@t.com -c user.name=t commit --allow-empty -m "init"
  local expected_sha; expected_sha="$(git -C "$src" rev-parse HEAD)"
  local output exit_code=0
  output="$(YUGO2_SRC_DIR="$src" bash "${SCRIPT_DIR}/scripts/yugo-git-chis" rev-parse 2>&1)" || exit_code=$?
  if [[ $exit_code -eq 0 && "$output" == "$expected_sha" ]]; then
    pass "$name"
  else
    fail "$name" "expected exit=0 and SHA ${expected_sha:0:12}; got exit=${exit_code}, output=${output}"
  fi
}

test_yugo_git_chis_checkout_moves_head() {
  local name="test_yugo_git_chis_checkout_moves_head"
  local tmpdir; tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN
  local src="${tmpdir}/src"
  git -C "$tmpdir" init -q src
  git -C "$src" -c user.email=t@t.com -c user.name=t commit --allow-empty -m "first"
  local first_sha; first_sha="$(git -C "$src" rev-parse HEAD)"
  git -C "$src" -c user.email=t@t.com -c user.name=t commit --allow-empty -m "second"
  local second_sha; second_sha="$(git -C "$src" rev-parse HEAD)"
  git -C "$src" checkout -q "$first_sha"
  local exit_code=0
  YUGO2_SRC_DIR="$src" bash "${SCRIPT_DIR}/scripts/yugo-git-chis" checkout "$second_sha" 2>/dev/null || exit_code=$?
  local actual_head; actual_head="$(git -C "$src" rev-parse HEAD)"
  if [[ $exit_code -eq 0 && "$actual_head" == "$second_sha" ]]; then
    pass "$name"
  else
    fail "$name" "expected exit=0 and HEAD at ${second_sha:0:12}; got exit=${exit_code}, HEAD=${actual_head:0:12}"
  fi
}

test_yugo_git_chis_fetch_succeeds() {
  local name="test_yugo_git_chis_fetch_succeeds"
  local tmpdir; tmpdir="$(mktemp -d)"
  trap "rm -rf '$tmpdir'" RETURN
  # Bare local origin so fetch has a reachable remote
  git -C "$tmpdir" init -q --bare origin.git
  local src="${tmpdir}/src"
  git clone -q "${tmpdir}/origin.git" "$src"
  git -C "$src" -c user.email=t@t.com -c user.name=t commit --allow-empty -m "init"
  git -C "$src" push -q origin HEAD:main
  local exit_code=0
  YUGO2_SRC_DIR="$src" bash "${SCRIPT_DIR}/scripts/yugo-git-chis" fetch 2>/dev/null || exit_code=$?
  if [[ $exit_code -eq 0 ]]; then
    pass "$name"
  else
    fail "$name" "expected exit=0; got exit=${exit_code}"
  fi
}

test_yugo_git_chis_rev_parse_returns_head
test_yugo_git_chis_checkout_moves_head
test_yugo_git_chis_fetch_succeeds

echo ""
echo "Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
[[ $FAIL_COUNT -eq 0 ]] || exit 1

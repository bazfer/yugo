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
    fi
    echo ""; exit 0
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
# Mutation: make rollback() a no-op → test goes RED (no ROLLBACK in output).

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

  if [[ $exit_code -ne 0 ]] && echo "$output" | grep -qi "ROLLBACK"; then
    pass "$name"
  else
    fail "$name" "expected non-zero exit with ROLLBACK in output; got exit=${exit_code}, output=${output}"
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

  cat > "${tmpdir}/compose.yml" << 'EOF'
services:
  bot:
    image: chis-bot:${YUGO_IMAGE_TAG:-latest}
    container_name: chis
EOF

  # git stub: logs calls; returns stable HEAD for rev-parse
  cat > "${tmpdir}/git" << STUBEOF
#!/usr/bin/env bash
echo "\$*" >> "${git_log}"
for a in "\$@"; do
  if [[ "\$a" == "rev-parse" ]]; then echo "prevsha0000000000000000000000000000000000"; exit 0; fi
done
exit 0
STUBEOF
  chmod +x "${tmpdir}/git"

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
    fi
    echo ""; exit 0
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

echo ""
echo "Results: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
[[ $FAIL_COUNT -eq 0 ]] || exit 1

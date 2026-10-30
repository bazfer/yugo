#!/usr/bin/env bash
# Tests for scripts/assert_pr_runner_isolation.py.
#
# The check's whole value is its REJECTION. Watching it pass against the
# repository's current workflows proves nothing, so every case below builds a
# synthetic workflow directory and asserts on the verdict. Each case names the
# mutation that would make it fail.
#
# Run:  bash test/test_pr_runner_isolation.sh
# CI:   invoked by .github/workflows/test.yml

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CHECK_SRC="${SCRIPT_DIR}/scripts/assert_pr_runner_isolation.py"
PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "PASS  $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "FAIL  $1: $2" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT

# new_repo <name> — lays out a fake repo whose scripts/ holds the real check,
# so the check resolves .github/workflows relative to the fake root.
new_repo() {
  local dir="${ROOT}/$1"
  mkdir -p "${dir}/.github/workflows" "${dir}/scripts"
  cp "$CHECK_SRC" "${dir}/scripts/"
  echo "$dir"
}

run_in() {  # run_in <repo_dir>
  ( cd "$1" && python3 -I scripts/assert_pr_runner_isolation.py >"${1}/out" 2>"${1}/err" )
}

echo "── assert_pr_runner_isolation.py ─────────────────────────────────────────"

# ── 1. A hosted PR job passes ─────────────────────────────────────────────────
d="$(new_repo hosted)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on:
  pull_request:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? == 0 )); then
  pass "a pull_request job on ubuntu-latest passes"
else
  fail "hosted job" "refused a hosted runner: $(cat "${d}/err")"
fi

# ── 2. self-hosted on a pull_request job is refused ───────────────────────────
# This is the regression the check exists to catch: #42 moved this job off
# norstar and recorded the reason in a comment only.
# Mutation that breaks this: removing 'self-hosted' from PRIVATE_LABELS.
d="$(new_repo selfhosted)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on:
  pull_request:
jobs:
  test:
    runs-on: [self-hosted, norstar]
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? != 0 )) && grep -q "ISOLATION BROKEN" "${d}/err"; then
  pass "a pull_request job on [self-hosted, norstar] is refused"
else
  fail "self-hosted PR job" "accepted; untrusted code could reach norstar"
fi

# ── 3. A job-level if: guard does NOT rescue it ───────────────────────────────
# The guard is exactly what the DeetGates review of PR #41 rejected: the PR's
# own merge commit supplies the workflow, so the PR can delete the guard.
# Mutation that breaks this: skipping jobs that carry an `if:`.
d="$(new_repo guarded)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on:
  pull_request:
jobs:
  test:
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: [self-hosted, norstar]
    steps: [{run: "true"}]
YML
run_in "$d"
rc=$?
if (( rc != 0 )) && ! grep -q "ISOLATION BROKEN" "${d}/err"; then
  fail "guarded self-hosted job" "exited non-zero for the wrong reason: $(head -1 "${d}/err")"
elif (( rc != 0 )); then
  pass "a branch-owner if: guard does not make a self-hosted PR job acceptable"
else
  fail "guarded self-hosted job" "accepted because of an if: guard that a PR can delete"
fi

# ── 4. A bare private machine label, without 'self-hosted', is refused ────────
# runs-on: norstar is enough to select our runner. A check that only looks for
# the literal 'self-hosted' misses it.
# Mutation that breaks this: narrowing PRIVATE_LABELS to {'self-hosted'}.
d="$(new_repo barelabel)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on:
  pull_request:
jobs:
  test:
    runs-on: norstar
    steps: [{run: "true"}]
YML
run_in "$d"
rc=$?
if (( rc != 0 )) && ! grep -q "ISOLATION BROKEN" "${d}/err"; then
  fail "bare label" "exited non-zero for the wrong reason: $(head -1 "${d}/err")"
elif (( rc != 0 )); then
  pass "a bare private machine label is refused"
else
  fail "bare label" "accepted runs-on: norstar on a pull_request job"
fi

# ── 5. The group/labels mapping form is inspected ─────────────────────────────
# runs-on accepts {group:, labels:}. A check that only handles strings and
# lists reads this as empty and passes.
# Mutation that breaks this: dropping the dict branch of runner_labels().
d="$(new_repo groupform)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on:
  pull_request:
jobs:
  test:
    runs-on:
      group: norstar
      labels: [linux]
    steps: [{run: "true"}]
YML
run_in "$d"
rc=$?
if (( rc != 0 )) && ! grep -q "ISOLATION BROKEN" "${d}/err"; then
  fail "group form" "exited non-zero for the wrong reason: $(head -1 "${d}/err")"
elif (( rc != 0 )); then
  pass "the group/labels mapping form is inspected, not skipped"
else
  fail "group form" "accepted a private runner group"
fi

# ── 6. pull_request_target counts too ─────────────────────────────────────────
# pull_request_target is the MORE dangerous trigger: it runs with repository
# secrets available.
# Mutation that breaks this: dropping pull_request_target from PR_TRIGGERS.
d="$(new_repo prtarget)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on:
  pull_request_target:
jobs:
  test:
    runs-on: [self-hosted, norstar]
    steps: [{run: "true"}]
YML
run_in "$d"
rc=$?
# Assert on the REASON, not just the exit code. Dropping pull_request_target
# from PR_TRIGGERS also exits non-zero — via the "inspected zero workflows"
# guard — so an exit-code-only assertion passes while the behaviour is gone.
if (( rc != 0 )) && grep -q "ISOLATION BROKEN" "${d}/err"; then
  pass "pull_request_target is treated as a pull-request trigger"
elif (( rc != 0 )); then
  fail "pull_request_target" "exited non-zero for the wrong reason: $(head -1 "${d}/err")"
else
  fail "pull_request_target" "accepted a self-hosted job on pull_request_target"
fi

# ── 7. A push-only self-hosted workflow is allowed ────────────────────────────
# deploy.yml legitimately runs on norstar. A check that refused it would be
# wrong and would be switched off.
# Mutation that breaks this: checking every workflow regardless of trigger.
d="$(new_repo pushonly)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps: [{run: "true"}]
YML
cat > "${d}/.github/workflows/deploy.yml" <<'YML'
name: deploy
on:
  push:
    branches: [main]
  workflow_dispatch: {}
jobs:
  deploy:
    runs-on: [self-hosted, norstar]
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? == 0 )); then
  pass "a push-only self-hosted workflow is left alone"
else
  fail "push-only workflow" "refused deploy.yml, which carries no untrusted code: $(cat "${d}/err")"
fi

# ── 8. No pull-request workflow at all is an ERROR, not a pass ────────────────
# This is the vacuous-success case. If the triggers change, or the check looks
# in the wrong directory, it must refuse rather than report that everything is
# fine.
# Mutation that breaks this: returning 0 when checked == 0.
d="$(new_repo nopr)"
cat > "${d}/.github/workflows/deploy.yml" <<'YML'
name: deploy
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: [self-hosted, norstar]
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? != 0 )); then
  pass "no pull-request workflow is an error, not a vacuous pass"
else
  fail "vacuous pass" "reported success while inspecting zero pull-request workflows"
fi

# ── 9. An empty workflow directory is an ERROR ────────────────────────────────
# Mutation that breaks this: returning 0 on an empty file list.
d="$(new_repo emptydir)"
run_in "$d"
rc=$?
# Ohm: an exit-code-only assertion here passes even with the empty-directory
# guard removed, because the zero-inspected guard refuses the same case for a
# different reason. Assert on which guard fired.
if (( rc != 0 )) && grep -q "no workflows found" "${d}/err"; then
  pass "an empty workflow directory is refused by the empty-directory guard"
elif (( rc != 0 )); then
  fail "empty dir" "refused for the wrong reason: $(head -1 "${d}/err")"
else
  fail "empty dir" "reported success with no workflows to inspect"
fi

# ── 11. A matrix value that is not hosted is refused ──────────────────────────
# Ohm broke the previous denylist with this: runs-on: ${{ matrix.os }} where the
# matrix contains norstar. The expression itself matches no denylist entry.
# Mutation that breaks this: accepting an expression without expanding the matrix.
d="$(new_repo matrixbad)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-latest, norstar]
    runs-on: ${{ matrix.os }}
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? != 0 )) && grep -q "matrix value" "${d}/err"; then
  pass "a matrix containing a non-hosted value is refused"
else
  fail "bad matrix" "accepted a matrix containing norstar: $(cat "${d}/err")"
fi

# An all-hosted matrix must still pass, or the check is unusable.
d="$(new_repo matrixok)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-latest, ubuntu-24.04, macos-latest]
    runs-on: ${{ matrix.os }}
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? == 0 )); then
  pass "an all-hosted matrix passes"
else
  fail "good matrix" "refused an all-hosted matrix: $(cat "${d}/err")"
fi

# ── 12. An UNKNOWN label is refused, not assumed hosted ───────────────────────
# This is why the check is an allowlist. A denylist cannot know a label that
# did not exist when it was written.
# Mutation that breaks this: treating an unrecognised label as hosted.
d="$(new_repo unknownlabel)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    runs-on: [some-new-box-2027]
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? != 0 )) && grep -q "not GitHub-hosted" "${d}/err"; then
  pass "an unknown runner label is refused rather than assumed hosted"
else
  fail "unknown label" "accepted a label it does not recognise: $(cat "${d}/err")"
fi

# ── 13. A LOCAL reusable workflow is followed into the called file ────────────
# Ohm's third bypass: the calling workflow looks clean and the runner choice
# lives in the called file.
# Mutation that breaks this: skipping jobs that carry `uses:`.
d="$(new_repo localreusable)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  hosted:
    runs-on: ubuntu-latest
    steps: [{run: "true"}]
  sneaky:
    uses: ./.github/workflows/inner.yml
YML
cat > "${d}/.github/workflows/inner.yml" <<'YML'
name: inner
on: [workflow_call]
jobs:
  build:
    runs-on: [self-hosted, norstar]
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? != 0 )) && grep -q "inner.yml" "${d}/err"; then
  pass "a local reusable workflow is followed into the called file"
else
  fail "local reusable" "did not inspect the called workflow: $(cat "${d}/err")"
fi

# ── 14. A REMOTE reusable workflow is UNVERIFIED, not accepted ────────────────
# Its runner lives in another repository and cannot be read here. Unknown
# must fail, not pass.
# Mutation that breaks this: ignoring remote `uses:` jobs.
d="$(new_repo remotereusable)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    uses: someorg/somerepo/.github/workflows/x.yml@main
YML
run_in "$d"
if (( $? != 0 )) && grep -q "UNVERIFIED" "${d}/err"; then
  pass "a remote reusable workflow is reported UNVERIFIED and refused"
else
  fail "remote reusable" "accepted a workflow whose runner it cannot see"
fi

# ── 15. A SECOND self-hosted job beside a hosted one is refused ───────────────
# Ohm's admission point: #42 fixed the default job, but a PR can add an
# independent self-hosted job next to the hosted one. Every job is checked.
# Mutation that breaks this: breaking out of the job loop on the first ok.
d="$(new_repo extrajob)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps: [{run: "true"}]
  extra:
    runs-on: [self-hosted, norstar]
    steps: [{run: "whoami"}]
YML
run_in "$d"
if (( $? != 0 )) && grep -q "extra" "${d}/err"; then
  pass "a second self-hosted job beside a hosted one is refused"
else
  fail "extra job" "missed an added self-hosted job: $(cat "${d}/err")"
fi

# ── 16. A job with no runs-on at all is UNVERIFIED ────────────────────────────
# Mutation that breaks this: returning None for an empty label list.
d="$(new_repo norunson)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? != 0 )) && grep -q "UNVERIFIED" "${d}/err"; then
  pass "a job declaring no runs-on is UNVERIFIED and refused"
else
  fail "no runs-on" "accepted a job with no runner declared"
fi

# ── 10. The real repository passes ────────────────────────────────────────────
# Runs the check against this repo's actual workflows. Named last so a failure
# here is unambiguous: the synthetic cases above already proved the logic.
( cd "$SCRIPT_DIR" && python3 -I scripts/assert_pr_runner_isolation.py >/dev/null 2>"${ROOT}/real.err" )
if (( $? == 0 )); then
  pass "this repository's own workflows satisfy the check"
else
  fail "this repository" "$(cat "${ROOT}/real.err")"
fi

echo
echo "assert_pr_runner_isolation.py: ${PASS_COUNT} passed, ${FAIL_COUNT} failed"
(( FAIL_COUNT == 0 ))

#!/usr/bin/env bash
# Tests for scripts/assert_pr_runner_isolation.py.
#
# The check's whole value is its REJECTION. Watching it pass against the
# repository's current workflows proves nothing, so every case below builds a
# synthetic workflow directory and asserts on the verdict. Each case names the
# mutation that would make it fail.
#
# Run:  bash test/test_pr_runner_isolation.sh
# >>> VENDOR-LOCAL BEGIN
# CI:   invoked by .github/workflows/ci.yml (pr-runner-isolation job)
# <<< VENDOR-LOCAL END
#
# The two sentinel lines above are deliberately SHORT and PURE ASCII, and
# carry no prose. An earlier revision put an explanation on the BEGIN line and
# used an em dash (U+2014, three bytes of UTF-8) inside it. A sentinel that
# must be byte-identical across repositories should not contain a character
# that re-encoding, an editor's smart-punctuation substitution, a
# locale-dependent tool or a copy-paste through a normaliser can alter, and it
# should not contain a sentence anybody might be tempted to reword. The
# failure would not be a security hole — a validator that fails closed reads a
# mangled sentinel as missing and refuses — but it would present as an
# unexplained CI failure in a file nobody touched. Ohm proposed this shape.
#
# Everything OUTSIDE the VENDOR-LOCAL block above is governed by the canonical
# upstream, artifice-ia/codex-container, and must not be edited in a vendored
# copy — a change lands upstream first and arrives as a reviewed sync PR. See
# the canonical-source block in scripts/assert_pr_runner_isolation.py.
#
# The block INSIDE the markers is local by design, because it names a workflow
# in the repository that holds this file and that name cannot be shared. A
# vendored copy edits it and only it.
#
# WHY THE MARKERS EXIST. An integrity manifest that hashes this file whole
# would flag that one necessarily-different line on every run, forever, and the
# natural response to a check that cries wolf is to drop the file from the
# manifest — losing the guard on every other line to tolerate one. Hashing
# the region outside the markers covers all the logic and ignores exactly the
# part meant to differ. Keep the marker lines byte-identical across copies, or
# the region cannot be located.


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

# ── 11. Matrix and expression resolution — Ohm's round-4 bypasses ────────────
# The previous version FLATTENED every matrix value and returned early as soon
# as the flattened set looked hosted. That broke four ways at once.

# 11a. A literal self-hosted beside a hosted matrix expression.
# The worst of the four: checking the expression and returning early DROPPED
# the literal sitting next to it, so a job could name the self-hosted runner
# explicitly and pass.
# Mutation that breaks this: returning None after the first hosted expression.
d="$(new_repo litbeside)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    strategy: {matrix: {os: [ubuntu-latest]}}
    runs-on: [self-hosted, "${{ matrix.os }}"]
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? != 0 )) && grep -q "self-hosted" "${d}/err"; then
  pass "a literal self-hosted beside a hosted matrix expression is refused"
else
  fail "literal beside expression" "dropped the literal: $(cat "${d}/err")"
fi

# 11b. An unresolvable expression blessed by an unrelated matrix.
# inputs.runner has nothing to do with matrix.os, but flattening checked the
# whole matrix and passed.
# Mutation that breaks this: resolving any expression against all matrix values.
d="$(new_repo unrelatedmatrix)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    strategy: {matrix: {os: [ubuntu-latest]}}
    runs-on: "${{ inputs.runner }}"
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? != 0 )) && grep -q "cannot.*resolve\|UNVERIFIED" "${d}/err"; then
  pass "an unresolvable expression is not blessed by an unrelated matrix"
else
  fail "unrelated matrix" "accepted inputs.runner: $(cat "${d}/err")"
fi

# 11c. A matrix axis that is not the runner axis must NOT be judged as one.
# A legitimate os + python-version matrix FAILED because "3.12" was flattened
# in and judged as a runner label. A check that refuses legitimate
# configurations gets deleted rather than fixed.
# Mutation that breaks this: flattening all matrix keys.
d="$(new_repo twoaxis)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-latest, macos-14]
        python-version: ["3.12", "3.13"]
    runs-on: "${{ matrix.os }}"
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? == 0 )); then
  pass "a legitimate os plus python-version matrix passes"
else
  fail "two-axis matrix" "refused a legitimate matrix: $(cat "${d}/err")"
fi

# 11d. The referenced axis is still checked, including values from include.
# Mutation that breaks this: ignoring matrix include entries.
d="$(new_repo includeaxis)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-latest]
        include:
          - os: ubuntu-latest
            runner: norstar
    runs-on: "${{ matrix.runner }}"
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? != 0 )) && grep -q "matrix.runner" "${d}/err"; then
  pass "a private value reachable only through matrix include is refused"
else
  fail "include axis" "missed an include-only value: $(cat "${d}/err")"
fi

# 11e. A matrix reference to a key the matrix does not define is UNVERIFIED.
# Mutation that breaks this: treating an absent key as an empty, vacuous pass.
d="$(new_repo missingkey)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    strategy: {matrix: {os: [ubuntu-latest]}}
    runs-on: "${{ matrix.runner }}"
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? != 0 )) && grep -q "does not define" "${d}/err"; then
  pass "a matrix reference to an undefined key is UNVERIFIED"
else
  fail "missing matrix key" "passed vacuously: $(cat "${d}/err")"
fi

# 11f. A hosted matrix axis containing a private value is refused.
# Mutation that breaks this: checking only the first matrix value.
d="$(new_repo matrixbad)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    strategy: {matrix: {os: [ubuntu-latest, norstar]}}
    runs-on: "${{ matrix.os }}"
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? != 0 )) && grep -q "norstar" "${d}/err"; then
  pass "a matrix axis containing norstar is refused"
else
  fail "bad matrix" "accepted a matrix containing norstar: $(cat "${d}/err")"
fi

# ── 11g. A hosted-looking NAME is not a hosted runner ─────────────────────────
# Ohm: ubuntu-private-machine passed the prefix predicate. A prefix is a
# pattern over a namespace other people extend — the same mistake as the
# denylist, one level down. The allowlist now matches published image names
# EXACTLY.
# Mutation that breaks this: restoring startswith("ubuntu-").
for label in ubuntu-private-machine ubuntu-norstar macos-ours windows-kat \
             ubuntu-slim-norstar ubuntu-privslim macos-slimline slim \
             ubuntu-latest-norstar norstar deet-01 norstar-yugo-1; do
  d="$(new_repo "lookalike-${label}")"
  cat > "${d}/.github/workflows/test.yml" <<YML
name: test
on: [pull_request]
jobs:
  test:
    runs-on: ${label}
    steps: [{run: "true"}]
YML
  run_in "$d"
  if (( $? != 0 )) && grep -q "not a published" "${d}/err"; then
    pass "${label} is refused: a hosted-looking name is not a hosted runner"
  else
    fail "$label" "accepted a lookalike label: $(cat "${d}/err")"
  fi
done

# Every label in the dated catalogue must pass. This list is the catalogue, so
# a label added to the script without a case here, or a case here without the
# script, shows up as a failure rather than as drift.
#
# The list itself was wrong twice, in opposite directions:
#   - An earlier version asserted ubuntu-20.04, windows-2019, macos-13-large
#     and macos-latest-xlarge must PASS. The first three are retired or gone,
#     so the test data was memory-derived exactly as the script's old list was.
#   - I then called macos-latest-xlarge nonexistent and moved it to the refuse
#     list. Ohm found it in GitHub's larger-runners reference. ABSENT FROM THE
#     SOURCE I READ IS NOT NONEXISTENT; it is back here, and the script's
#     catalogue cites that third source for it.
for label in ubuntu-latest ubuntu-26.04 ubuntu-26.04-arm ubuntu-24.04 \
             ubuntu-24.04-arm ubuntu-22.04 ubuntu-22.04-arm ubuntu-slim \
             windows-latest windows-2025 windows-2025-vs2026 windows-2022 \
             windows-11-arm windows-11-vs2026-arm \
             macos-latest macos-latest-large macos-latest-xlarge \
             macos-26 macos-26-intel \
             macos-26-large macos-26-xlarge macos-15 macos-15-intel \
             macos-15-large macos-15-xlarge macos-14 macos-14-large \
             macos-14-xlarge xcode-27 xcode-27-xlarge; do
  d="$(new_repo "published-${label}")"
  cat > "${d}/.github/workflows/test.yml" <<YML
name: test
on: [pull_request]
jobs:
  test:
    runs-on: ${label}
    steps: [{run: "true"}]
YML
  run_in "$d"
  if (( $? == 0 )); then
    pass "${label} passes"
  else
    fail "$label" "refused a catalogued image: $(cat "${d}/err")"
  fi
done

# RETIRED and never-existed labels are refused. A retired image cannot run the
# job anyway, so refusing it reports the real problem instead of blessing a
# dead label. ubuntu-99.99 is the case a naming heuristic wrongly accepted.
for label in ubuntu-20.04 windows-2019 macos-13 macos-13-large \
             ubuntu-99.99 ubuntu-18.04; do
  d="$(new_repo "retired-${label}")"
  cat > "${d}/.github/workflows/test.yml" <<YML
name: test
on: [pull_request]
jobs:
  test:
    runs-on: ${label}
    steps: [{run: "true"}]
YML
  run_in "$d"
  if (( $? != 0 )); then
    pass "${label} is refused: not in the dated catalogue"
  else
    fail "$label" "accepted a label absent from the catalogue"
  fi
done

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
if (( $? != 0 )) && grep -q "not a published GitHub-hosted image" "${d}/err"; then
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
rc=$?
# Ohm: this case used to pass on the WRONG error. lstrip("./") stripped the dot
# out of ".github", the file was never found, and the resulting
# "not readable here: UNVERIFIED" message happened to contain "inner.yml" — so
# the grep matched while local reuse was never actually inspected. Assert that
# the callee's own job and its runner label were judged.
if (( rc != 0 )) && grep -q "inner.yml:b" "${d}/err" \
   && grep -q "self-hosted" "${d}/err" \
   && ! grep -q "not readable" "${d}/err"; then
  pass "a local reusable workflow is read and its job's runner judged"
elif (( rc != 0 )) && grep -q "not readable" "${d}/err"; then
  fail "local reusable" "the callee path did not resolve: $(cat "${d}/err")"
else
  fail "local reusable" "did not inspect the called workflow: $(cat "${d}/err")"
fi

# A local reusable workflow whose own job IS hosted, with its own matrix, must
# PASS. The path bug made every local callee fail, which is the shape that gets
# a check switched off.
# Mutation that breaks this: restoring lstrip("./").
d="$(new_repo localreusableok)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  j:
    uses: ./.github/workflows/inner.yml
YML
cat > "${d}/.github/workflows/inner.yml" <<'YML'
name: inner
on: [workflow_call]
jobs:
  b:
    strategy:
      matrix:
        os: [ubuntu-latest, ubuntu-26.04]
        python-version: ["3.12"]
    runs-on: "${{ matrix.os }}"
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? == 0 )); then
  pass "a hosted local reusable workflow with its own matrix passes"
else
  fail "hosted local reusable" "refused a hosted callee: $(cat "${d}/err")"
fi

# ── A runner GROUP is UNVERIFIED, whatever it is called ───────────────────────
# Ohm: runs-on: {group: ubuntu-latest} passed, because the group NAME looked
# like a hosted image. A group selects whatever runners it contains, which this
# script cannot see.
# Mutation that breaks this: treating the group name as an ordinary label.
d="$(new_repo groupname)"
cat > "${d}/.github/workflows/test.yml" <<'YML'
name: test
on: [pull_request]
jobs:
  test:
    runs-on:
      group: ubuntu-latest
    steps: [{run: "true"}]
YML
run_in "$d"
if (( $? != 0 )) && grep -q "group" "${d}/err" && grep -q "UNVERIFIED" "${d}/err"; then
  pass "a runner group named like a hosted image is UNVERIFIED"
else
  fail "group name" "accepted a group selection: $(cat "${d}/err")"
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

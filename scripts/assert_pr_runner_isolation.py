#!/usr/bin/env python3
"""Refuse the build if a pull-request-triggered workflow uses a self-hosted runner.

WHY THIS EXISTS. PR #42 moved the test job from the self-hosted norstar runner
to ubuntu-latest, because pull request code is untrusted and the norstar runner
user is in the host's docker group, which is root-equivalent on a host that
holds every fleet secret. That change recorded its reason in an inline comment:

    # PR code is untrusted. Never use the self-hosted norstar runner here

A comment cannot fail. The next edit that moves a pull-request job back onto
the self-hosted runner would leave that comment sitting above it, still reading
as if the rule were in force. This script turns the rule into a check.

WHAT THIS IS, EXACTLY. Lint, not admission control. It runs as a CI step, which
is AFTER GitHub has already scheduled the jobs in the workflow, so it cannot
prevent anything from running. A pull request that adds a self-hosted job gets
that job scheduled, and this check then fails the build afterwards. What it
buys is that such a change cannot pass review silently, which is the failure
mode an inline comment leaves wide open.

An earlier revision of this header said it "keeps untrusted code away from the
runner". It does not. Only admission control does that — runner-group
restrictions limiting which workflows may use the runner, or not having a
self-hosted runner reachable by pull-request workflows at all. Those are
separate work and are not addressed here.

WHAT IT CHECKS. For every workflow in .github/workflows that `pull_request` or
`pull_request_target` can trigger, EVERY label of EVERY job's `runs-on` must
resolve to a label in HOSTED_IMAGES, the dated catalogue above, matched
exactly. An earlier revision replaced that catalogue with a naming heuristic
and left this paragraph claiming exact validation against a set that no longer
existed; the catalogue is back and this paragraph is true again. The forms accepted by `runs-on` are all flattened first: a
string, a list, and a `group`/`labels` mapping, where the group name counts as
a label because it can select a private pool.

Resolution is per selector, not per matrix. A `${{ matrix.KEY }}` resolves to
that key's values only, including values introduced by `include`. Every other
expression is UNVERIFIED, because it names something this script cannot see.

Anything unrecognised FAILS: an unknown label, an unresolvable expression, a
matrix reference to a key the matrix does not define, a job with no `runs-on`,
and a remote reusable workflow whose runner lives in another repository. A
LOCAL reusable workflow is followed into the called file.

TWO DELIBERATE APPROXIMATIONS, both conservative, both named because an
unstated approximation reads as a guarantee.

1. `exclude` is ignored. A matrix `exclude` removes combinations, so ignoring
   it can never make a job pass that would otherwise fail. It CAN falsely
   reject: a matrix listing a private runner that `exclude` removes from every
   surviving combination is refused here. That is the safe direction and it is
   visible when it happens, so it is not special-cased.

2. Only a bare `${{ matrix.KEY }}` is resolved. `fromJSON(...)`,
   `format(...)`, a conditional, and `matrix.KEY` embedded in a larger string
   are all legitimate and all refused as UNVERIFIED. Evaluating GitHub
   expressions properly means implementing their language, which is not worth
   it for a lint; a job that needs one of those forms should be told to use a
   bare label, or this script should grow support for the specific form.

WHAT IT DOES NOT CHECK. `push` and `workflow_dispatch` workflows: deploy.yml
legitimately runs on norstar, and neither trigger carries untrusted code. It
does not evaluate GitHub expressions, so it cannot know what `inputs.runner`
becomes at run time — it refuses rather than guesses. It says nothing about
what a job does once it runs.

Tests: test/test_pr_runner_isolation.sh.
"""

from __future__ import annotations

import sys
import re
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    print(
        "assert_pr_runner_isolation: PyYAML is required. It is in requirements.txt.",
        file=sys.stderr,
    )
    raise SystemExit(2)

# POSITIVE allowlist, deliberately. An earlier revision of this script held a
# denylist of labels we own: self-hosted, norstar, deet-01 and so on. Ohm broke
# it in three ways at once — a matrix whose values include norstar, a machine
# label the list had never heard of, and a local reusable workflow that selects
# norstar inside the called file. All three exited 0.
#
# A denylist is as narrow as the labels whoever wrote it happened to think of,
# and a new machine label is invisible to it by construction. So the rule is
# inverted: a pull-request job must positively select a runner we can see is
# GitHub-hosted. Anything else — an unknown label, an unresolved expression, a
# reusable workflow we cannot read — is reported as UNVERIFIED and fails.
#
# This is still lint, not admission control. It runs after scheduling and
# cannot stop a job from being scheduled. What it does is keep a regression
# from passing review silently.
# A DATED, SOURCED catalogue of GitHub-hosted runner labels.
#
# CATALOGUE AS OF 2026-10-30, read from two sources that agree:
#   https://github.com/actions/runner-images  (README available-images table)
#   https://docs.github.com/en/actions/reference/runners/github-hosted-runners
#   plus GitHub's larger-runners reference for `macos-latest-xlarge`, which the
#   runner-images table does not list. Ohm caught me calling that label
#   nonexistent when it was merely absent from the two sources I happened to
#   read: ABSENT FROM A SOURCE IS NOT NONEXISTENT, and a third source was
#   needed to settle it.
#
# This is the third design for this check and the second enumeration. The
# history matters, because each failure was a different shape:
#   - A denylist of labels we own let through anything it had not heard of.
#   - A prefix test on "ubuntu-" let through `ubuntu-private-machine`.
#   - An exact enumeration typed FROM MEMORY omitted ubuntu-26.04,
#     ubuntu-26.04-arm and ubuntu-slim.
#   - A naming heuristic admitted `ubuntu-99.99` while REJECTING the real
#     labels xcode-27, macos-26-intel, windows-2025-vs2026 and
#     windows-11-vs2026-arm, none of which fit <os>-<version>.
#
# The defect in the memory-derived list was the SOURCE, not the approach: a
# published catalogue cannot be guessed at and must not be. So this list is
# enumerated, dated, and cited, and the rule for changing it is in
# MAINTENANCE below.
#
# MAINTENANCE. When GitHub publishes a new image, add its label here and move
# the date. Read it off one of the two URLs above; do not infer it from a
# pattern and do not add a label because a workflow wants one. A label absent
# here is refused as UNVERIFIED, which fails CLOSED: legitimate work breaks
# loudly and visibly rather than a private runner passing quietly. That is the
# intended direction of failure.
#
# RETIRED IMAGES ARE DELIBERATELY ABSENT. ubuntu-20.04 and windows-2019 are no
# longer provided, so a workflow naming one cannot run on it anyway; refusing
# them here reports the real problem instead of blessing a dead label.
CATALOGUE_DATE = "2026-10-30"

HOSTED_IMAGES = frozenset({
    # Ubuntu
    "ubuntu-latest",
    "ubuntu-26.04",
    "ubuntu-26.04-arm",
    "ubuntu-24.04",
    "ubuntu-24.04-arm",
    "ubuntu-22.04",
    "ubuntu-22.04-arm",
    "ubuntu-slim",
    # Windows
    "windows-latest",
    "windows-2025",
    "windows-2025-vs2026",
    "windows-2022",
    "windows-11-arm",
    "windows-11-vs2026-arm",
    # macOS, including the Intel and larger variants
    "macos-latest",
    "macos-latest-large",
    "macos-latest-xlarge",
    "macos-26",
    "macos-26-intel",
    "macos-26-large",
    "macos-26-xlarge",
    "macos-15",
    "macos-15-intel",
    "macos-15-large",
    "macos-15-xlarge",
    "macos-14",
    "macos-14-large",
    "macos-14-xlarge",
    # Xcode images, preview as of the catalogue date
    "xcode-27",
    "xcode-27-xlarge",
})

# Labels this fleet uses for its own runners. Refused regardless, so that an
# accidental addition to HOSTED_IMAGES cannot bless one of ours.
KNOWN_PRIVATE = frozenset({
    "self-hosted",
    "norstar",
    "deet-01",
    "old-shadow",
    "cm-01",
    "norstar-1",
    "norstar-2",
    "norstar-yugo-1",
})


def is_hosted(label: str) -> bool:
    """True only for a label in the dated catalogue above, matched exactly."""
    label = label.strip()
    if not label or label in KNOWN_PRIVATE:
        return False
    return label in HOSTED_IMAGES


PR_TRIGGERS = {"pull_request", "pull_request_target"}

# ${{ matrix.KEY }}, with any spacing. Only a matrix reference is resolvable
# here; every other expression names something this script cannot see.
GROUP_SENTINEL = "\x00group:"

MATRIX_REF = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}$")

EXPRESSION = re.compile(r"\$\{\{")


def runner_labels(runs_on: object) -> list[str]:
    """Flatten every form `runs-on` accepts into a list of label strings.

    A string, a list, or a {group, labels} mapping. The group name can itself
    select a private runner pool, so it is returned as a label. Anything
    unexpected is stringified rather than dropped: a value this function cannot
    read must reach the caller and be refused, not silently vanish.
    """
    if runs_on is None:
        return []
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, list):
        out: list[str] = []
        for item in runs_on:
            out.extend(runner_labels(item))
        return out
    if isinstance(runs_on, dict):
        out = []
        group = runs_on.get("group")
        if group is not None:
            # A GROUP NAME IS NOT AN IMAGE IDENTITY. Ohm got past the previous
            # version with `runs-on: {group: ubuntu-latest}`, where the group
            # name merely looked like a hosted image while selecting whatever
            # runners that group actually contains — which this script cannot
            # see. Marked so the caller refuses it as UNVERIFIED rather than
            # judging it as a label.
            out.append(f"{GROUP_SENTINEL}{group}")
        out.extend(runner_labels(runs_on.get("labels")))
        return out
    return [str(runs_on)]


def matrix_values_for(job: dict, key: str) -> list[str] | None:
    """The values a specific matrix key can take, or None when it is absent.

    Keyed deliberately. An earlier revision flattened EVERY matrix value into
    one list, which broke two ways at once: `${{ inputs.runner }}` was blessed
    by a matrix that never mentioned it, and a legitimate `os` + `python-version`
    matrix failed because "3.12" was judged as a runner label.
    """
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return None

    values: list[str] = []
    found = False

    if key in matrix:
        found = True
        raw = matrix[key]
        if isinstance(raw, list):
            values.extend(str(v) for v in raw)
        else:
            values.append(str(raw))

    # `include` can introduce additional combinations, and therefore additional
    # values for this key. `exclude` only removes combinations, so it can never
    # make a job hosted that was not already.
    include = matrix.get("include")
    if isinstance(include, list):
        for entry in include:
            if isinstance(entry, dict) and key in entry:
                found = True
                values.append(str(entry[key]))

    return values if found else None


def check_runs_on(job: dict) -> str | None:
    """None when the job positively selects GitHub-hosted runners only.

    Otherwise a string saying why it is refused. EVERY label must resolve to a
    published hosted image. Unknown is refused.
    """
    labels = runner_labels(job.get("runs-on"))
    if not labels:
        return "declares no runs-on: UNVERIFIED"

    problems: list[str] = []

    for label in labels:
        label = label.strip()

        if label.startswith(GROUP_SENTINEL):
            name = label[len(GROUP_SENTINEL):]
            problems.append(
                f"selects runner group {name!r}; a group name is not an image "
                "identity and this script cannot see which runners the group "
                "contains: UNVERIFIED"
            )
            continue

        if not EXPRESSION.search(label):
            # A plain literal. This is where `self-hosted` beside an expression
            # used to be dropped: an earlier revision checked the expression,
            # found its matrix hosted, and returned early without ever judging
            # the literal sitting next to it.
            if not is_hosted(label):
                problems.append(
                    f"label {label!r} is not a published GitHub-hosted image"
                )
            continue

        ref = MATRIX_REF.match(label)
        if not ref:
            problems.append(
                f"selector {label!r} is an expression this script cannot "
                "resolve: UNVERIFIED"
            )
            continue

        key = ref.group(1)
        candidates = matrix_values_for(job, key)
        if candidates is None:
            problems.append(
                f"selector {label!r} references matrix key {key!r}, which the "
                "job's matrix does not define: UNVERIFIED"
            )
            continue
        bad = [c for c in candidates if not is_hosted(c)]
        if bad:
            problems.append(
                f"selector {label!r} resolves through matrix.{key} to "
                f"{bad}, which are not published GitHub-hosted images"
            )

    if problems:
        return "; ".join(problems)
    return None


def triggers(doc: dict) -> set[str]:
    # `on` is the YAML 1.1 boolean True once parsed, which is a long-standing
    # trap: a plain doc["on"] lookup misses it and every workflow then looks
    # untriggered, which would make this whole check pass vacuously.
    raw = doc.get("on", doc.get(True))
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {str(x) for x in raw}
    if isinstance(raw, dict):
        return {str(k) for k in raw}
    return set()


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    workflow_dir = root / ".github" / "workflows"
    files = sorted(
        p for p in workflow_dir.iterdir() if p.suffix in (".yml", ".yaml")
    )
    if not files:
        print(
            f"assert_pr_runner_isolation: no workflows found in {workflow_dir}. "
            "A check that inspects nothing cannot fail, so this is an error.",
            file=sys.stderr,
        )
        return 2

    violations: list[str] = []
    checked = 0

    for path in files:
        doc = yaml.safe_load(path.read_text())
        if not isinstance(doc, dict):
            violations.append(f"{path.name}: not a mapping; cannot be inspected")
            continue

        fired_by_pr = triggers(doc) & PR_TRIGGERS
        if not fired_by_pr:
            print(f"    skip {path.name}: not triggered by a pull request")
            continue

        checked += 1
        jobs = doc.get("jobs") or {}
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue

            # A reusable workflow puts the runner choice in the CALLED file.
            # A local one (./.github/workflows/x.yml) is readable, so follow
            # it. A remote one is not, so it is UNVERIFIED, not acceptable.
            uses = job.get("uses")
            if uses and "runs-on" not in job:
                if isinstance(uses, str) and uses.startswith("./"):
                    # NOT lstrip("./"): lstrip removes ANY leading "." or "/"
                    # characters, so "./.github/..." became "github/..." and the
                    # file was never found. Local reuse was therefore never
                    # actually inspected, and the negative test passed only
                    # because the unreadable-file message happened to name the
                    # callee. removeprefix removes the one prefix meant.
                    rel = uses.split("@", 1)[0].removeprefix("./")
                    called = root / rel
                    if not called.is_file():
                        violations.append(
                            f"{path.name}:{job_name} calls {uses}, which is not "
                            "readable here: UNVERIFIED"
                        )
                        continue
                    sub = yaml.safe_load(called.read_text())
                    sub_jobs = (sub or {}).get("jobs") or {}
                    for sub_name, sub_job in sub_jobs.items():
                        if not isinstance(sub_job, dict):
                            continue
                        verdict = check_runs_on(sub_job)
                        if verdict is not None:
                            violations.append(
                                f"{path.name}:{job_name} -> {uses}:{sub_name} "
                                f"{verdict}"
                            )
                    continue
                violations.append(
                    f"{path.name}:{job_name} calls the remote reusable workflow "
                    f"{uses}; its runner cannot be inspected here: UNVERIFIED"
                )
                continue

            verdict = check_runs_on(job)
            if verdict is not None:
                violations.append(f"{path.name}:{job_name} {verdict}")
            else:
                print(f"    ok   {path.name}:{job_name}")

    if checked == 0:
        print(
            "assert_pr_runner_isolation: no workflow is triggered by a pull "
            "request. Either the triggers changed or this check is looking in "
            "the wrong place; refusing rather than reporting success.",
            file=sys.stderr,
        )
        return 2

    if violations:
        print(file=sys.stderr)
        print(
            "PR RUNNER ISOLATION BROKEN: a pull request can run code on a "
            "runner we own.",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print(file=sys.stderr)
        print(
            "Pull request code is untrusted, and the norstar runner user is in "
            "the host's docker group, which is root-equivalent on a host that "
            "holds the fleet's secrets. A job-level `if:` guard does not fix "
            "this: a pull_request event runs the workflow from the PR's own "
            "merge commit, so the pull request can delete the guard. Move the "
            "job to a GitHub-hosted runner.",
            file=sys.stderr,
        )
        return 1

    print(
        f"==> PR runner isolation intact across {checked} "
        f"pull-request workflow(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

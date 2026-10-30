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

WHAT IT CHECKS. For every workflow in .github/workflows that `pull_request` or
`pull_request_target` can trigger, every job's `runs-on` must be
GitHub-hosted. A job is refused when its `runs-on` names `self-hosted`, or
names a known private runner label, in any of the forms GitHub accepts: a
string, a list, or a `group`/`labels` mapping.

WHAT IT DOES NOT CHECK. It says nothing about `push` or `workflow_dispatch`
workflows: deploy.yml legitimately runs on norstar, and neither trigger carries
untrusted code. It does not inspect a reusable workflow called with `uses:`,
because the runner choice then lives in the called file, which may be in
another repository. It does not make the runner safe; it keeps untrusted code
away from it.

Tests: test/test_pr_runner_isolation.sh.
"""

from __future__ import annotations

import sys
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
HOSTED_PREFIXES = (
    "ubuntu-",
    "windows-",
    "macos-",
)
HOSTED_EXACT = {
    "ubuntu-latest",
    "windows-latest",
    "macos-latest",
}

PR_TRIGGERS = {"pull_request", "pull_request_target"}


def is_hosted(label: str) -> bool:
    """True only when the label is recognisably a GitHub-hosted runner image."""
    label = label.strip()
    if not label:
        return False
    # An unresolved expression could evaluate to anything, including a label we
    # own, so it is never accepted.
    if "${{" in label:
        return False
    if label in HOSTED_EXACT:
        return True
    return label.startswith(HOSTED_PREFIXES)


def runner_labels(runs_on: object) -> list[str]:
    """Flatten every form `runs-on` accepts into a list of label strings."""
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
        # {group: ..., labels: [...]} — the group name can itself select a
        # private runner pool, so it counts as a label here.
        out = []
        group = runs_on.get("group")
        if isinstance(group, str):
            out.append(group)
        out.extend(runner_labels(runs_on.get("labels")))
        return out
    # An expression, a number, anything unexpected: stringify rather than
    # silently ignore, so an unreadable value cannot pass as hosted.
    return [str(runs_on)]


def matrix_values(job: dict) -> list[str]:
    """Every literal string a job's matrix could substitute into runs-on.

    A `runs-on: ${{ matrix.os }}` is only as safe as the worst value in the
    matrix, so each one is checked. Values that are themselves expressions or
    non-strings are returned as-is so is_hosted() rejects them.
    """
    strategy = job.get("strategy") or {}
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    out: list[str] = []
    if not isinstance(matrix, dict):
        return out
    for key, value in matrix.items():
        if key in ("include", "exclude"):
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict):
                        out.extend(str(v) for v in entry.values())
            continue
        if isinstance(value, list):
            out.extend(str(v) for v in value)
        else:
            out.append(str(value))
    return out


def check_runs_on(job: dict) -> str | None:
    """None when the job positively selects GitHub-hosted runners only.

    Otherwise a string describing why it is refused. Unknown is refused: an
    unrecognised label may be a runner we own, and a denylist cannot know.
    """
    labels = runner_labels(job.get("runs-on"))
    if not labels:
        return "declares no runs-on: UNVERIFIED"

    # An expression means the real value comes from somewhere else. The matrix
    # is the common case and is expanded; anything else stays unverified.
    if any("${{" in l for l in labels):
        candidates = matrix_values(job)
        if not candidates:
            return (
                f"selects its runner by expression {labels} with no matrix to "
                "resolve: UNVERIFIED"
            )
        bad = [c for c in candidates if not is_hosted(c)]
        if bad:
            return (
                f"selects its runner by expression {labels}, and matrix "
                f"value(s) {bad} are not GitHub-hosted"
            )
        return None

    bad = [l for l in labels if not is_hosted(l)]
    if bad:
        return f"runs on {labels}; label(s) {bad} are not GitHub-hosted"
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
                    called = root / uses.split("@", 1)[0].lstrip("./")
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

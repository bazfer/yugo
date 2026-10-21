"""submit_review — formal GitHub PR review with injection-proof verdict guards.

Does NOT generalize http_tools. That transport is GET-only by deliberate design
and stays that way. This module owns GitHub review POST. It reads the token from
the credentials file, builds its own fixed request, and never accepts a URL or
HTTP method from the model.

The three guards are documented on submit_review(). Input validation fires
before any network call — see _validate_inputs().
"""
from __future__ import annotations

import http.client
import json
import os
import re
import ssl
from pathlib import Path
from typing import Any, Callable

import http_tools

# Allowlist and path-segment safety. Repo path segments go directly into the
# URL path: "/repos/{owner}/{repo}/pulls/{pr}". The model supplies them, so
# unvalidated values can carry separators ("/"), traversal (".."), query ("?"),
# or fragment ("#") — turning path control into partial URL control. Type hints
# are not runtime checks and the schema enum for pr=integer is advisory.
# Validation happens before any network call so the stub can assert no request
# was made.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")


class _AllowAllType:
    """Sentinel allowlist that permits any repo.

    Used in tests and for any caller that deliberately wants no restriction.
    Making this a named, explicit sentinel (rather than None) means omitting
    the allowlist argument is a hard error, not a silent allow-all.

    `repo not in ALLOW_ALL` is always False so the guard never fires.
    """

    def __contains__(self, item: object) -> bool:
        return True

    def __repr__(self) -> str:
        return "ALLOW_ALL"


ALLOW_ALL: _AllowAllType = _AllowAllType()

GITHUB_HOST = "api.github.com"
GITHUB_API_ORIGIN = http_tools.parse_authority(GITHUB_HOST)
DEFAULT_CREDENTIALS_FILE = http_tools.DEFAULT_CREDENTIALS_FILE

_DECISION_EVENT: dict[str, str] = {
    "approve": "APPROVE",
    "request_changes": "REQUEST_CHANGES",
    "insufficient_evidence": "COMMENT",
}


class GitHubReviewError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _validate_inputs(repo: Any, pr: Any) -> int:
    """Validate repo and pr before they touch a URL path.

    Returns the validated pr as int. Raises GitHubReviewError so the refusal
    is testable and the stub can assert no HTTP call was attempted.
    """
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        raise GitHubReviewError(
            "invalid_repo",
            f"repo must be owner/name with alphanumeric, dot, hyphen, underscore "
            f"(1-100 chars each); got {repo!r}",
        )
    try:
        pr_int = int(pr)
    except (TypeError, ValueError) as exc:
        raise GitHubReviewError("invalid_pr", f"pr must be an integer; got {pr!r}") from exc
    if pr_int <= 0:
        raise GitHubReviewError("invalid_pr", f"pr must be positive; got {pr_int}")
    return pr_int


def _load_token(credentials_path: Path | None) -> str:
    creds = http_tools.load_credentials(credentials_path)
    cred = creds.get(GITHUB_API_ORIGIN)
    if cred is None:
        raise GitHubReviewError(
            "github_no_credential",
            f"no credential found for {GITHUB_API_ORIGIN.rendered()!r} in credentials file",
        )
    return cred.value


def _make_ssl() -> ssl.SSLContext:
    return ssl.create_default_context()


def _default_conn_factory() -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(GITHUB_HOST, context=_make_ssl())


def _github_get(
    path: str,
    token: str,
    *,
    _conn_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    factory = _conn_factory or _default_conn_factory
    conn = factory()
    try:
        conn.request(
            "GET",
            path,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "yugo-submit-review/1",
            },
        )
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status != 200:
            raise GitHubReviewError(
                "github_get_error",
                f"GET {path} returned HTTP {resp.status}",
            )
        return json.loads(body)
    finally:
        conn.close()


def _github_post(
    path: str,
    payload: dict[str, Any],
    token: str,
    *,
    _conn_factory: Callable[[], Any] | None = None,
) -> None:
    factory = _conn_factory or _default_conn_factory
    raw = json.dumps(payload).encode("utf-8")
    conn = factory()
    try:
        conn.request(
            "POST",
            path,
            body=raw,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "Content-Length": str(len(raw)),
                "User-Agent": "yugo-submit-review/1",
            },
        )
        resp = conn.getresponse()
        resp.read()
        if resp.status not in (200, 201):
            raise GitHubReviewError(
                "github_post_error",
                f"POST {path} returned HTTP {resp.status}",
            )
    finally:
        conn.close()


def submit_review(
    repo: str,
    pr: int,
    head_sha: str,
    decision: str,
    verdict: Any,
    *,
    credentials_path: Path | None = None,
    allowlist: frozenset[str] | _AllowAllType,
    _conn_factory: Callable[[], Any] | None = None,
) -> str:
    """Submit a formal GitHub PR review.

    Never accepts a URL or HTTP method from the model — these are hardcoded.
    Input validation fires before any network call (see _validate_inputs).

    `allowlist` is required and explicit — no default. Pass `ALLOW_ALL` to
    bypass the check deliberately (tests, or tools without an allowlist grant).
    An empty frozenset refuses all repos (fail-safe: missing grant → deny all).

    Three guards (all required):

    1. Verdict must be a mapping with non-empty `properties_asserted` and
       `mutations_tried` lists. Additionally:
       - `request_changes` requires a non-empty `findings` list, where each
         item has `summary` and `evidence` strings (and an optional `location`
         string). A blocking verdict without a named defect is not actionable.
       - `insufficient_evidence` requires a non-empty `unverified` list of
         strings naming what the reviewer could not verify.
       - `approve` keeps the existing shape; no extra fields required.
       Forcing real evidence fields means an injection must fabricate them,
       which is auditable afterwards.

    2. PR head is re-fetched and must match `head_sha`. GitHub's
       dismiss_stale_reviews_on_push and require_last_push_approval are both OFF
       on the current fleet repos, so without this guard an approval survives a
       later push and covers code the reviewer never saw.

    3. The reviewed SHA is written into the review body so the record is
       self-describing even if the guard is later disabled.
    """
    # Input validation: reject malformed repo/pr before any network call
    pr = _validate_inputs(repo, pr)

    # Allowlist check: scope is set in the grant file, not here
    if repo not in allowlist:
        raise GitHubReviewError(
            "repo_not_allowlisted",
            f"repo {repo!r} not in the configured allowlist",
        )

    # Guard 1: structured, non-empty verdict
    if not isinstance(verdict, dict):
        raise GitHubReviewError("verdict_not_structured", "verdict must be a mapping")
    props = verdict.get("properties_asserted")
    muts = verdict.get("mutations_tried")
    if not isinstance(props, list) or len(props) == 0:
        raise GitHubReviewError(
            "verdict_no_properties",
            "verdict.properties_asserted must be a non-empty list",
        )
    if not isinstance(muts, list) or len(muts) == 0:
        raise GitHubReviewError(
            "verdict_no_mutations",
            "verdict.mutations_tried must be a non-empty list",
        )
    if decision not in _DECISION_EVENT:
        raise GitHubReviewError(
            "verdict_bad_decision",
            f"decision must be one of {sorted(_DECISION_EVENT)}; got {decision!r}",
        )

    # Guard 1 (continued): decision-specific required fields
    if decision == "request_changes":
        findings = verdict.get("findings")
        if not isinstance(findings, list) or len(findings) == 0:
            raise GitHubReviewError(
                "verdict_no_findings",
                "verdict.findings must be a non-empty list for request_changes",
            )
        for i, f in enumerate(findings):
            if (not isinstance(f, dict)
                    or not isinstance(f.get("summary"), str) or not f.get("summary")
                    or not isinstance(f.get("evidence"), str) or not f.get("evidence")):
                raise GitHubReviewError(
                    "verdict_finding_malformed",
                    f"verdict.findings[{i}] must have non-empty string 'summary' and 'evidence'",
                )
            loc = f.get("location")
            if loc is not None and not isinstance(loc, str):
                raise GitHubReviewError(
                    "verdict_finding_malformed",
                    f"verdict.findings[{i}].location must be a string if present",
                )
    elif decision == "insufficient_evidence":
        unverified = verdict.get("unverified")
        if not isinstance(unverified, list) or len(unverified) == 0:
            raise GitHubReviewError(
                "verdict_no_unverified",
                "verdict.unverified must be a non-empty list for insufficient_evidence",
            )
        for i, u in enumerate(unverified):
            if not isinstance(u, str) or not u:
                raise GitHubReviewError(
                    "verdict_unverified_malformed",
                    f"verdict.unverified[{i}] must be a non-empty string",
                )

    token = _load_token(credentials_path)

    # Guard 2: re-fetch PR head and refuse if it has moved
    pr_data = _github_get(f"/repos/{repo}/pulls/{pr}", token, _conn_factory=_conn_factory)
    current_head = pr_data.get("head", {}).get("sha", "")
    if current_head != head_sha:
        raise GitHubReviewError(
            "head_moved",
            f"PR head is now {current_head!r}; reviewed {head_sha!r} — re-review required",
        )

    event = _DECISION_EVENT[decision]

    # Guard 3: write reviewed SHA into body so the record is self-describing
    lines = [f"Reviewed at {head_sha}.", ""]
    lines.extend(f"- {p}" for p in props)
    lines.append("")
    lines.extend(f"- {m} [mutation]" for m in muts)
    if decision == "request_changes":
        lines.append("")
        lines.append("Findings:")
        for f in findings:  # type: ignore[assignment]
            loc_str = f" ({f['location']})" if f.get("location") else ""
            lines.append(f"- {f['summary']}: {f['evidence']}{loc_str}")
    elif decision == "insufficient_evidence":
        lines.append("")
        lines.append("Could not verify:")
        lines.extend(f"- {u}" for u in unverified)  # type: ignore[assignment]
    body_text = "\n".join(lines).strip()

    _github_post(
        f"/repos/{repo}/pulls/{pr}/reviews",
        {"commit_id": head_sha, "event": event, "body": body_text},
        token,
        _conn_factory=_conn_factory,
    )
    return f"submit_review ok: {event} on {repo}#{pr} at {head_sha}"

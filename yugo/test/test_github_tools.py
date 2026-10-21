"""submit_review guards and positive control.

No live GitHub calls. Transport is stubbed via _conn_factory. Credentials are
written to a temp file in the http_tools format so _load_token exercises the
real credential loading path.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

import github_tools as gh


HEAD = "abc1234def5678901234567890123456789012ab"
REPO = "bazfer/yugo"
PR = 42

_VALID_VERDICT = {
    "properties_asserted": ["bus-frame continue skips, not breaks"],
    "mutations_tried": ["reverted continue to break — test failed"],
}

_VALID_VERDICT_REQUEST_CHANGES = {
    **_VALID_VERDICT,
    "findings": [
        {"summary": "reload drops names on empty file", "evidence": "clear() runs before parse; exception leaves set empty", "location": "src/fleet-bus.ts:312"},
    ],
}

_VALID_VERDICT_INSUFFICIENT = {
    **_VALID_VERDICT,
    "unverified": ["could not reproduce concurrency race under test conditions"],
}


# --- test transport stub ---


class _FakeConn:
    """Minimal http.client.HTTPSConnection stand-in.

    Collects (method, path, body, headers) for each request and returns one
    synthetic response per call.
    """

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self._queue = list(responses)
        self.calls: list[tuple[str, str, bytes | None, dict | None]] = []
        self._pending: tuple[int, str] | None = None

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict | None = None,
    ) -> None:
        self.calls.append((method, path, body, headers))
        self._pending = self._queue.pop(0)

    def getresponse(self) -> "_FakeResponse":
        assert self._pending is not None
        status, body = self._pending
        self._pending = None
        return _FakeResponse(status, body)

    def close(self) -> None:
        pass


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body


def _conn_factory_for(responses: list[tuple[int, str]]):
    """Returns a factory that dispenses one FakeConn per call, recording them."""
    conns: list[_FakeConn] = []

    def factory() -> _FakeConn:
        conn = _FakeConn(responses[:1])
        responses.pop(0)
        conns.append(conn)
        return conn

    factory.conns = conns  # type: ignore[attr-defined]
    return factory


def _pr_response(head: str = HEAD) -> tuple[int, str]:
    return 200, json.dumps({"head": {"sha": head}})


def _review_response() -> tuple[int, str]:
    return 201, json.dumps({"id": 1, "state": "APPROVED"})


# --- credentials fixture ---


def _creds_file(tmp_path: Path, *, token: str = "ghp_test_token_x") -> Path:
    """Write a minimal credentials file with an api.github.com entry."""
    p = tmp_path / "creds.yaml"
    p.write_text(
        "version: 1\n"
        "credentials:\n"
        f"  - id: github-test\n"
        f"    host: api.github.com\n"
        f"    header: Authorization\n"
        f"    value: {token}\n"
    )
    p.chmod(0o600)
    return p


# --- P1: input validation fires before any network call ---
# repo and pr go directly into the URL path. Path separators, traversal
# components, query markers, and non-integer pr values are rejected here,
# before either HTTP call, so the stub can assert no request was made.


@pytest.mark.parametrize(
    "bad_repo",
    [
        "o/r/../../x",     # path traversal via extra slash
        "o/r?x=1",         # query string injection
        "o/r#frag",        # fragment injection
        "../etc/passwd",   # no slash in right place
        "",                # empty
        "o/" + "a" * 101,  # name too long
    ],
)
def test_invalid_repo_raises_before_network(bad_repo: str, tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    calls: list = []

    def factory():
        calls.append("CALLED")
        return _FakeConn([])

    with pytest.raises(gh.GitHubReviewError, match="invalid_repo"):
        gh.submit_review(
            bad_repo, PR, HEAD, "approve", _VALID_VERDICT,
            credentials_path=creds, allowlist=gh.ALLOW_ALL, _conn_factory=factory,
        )
    assert calls == [], "no HTTP call must happen before input validation"


@pytest.mark.parametrize(
    "bad_pr",
    [
        "1/merge",   # path segment injection
        "abc",       # non-integer string
        0,           # non-positive
        -5,          # negative
    ],
)
def test_invalid_pr_raises_before_network(bad_pr: Any, tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    calls: list = []

    def factory():
        calls.append("CALLED")
        return _FakeConn([])

    with pytest.raises(gh.GitHubReviewError, match="invalid_pr"):
        gh.submit_review(
            REPO, bad_pr, HEAD, "approve", _VALID_VERDICT,
            credentials_path=creds, allowlist=gh.ALLOW_ALL, _conn_factory=factory,
        )
    assert calls == [], "no HTTP call must happen before input validation"


# --- P2: allowlist — scope is set in the grant file ---


def test_repo_not_in_allowlist_raises(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    allowlist = frozenset({"bazfer/other-repo"})
    with pytest.raises(gh.GitHubReviewError, match="repo_not_allowlisted"):
        gh.submit_review(
            REPO, PR, HEAD, "approve", _VALID_VERDICT,
            credentials_path=creds, allowlist=allowlist,
        )


def test_repo_in_allowlist_proceeds(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    allowlist = frozenset({REPO})
    responses = [_pr_response(), _review_response()]
    factory = _conn_factory_for(responses)
    result = gh.submit_review(
        REPO, PR, HEAD, "approve", _VALID_VERDICT,
        credentials_path=creds, allowlist=allowlist, _conn_factory=factory,
    )
    assert "APPROVE" in result
    assert len(factory.conns) == 2


def test_empty_allowlist_refuses_all(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="repo_not_allowlisted"):
        gh.submit_review(
            REPO, PR, HEAD, "approve", _VALID_VERDICT,
            credentials_path=creds, allowlist=frozenset(),
        )


# --- guard 1: structured verdict ---


def test_non_dict_verdict_raises(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_not_structured"):
        gh.submit_review(
            REPO, PR, HEAD, "approve", "not a dict",
            credentials_path=creds, allowlist=gh.ALLOW_ALL,
        )


def test_missing_properties_asserted_raises(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_no_properties"):
        gh.submit_review(
            REPO, PR, HEAD, "approve",
            {"properties_asserted": [], "mutations_tried": ["m"]},
            credentials_path=creds, allowlist=gh.ALLOW_ALL,
        )


def test_missing_mutations_tried_raises(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_no_mutations"):
        gh.submit_review(
            REPO, PR, HEAD, "approve",
            {"properties_asserted": ["p"], "mutations_tried": []},
            credentials_path=creds, allowlist=gh.ALLOW_ALL,
        )


def test_bad_decision_raises(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_bad_decision"):
        gh.submit_review(
            REPO, PR, HEAD, "rubber_stamp", _VALID_VERDICT,
            credentials_path=creds, allowlist=gh.ALLOW_ALL,
        )


# --- guard 1: decision-specific fields ---


def test_request_changes_without_findings_raises(tmp_path: Path) -> None:
    """request_changes without findings is not actionable — the reviewer cannot say what is wrong."""
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_no_findings"):
        gh.submit_review(
            REPO, PR, HEAD, "request_changes", _VALID_VERDICT,
            credentials_path=creds, allowlist=gh.ALLOW_ALL,
        )


def test_request_changes_empty_findings_raises(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_no_findings"):
        gh.submit_review(
            REPO, PR, HEAD, "request_changes", {**_VALID_VERDICT, "findings": []},
            credentials_path=creds, allowlist=gh.ALLOW_ALL,
        )


@pytest.mark.parametrize("bad_finding", [
    {"evidence": "e"},                                     # missing summary
    {"summary": "", "evidence": "e"},                     # empty summary
    {"summary": "   ", "evidence": "e"},                  # whitespace-only summary
    {"summary": "s"},                                      # missing evidence
    {"summary": "s", "evidence": ""},                     # empty evidence
    {"summary": "s", "evidence": "   "},                  # whitespace-only evidence
    {"summary": "s", "evidence": "e", "location": 42},    # location not a string
    {"summary": "s", "evidence": "e", "location": ""},    # empty location
    {"summary": "s", "evidence": "e", "location": "   "}, # whitespace-only location
    "not a dict",                                          # wrong type entirely
    None,                                                  # null item
])
def test_request_changes_malformed_finding_raises(tmp_path: Path, bad_finding: Any) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_finding_malformed"):
        gh.submit_review(
            REPO, PR, HEAD, "request_changes", {**_VALID_VERDICT, "findings": [bad_finding]},
            credentials_path=creds, allowlist=gh.ALLOW_ALL,
        )


def test_request_changes_finding_location_optional(tmp_path: Path) -> None:
    """location is optional; omitting it must not raise."""
    creds = _creds_file(tmp_path)
    responses = [_pr_response(), _review_response()]
    factory = _conn_factory_for(responses)
    verdict = {**_VALID_VERDICT, "findings": [{"summary": "s", "evidence": "e"}]}
    gh.submit_review(
        REPO, PR, HEAD, "request_changes", verdict,
        credentials_path=creds, allowlist=gh.ALLOW_ALL, _conn_factory=factory,
    )


def test_insufficient_evidence_without_unverified_raises(tmp_path: Path) -> None:
    """insufficient_evidence without unverified names nothing — the verdict's whole content is the residual."""
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_no_unverified"):
        gh.submit_review(
            REPO, PR, HEAD, "insufficient_evidence", _VALID_VERDICT,
            credentials_path=creds, allowlist=gh.ALLOW_ALL,
        )


def test_insufficient_evidence_empty_unverified_raises(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_no_unverified"):
        gh.submit_review(
            REPO, PR, HEAD, "insufficient_evidence", {**_VALID_VERDICT, "unverified": []},
            credentials_path=creds, allowlist=gh.ALLOW_ALL,
        )


@pytest.mark.parametrize("bad_item", [
    "",         # empty string
    "   ",      # whitespace-only string
    None,       # null
    42,         # wrong type
])
def test_insufficient_evidence_malformed_unverified_item_raises(tmp_path: Path, bad_item: Any) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_unverified_malformed"):
        gh.submit_review(
            REPO, PR, HEAD, "insufficient_evidence",
            {**_VALID_VERDICT, "unverified": [bad_item]},
            credentials_path=creds, allowlist=gh.ALLOW_ALL,
        )


def test_findings_and_unverified_appear_in_post_body(tmp_path: Path) -> None:
    """findings surface in request_changes body; unverified surface in insufficient_evidence body."""
    creds = _creds_file(tmp_path)

    responses = [_pr_response(), _review_response()]
    factory = _conn_factory_for(responses)
    gh.submit_review(
        REPO, PR, HEAD, "request_changes", _VALID_VERDICT_REQUEST_CHANGES,
        credentials_path=creds, allowlist=gh.ALLOW_ALL, _conn_factory=factory,
    )
    body = json.loads(factory.conns[1].calls[0][2])["body"]
    assert "reload drops names on empty file" in body
    assert "clear() runs before parse" in body
    assert "src/fleet-bus.ts:312" in body

    responses2 = [_pr_response(), _review_response()]
    factory2 = _conn_factory_for(responses2)
    gh.submit_review(
        REPO, PR, HEAD, "insufficient_evidence", _VALID_VERDICT_INSUFFICIENT,
        credentials_path=creds, allowlist=gh.ALLOW_ALL, _conn_factory=factory2,
    )
    body2 = json.loads(factory2.conns[1].calls[0][2])["body"]
    assert "could not reproduce concurrency race" in body2


def test_approve_does_not_require_findings_or_unverified(tmp_path: Path) -> None:
    """approve keeps the existing shape — no extra fields needed."""
    creds = _creds_file(tmp_path)
    responses = [_pr_response(), _review_response()]
    factory = _conn_factory_for(responses)
    gh.submit_review(
        REPO, PR, HEAD, "approve", _VALID_VERDICT,
        credentials_path=creds, allowlist=gh.ALLOW_ALL, _conn_factory=factory,
    )


# --- guard 2: head moved ---


def test_head_moved_refuses(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    other_head = "0" * 40
    responses = [_pr_response(head=other_head)]
    factory = _conn_factory_for(responses)
    with pytest.raises(gh.GitHubReviewError, match="head_moved"):
        gh.submit_review(
            REPO, PR, HEAD, "approve", _VALID_VERDICT,
            credentials_path=creds, allowlist=gh.ALLOW_ALL, _conn_factory=factory,
        )
    # Exactly one request fired (the GET), no POST attempted
    assert len(factory.conns) == 1
    assert factory.conns[0].calls[0][0] == "GET"


# --- no credential ---


def test_no_github_credential_raises(tmp_path: Path) -> None:
    empty_creds = tmp_path / "empty.yaml"
    empty_creds.write_text("version: 1\ncredentials: []\n")
    empty_creds.chmod(0o600)
    with pytest.raises(gh.GitHubReviewError, match="github_no_credential"):
        gh.submit_review(
            REPO, PR, HEAD, "approve", _VALID_VERDICT,
            credentials_path=empty_creds, allowlist=gh.ALLOW_ALL,
        )


# --- positive control ---


@pytest.mark.parametrize(
    ("decision", "expected_event", "verdict"),
    [
        ("approve", "APPROVE", _VALID_VERDICT),
        ("request_changes", "REQUEST_CHANGES", _VALID_VERDICT_REQUEST_CHANGES),
        ("insufficient_evidence", "COMMENT", _VALID_VERDICT_INSUFFICIENT),
    ],
)
def test_valid_verdict_posts_exactly_once_with_correct_event(
    tmp_path: Path, decision: str, expected_event: str, verdict: dict,
) -> None:
    creds = _creds_file(tmp_path)
    responses = [_pr_response(), _review_response()]
    factory = _conn_factory_for(responses)

    result = gh.submit_review(
        REPO, PR, HEAD, decision, verdict,
        credentials_path=creds, allowlist=gh.ALLOW_ALL, _conn_factory=factory,
    )

    assert expected_event in result
    assert REPO in result
    assert HEAD in result

    # Exactly two connections: GET then POST
    assert len(factory.conns) == 2
    get_conn, post_conn = factory.conns

    assert get_conn.calls[0][0] == "GET"
    assert f"/repos/{REPO}/pulls/{PR}" in get_conn.calls[0][1]

    assert post_conn.calls[0][0] == "POST"
    assert f"/repos/{REPO}/pulls/{PR}/reviews" in post_conn.calls[0][1]

    post_body = json.loads(post_conn.calls[0][2])
    assert post_body["event"] == expected_event
    assert post_body["commit_id"] == HEAD
    # Guard 3: reviewed SHA in body
    assert HEAD in post_body["body"]


def test_post_body_contains_verdict_evidence(tmp_path: Path) -> None:
    """Body must include both asserted properties and tried mutations."""
    creds = _creds_file(tmp_path)
    verdict = {
        "properties_asserted": ["guard skips bus frames"],
        "mutations_tried": ["break instead of continue"],
    }
    responses = [_pr_response(), _review_response()]
    factory = _conn_factory_for(responses)
    gh.submit_review(
        REPO, PR, HEAD, "approve", verdict,
        credentials_path=creds, allowlist=gh.ALLOW_ALL, _conn_factory=factory,
    )

    post_body = json.loads(factory.conns[1].calls[0][2])
    assert "guard skips bus frames" in post_body["body"]
    assert "break instead of continue" in post_body["body"]
    assert "[mutation]" in post_body["body"]


def test_github_get_error_propagates(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    responses = [(403, "")]
    factory = _conn_factory_for(responses)
    with pytest.raises(gh.GitHubReviewError, match="github_get_error"):
        gh.submit_review(
            REPO, PR, HEAD, "approve", _VALID_VERDICT,
            credentials_path=creds, allowlist=gh.ALLOW_ALL, _conn_factory=factory,
        )


def test_submit_review_in_tools_registry() -> None:
    """submit_review is in the registry with the allowlist config key."""
    import tools
    tool = tools.REGISTRY["submit_review"]
    assert tool.name == "submit_review"
    required = set(tool.parameters["required"])
    assert required == {"repo", "pr", "head_sha", "decision", "verdict"}

    # Allowlist config must be declared — it is the access-scope control.
    assert "allowlist" in tool.config
    assert isinstance(tool.config["allowlist"], tools.ToolListConfig)

    # The fail-safe: empty allowlist must refuse every repo.
    # This asserts the property that matters — not just presence of the key.
    # An allow-all default would silently pass `"allowlist" in tool.config`
    # but fail here, which is exactly the gap we are preventing.
    with pytest.raises(gh.GitHubReviewError, match="repo_not_allowlisted"):
        gh.submit_review(
            "bazfer/yugo", 1,
            "abc1234def5678901234567890123456789012ab",
            "approve",
            {"properties_asserted": ["p"], "mutations_tried": ["m"]},
            credentials_path=None,  # allowlist check fires before credential load
            allowlist=frozenset(),
        )


def test_submit_review_granted_via_tools_yaml(tmp_path: Path) -> None:
    """tools.yaml can grant submit_review; the bound handler replaces the unbound one."""
    import tools

    grant = tmp_path / "tools.yaml"
    grant.write_text("version: 1\ntools:\n  submit_review: null\n")
    grant.chmod(0o644)
    creds = _creds_file(tmp_path)

    import os
    old = os.environ.get("YUGO_GITHUB_CREDENTIALS_FILE")
    os.environ["YUGO_GITHUB_CREDENTIALS_FILE"] = str(creds)
    try:
        selected = tools.resolve_declared(grant)
    finally:
        if old is None:
            os.environ.pop("YUGO_GITHUB_CREDENTIALS_FILE", None)
        else:
            os.environ["YUGO_GITHUB_CREDENTIALS_FILE"] = old

    assert "submit_review" in selected
    # Handler must have been replaced (not the unbound sentinel)
    assert selected["submit_review"].handler is not tools._submit_review_unbound

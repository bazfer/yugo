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


# --- guard 1: structured verdict ---


def test_non_dict_verdict_raises(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_not_structured"):
        gh.submit_review(REPO, PR, HEAD, "approve", "not a dict", credentials_path=creds)


def test_missing_properties_asserted_raises(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_no_properties"):
        gh.submit_review(
            REPO, PR, HEAD, "approve",
            {"properties_asserted": [], "mutations_tried": ["m"]},
            credentials_path=creds,
        )


def test_missing_mutations_tried_raises(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_no_mutations"):
        gh.submit_review(
            REPO, PR, HEAD, "approve",
            {"properties_asserted": ["p"], "mutations_tried": []},
            credentials_path=creds,
        )


def test_bad_decision_raises(tmp_path: Path) -> None:
    creds = _creds_file(tmp_path)
    with pytest.raises(gh.GitHubReviewError, match="verdict_bad_decision"):
        gh.submit_review(
            REPO, PR, HEAD, "rubber_stamp", _VALID_VERDICT, credentials_path=creds,
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
            credentials_path=creds, _conn_factory=factory,
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
        gh.submit_review(REPO, PR, HEAD, "approve", _VALID_VERDICT, credentials_path=empty_creds)


# --- positive control ---


@pytest.mark.parametrize(
    ("decision", "expected_event"),
    [
        ("approve", "APPROVE"),
        ("request_changes", "REQUEST_CHANGES"),
        ("insufficient_evidence", "COMMENT"),
    ],
)
def test_valid_verdict_posts_exactly_once_with_correct_event(
    tmp_path: Path, decision: str, expected_event: str
) -> None:
    creds = _creds_file(tmp_path)
    responses = [_pr_response(), _review_response()]
    factory = _conn_factory_for(responses)

    result = gh.submit_review(
        REPO, PR, HEAD, decision, _VALID_VERDICT,
        credentials_path=creds, _conn_factory=factory,
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
    gh.submit_review(REPO, PR, HEAD, "approve", verdict, credentials_path=creds, _conn_factory=factory)

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
            credentials_path=creds, _conn_factory=factory,
        )


def test_submit_review_in_tools_registry() -> None:
    """submit_review must be declared in the tool registry with the right schema."""
    import tools
    tool = tools.REGISTRY["submit_review"]
    assert tool.name == "submit_review"
    required = set(tool.parameters["required"])
    assert required == {"repo", "pr", "head_sha", "decision", "verdict"}
    # No configurable bounds — no operator-tunable parameters
    assert tool.config == {}


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

"""The native GitHub link is read-only, bounded and honest about limits.

The whole point of this tool is the half of GitHub it cannot do: there is no
POST, PATCH, PUT or DELETE anywhere in the module, so "never claim you changed
something" is enforced by construction rather than by instruction. These tests
pin that surface, plus the behaviours a reader has to trust:

* the token goes only in an Authorization header and never into a sentence;
* redirects are refused (so the token cannot be forwarded off-host);
* a rate limit is reported as a rate limit, with the reset time GitHub gave;
* pagination is capped, so one call cannot become an unbounded download;
* repository and ref arguments are validated and URL-quoted before any path
  is built.
"""

from __future__ import annotations

import json

import pytest

from capabilities import github

TOKEN = "ghp_averyrealtokenusedonlyintests1234"


class _Response:
    def __init__(self, status: int = 200, payload=None, headers=None, raw=None):
        self.status_code = status
        self._payload = payload
        self.headers = dict(headers or {})
        if raw is not None:
            self.content = raw
        elif payload is None:
            self.content = b""
        else:
            self.content = json.dumps(payload).encode("utf-8")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Api:
    """A little in-memory GitHub API that records every request it sees."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        assert kwargs.get("allow_redirects") is False, "redirects must be refused"
        self.calls.append((url, dict(kwargs)))
        params = dict(kwargs.get("params") or {})
        path = url.replace("https://api.github.com", "")
        if path == "/user":
            return _Response(payload={"login": "octocat"},
                             headers={"x-ratelimit-remaining": "4999",
                                      "x-ratelimit-limit": "5000"})
        if path == "/user/repos":
            return _Response(payload=[
                {"full_name": "octocat/hello", "private": False,
                 "description": "A demo"},
                {"full_name": "octocat/secret", "private": True,
                 "description": ""},
            ])
        if path == "/notifications":
            return _Response(payload=[
                {"repository": {"full_name": "octocat/hello"},
                 "subject": {"title": "Review requested", "type": "PullRequest"},
                 "reason": "review_requested"},
            ])
        if path == "/repos/octocat/hello/issues":
            items = [
                {"number": 1, "title": "Bug", "state": "open",
                 "user": {"login": "octocat"}, "comments": 2,
                 "labels": [{"name": "bug"}],
                 "html_url": "https://github.com/octocat/hello/issues/1"},
                {"number": 2, "title": "A PR in the issue list", "state": "open",
                 "user": {"login": "octocat"}, "comments": 0,
                 "pull_request": {"url": "..."}, "labels": [],
                 "html_url": "https://github.com/octocat/hello/pull/2"},
            ]
            return _Response(payload=items)
        if path == "/repos/octocat/hello/issues/1":
            return _Response(payload={"number": 1, "title": "Bug", "state": "open",
                                      "user": {"login": "octocat"},
                                      "comments": 1, "labels": [],
                                      "body": "Something is broken."})
        if path == "/repos/octocat/hello/issues/1/comments":
            return _Response(payload=[{"user": {"login": "hubber"},
                                       "body": "I can reproduce."}])
        if path == "/repos/octocat/hello/pulls":
            return _Response(payload=[
                {"number": 7, "title": "Fix the bug", "state": "open",
                 "draft": True, "user": {"login": "octocat"},
                 "head": {"ref": "fix/bug", "sha": "abc123"},
                 "base": {"ref": "main"},
                 "html_url": "https://github.com/octocat/hello/pull/7"},
            ])
        if path == "/repos/octocat/hello/pulls/7":
            return _Response(payload={
                "number": 7, "title": "Fix the bug", "state": "open",
                "draft": True, "user": {"login": "octocat"},
                "head": {"ref": "fix/bug", "sha": "abc123"},
                "base": {"ref": "main"},
                "body": "This fixes it.",
                "html_url": "https://github.com/octocat/hello/pull/7"})
        if path == "/repos/octocat/hello/pulls/7/files":
            return _Response(payload=[
                {"filename": "src/app.py", "additions": 4, "deletions": 1},
            ])
        if path == "/repos/octocat/hello/actions/runs":
            return _Response(payload={"workflow_runs": [
                {"name": "CI", "status": "completed", "conclusion": "success",
                 "head_branch": "main", "event": "push",
                 "html_url": "https://github.com/octocat/hello/actions/runs/5"},
            ]})
        if path.endswith("/check-runs"):
            return _Response(payload={"check_runs": [
                {"name": "tests", "status": "completed", "conclusion": "success"},
            ]})
        if path.endswith("/status"):
            return _Response(payload={"state": "success", "total_count": 1})
        if path == "/search/issues":
            return _Response(payload={"items": [
                {"number": 3, "title": "login bug", "state": "open",
                 "user": {"login": "octocat"}, "comments": 0,
                 "labels": [], "html_url": "https://github.com/x/y/issues/3"},
            ]})
        if path == "/search/repositories":
            return _Response(payload={"items": [
                {"full_name": "octocat/hello", "description": "A demo"},
            ]})
        if path == "/search/code":
            return _Response(payload={"items": [
                {"repository": {"full_name": "octocat/hello"},
                 "path": "src/app.py"},
            ]})
        return _Response(status=404, payload={"message": "Not Found"})


@pytest.fixture()
def api(monkeypatch):
    import requests

    fake = _Api()
    monkeypatch.setattr(github, "_config",
                        lambda: {"github_token": TOKEN,
                                 "github_api_url": "https://api.github.com"})
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: pytest.fail("GitHub is read-only"))
    monkeypatch.setattr(requests, "patch",
                        lambda *a, **k: pytest.fail("GitHub is read-only"))
    monkeypatch.setattr(requests, "delete",
                        lambda *a, **k: pytest.fail("GitHub is read-only"))
    return fake


def _last(api: _Api) -> tuple[str, dict]:
    return api.calls[-1]


# ── the read-only surface ─────────────────────────────────────────────────────

def test_the_module_has_no_write_method_at_all():
    for name in ("_post", "_put", "_patch", "_delete", "request"):
        assert not hasattr(github, name), f"github must not expose {name}"


def test_no_action_can_become_a_write(api):
    for action, args in (("status", {}), ("repos", {}), ("notifications", {}),
                         ("issues", {"repo": "octocat/hello"}),
                         ("pulls", {"repo": "octocat/hello"}),
                         ("workflow_runs", {"repo": "octocat/hello"}),
                         ("search", {"query": "bug"})):
        result = github.run(action, args)
        assert result["ok"], result["detail"]
    assert api.calls, "the reads really happened"


# ── reads ─────────────────────────────────────────────────────────────────────

def test_status_reports_the_identity_and_the_rate_limit(api):
    result = github.run("status", {})
    assert result["ok"]
    assert "octocat" in result["detail"]
    assert "4999" in result["detail"] and "5000" in result["detail"]


def test_repos_marks_private_repositories(api):
    result = github.run("repos", {})
    assert result["ok"]
    assert "octocat/hello (public)" in result["detail"]
    assert "octocat/secret (private)" in result["detail"]


def test_the_issue_list_hides_pull_requests_unless_asked(api):
    result = github.run("issues", {"repo": "octocat/hello"})
    assert result["ok"]
    assert "#1" in result["detail"]
    assert "#2" not in result["detail"]

    with_pulls = github.run("issues", {"repo": "octocat/hello",
                                       "include_pulls": True})
    assert with_pulls["ok"]
    assert "#2" in with_pulls["detail"]


def test_issue_filters_are_forwarded_to_github(api):
    github.run("issues", {"repo": "octocat/hello", "state": "closed",
                          "labels": "bug,urgent", "assignee": "octocat"})
    _, call = _last(api)
    assert call["params"]["state"] == "closed"
    assert call["params"]["labels"] == "bug,urgent"
    assert call["params"]["assignee"] == "octocat"


def test_one_issue_includes_the_body_and_comments(api):
    result = github.run("issue", {"repo": "octocat/hello", "number": 1})
    assert result["ok"]
    assert "Something is broken." in result["detail"]
    assert "@hubber" in result["detail"]


def test_a_pull_is_reported_with_files_and_checks(api):
    result = github.run("pull", {"repo": "octocat/hello", "number": 7})
    assert result["ok"], result["detail"]
    assert "fix/bug → main" in result["detail"]
    assert "tests: completed / success" in result["detail"]


def test_checks_url_quote_the_ref(api):
    result = github.run("checks", {"repo": "octocat/hello",
                                   "ref": "feature/new thing"})
    assert result["ok"], result["detail"]
    urls = " ".join(url for url, _ in api.calls)
    assert "feature%2Fnew%20thing" in urls
    assert "feature/new thing" not in urls


def test_search_covers_issues_repositories_and_code(api):
    assert "login bug" in github.run("search", {"query": "login"})["detail"]
    assert "octocat/hello" in github.run(
        "search", {"query": "hello", "search_type": "repos"})["detail"]
    assert "src/app.py" in github.run(
        "search", {"query": "app", "search_type": "code"})["detail"]


# ── bounded and validated ────────────────────────────────────────────────────

def test_pagination_is_capped(api):
    github.run("repos", {"limit": 9999, "page": 3})
    _, call = _last(api)
    assert call["params"]["per_page"] == github._MAX_LIMIT
    assert call["params"]["page"] == 3


def test_a_repository_can_be_given_as_a_url(api):
    result = github.run("issues", {"repo": "https://github.com/octocat/hello.git"})
    assert result["ok"]
    assert "/repos/octocat/hello/issues" in _last(api)[0]


@pytest.mark.parametrize("repo", ["hello", "octocat/hel lo", "../etc/passwd",
                                  "octocat/hello/extra"])
def test_a_repository_that_is_not_owner_name_is_refused(repo, api):
    result = github.run("issues", {"repo": repo})
    assert result["ok"] is False
    assert result["reason"] in ("bad_repo",)
    assert "owner/name" in result["detail"]


def test_a_missing_or_bad_number_is_refused(api):
    result = github.run("issue", {"repo": "octocat/hello", "number": 0})
    assert result["ok"] is False and result["reason"] == "bad_number"


# ── failures stay honest ─────────────────────────────────────────────────────

def test_a_rate_limit_is_reported_as_a_rate_limit(api, monkeypatch):
    import requests

    def limited(url, **kwargs):
        return _Response(status=403, headers={"x-ratelimit-reset": "1900000000"},
                         payload={"message": "API rate limit exceeded for user"})

    monkeypatch.setattr(requests, "get", limited)
    result = github.run("status", {})
    assert result["ok"] is False
    assert result["reason"] == "rate_limited"
    assert "limit resets" in result["detail"]


def test_http_429_is_also_a_rate_limit(api, monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _Response(status=429,
                                                  payload={"message": "slow down"}))
    result = github.run("status", {})
    assert result["reason"] == "rate_limited"


def test_a_rejected_token_names_401_and_never_the_token(api, monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _Response(status=401,
                                                  payload={"message": "Bad credentials"}))
    result = github.run("status", {})
    assert result["ok"] is False
    assert result["reason"] == "auth_failed"
    assert TOKEN not in json.dumps(result)


def test_a_404_says_what_was_not_found(api, monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _Response(status=404,
                                                  payload={"message": "Not Found"}))
    result = github.run("issues", {"repo": "octocat/hello"})
    assert result["ok"] is False
    assert result["reason"] == "not_found"


def test_without_a_token_nothing_is_sent(monkeypatch):
    import requests

    monkeypatch.setattr(github, "_config", lambda: {"github_token": "",
                                                    "github_api_url": ""})
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: pytest.fail("no request may go out"))
    result = github.run("status", {})
    assert result["ok"] is False
    assert result["reason"] == "github_not_configured"
    assert "read-only" in result["detail"]


def test_a_token_with_line_breaks_is_treated_as_pasted_text(monkeypatch):
    import requests

    monkeypatch.setattr(github, "_config",
                        lambda: {"github_token": "line one\nline two",
                                 "github_api_url": ""})
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: pytest.fail("no request may go out"))
    result = github.run("status", {})
    assert result["ok"] is False
    assert result["reason"] == "bad_config"


def test_an_unknown_action_lists_the_real_ones(api):
    result = github.run("merge", {"repo": "octocat/hello"})
    assert result["ok"] is False
    assert result["reason"] == "unknown_action"
    assert "issues" in result["detail"]

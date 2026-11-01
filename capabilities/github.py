"""Read-only GitHub intelligence over the REST API.

The whole module has exactly one HTTP entry point, :func:`_get`, and it only
issues ``GET`` requests. There is no generic request helper, no method argument
and no write path to disable: creating an issue, commenting, merging or pushing
is not an action this tool can perform. The Composio connector remains the
write-capable path.

The token is read from ``config/api_keys.json`` and sent only in an
``Authorization`` header to the configured API host. It is never placed in a
URL, a result sentence or an error. Responses are bounded, redirects are
refused, and pagination is capped so a large organisation cannot turn one tool
call into an unbounded download.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

__all__ = ["run", "status"]

_API_DEFAULT = "https://api.github.com"
_TIMEOUT = 12.0
_MAX_BYTES = 4 * 1024 * 1024
_MAX_TEXT = 8000
_MAX_LIMIT = 50
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubError(Exception):
    """A real GitHub failure with a sentence the model may repeat."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _config() -> dict:
    try:
        from config.settings import get
        return {key: get(key, "") for key in ("github_token", "github_api_url")}
    except Exception:
        try:
            from memory.config_manager import load_api_keys
            data = load_api_keys()
            return dict(data) if isinstance(data, dict) else {}
        except Exception:
            return {}


def _token(config: dict | None = None) -> str:
    data = _config() if config is None else config
    return str(data.get("github_token") or "").strip()


def _base(config: dict | None = None) -> str:
    data = _config() if config is None else config
    base = str(data.get("github_api_url") or _API_DEFAULT).strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise GitHubError("bad_config",
                          "The GitHub API base URL must start with http:// or "
                          "https://.")
    return base


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": _ACCEPT,
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": "JarvisOS",
    }


def _reset_text(headers: Any) -> str:
    raw = str((headers or {}).get("x-ratelimit-reset") or "").strip()
    if not raw:
        return ""
    try:
        when = datetime.fromtimestamp(int(raw)).strftime("%Y-%m-%d %H:%M")
        return f" The limit resets at {when}."
    except Exception:
        return ""


def _get(path: str, params: dict | None = None,
         extra_headers: dict | None = None) -> tuple[Any, Any]:
    """The only HTTP call in this module. Always a GET.

    Returns ``(json, headers)`` or raises :class:`GitHubError`. The method is
    hardcoded; callers cannot turn this into a write.
    """
    token = _token()
    if not token:
        raise GitHubError(
            "github_not_configured",
            "GitHub is not configured yet. Add a read-only personal access "
            "token in Settings ▸ API ▸ GitHub (or set GITHUB_TOKEN) and try "
            "again. Nothing was read from GitHub.")
    if any(ch.isspace() for ch in token):
        raise GitHubError("bad_config",
                          "The stored GitHub token contains spaces or line "
                          "breaks, so it is pasted text rather than a token. "
                          "Nothing was sent to GitHub.")

    url = _base() + (path if path.startswith("/") else "/" + path)
    headers = _headers(token)
    if extra_headers:
        headers.update({str(k): str(v) for k, v in extra_headers.items()})
    try:
        import requests
        resp = requests.get(url, headers=headers, params=params or None,
                            timeout=_TIMEOUT, allow_redirects=False)
    except Exception as exc:
        raise GitHubError(
            "network_error",
            f"GitHub did not answer ({type(exc).__name__}: {exc}). Nothing "
            "was changed.") from exc

    status = int(getattr(resp, "status_code", 0) or 0)
    if len(getattr(resp, "content", b"") or b"") > _MAX_BYTES:
        raise GitHubError("response_too_large",
                          "GitHub returned more data than this tool will read. "
                          "Narrow the request (a repository, a search, a smaller "
                          "page) and try again.")
    if status == 401:
        raise GitHubError("auth_failed",
                          "GitHub rejected the token (HTTP 401). Check that it "
                          "is valid and has not expired. Nothing was changed.")
    if status in (403, 429):
        message = ""
        try:
            message = str((resp.json() or {}).get("message") or "")
        except Exception:
            message = ""
        detail = ("GitHub refused the request (HTTP "
                  f"{status})" + (f": {message}" if message else ".") +
                  _reset_text(getattr(resp, "headers", {})))
        raise GitHubError("rate_limited" if status == 429 or "rate limit" in
                          message.lower() else "forbidden", detail)
    if status == 404:
        raise GitHubError("not_found",
                          "GitHub returned 404: the repository, issue or pull "
                          "request was not found, or this token cannot see it.")
    if status >= 400:
        message = ""
        try:
            message = str((resp.json() or {}).get("message") or "")
        except Exception:
            message = ""
        raise GitHubError("github_error",
                          f"GitHub returned HTTP {status}"
                          + (f": {message}" if message else "") + ".")
    try:
        return resp.json(), getattr(resp, "headers", {})
    except Exception as exc:
        raise GitHubError("bad_response",
                          "GitHub answered, but the response was not JSON.") from exc


def _ok(detail: str, **evidence: Any) -> dict:
    return {"ok": True, "controller": "github", "detail": detail,
            "reason": "", "evidence": evidence}


def _fail(detail: str, reason: str = "github_error") -> dict:
    return {"ok": False, "controller": "github", "detail": detail,
            "reason": reason, "evidence": {}}


def _repo(args: dict) -> str:
    raw = str(args.get("repo") or "").strip()
    if raw.startswith(("https://github.com/", "http://github.com/")):
        raw = raw.split("github.com/", 1)[1]
    raw = raw.strip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    if not _REPO_RE.match(raw):
        raise GitHubError("bad_repo",
                          "Give the repository as 'owner/name' (for example "
                          "'microsoft/markitdown').")
    return raw


def _number(args: dict) -> int:
    try:
        value = int(args.get("number"))
    except (TypeError, ValueError):
        raise GitHubError("bad_number",
                          "An issue or pull request needs a numeric 'number'.")
    if value <= 0:
        raise GitHubError("bad_number", "The issue/PR number must be positive.")
    return value


def _limit(args: dict, default: int = 20) -> int:
    try:
        value = int(args.get("limit") or default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(_MAX_LIMIT, value))


def _page(args: dict) -> int:
    try:
        value = int(args.get("page") or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, value)


def _short(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _body(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "\n… (truncated)"
    return text


def _label_names(item: dict) -> str:
    labels = item.get("labels") or []
    names = []
    for label in labels:
        if isinstance(label, dict):
            name = str(label.get("name") or "").strip()
        else:
            name = str(label or "").strip()
        if name:
            names.append(name)
    return ", ".join(names)


def _issue_line(item: dict) -> str:
    number = item.get("number")
    title = _short(item.get("title"), 120)
    state = str(item.get("state") or "?").upper()
    user = str((item.get("user") or {}).get("login") or "?")
    comments = int(item.get("comments") or 0)
    kind = "PR" if item.get("pull_request") else "issue"
    labels = _label_names(item)
    bits = [f"#{number} [{state}] {kind}: {title}", f"by @{user}",
            f"{comments} comment(s)"]
    if labels:
        bits.append(f"labels: {labels}")
    url = str(item.get("html_url") or "")
    if url:
        bits.append(url)
    return " — ".join(bits)


def _pull_line(item: dict) -> str:
    number = item.get("number")
    title = _short(item.get("title"), 120)
    state = str(item.get("state") or "?").upper()
    draft = " draft" if item.get("draft") else ""
    user = str((item.get("user") or {}).get("login") or "?")
    head = str((item.get("head") or {}).get("ref") or "?")
    base = str((item.get("base") or {}).get("ref") or "?")
    url = str(item.get("html_url") or "")
    line = (f"#{number} [{state}{draft}] {title} — by @{user}, "
            f"{head} → {base}")
    return line + (f" — {url}" if url else "")


def _h_status(args: dict) -> dict:
    del args
    user, headers = _get("/user")
    login = str((user or {}).get("login") or "the token owner")
    remaining = str((headers or {}).get("x-ratelimit-remaining") or "?")
    limit = str((headers or {}).get("x-ratelimit-limit") or "?")
    reset = _reset_text(headers).strip()
    detail = (f"Signed in to GitHub as {login}. Core rate limit: {remaining} "
              f"of {limit} remaining.")
    if reset:
        detail += f" {reset}"
    return _ok(detail, login=login, remaining=remaining)


def _h_repos(args: dict) -> dict:
    limit = _limit(args)
    data, _ = _get("/user/repos",
                   {"sort": "updated", "direction": "desc",
                    "per_page": limit, "page": _page(args)})
    items = list(data or [])
    if not items:
        return _ok("No repositories are visible to this token.", count=0)
    lines = []
    for item in items[:limit]:
        name = str(item.get("full_name") or "?")
        private = "private" if item.get("private") else "public"
        desc = _short(item.get("description"), 100)
        lines.append(f"{name} ({private})" + (f" — {desc}" if desc else ""))
    return _ok(f"{len(lines)} repository/repositories:\n" + "\n".join(lines),
               count=len(lines))


def _h_notifications(args: dict) -> dict:
    limit = _limit(args)
    data, _ = _get("/notifications",
                   {"all": "false", "per_page": limit, "page": _page(args)})
    items = list(data or [])
    if not items:
        return _ok("No unread GitHub notifications.", count=0)
    lines = []
    for item in items[:limit]:
        repo = str((item.get("repository") or {}).get("full_name") or "?")
        subject = item.get("subject") or {}
        title = _short(subject.get("title"), 120)
        kind = str(subject.get("type") or "?")
        reason = str(item.get("reason") or "")
        lines.append(f"{repo} — [{kind}] {title}" +
                     (f" ({reason})" if reason else ""))
    return _ok(f"{len(lines)} unread notification(s):\n" + "\n".join(lines),
               count=len(lines))


def _h_issues(args: dict) -> dict:
    repo = _repo(args)
    limit = _limit(args)
    params: dict[str, Any] = {"per_page": limit, "page": _page(args)}
    state = str(args.get("state") or "").strip().lower()
    if state in ("open", "closed", "all"):
        params["state"] = state
    labels = str(args.get("labels") or "").strip()
    if labels:
        params["labels"] = labels
    assignee = str(args.get("assignee") or "").strip()
    if assignee:
        params["assignee"] = assignee
    data, _ = _get(f"/repos/{repo}/issues", params)
    items = list(data or [])
    include_pulls = bool(args.get("include_pulls"))
    if not include_pulls:
        items = [i for i in items if not i.get("pull_request")]
    if not items:
        return _ok(f"No issues matched in {repo}.", repo=repo, count=0)
    lines = [_issue_line(item) for item in items[:limit]]
    return _ok(f"{len(lines)} issue(s) in {repo}:\n" + "\n".join(lines),
               repo=repo, count=len(lines))


def _h_issue(args: dict) -> dict:
    repo = _repo(args)
    number = _number(args)
    item, _ = _get(f"/repos/{repo}/issues/{number}")
    comments, _ = _get(f"/repos/{repo}/issues/{number}/comments",
                       {"per_page": 20})
    lines = [_issue_line(item or {}), "",
             _body((item or {}).get("body")) or "(no body)"]
    comment_items = list(comments or [])
    if comment_items:
        lines.append("")
        lines.append(f"Comments ({len(comment_items)}):")
        for comment in comment_items[:20]:
            user = str((comment.get("user") or {}).get("login") or "?")
            lines.append(f"@{user}: {_body(comment.get('body'))}")
    return _ok("\n".join(lines), repo=repo, number=number,
               comments=len(comment_items))


def _h_pulls(args: dict) -> dict:
    repo = _repo(args)
    limit = _limit(args)
    params: dict[str, Any] = {"per_page": limit, "page": _page(args)}
    state = str(args.get("state") or "").strip().lower()
    if state in ("open", "closed", "all"):
        params["state"] = state
    data, _ = _get(f"/repos/{repo}/pulls", params)
    items = list(data or [])
    if not items:
        return _ok(f"No pull requests matched in {repo}.", repo=repo, count=0)
    lines = [_pull_line(item) for item in items[:limit]]
    return _ok(f"{len(lines)} pull request(s) in {repo}:\n" + "\n".join(lines),
               repo=repo, count=len(lines))


def _h_pull(args: dict) -> dict:
    repo = _repo(args)
    number = _number(args)
    item, _ = _get(f"/repos/{repo}/pulls/{number}")
    files, _ = _get(f"/repos/{repo}/pulls/{number}/files",
                    {"per_page": 30})
    lines = [_pull_line(item or {}), "",
             _body((item or {}).get("body")) or "(no body)"]
    changed = list(files or [])
    if changed:
        lines.append("")
        lines.append(f"Changed files ({len(changed)}):")
        for file in changed[:30]:
            name = str(file.get("filename") or "?")
            adds = int(file.get("additions") or 0)
            dels = int(file.get("deletions") or 0)
            lines.append(f"{name} (+{adds}/-{dels})")
    sha = str(((item or {}).get("head") or {}).get("sha") or "")
    if sha:
        check_text = _checks_for(repo, sha)
        if check_text:
            lines.extend(["", check_text])
    return _ok("\n".join(lines), repo=repo, number=number,
               files=len(changed))


def _checks_for(repo: str, ref: str) -> str:
    """Check runs and combined status for one ref, best-effort but honest."""
    encoded = quote(ref, safe="")
    parts: list[str] = []
    try:
        runs, _ = _get(f"/repos/{repo}/commits/{encoded}/check-runs",
                       {"per_page": 30},
                       {"Accept": "application/vnd.github+json"})
        items = list((runs or {}).get("check_runs") or [])
        if items:
            parts.append(f"Check runs ({len(items)}):")
            for run in items[:30]:
                name = str(run.get("name") or "?")
                status = str(run.get("status") or "?")
                conclusion = str(run.get("conclusion") or "")
                parts.append(f"- {name}: {status}" +
                             (f" / {conclusion}" if conclusion else ""))
    except GitHubError as exc:
        parts.append(f"Check runs unavailable: {exc.detail}")
    try:
        status, _ = _get(f"/repos/{repo}/commits/{encoded}/status")
        state = str((status or {}).get("state") or "")
        total = int((status or {}).get("total_count") or 0)
        if state:
            parts.append(f"Combined commit status: {state} ({total} context(s)).")
    except GitHubError:
        pass
    return "\n".join(parts)


def _h_checks(args: dict) -> dict:
    repo = _repo(args)
    ref = str(args.get("ref") or "").strip()
    if not ref:
        raise GitHubError("bad_ref", "Checks need a branch, tag or commit SHA.")
    text = _checks_for(repo, ref)
    if not text:
        return _ok(f"No checks were reported for {ref} in {repo}.",
                   repo=repo, ref=ref)
    return _ok(f"CI for {ref} in {repo}:\n{text}", repo=repo, ref=ref)


def _h_workflow_runs(args: dict) -> dict:
    repo = _repo(args)
    limit = _limit(args)
    data, _ = _get(f"/repos/{repo}/actions/runs",
                   {"per_page": limit, "page": _page(args)})
    items = list((data or {}).get("workflow_runs") or [])
    if not items:
        return _ok(f"No workflow runs were returned for {repo}.",
                   repo=repo, count=0)
    lines = []
    for run in items[:limit]:
        name = str(run.get("name") or "workflow")
        status = str(run.get("status") or "?")
        conclusion = str(run.get("conclusion") or "")
        branch = str(run.get("head_branch") or "?")
        event = str(run.get("event") or "?")
        url = str(run.get("html_url") or "")
        lines.append(f"{name} [{status}" +
                     (f"/{conclusion}" if conclusion else "") +
                     f"] on {branch} ({event})" + (f" — {url}" if url else ""))
    return _ok(f"{len(lines)} workflow run(s) in {repo}:\n" + "\n".join(lines),
               repo=repo, count=len(lines))


def _h_search(args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise GitHubError("bad_query", "Search needs a query.")
    kind = str(args.get("search_type") or "issues").strip().lower()
    limit = _limit(args)
    params = {"q": query[:256], "per_page": limit, "page": _page(args)}
    if kind in ("code",):
        data, _ = _get("/search/code", params,
                       {"Accept": "application/vnd.github.text-match+json"})
        items = list((data or {}).get("items") or [])
        lines = [f"{((item.get('repository') or {}).get('full_name') or '?')} — "
                 f"{item.get('path', '?')}" for item in items[:limit]]
        return _ok(f"{len(lines)} code result(s):\n" + "\n".join(lines),
                   count=len(lines), search_type="code")
    if kind in ("repos", "repositories"):
        data, _ = _get("/search/repositories", params)
        items = list((data or {}).get("items") or [])
        lines = [f"{item.get('full_name', '?')} — "
                 f"{_short(item.get('description'), 100)}"
                 for item in items[:limit]]
        return _ok(f"{len(lines)} repository result(s):\n" + "\n".join(lines),
                   count=len(lines), search_type="repos")
    data, _ = _get("/search/issues", params)
    items = list((data or {}).get("items") or [])
    if not items:
        return _ok(f"No issues or pull requests matched '{query}'.",
                   count=0, search_type="issues")
    lines = [_issue_line(item) for item in items[:limit]]
    return _ok(f"{len(lines)} result(s) for '{query}':\n" + "\n".join(lines),
               count=len(lines), search_type="issues")


_HANDLERS = {
    "status": _h_status,
    "repos": _h_repos,
    "notifications": _h_notifications,
    "issues": _h_issues,
    "issue": _h_issue,
    "pulls": _h_pulls,
    "pull": _h_pull,
    "checks": _h_checks,
    "workflow_runs": _h_workflow_runs,
    "search": _h_search,
}


def status() -> dict:
    """Configuration shape without spending a request (used by diagnostics)."""
    token = _token()
    base = ""
    if token:
        try:
            base = _base()
        except GitHubError:
            base = ""
    return {"configured": bool(token), "base": base,
            "detail": ("A GitHub token is configured." if token else
                       "No GitHub token is configured.")}


def run(action_name: str, args: dict) -> dict:
    """Run one read-only GitHub action; never raises."""
    handler = _HANDLERS.get(str(action_name or "").strip())
    if handler is None:
        return _fail(f"'{action_name}' is not a GitHub action. Valid actions: "
                     f"{', '.join(_HANDLERS)}. Nothing was read.",
                     "unknown_action")
    try:
        return handler(dict(args or {}))
    except GitHubError as exc:
        return _fail(exc.detail, exc.reason)
    except Exception as exc:
        return _fail(f"GitHub action failed ({type(exc).__name__}: {exc}). "
                     "Nothing was changed.", "github_error")

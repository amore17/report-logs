"""Match CI failures to Pagure.io FreeIPA tracker issues (public JSON API)."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from report_logs.jira_child_issues import failure_match_tokens

_CACHE_TTL_SEC = 300.0
_cache_key_to_rows: dict[str, tuple[float, list[tuple[int, str]]]] = {}

_PAGURE_REPO_DEFAULT = "freeipa"
_API_LIST_TMPL = "https://pagure.io/api/0/{repo}/issues"
_API_ISSUE_COMMENT_TMPL = "https://pagure.io/api/0/{repo}/issue/{id}/comment"
_ISSUE_UI_TMPL = "https://pagure.io/{repo}/issue/{id}"


def pagure_freeipa_issue_url(issue_id: int, *, repo: str | None = None) -> str:
    r = (repo or os.environ.get("REPORT_LOGS_PAGURE_REPO", _PAGURE_REPO_DEFAULT)).strip() or _PAGURE_REPO_DEFAULT
    return _ISSUE_UI_TMPL.format(repo=r, id=issue_id)


def pagure_known_issue_markdown(issue_id: int, *, repo: str | None = None) -> str:
    """Markdown link label ``freeipa#<id>`` pointing at the Pagure issue page."""
    r = (repo or os.environ.get("REPORT_LOGS_PAGURE_REPO", _PAGURE_REPO_DEFAULT)).strip() or _PAGURE_REPO_DEFAULT
    url = pagure_freeipa_issue_url(issue_id, repo=r)
    return f"[freeipa#{issue_id}]({url})"


def pagure_fetch_wanted() -> bool:
    """True unless ``REPORT_LOGS_PAGURE_FETCH`` is ``0`` / false (default: on for direct Pagure helpers)."""
    return os.environ.get("REPORT_LOGS_PAGURE_FETCH", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def pagure_comments_fetch_wanted() -> bool:
    """Include Pagure issue thread comments in the haystack unless ``REPORT_LOGS_PAGURE_FETCH_COMMENTS`` is off."""
    return os.environ.get("REPORT_LOGS_PAGURE_FETCH_COMMENTS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _pagure_fetch_enabled() -> bool:
    return pagure_fetch_wanted()


def _http_get_json(url: str, *, timeout: float = 60.0) -> dict[str, Any] | None:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


_ISSUE_TITLE_SUITE_RE = re.compile(
    r"\b(?:upstream-[a-z0-9]+(?:-[a-z0-9]+)*)\b",
    re.IGNORECASE,
)


def _pagure_issue_comments_blob(repo: str, issue_id: int) -> str:
    """Plain text from all Pagure issue comments (paginated API), or empty on failure."""
    parts: list[str] = []
    r = urllib.parse.quote(repo, safe="")
    url: str | None = _API_ISSUE_COMMENT_TMPL.format(repo=r, id=issue_id)
    while url:
        data = _http_get_json(url)
        if not data:
            break
        for c in data.get("comments") or []:
            if not isinstance(c, dict):
                continue
            t = (c.get("comment") or c.get("content") or "").strip()
            if t:
                parts.append(t)
        pag = data.get("pagination") or {}
        url = pag.get("next") if isinstance(pag, dict) else None
    return "\n".join(parts)


def _issue_text_blob(
    issue: dict[str, Any],
    *,
    repo: str,
    issue_id: int,
) -> str:
    """Lowercase haystack: title + body + suite-like tokens from title + optional thread comments."""
    title = (issue.get("title") or "").strip()
    content = (issue.get("content") or "").strip()
    parts = [title, content]
    extra = []
    for m in _ISSUE_TITLE_SUITE_RE.finditer(title):
        extra.append(m.group(0))
    blob = "\n".join(parts + extra)
    if pagure_comments_fetch_wanted():
        cmt = _pagure_issue_comments_blob(repo, issue_id).strip()
        if cmt:
            blob = f"{blob}\n{cmt}"
    return blob.lower()


def collect_open_pagure_issues_for_matching(
    *,
    force_refresh: bool = False,
) -> list[tuple[int, str]]:
    """
    Open Pagure issues tagged for CI/test failures, as ``(id, lowercase haystack)``.

    Uses ``REPORT_LOGS_PAGURE_REPO`` (default ``freeipa``), ``REPORT_LOGS_PAGURE_TAGS``
    (default ``test-failure``), ``REPORT_LOGS_PAGURE_PER_PAGE`` (default ``100``),
    ``REPORT_LOGS_PAGURE_MAX_PAGES`` (default ``3``). Cached ~5 minutes.

    On network/API failure returns ``[]``.
    """
    if not _pagure_fetch_enabled():
        return []

    repo = (os.environ.get("REPORT_LOGS_PAGURE_REPO", _PAGURE_REPO_DEFAULT).strip() or _PAGURE_REPO_DEFAULT)
    tags_raw = (os.environ.get("REPORT_LOGS_PAGURE_TAGS", "test-failure").strip() or "test-failure")
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    try:
        per_page = max(10, min(100, int(os.environ.get("REPORT_LOGS_PAGURE_PER_PAGE", "100"))))
    except ValueError:
        per_page = 100
    try:
        max_pages = max(1, min(20, int(os.environ.get("REPORT_LOGS_PAGURE_MAX_PAGES", "3"))))
    except ValueError:
        max_pages = 3

    cache_key = f"{repo}|{','.join(tags)}|{per_page}|{max_pages}"
    now = time.monotonic()
    if not force_refresh and cache_key in _cache_key_to_rows:
        exp, rows = _cache_key_to_rows[cache_key]
        if now < exp:
            return rows

    rows_out: list[tuple[int, str]] = []
    url: str | None = None
    base_list = _API_LIST_TMPL.format(repo=urllib.parse.quote(repo, safe=""))
    first_params = urllib.parse.urlencode(
        {
            "tags": tags,
            "status": "Open",
            "per_page": per_page,
        },
        doseq=True,
    )
    url = f"{base_list}?{first_params}"

    for _ in range(max_pages):
        if not url:
            break
        data = _http_get_json(url)
        if not data or "issues" not in data:
            break
        for iss in data["issues"]:
            if not isinstance(iss, dict):
                continue
            iid = iss.get("id")
            if not isinstance(iid, int):
                try:
                    iid = int(iid)
                except (TypeError, ValueError):
                    continue
            hay = _issue_text_blob(iss, repo=repo, issue_id=iid)
            if hay:
                rows_out.append((iid, hay))
        pag = data.get("pagination") or {}
        url = pag.get("next") if isinstance(pag, dict) else None
        if not url:
            break

    _cache_key_to_rows[cache_key] = (now + _CACHE_TTL_SEC, rows_out)
    return rows_out


def failure_matching_pagure_issue_id(
    suite_name: str,
    classname: str,
    test_name: str,
    pagure_rows: list[tuple[int, str]],
) -> int | None:
    """
    First Pagure issue whose title, body, or **thread comments** contain a failure token
    (same token rules as Jira children; see :func:`~report_logs.jira_child_issues.failure_match_tokens`).

    *pagure_rows* is typically from :func:`collect_open_pagure_issues_for_matching`.
    """
    if not pagure_rows:
        return None
    tokens = failure_match_tokens(suite_name, classname, test_name)
    for issue_id, hay in pagure_rows:
        for t in tokens:
            if t and t in hay:
                return issue_id
    return None

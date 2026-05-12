"""Load text from Jira child work items of a parent issue (e.g. IDM-5601) for known-issue matching."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Simple time-based cache: (monotonic_expiry, list of (issue key, haystack, summary lower))
_cache: dict[str, tuple[float, list[tuple[str, str, str]]]] = {}
_CACHE_TTL_SEC = 300.0

# JQL umbrella scope + suite needles (short TTL; one lookup per distinct suite+scope per run).
_jql_summary_cache: dict[tuple[str, str, str, str, str], tuple[float, str | None]] = {}
_JQL_SUMMARY_CACHE_TTL_SEC = 60.0

_PARENT_KEY_JQL_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


def jira_rest_credentials() -> tuple[str, str, str] | None:
    base = os.environ.get("JIRA_URL", "").strip().rstrip("/")
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = (
        os.environ.get("JIRA_API_TOKEN") or os.environ.get("JIRA_TOKEN") or ""
    ).strip()
    if not base or not email or not token:
        return None
    return base, email, token


def _auth_header(email: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def _jira_http_open(req: urllib.request.Request, *, timeout: float) -> Any:
    """Like ``post-freeipa-jira-comment``: bypass ``HTTP(S)_PROXY`` unless explicitly disabled."""
    if os.environ.get("REPORT_LOGS_JIRA_BYPASS_PROXY", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    ):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _http_json(
    method: str,
    url: str,
    *,
    email: str,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any] | None:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": _auth_header(email, token),
        },
    )
    try:
        with _jira_http_open(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def issues_and_next_token_from_search_jql(body: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Parse ``POST /rest/api/3/search/jql`` JSON; return issue dicts and ``nextPageToken``."""
    issues = body.get("issues")
    if not isinstance(issues, list):
        nodes = body.get("nodes")
        issues = nodes if isinstance(nodes, list) else []
    clean: list[dict[str, Any]] = [iss for iss in issues if isinstance(iss, dict)]
    tok = body.get("nextPageToken")
    next_tok = tok.strip() if isinstance(tok, str) and tok.strip() else None
    return clean, next_tok


def jira_search_jql_post(
    base: str,
    email: str,
    token: str,
    *,
    jql: str,
    fields: list[str],
    max_results: int,
    next_page_token: str | None = None,
) -> dict[str, Any] | None:
    """POST ``{base}/rest/api/3/search/jql`` (legacy ``/search`` removed on Jira Cloud)."""
    url = f"{base}/rest/api/3/search/jql"
    payload: dict[str, Any] = {"jql": jql, "maxResults": max_results, "fields": fields}
    if next_page_token:
        payload["nextPageToken"] = next_page_token
    res = _http_json("POST", url, email=email, token=token, payload=payload)
    return res if isinstance(res, dict) else None


def jira_search_jql_collect_issues(
    base: str,
    email: str,
    token: str,
    *,
    jql: str,
    fields: list[str],
    page_size: int = 50,
    max_issues: int = 500,
) -> list[dict[str, Any]]:
    """Paginated JQL search via ``/search/jql``; dedupes by issue key."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    next_tok: str | None = None
    page = max(1, min(page_size, 100))
    while len(out) < max_issues:
        body = jira_search_jql_post(
            base,
            email,
            token,
            jql=jql,
            fields=fields,
            max_results=min(page, max_issues - len(out)),
            next_page_token=next_tok,
        )
        if not body:
            break
        batch, next_tok = issues_and_next_token_from_search_jql(body)
        if not batch:
            break
        for iss in batch:
            k = (iss.get("key") or "").strip()
            if k and k not in seen:
                seen.add(k)
                out.append(iss)
            if len(out) >= max_issues:
                break
        if not next_tok:
            break
    return out


def known_issue_list_max_from_env() -> int:
    """Cap for Known Issue cells (``REPORT_LOGS_KNOWN_ISSUE_LIST_MAX``, default **25**, max **100**)."""
    try:
        lim = int(os.environ.get("REPORT_LOGS_KNOWN_ISSUE_LIST_MAX", "25"))
    except ValueError:
        lim = 25
    return max(1, min(lim, 100))


def collect_child_keys_matching_suite_summary(
    suite_name: str,
    child_items: list[tuple[str, str, str]],
    *,
    parent_key: str,
    max_keys: int | None = None,
) -> list[str]:
    """
    Child issue keys whose **summary** matches suite needles (same rules as
    :func:`_suite_summary_match_needles` / :func:`_summary_contains_suite_needle`).

    Used when JQL search did not return hits but :func:`collect_child_work_items` rows are
    available (proxy-safe parity with ``post-freeipa-jira-comment``).
    """
    lim = max_keys if max_keys is not None else known_issue_list_max_from_env()
    needles = _suite_summary_match_needles(suite_name)
    if not needles:
        return []
    pk = (parent_key or "").strip()
    hits: list[str] = []
    seen: set[str] = set()
    for key, _hay, summ in child_items:
        k = (key or "").strip()
        if not k or k == pk or k in seen:
            continue
        for n in needles:
            if _summary_contains_suite_needle(summ, n):
                hits.append(k)
                seen.add(k)
                break
    hits.sort()
    return hits[:lim]


def fetch_known_issue_non_closed_matches_for_suite(suite_name: str) -> list[tuple[str, str]]:
    """
    Umbrella issues under :func:`known_issue_parent_key` that are **not Closed**, whose
    summary/description matches the failing **suite** JQL needles (same rules as Known Issue JQL).

    Returns ``(issue_key, summary)`` sorted by issue key, capped by
    ``REPORT_LOGS_KNOWN_ISSUE_LIST_MAX`` (default 25). Empty when credentials are missing,
    there are no needles, or umbrella JQL is disabled via ``REPORT_LOGS_IDM_5601_JQL_SUMMARY=0``.
    """
    cred = jira_rest_credentials()
    if cred is None:
        return []
    if not _jql_summary_suite_search_wanted():
        return []
    needles = _jql_suite_needles_for_umbrella_search(suite_name)
    if not needles:
        return []
    pkey = known_issue_parent_key()
    scope = _known_issue_parent_scope_jql(pkey)
    clauses = _jql_build_summary_and_description_clauses(needles)
    clauses_or = " OR ".join(clauses)
    status_frag = '(status != "Closed" OR status IS EMPTY)'
    jql = f"{scope} AND {status_frag} AND ({clauses_or}) ORDER BY key ASC"
    lim = known_issue_list_max_from_env()

    base, email, token = cred
    raw = jira_search_jql_collect_issues(
        base,
        email,
        token,
        jql=jql,
        fields=["summary"],
        page_size=min(50, lim),
        max_issues=lim,
    )
    rows: list[tuple[str, str]] = []
    for iss in raw:
        k = (iss.get("key") or "").strip()
        if not k or k == pkey:
            continue
        fo = iss.get("fields") if isinstance(iss.get("fields"), dict) else {}
        summ = (fo.get("summary") or "").strip()
        rows.append((k, summ))
    return rows


def adf_to_plain(node: object) -> str:
    """Extract plain text from Jira Cloud ADF description (or nested dict/list)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text") or "")
        return "".join(adf_to_plain(c) for c in node.get("content", []))
    if isinstance(node, list):
        return "".join(adf_to_plain(c) for c in node)
    return ""


def _summary_from_fields(fields: dict[str, Any] | None) -> str:
    if not fields:
        return ""
    return (fields.get("summary") or "").strip().lower()


def _issue_text_fields(fields: dict[str, Any] | None) -> str:
    if not fields:
        return ""
    parts: list[str] = []
    s = (fields.get("summary") or "").strip()
    if s:
        parts.append(s)
    desc = fields.get("description")
    if isinstance(desc, str):
        parts.append(desc)
    elif isinstance(desc, dict):
        parts.append(adf_to_plain(desc))
    return " ".join(parts)


def _jira_comments_fetch_wanted() -> bool:
    """Append issue comments to the match haystack unless explicitly disabled (default: on)."""
    return os.environ.get("REPORT_LOGS_IDM_5601_FETCH_COMMENTS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _fetch_issue_comments_plain(
    base: str, email: str, token: str, issue_key: str
) -> str:
    """Plain text from all Jira issue comments (ADF bodies), paginated."""
    parts: list[str] = []
    start_at = 0
    page = 100
    qkey = urllib.parse.quote(issue_key, safe="")
    while True:
        url = f"{base}/rest/api/3/issue/{qkey}/comment?startAt={start_at}&maxResults={page}"
        res = _http_json("GET", url, email=email, token=token)
        if not res:
            break
        batch = res.get("comments") or []
        for c in batch:
            if not isinstance(c, dict):
                continue
            body = c.get("body")
            if isinstance(body, str):
                parts.append(body)
            elif isinstance(body, dict):
                parts.append(adf_to_plain(body))
        try:
            total = int(res.get("total", start_at + len(batch)))
        except (TypeError, ValueError):
            total = start_at + len(batch)
        start_at += len(batch)
        if start_at >= total or not batch:
            break
    return "\n".join(parts)


def _issue_text_with_comments(
    base: str,
    email: str,
    token: str,
    issue_key: str,
    summary_desc: str,
) -> str:
    """Summary + description + optionally issue comments (token matching haystack)."""
    head = (summary_desc or "").strip()
    if not _jira_comments_fetch_wanted():
        return head
    cmt = _fetch_issue_comments_plain(base, email, token, issue_key).strip()
    if not cmt:
        return head
    if not head:
        return cmt
    return f"{head}\n{cmt}"


_CHILD_SEARCH_PAGE = 100
_CHILD_SEARCH_MAX_PAGES = 50  # cap 5000 issues per parent


def _parent_scope_only_jql_enabled() -> bool:
    """If true, only ``parent = KEY`` is used (no parentEpic / Epic Link)."""
    return os.environ.get("REPORT_LOGS_IDM_5601_PARENT_SCOPE_ONLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _epic_link_clause_in_known_issue_scope_enabled() -> bool:
    """
    If true, Known Issue JQL and child search also include ``\"Epic Link\" = KEY``.

    Default **off** — scope is ``(parent = KEY OR parentEpic = KEY)`` only (matches manual Jira
    filters many teams use for umbrella work).
    """
    return os.environ.get("REPORT_LOGS_IDM_5601_INCLUDE_EPIC_LINK", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _known_issue_scope_cache_token() -> str:
    """Cache partition for JQL suite search (scope variant)."""
    if _parent_scope_only_jql_enabled():
        return "po"
    if _epic_link_clause_in_known_issue_scope_enabled():
        return "pel"
    return "ppe"


def _known_issue_parent_scope_jql(pkey: str) -> str:
    """
    JQL fragment matching issues under the known-issue umbrella (default IDM-5601).

    Default shape (Epic Link clause **off**)::

        (parent = KEY OR parentEpic = KEY)

    Set ``REPORT_LOGS_IDM_5601_INCLUDE_EPIC_LINK=1`` to use::

        (parent = KEY OR "Epic Link" = KEY OR parentEpic = KEY)

    **Known Issue suite JQL** then adds::

        AND (summary ~ "..." OR description ~ "..." [OR …])

    Built by :func:`_jql_search_first_child_key_by_summary_suite` (needles from the failing
    suite via :func:`_suite_summary_match_needles`). Append ``REPORT_LOGS_IDM_5601_JQL_SUFFIX``
    or the default status + ``ORDER BY``. Use ``REPORT_LOGS_IDM_5601_PARENT_SCOPE_ONLY=1`` for
    ``parent = KEY`` only; ``REPORT_LOGS_IDM_5601_JQL_TEXT=0`` to omit ``description ~``.
    """
    if _parent_scope_only_jql_enabled():
        return f"parent = {pkey}"
    if _epic_link_clause_in_known_issue_scope_enabled():
        return f'(parent = {pkey} OR "Epic Link" = {pkey} OR parentEpic = {pkey})'
    return f"(parent = {pkey} OR parentEpic = {pkey})"


def _paginated_issue_search(
    base: str,
    email: str,
    token: str,
    jql: str,
) -> list[dict[str, Any]]:
    """Paginated JQL search (``POST /rest/api/3/search/jql``); returns issue dicts."""
    return jira_search_jql_collect_issues(
        base,
        email,
        token,
        jql=jql,
        fields=["summary", "description", "issuetype", "subtask"],
        page_size=_CHILD_SEARCH_PAGE,
        max_issues=_CHILD_SEARCH_PAGE * _CHILD_SEARCH_MAX_PAGES,
    )


def _merge_issue_rows(parent_key: str, batches: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for batch in batches:
        for iss in batch:
            k = (iss.get("key") or "").strip()
            if k and k != parent_key and k not in seen:
                seen.add(k)
                out.append(iss)
    return out


def _search_children(
    base: str, email: str, token: str, parent_key: str
) -> list[dict[str, Any]]:
    """
    Issues under *parent_key* for known-issue matching: ``parent`` children plus ``parentEpic``
    (and optionally ``Epic Link`` when ``REPORT_LOGS_IDM_5601_INCLUDE_EPIC_LINK=1``), unless
    ``REPORT_LOGS_IDM_5601_PARENT_SCOPE_ONLY=1``.
    """
    pk = parent_key
    batches: list[list[dict[str, Any]]] = [_paginated_issue_search(base, email, token, f"parent = {pk}")]
    if not _parent_scope_only_jql_enabled():
        if _epic_link_clause_in_known_issue_scope_enabled():
            batches.append(_paginated_issue_search(base, email, token, f'"Epic Link" = {pk}'))
        batches.append(_paginated_issue_search(base, email, token, f"parentEpic = {pk}"))
    return _merge_issue_rows(pk, batches)


def _parent_subtasks(
    base: str, email: str, token: str, parent_key: str
) -> list[dict[str, Any]]:
    url = (
        f"{base}/rest/api/3/issue/{urllib.parse.quote(parent_key, safe='')}"
        "?fields=subtasks"
    )
    res = _http_json("GET", url, email=email, token=token)
    if not res or "fields" not in res:
        return []
    return res["fields"].get("subtasks") or []


def _fetch_issue_fields(
    base: str, email: str, token: str, issue_key: str
) -> dict[str, Any] | None:
    url = (
        f"{base}/rest/api/3/issue/{urllib.parse.quote(issue_key, safe='')}"
        "?fields=summary,description"
    )
    res = _http_json("GET", url, email=email, token=token)
    if not res or "fields" not in res:
        return None
    return res["fields"]


def collect_child_work_items(
    parent_key: str,
    *,
    force_refresh: bool = False,
) -> list[tuple[str, str, str]]:
    """
    Return ``(issue_key, lowercase haystack, summary_lower)`` per child under *parent_key*.

    Haystack is **summary** and **description** (ADF plain text), plus **issue comments** when
    ``REPORT_LOGS_IDM_5601_FETCH_COMMENTS`` is on (default **on**). Token matching uses this
    haystack; JQL still uses only ``summary ~`` / ``description ~`` (not comments). Set
    ``REPORT_LOGS_IDM_5601_FETCH_COMMENTS=0`` for summary+description only (faster).

    *summary_lower* is the issue summary alone (for “suite name in **summary**” matching in
    :func:`failure_matching_child_issue_key`).

    Sources: **classic subtasks** from the parent's ``subtasks`` field, plus paginated search for
    ``parent = parent_key`` and ``parentEpic = parent_key`` (and ``\"Epic Link\" = parent_key``
    only if ``REPORT_LOGS_IDM_5601_INCLUDE_EPIC_LINK=1``). Skipped when
    ``REPORT_LOGS_IDM_5601_PARENT_SCOPE_ONLY=1`` (**parent** only).

    Uses ``JIRA_URL`` / ``JIRA_EMAIL`` / ``JIRA_TOKEN``. On failure or missing credentials
    returns ``[]``. Duplicate issue keys keep the first occurrence (stable order: embedded
    subtasks first, then ``parent =`` search).
    """
    cred = jira_rest_credentials()
    if cred is None:
        return []

    base, email, token = cred
    now = time.monotonic()
    if not force_refresh and parent_key in _cache:
        exp, items = _cache[parent_key]
        if now < exp:
            return items

    rows: list[tuple[str, str, str]] = []
    seen_keys: set[str] = set()

    def add_row(key: str, plain: str, summary_lower: str) -> None:
        k = (key or "").strip()
        t = plain.strip()
        if not k or not t:
            return
        if k in seen_keys:
            return
        seen_keys.add(k)
        rows.append((k, t.lower(), summary_lower.strip().lower()))

    # Classic subtasks embedded on parent
    for st in _parent_subtasks(base, email, token, parent_key):
        k = (st.get("key") or "").strip()
        fld = st.get("fields") or {}
        summ = _summary_from_fields(fld)
        txt = _issue_text_fields(fld).strip()
        if k and not txt:
            ff = _fetch_issue_fields(base, email, token, k)
            fld_for_summary = ff or {}
            txt = _issue_text_fields(fld_for_summary).strip()
            if not summ:
                summ = _summary_from_fields(fld_for_summary)
        if k:
            txt = _issue_text_with_comments(base, email, token, k, txt)
        add_row(k, txt, summ)

    # Parent-field children (team-managed / hierarchy)
    for iss in _search_children(base, email, token, parent_key):
        fld = iss.get("fields") or {}
        summ = _summary_from_fields(fld)
        txt = _issue_text_fields(fld).strip()
        k = (iss.get("key") or "").strip()
        if k and not txt:
            ff = _fetch_issue_fields(base, email, token, k)
            fld_for_summary = ff or {}
            txt = _issue_text_fields(fld_for_summary).strip()
            if not summ:
                summ = _summary_from_fields(fld_for_summary)
        if k:
            txt = _issue_text_with_comments(base, email, token, k, txt)
        add_row(k, txt, summ)

    _cache[parent_key] = (now + _CACHE_TTL_SEC, rows)
    return rows


def collect_child_work_item_text_blobs(
    parent_key: str,
    *,
    force_refresh: bool = False,
) -> list[str]:
    """
    Return one lowercase string per child work item for *parent_key*.

    Same sources as :func:`collect_child_work_items`; credentials and caching are shared.
    """
    return [
        text for _, text, _ in collect_child_work_items(parent_key, force_refresh=force_refresh)
    ]


_WS_RE = re.compile(r"\s+")
_SUITE_SEGMENT_SPLIT = re.compile(r"[\s/—\-_]+")

# Dropped from whitespace- and path-segment splits of the suite name only (not from the
# full suite string). Stops false positives like the segment ``upstream`` matching unrelated
# CI log URL text in Pagure/Jira bodies while keeping ``upstream-edns`` as a full token and
# ``edns`` / ``xmlrpc``.
_SUITE_TOKEN_STOPWORDS = frozenset(
    {
        "ci",
        "development",
        "downstream",
        "freeipa",
        "idm",
        "master",
        "nightly",
        "rawhide",
        "staging",
        "tests",
        "upstream",
    }
)

# Dotted class path segments only (the full ``classname`` string is always still a token).
# ``tests`` otherwise matches as a substring of ``ipatests`` in Pagure/Jira; ``test`` matches
# generic English ("The test …").
_CLASS_SEGMENT_STOPWORDS = frozenset(
    {
        "test",
        "tests",
    }
)


def _norm_token(s: str) -> str:
    return _WS_RE.sub(" ", s.strip().lower())


def _normalize_for_summary_substring_match(s: str) -> str:
    """
    Lowercase text for “suite name appears in child **summary**” checks.

    Jira summaries often use spaces where JUnit suite names use hyphens or underscores
    (e.g. ``upstream xmlrpc tests`` vs ``upstream-xmlrpc-tests``). Collapse separators to
    spaces so substring containment matches both styles.
    """
    t = (s or "").strip().lower()
    if not t:
        return ""
    t = _SUITE_SEGMENT_SPLIT.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def _maybe_add_suite_split_token(tokens: set[str], fragment: str) -> None:
    """Add a lowercase fragment from suite word/segment splitting unless it is generic CI noise."""
    p = fragment.strip().lower()
    if len(p) < 4 or p in _SUITE_TOKEN_STOPWORDS:
        return
    tokens.add(p)


def class_name_match_tokens(classname: str) -> set[str]:
    """
    Tokens from the failed test **class name** (full dotted path, last segment, each
    ``.``-separated segment) for matching IDM-5601 child issue text.
    """
    tokens: set[str] = set()
    c = (classname or "").strip()
    if not c:
        return tokens
    tokens.add(c.lower())
    if "." in c:
        last = c.rsplit(".", 1)[-1].strip().lower()
        if len(last) >= 4 and last not in _CLASS_SEGMENT_STOPWORDS:
            tokens.add(last)
    for seg in c.split("."):
        seg = seg.strip().lower()
        if len(seg) >= 4 and seg not in _CLASS_SEGMENT_STOPWORDS:
            tokens.add(seg)
    return {t for t in tokens if len(t) >= 4}


def test_name_match_tokens(test_name: str) -> set[str]:
    """
    Tokens from the failed **test method/function name**: full name (length ≥ 4) and
    segments split on ``_`` (so ``test_su_ad_user`` yields ``su``, ``ad``, ``user``… per
    length rule); bare ``test`` alone is skipped as a fragment.
    """
    tokens: set[str] = set()
    n = (test_name or "").strip()
    if not n:
        return tokens
    nl = n.lower()
    if nl != "test" and len(nl) >= 4:
        tokens.add(nl)
    for part in nl.split("_"):
        pl = part.strip().lower()
        if len(pl) >= 4 and pl != "test":
            tokens.add(pl)
    return {t for t in tokens if len(t) >= 4}


def suite_name_match_tokens(suite_name: str) -> set[str]:
    """
    Tokens derived **only** from the JUnit/pytest **suite name** for matching IDM-5601
    child issues (full suite string, word splits, and hyphen/underscore path segments).

    Generic path fragments (``upstream``, ``nightly``, ``freeipa``, …) are **not** emitted
    from splits so artifact URLs do not spuriously match; the full hyphenated suite string
    (e.g. ``upstream-edns``) is always kept.

    Used first when resolving :func:`failure_matching_child_issue_key` so child summaries
    that mention the suite (e.g. ``upstream-xmlrpc-tests`` / ``xmlrpc``) match before
    falling back to class/test tokens.
    """
    tokens: set[str] = set()
    s = (suite_name or "").strip()
    if not s:
        return tokens
    tokens.add(_norm_token(s))
    if " — " in s:
        tokens.add(_norm_token(s.split(" — ", 1)[0]))
    for part in s.replace(" — ", " ").split():
        _maybe_add_suite_split_token(tokens, part)
    for part in _SUITE_SEGMENT_SPLIT.split(s):
        _maybe_add_suite_split_token(tokens, part)
    return {t for t in tokens if len(t) >= 4}


def pytest_style_match_tokens(classname: str, test_name: str) -> set[str]:
    """
    Tokens like ``dotted.path.ClassName::test_method`` as in pytest/JUnit failure lines
    often pasted into trackers.
    """
    tokens: set[str] = set()
    c = (classname or "").strip()
    n = (test_name or "").strip()
    if not c or not n:
        return tokens
    cl = c.lower()
    nl = n.lower()
    merged = f"{cl}::{nl}"
    if len(merged) >= 8:
        tokens.add(merged)
    if "." in c:
        short = c.rsplit(".", 1)[-1].strip().lower()
        if len(short) >= 4:
            alt = f"{short}::{nl}"
            if len(alt) >= 8:
                tokens.add(alt)
    return tokens


def failure_match_tokens(
    suite_name: str,
    classname: str,
    test_name: str,
) -> set[str]:
    """Union of suite, class, and test tokens for substring matching in child/Pagure text."""
    tokens: set[str] = set()
    tokens.update(suite_name_match_tokens(suite_name))
    tokens.update(class_name_match_tokens(classname))
    tokens.update(test_name_match_tokens(test_name))
    tokens.update(pytest_style_match_tokens(classname, test_name))
    return tokens


def known_issue_parent_key() -> str:
    """
    ``REPORT_LOGS_IDM_5601_PARENT_KEY`` when it looks like a Jira issue key, else ``IDM-5601``.

    Used for umbrella **child fetch** and JQL so invalid env values cannot desync the two paths.
    """
    raw = (os.environ.get("REPORT_LOGS_IDM_5601_PARENT_KEY", "IDM-5601") or "").strip() or "IDM-5601"
    if _PARENT_KEY_JQL_RE.fullmatch(raw):
        return raw
    return "IDM-5601"


def _sanitized_parent_issue_key_for_jql() -> str:
    return known_issue_parent_key()


def _jql_summary_suite_search_wanted() -> bool:
    return os.environ.get("REPORT_LOGS_IDM_5601_JQL_SUMMARY", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _jql_retry_without_status_clause_enabled() -> bool:
    return os.environ.get(
        "REPORT_LOGS_IDM_5601_JQL_RETRY_WITHOUT_STATUS",
        "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def _jql_suffix_after_summary_clause() -> str:
    """
    Status filter + ``ORDER BY`` appended after the ``summary ~`` OR group.

    Default matches the common UI-style JQL (quoted ``Closed``, ``ORDER BY created DESC``).
    Override with ``REPORT_LOGS_IDM_5601_JQL_SUFFIX`` (set to empty to append nothing).
    """
    if "REPORT_LOGS_IDM_5601_JQL_SUFFIX" in os.environ:
        extra = (os.environ.get("REPORT_LOGS_IDM_5601_JQL_SUFFIX") or "").strip()
        return f" {extra}" if extra else ""
    # Align with manual search: parent + summary ~ + non-closed + newest first
    return ' AND (status != "Closed" OR status IS EMPTY) ORDER BY created DESC'


def _jql_escape_summary_contains(needle: str) -> str:
    """Escape backslash and double-quote for JQL ``summary ~`` / ``text ~`` quoted operands."""
    return needle.replace("\\", "\\\\").replace('"', '\\"')


def _jql_include_description_clauses_wanted() -> bool:
    """When true, add ``description ~ "…"`` for each suite needle (not comments — avoids ``text ~``)."""
    return os.environ.get("REPORT_LOGS_IDM_5601_JQL_TEXT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _jql_build_summary_and_description_clauses(needles: list[str]) -> list[str]:
    clauses: list[str] = []
    for n in needles:
        inner = _jql_escape_summary_contains(n)
        clauses.append(f'summary ~ "{inner}"')
        if _jql_include_description_clauses_wanted():
            clauses.append(f'description ~ "{inner}"')
    return clauses


def _jql_multi_needle_aliases_wanted() -> bool:
    """
    When true, umbrella JQL ORs extra suite needles (hyphen/space variants).

    Default **off**: a single **NAME** = failed suite name (see :func:`_jql_suite_needles_for_umbrella_search`).
    """
    return os.environ.get("REPORT_LOGS_IDM_5601_JQL_MULTI_NEEDLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _jql_suite_needles_for_umbrella_search(suite_name: str) -> list[str]:
    """
    Needles for ``(parent … OR parentEpic …) AND (summary ~ … OR description ~ …)``.

    **Default:** one needle — whitespace-normalized failing **suite name** (JUnit/pytest **Suite
    name**), i.e. ``NAME`` in::

        (parent = KEY OR parentEpic = KEY)
        AND (summary ~ "NAME" OR description ~ "NAME")

    Set ``REPORT_LOGS_IDM_5601_JQL_MULTI_NEEDLE=1`` to OR additional legacy alias needles from
    :func:`_suite_summary_match_needles`. In-memory matching after JQL still uses
    :func:`_suite_summary_match_needles` when JQL misses or is disabled.
    """
    if _jql_multi_needle_aliases_wanted():
        needles = _suite_summary_match_needles(suite_name)
        return [n for n in needles if n and len(n.strip()) >= 2]
    s = (suite_name or "").strip()
    if not s:
        return []
    n = _norm_token(s)
    if not n or len(n) < 2:
        return []
    return [n]


def _jql_search_first_child_key_by_summary_suite(parent_key: str, suite_name: str) -> str | None:
    """
    First child issue key via JQL aligned with manual Jira filters (default scope)::

        (parent = KEY OR parentEpic = KEY)
        AND (summary ~ "NAME" OR description ~ "NAME")

    **NAME** is the failed suite string (normalized); optionally more OR clauses when
    ``REPORT_LOGS_IDM_5601_JQL_MULTI_NEEDLE=1``. Append ``{suffix}`` (status + ``ORDER BY`` by
    default). When Jira returns several issues, results are re-ranked so a **summary** match
    wins over a newer **description**-only hit.

    See :func:`_known_issue_parent_scope_jql`. Set ``REPORT_LOGS_IDM_5601_JQL_TEXT=0`` for
    **summary-only** clauses (no ``description ~``).
    """
    cred = jira_rest_credentials()
    if cred is None:
        return None
    needles = _jql_suite_needles_for_umbrella_search(suite_name)
    if not needles:
        return None
    pkey = parent_key if _PARENT_KEY_JQL_RE.fullmatch(parent_key) else _sanitized_parent_issue_key_for_jql()
    now = time.monotonic()
    text_flag = "1" if _jql_include_description_clauses_wanted() else "0"
    scope_tok = _known_issue_scope_cache_token()
    multi_flag = "1" if _jql_multi_needle_aliases_wanted() else "0"
    ckey = (pkey, suite_name.strip(), text_flag, scope_tok, multi_flag)
    if ckey in _jql_summary_cache:
        exp, prev = _jql_summary_cache[ckey]
        if now < exp:
            return prev

    base, email, token = cred
    clauses = _jql_build_summary_and_description_clauses(needles)
    clauses_or = " OR ".join(clauses)
    suffix = _jql_suffix_after_summary_clause()
    scope = _known_issue_parent_scope_jql(pkey)

    def post_search(jql_full: str) -> dict[str, Any] | None:
        rows = jira_search_jql_collect_issues(
            base,
            email,
            token,
            jql=jql_full,
            fields=["key", "summary"],
            page_size=50,
            max_issues=50,
        )
        if not rows:
            return None
        return {"issues": rows}

    res = post_search(f"{scope} AND ({clauses_or}){suffix}")
    if res is None and not _parent_scope_only_jql_enabled():
        res = post_search(f"parent = {pkey} AND ({clauses_or}){suffix}")
    if res is None and _jql_retry_without_status_clause_enabled():
        res = post_search(f"{scope} AND ({clauses_or}) ORDER BY created DESC")
        if res is None and not _parent_scope_only_jql_enabled():
            res = post_search(f"parent = {pkey} AND ({clauses_or}) ORDER BY created DESC")
    if res is None:
        # Do not cache: likely HTTP/auth/JQL parse failure; caching None hid real errors.
        return None
    out: str | None = None
    raw_issues: list[dict[str, Any]] = []
    if isinstance(res.get("issues"), list):
        for iss in res["issues"]:
            if not isinstance(iss, dict):
                continue
            k = (iss.get("key") or "").strip()
            if k and k != pkey:
                raw_issues.append(iss)
    keys = _rank_jql_keys_by_suite_summary_pref(raw_issues, needles)
    if keys:
        out = _choose_key_preferring_pin(suite_name, keys)
    _jql_summary_cache[ckey] = (now + _JQL_SUMMARY_CACHE_TTL_SEC, out)
    return out


def _suite_name_substrings_for_summary_match(suite_name: str) -> list[str]:
    """
    Normalized substrings that may appear in a Jira child **summary** for the same job.

    Uses the full suite string (whitespace-normalized) and, for ``before — after`` labels,
    the part before the em dash (common in IDM-CI table strings).
    """
    s = (suite_name or "").strip()
    if not s:
        return []
    out: list[str] = []
    full = _norm_token(s)
    if full:
        out.append(full)
    if " — " in s:
        head = _norm_token(s.split(" — ", 1)[0])
        if head and head not in out:
            out.append(head)
    return out


def _suite_summary_match_needles(suite_name: str) -> list[str]:
    """
    Ordered unique needles for JQL and in-memory “suite in child summary” matching.

    Includes :func:`_suite_name_substrings_for_summary_match` plus a separator-normalized
    alias for each (hyphens/underscores → spaces) when that differs, so summaries that
    spell the suite with spaces still match.
    """
    base = _suite_name_substrings_for_summary_match(suite_name)
    out: list[str] = []
    seen: set[str] = set()
    for n in base:
        raw = (n or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
        alias = _normalize_for_summary_substring_match(raw)
        if alias and alias != raw and alias not in seen:
            seen.add(alias)
            out.append(alias)
    return out


def _summary_contains_suite_needle(summary_lower: str, needle: str) -> bool:
    """True if *needle* or its separator-normalized form appears in *summary_lower* (also normalized)."""
    summ = (summary_lower or "").strip()
    if not summ:
        return False
    n = (needle or "").strip()
    if not n:
        return False
    if n in summ:
        return True
    summ_n = _normalize_for_summary_substring_match(summ)
    n_n = _normalize_for_summary_substring_match(n)
    return bool(n_n and len(n_n) >= 2 and n_n in summ_n)


def _merged_suite_pin_pairs() -> list[tuple[str, str]]:
    """
    Optional manual overrides: ``(suite needle, issue key)`` sorted by needle length descending.

    Populated only from env ``REPORT_LOGS_IDM_5601_SUITE_ISSUE_PINS`` JSON
    (e.g. ``{"upstream-edns": "IDM-5515"}``). Empty when unset — matching uses subtask text only.
    """
    d: dict[str, str] = {}
    raw = os.environ.get("REPORT_LOGS_IDM_5601_SUITE_ISSUE_PINS", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                        d[k.strip().lower()] = v.strip()
        except json.JSONDecodeError:
            pass
    pairs = sorted(d.items(), key=lambda kv: len(kv[0]), reverse=True)
    return pairs


def _suite_pin_lookup(suite_name: str) -> str | None:
    """Issue key from optional ``REPORT_LOGS_IDM_5601_SUITE_ISSUE_PINS`` (prefix match on suite string)."""
    s = (suite_name or "").strip()
    if not s:
        return None
    candidates = [_norm_token(s)]
    if " — " in s:
        head = _norm_token(s.split(" — ", 1)[0])
        if head and head not in candidates:
            candidates.append(head)
    for cand in candidates:
        for needle, key in _merged_suite_pin_pairs():
            if cand == needle or cand.startswith(needle + "-") or cand.startswith(needle + " "):
                return key
    return None


def _dedupe_keys_preserve_order(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _choose_key_preferring_pin(suite_name: str, keys: list[str]) -> str | None:
    """Among matching issues, prefer the env override key from :func:`_suite_pin_lookup` when listed."""
    ordered = _dedupe_keys_preserve_order(keys)
    if not ordered:
        return None
    pin = _suite_pin_lookup(suite_name)
    if pin and pin in ordered:
        return pin
    return ordered[0]


def _rank_jql_keys_by_suite_summary_pref(
    issues: list[dict[str, Any]],
    needles: list[str],
) -> list[str]:
    """
    Re-order JQL hit keys so **summary** matches beat description-only matches.

    ``/search`` uses ``ORDER BY created DESC``; several issues can satisfy the same
    ``summary ~ OR description ~`` clause. Prefer the issue whose **summary** matches the
    earliest (highest-priority) suite needle from :func:`_suite_summary_match_needles`.
    Issues with no summary needle match keep API order (newest first among ties).
    """
    keys: list[str] = []
    scores: dict[str, tuple[int, int]] = {}
    for idx, iss in enumerate(issues):
        if not isinstance(iss, dict):
            continue
        k = (iss.get("key") or "").strip()
        if not k:
            continue
        fields = iss.get("fields") if isinstance(iss.get("fields"), dict) else {}
        summ = (fields.get("summary") or "").strip().lower()
        best = 999
        for i, needle in enumerate(needles):
            if _summary_contains_suite_needle(summ, needle):
                best = i
                break
        if k not in scores:
            keys.append(k)
            scores[k] = (best, idx)
    return sorted(keys, key=lambda kk: scores[kk])


def failure_matching_child_issue_key(
    suite_name: str,
    classname: str,
    test_name: str,
    child_items: list[tuple[str, str, str]],
) -> str | None:
    """
    First child issue key for the failure (see :func:`collect_child_work_items`).

    Matching is **derived from IDM-5601 umbrella subtasks** (``parent`` + ``parentEpic``;
    optional ``Epic Link`` via ``REPORT_LOGS_IDM_5601_INCLUDE_EPIC_LINK``):     JQL on summary and description using the failed suite as **NAME** (see
    :func:`_jql_suite_needles_for_umbrella_search`), then in-memory summary containment
    with broader alias needles,
    then suite tokens and full failure tokens on each issue’s haystack (summary, description,
    comments by default).

    Optional env ``REPORT_LOGS_IDM_5601_SUITE_ISSUE_PINS`` JSON can force a preferred issue key
    when several subtasks match (:func:`_choose_key_preferring_pin`).

    Disable JQL with ``REPORT_LOGS_IDM_5601_JQL_SUMMARY=0``. JQL runs even when *child_items*
    is empty as long as Jira credentials exist.
    """
    parent_key = _sanitized_parent_issue_key_for_jql()

    if _jql_summary_suite_search_wanted():
        hit = _jql_search_first_child_key_by_summary_suite(parent_key, suite_name)
        if hit:
            return hit

    if not child_items:
        return None

    # (2) Client-side summary match (alias needles included) when JQL missed or was disabled.
    suite_needles = _suite_summary_match_needles(suite_name)
    if suite_needles:
        summary_hits: list[str] = []
        for key, _hay, summ in child_items:
            if not summ:
                continue
            for needle in suite_needles:
                if _summary_contains_suite_needle(summ, needle):
                    summary_hits.append(key)
                    break
        chosen = _choose_key_preferring_pin(suite_name, summary_hits)
        if chosen:
            return chosen

    suite_only = suite_name_match_tokens(suite_name)
    if suite_only:
        suite_hits: list[str] = []
        for key, hay, _summ in child_items:
            for t in suite_only:
                if t and t in hay:
                    suite_hits.append(key)
                    break
        chosen = _choose_key_preferring_pin(suite_name, suite_hits)
        if chosen:
            return chosen

    tokens = failure_match_tokens(suite_name, classname, test_name)
    token_hits: list[str] = []
    for key, hay, _summ in child_items:
        for t in tokens:
            if t and t in hay:
                token_hits.append(key)
                break
    chosen = _choose_key_preferring_pin(suite_name, token_hits)
    if chosen:
        return chosen
    return None


def failure_matches_child_work_items(
    suite_name: str,
    classname: str,
    test_name: str,
    child_blobs: list[str],
) -> bool:
    """
    True if any *child_blobs* mentions a suite token (preferred), then any suite/class/test
    token per :func:`failure_match_tokens`.
    """
    if not child_blobs:
        return False
    hay = " \n ".join(child_blobs).lower()
    suite_only = suite_name_match_tokens(suite_name)
    if suite_only:
        for t in suite_only:
            if t and t in hay:
                return True
    tokens = failure_match_tokens(suite_name, classname, test_name)
    for t in tokens:
        if t and t in hay:
            return True
    return False

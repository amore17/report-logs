"""Post FreeIPA CI analyzer output to a Jira issue (Cloud REST API v3, ADF)."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from report_logs.artifacts import (
    discover_latest_pipeline_index_url,
    normalize_rhel_version,
)
from report_logs.models import ParseResult
from report_logs.render import (
    iter_failure_table_rows,
    known_issue_empty_placeholder,
    known_issue_jira_links_enabled,
    render_brief,
    render_failure_table,
    render_short,
)
from report_logs.jira_child_issues import issues_and_next_token_from_search_jql
from report_logs.server import fetch_freeipa_ci_parse_result


def _pipeline_link_caption(href: str) -> str:
    """Short label for ADF link (last URL path segment + ``/``)."""
    path = urlparse(href).path.rstrip("/")
    seg = path.split("/")[-1] if path else "index"
    return (seg or "index") + "/"


def strip_optional_for_rhel(argv: list[str]) -> tuple[list[str], str | None]:
    """
    If *argv* starts with ``for`` and a RHEL stream token, drop them and return the RHEL
    string normalized (e.g. ``9.8``). Example: ``['for', 'RHEL9.8', '--help']`` →
    ``(['--help'], '9.8')``.
    """
    if len(argv) >= 2 and argv[0] == "for":
        rhel = normalize_rhel_version(argv[1])
        return argv[2:], rhel
    return argv, None


def load_env_file(path: Path, *, override: bool = False) -> None:
    """Parse shell-style KEY=value lines (optional quotes) into os.environ."""
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and (override or key not in os.environ):
            os.environ[key] = val


def jira_credentials() -> tuple[str, str, str]:
    base = os.environ.get("JIRA_URL", "").strip().rstrip("/")
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = (
        os.environ.get("JIRA_API_TOKEN") or os.environ.get("JIRA_TOKEN") or ""
    ).strip()
    return base, email, token


def extract_totals(report_text: str) -> tuple[int, int, int, int] | None:
    m = re.search(
        r"\*\*Totals:\*\*\s*(\d+)\s*passed,\s*(\d+)\s*failed,\s*(\d+)\s*skipped\s*\(of\s*(\d+)\)",
        report_text,
    )
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def extract_pipeline_listing_url(report_text: str) -> str | None:
    m = re.search(
        r"Discovered \d+ job junit URL\(s\) under (https://\S+)",
        report_text,
    )
    return m.group(1).rstrip("/") + "/" if m else None


def extract_title_folder(report_text: str) -> str | None:
    m = re.search(r"^## .+\((\d{4}-\d{2}-\d{2}_[^\)]+)\)\s*$", report_text, re.MULTILINE)
    if m:
        return m.group(1)
    listing = extract_pipeline_listing_url(report_text)
    if listing:
        m2 = re.search(r"/RHEL[\d.]+/(\d{4}-\d{2}-\d{2}_[^/]+)/", listing)
        if m2:
            return m2.group(1)
    return None


def strip_per_job_fetch_lines(report_text: str) -> str:
    lines = [
        ln for ln in report_text.splitlines() if not ln.startswith("Fetched JUnit XML from ")
    ]
    return "\n".join(lines).strip() + "\n"


def adf_text(t: str, *, href: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": t}
    if href:
        node["marks"] = [{"type": "link", "attrs": {"href": href}}]
    return node


def adf_paragraph(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(nodes)}


def adf_known_issue_cell(text: str) -> dict[str, Any]:
    """ADF paragraph for **AI Suggested Known Issue**: one or more markdown ``[KEY](url)`` become links (`` · `` between)."""
    t = (text or "").strip()
    if not t:
        return adf_paragraph(adf_text(""))
    empty = known_issue_empty_placeholder()
    if t == "—" or t == empty:
        return adf_paragraph(adf_text(empty))
    matches = list(re.finditer(r"\[([^\]]+)\]\((https://[^)]+)\)", t))
    if not matches:
        return adf_paragraph(adf_text(t))
    nodes: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        if i > 0:
            nodes.append(adf_text(" · "))
        nodes.append(adf_text(m.group(1), href=m.group(2)))
    return adf_paragraph(*nodes)


def adf_suite_name_cell(label: str, report_html_href: str | None) -> dict[str, Any]:
    """ADF cell for **Suite Name**: link to job ``report.html`` when *report_html_href* is http(s)."""
    t = (label or "").strip() or "—"
    h = (report_html_href or "").strip()
    if h.startswith(("http://", "https://")):
        return adf_paragraph(adf_text(t, href=h))
    return adf_paragraph(adf_text(t))


def adf_table_comment(
    *,
    intro: str,
    rows: list[tuple[str, str, str, str | None]],
    footer: str | None,
) -> dict[str, Any]:
    """rows: (tier_label, date_folder, results_cell, pipeline_href or None)."""

    def tr_header() -> dict[str, Any]:
        return {
            "type": "tableRow",
            "content": [
                {
                    "type": "tableHeader",
                    "attrs": {},
                    "content": [adf_paragraph(adf_text("Tier"))],
                },
                {
                    "type": "tableHeader",
                    "attrs": {},
                    "content": [adf_paragraph(adf_text("Published run"))],
                },
                {
                    "type": "tableHeader",
                    "attrs": {},
                    "content": [adf_paragraph(adf_text("Pass / fail / skip (total)"))],
                },
            ],
        }

    def tr_data(
        tier: str, folder_line: str, results: str, pipeline_href: str | None
    ) -> dict[str, Any]:
        if pipeline_href:
            cell2 = adf_paragraph(
                adf_text(f"{folder_line} — "),
                adf_text(_pipeline_link_caption(pipeline_href), href=pipeline_href),
            )
        else:
            cell2 = adf_paragraph(adf_text(folder_line))
        return {
            "type": "tableRow",
            "content": [
                {"type": "tableCell", "attrs": {}, "content": [adf_paragraph(adf_text(tier))]},
                {"type": "tableCell", "attrs": {}, "content": [cell2]},
                {
                    "type": "tableCell",
                    "attrs": {},
                    "content": [adf_paragraph(adf_text(results))],
                },
            ],
        }

    content: list[dict[str, Any]] = [adf_paragraph(adf_text(intro))]
    table_rows: list[dict[str, Any]] = [tr_header()]
    for tier, folder, results, href in rows:
        table_rows.append(tr_data(tier, folder, results, href))
    content.append(
        {
            "type": "table",
            "attrs": {"isNumberColumnEnabled": False, "layout": "align-start"},
            "content": table_rows,
        }
    )
    if footer:
        content.append(adf_paragraph(adf_text(footer)))
    return {"type": "doc", "version": 1, "content": content}


def plain_chunked_code_blocks(plaintext: str) -> list[dict[str, Any]]:
    """Split plaintext into ADF codeBlock nodes (newline-safe chunks)."""
    max_chunk = 8000
    chunks: list[str] = []
    buf = ""
    for line in plaintext.splitlines(True):
        if len(buf) + len(line) > max_chunk and buf:
            chunks.append(buf)
            buf = line
            while len(buf) > max_chunk:
                chunks.append(buf[:max_chunk])
                buf = buf[max_chunk:]
        else:
            buf += line
    if buf:
        chunks.append(buf)
    return [
        {
            "type": "codeBlock",
            "attrs": {"language": "plaintext"},
            "content": [{"type": "text", "text": ch}],
        }
        for ch in chunks
    ]


def adf_with_plain_append(doc: dict[str, Any], plaintext: str) -> dict[str, Any]:
    """Append monospace blocks after existing ADF body."""
    out = dict(doc)
    inner = list(out["content"])
    inner.extend(plain_chunked_code_blocks(plaintext))
    out["content"] = inner
    return out


def _jira_http_open(req: urllib.request.Request, *, timeout: float = 120):
    """Bypass ``HTTP(S)_PROXY`` by default (many proxies block CONNECT to Atlassian)."""
    if os.environ.get("REPORT_LOGS_JIRA_BYPASS_PROXY", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    ):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def jira_api_request(
    method: str,
    url: str,
    *,
    email: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with _jira_http_open(req, timeout=120) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code}: {err_body[:2000]}") from e


def post_comment(base: str, email: str, token: str, issue_key: str, body: dict[str, Any]) -> dict[str, Any]:
    url = f"{base}/rest/api/3/issue/{issue_key}/comment"
    _, result = jira_api_request(
        "POST", url, email=email, token=token, payload={"body": body}
    )
    return result


def build_epic_umbrella_non_closed_jql(epic_key: str) -> str | None:
    """
    JQL for **non-Closed** issues under the umbrella epic/key (same scope as Known Issue:
    :func:`~report_logs.jira_child_issues._known_issue_parent_scope_jql`).
    """
    from report_logs.jira_child_issues import _known_issue_parent_scope_jql

    ek = (epic_key or "").strip()
    if not ek:
        return None
    scope = _known_issue_parent_scope_jql(ek)
    status_frag = '(status != "Closed" OR status IS EMPTY)'
    return f"{scope} AND {status_frag} ORDER BY key ASC"


def _issue_status_name(fields: dict[str, Any] | None) -> str:
    """Human-readable status name from Jira issue ``fields`` (Cloud REST)."""
    if not fields or not isinstance(fields, dict):
        return ""
    st = fields.get("status")
    if isinstance(st, dict):
        name = (st.get("name") or "").strip()
        if name:
            return name
    return ""


def jira_search_issues_page(
    base: str,
    email: str,
    token: str,
    *,
    jql: str,
    fields: list[str],
    max_results: int,
    next_page_token: str | None = None,
) -> dict[str, Any]:
    """
    One page of issues via ``POST {base}/rest/api/3/search/jql`` (replaces removed ``/search``).

    Pagination: pass *next_page_token* from the previous response's ``nextPageToken``.
    """
    url = f"{base}/rest/api/3/search/jql"
    payload: dict[str, Any] = {
        "jql": jql,
        "maxResults": max_results,
        "fields": fields,
    }
    if next_page_token:
        payload["nextPageToken"] = next_page_token
    _, body = jira_api_request("POST", url, email=email, token=token, payload=payload)
    return body


def fetch_epic_in_progress_issue_tuples(
    base: str,
    email: str,
    token: str,
    *,
    epic_key: str | None = None,
) -> list[tuple[str, str, str]]:
    """
    Return ``(issue_key, summary, status_name)`` for **non-Closed** issues under the umbrella epic
    (default :func:`~report_logs.jira_child_issues.known_issue_parent_key`).
    """
    from report_logs.jira_child_issues import known_issue_parent_key

    ek = ((epic_key or known_issue_parent_key()) or "").strip()
    jql = build_epic_umbrella_non_closed_jql(ek)
    if not jql:
        return []
    try:
        max_total = int(os.environ.get("FREEIPA_JIRA_EPIC_IN_PROGRESS_MAX", "300"))
    except ValueError:
        max_total = 300
    max_total = max(1, min(max_total, 1000))
    page_sz = min(50, max_total)

    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    fields = ["summary", "status"]
    next_tok: str | None = None
    while len(out) < max_total:
        data = jira_search_issues_page(
            base,
            email,
            token,
            jql=jql,
            fields=fields,
            max_results=page_sz,
            next_page_token=next_tok,
        )
        if not isinstance(data, dict):
            break
        issues, next_tok = issues_and_next_token_from_search_jql(data)
        if not issues:
            break
        for iss in issues:
            if not isinstance(iss, dict):
                continue
            k = (iss.get("key") or "").strip()
            if not k or k in seen:
                continue
            fo = iss.get("fields") if isinstance(iss.get("fields"), dict) else {}
            summ = (fo.get("summary") or "").strip()
            st_name = _issue_status_name(fo)
            seen.add(k)
            out.append((k, summ, st_name))
            if len(out) >= max_total:
                break
        if not next_tok:
            break
    return out


def adf_epic_in_progress_section(
    epic_key: str,
    browse_base: str,
    rows: list[tuple[str, str, str]],
    *,
    section_title: str | None = None,
) -> list[dict[str, Any]]:
    """ADF blocks: heading + bullet list of linked keys, status, and summaries."""
    title = (section_title or "").strip() or f"In Progress — {epic_key}"
    bb = browse_base.rstrip("/")
    heading: dict[str, Any] = {
        "type": "heading",
        "attrs": {"level": 3},
        "content": [{"type": "text", "text": title}],
    }
    if not rows:
        return [heading, adf_paragraph(adf_text("(No open issues under this epic.)"))]

    items: list[dict[str, Any]] = []
    for key, summary, status_name in rows:
        url = f"{bb}/browse/{key}"
        summ = summary[:800] if summary else ""
        nodes: list[dict[str, Any]] = [adf_text(key, href=url)]
        if status_name:
            nodes.append(adf_text(f" — {status_name}"))
        if summ:
            nodes.append(adf_text(f" — {summ}"))
        items.append({"type": "listItem", "content": [adf_paragraph(*nodes)]})
    return [heading, {"type": "bulletList", "content": items}]


def merge_epic_in_progress_into_comment_doc(
    doc: dict[str, Any],
    *,
    base: str,
    email: str,
    token: str,
) -> dict[str, Any]:
    """
    Append an **In Progress** subsection listing **non-Closed** umbrella issues under epic
    ``REPORT_LOGS_IDM_5601_PARENT_KEY`` (default IDM-5601), each bullet showing **key**, **status**,
    and **summary**. On error, returns *doc* unchanged and prints a warning to stderr.
    """
    from report_logs.jira_child_issues import known_issue_parent_key

    try:
        epic_key = known_issue_parent_key()
        rows = fetch_epic_in_progress_issue_tuples(base, email, token, epic_key=epic_key)
        title = os.environ.get("FREEIPA_JIRA_EPIC_IN_PROGRESS_TITLE", "").strip() or None
        block = adf_epic_in_progress_section(epic_key, base, rows, section_title=title)
        out = dict(doc)
        inner = list(out.get("content", []))
        inner.extend(block)
        out["content"] = inner
        return out
    except (
        RuntimeError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
        urllib.error.URLError,
    ) as exc:
        print(f"warning: epic In Progress section skipped: {exc}", file=sys.stderr)
        return doc


def build_table_rows_from_reports(
    reports: list[tuple[str, str]],
) -> list[tuple[str, str, str, str | None]]:
    rows: list[tuple[str, str, str, str | None]] = []
    for tier, rep in reports:
        totals = extract_totals(rep)
        listing = extract_pipeline_listing_url(rep)
        folder = extract_title_folder(rep) or "—"
        if totals:
            p, f, s, tot = totals
            results = f"{p} / {f} / {s} ({tot})"
        else:
            results = "—"
        pipe_href = None
        if listing and re.search(r"/tier-[123]/", listing):
            pipe_href = listing
        rows.append((tier, folder, results, pipe_href))
    return rows


def build_table_rows_from_parse_results(
    items: list[tuple[str, ParseResult | None, str]],
) -> list[tuple[str, str, str, str | None]]:
    """Summary rows from merged :class:`ParseResult` (same shape as :func:`build_table_rows_from_reports`)."""
    rows: list[tuple[str, str, str, str | None]] = []
    for tier, result, note in items:
        listing = extract_pipeline_listing_url(note)
        folder = extract_title_folder(note) or "—"
        if result is None:
            rows.append(
                (
                    tier,
                    folder,
                    "—",
                    listing if listing and re.search(r"/tier-[123]/", listing) else None,
                )
            )
            continue
        p = result.passed
        bad = result.failures + result.errors
        s = result.skipped
        tot = result.tests
        results = f"{p} / {bad} / {s} ({tot})"
        pipe_href = listing if listing and re.search(r"/tier-[123]/", listing) else None
        rows.append((tier, folder, results, pipe_href))
    return rows


def adf_failure_detail_table(
    *,
    section_title: str,
    row_tuples: list[tuple[str, str, str | None, str, str, str]],
) -> dict[str, Any]:
    """ADF table: Tier, Suite Name (optional report.html link), Test Name, Failure Details, AI Suggested Known Issue."""

    def tr_header() -> dict[str, Any]:
        headers = (
            "Tier",
            "Suite Name",
            "Test Name",
            "Failure Details",
            "AI Suggested Known Issue",
        )
        return {
            "type": "tableRow",
            "content": [
                {
                    "type": "tableHeader",
                    "attrs": {},
                    "content": [adf_paragraph(adf_text(h))],
                }
                for h in headers
            ],
        }

    def tr_data(cells: tuple[str, str, str | None, str, str, str]) -> dict[str, Any]:
        c0, c1, c1href, c2, c3, c4 = cells
        return {
            "type": "tableRow",
            "content": [
                {
                    "type": "tableCell",
                    "attrs": {},
                    "content": [adf_paragraph(adf_text(c0))],
                },
                {
                    "type": "tableCell",
                    "attrs": {},
                    "content": [adf_suite_name_cell(c1, c1href)],
                },
                {
                    "type": "tableCell",
                    "attrs": {},
                    "content": [adf_paragraph(adf_text(c2))],
                },
                {
                    "type": "tableCell",
                    "attrs": {},
                    "content": [adf_paragraph(adf_text(c3))],
                },
                {
                    "type": "tableCell",
                    "attrs": {},
                    "content": [adf_known_issue_cell(c4)],
                },
            ],
        }

    content: list[dict[str, Any]] = [
        adf_paragraph(adf_text(section_title)),
        {
            "type": "table",
            "attrs": {"isNumberColumnEnabled": False, "layout": "align-start"},
            "content": [tr_header(), *[tr_data(r) for r in row_tuples]],
        },
    ]
    return {"type": "doc", "version": 1, "content": content}


def _merge_adf_docs(*docs: dict[str, Any]) -> dict[str, Any]:
    """Concatenate ADF doc bodies (first doc supplies outer wrapper)."""
    if not docs:
        return {"type": "doc", "version": 1, "content": []}
    out = dict(docs[0])
    inner: list[dict[str, Any]] = list(out.get("content", []))
    for d in docs[1:]:
        inner.extend(d.get("content", []))
    out["content"] = inner
    return out


def failure_rows_for_tiers(
    items: list[tuple[str, ParseResult | None, str]],
    *,
    detail_limit: int,
    max_rows: int | None,
) -> list[tuple[str, str, str | None, str, str, str]]:
    """Flatten per-tier failure rows for the detail table."""
    out: list[tuple[str, str, str | None, str, str, str]] = []
    for tier, result, _note in items:
        if result is None:
            out.append(
                (
                    tier,
                    "—",
                    None,
                    "—",
                    "(could not fetch JUnit for this tier)",
                    known_issue_empty_placeholder()
                    if known_issue_jira_links_enabled()
                    else "",
                )
            )
            continue
        tier_rows = iter_failure_table_rows(
            result,
            tier=tier,
            detail_limit=detail_limit,
            known_issue_for=None,
        )
        for tc, suite, suite_href, ident, detail, known in tier_rows:
            out.append((tc, suite, suite_href, ident, detail, known))
    if max_rows is not None and len(out) > max_rows:
        out = out[:max_rows]
        out.append(
            (
                "—",
                "—",
                None,
                "—",
                f"(Table truncated to {max_rows} rows; raise FREEIPA_JIRA_FAILURE_TABLE_MAX_ROWS.)",
                known_issue_empty_placeholder()
                if known_issue_jira_links_enabled()
                else "",
            )
        )
    return out


def run_fetch_for_tiers(
    rhel_version: str,
    tiers: list[str],
) -> list[tuple[str, ParseResult | None, str]]:
    """One merged fetch per tier: ``(tier, result | None, artifact_note)``."""
    out: list[tuple[str, ParseResult | None, str]] = []
    for tier in tiers:
        tier = tier.strip()
        if not tier:
            raise ValueError("empty tier name in tiers list")
        idx, disc = discover_latest_pipeline_index_url(rhel_version, tier)
        if not idx:
            out.append((tier, None, disc))
            continue
        result, note = fetch_freeipa_ci_parse_result(
            rhel_version,
            tier,
            pipeline_index_url=idx,
        )
        out.append((tier, result, disc + "\n\n" + note))
    return out


def render_reports_text_from_fetches(
    rhel_version: str,
    items: list[tuple[str, ParseResult | None, str]],
    report_style: str,
) -> list[tuple[str, str]]:
    """Build the same markdown strings as ``analyze_freeipa_ci_artifacts`` for each tier."""
    base = "https://idm-artifacts.psi.redhat.com/idm-ci/freeipa/"
    style = (report_style or "brief").lower().strip()
    reports: list[tuple[str, str]] = []
    for tier, result, note in items:
        run_title = f"FreeIPA {tier} RHEL {rhel_version}"
        if result is None:
            reports.append((tier, note))
            continue
        if style == "short":
            body = render_short(
                result,
                header=f"## {run_title}",
                artifact_url=base.rstrip("/"),
            )
        elif style == "table":
            body = render_failure_table(
                result,
                header=f"## {run_title}",
                artifact_url=base.rstrip("/"),
                tier=tier,
                known_issue_for=None,
            )
        else:
            axis = [
                f"{tier} RHEL{rhel_version}: {result.failures + result.errors} failed",
            ]
            body = render_brief(result, title=run_title, axis_lines=axis)
        reports.append((tier, note + body))
    return reports


def _env_truthy(key: str, default: bool = True) -> bool:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "no", "off")


def _is_content_limit_exceeded(exc: BaseException) -> bool:
    """
    Return True when Jira rejects a comment because it is too large.

    Observed payload: HTTP 400 with `{"errorMessages":["CONTENT_LIMIT_EXCEEDED"], ...}`.
    """
    msg = str(exc)
    return "CONTENT_LIMIT_EXCEEDED" in msg


def _post_with_content_limit_fallback(
    *,
    base: str,
    email: str,
    token: str,
    issue_key: str,
    doc: dict[str, Any],
    label: str,
    rhel: str,
    tiers: list[str],
    intro: str,
    footer: str | None,
    style: str,
    mode: str,
    include_failure_table: bool,
    failure_section_title: str,
    detail_limit: int,
    max_rows: int | None,
    include_epic_in_progress: bool,
) -> int:
    """
    Post *doc*; if Jira rejects with CONTENT_LIMIT_EXCEEDED and multiple tiers were requested,
    post one comment per tier and add the umbrella epic child-issues section only once.
    """
    try:
        result = post_comment(base, email, token, issue_key, doc)
        print(f"Posted comment ({label}) id={result.get('id', '?')} on {issue_key}")
        return 0
    except RuntimeError as exc:
        if not (_is_content_limit_exceeded(exc) and len(tiers) > 1):
            raise

    print(
        "warning: Jira comment too large (CONTENT_LIMIT_EXCEEDED); falling back to one comment per tier.",
        file=sys.stderr,
    )

    epic_posted = False

    for tier in tiers:
        one_fetch = run_fetch_for_tiers(rhel, [tier])
        one_table_rows = build_table_rows_from_parse_results(one_fetch)
        one_reports = render_reports_text_from_fetches(rhel, one_fetch, style)

        parts = [
            "Generated by analyze_freeipa_ci_artifacts (%s).\n" % style,
            "",
            "Scope: RHEL %s — tiers: %s\n" % (rhel, tier),
            "",
        ]
        for _t, rep in one_reports:
            parts.append(f"### {tier}\n\n")
            parts.append(strip_per_job_fetch_lines(rep))
            parts.append("\n")
        one_full_text = "".join(parts)

        one_intro = intro
        one_summary_doc = adf_table_comment(intro=one_intro, rows=one_table_rows, footer=footer)

        one_failure_doc_inner: list[dict[str, Any]] | None = None
        if include_failure_table:
            one_failure_rows = failure_rows_for_tiers(
                one_fetch,
                detail_limit=detail_limit,
                max_rows=max_rows,
            )
            one_failure_doc_inner = adf_failure_detail_table(
                section_title=failure_section_title,
                row_tuples=one_failure_rows,
            )["content"]

        one_summary_plus_failure: dict[str, Any] = one_summary_doc
        if one_failure_doc_inner is not None:
            one_summary_plus_failure = _merge_adf_docs(
                one_summary_doc,
                {"type": "doc", "version": 1, "content": one_failure_doc_inner},
            )

        if mode == "table":
            one_doc = one_summary_plus_failure
            one_label = "table"
        elif mode == "full":
            inner: list[dict[str, Any]] = [adf_paragraph(adf_text(one_intro))]
            if one_failure_doc_inner is not None:
                inner.extend(one_failure_doc_inner)
            inner.extend(plain_chunked_code_blocks(one_full_text))
            one_doc = {"type": "doc", "version": 1, "content": inner}
            one_label = "full"
        else:
            one_doc = adf_with_plain_append(one_summary_plus_failure, one_full_text)
            one_label = "both"

        # Do not include the epic "In Progress" section in each tier comment.
        # We'll add it once after per-tier comments are posted.
        try:
            res = post_comment(base, email, token, issue_key, one_doc)
            print(
                f"Posted comment ({one_label}) id={res.get('id', '?')} on {issue_key} ({tier})"
            )
        except RuntimeError as exc:
            if _is_content_limit_exceeded(exc) and include_failure_table:
                # Last-resort shrink: drop the large per-failure detail table for this tier only.
                smaller = one_summary_doc
                try:
                    res = post_comment(base, email, token, issue_key, smaller)
                    print(
                        f"Posted comment (table) id={res.get('id', '?')} on {issue_key} ({tier}; failure table disabled)"
                    )
                except RuntimeError:
                    raise
            else:
                raise

    if include_epic_in_progress:
        epic_doc: dict[str, Any] = {
            "type": "doc",
            "version": 1,
            "content": [
                adf_paragraph(
                    adf_text(
                        f"FreeIPA CI — umbrella epic child issues (posted once for RHEL {rhel}; tiers: {', '.join(tiers)})."
                    )
                )
            ],
        }
        epic_doc = merge_epic_in_progress_into_comment_doc(
            epic_doc, base=base, email=email, token=token
        )
        try:
            res = post_comment(base, email, token, issue_key, epic_doc)
            epic_posted = True
            print(
                f"Posted comment (epic-in-progress) id={res.get('id', '?')} on {issue_key}"
            )
        except RuntimeError as exc:
            if _is_content_limit_exceeded(exc):
                print(
                    "warning: epic In Progress section comment too large; skipped.",
                    file=sys.stderr,
                )
            else:
                raise

    if include_epic_in_progress and not epic_posted:
        print(
            "warning: epic In Progress section was enabled but not posted.",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    raw, rhel_from_for = strip_optional_for_rhel(raw)
    p = argparse.ArgumentParser(
        description=(
            "Post FreeIPA CI analyzer summary to Jira (ADF table + optional full short report). "
            "May append non-Closed umbrella issues (with status) under epic REPORT_LOGS_IDM_5601_PARENT_KEY (FREEIPA_JIRA_EPIC_IN_PROGRESS_SECTION)."
        ),
        usage=(
            "post-freeipa-jira-comment [for RHEL] [--rhel VERSION] [--env-file PATH] "
            "--jira-issue-key KEY [--env-override] [--dry-run] TIER [TIER ...]"
        ),
    )
    p.add_argument(
        "--rhel",
        metavar="VERSION",
        default=None,
        help="RHEL stream for artifacts (e.g. 9.8). Overrides a leading "
        "'for RHEL…' token. Default: env FREEIPA_RHEL_VERSION or 9.8.",
    )
    p.add_argument(
        "--jira-issue-key",
        metavar="KEY",
        required=True,
        help="Jira issue to comment on (e.g. IDM-5885). Not taken from JIRA_ISSUE_KEY / JIRA_ISSUE.",
    )
    p.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help="Load KEY=value pairs into the environment (same keys as ~/.config/wtmcp/env.d/jira.env).",
    )
    p.add_argument(
        "--env-override",
        action="store_true",
        help="With --env-file, values from the file override existing environment variables.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print ADF JSON and exit without posting.",
    )
    p.add_argument(
        "tiers",
        nargs="+",
        metavar="TIER",
        help="Pipeline tier label(s) as published on idm-artifacts, e.g. Nightly-Tier1 Nightly-Tier2.",
    )
    args = p.parse_args(raw)

    if args.env_file:
        load_env_file(args.env_file, override=args.env_override)

    # AI Suggested Known Issue column needs Jira lookups; default on for this CLI only (MCP defaults off).
    if "REPORT_LOGS_KNOWN_ISSUE_LINKS" not in os.environ:
        os.environ["REPORT_LOGS_KNOWN_ISSUE_LINKS"] = "1"

    base, email, token = jira_credentials()
    issue_key = (args.jira_issue_key or "").strip()

    if not base or not email or not token:
        print(
            "error: JIRA_URL, JIRA_EMAIL, and JIRA_TOKEN (or JIRA_API_TOKEN) are required.",
            file=sys.stderr,
        )
        return 2
    if not issue_key:
        print(
            "error: --jira-issue-key must be a non-empty issue key (e.g. IDM-5885).",
            file=sys.stderr,
        )
        return 2

    rhel = args.rhel or rhel_from_for or os.environ.get("FREEIPA_RHEL_VERSION", "9.8")
    rhel = normalize_rhel_version(rhel.strip())
    tiers = [t.strip() for t in args.tiers if t.strip()]
    if not tiers:
        print("error: supply at least one TIER (e.g. Nightly-Tier1).", file=sys.stderr)
        return 2
    style = os.environ.get("FREEIPA_REPORT_STYLE", "short").strip().lower()

    mode = os.environ.get("FREEIPA_JIRA_POST_MODE", "table").strip().lower()
    if mode not in ("table", "full", "both"):
        print("error: FREEIPA_JIRA_POST_MODE must be table, full, or both.", file=sys.stderr)
        return 2

    intro = os.environ.get(
        "FREEIPA_JIRA_INTRO",
        f"FreeIPA CI — merged pipeline JUnit (report-logs analyze_freeipa_ci_artifacts, {style}). RHEL {rhel}.",
    ).strip()

    footer = os.environ.get("FREEIPA_JIRA_TABLE_FOOTER", "").strip() or None

    fetches = run_fetch_for_tiers(rhel, tiers)
    table_rows = build_table_rows_from_parse_results(fetches)
    reports = render_reports_text_from_fetches(rhel, fetches, style)

    try:
        detail_limit = int(os.environ.get("FREEIPA_JIRA_FAILURE_DETAIL_LIMIT", "1200"))
    except ValueError:
        detail_limit = 1200
    raw_max = os.environ.get("FREEIPA_JIRA_FAILURE_TABLE_MAX_ROWS", "").strip()
    try:
        max_rows = int(raw_max) if raw_max else None
    except ValueError:
        max_rows = None
    include_failure_table = _env_truthy("FREEIPA_JIRA_FAILURE_TABLE", True)
    failure_section_title = (
        os.environ.get("FREEIPA_JIRA_FAILURE_TABLE_TITLE", "Failing tests (per JUnit)").strip()
        or "Failing tests (per JUnit)"
    )

    parts = [
        "Generated by analyze_freeipa_ci_artifacts (%s).\n" % style,
        "",
        "Scope: RHEL %s — tiers: %s\n" % (rhel, ", ".join(tiers)),
        "",
    ]
    for tier, rep in reports:
        parts.append(f"### {tier}\n\n")
        parts.append(strip_per_job_fetch_lines(rep))
        parts.append("\n")
    full_text = "".join(parts)

    summary_doc = adf_table_comment(intro=intro, rows=table_rows, footer=footer)
    failure_rows: list[tuple[str, str, str | None, str, str, str]] = []
    failure_doc_inner: list[dict[str, Any]] | None = None
    if include_failure_table:
        failure_rows = failure_rows_for_tiers(
            fetches,
            detail_limit=detail_limit,
            max_rows=max_rows,
        )
        failure_doc_inner = adf_failure_detail_table(
            section_title=failure_section_title,
            row_tuples=failure_rows,
        )["content"]

    summary_plus_failure: dict[str, Any] = summary_doc
    if failure_doc_inner is not None:
        summary_plus_failure = _merge_adf_docs(
            summary_doc,
            {"type": "doc", "version": 1, "content": failure_doc_inner},
        )

    if mode == "table":
        doc = summary_plus_failure
        label = "table"
    elif mode == "full":
        inner: list[dict[str, Any]] = [adf_paragraph(adf_text(intro))]
        if failure_doc_inner is not None:
            inner.extend(failure_doc_inner)
        inner.extend(plain_chunked_code_blocks(full_text))
        doc = {"type": "doc", "version": 1, "content": inner}
        label = "full"
    else:
        doc = adf_with_plain_append(summary_plus_failure, full_text)
        label = "both"

    include_epic_in_progress = _env_truthy("FREEIPA_JIRA_EPIC_IN_PROGRESS_SECTION", True)
    if include_epic_in_progress:
        doc = merge_epic_in_progress_into_comment_doc(
            doc, base=base, email=email, token=token
        )

    if args.dry_run:
        print(f"--- {label} (dry-run) ---")
        s = json.dumps(doc, indent=2)
        print(s[:12000] + ("...(truncated)\n" if len(s) > 12000 else ""))
        return 0

    return _post_with_content_limit_fallback(
        base=base,
        email=email,
        token=token,
        issue_key=issue_key,
        doc=doc,
        label=label,
        rhel=rhel,
        tiers=tiers,
        intro=intro,
        footer=footer,
        style=style,
        mode=mode,
        include_failure_table=include_failure_table,
        failure_section_title=failure_section_title,
        detail_limit=detail_limit,
        max_rows=max_rows,
        include_epic_in_progress=include_epic_in_progress,
    )


if __name__ == "__main__":
    raise SystemExit(main())

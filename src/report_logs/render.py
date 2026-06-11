from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable

from report_logs.models import ParseResult, TestFailure

AI_INSIGHTS_COLUMN_HEADER = "AI Insights"


def _cluster_key(f) -> str:
    base = f"{f.classname}.{f.name}".strip(".")
    return base or f.name or "unknown"


def _failure_ident(f) -> str:
    if f.classname and f.name:
        return f"{f.classname}.{f.name}".strip(".")
    return (f.name or f.classname or "unknown").strip() or "unknown"


def _clip_message(msg: str, limit: int = 360) -> str:
    one = " ".join(msg.split())
    if len(one) <= limit:
        return one
    return one[: limit - 1] + "…"


def render_brief(
    result: ParseResult,
    *,
    title: str = "JUnit summary",
    axis_lines: list[str] | None = None,
    max_clusters: int = 3,
) -> str:
    """Compact status (brief report style)."""
    bad = result.failures + result.errors
    lines = [
        f"{title}: {'PASS' if bad == 0 else 'FAIL'} — "
        f"{bad} failed of {result.tests} tests",
    ]
    if axis_lines:
        lines.append(" | ".join(axis_lines))
    keys = [_cluster_key(f) for f in result.failures_detail]
    counts = Counter(keys)
    top = counts.most_common(max_clusters)
    for key, n in top:
        sample = next(
            (f for f in result.failures_detail if _cluster_key(f) == key),
            None,
        )
        hint = (sample.message[:80] + "…") if sample and len(sample.message) > 80 else (
            sample.message if sample else ""
        )
        lines.append(f"• {key} — {n} — {hint}")
    return "\n".join(lines)


def render_short(
    result: ParseResult,
    *,
    header: str = "## CI results",
    artifact_url: str | None = None,
    table_rows: list[tuple[str, str, int, int, int]] | None = None,
) -> str:
    """Longer markdown summary (short report style)."""
    bad = result.failures + result.errors
    parts = [header, ""]
    if artifact_url:
        parts.extend([f"**Artifacts:** {artifact_url}", ""])

    if table_rows:
        parts.append("| Tier/OS | Pass | Fail | Skip |")
        parts.append("|---------|------|------|------|")
        for label, _os, p, f, sk in table_rows:
            parts.append(f"| {label} | {p} | {f} | {sk} |")
        parts.append("")
    else:
        parts.append(
            f"**Totals:** {result.passed} passed, {bad} failed, "
            f"{result.skipped} skipped (of {result.tests})."
        )
        parts.append("")

    parts.append("### Failures by suite")
    parts.append("")
    parts.append(
        "Grouped by **suite**: CI **job** folder when merging tier artifacts (`…/job-name/1/junit.xml`), "
        "otherwise the JUnit **testsuite** name or Python package path."
    )
    parts.append("")

    by_suite: dict[str, list] = {}
    for f in result.failures_detail:
        suite = f.suite_name or (
            f.classname.rsplit(".", 1)[0] if f.classname and "." in f.classname else (f.classname or "default")
        )
        by_suite.setdefault(suite, []).append(f)

    if not result.failures_detail:
        parts.append("_No failing tests with captured detail._")
        parts.append("")
    else:
        for suite in sorted(by_suite.keys(), key=str.casefold):
            rows = by_suite[suite]
            parts.append(f"#### {suite}")
            parts.append("")
            parts.append(f"*{len(rows)} failure(s) in this suite*")
            parts.append("")
            for f in rows:
                ident = _failure_ident(f)
                kind = (f.type or "failure").strip().lower()
                label = "failure" if kind == "failure" else "error"
                msg = _clip_message(f.message)
                parts.append(f"- **`{ident}`** — *({label})* {msg}")
            parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def _escape_md_table_cell(s: str) -> str:
    one = " ".join(s.split())
    return one.replace("|", "\\|")


def _http_url_for_suite_link(url: str | None) -> str | None:
    u = (url or "").strip()
    if u.startswith(("http://", "https://")):
        return u
    return None


def _suite_name_markdown_cell(suite_display: str, report_html_url: str | None) -> str:
    """Markdown table cell: linked suite label when *report_html_url* is an http(s) URL."""
    s = (suite_display or "").strip() or "—"
    esc = _escape_md_table_cell(s)
    href = _http_url_for_suite_link(report_html_url)
    if href:
        return f"[{esc}]({href})"
    return esc


def _tier_cell_value(tier: str | None, run_label: str | None) -> str:
    """Pipeline tier (preferred); else optional run title for merged summaries."""
    if tier is not None and str(tier).strip():
        return str(tier).strip()
    if run_label is not None and str(run_label).strip():
        return str(run_label).strip()
    return "—"


def known_issue_empty_placeholder() -> str:
    """Text for the **AI Insights** column when no tracker match (env ``REPORT_LOGS_KNOWN_ISSUE_EMPTY``, default em dash)."""
    raw = os.environ.get("REPORT_LOGS_KNOWN_ISSUE_EMPTY", "—")
    if raw is None:
        return "—"
    s = str(raw).strip()
    return s if s else "—"


def known_issue_jira_links_enabled() -> bool:
    """
    When true, the **AI Insights** column is filled via Jira (IDM-5601 umbrella, JQL, child text).

    Default **false** — the column is still present but cells stay **empty** (no Jira calls, no
    em dash). Set ``REPORT_LOGS_KNOWN_ISSUE_LINKS=1`` to enable linking and placeholder text
    when there is no match.
    """
    return os.environ.get("REPORT_LOGS_KNOWN_ISSUE_LINKS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _known_issue_cell(f: TestFailure) -> str:
    """AI Insights column: optional Jira IDM-5601 children when :func:`known_issue_jira_links_enabled`."""
    if not known_issue_jira_links_enabled():
        return ""
    from report_logs.known_issues import known_issue_markdown_idm_5601

    return known_issue_markdown_idm_5601(f)


def _known_issue_fallback_placeholder(*, explicit_resolver: bool) -> str:
    """Placeholder when the resolver returned nothing; blank column when Jira linking is off."""
    if explicit_resolver or known_issue_jira_links_enabled():
        return known_issue_empty_placeholder()
    return ""


def _blocked_reason_cell(insights_md: str) -> str:
    """Blocked Reason short links from AI Insights issue keys (Jira custom field)."""
    if not known_issue_jira_links_enabled():
        return ""
    empty = known_issue_empty_placeholder()
    known = (insights_md or "").strip()
    if not known or known in (empty, "—"):
        return ""
    from report_logs.jira_child_issues import blocked_reason_markdown_for_insights

    return blocked_reason_markdown_for_insights(known)


def iter_failure_table_rows(
    result: ParseResult,
    *,
    tier: str | None = None,
    run_label: str | None = None,
    detail_limit: int = 1200,
    known_issue_for: Callable[[TestFailure], str] | None = None,
) -> list[tuple[str, str, str | None, str, str, str, str]]:
    """
    Row tuples: **Tier**, **Suite name** (plain text), optional **report.html** URL for that
    suite/job, **Test name**, **Failure Details**, **AI Insights**, **Blocked Reason** (same
    semantics as :func:`render_failure_table`).
    """
    bad = result.failures + result.errors
    ki_fn = known_issue_for or _known_issue_cell
    explicit_resolver = known_issue_for is not None
    tc_base = _tier_cell_value(tier, run_label)

    if not result.failures_detail:
        if bad == 0:
            msg = "No failing tests in this result."
        else:
            msg = "Failures were counted but no per-test detail was captured in JUnit."
        return [(tc_base, "—", None, "—", msg, _known_issue_fallback_placeholder(explicit_resolver=explicit_resolver), "")]

    rows: list[tuple[str, str, str | None, str, str, str, str]] = []
    for f in result.failures_detail:
        suite = (f.suite_name or "").strip() or "—"
        href = _http_url_for_suite_link(f.report_html_url)
        ident = _failure_ident(f)
        kind = (f.type or "failure").strip().lower()
        prefix = "[error] " if kind == "error" else "[failure] "
        raw_detail = prefix + (f.message or "")
        one = " ".join(raw_detail.split())
        if len(one) > detail_limit:
            one = one[: detail_limit - 1] + "…"
        known = (ki_fn(f) or "").strip()
        if not known:
            known = _known_issue_fallback_placeholder(explicit_resolver=explicit_resolver)
        blocked = _blocked_reason_cell(known)
        rows.append((tc_base, suite, href, ident, one, known, blocked))
    return rows


def render_failure_table(
    result: ParseResult,
    *,
    header: str = "## Failure report (table)",
    artifact_url: str | None = None,
    tier: str | None = None,
    run_label: str | None = None,
    detail_limit: int = 1200,
    known_issue_for: Callable[[TestFailure], str] | None = None,
) -> str:
    """
    Markdown table columns: **Tier**, **Suite name**, **Test name**, **Failure Details**, **AI Insights**.

    When failures were parsed from pipeline JUnit URLs (``…/job/1/junit.xml``), **Suite name**
    is a markdown link to ``…/job/1/report.html`` for that job.

    **tier** is the pipeline label (e.g. ``Nightly-Tier1``). If omitted, **run_label**
    is used for the Tier column (legacy merged-run titles). Otherwise ``—``.
    """
    parts = [header, ""]
    if artifact_url:
        parts.extend([f"**Artifacts:** {artifact_url}", ""])

    bad = result.failures + result.errors
    parts.append(
        f"**Totals:** {result.passed} passed, {bad} failed, "
        f"{result.skipped} skipped (of {result.tests})."
    )
    parts.append("")

    parts.append(
        f"| Tier | Suite name | Test name | Failure Details | {AI_INSIGHTS_COLUMN_HEADER} |"
    )
    parts.append(
        "|------|------------|-----------|-----------------|-------------|"
    )

    for tc, suite, suite_href, ident, detail, known, blocked in iter_failure_table_rows(
        result,
        tier=tier,
        run_label=run_label,
        detail_limit=detail_limit,
        known_issue_for=known_issue_for,
    ):
        suite_cell = _suite_name_markdown_cell(suite, suite_href)
        parts.append(
            f"| {_escape_md_table_cell(tc)} | {suite_cell} | "
            f"{_escape_md_table_cell(ident)} | {_escape_md_table_cell(detail)} | "
            f"{_escape_md_table_cell(known)} |"
        )

    parts.append("")
    return "\n".join(parts).rstrip() + "\n"

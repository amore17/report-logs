"""Map failing tests to tracker links for the **AI Insights** table column."""

from __future__ import annotations

import os

from report_logs.jira_child_issues import (
    collect_child_keys_matching_suite_summary,
    collect_child_work_items,
    fetch_known_issue_non_closed_matches_for_suite,
    jira_rest_credentials,
    known_issue_parent_key,
)
from report_logs.models import TestFailure

# Reference URL for documentation / legacy callers.
IDM_5601_URL = "https://redhat.atlassian.net/browse/IDM-5601"


def _jira_browse_url(issue_key: str) -> str:
    cred = jira_rest_credentials()
    if cred:
        return f"{cred[0]}/browse/{issue_key}"
    return f"https://redhat.atlassian.net/browse/{issue_key}"


def _known_issue_suppressed_for_test_name(test_name: str) -> bool:
    """No Known Issue row when JUnit **test name** is infra setup noise (exact match, case-insensitive)."""
    n = (test_name or "").strip().lower()
    return n in ("provision", "prep")


def _jira_child_match_enabled() -> bool:
    if os.environ.get("REPORT_LOGS_IDM_5601_DISABLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return False
    return os.environ.get("REPORT_LOGS_IDM_5601_JIRA_FETCH", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def known_issue_markdown_idm_5601(f: TestFailure) -> str:
    """
    Return markdown links for tracker matches, else empty string.

    **Primary:** all **non-Closed** umbrella children under :envvar:`REPORT_LOGS_IDM_5601_PARENT_KEY`
    (default **IDM-5601**) whose **summary** or **description** matches the failing **suite name**
    JQL needles — same umbrella scope as Known Issue (``parent`` / ``parentEpic`` / optional
    ``Epic Link``). Multiple hits are joined with `` · `` in the table cell (cap:
    ``REPORT_LOGS_KNOWN_ISSUE_LIST_MAX``).

    **Fallback:** loaded umbrella children whose **summary** matches the same suite needles as JQL
    (:func:`~report_logs.jira_child_issues.collect_child_keys_matching_suite_summary`). There is
    no looser token/haystack fallback here: if neither path matches, the cell stays empty (avoids
    false positives such as short segments like ``win2025`` matching unrelated trackers).

    Rows with **test name** ``provision`` or ``prep`` (case-insensitive) get no link—same empty cell
    path as no match (no Jira calls for those rows).

    ``REPORT_LOGS_IDM_5601_DISABLE=1`` or ``REPORT_LOGS_IDM_5601_JIRA_FETCH=0`` yields no link.
    Jira requires ``JIRA_URL``, ``JIRA_EMAIL``, ``JIRA_TOKEN``.
    """
    if not _jira_child_match_enabled():
        return ""
    if _known_issue_suppressed_for_test_name(f.name):
        return ""
    suite = f.suite_name or ""
    rows = fetch_known_issue_non_closed_matches_for_suite(suite)
    if rows:
        return " · ".join(f"[{k}]({_jira_browse_url(k)})" for k, _ in rows)
    pkey = known_issue_parent_key()
    items = collect_child_work_items(pkey)
    summary_keys = collect_child_keys_matching_suite_summary(
        suite, items, parent_key=pkey
    )
    if summary_keys:
        return " · ".join(f"[{k}]({_jira_browse_url(k)})" for k in summary_keys)
    return ""

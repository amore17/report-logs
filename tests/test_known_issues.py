"""Known-issue mapping for IDM-5601."""

from __future__ import annotations

import pytest

from report_logs import known_issues as ki
from report_logs.known_issues import (
    IDM_5601_URL,
    known_issue_markdown_idm_5601,
)
from report_logs.models import TestFailure


def test_idm_5601_link_via_jira_child_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ki,
        "fetch_known_issue_non_closed_matches_for_suite",
        lambda _suite: [("IDM-5601", "upstream-edns")],
    )
    monkeypatch.setattr(
        ki,
        "collect_child_work_items",
        lambda _k: [
            (
                "IDM-5601",
                "nightly concern: upstream-edns and dnsconfd",
                "[upstream-edns] dnsconfd nightly",
            ),
        ],
    )
    monkeypatch.setenv("REPORT_LOGS_IDM_5601_JIRA_FETCH", "1")
    f = TestFailure(
        suite_name="upstream-edns",
        classname="ipa.test_integration.test_edns.TestX",
        name="test_dot",
        message="error",
        type="failure",
    )
    out = known_issue_markdown_idm_5601(f)
    assert "[IDM-5601](" in out
    assert IDM_5601_URL in out


def test_known_issue_no_link_when_only_haystack_segment_would_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Haystack-only token overlap (e.g. win2025) must not link a different suite's tracker."""
    monkeypatch.setattr(ki, "fetch_known_issue_non_closed_matches_for_suite", lambda _s: [])
    monkeypatch.setattr(
        ki,
        "collect_child_work_items",
        lambda _k: [
            (
                "IDM-5738",
                "track upstream-xmlrpc-tests nightly failures on win2025 ad trust lab",
                "track upstream-xmlrpc-tests nightly failures",
            ),
        ],
    )
    monkeypatch.setenv("REPORT_LOGS_IDM_5601_JIRA_FETCH", "1")
    f = TestFailure(
        suite_name="upstream-trust-win2025",
        classname="ipa.tests.test_trust.TestTrust",
        name="test_foo",
        message="m",
        type="failure",
    )
    assert known_issue_markdown_idm_5601(f) == ""


def test_known_issue_summary_fallback_from_loaded_children(monkeypatch: pytest.MonkeyPatch) -> None:
    """When JQL returns nothing, match umbrella children by suite string in summary."""
    monkeypatch.setattr(ki, "fetch_known_issue_non_closed_matches_for_suite", lambda _s: [])
    monkeypatch.setattr(
        ki,
        "collect_child_work_items",
        lambda _k: [
            (
                "IDM-6100",
                "upstream-xmlrpc-tests nightly tracker summary field",
                "track upstream-xmlrpc-tests nightly failures",
            ),
        ],
    )
    monkeypatch.setenv("REPORT_LOGS_IDM_5601_JIRA_FETCH", "1")
    f = TestFailure(
        suite_name="upstream-xmlrpc-tests",
        classname="ipa.tests.test_xmlrpc",
        name="test_ping",
        message="rpc fail",
        type="failure",
    )
    out = known_issue_markdown_idm_5601(f)
    assert "[IDM-6100](" in out
    assert " · " not in out


def test_known_issue_blank_when_test_name_provision_or_prep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Infra-style test names get no Known Issue links (no Jira lookups needed)."""
    calls = {"fetch": 0}

    def counting_fetch(suite: str) -> list[tuple[str, str]]:
        calls["fetch"] += 1
        return [("IDM-7777", "hit")]

    monkeypatch.setattr(ki, "fetch_known_issue_non_closed_matches_for_suite", counting_fetch)
    monkeypatch.setenv("REPORT_LOGS_IDM_5601_JIRA_FETCH", "1")

    for test_name in ("provision", "PROVISION ", "prep"):
        calls["fetch"] = 0
        f = TestFailure(
            suite_name="upstream-xmlrpc-tests",
            classname="ipa.tests.test_x",
            name=test_name,
            message="m",
            type="failure",
        )
        assert known_issue_markdown_idm_5601(f) == ""
        assert calls["fetch"] == 0

    f_ok = TestFailure(
        suite_name="upstream-xmlrpc-tests",
        classname="ipa.tests.test_x",
        name="test_provision",
        message="m",
        type="failure",
    )
    assert known_issue_markdown_idm_5601(f_ok) != ""


def test_known_issue_lists_multiple_jql_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-closed JQL matches produce several markdown links separated by middle dot."""
    monkeypatch.setattr(
        ki,
        "fetch_known_issue_non_closed_matches_for_suite",
        lambda _suite: [
            ("IDM-1111", "suite A"),
            ("IDM-2222", "suite B"),
        ],
    )
    monkeypatch.setenv("REPORT_LOGS_IDM_5601_JIRA_FETCH", "1")
    f = TestFailure(
        suite_name="upstream-xmlrpc-tests",
        classname="ipa.tests.test_xmlrpc",
        name="test_ping",
        message="rpc fail",
        type="failure",
    )
    out = known_issue_markdown_idm_5601(f)
    assert " · " in out
    assert "[IDM-1111](" in out and "[IDM-2222](" in out
    assert out.index("[IDM-1111]") < out.index("[IDM-2222]")


def test_known_issue_links_child_subtask_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jira child IDM-5738 when summary matches suite."""
    monkeypatch.setattr(
        ki,
        "collect_child_work_items",
        lambda _k: [
            (
                "IDM-5738",
                "track upstream-xmlrpc-tests nightly failures",
                "upstream-xmlrpc-tests nightly failures",
            ),
        ],
    )
    monkeypatch.setenv("REPORT_LOGS_IDM_5601_JIRA_FETCH", "1")
    f = TestFailure(
        suite_name="upstream-xmlrpc-tests",
        classname="ipa.tests.test_xmlrpc",
        name="test_ping",
        message="rpc fail",
        type="failure",
    )
    out = known_issue_markdown_idm_5601(f)
    assert "[IDM-5738](" in out
    assert "https://redhat.atlassian.net/browse/IDM-5738" in out


def test_known_issue_no_pagure_when_jira_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pagure is not used; Jira off yields no known-issue link."""
    monkeypatch.setattr(ki, "collect_child_work_items", lambda _k: [])
    monkeypatch.setenv("REPORT_LOGS_IDM_5601_JIRA_FETCH", "0")
    f = TestFailure(
        suite_name="upstream-xmlrpc-tests",
        classname="c",
        name="test_x",
        message="msg",
        type="failure",
    )
    assert known_issue_markdown_idm_5601(f) == ""


def test_no_pagure_when_no_jira_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Jira child match → empty string (no Pagure fallback)."""
    monkeypatch.setattr(ki, "collect_child_work_items", lambda _k: [])
    f = TestFailure(
        suite_name="upstream-edns",
        classname="ipa.test_integration.test_edns.TestX",
        name="test_dot",
        message="dnf install dnsconfd failed",
        type="failure",
    )
    assert known_issue_markdown_idm_5601(f) == ""


def test_idm_5601_no_match_clean_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ki, "collect_child_work_items", lambda _k: [])
    monkeypatch.delenv("REPORT_LOGS_IDM_5601_PATTERNS", raising=False)
    f = TestFailure(
        suite_name="upstream-dns",
        classname="ipa.tests.test_foo",
        name="test_bar",
        message="AssertionError: unexpected",
        type="failure",
    )
    assert known_issue_markdown_idm_5601(f) == ""


def test_idm_5601_patterns_env_does_not_add_umbrella_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy ``REPORT_LOGS_IDM_5601_PATTERNS`` no longer maps to IDM-5601 without Jira/Pagure."""
    monkeypatch.setattr(ki, "collect_child_work_items", lambda _k: [])
    monkeypatch.setenv("REPORT_LOGS_IDM_5601_PATTERNS", "unexpected")
    f = TestFailure(
        suite_name="x",
        classname="y",
        name="z",
        message="unexpected topology error",
        type="failure",
    )
    assert known_issue_markdown_idm_5601(f) == ""
    monkeypatch.delenv("REPORT_LOGS_IDM_5601_PATTERNS", raising=False)


def test_idm_5601_disable_yields_no_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """REPORT_LOGS_IDM_5601_DISABLE skips Jira matching (no Pagure)."""
    monkeypatch.setenv("REPORT_LOGS_IDM_5601_DISABLE", "1")
    monkeypatch.setattr(ki, "collect_child_work_items", lambda _k: [])
    f = TestFailure(
        suite_name="upstream-edns",
        classname="c",
        name="n",
        message="dnsconfd",
        type="failure",
    )
    assert known_issue_markdown_idm_5601(f) == ""

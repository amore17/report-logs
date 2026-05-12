"""Jira child-issue text matching for known issues."""

from __future__ import annotations

import pytest

import report_logs.jira_child_issues as jci
from report_logs.jira_child_issues import (
    failure_match_tokens,
    failure_matches_child_work_items,
    failure_matching_child_issue_key,
)


def test_failure_matches_upstream_suite_in_child_summary() -> None:
    blobs = [
        "upstream-edns fails on rhel 9.8 — track dnsconfd install",
    ]
    assert failure_matches_child_work_items(
        "upstream-edns",
        "ipa.test_integration.test_edns.TestDNS",
        "test_dot",
        blobs,
    )


def test_upstream_edns_no_match_on_ci_log_url_alone() -> None:
    """Suite segment ``upstream`` is not used; unrelated CI path text must not match."""
    assert not failure_matches_child_work_items(
        "upstream-edns",
        "ipa.test_integration.test_edns.TestDNS",
        "test_dot",
        ["log at idm-ci/nightly-aggregate/tier1/job"],
    )


def test_failure_matches_classname_segment() -> None:
    blobs = [
        "investigate TestDNSOverTLS failures in nightly tier1",
    ]
    assert failure_matches_child_work_items(
        "pytest — suite",
        "ipa.test_integration.test_edns.TestDNSOverTLS",
        "test_foo",
        blobs,
    )


def test_failure_no_spurious_short_token() -> None:
    blobs = ["dns only mentioned"]
    assert not failure_matches_child_work_items(
        "x",
        "y",
        "short",
        blobs,
    )


def test_failure_matching_child_issue_key_class_segment_in_child() -> None:
    """Failed class path segment (e.g. test_edns) matches child summary."""
    items = [
        ("IDM-111", "nightly regression test_edns on el10", "edns tier regression"),
    ]
    assert (
        failure_matching_child_issue_key(
            "pytest-tier",
            "ipa.test_integration.test_edns.TestDNS",
            "test_dot",
            items,
        )
        == "IDM-111"
    )


def test_failure_matching_child_issue_key_test_name_in_child() -> None:
    items = [
        ("IDM-222", "flake test_su_ad_user in trust suite", "trust functional"),
    ]
    assert (
        failure_matching_child_issue_key(
            "upstream-trust",
            "ipa.tests.test_trust.TestTrust",
            "test_su_ad_user",
            items,
        )
        == "IDM-222"
    )


def test_failure_matching_child_issue_key_suite_segment_in_child() -> None:
    """Hyphenated suite yields segments (e.g. xmlrpc) that match child text."""
    items = [
        ("IDM-5601", "umbrella", "umbrella parent"),
        ("IDM-9999", "track xmlrpc nightly regression", "xmlrpc nightly"),
    ]
    assert (
        failure_matching_child_issue_key(
            "upstream-xmlrpc-tests",
            "ipa.tests.test_x.TestY",
            "test_ping",
            items,
        )
        == "IDM-9999"
    )


def test_failure_match_tokens_includes_pytest_style() -> None:
    t = failure_match_tokens(
        "tier1",
        "ipa.test_integration.test_edns.TestDNS",
        "test_dot",
    )
    assert "ipa.test_integration.test_edns.testdns::test_dot" in t
    assert "testdns::test_dot" in t


def test_failure_matching_pytest_style_in_child_text() -> None:
    """Pytest/JUnit failure lines pasted into child text match via :: tokens."""
    items = [
        ("IDM-333", "see ipa.test_integration.test_edns.testdns::test_dot in ci", "ci ticket"),
    ]
    assert (
        failure_matching_child_issue_key(
            "pytest — suite",
            "ipa.test_integration.test_edns.TestDNS",
            "test_dot",
            items,
        )
        == "IDM-333"
    )


def test_failure_matching_child_issue_key_first_match() -> None:
    items = [
        ("IDM-5601", "catch-all umbrella text", "umbrella meta"),
        ("IDM-5738", "upstream-xmlrpc-tests tracker", "upstream-xmlrpc-tests"),
    ]
    assert (
        failure_matching_child_issue_key(
            "upstream-xmlrpc-tests",
            "ipa.tests.test_x",
            "test_long_name",
            items,
        )
        == "IDM-5738"
    )


def test_jql_summary_search_used_before_empty_child_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """JQL umbrella scope + summary/description clauses run when credentials exist."""
    jci._jql_summary_cache.clear()
    captured: dict[str, str | None] = {}

    def fake_cred() -> tuple[str, str, str]:
        return ("https://example.atlassian.net", "user@example.com", "tok")

    def fake_http(
        method: str,
        url: str,
        *,
        email: str,
        token: str,
        payload: dict | None = None,
        timeout: float = 60.0,
    ):
        captured["jql"] = (payload or {}).get("jql")
        return {"issues": [{"key": "IDM-7777"}]}

    monkeypatch.setattr(jci, "jira_rest_credentials", fake_cred)
    monkeypatch.setattr(jci, "_http_json", fake_http)
    monkeypatch.delenv("REPORT_LOGS_IDM_5601_SUITE_ISSUE_PINS", raising=False)
    assert failure_matching_child_issue_key("zzz-unpinned-ci-suite", "c", "n", []) == "IDM-7777"
    jql = captured.get("jql") or ""
    assert "parent = IDM-5601" in jql
    assert "Epic Link" not in jql
    assert "parentEpic" in jql
    assert jql.count("summary ~") == 1
    assert "summary ~" in jql
    assert "zzz-unpinned-ci-suite" in jql
    assert "order by created desc" in jql.lower()
    assert 'status != "Closed"' in jql
    assert "description ~" in jql


def test_jql_prefers_summary_match_over_newer_description_only_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Several JQL hits: prefer summary match over a newer issue that matched description only."""
    jci._jql_summary_cache.clear()

    def fake_cred() -> tuple[str, str, str]:
        return ("https://example.atlassian.net", "user@example.com", "tok")

    def fake_http(
        method: str,
        url: str,
        *,
        email: str,
        token: str,
        payload: dict | None = None,
        timeout: float = 60.0,
    ):
        return {
            "issues": [
                {
                    "key": "IDM-1111",
                    "fields": {"summary": "unrelated generic regression triage"},
                },
                {
                    "key": "IDM-5738",
                    "fields": {
                        "summary": "track upstream-xmlrpc-tests nightly failures",
                    },
                },
            ]
        }

    monkeypatch.setattr(jci, "jira_rest_credentials", fake_cred)
    monkeypatch.setattr(jci, "_http_json", fake_http)
    monkeypatch.delenv("REPORT_LOGS_IDM_5601_SUITE_ISSUE_PINS", raising=False)
    assert (
        failure_matching_child_issue_key("upstream-xmlrpc-tests", "c", "n", [])
        == "IDM-5738"
    )


def test_known_issue_parent_key_rejects_invalid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORT_LOGS_IDM_5601_PARENT_KEY", "not-a-jira-key")
    assert jci.known_issue_parent_key() == "IDM-5601"


def test_jql_scope_includes_epic_link_when_env_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in restores Epic Link in umbrella JQL for legacy three-way scope."""
    jci._jql_summary_cache.clear()
    captured: dict[str, str | None] = {}
    monkeypatch.setenv("REPORT_LOGS_IDM_5601_INCLUDE_EPIC_LINK", "1")

    def fake_cred() -> tuple[str, str, str]:
        return ("https://example.atlassian.net", "user@example.com", "tok")

    def fake_http(
        method: str,
        url: str,
        *,
        email: str,
        token: str,
        payload: dict | None = None,
        timeout: float = 60.0,
    ):
        captured["jql"] = (payload or {}).get("jql")
        return {"issues": [{"key": "IDM-7777"}]}

    monkeypatch.setattr(jci, "jira_rest_credentials", fake_cred)
    monkeypatch.setattr(jci, "_http_json", fake_http)
    monkeypatch.delenv("REPORT_LOGS_IDM_5601_SUITE_ISSUE_PINS", raising=False)
    assert failure_matching_child_issue_key("zzz-ci-suite-epic", "c", "n", []) == "IDM-7777"
    jql = captured.get("jql") or ""
    assert "Epic Link" in jql
    assert "parentEpic" in jql


def test_jql_prefers_env_suite_issue_pin_among_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """When JQL returns several issues, ``REPORT_LOGS_IDM_5601_SUITE_ISSUE_PINS`` can pick the key."""
    jci._jql_summary_cache.clear()
    monkeypatch.setenv(
        "REPORT_LOGS_IDM_5601_SUITE_ISSUE_PINS",
        '{"upstream-edns": "IDM-5515"}',
    )

    def fake_cred() -> tuple[str, str, str]:
        return ("https://example.atlassian.net", "user@example.com", "tok")

    def fake_http(
        method: str,
        url: str,
        *,
        email: str,
        token: str,
        payload: dict | None = None,
        timeout: float = 60.0,
    ):
        return {
            "issues": [
                {"key": "IDM-7777"},
                {"key": "IDM-5515"},
            ]
        }

    monkeypatch.setattr(jci, "jira_rest_credentials", fake_cred)
    monkeypatch.setattr(jci, "_http_json", fake_http)
    assert failure_matching_child_issue_key("upstream-edns", "c", "n", []) == "IDM-5515"


def test_jql_escape_summary_contains() -> None:
    assert jci._jql_escape_summary_contains('a"b\\c') == 'a\\"b\\\\c'


def test_suite_name_in_jira_summary_before_token_match_in_body() -> None:
    """Phase 1: summary contains suite string wins over an earlier issue matching hay only."""
    items = [
        ("IDM-100", "track xmlrpc keyword in description only", "unrelated topic"),
        ("IDM-200", "short body", "upstream-xmlrpc-tests nightly"),
    ]
    assert (
        failure_matching_child_issue_key(
            "upstream-xmlrpc-tests",
            "ipa.tests.test_x.TestY",
            "test_ping",
            items,
        )
        == "IDM-200"
    )


def test_suite_name_in_summary_space_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Child summary uses spaces; JUnit suite uses hyphens — still link on summary match."""
    monkeypatch.setattr(jci, "_jql_summary_suite_search_wanted", lambda: False)
    items = [
        ("IDM-100", "xmlrpc mentioned in body not summary", "different tracker title"),
        ("IDM-400", "body", "upstream xmlrpc tests nightly regression"),
    ]
    assert (
        failure_matching_child_issue_key(
            "upstream-xmlrpc-tests",
            "ipa.tests.test_x.TestY",
            "test_ping",
            items,
        )
        == "IDM-400"
    )

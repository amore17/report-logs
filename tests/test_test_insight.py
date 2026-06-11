"""Tests for AI Insight classification (flaky / regression)."""

from __future__ import annotations

import pytest

from report_logs.models import TestFailure
from report_logs.test_insight import (
    build_ai_insight_cell,
    classify_historical_pattern,
    failure_history_key,
    is_prep_or_provision_test_name,
    prior_outcomes_for_key,
)


def test_is_prep_or_provision() -> None:
    assert is_prep_or_provision_test_name("prep")
    assert is_prep_or_provision_test_name(" PROVISION")
    assert not is_prep_or_provision_test_name("test_provision")


def test_classify_regression() -> None:
    assert classify_historical_pattern([False, False, False]) == "REGRESSION"
    assert classify_historical_pattern([False, None, False]) == "REGRESSION"


def test_classify_flaky() -> None:
    assert classify_historical_pattern([True, False, True]) == "FLEAKY TEST"
    assert classify_historical_pattern([False, True]) == "FLEAKY TEST"


def test_classify_chronic_no_tag() -> None:
    assert classify_historical_pattern([True, True, True]) is None
    assert classify_historical_pattern([]) is None


def test_prior_outcomes_absent_key_means_passed() -> None:
    assert prior_outcomes_for_key({}, "missing", 3) == [False, False, False]


def test_build_ai_insight_regression_not_for_prep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPORT_LOGS_KNOWN_ISSUE_LINKS", raising=False)
    f = TestFailure(
        suite_name="s",
        classname="C",
        name="prep",
        message="m",
        type="failure",
    )
    out = build_ai_insight_cell(f, prior_failed=[False, False], known_issue="")
    assert out == ""
    assert "REGRESSION" not in out


def test_build_ai_insight_regression_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPORT_LOGS_KNOWN_ISSUE_LINKS", raising=False)
    f = TestFailure(
        suite_name="s",
        classname="ipa.tests.test_x.TestY",
        name="test_z",
        message="m",
        type="failure",
    )
    out = build_ai_insight_cell(f, prior_failed=[False, False, False], known_issue="")
    assert out == "REGRESSION"


def test_build_ai_insight_flaky_and_jira(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPORT_LOGS_KNOWN_ISSUE_LINKS", raising=False)
    f = TestFailure(
        suite_name="s",
        classname="C",
        name="t",
        message="m",
        type="failure",
    )
    ki = "[IDM-99](https://redhat.atlassian.net/browse/IDM-99)"
    out = build_ai_insight_cell(f, prior_failed=[True, False, True], known_issue=ki)
    assert out.startswith("FLEAKY TEST")
    assert "IDM-99" in out


def test_failure_history_key_includes_suite() -> None:
    f = TestFailure(
        suite_name="upstream-dns",
        classname="ipa.test_dns.TestFoo",
        name="test_bar",
        message="",
        type="failure",
    )
    assert "upstream-dns" in failure_history_key(f)
    assert "test_bar" in failure_history_key(f)

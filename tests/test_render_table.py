"""Tests for markdown failure table rendering."""

import pytest

from report_logs.models import ParseResult, TestFailure
from report_logs.render import iter_failure_table_rows, render_failure_table


def test_render_failure_table_five_columns_no_tier():
    r = ParseResult(
        tests=2,
        failures=1,
        errors=0,
        skipped=0,
        failures_detail=[
            TestFailure(
                suite_name="upstream-dns",
                classname="ipa.test_dns.TestFoo",
                name="test_bar",
                message="boom",
                type="failure",
            )
        ],
    )
    out = render_failure_table(r, header="## H")
    assert "| Tier | Suite name | Test name | Failure Details | AI Insights |" in out
    assert "| — | upstream-dns |" in out
    assert "ipa.test_dns.TestFoo.test_bar" in out
    assert "[failure]" in out
    assert "boom" in out


def test_render_failure_table_with_run_label_fallback_tier():
    r = ParseResult(
        tests=1,
        failures=1,
        errors=0,
        skipped=0,
        failures_detail=[
            TestFailure(
                suite_name="j",
                classname="c",
                name="n",
                message="m",
                type="error",
            )
        ],
    )
    out = render_failure_table(r, run_label="Tier1 9.8")
    assert "| Tier | Suite name | Test name | Failure Details | AI Insights |" in out
    assert "| Tier1 9.8 | j |" in out
    assert "[error]" in out


def test_render_failure_table_tier_prefers_pipeline_name():
    r = ParseResult(
        tests=1,
        failures=1,
        errors=0,
        skipped=0,
        failures_detail=[
            TestFailure(
                suite_name="s",
                classname="c",
                name="n",
                message="x",
                type="failure",
            )
        ],
    )
    out = render_failure_table(r, tier="Nightly-Tier1", run_label="Long custom title")
    assert "| Nightly-Tier1 | s |" in out


def test_iter_failure_table_rows_six_tuples():
    r = ParseResult(
        tests=1,
        failures=1,
        errors=0,
        skipped=0,
        failures_detail=[
            TestFailure(
                suite_name="s",
                classname="C",
                name="t",
                message="e",
                type="failure",
            )
        ],
    )
    rows = iter_failure_table_rows(r, tier="Nightly-T1")
    assert len(rows) == 1
    assert rows[0] == ("Nightly-T1", "s", None, "C.t", "[failure] e", "")


def test_iter_failure_table_rows_known_issue_em_dash_when_links_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORT_LOGS_KNOWN_ISSUE_LINKS", "1")
    r = ParseResult(
        tests=1,
        failures=1,
        errors=0,
        skipped=0,
        failures_detail=[
            TestFailure(
                suite_name="s",
                classname="C",
                name="t",
                message="e",
                type="failure",
            )
        ],
    )
    rows = iter_failure_table_rows(r, tier="T")
    assert len(rows) == 1
    assert rows[0][-1] == "—"


def test_iter_failure_table_rows_includes_report_html_href():
    r = ParseResult(
        tests=1,
        failures=1,
        errors=0,
        skipped=0,
        failures_detail=[
            TestFailure(
                suite_name="upstream-dns",
                classname="C",
                name="t",
                message="e",
                type="failure",
                report_html_url="https://artifacts.example/tier-1/upstream-dns/1/report.html",
            )
        ],
    )
    rows = iter_failure_table_rows(r, tier="Nightly-T1")
    assert rows[0][1] == "upstream-dns"
    assert rows[0][2] == "https://artifacts.example/tier-1/upstream-dns/1/report.html"


def test_render_failure_table_suite_column_links_report_html():
    r = ParseResult(
        tests=1,
        failures=1,
        errors=0,
        skipped=0,
        failures_detail=[
            TestFailure(
                suite_name="upstream-dns",
                classname="a.b",
                name="c",
                message="x",
                type="failure",
                report_html_url="https://host/tier-1/upstream-dns/1/report.html",
            )
        ],
    )
    out = render_failure_table(r, tier="Nightly-Tier1")
    assert "https://host/tier-1/upstream-dns/1/report.html" in out
    assert "[upstream-dns]" in out


def test_render_failure_table_escapes_pipe():
    r = ParseResult(
        tests=1,
        failures=1,
        errors=0,
        skipped=0,
        failures_detail=[
            TestFailure(
                suite_name="a|b",
                classname="x",
                name="y",
                message="msg | tail",
                type="failure",
            )
        ],
    )
    out = render_failure_table(r)
    assert "\\|" in out or "a\\|b" in out

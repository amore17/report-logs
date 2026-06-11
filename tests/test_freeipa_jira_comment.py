"""Unit tests for Jira comment helpers (no HTTP)."""

from __future__ import annotations

from pathlib import Path

import pytest

from report_logs.models import ParseResult
from report_logs import freeipa_jira_comment as fjc
from report_logs.freeipa_jira_comment import (
    adf_failure_detail_table,
    adf_table_comment,
    build_table_rows_from_parse_results,
    build_table_rows_from_reports,
    dedupe_failure_rows_by_tier_suite,
    extract_totals,
    extract_title_folder,
    failure_doc_inner_for_rows,
    format_pass_fail_skip_cell,
    load_env_file,
    strip_optional_short,
    _is_content_limit_exceeded,
    _post_with_content_limit_fallback,
    strip_optional_for_rhel,
    strip_per_job_fetch_lines,
)


def test_strip_optional_for_rhel() -> None:
    assert strip_optional_for_rhel(["for", "RHEL9.8", "Nightly-Tier1"]) == (
        ["Nightly-Tier1"],
        "9.8",
    )
    assert strip_optional_for_rhel(["--help"]) == (["--help"], None)
    assert strip_optional_for_rhel(["for", "10.2", "a", "b"]) == (["a", "b"], "10.2")


def test_main_requires_jira_issue_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "a@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "t")
    with pytest.raises(SystemExit) as exc_info:
        fjc.main(["--rhel", "9.8", "Nightly-Tier1"])
    assert exc_info.value.code == 2


def test_run_fetch_for_all_tier_fips_signoff_expands_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_signoff(_rhel: str, tier: str) -> tuple[list[tuple[str, str, str]], str]:
        assert tier == "All-Tier-FIPS-Signoff"
        return (
            [("All-Tier-FIPS-Signoff (tier-1)", "https://example.test/run/tier-1/", "t1")],
            "run ok",
        )

    monkeypatch.setattr(fjc, "discover_signoff_pipeline_index_urls", fake_signoff)
    monkeypatch.setattr(
        fjc,
        "fetch_freeipa_ci_parse_result",
        lambda *a, **k: (ParseResult(tests=1, failures=0, errors=0, skipped=0, failures_detail=[]), "ok"),
    )
    out = fjc.run_fetch_for_tiers("9.9", ["All-Tier-FIPS-Signoff"])
    assert len(out) == 1
    assert out[0][0] == "All-Tier-FIPS-Signoff (tier-1)"


def test_run_fetch_for_all_tier_signoff_expands_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_signoff(_rhel: str, tier: str) -> tuple[list[tuple[str, str, str]], str]:
        assert tier == "All-Tier-Signoff"
        return (
            [
                ("All-Tier-Signoff (tier-1)", "https://example.test/run/tier-1/", "t1"),
                ("All-Tier-Signoff (tier-2)", "https://example.test/run/tier-2/", "t2"),
            ],
            "run ok",
        )

    def fake_fetch(
        rhel_version: str,
        tier: str,
        *,
        junit_relative_path: str | None = None,
        junit_xml_url: str | None = None,
        junit_xml_urls: list[str] | None = None,
        pipeline_index_url: str | None = None,
    ):
        n = 1 if "tier-1" in (pipeline_index_url or "") else 2
        return ParseResult(
            tests=n,
            failures=0,
            errors=0,
            skipped=0,
            failures_detail=[],
        ), f"merged-{n}"

    monkeypatch.setattr(fjc, "discover_signoff_pipeline_index_urls", fake_signoff)
    monkeypatch.setattr(fjc, "fetch_freeipa_ci_parse_result", fake_fetch)

    out = fjc.run_fetch_for_tiers("9.9", ["All-Tier-Signoff"])
    assert len(out) == 2
    assert out[0][0] == "All-Tier-Signoff (tier-1)"
    assert out[1][0] == "All-Tier-Signoff (tier-2)"
    assert out[0][1] is not None and out[0][1].tests == 1
    assert out[1][1] is not None and out[1][1].tests == 2


def test_run_fetch_for_tiers_uses_discover(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_disc(_rhel: str, _tier: str) -> tuple[str, str]:
        return "https://example.test/run/tier-1/", "discovered"

    def fake_fetch(
        rhel_version: str,
        tier: str,
        *,
        junit_relative_path: str | None = None,
        junit_xml_url: str | None = None,
        junit_xml_urls: list[str] | None = None,
        pipeline_index_url: str | None = None,
    ):
        assert pipeline_index_url == "https://example.test/run/tier-1/"
        return ParseResult(
            tests=1,
            failures=0,
            errors=0,
            skipped=0,
            failures_detail=[],
        ), "merged"

    monkeypatch.setattr(fjc, "discover_latest_pipeline_index_url", fake_disc)
    monkeypatch.setattr(fjc, "fetch_freeipa_ci_parse_result", fake_fetch)

    out = fjc.run_fetch_for_tiers("9.8", ["Nightly-Tier1"])
    assert len(out) == 1
    tier, pr, note = out[0]
    assert tier == "Nightly-Tier1"
    assert pr is not None and pr.tests == 1
    assert "discovered" in note and "merged" in note


def test_extract_totals() -> None:
    text = """
## X

**Totals:** 4284 passed, 6 failed, 8 skipped (of 4298).
"""
    assert extract_totals(text) == (4284, 6, 8, 4298)


def test_extract_title_folder() -> None:
    rep = """Discovered 2 job junit URL(s) under https://idm-artifacts.psi.redhat.com/idm-ci/freeipa/Nightly-Tier1/RHEL9.8/2026-04-27_16-00/tier-1/

## Nightly-Tier1 RHEL 9.8 (2026-04-27_16-00)
"""
    assert extract_title_folder(rep) == "2026-04-27_16-00"


def test_strip_fetch_lines() -> None:
    raw = "Merged 2.\nFetched JUnit XML from https://x.test/a.xml\nOK\n"
    assert "Fetched JUnit" not in strip_per_job_fetch_lines(raw)


def test_strip_optional_short() -> None:
    assert strip_optional_short(["short", "Nightly-Tier1"]) == (["Nightly-Tier1"], True)
    assert strip_optional_short(["--rhel", "9.8", "short"]) == (["--rhel", "9.8"], True)
    assert strip_optional_short(["Nightly-Tier1"]) == (["Nightly-Tier1"], False)


def test_failure_doc_inner_for_rows_table_vs_lines() -> None:
    rows = [
        (
            "Nightly-Tier1",
            "upstream-dns",
            "https://artifacts.example/report.html",
            "TestDNS.test_foo",
            "[failure] boom",
            "[IDM-6832](https://redhat.atlassian.net/browse/IDM-6832)",
            "[RHEL-4898](https://redhat.atlassian.net/browse/RHEL-4898)",
        )
    ]
    table_inner = failure_doc_inner_for_rows(
        section_title="Failing tests (per JUnit)",
        row_tuples=rows,
        as_labeled_lines=False,
    )
    assert any(b.get("type") == "table" for b in table_inner)
    headers = [
        c["content"][0]["content"][0]["text"]
        for c in next(b for b in table_inner if b.get("type") == "table")["content"][0]["content"]
    ]
    assert headers == ["Tier", "Suite Name", "AI Insights", "Blocked Reason"]
    line_inner = failure_doc_inner_for_rows(
        section_title="Failing tests (per JUnit)",
        row_tuples=rows,
        as_labeled_lines=True,
    )
    assert not any(b.get("type") == "table" for b in line_inner)
    labels = {
        node["text"].rstrip(":")
        for block in line_inner
        if block.get("type") == "paragraph"
        for node in block.get("content", [])
        if node.get("marks") == [{"type": "strong"}]
    }
    assert "Blocked Reason" in labels
    assert labels == {"Tier", "Suite Name", "Test Name", "AI Insights", "Blocked Reason"}


def test_dedupe_failure_rows_by_tier_suite() -> None:
    rows = [
        ("Nightly-Tier1", "upstream-dns", None, "TestA", "d1", "ki1", "br1"),
        ("Nightly-Tier1", "upstream-dns", None, "TestB", "d2", "ki2", "br2"),
        ("Nightly-Tier1", "other-suite", None, "TestC", "d3", "ki3", "br3"),
        ("Nightly-Tier2", "upstream-dns", None, "TestD", "d4", "ki4", "br4"),
    ]
    out = dedupe_failure_rows_by_tier_suite(rows)
    assert len(out) == 3
    assert out[0][3] == "TestA"
    assert [r[0] + "/" + r[1] for r in out] == [
        "Nightly-Tier1/upstream-dns",
        "Nightly-Tier1/other-suite",
        "Nightly-Tier2/upstream-dns",
    ]


def test_failure_doc_inner_table_dedupes_duplicate_suite_names() -> None:
    rows = [
        (
            "Nightly-Tier1",
            "upstream-dns",
            "https://artifacts.example/report.html",
            "TestDNS.test_foo",
            "[failure] boom",
            "[IDM-6832](https://redhat.atlassian.net/browse/IDM-6832)",
            "",
        ),
        (
            "Nightly-Tier1",
            "upstream-dns",
            "https://artifacts.example/report.html",
            "TestDNS.test_bar",
            "[failure] bang",
            "[IDM-6832](https://redhat.atlassian.net/browse/IDM-6832)",
            "",
        ),
    ]
    table_inner = failure_doc_inner_for_rows(
        section_title="Failing tests (per JUnit)",
        row_tuples=rows,
        as_labeled_lines=False,
    )
    table = next(b for b in table_inner if b.get("type") == "table")
    assert len(table["content"]) == 2  # header + one suite
    line_inner = failure_doc_inner_for_rows(
        section_title="Failing tests (per JUnit)",
        row_tuples=rows,
        as_labeled_lines=True,
    )
    test_name_labels = [
        node["text"]
        for block in line_inner
        if block.get("type") == "paragraph"
        for node in block.get("content", [])
        if node.get("type") == "text"
        and not node.get("marks")
        and node["text"] in ("TestDNS.test_foo", "TestDNS.test_bar")
    ]
    assert len(test_name_labels) == 2


def test_adf_failure_detail_table_omits_failure_details_column() -> None:
    doc = adf_failure_detail_table(
        section_title="Failing tests (per JUnit)",
        row_tuples=[
            (
                "Nightly-Tier1",
                "upstream-dns",
                "https://artifacts.example/tier-1/upstream-dns/1/report.html",
                "TestDNS.test_foo",
                "[failure] assertion failed",
                "—",
                "",
            )
        ],
    )
    table = next(b for b in doc["content"] if b.get("type") == "table")
    header_cells = table["content"][0]["content"]
    headers = [
        c["content"][0]["content"][0]["text"]
        for c in header_cells
    ]
    assert headers == ["Tier", "Suite Name", "AI Insights", "Blocked Reason"]
    data_row = table["content"][1]["content"]
    assert len(data_row) == 4


def test_format_pass_fail_skip_cell() -> None:
    assert format_pass_fail_skip_cell(10, 1, 0, 11) == "10 (90.9%) / 1 (9.1%) / 0 (0.0%) (11)"
    assert format_pass_fail_skip_cell(0, 0, 0, 0) == "0 / 0 / 0 (0)"


def test_build_table_rows_from_parse_results() -> None:
    note = """Discovered 1 job junit URL(s) under https://example.com/Nightly-Tier1/RHEL9.8/2026-01-01_12-00/tier-1/

"""
    pr = ParseResult(tests=11, failures=1, errors=0, skipped=0, failures_detail=[])
    rows = build_table_rows_from_parse_results([("Nightly-Tier1", pr, note)])
    assert len(rows) == 1
    tier, folder, results, href = rows[0]
    assert tier == "Nightly-Tier1"
    assert folder == "2026-01-01_12-00"
    assert results == "10 (90.9%) / 1 (9.1%) / 0 (0.0%) (11)"
    assert href and "tier-1/" in href


def test_build_table_rows_from_parse_results_multiple_tiers() -> None:
    items: list[tuple[str, ParseResult, str]] = []
    for i, tier in enumerate(("Nightly-Tier1", "Nightly-Tier2", "Nightly-Tier3"), start=1):
        note = (
            f"Discovered 1 job junit URL(s) under "
            f"https://example.com/{tier}/RHEL10.3/2026-06-0{i}_12-00/tier-{i}/\n\n"
        )
        pr = ParseResult(tests=10 + i, failures=i, errors=0, skipped=0, failures_detail=[])
        items.append((tier, pr, note))
    rows = build_table_rows_from_parse_results(items)
    assert [r[0] for r in rows] == [
        "Nightly-Tier1",
        "Nightly-Tier2",
        "Nightly-Tier3",
    ]
    doc = adf_table_comment(intro="intro", rows=rows, footer=None)
    table = next(b for b in doc["content"] if b.get("type") == "table")
    assert len(table["content"]) == 4  # header + 3 tiers


def test_content_limit_fallback_posts_merged_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = "Discovered 1 job junit URL(s) under https://example.com/Nightly-Tier1/RHEL9.8/2026-01-01_12-00/tier-1/\n\n"
    fetches = [
        (
            "Nightly-Tier1",
            ParseResult(tests=2, failures=1, errors=0, skipped=0, failures_detail=[]),
            note,
        ),
        (
            "Nightly-Tier2",
            ParseResult(tests=3, failures=0, errors=0, skipped=0, failures_detail=[]),
            note.replace("Nightly-Tier1", "Nightly-Tier2").replace("tier-1", "tier-2"),
        ),
    ]
    posted: list[dict] = []
    post_calls = 0

    def fake_post(_base, _email, _token, _key, body):
        nonlocal post_calls
        post_calls += 1
        posted.append(body)
        if post_calls == 1:
            raise RuntimeError('HTTP 400: {"errorMessages":["CONTENT_LIMIT_EXCEEDED"]}')
        return {"id": str(post_calls)}

    monkeypatch.setattr(fjc, "post_comment", fake_post)
    monkeypatch.setattr(
        fjc,
        "failure_rows_for_tiers",
        lambda *_a, **_k: [
            (
                "Nightly-Tier1",
                "suite",
                None,
                "TestFoo",
                "detail",
                "",
                "",
            )
        ],
    )

    rc = _post_with_content_limit_fallback(
        base="https://example.atlassian.net",
        email="a@example.com",
        token="t",
        issue_key="IDM-1",
        doc={"type": "doc", "version": 1, "content": []},
        label="table",
        rhel="9.8",
        tiers=["Nightly-Tier1", "Nightly-Tier2"],
        fetches=fetches,
        intro="intro",
        footer=None,
        style="short",
        mode="table",
        include_failure_table=True,
        failure_section_title="Failing tests (per JUnit)",
        failure_as_labeled_lines=False,
        detail_limit=1200,
        max_rows=None,
        include_epic_in_progress=False,
    )
    assert rc == 0
    assert len(posted) == 3  # combined attempt, then summary, then failures
    summary_table = next(b for b in posted[1]["content"] if b.get("type") == "table")
    summary_tiers = [
        row["content"][0]["content"][0]["content"][0]["text"]
        for row in summary_table["content"][1:]
    ]
    assert summary_tiers == ["Nightly-Tier1", "Nightly-Tier2"]


def test_build_table_rows_from_reports() -> None:
    sample = """Discovered 1 job junit URL(s) under https://example.com/Nightly-Tier1/RHEL9.8/2026-01-01_12-00/tier-1/

## Nightly-Tier1 RHEL 9.8 (2026-01-01_12-00)

**Totals:** 10 passed, 1 failed, 0 skipped (of 11).
"""
    rows = build_table_rows_from_reports([("Nightly-Tier1", sample)])
    assert len(rows) == 1
    tier, folder, results, href = rows[0]
    assert tier == "Nightly-Tier1"
    assert folder == "2026-01-01_12-00"
    assert results == "10 (90.9%) / 1 (9.1%) / 0 (0.0%) (11)"
    assert href and href.endswith("/tier-1/")


def test_load_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "e.env"
    p.write_text(
        "export FOO='bar'\n# c\nUNSET_ME=1\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("FOO", raising=False)
    load_env_file(p)
    import os

    assert os.environ["FOO"] == "bar"
    load_env_file(p, override=True)
    assert os.environ["FOO"] == "bar"


def test_is_content_limit_exceeded() -> None:
    assert _is_content_limit_exceeded(
        RuntimeError(
            'HTTP 400: {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{"comment":"CONTENT_LIMIT_EXCEEDED"}}'
        )
    )
    assert not _is_content_limit_exceeded(RuntimeError("HTTP 400: something else"))

"""Unit tests for Jira comment helpers (no HTTP)."""

from __future__ import annotations

from pathlib import Path

import pytest

from report_logs.models import ParseResult
from report_logs import freeipa_jira_comment as fjc
from report_logs.freeipa_jira_comment import (
    build_table_rows_from_parse_results,
    build_table_rows_from_reports,
    extract_totals,
    extract_title_folder,
    load_env_file,
    _is_content_limit_exceeded,
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


def test_build_table_rows_from_parse_results() -> None:
    note = """Discovered 1 job junit URL(s) under https://example.com/Nightly-Tier1/RHEL9.8/2026-01-01_12-00/tier-1/

"""
    pr = ParseResult(tests=11, failures=1, errors=0, skipped=0, failures_detail=[])
    rows = build_table_rows_from_parse_results([("Nightly-Tier1", pr, note)])
    assert len(rows) == 1
    tier, folder, results, href = rows[0]
    assert tier == "Nightly-Tier1"
    assert folder == "2026-01-01_12-00"
    assert results == "10 / 1 / 0 (11)"
    assert href and "tier-1/" in href


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
    assert results == "10 / 1 / 0 (11)"
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

import pytest

from report_logs.models import ParseResult

from report_logs.artifacts import (
    candidate_directory_urls,
    discover_pipeline_junit_xml_urls,
    discover_signoff_pipeline_index_urls,
    fetch_and_merge_junit_urls,
    guess_junit_urls_for_run,
    is_all_tier_signoff,
    junit_xml_relative_path_for_job_dir,
    normalize_rhel_version,
    normalize_tier_slug,
    parse_nginx_numeric_subdirectory_indices,
    rhel_path_variants,
)


def test_is_all_tier_signoff() -> None:
    assert is_all_tier_signoff("All-Tier-Signoff")
    assert is_all_tier_signoff("all-tier-signoff")
    assert is_all_tier_signoff("All Tier Signoff")
    assert not is_all_tier_signoff("Nightly-Tier1")


def test_discover_signoff_pipeline_index_urls_all_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    root = "https://artifacts.example/idm-ci/freeipa/All-Tier-Signoff/RHEL9.9/"
    run_html = '<a href="2026-06-10_12-00/">2026-06-10_12-00/</a>'
    tier_html = '<a href="upstream/">upstream/</a>'

    def fake_fetch(url: str, **kwargs):
        u = url.rstrip("/")
        if u.endswith("All-Tier-Signoff/RHEL9.9"):
            return (200, run_html)
        if u.endswith("2026-06-10_12-00/tier-1") or u.endswith("2026-06-10_12-00/tier-2"):
            return (200, tier_html)
        if u.endswith("2026-06-10_12-00/tier-3"):
            return (404, "")
        raise AssertionError(f"unexpected fetch URL {url!r}")

    monkeypatch.setattr("report_logs.artifacts.fetch_url_optional", fake_fetch)
    entries, diag = discover_signoff_pipeline_index_urls("9.9", "All-Tier-Signoff")
    assert "2026-06-10_12-00" in diag
    assert len(entries) == 2
    assert entries[0][0] == "All-Tier-Signoff (tier-1)"
    assert entries[0][1].endswith("/2026-06-10_12-00/tier-1/")
    assert entries[1][0] == "All-Tier-Signoff (tier-2)"


def test_normalize_tier():
    assert normalize_tier_slug("Nightly-Tier1") == "tier1"
    assert normalize_tier_slug("tier2") == "tier2"
    assert normalize_tier_slug("Nightly-Tier2") == "tier2"


def test_normalize_rhel():
    assert normalize_rhel_version("9.8") == "9.8"
    assert normalize_rhel_version("RHEL 9.8") == "9.8"
    assert normalize_rhel_version("RHEL9.8") == "9.8"


def test_rhel_path_variants_distinct():
    v = rhel_path_variants("9.8")
    assert v[0] == "RHEL9.8"
    assert "rhel-9.8" in v
    assert "9.8" in v or "rhel9.8" in v


def test_candidate_urls_contain_tier_and_rhel():
    urls = candidate_directory_urls("9.8", "tier1")
    assert any("tier1" in u and "9.8" in u for u in urls)
    assert all(u.startswith("https://") for u in urls)


def test_guess_junit_urls():
    u = guess_junit_urls_for_run("10.2", "Nightly-Tier2", None)
    assert len(u) >= 4
    assert any("junit.xml" in x for x in u)


def test_parse_nginx_numeric_subdirectory_indices() -> None:
    html = '<a href="1/">1</a><a href="2/">two</a><a href="10/"></a>'
    assert parse_nginx_numeric_subdirectory_indices(html) == [1, 2, 10]


def test_discover_pipeline_junit_xml_urls_picks_highest_numeric_run(monkeypatch):
    idx = "https://artifacts.example/idm-ci/freeipa/Nightly-Tier1/RHEL9.9/run/tier-1/"
    tier_html = """
    <html><a href="../">..</a>
    <a href="/idm-ci/freeipa/Nightly-Tier1/RHEL9.9/run/tier-1/upstream-edns/">upstream-edns/</a>
    <a href="/idm-ci/freeipa/Nightly-Tier1/RHEL9.9/run/tier-1/upstream-xmlrpc-tests/">upstream-xmlrpc-tests/</a>
    </html>"""

    def fake_fetch(url: str, **kwargs):
        u = url.rstrip("/")
        if u.endswith("tier-1"):
            return (200, tier_html)
        if "upstream-edns" in u:
            return (200, '<a href="1/">1/</a><a href="2/">2/</a>')
        if "upstream-xmlrpc-tests" in u:
            return (200, '<a href="logs/">logs/</a>')
        raise AssertionError(f"unexpected fetch URL {url!r}")

    monkeypatch.setattr("report_logs.artifacts.fetch_url_optional", fake_fetch)
    urls, note = discover_pipeline_junit_xml_urls(idx)
    assert "Discovered 2 job junit URL(s)" in note
    assert len(urls) == 2
    assert urls[0].endswith("/upstream-edns/2/junit.xml")
    assert urls[1].endswith("/upstream-xmlrpc-tests/1/junit.xml")


def test_discover_pipeline_junit_xml_urls_fixed_subpath_skips_run_probe(monkeypatch) -> None:
    idx = "https://artifacts.example/run/tier-1/"
    tier_html = '<a href="upstream/">upstream/</a>'

    calls: list[str] = []

    def fake_fetch(url: str, **kwargs):
        calls.append(url)
        if url.rstrip("/").endswith("tier-1"):
            return (200, tier_html)
        raise AssertionError(f"unexpected {url}")

    monkeypatch.setattr("report_logs.artifacts.fetch_url_optional", fake_fetch)
    urls, _note = discover_pipeline_junit_xml_urls(idx, junit_subpath="1/junit.xml")
    assert len(urls) == 1
    assert urls[0].endswith("/upstream/1/junit.xml")
    assert len(calls) == 1


def test_junit_xml_relative_path_for_job_dir(monkeypatch) -> None:
    def fake_fetch(url: str, **kwargs):
        assert "win2012" in url
        return (200, '<a href="1/">1/</a><a href="2/">2/</a>')

    monkeypatch.setattr("report_logs.artifacts.fetch_url_optional", fake_fetch)
    rel = junit_xml_relative_path_for_job_dir(
        "https://h.example/tier-2/upstream-trust-win2012/",
        timeout=5.0,
    )
    assert rel == "2/junit.xml"


def test_fetch_and_merge_junit_urls(monkeypatch):
    xml_ok = """<?xml version="1.0"?>
<testsuite tests="1" failures="0" errors="0" skipped="0" name="s">
  <testcase classname="X" name="y"/>
</testsuite>"""

    def fake_fetch(url, **kwargs):
        if "bad" in url:
            return None, "404"
        return xml_ok, "ok"

    monkeypatch.setattr("report_logs.artifacts.fetch_junit_from_absolute_url", fake_fetch)
    pr, note = fetch_and_merge_junit_urls(
        ["https://x/good.xml", "https://x/bad.xml"],
    )
    assert pr is not None
    assert pr.tests == 1
    assert "SKIP" in note
    assert "Merged 1" in note


def test_analyze_freeipa_merges_urls(monkeypatch):
    from report_logs import server

    pr = ParseResult(
        tests=2,
        failures=1,
        errors=0,
        skipped=0,
        failures_detail=[],
    )

    monkeypatch.setattr(
        server,
        "fetch_and_merge_junit_urls",
        lambda urls: (pr, "merged note"),
    )

    out = server.analyze_freeipa_ci_artifacts(
        rhel_version="9.9",
        tier="Nightly-Tier1",
        junit_xml_urls=["https://a/junit.xml", "https://b/junit.xml"],
        report_style="brief",
    )
    assert "merged note" in out
    assert "FAIL" in out


def test_analyze_freeipa_requires_pipeline_or_junit_urls():
    from report_logs import server

    out = server.analyze_freeipa_ci_artifacts(
        rhel_version="9.8",
        tier="Nightly-Tier1",
        report_style="brief",
    )
    assert "No JUnit source specified" in out
    assert "pipeline_index_url" in out
    assert "freeipa_candidate_artifact_urls" in out


def test_analyze_freeipa_table_style(monkeypatch):
    from report_logs import server
    from report_logs.models import ParseResult, TestFailure

    pr = ParseResult(
        tests=2,
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
    monkeypatch.setattr(
        server,
        "fetch_and_merge_junit_urls",
        lambda urls: (pr, "merged"),
    )

    out = server.analyze_freeipa_ci_artifacts(
        rhel_version="9.8",
        tier="Nightly-Tier1",
        junit_xml_urls=["https://example/junit.xml"],
        report_style="table",
    )
    assert "| Tier | Suite name | Test name | Failure Details | AI Insights |" in out
    assert "| Nightly-Tier1 |" in out
    assert "merged" in out
    assert "[failure]" in out

    out2 = server.analyze_freeipa_ci_artifacts(
        rhel_version="9.8",
        tier="Nightly-Tier1",
        junit_xml_urls=["https://example/junit.xml"],
        report_style="table",
        failure_table_include_run_label=True,
    )
    assert "| Nightly-Tier1 |" in out2

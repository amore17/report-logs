from pathlib import Path

from report_logs.junit import (
    job_name_from_junit_url,
    merge_parse_results,
    parse_junit_xml,
    report_html_url_from_junit_xml_url,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_junit.xml"


def test_parse_sample_aggregates_counts():
    text = FIXTURE.read_text()
    r = parse_junit_xml(text)
    assert r.tests == 3
    assert r.failures == 3
    assert r.errors == 0
    assert len(r.failures_detail) == 3


def test_merge_parse_results():
    a = parse_junit_xml(
        """<?xml version="1.0"?>
<testsuite tests="2" failures="1" errors="0" skipped="0" name="s1">
  <testcase classname="A" name="a"><failure message="one"/></testcase>
  <testcase classname="A" name="b"/>
</testsuite>"""
    )
    b = parse_junit_xml(
        """<?xml version="1.0"?>
<testsuite tests="1" failures="0" errors="0" skipped="0" name="s2">
  <testcase classname="B" name="c"/>
</testsuite>"""
    )
    m = merge_parse_results([a, b])
    assert m.tests == 3
    assert m.failures == 1
    assert len(m.failures_detail) == 1


def test_job_name_from_junit_url():
    u = "https://artifacts.example/idm-ci/freeipa/Nightly-Tier1/RHEL9.8/run/tier-1/upstream-dns/1/junit.xml"
    assert job_name_from_junit_url(u) == "upstream-dns"
    assert job_name_from_junit_url("https://x/y/z/report.xml") is None


def test_report_html_url_from_junit_xml_url():
    u = "https://artifacts.example/Nightly-Tier1/RHEL9.8/run/tier-1/upstream-dns/1/junit.xml"
    assert (
        report_html_url_from_junit_xml_url(u)
        == "https://artifacts.example/Nightly-Tier1/RHEL9.8/run/tier-1/upstream-dns/1/report.html"
    )
    assert report_html_url_from_junit_xml_url("https://x/y") is None


def test_parse_junit_carries_report_html_from_source_url():
    xml = """<?xml version="1.0"?>
<testsuite tests="1" failures="1" errors="0" skipped="0" name="pytest">
  <testcase classname="a" name="b"><failure message="x"/></testcase>
</testsuite>"""
    ju = "https://artifacts/job-z/1/junit.xml"
    r = parse_junit_xml(xml, job_name="job-z", junit_xml_url=ju)
    assert len(r.failures_detail) == 1
    assert r.failures_detail[0].report_html_url == "https://artifacts/job-z/1/report.html"


def test_parse_uses_job_for_generic_pytest_suite():
    xml = """<?xml version="1.0"?>
<testsuite name="pytest" tests="1" failures="1" errors="0" skipped="0">
  <testcase classname="ipa.tests.test_x" name="test_y"><failure message="boom"/></testcase>
</testsuite>"""
    r = parse_junit_xml(xml, job_name="upstream-dns")
    assert len(r.failures_detail) == 1
    assert r.failures_detail[0].suite_name == "upstream-dns"


def test_parse_job_plus_named_suite():
    xml = """<?xml version="1.0"?>
<testsuite name="ldap" tests="1" failures="1" errors="0" skipped="0">
  <testcase classname="a.b" name="c"><failure message="x"/></testcase>
</testsuite>"""
    r = parse_junit_xml(xml, job_name="job-a")
    assert r.failures_detail[0].suite_name == "job-a — ldap"


def test_passed_property():
    xml = """<?xml version="1.0"?>
<testsuite tests="10" failures="1" errors="0" skipped="2">
  <testcase classname="a" name="b"><failure message="x"/></testcase>
</testsuite>"""
    r = parse_junit_xml(xml)
    assert r.passed == 7

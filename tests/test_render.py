from pathlib import Path

from report_logs.junit import parse_junit_xml
from report_logs.render import render_brief, render_short

FIXTURE = Path(__file__).parent / "fixtures" / "sample_junit.xml"


def test_render_brief_contains_headline_and_clusters():
    r = parse_junit_xml(FIXTURE.read_text())
    out = render_brief(
        r,
        title="FreeIPA nightly (Mon)",
        axis_lines=["Tier1 RHEL9.8: 2"],
    )
    assert "FAIL" in out
    assert "failed of 3 tests" in out
    assert "Tier1 RHEL9.8" in out
    assert "ipatests.test_dns.test_forwarders" in out or "test_forwarders" in out


def test_render_short_markdown():
    r = parse_junit_xml(FIXTURE.read_text())
    out = render_short(
        r,
        header="## FreeIPA nightly",
        artifact_url="https://example.com/artifacts/",
    )
    assert "## FreeIPA nightly" in out
    assert "https://example.com/artifacts/" in out
    assert "### Failures by suite" in out
    assert "ldap" in out.lower()

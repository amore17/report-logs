from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from report_logs.models import ParseResult, TestFailure

# Suite names JUnit/pytest often uses without distinguishing scenarios — collapse under CI job name when present.
_GENERIC_SUITE_NAMES = frozenset({"", "pytest", "tests", "nosetests", "unittest"})


def report_html_url_from_junit_xml_url(junit_url: str) -> str | None:
    """
    Map a fetched JUnit artifact URL to the sibling ``report.html`` in the same folder.

    Example:
      ``…/tier-1/upstream-dns/2/junit.xml`` → ``…/tier-1/upstream-dns/2/report.html``
    """
    raw = (junit_url or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    suf = "junit.xml"
    idx = lower.rfind(suf)
    if idx < 0:
        return None
    return raw[:idx] + "report.html"


def job_name_from_junit_url(url: str) -> str | None:
    """
    Extract CI job folder from URLs shaped like ``.../<job>/<N>/junit.xml`` (run index ``N``).

    Example:
      ``.../tier-1/upstream-dns/2/junit.xml`` → ``upstream-dns``
    """
    parts = urlparse(url.strip()).path.rstrip("/").split("/")
    if not parts or len(parts) < 3:
        return None
    if parts[-1].lower() != "junit.xml":
        return None
    step = parts[-2]
    if step.isdigit():
        return parts[-3] if len(parts) >= 3 else None
    return None


def _effective_suite_label(job_name: str | None, xml_suite_name: str) -> str:
    """Stable grouping label: CI job + XML suite name when both carry signal."""
    j = (job_name or "").strip()
    s = (xml_suite_name or "").strip()
    sl = s.lower()
    if j:
        if not s or sl in _GENERIC_SUITE_NAMES:
            return j
        return f"{j} — {s}"
    if s and sl not in _GENERIC_SUITE_NAMES:
        return s
    if s:
        return s
    return "default"


def merge_parse_results(parts: list[ParseResult]) -> ParseResult:
    """Combine counts and failure rows from multiple JUnit parses (same run, split jobs)."""
    tests = failures = errors = skipped = 0
    failures_detail: list[TestFailure] = []
    for r in parts:
        tests += r.tests
        failures += r.failures
        errors += r.errors
        skipped += r.skipped
        failures_detail.extend(r.failures_detail)
    return ParseResult(
        tests=tests,
        failures=failures,
        errors=errors,
        skipped=skipped,
        failures_detail=failures_detail,
    )


def parse_junit_xml(
    xml_text: str,
    *,
    job_name: str | None = None,
    junit_xml_url: str | None = None,
) -> ParseResult:
    """Parse JUnit XML (one or more ``testsuite`` elements)."""
    root = ET.fromstring(xml_text)
    tests = failures = errors = skipped = 0
    failures_detail: list[TestFailure] = []

    suites = root.findall(".//testsuite")
    if not suites:
        if root.tag == "testsuite":
            suites = [root]

    for suite in suites:
        st = int(suite.attrib.get("tests") or 0)
        sf = int(suite.attrib.get("failures") or 0)
        se = int(suite.attrib.get("errors") or 0)
        sk = int(suite.attrib.get("skipped") or 0)
        tests += st
        failures += sf
        errors += se
        skipped += sk
        sname = suite.attrib.get("name") or ""
        suite_label = _effective_suite_label(job_name, sname)
        report_html = report_html_url_from_junit_xml_url(junit_xml_url or "")

        for case in suite.findall("testcase"):
            classname = case.attrib.get("classname") or ""
            name = case.attrib.get("name") or ""
            for tag, kind in (("failure", "failure"), ("error", "error")):
                node = case.find(tag)
                if node is not None:
                    msg = (node.attrib.get("message") or "").strip()
                    if not msg and node.text:
                        msg = node.text.strip()[:500]
                    lbl = suite_label
                    if lbl == "default" and classname:
                        lbl = classname.rsplit(".", 1)[0] if "." in classname else classname
                    failures_detail.append(
                        TestFailure(
                            suite_name=lbl,
                            classname=classname,
                            name=name,
                            message=msg or f"({kind})",
                            type=kind,
                            report_html_url=report_html,
                        )
                    )

    return ParseResult(
        tests=tests,
        failures=failures,
        errors=errors,
        skipped=skipped,
        failures_detail=failures_detail,
    )

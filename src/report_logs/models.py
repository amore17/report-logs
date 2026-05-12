from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TestFailure:
    """One failed or errored test case."""

    __test__ = False  # not a pytest class; name matches Test* pattern

    suite_name: str
    classname: str
    name: str
    message: str
    type: str  # "failure" | "error"
    #: CI HTML report for this job (``…/1/junit.xml`` → ``…/1/report.html``) when known.
    report_html_url: str | None = None


@dataclass
class ParseResult:
    """Aggregated result from one or more JUnit XML blobs."""

    tests: int
    failures: int
    errors: int
    skipped: int
    failures_detail: list[TestFailure] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return self.tests - self.failures - self.errors - self.skipped

"""Pagure FreeIPA issue matching for known issues."""

from __future__ import annotations

from report_logs.pagure_issues import (
    failure_matching_pagure_issue_id,
    pagure_known_issue_markdown,
)


def test_failure_matching_pagure_issue_id_title_match() -> None:
    rows = [
        (
            9981,
            "test failure for test_ad_user_in_posix_group_fully_qualified\nbody",
        ),
    ]
    assert (
        failure_matching_pagure_issue_id(
            "pytest — suite",
            "ipa.tests.test_posix.TestCase",
            "test_ad_user_in_posix_group_fully_qualified",
            rows,
        )
        == 9981
    )


def test_pagure_known_issue_markdown() -> None:
    md = pagure_known_issue_markdown(9981, repo="freeipa")
    assert "freeipa#9981" in md
    assert "https://pagure.io/freeipa/issue/9981" in md


def test_failure_matching_pagure_test_name_in_issue_body() -> None:
    rows = [
        (
            9980,
            "test failure in test_trust_functional.py::testtrustfunctionaluser::test_su_ad_user",
        ),
    ]
    assert (
        failure_matching_pagure_issue_id(
            "upstream-trust",
            "ipa.test_integration.test_trust_functional.TestTrustFunctionalUser",
            "test_su_ad_user",
            rows,
        )
        == 9980
    )


def test_ipatests_does_not_false_positive_tests_segment() -> None:
    """Class segment ``tests`` must not match as substring of ``ipatests`` in comments."""
    rows = [
        (
            9980,
            "master: * 6da412e ipatests: adapt the expected output message",
        ),
        (
            9877,
            "upstream-edns dnsconfd regression description",
        ),
    ]
    assert (
        failure_matching_pagure_issue_id(
            "upstream-edns",
            "ipa.tests.test_edns.TestDNS",
            "test_dot",
            rows,
        )
        == 9877
    )


def test_upstream_edns_does_not_false_positive_on_upstream_in_url() -> None:
    """Generic ``upstream`` segment must not link unrelated suites to CI URLs (pagure#9980 style)."""
    rows = [
        (
            9980,
            "seen in idm-ci/aggregate/tier-pipeline/report.html",
        ),
        (
            9877,
            "upstream-edns dnsconfd failure on el10",
        ),
    ]
    assert (
        failure_matching_pagure_issue_id(
            "upstream-edns",
            "ipa.test_integration.test_edns.TestDNS",
            "test_dot",
            rows,
        )
        == 9877
    )

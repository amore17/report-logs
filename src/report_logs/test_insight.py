"""AI Insight labels (flaky / regression) from prior CI run history."""

from __future__ import annotations

import os
import re

from report_logs.artifacts import (
    discover_prior_pipeline_index_urls,
    fetch_and_merge_junit_urls,
    discover_pipeline_junit_xml_urls,
    pipeline_merge_max_jobs,
)
from report_logs.models import ParseResult, TestFailure


def is_prep_or_provision_test_name(name: str) -> bool:
    """Infra setup failures (exact JUnit test name, case-insensitive)."""
    n = (name or "").strip().lower()
    return n in ("provision", "prep")


def failure_history_key(f: TestFailure) -> str:
    """Stable key for the same logical test across nightly runs."""
    suite = (f.suite_name or "").strip()
    cn = (f.classname or "").strip()
    name = (f.name or "").strip()
    if cn and name:
        ident = f"{cn}.{name}".strip(".")
    else:
        ident = name or cn or "unknown"
    return f"{suite}\x1e{ident}" if suite else ident


def failed_keys_from_result(result: ParseResult) -> set[str]:
    return {failure_history_key(f) for f in result.failures_detail}


def classify_historical_pattern(prior_failed: list[bool | None]) -> str | None:
    """
    Classify using outcomes from prior runs only (oldest → newest).

    * **REGRESSION** — passed every prior run we could see (no prior failures).
    * **FLEAKY TEST** — mixed pass/fail in prior runs (intermittent).
    * ``None`` — always failed in prior runs, or insufficient data.
    """
    observed = [x for x in prior_failed if x is not None]
    if not observed:
        return None
    fail_count = sum(1 for x in observed if x)
    if fail_count == 0:
        return "REGRESSION"
    if 0 < fail_count < len(observed):
        return "FLEAKY TEST"
    return None


def ai_insight_history_enabled() -> bool:
    return os.environ.get("REPORT_LOGS_AI_INSIGHT_HISTORY", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def ai_insight_prior_run_count() -> int:
    raw = os.environ.get("REPORT_LOGS_AI_INSIGHT_PRIOR_RUNS", "3")
    try:
        n = int(raw)
        return max(0, min(n, 10))
    except ValueError:
        return 5


_RUN_FOLDER_RE = re.compile(r"/(\d{4}-\d{2}-\d{2}_\d{2}-\d{2})/")


def extract_run_folder_from_pipeline_url(pipeline_index_url: str) -> str | None:
    m = _RUN_FOLDER_RE.search(pipeline_index_url or "")
    return m.group(1) if m else None


def fetch_prior_run_failure_keys(
    rhel_version: str,
    tier: str,
    *,
    current_pipeline_index_url: str,
    num_prior: int | None = None,
) -> tuple[dict[str, list[bool | None]], int]:
    """
    For tests that failed in at least one prior run, map key → failure flags (oldest → newest).

    Tests that passed every prior run are omitted; use :func:`prior_outcomes_for_key` for lookups.
    Returns ``(history_map, prior_run_count)``.
    """
    limit = num_prior if num_prior is not None else ai_insight_prior_run_count()
    if limit <= 0:
        return {}, 0

    prior_urls, _disc = discover_prior_pipeline_index_urls(
        rhel_version,
        tier,
        current_pipeline_index_url=current_pipeline_index_url,
        limit=limit,
    )
    if not prior_urls:
        return {}, 0

    cap = pipeline_merge_max_jobs()
    per_run: list[set[str]] = []

    for pipeline_url in prior_urls:
        junit_urls, _note = discover_pipeline_junit_xml_urls(pipeline_url)
        run_failed: set[str] = set()
        if junit_urls:
            truncated = len(junit_urls) > cap
            if truncated:
                junit_urls = junit_urls[:cap]
            merged, _fetch_note = fetch_and_merge_junit_urls(junit_urls)
            if merged is not None:
                run_failed = failed_keys_from_result(merged)
        per_run.append(run_failed)

    all_keys: set[str] = set()
    for s in per_run:
        all_keys |= s

    history: dict[str, list[bool | None]] = {}
    for key in all_keys:
        history[key] = [key in run_failed for run_failed in per_run]

    return history, len(prior_urls)


def prior_outcomes_for_key(
    history: dict[str, list[bool | None]],
    key: str,
    prior_run_count: int,
) -> list[bool | None]:
    """Outcomes for *key* across prior runs; absent keys mean the test passed every prior run."""
    if key in history:
        return history[key]
    if prior_run_count <= 0:
        return []
    return [False] * prior_run_count


def build_ai_insight_cell(
    f: TestFailure,
    *,
    prior_failed: list[bool | None] | None,
    known_issue: str,
) -> str:
    """Compose **AI Insights** cell text: classification tag plus optional Jira links."""
    parts: list[str] = []

    if not is_prep_or_provision_test_name(f.name) and prior_failed is not None:
        tag = classify_historical_pattern(prior_failed)
        if tag:
            parts.append(tag)

    ki = (known_issue or "").strip()
    if ki:
        parts.append(ki)

    if parts:
        return " · ".join(parts)

    from report_logs.render import known_issue_empty_placeholder, known_issue_jira_links_enabled

    if known_issue_jira_links_enabled():
        return known_issue_empty_placeholder()
    return ""

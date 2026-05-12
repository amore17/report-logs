"""Epic In Progress section for post-freeipa-jira-comment."""

from __future__ import annotations

import pytest

from report_logs import freeipa_jira_comment as fjc


def test_build_epic_umbrella_non_closed_jql_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPORT_LOGS_IDM_5601_PARENT_SCOPE_ONLY", raising=False)
    monkeypatch.delenv("REPORT_LOGS_IDM_5601_INCLUDE_EPIC_LINK", raising=False)
    jql = fjc.build_epic_umbrella_non_closed_jql("IDM-5601")
    assert jql is not None
    assert "parent = IDM-5601" in jql
    assert "parentEpic = IDM-5601" in jql
    assert 'status != "Closed"' in jql
    assert "status IS EMPTY" in jql
    assert "ORDER BY key ASC" in jql


def test_build_epic_umbrella_non_closed_jql_empty_epic() -> None:
    assert fjc.build_epic_umbrella_non_closed_jql("") is None


def test_adf_epic_in_progress_section_empty_rows() -> None:
    blocks = fjc.adf_epic_in_progress_section(
        "IDM-5601",
        "https://example.atlassian.net",
        [],
        section_title="Test heading",
    )
    assert len(blocks) == 2
    assert blocks[0]["type"] == "heading"
    assert blocks[1]["type"] == "paragraph"


def test_adf_epic_in_progress_section_bullet_list() -> None:
    blocks = fjc.adf_epic_in_progress_section(
        "IDM-5601",
        "https://example.atlassian.net",
        [("IDM-9999", "some summary", "In Progress")],
    )
    assert blocks[0]["type"] == "heading"
    assert blocks[1]["type"] == "bulletList"
    assert len(blocks[1]["content"]) == 1


def test_merge_epic_in_progress_returns_same_doc_on_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc_in = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": []}]}

    def boom(*_a: object, **_k: object) -> list[tuple[str, str, str]]:
        raise RuntimeError("network")

    monkeypatch.setattr(fjc, "fetch_epic_in_progress_issue_tuples", boom)
    out = fjc.merge_epic_in_progress_into_comment_doc(
        doc_in,
        base="https://x.example",
        email="e",
        token="t",
    )
    assert out == doc_in

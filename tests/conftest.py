"""Pytest configuration: keep unit tests from calling Pagure over the network."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_pagure_network_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pagure unit tests opt in explicitly; known-issue column does not call Pagure."""
    monkeypatch.setenv("REPORT_LOGS_PAGURE_FETCH", "0")

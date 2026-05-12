"""Smoke test: MCP server module imports and tool function runs."""

import os
import sys

import pytest

from report_logs.server import failure_report, main

SAMPLE = """<?xml version="1.0"?>
<testsuite tests="1" failures="1" errors="0" skipped="0">
  <testcase classname="t" name="t1"><failure message="boom">x</failure></testcase>
</testsuite>"""


def test_main_help_exits_zero(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["report-logs-mcp", "--help"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0


def test_main_version_exits_zero(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["report-logs-mcp", "--version"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0


def test_main_unknown_flag_exits_two(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["report-logs-mcp", "--nope"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_skip_blank_lines_stdin_discards_empty_lines():
    from report_logs.server import _SkipBlankLinesStdin

    class Raw:
        def __init__(self) -> None:
            self._lines = ["\n", "\r\n", "  \n", '{"jsonrpc":"2.0"}\n']

        def readline(self, size: int = -1) -> str:
            if not self._lines:
                return ""
            return self._lines.pop(0)

    w = _SkipBlankLinesStdin(Raw())
    assert w.readline() == '{"jsonrpc":"2.0"}\n'


class _FakeStdin:
    """stdin that looks like a shell (not a pipe) without using pytest's real stdin fd."""

    def __init__(self, *, tty: bool = False, fd: int = 3) -> None:
        self._tty = tty
        self._fd = fd

    def isatty(self) -> bool:
        return self._tty

    def fileno(self) -> int:
        return self._fd


def test_failure_report_brief():
    out = failure_report(SAMPLE, report_style="brief", title="T")
    assert "FAIL" in out
    assert "boom" in out or "t.t1" in out


def test_failure_report_short():
    out = failure_report(
        SAMPLE,
        report_style="short",
        title="T",
        artifact_url="https://a/",
    )
    assert "https://a/" in out
    assert "##" in out


def test_main_exits_on_tty_without_override(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["report-logs-mcp"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.delenv("REPORT_LOGS_MCP_ALLOW_TTY", raising=False)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_main_exits_when_not_isatty_but_stdin_is_tty_device(monkeypatch):
    """IDE shells sometimes set isatty() false while stdin is still /dev/ttys*."""
    monkeypatch.setattr(sys, "argv", ["report-logs-mcp"])
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=False, fd=3))
    monkeypatch.setattr(os, "ttyname", lambda fd: "/dev/ttys012")
    monkeypatch.delenv("REPORT_LOGS_MCP_ALLOW_TTY", raising=False)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_main_starts_mcp_when_stdin_is_real_pipe(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["report-logs-mcp"])
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=False, fd=4))

    def not_a_tty(_fd: int) -> str:
        raise OSError(19, "not a tty")

    monkeypatch.setattr(os, "ttyname", not_a_tty)
    monkeypatch.delenv("REPORT_LOGS_MCP_ALLOW_TTY", raising=False)
    called: list[str] = []
    monkeypatch.setattr(
        "report_logs.server.mcp.run",
        lambda **kw: called.append(kw.get("transport", "stdio")),
    )
    main()
    assert called == ["stdio"]

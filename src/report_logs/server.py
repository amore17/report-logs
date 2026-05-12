"""MCP stdio server: tools for JUnit text in and brief/short reports out."""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, version as pkg_version

from mcp.server.fastmcp import FastMCP

from report_logs.artifacts import (
    candidate_directory_urls,
    discover_pipeline_junit_xml_urls,
    fetch_and_merge_junit_urls,
    pipeline_merge_max_jobs,
)

_MISSING_JUNIT_SOURCE = """\
No JUnit source specified. Provide one of:

- **pipeline_index_url** — HTTPS directory whose path ends with ``tier-1/``, ``tier-2/``, or ``tier-3/`` (nginx index listing job subfolders). For each job, the latest ``N/junit.xml`` is used when numeric run dirs ``1/``, ``2/``, … exist (**highest N**); otherwise ``1/junit.xml``. Typical layout: ``…/Nightly-Tier1/RHEL9.8/<date>_HH-MM/tier-N/``.

- **junit_xml_url** and/or **junit_xml_urls** — absolute JUnit XML URLs (merged).

Default RHEL/tier directory guessing is disabled (it no longer matches the published tree). Use **freeipa_candidate_artifact_urls** to browse candidate folders, open the dated run, then pass that run's ``tier-N/`` URL as **pipeline_index_url**. The **post-freeipa-jira-comment** CLI discovers the latest dated ``tier-N/`` index from RHEL + tier labels (see README).
"""
from report_logs.junit import parse_junit_xml
from report_logs.models import ParseResult
from report_logs.render import render_brief, render_failure_table, render_short

mcp = FastMCP("report-logs")


def fetch_freeipa_ci_parse_result(
    rhel_version: str,
    tier: str,
    junit_relative_path: str | None = None,
    junit_xml_url: str | None = None,
    junit_xml_urls: list[str] | None = None,
    pipeline_index_url: str | None = None,
) -> tuple[ParseResult | None, str]:
    """
    Download and merge JUnit for a FreeIPA CI tier (same resolution as
    :func:`analyze_freeipa_ci_artifacts`) and return a :class:`ParseResult` plus
    artifact/diagnostic prefix text. On failure, returns ``(None, error_message)``.

    Requires **pipeline_index_url** and/or **junit_xml_url(s)**. There is no fallback
    that guesses paths from ``rhel_version`` + ``tier`` alone.
    """
    result = None
    artifact_note = ""

    if pipeline_index_url and pipeline_index_url.strip():
        urls, disc = discover_pipeline_junit_xml_urls(pipeline_index_url.strip())
        if not urls:
            return None, disc
        cap = pipeline_merge_max_jobs()
        truncated = len(urls) > cap
        if truncated:
            urls = urls[:cap]
        merged, fetch_note = fetch_and_merge_junit_urls(urls)
        artifact_note = disc + "\n\n"
        if truncated:
            artifact_note += (
                f"(Limited to first {cap} jobs; raise REPORT_LOGS_PIPELINE_MAX_JOBS to include more.)\n\n"
            )
        if merged is None:
            return None, artifact_note + fetch_note
        result = merged
        artifact_note += fetch_note + "\n\n"
    else:
        merged_list: list[str] = []
        if junit_xml_url and junit_xml_url.strip():
            merged_list.append(junit_xml_url.strip())
        if junit_xml_urls is not None:
            merged_list.extend(x.strip() for x in junit_xml_urls if x and str(x).strip())
        merged_list = _dedupe_urls(merged_list)
        if merged_list:
            merged, fetch_note = fetch_and_merge_junit_urls(merged_list)
            if merged is None:
                return None, fetch_note
            result = merged
            artifact_note = fetch_note + "\n\n"
        else:
            return None, _MISSING_JUNIT_SOURCE

    assert result is not None
    return result, artifact_note


def _dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        u = u.strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


@mcp.tool()
def freeipa_candidate_artifact_urls(rhel_version: str, tier: str) -> str:
    """
    List candidate **directory** URLs under the FreeIPA artifact server for a
    **RHEL** stream (e.g. ``9.8``, ``10.2``) and **tier** (e.g. ``Nightly-Tier1``
    or ``tier2``). Use when paths need manual browsing or to verify layout.

    Base URL: ``https://idm-artifacts.psi.redhat.com/idm-ci/freeipa/`` unless
    overridden by env ``FREEIPA_ARTIFACTS_BASE_URL``.
    """
    lines = [
        "Candidate artifact directory URLs (open in browser if needed):",
        "",
    ]
    for u in candidate_directory_urls(rhel_version, tier):
        lines.append(f"- {u}")
    lines.extend(
        [
            "",
            "Override base with env FREEIPA_ARTIFACTS_BASE_URL.",
        ]
    )
    return "\n".join(lines)


@mcp.tool()
def analyze_freeipa_ci_artifacts(
    rhel_version: str,
    tier: str,
    report_style: str = "brief",
    junit_relative_path: str | None = None,
    junit_xml_url: str | None = None,
    junit_xml_urls: list[str] | None = None,
    pipeline_index_url: str | None = None,
    title: str | None = None,
    failure_table_include_run_label: bool = False,
) -> str:
    """
    Fetch JUnit XML from the **idm-artifacts** FreeIPA tree and return a
    **brief**, **short**, or **table** failure report.

    - **rhel_version**: e.g. ``9.8`` or ``10.2``
    - **tier**: e.g. ``Nightly-Tier1``, ``Nightly-Tier2``
    - **junit_xml_url**: one absolute JUnit URL (optional)
    - **junit_xml_urls**: merge several absolute JUnit URLs into one summary
      (optional; combined with **junit_xml_url** if both are set, deduped)
    - **pipeline_index_url**: directory URL ending in ``tier-1/``, ``tier-2/``, or ``tier-3/`` —
      list child jobs and merge each job's latest ``N/junit.xml`` (max ``N`` under ``…/<job>/``). Required unless you pass **junit_xml_url(s)**.
      Use when the tree uses dated run folders (``…/RHEL9.8/YYYY-MM-DD/tier-N/``).
      Cap: env ``REPORT_LOGS_PIPELINE_MAX_JOBS`` (default 200; ``REPORT_LOGS_TIER1_MAX_JOBS`` is still read as a legacy alias).
    - **junit_relative_path**: reserved (ignored); default path guessing is disabled.
    - **report_style**: ``brief``, ``short``, or ``table`` (markdown table: Tier, Suite name,
      Test name, Failure Details, AI Suggested Known Issue).
    - **failure_table_include_run_label**: when ``report_style`` is ``table``, use **title**
      as a fallback for the **Tier** column only if **tier** would otherwise be empty (see ``failure_report``).
    """
    run_title = title or f"FreeIPA {tier} RHEL {rhel_version}"
    style = (report_style or "brief").lower().strip()
    base = "https://idm-artifacts.psi.redhat.com/idm-ci/freeipa/"
    result, artifact_note = fetch_freeipa_ci_parse_result(
        rhel_version,
        tier,
        junit_relative_path=junit_relative_path,
        junit_xml_url=junit_xml_url,
        junit_xml_urls=junit_xml_urls,
        pipeline_index_url=pipeline_index_url,
    )
    if result is None:
        return artifact_note

    assert result is not None
    if style == "short":
        return artifact_note + render_short(
            result,
            header=f"## {run_title}",
            artifact_url=base.rstrip("/"),
        )
    if style == "table":
        rl = run_title if failure_table_include_run_label else None
        return artifact_note + render_failure_table(
            result,
            header=f"## {run_title}",
            artifact_url=base.rstrip("/"),
            tier=tier,
            run_label=rl,
        )
    axis = [
        f"{tier} RHEL{rhel_version}: {result.failures + result.errors} failed",
    ]
    return artifact_note + render_brief(result, title=run_title, axis_lines=axis)


@mcp.tool()
def failure_report(
    junit_xml: str,
    report_style: str = "brief",
    title: str = "Nightly summary",
    artifact_url: str | None = None,
    failure_table_include_run_label: bool = False,
    tier: str | None = None,
) -> str:
    """
    Parse JUnit XML and return a **brief**, **short**, or **table** text report.

    - **brief**: headline, optional axis line, top failure clusters.
    - **short**: markdown with optional artifact link and grouped failures.
    - **table**: markdown table (Tier, Suite name, Test name, Failure Details, AI Suggested Known Issue).
    - **tier**: optional pipeline tier label for the **Tier** column (e.g. ``Nightly-Tier1``).
    - **failure_table_include_run_label**: with **table**, if **tier** is omitted, use **title** as the Tier column.
    """
    result = parse_junit_xml(junit_xml)
    style = (report_style or "brief").lower().strip()
    if style == "short":
        return render_short(
            result,
            header=f"## {title}",
            artifact_url=artifact_url,
        )
    if style == "table":
        rl = title if failure_table_include_run_label else None
        return render_failure_table(
            result,
            header=f"## {title}",
            artifact_url=artifact_url,
            tier=tier,
            run_label=rl,
        )
    return render_brief(result, title=title)


def _stdio_server_help() -> None:
    print(
        "report-logs-mcp is an MCP server: it speaks JSON-RPC on stdin/stdout.\n"
        "Do not run it directly in a terminal — pressing Enter sends '\\n', which\n"
        "is not valid JSON-RPC (you will see Pydantic JSON parse errors).\n"
        "\n"
        "Configure Cursor: Settings → MCP → add this command, or use this repo's\n"
        ".cursor/mcp.json with the report-logs folder as a workspace root.\n"
        "\n"
        "To force-start anyway (debug only): REPORT_LOGS_MCP_ALLOW_TTY=1",
        file=sys.stderr,
    )


class _SkipBlankLinesStdin:
    """
    MCP stdio is JSON-per-line; empty lines (e.g. user pressed Enter in a TTY)
    are not valid JSON. When REPORT_LOGS_MCP_ALLOW_TTY=1, wrap stdin so blank
    lines are discarded before parsing.
    """

    __slots__ = ("_raw",)

    def __init__(self, raw: object) -> None:
        self._raw = raw

    def readline(self, size: int = -1) -> str:
        while True:
            line = self._raw.readline(size)
            if not line:
                return line
            if line.strip():
                return line

    def __iter__(self):
        return self

    def __next__(self) -> str:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def __getattr__(self, name: str):
        return getattr(self._raw, name)


def _package_version() -> str:
    try:
        return pkg_version("report-logs")
    except PackageNotFoundError:
        return "0.0.0"


def _print_version() -> None:
    print(f"report-logs-mcp {_package_version()}")


def _print_usage() -> None:
    print(
        """usage: report-logs-mcp [-h] [--version]

Model Context Protocol server (stdio JSON-RPC) for FreeIPA CI log analysis.

optional arguments:
  -h, --help     show this help and exit
  --version      print version and exit

Run with no arguments to start the MCP server on stdin/stdout (normally started
by Cursor or another MCP host). Do not use an interactive shell unless you set
REPORT_LOGS_MCP_ALLOW_TTY=1 for debugging.

Environment:
  FREEIPA_ARTIFACTS_BASE_URL   artifact tree base (default: idm-ci/freeipa)
  REPORT_LOGS_MCP_ALLOW_TTY    set to 1 to allow starting from a TTY (debug)

analyze_freeipa_ci_artifacts requires pipeline_index_url or junit_xml_url(s); no RHEL/tier path guess.
"""
    )


def _handle_cli_args() -> None:
    """If argv contains --help or --version, print and exit; reject unknown flags."""
    argv = sys.argv[1:]
    if not argv:
        return
    if any(a in ("-h", "--help") for a in argv):
        _print_usage()
        sys.exit(0)
    if "--version" in argv:
        _print_version()
        sys.exit(0)
    for a in argv:
        if a.startswith("-"):
            print(f"report-logs-mcp: unknown option {a}", file=sys.stderr)
            sys.exit(2)


def _stdin_is_terminal_session() -> bool:
    """
    True when stdin is an interactive terminal (not a pipe from an MCP client).

    Some IDE terminals report isatty() == False even for a real shell; os.ttyname()
    still identifies a controlling tty device in those cases.
    """
    if sys.stdin.isatty():
        return True
    try:
        os.ttyname(sys.stdin.fileno())
    except OSError:
        return False
    return True


def main() -> None:
    _handle_cli_args()
    # stdio transport expects JSON-RPC from a client pipe — not the user's keyboard.
    if (
        os.environ.get("REPORT_LOGS_MCP_ALLOW_TTY") != "1"
        and _stdin_is_terminal_session()
    ):
        _stdio_server_help()
        sys.exit(2)
    if os.environ.get("REPORT_LOGS_MCP_ALLOW_TTY") == "1":
        sys.stdin = _SkipBlankLinesStdin(sys.stdin)
        print(
            "REPORT_LOGS_MCP_ALLOW_TTY: ignoring blank stdin lines "
            "(Enter alone no longer triggers JSON parse errors). "
            "You still need real JSON-RPC from an MCP client or a pipe.",
            file=sys.stderr,
        )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

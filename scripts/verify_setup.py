#!/usr/bin/env python3
"""
Smoke-check imports and MCP app without starting stdio (avoids hanging shells).

Usage (from repo root, venv active):

    python scripts/verify_setup.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    from report_logs.server import mcp

    name = getattr(mcp, "name", None) or "report-logs"
    print(f"OK — FastMCP server object loaded ({name!r}).")
    print("Next: run `pytest` and configure Cursor MCP (see README).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

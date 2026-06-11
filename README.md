# report-logs

MCP server and helpers for FreeIPA nightly CI failure reports (**brief** / **short** / **table**).

## Install

```bash
cd /Users/amore/amore-mcp-server/report-logs
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Test this project

```bash
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pytest -q
python scripts/verify_setup.py
```

You should see **all tests passed** and **`OK — FastMCP server object loaded`**.

## Create / enable the MCP server in Cursor

The server is implemented in **`src/report_logs/server.py`** and installed as the **`report-logs-mcp`** console script.

1. Finish **Install** above so `.venv/bin/report-logs-mcp` exists.
2. **Project MCP:** this repo already has **`.cursor/mcp.json`** pointing at `${workspaceFolder}/.venv/bin/report-logs-mcp`.
3. In Cursor, open **`report-logs` as a workspace folder** (add folder or open this repo alone) so `${workspaceFolder}` resolves.
4. Reload MCP: **Cursor Settings → Features → Model Context Protocol** — refresh or restart Cursor.
5. In Chat / Agent, confirm **report-logs** appears under MCP tools (e.g. `failure_report`, `analyze_freeipa_ci_artifacts`).

**If `report-logs` is not a workspace root**, add an entry to **`~/.cursor/mcp.json`** manually (same `command` / `cwd`, but use absolute paths to your `.venv/bin/report-logs-mcp` and repo directory).

**Do not** run `report-logs-mcp` alone in a terminal for normal use — it waits for JSON-RPC on stdin. Cursor starts it for you.

With **`REPORT_LOGS_MCP_ALLOW_TTY=1`**, the server skips **blank lines** on stdin so pressing Enter alone does not trigger JSON parse errors; you still must send real MCP JSON-RPC (from Cursor or a pipe), not interactive shell input.

Quick checks without starting the server (no hang):

```bash
report-logs-mcp --help
report-logs-mcp --version
```

## FreeIPA artifact server

Default base: **https://idm-artifacts.psi.redhat.com/idm-ci/freeipa/**  
Override with env **`FREEIPA_ARTIFACTS_BASE_URL`** if needed.

MCP tool **`analyze_freeipa_ci_artifacts`** needs a real JUnit source: pass **`pipeline_index_url`** (directory URL ending in `tier-1/`, `tier-2/`, or `tier-3/` on the artifact host, e.g. `…/RHEL9.8/2026-04-27_16-00/tier-1/`) so each job’s latest **`N/junit.xml`** is discovered (**highest numeric** run folder `N` under `…/<job>/`, e.g. `1/` and `2/` → use `2/junit.xml`; if none, fall back to `1/junit.xml`) and merged, **or** one or more absolute **`junit_xml_url` / `junit_xml_urls`**. RHEL/tier-only “default path” guessing is **not** used. Use **`freeipa_candidate_artifact_urls`** to list folder URLs to browse, then open the dated run and use that run’s pipeline index URL. The Jira helper (**`post-freeipa-jira-comment`**) instead takes **`--jira-issue-key`**, **RHEL** + **tier name(s)** on the command line and **discovers** the latest dated pipeline index on the artifact server; see **Post FreeIPA report to Jira** below.

The **AI Insights** column is always present in table-style reports. **Jira links** (IDM-5601 resolution) are **off** by default; set **`REPORT_LOGS_KNOWN_ISSUE_LINKS=1`** to fill the column. When off, cells are **empty** (no em dash, no Jira API use). When **on**, the cell lists **all non-Closed** umbrella **children** of **[IDM-5601](https://redhat.atlassian.net/browse/IDM-5601)** whose **summary** or **description** matches the failing **suite name** (same **`parent`** / **`parentEpic`** scope and optional **`Epic Link`** as elsewhere). Matches are shown as **multiple** markdown links separated by **` · `**, capped by **`REPORT_LOGS_KNOWN_ISSUE_LIST_MAX`** (default **25**). If that JQL search finds nothing, behavior falls back to loading children and matching each issue’s **summary** only, using the same normalized suite needles as JQL (no haystack / class / test token matching—short segments such as **`win2025`** must not link unrelated trackers). There are **no built-in suite→IDM mappings** in code—relationships come from how subtasks are titled. **`REPORT_LOGS_IDM_5601_FETCH_COMMENTS`** and **`REPORT_LOGS_IDM_5601_SUITE_ISSUE_PINS`** affect other umbrella helpers in `jira_child_issues`, not this column’s summary fallback. **`REPORT_LOGS_IDM_5601_PARENT_SCOPE_ONLY=1`** restricts discovery + JQL to **`parent =`** only (legacy behavior). **[Pagure](https://pagure.io/freeipa/issues)** is **not** consulted for this column.

**Jira** — **`JIRA_URL`**, **`JIRA_EMAIL`**, **`JIRA_TOKEN`**. The first pass is JQL in the same shape you would use in the issue navigator (KEY = **`REPORT_LOGS_IDM_5601_PARENT_KEY`**, default **IDM-5601**; replace `YOUR_SUITE` with a suite needle from the failure, e.g. from JUnit **Suite name**):

```text
(parent = IDM-5601 OR parentEpic = IDM-5601)
AND (status != "Closed" OR status IS EMPTY)
AND (summary ~ "YOUR_SUITE" OR description ~ "YOUR_SUITE")
```

The **AI Insights** column’s primary query adds **`ORDER BY key ASC`** and the list cap described above. Other umbrella JQL helpers (single “best” child key) still use **`REPORT_LOGS_IDM_5601_JQL_SUFFIX`** for status / **`ORDER BY`** when set.

By default **NAME** is the failing suite string only (one `summary ~` / `description ~` pair). Set **`REPORT_LOGS_IDM_5601_JQL_MULTI_NEEDLE=1`** to OR extra normalized variants (hyphen vs spaces). The single-key umbrella helper appends a default **status** + **ORDER BY** suffix unless **`REPORT_LOGS_IDM_5601_JQL_SUFFIX`** is set. Jira **`text ~`** is not used. **`REPORT_LOGS_IDM_5601_JQL_SUMMARY=0`** skips **all** umbrella JQL (including the AI-suggested multi-link query); the **AI Insights** column then uses only the in-memory **summary** needle fallback above (not haystack token matching).

When linking is **on** and there is still no child match, the cell shows **`REPORT_LOGS_KNOWN_ISSUE_EMPTY`** (default **`—`**, not the word “error”, which was easy to confuse with JUnit `[error]` failures).

There is **no** generic link without a hit. **`REPORT_LOGS_IDM_5601_JIRA_FETCH=0`** or **`REPORT_LOGS_IDM_5601_DISABLE=1`** skips known-issue matching entirely.

## Run MCP (stdio)

This binary **expects JSON-RPC on stdin** (an MCP client). If you run it in a normal terminal, it will exit with a short message—**do not** type into it; that is what caused `Invalid JSON: EOF` / parse errors.

**Cursor:** this repo includes **`.cursor/mcp.json`**. Add the `report-logs` folder as a **workspace root** (or open it alone), create `.venv` + `pip install -e .`, then reload MCP (**Cursor Settings → MCP → refresh**, or restart Cursor). `${workspaceFolder}` points at the folder that contains `.cursor/mcp.json`.

If you use **multi-root** workspaces, ensure `amore-mcp-server/report-logs` is one of the roots so that server resolves correctly.

For debugging transport only: `REPORT_LOGS_MCP_ALLOW_TTY=1 report-logs-mcp` (still needs a client speaking MCP over stdio).

## Post FreeIPA report to Jira (CLI)

Console script: **`post-freeipa-jira-comment`** (from `report_logs.freeipa_jira_comment`).

**Required environment**

| Variable | Description |
|----------|-------------|
| `JIRA_URL` | e.g. `https://redhat.atlassian.net` |
| `JIRA_EMAIL` | Atlassian account email |
| `JIRA_TOKEN` or `JIRA_API_TOKEN` | API token |
| `REPORT_LOGS_JIRA_BYPASS_PROXY` | Default **`1`**: **`post-freeipa-jira-comment`** (and umbrella / **AI Insights** Jira lookups in `jira_child_issues`) ignore ``HTTP_PROXY`` / ``HTTPS_PROXY`` by default (avoids CONNECT **403** to Atlassian). Set **`0`** to use the system proxy |

**Optional**

If **`REPORT_LOGS_KNOWN_ISSUE_LINKS`** is **unset**, **`post-freeipa-jira-comment`** sets it to **`1`** so the **Failing tests** ADF table resolves **AI Insights** cells; put **`REPORT_LOGS_KNOWN_ISSUE_LINKS=0`** in your env file to leave them blank. The MCP server and other tools still default **`REPORT_LOGS_KNOWN_ISSUE_LINKS`** off when unset.

| Variable | Default | Description |
|----------|---------|-------------|
| `FREEIPA_RHEL_VERSION` | `9.8` | RHEL stream if you do not pass **`--rhel`** or a leading **`for RHEL…`** on the CLI |
| `FREEIPA_REPORT_STYLE` | `short` | `brief`, `short`, or `table` |
| `FREEIPA_JIRA_POST_MODE` | `table` | `table` (summary table only), `full` (short report only), or `both` (table + full text) |
| `FREEIPA_JIRA_INTRO` | (built-in) | Opening line above the table |
| `FREEIPA_JIRA_TABLE_FOOTER` | (none) | Extra paragraph under the table |
| `FREEIPA_JIRA_EPIC_IN_PROGRESS_SECTION` | `0` (off) | Set to **`1`** to append a subsection (default heading **In Progress — <EPIC_KEY>**) listing **all non-Closed** umbrella children of **`REPORT_LOGS_IDM_5601_PARENT_KEY`** (default **IDM-5601**): each bullet is **key**, **status**, and **summary**. Uses **`POST /rest/api/3/search/jql`** |
| `FREEIPA_JIRA_EPIC_IN_PROGRESS_MAX` | `300` | Cap on issues fetched for that subsection |
| `FREEIPA_JIRA_EPIC_IN_PROGRESS_TITLE` | (built-in) | Optional ADF heading (default `In Progress — <EPIC_KEY>`) |
| (automatic) | — | **Suite name** in the “Failing tests” table links to **`…/<job>/<N>/report.html`** (same **`N`** as the fetched **`N/junit.xml`**) for Jira ADF and markdown table output |
| `REPORT_LOGS_IDM_5601_PARENT_SCOPE_ONLY` | `0` (off) | Set to `1` for **`parent =`** only (no parentEpic / Epic Link in search or child loading) |
| `REPORT_LOGS_IDM_5601_INCLUDE_EPIC_LINK` | `0` (off) | Set to `1` to also include **`"Epic Link" = IDM-5601`** in child loading and known-issue JQL (matches older three-way scope) |
| `REPORT_LOGS_IDM_5601_JQL_SUMMARY` | `1` (on) | Set to `0` to skip umbrella JQL `(parent/parentEpic [+ Epic Link]) AND (summary ~ …)` and use only loaded rows |
| `REPORT_LOGS_IDM_5601_JQL_SUFFIX` | (see code) | If **unset**, appends `AND (status != "Closed" OR status IS EMPTY) ORDER BY created DESC`. Set to empty to omit; or set a full custom suffix after the `summary ~` clause |
| `REPORT_LOGS_IDM_5601_JQL_RETRY_WITHOUT_STATUS` | `1` (on) | If the primary JQL POST fails, retry once with `parent` + `summary ~` + `ORDER BY` only (no status clause) |
| `REPORT_LOGS_IDM_5601_JQL_TEXT` | `1` (on) | Add ``description ~ "…"`` for each suite needle (not only ``summary ~``). Set to `0` for **summary-only** JQL |
| `REPORT_LOGS_IDM_5601_JQL_MULTI_NEEDLE` | `0` (off) | Set to `1` to OR **extra** suite needles in umbrella JQL (legacy hyphen/space aliases). Default is a **single** ``NAME`` = failed suite |
| `REPORT_LOGS_IDM_5601_SUITE_ISSUE_PINS` | (none) | Optional JSON map ``{"suite-needle": "IDM-NNNN", …}`` to prefer a key when multiple subtasks match |
| `REPORT_LOGS_IDM_5601_FETCH_COMMENTS` | `1` (on) | Set to `0` to skip loading issue comments for known-issue token matching (faster; fewer hits) |
| `REPORT_LOGS_KNOWN_ISSUE_LINKS` | `0` (off) globally | Set to **`1`** to resolve **AI Insights** cells via Jira (IDM-5601). **`post-freeipa-jira-comment`** defaults **on** when this variable is unset (see note above). |
| `REPORT_LOGS_KNOWN_ISSUE_LIST_MAX` | `25` | Max issue keys in an **AI Insights** cell when **`REPORT_LOGS_KNOWN_ISSUE_LINKS=1`** (capped at **100**) |
| `REPORT_LOGS_KNOWN_ISSUE_EMPTY` | `—` | When **`REPORT_LOGS_KNOWN_ISSUE_LINKS=1`**, text when **no** Jira child matches (historically ``error`` looked like JUnit `[error]`) |

**Command line:** required **`--jira-issue-key KEY`** (e.g. `IDM-5885`) for the issue to comment on; values in **`JIRA_ISSUE_KEY`** / **`JIRA_ISSUE`** from the environment or **`--env-file`** are **not** used for that. Also pass one or more **tier** names (as on idm-artifacts, e.g. `Nightly-Tier1`, `Nightly-Tier2`, `Nightly-Tier3`, or **`All-Tier-Signoff`** / **`All-Tier-FIPS-Signoff`** / **`All-Tier-STIG-Signoff`**). With **multiple** tier labels, the comment uses **one** pass/fail summary table (**FreeIPA CI — merged pipeline JUnit…**) with **one row per tier**, and **one** **Failing tests (per JUnit)** section listing failures from **all** tiers (each row includes a **Tier** column; **one row per tier + suite**, not per failing test). If Jira rejects the combined comment as too large, the tool posts the same merged summary table and merged failure table as **separate** comments (still all tiers in each), truncating failure rows only if needed. Optional **`short`** token anywhere on the command line: same merged summary table; failures are labeled lines (**Tier**, **Suite Name**, **Test Name**, **AI Insights**, **Blocked Reason** when present) with **one block per failing test**. RHEL: optional leading **`for RHEL9.8`** / **`for 9.8`**, and/or **`--rhel 9.8`** (overrides the `for` token). The tool finds the **newest** dated run under `…/<Tier>/RHELx.y/…/tier-N/` and merges JUnit from that pipeline. **All-Tier-*-Signoff** labels expand to **tier-1**, **tier-2**, and **tier-3** under the same dated run (one summary row per tier segment).

```bash
source .venv/bin/activate
post-freeipa-jira-comment for RHEL9.8 short --env-file ~/.config/wtmcp/env.d/jira.env \
  --jira-issue-key IDM-5885 Nightly-Tier1 Nightly-Tier2
```

Default (failures in table):

```bash
post-freeipa-jira-comment for RHEL9.8 --env-file ~/.config/wtmcp/env.d/jira.env \
  --jira-issue-key IDM-5885 Nightly-Tier1 Nightly-Tier2
```

Same with explicit RHEL flag:

```bash
post-freeipa-jira-comment --rhel 9.8 --env-file ~/.config/wtmcp/env.d/jira.env \
  --jira-issue-key IDM-5885 Nightly-Tier1
```

`--env-override` makes keys from the file override the current shell. `--dry-run` prints the Atlassian Document Format JSON without calling Jira.

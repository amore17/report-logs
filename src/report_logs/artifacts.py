"""FreeIPA CI artifact base URL, tier/RHEL path helpers, and HTTP fetch."""

from __future__ import annotations

import gzip
import html as html_lib
import os
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from report_logs.junit import job_name_from_junit_url, merge_parse_results, parse_junit_xml
from report_logs.models import ParseResult

DEFAULT_ARTIFACTS_BASE = "https://idm-artifacts.psi.redhat.com/idm-ci/freeipa/"

# Common JUnit filenames under artifact trees (try in order).
DEFAULT_JUNIT_RELATIVE_PATHS: tuple[str, ...] = (
    "junit.xml",
    "results.xml",
    "report.xml",
    "pytest-report.xml",
    "ipatests.xml",
)

USER_AGENT = "report-logs-mcp/0.1 (+ FreeIPA CI analyzer)"


def artifacts_base_url() -> str:
    return (
        os.environ.get("FREEIPA_ARTIFACTS_BASE_URL", DEFAULT_ARTIFACTS_BASE).rstrip("/")
        + "/"
    )


def normalize_tier_slug(tier: str) -> str:
    """
    Normalize folder-style tier names for URL segments.

    Accepts e.g. ``tier1``, ``Tier1``, ``Nightly-Tier1``, ``nightly-tier2``.
    """
    t = tier.strip().lower().replace(" ", "-")
    if "tier2" in t or t == "t2" or t.endswith("-2"):
        return "tier2"
    if "tier1" in t or t == "t1" or t.endswith("-1"):
        return "tier1"
    return tier.strip().replace(" ", "_")


def normalize_rhel_version(rhel: str) -> str:
    """Keep ``9.8`` / ``10.2`` style; strip ``RHEL`` prefix if present."""
    s = rhel.strip()
    if s.lower().startswith("rhel"):
        s = s[4:].lstrip("-_ \t")
    return s.strip()


def rhel_path_variants(rhel_version: str) -> list[str]:
    """Possible directory names for one RHEL stream on the artifact server."""
    v = normalize_rhel_version(rhel_version)
    dots = v.replace("_", ".")
    no_dot = dots.replace(".", "")
    out: list[str] = []
    # Prefer idm-artifacts layout first (e.g. Nightly-Tier1/RHEL9.8/).
    for candidate in (
        f"RHEL{dots}",
        f"rhel{dots}",
        f"rhel-{dots}",
        f"RHEL-{dots}",
        f"rhel{no_dot}",
        f"RHEL{no_dot}",
        f"rhel_{dots.replace('.', '_')}",
        dots,
    ):
        if candidate not in out:
            out.append(candidate)
    return out


def tier_path_variants(tier: str) -> list[str]:
    """Tier folder names on the artifact server (exact CI name before slug shortcuts)."""
    t = tier.strip()
    seen: list[str] = []
    for candidate in (t, t.replace(" ", "-"), normalize_tier_slug(tier)):
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


def candidate_directory_urls(rhel_version: str, tier: str) -> list[str]:
    """
    Ordered list of directory URLs to try under ``artifacts_base_url()``.

    Layout on the server may vary; we try tier-first and rhel-first folder orders.
    """
    base = artifacts_base_url()
    tier_segments = tier_path_variants(tier)
    rhel_vars = rhel_path_variants(rhel_version)
    seen: set[str] = set()
    urls: list[str] = []

    def add(path: str) -> None:
        u = urljoin(base, path)
        if u not in seen:
            seen.add(u)
            urls.append(u)

    for tier_seg in tier_segments:
        tier_slug = normalize_tier_slug(tier_seg)
        for rh in rhel_vars:
            add(f"{tier_seg}/{rh}/")
            add(f"{rh}/{tier_seg}/")
            add(f"nightly-{tier_slug}/{rh}/")
            add(f"Nightly-{tier_slug.title()}/{rh}/")
            add(f"{tier_slug}-nightly/{rh}/")

    return urls


DATE_RUN_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}$")

SIGNOFF_PIPELINE_SEGMENTS: tuple[str, ...] = ("tier-1", "tier-2", "tier-3")


def is_all_tier_signoff(tier: str) -> bool:
    """True for umbrella signoff folders that publish ``tier-1/``, ``tier-2/``, ``tier-3/`` under one run."""
    t = tier.strip().lower().replace("_", "-").replace(" ", "-")
    while "--" in t:
        t = t.replace("--", "-")
    return t == "all-tier-signoff" or "all-tier-signoff" in t


def discover_signoff_pipeline_index_urls(
    rhel_version: str,
    tier: str,
    *,
    timeout: float = 60.0,
) -> tuple[list[tuple[str, str, str]], str]:
    """
    For **All-Tier-Signoff** (and similar): newest dated run with any ``tier-N/`` index,
    then every ``tier-1/``, ``tier-2/``, ``tier-3/`` present under that run.

    Returns ``([(display_label, pipeline_index_url, note), ...], run_diagnostics)``.
    """
    tier_label = tier.strip()
    bases = candidate_directory_urls(rhel_version, tier)
    notes: list[str] = []
    for root in bases:
        root_u = root.rstrip("/") + "/"
        status, html = fetch_url_optional(root_u, timeout=timeout)
        if status is None:
            notes.append(f"{root_u}: {html}")
            continue
        if status >= 400:
            notes.append(f"{root_u}: HTTP {status}")
            continue
        folders = parse_nginx_date_run_folders(html)
        if not folders:
            notes.append(f"{root_u}: no dated run folders")
            continue
        for folder in sorted(folders, reverse=True):
            run_root = urljoin(root_u, f"{folder}/").rstrip("/") + "/"
            entries: list[tuple[str, str, str]] = []
            seg_notes: list[str] = []
            for seg in SIGNOFF_PIPELINE_SEGMENTS:
                pipeline_u = urljoin(run_root, f"{seg}/")
                st2, body = fetch_url_optional(pipeline_u, timeout=timeout)
                if st2 == 200 and body and "<a href=" in body:
                    ok = pipeline_u.rstrip("/") + "/"
                    label = f"{tier_label} ({seg})"
                    entries.append(
                        (
                            label,
                            ok,
                            f"Discovered pipeline index {ok} (run `{folder}`, `{seg}/`).",
                        )
                    )
                elif st2 is None:
                    seg_notes.append(f"{pipeline_u}: {body}")
                else:
                    seg_notes.append(f"{pipeline_u}: unusable (HTTP {st2})")
            if entries:
                run_diag = f"Discovered run folder `{folder}` under {root_u}."
                return entries, run_diag
            notes.extend(seg_notes)
    tail = "\n".join(notes[-25:]) if notes else "(no candidate bases)"
    return [], f"Could not discover All-Tier-Signoff pipelines for tier {tier!r}:\n{tail}"


def pipeline_tier_segment(tier: str) -> str:
    """Return URL path segment ``tier-1``, ``tier-2``, or ``tier-3`` for a CI tier label."""
    t = tier.strip().lower()
    if "tier3" in t or t in ("t3", "tier3"):
        return "tier-3"
    if "tier2" in t or t in ("t2", "tier2"):
        return "tier-2"
    return "tier-1"


def parse_nginx_date_run_folders(html: str) -> list[str]:
    """Extract dated run folder names (``YYYY-MM-DD_HH-MM``) from an nginx directory index."""
    found: set[str] = set()
    for m in re.finditer(r'<a\s+href="([^"]+)"', html, re.IGNORECASE):
        href = html_lib.unescape(m.group(1)).strip()
        if not href or href in ("../", ".."):
            continue
        if "?" in href:
            continue
        name = href.rstrip("/").split("/")[-1]
        if DATE_RUN_FOLDER_RE.match(name):
            found.add(name)
    return sorted(found)


def discover_latest_pipeline_index_url(
    rhel_version: str,
    tier: str,
    *,
    timeout: float = 60.0,
) -> tuple[str | None, str]:
    """
    Under ``…/<Tier>/<RHEL>/``, pick the **lexicographically newest** dated run folder,
    then use ``<run>/<tier-N>/`` (``tier-1``, ``tier-2``, or ``tier-3`` from the tier label)
    as the pipeline index for :func:`discover_pipeline_junit_xml_urls`.
    """
    seg = pipeline_tier_segment(tier)
    bases = candidate_directory_urls(rhel_version, tier)
    notes: list[str] = []
    for root in bases:
        root_u = root.rstrip("/") + "/"
        status, html = fetch_url_optional(root_u, timeout=timeout)
        if status is None:
            notes.append(f"{root_u}: {html}")
            continue
        if status >= 400:
            notes.append(f"{root_u}: HTTP {status}")
            continue
        folders = parse_nginx_date_run_folders(html)
        if not folders:
            notes.append(f"{root_u}: no dated run folders")
            continue
        for folder in sorted(folders, reverse=True):
            pipeline_u = urljoin(root_u, f"{folder}/{seg}/")
            st2, body = fetch_url_optional(pipeline_u, timeout=timeout)
            if st2 == 200 and body and "<a href=" in body:
                ok = pipeline_u.rstrip("/") + "/"
                return ok, f"Discovered pipeline index {ok} (run `{folder}`, `{seg}/`)."
            if st2 is None:
                notes.append(f"{pipeline_u}: {body}")
            else:
                notes.append(f"{pipeline_u}: unusable (HTTP {st2})")
    tail = "\n".join(notes[-25:]) if notes else "(no candidate bases)"
    return None, f"Could not discover pipeline index for tier {tier!r}:\n{tail}"


def fetch_url_text(url: str, *, timeout: float = 60.0) -> tuple[int, str, str]:
    """
    GET URL; return (status_code, body_text, content_type).

    Raises HTTPError, URLError on failure.
    """
    req = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        method="GET",
    )
    ctx = ssl.create_default_context()
    with urlopen(req, timeout=timeout, context=ctx) as resp:
        status = getattr(resp, "status", 200)
        raw = resp.read()
        ctype = resp.headers.get_content_type() or ""
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip" or (len(raw) >= 2 and raw[:2] == b"\x1f\x8b"):
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        text = raw.decode("utf-8", errors="replace")
        return status, text, ctype


def fetch_url_optional(url: str, *, timeout: float = 60.0) -> tuple[int | None, str]:
    """GET URL; on failure return (None, error_message)."""
    try:
        status, text, _ = fetch_url_text(url, timeout=timeout)
        return status, text
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except OSError:
            pass
        return None, f"HTTP {e.code} for {url}\n{body}"
    except URLError as e:
        return None, f"URL error for {url}: {e!s}"
    except OSError as e:
        return None, f"IO error for {url}: {e}"


def guess_junit_urls_for_run(
    rhel_version: str,
    tier: str,
    junit_relative_path: str | None,
) -> list[str]:
    """Full URLs to try for JUnit XML for a given RHEL + tier."""
    paths: list[str] = []
    if junit_relative_path and junit_relative_path.strip():
        paths.append(junit_relative_path.strip().lstrip("/"))
    else:
        paths.extend(DEFAULT_JUNIT_RELATIVE_PATHS)

    urls: list[str] = []
    seen: set[str] = set()
    for root in candidate_directory_urls(rhel_version, tier):
        for rel in paths:
            full = urljoin(root, rel)
            if full not in seen:
                seen.add(full)
                urls.append(full)
    return urls


def fetch_first_junit_xml(
    rhel_version: str,
    tier: str,
    junit_relative_path: str | None = None,
    *,
    timeout: float = 60.0,
) -> tuple[str | None, str]:
    """
    Try candidate URLs until one returns parseable XML body.

    Returns (xml_text, diagnostic): on success diagnostic lists URL used;
    on failure diagnostic explains attempts.
    """
    errors: list[str] = []
    for url in guess_junit_urls_for_run(rhel_version, tier, junit_relative_path):
        status, payload = fetch_url_optional(url, timeout=timeout)
        if status is None:
            errors.append(payload)
            continue
        if status >= 400:
            errors.append(f"HTTP {status} {url}")
            continue
        text = payload.strip()
        if text.startswith("<?xml") or "<testsuite" in text[:2000] or "<testsuites" in text[
            :2000
        ]:
            return text, f"Fetched JUnit XML from {url}"
        errors.append(f"Not JUnit/XML at {url} (first bytes non-xml)")
    return None, "Failed to fetch JUnit XML:\n" + "\n".join(errors[-15:])


def parse_nginx_numeric_subdirectory_indices(html: str) -> list[int]:
    """
    Indices ``N`` from nginx-style ``<a href="N/">`` links (pure digits), for job run folders
    (``…/<job>/1/``, ``…/<job>/2/``, …).
    """
    found: set[int] = set()
    for m in re.finditer(r'<a\s+href="([^"]+)"', html, re.IGNORECASE):
        href = html_lib.unescape(m.group(1)).strip()
        if not href or href in ("../", ".."):
            continue
        if "?" in href or ".." in href:
            continue
        name = href.rstrip("/").split("/")[-1]
        if name.isdigit():
            found.add(int(name))
    return sorted(found)


def junit_xml_relative_path_for_job_dir(job_dir_url: str, *, timeout: float = 60.0) -> str:
    """
    Under ``…/<job>/``, CI often publishes ``1/junit.xml``, ``2/junit.xml``, … where the **largest**
    number is the latest run. Return ``{N}/junit.xml`` for the greatest ``N`` found in the directory
    listing, else ``1/junit.xml`` if listing fails or no numeric subdirs exist.
    """
    base = job_dir_url.strip().rstrip("/") + "/"
    status, html = fetch_url_optional(base, timeout=timeout)
    if status is None or status >= 400 or not html:
        return "1/junit.xml"
    nums = parse_nginx_numeric_subdirectory_indices(html)
    if not nums:
        return "1/junit.xml"
    return f"{max(nums)}/junit.xml"


def fetch_junit_from_absolute_url(url: str, *, timeout: float = 60.0) -> tuple[str | None, str]:
    """GET a single URL; return body if it looks like JUnit XML."""
    status, payload = fetch_url_optional(url.rstrip(), timeout=timeout)
    if status is None:
        return None, payload
    if status >= 400:
        return None, f"HTTP {status} for {url}"
    text = payload.strip()
    if text.startswith("<?xml") or "<testsuite" in text[:2000] or "<testsuites" in text[:2000]:
        return text, f"Fetched JUnit XML from {url}"
    return None, f"Response from {url} does not look like JUnit XML"


def discover_pipeline_junit_xml_urls(
    pipeline_index_url: str,
    *,
    timeout: float = 60.0,
    junit_subpath: str | None = None,
) -> tuple[list[str], str]:
    """
    Parse an nginx-style directory index for a pipeline folder (**tier-1/**, **tier-2/**,
    or **tier-3/**). For each job subfolder, list ``…/<job>/`` and choose **JUnit** at
    ``{max(N)}/junit.xml`` when numeric run dirs ``1/``, ``2/``, … exist (latest = highest **N**),
    otherwise ``1/junit.xml``.

    Pass *junit_subpath* (e.g. ``\"3/junit.xml\"``) to force the same relative path for **every**
    job (legacy / pinned layout).
    """
    base = pipeline_index_url.strip().rstrip("/") + "/"
    status, html = fetch_url_optional(base, timeout=timeout)
    if status is None:
        return [], f"Failed to list pipeline index: {html}"
    if status >= 400:
        return [], f"HTTP {status} listing {base}"
    urls: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\s+href="([^"]+)"', html, re.IGNORECASE):
        href = html_lib.unescape(m.group(1)).strip()
        if not href or href in ("../", ".."):
            continue
        if ".." in href or href.startswith("?"):
            continue
        full = urljoin(base, href)
        if not full.endswith("/"):
            continue
        child_path = urlparse(full).path.rstrip("/")
        base_path = urlparse(base).path.rstrip("/")
        if not child_path.startswith(base_path + "/") or child_path == base_path:
            continue
        if junit_subpath and junit_subpath.strip():
            rel = junit_subpath.strip().lstrip("/")
        else:
            rel = junit_xml_relative_path_for_job_dir(full, timeout=timeout)
        junit_u = urljoin(full, rel)
        if junit_u not in seen:
            seen.add(junit_u)
            urls.append(junit_u)
    note = f"Discovered {len(urls)} job junit URL(s) under {base}"
    return urls, note


def pipeline_merge_max_jobs() -> int:
    raw = os.environ.get("REPORT_LOGS_PIPELINE_MAX_JOBS") or os.environ.get(
        "REPORT_LOGS_TIER1_MAX_JOBS", "200"
    )
    try:
        n = int(raw)
        return max(1, min(n, 500))
    except ValueError:
        return 200


def fetch_and_merge_junit_urls(
    urls: list[str],
    *,
    timeout: float = 60.0,
) -> tuple[ParseResult | None, str]:
    """
    Fetch each URL, parse JUnit, merge into one ``ParseResult``.
    Skipped URLs are noted; returns ``None`` if none succeeded.
    """
    ok_lines: list[str] = []
    skip_lines: list[str] = []
    parts: list[ParseResult] = []
    for url in urls:
        u = url.strip()
        if not u:
            continue
        xml, note = fetch_junit_from_absolute_url(u, timeout=timeout)
        if xml is None:
            skip_lines.append(f"SKIP {u} — {note}")
            continue
        jname = job_name_from_junit_url(u)
        parts.append(parse_junit_xml(xml, job_name=jname, junit_xml_url=u))
        ok_lines.append(note)
    if not parts:
        body = "No JUnit XML could be fetched."
        if skip_lines:
            body += "\n" + "\n".join(skip_lines)
        return None, body
    merged = merge_parse_results(parts)
    bits = [
        f"Merged {len(parts)} JUnit file(s)"
        + (f"; could not fetch {len(skip_lines)} URL(s)." if skip_lines else "."),
    ]
    bits.extend(ok_lines)
    if skip_lines:
        bits.append("")
        bits.extend(skip_lines)
    return merged, "\n".join(bits)

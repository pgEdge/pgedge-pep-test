#!/usr/bin/env python3
"""
CI-only cross-slice consolidated report generator.

Reads the downloaded per-slice artifact tree produced by the PEP Regression
GitHub Actions workflow and renders a single consolidated-report.html that
spans every slice in the run.

This script is intentionally separate from the report generator embedded in
run_pep_tf.sh: local/sequential reporting is unchanged. It never renames or
flattens per-slice files; it only reads them.

Slice metadata comes from each slice's workflow-summary.txt, with a fallback
to the slice directory name (test-logs-r<N>-a<M>-pg<P>-<family>-<arch>).
Component is derived from the directory path (<slice>/<component>/<pg>/...),
not from report filename string-splitting. Test results are parsed from the
JUnit XML body; report XMLs that are duplicated under a consolidated-* folder
are excluded so results are not double-counted.
"""

import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


# Directory name pattern: test-logs-r<run>-a<attempt>-pg<pg>-<family>-<arch>
_DIRNAME_RE = re.compile(
    r"test-logs-r(?P<run>\d+)-a(?P<attempt>\d+)-pg(?P<pg>[^-]+)-(?P<family>[^-]+)-(?P<arch>.+)$"
)


def parse_slice_metadata(slice_dir: Path) -> dict:
    """Return slice identity for one per-slice artifact directory.

    Primary source: workflow-summary.txt. Fallback: the directory name.
    Always returns pg/family/arch (possibly 'unknown') and a metadata_source.
    """
    meta = {
        "pg": "unknown", "family": "unknown", "arch": "unknown",
        "runner_label": "", "runner_arch": "",
        "run_id": "", "run_attempt": "", "run_number": "",
        "event_name": "", "actor": "",
        "repo": "", "components": "", "execution_mode": "", "sha": "", "ref": "",
        "metadata_source": "",
    }
    summary = slice_dir / "workflow-summary.txt"
    if summary.is_file():
        kv = {}
        for line in summary.read_text(errors="replace").splitlines():
            m = re.match(r"\[workflow-summary\]\s+([^=]+)=(.*)$", line)
            if m:
                kv[m.group(1).strip()] = m.group(2).strip()
        meta["pg"] = kv.get("pg", meta["pg"])
        meta["family"] = kv.get("family", meta["family"])
        meta["arch"] = kv.get("arch", meta["arch"])
        meta["runner_label"] = kv.get("runner.label", "")
        meta["runner_arch"] = kv.get("runner.arch", "")
        meta["run_id"] = kv.get("github.run_id", "")
        meta["run_attempt"] = kv.get("github.run_attempt", "")
        meta["run_number"] = kv.get("github.run_number", "")
        meta["event_name"] = kv.get("github.event_name", "")
        meta["actor"] = kv.get("github.actor", "")
        # Effective run-selection fields (for the report header).
        meta["repo"] = kv.get("repo", "")
        meta["components"] = kv.get("components", "")
        meta["execution_mode"] = kv.get("execution_mode", "")
        meta["sha"] = kv.get("github.sha", "")
        meta["ref"] = kv.get("github.ref", "")
        meta["metadata_source"] = "workflow-summary.txt"
        # If summary somehow lacked the slice keys, backfill from dir name.
        if meta["pg"] == "unknown" or meta["family"] == "unknown" or meta["arch"] == "unknown":
            _backfill_from_dirname(slice_dir, meta)
        return meta

    _backfill_from_dirname(slice_dir, meta)
    meta["metadata_source"] = "directory-name"
    return meta


def _backfill_from_dirname(slice_dir: Path, meta: dict) -> None:
    m = _DIRNAME_RE.search(slice_dir.name)
    if not m:
        return
    if meta["pg"] == "unknown":
        meta["pg"] = m.group("pg")
    if meta["family"] == "unknown":
        meta["family"] = m.group("family")
    if meta["arch"] == "unknown":
        meta["arch"] = m.group("arch")
    if not meta["run_number"]:
        meta["run_number"] = m.group("run")
    if not meta["run_attempt"]:
        meta["run_attempt"] = m.group("attempt")


def discover_report_xmls(slice_dir: Path) -> list:
    """All report-*.xml under the slice, EXCLUDING the duplicate copies that
    live under any consolidated-* directory (those are byte-identical copies
    the framework makes for its own per-slice consolidated page).
    """
    result = []
    for xml in slice_dir.rglob("report-*.xml"):
        if any(part.startswith("consolidated-") for part in xml.relative_to(slice_dir).parts):
            continue
        result.append(xml)
    return result


def derive_component_from_path(xml_path: Path, slice_dir: Path) -> str:
    """Component is the first path segment under the slice directory:
    <slice>/<component>/<pg>/report-*.xml  ->  <component>.
    Falls back to 'unknown' if the layout is unexpected.
    """
    rel = xml_path.relative_to(slice_dir).parts
    if len(rel) >= 1:
        return rel[0]
    return "unknown"


# Match "[<container>-<rhel|deb>" where the type is followed by ] or -.
# Prevents a false match on 'debian' inside names like auto-debian13-amd.
_CONTAINER_RE = re.compile(r"\[(.+)-(rhel|deb)(?=[-\]])")


def _normalize_container(raw: str) -> str:
    """Strip an extension prefix so 'bloom-auto-alma10-arm' -> 'auto-alma10-arm'."""
    for pfx in ("auto-", "my-"):
        idx = raw.find(pfx)
        if idx >= 0:
            return raw[idx:]
    return raw


def _looks_like_container(name: str) -> bool:
    """Containers in containers_list.json all begin with 'auto-' or 'my-'.
    Gates the bracket FALLBACK so arbitrary pytest parameters (a port number,
    a feature flag, etc.) are not mistaken for containers.
    """
    return name.startswith("auto-") or name.startswith("my-")


# Two distinct non-container buckets (kept separate so the report can tell them
# apart and totals stay honest). The labels double as the grouping keys; the
# parentheses guarantee they never collide with a real container name.
#   NO_CONTAINERS_LABEL      : pytest '[NOTSET]' placeholder skips emitted when a
#                              matrix target resolved zero containers (empty
#                              parameter set). These are report-scope metadata,
#                              NOT real tests -> excluded from totals.
#   NOT_CONTAINER_SCOPED_LABEL: genuine tests that simply are not parametrized by
#                              container and actually ran -> counted normally.
NO_CONTAINERS_LABEL = "(none in scope)"
NOT_CONTAINER_SCOPED_LABEL = "(not container-scoped)"


@dataclass(frozen=True)
class TestcaseRecord:
    """One <testcase> from a JUnit XML, attributed to a container.

    Shared in-memory model for both counts (via _summarise_by_container) and
    per-container detail extraction (Task 3+). Holding both views to a single
    parse pass guarantees counts and detail can never drift.
    """
    container: str
    name: str
    time: float
    outcome: str         # "passed" | "failed" | "skipped"
    detail_tag: str      # "failure" | "error" | "skipped" | "" (no detail child)
    message: str
    body: str


def _resolve_container(tc_name: str) -> str:
    """Container attribution for one <testcase> name, identical to the logic
    that lived inline inside parse_junit_xml before the shared-model refactor.
    """
    m = _CONTAINER_RE.search(tc_name)
    if m:
        return _normalize_container(m.group(1))
    m2 = re.search(r"\[([^\]]+)\]", tc_name)
    if m2:
        raw = m2.group(1)
        # 'NOTSET' is pytest's empty-parameter-set sentinel. It appears
        # either alone ('[NOTSET]') or as a '-'-delimited token in a
        # doubly-parametrized id ('[bloom-NOTSET]' = extension set,
        # container empty). Both mean the matrix target had no containers
        # in scope -> report metadata, not a real test.
        if "NOTSET" in raw.split("-"):
            return NO_CONTAINERS_LABEL
        candidate = _normalize_container(raw)
        return candidate if _looks_like_container(candidate) else NOT_CONTAINER_SCOPED_LABEL
    return NOT_CONTAINER_SCOPED_LABEL


def _parse_junit_testcases(xml_path: Path) -> list:
    """Single source of truth for both counts and per-container detail.

    Iterates ALL <testcase> elements anywhere in the tree (single suite,
    multiple <testsuite> under <testsuites>, etc.) and returns a flat list of
    TestcaseRecord. Outcome precedence is identical to the prior count parser:
    <failure> OR <error> -> failed; else <skipped> -> skipped; else passed.
    """
    records = []
    tree = ET.parse(xml_path)
    root = tree.getroot()
    for tc in root.iter("testcase"):
        name = tc.get("name", "")
        tc_time = float(tc.get("time", 0) or 0)
        container = _resolve_container(name)

        failure = tc.find("failure")
        error   = tc.find("error")
        skipped = tc.find("skipped")
        if failure is not None or error is not None:
            outcome = "failed"
            elem = failure if failure is not None else error
            detail_tag = "failure" if failure is not None else "error"
        elif skipped is not None:
            outcome = "skipped"
            elem = skipped
            detail_tag = "skipped"
        else:
            outcome = "passed"
            elem = None
            detail_tag = ""

        message = elem.get("message", "") if elem is not None else ""
        body    = (elem.text or "") if elem is not None else ""

        records.append(TestcaseRecord(
            container=container, name=name, time=tc_time,
            outcome=outcome, detail_tag=detail_tag,
            message=message, body=body,
        ))
    return records


def _summarise_by_container(records: list) -> dict:
    """Reduce a list of TestcaseRecord into the {container: {tests, passed,
    failed, skipped, time}} shape parse_junit_xml has always returned.
    """
    groups = {}
    for r in records:
        g = groups.setdefault(
            r.container,
            {"tests": 0, "passed": 0, "failed": 0, "skipped": 0, "time": 0.0},
        )
        g["tests"] += 1
        g["time"]  += r.time
        g[r.outcome] += 1
    return groups


def parse_junit_xml(xml_path: Path) -> dict:
    """Parse one JUnit XML, grouping every test case by base container.

    Public shape unchanged: returns {container: {tests, passed, failed,
    skipped, time}}. Now implemented as a thin reducer over
    _parse_junit_testcases so per-testcase detail (Task 3+) and counts come
    from the same in-memory pass and cannot drift.

    Container resolution, in order:
      * Primary  : canonical pytest param form '[<container>-<rhel|deb>'.
      * Fallback : a bracketed param that, after normalization, still looks
                   like a container (starts with auto-/my-).
      * Otherwise: NOT_CONTAINER_SCOPED_LABEL (counted, never dropped).
    """
    return _summarise_by_container(_parse_junit_testcases(xml_path))


def _status_for(stats: dict) -> tuple:
    if stats["failed"] > 0:
        return "FAILED", "failed"
    if stats["tests"] > 0 and stats["skipped"] == stats["tests"]:
        return "SKIPPED", "skipped"
    return "PASSED", "passed"


def _real_row_key(row: dict) -> tuple:
    """The 5-tuple identity of a real container row.

    Tuple order matches the positional signature of ``_detail_filename`` and
    ``_assign_unique_detail_filenames(*k)``: ``(component, pg, family, arch,
    container)``. Returned as a stable tuple so the row-key set can be
    compared with ``in`` without ad-hoc string concat.
    """
    return (row["component"], row["pg"], row["family"],
            row["arch"], row["container"])


def _is_real_container_row(row: dict) -> bool:
    """Predicate matching the rows that will claim a detail page filename.

    Real container rows are:
      * NOT the unattributed component bucket,
      * NOT in the (none in scope) or (not container-scoped) buckets,
      * have at least one observed test (tests > 0).

    Anything else (NO REPORTS / PARSE ERROR / NO TESTCASES /
    NO CONTAINERS SELECTED) does not get a detail page.
    """
    if row.get("component") == UNATTRIBUTED_COMPONENT:
        return False
    if row.get("container") in (NO_CONTAINERS_LABEL, NOT_CONTAINER_SCOPED_LABEL):
        return False
    if row.get("tests", 0) <= 0:
        return False
    return True


def _assert_unique_real_row_keys(rows: list) -> None:
    """Assert raw (component, pg, family, arch, container) keys of every
    real-container row are unique. Skip-set rows are ignored.

    The tuple order matches ``_real_row_key`` / ``_detail_filename``'s
    positional signature; uniqueness itself is order-insensitive, but the
    docstring tracks the code so future readers are not misled.

    Fails loudly with an error message that names the duplicate key so a
    triager can find the offending slice quickly. Runs on raw row tuples,
    NOT on generated filenames, so a true duplicate cannot be silently
    masked by the -2/-3 suffix discipline that resolves slug aliasing.
    """
    seen = set()
    for r in rows:
        if not _is_real_container_row(r):
            continue
        key = _real_row_key(r)
        assert key not in seen, (
            f"duplicate real row key {key!r} in build_rows output; "
            "row-key uniqueness is required so per-container detail pages "
            "can be addressed unambiguously"
        )
        seen.add(key)


def build_rows(aggregated_dir: Path) -> list:
    """Walk every per-slice directory under aggregated_dir and produce one row
    per (slice, component, container). Slices with metadata but no report XMLs
    yield a single 'NO REPORTS' row so they are visible, not silently omitted.

    Real container rows additionally carry:
      * ``detail_href`` -- the relative path to the per-container detail
        page that ``main`` writes under ``<output>/details/``. Skip-set rows
        (NO REPORTS / PARSE ERROR / NO TESTCASES / NO CONTAINERS SELECTED /
        non-container buckets / unattributed) get ``detail_href = ""``.
      * ``_records`` -- the in-memory ``TestcaseRecord`` list for the row's
        container, used by main to render the detail page. The leading
        underscore signals scaffolding; main pops it from every row before
        ``render_html`` runs.
    """
    rows = []
    slice_dirs = sorted(
        d for d in aggregated_dir.iterdir()
        if d.is_dir() and d.name.startswith("test-logs-")
    )
    for sd in slice_dirs:
        meta = parse_slice_metadata(sd)
        xmls = discover_report_xmls(sd)

        if not xmls:
            rows.append({
                "pg": meta["pg"], "family": meta["family"], "arch": meta["arch"],
                "runner_label": meta["runner_label"],
                "component": "-", "container": "-",
                "tests": 0, "passed": 0, "failed": 0, "skipped": 0, "time": 0.0,
                "status": "NO REPORTS", "status_class": "noreports",
                "report_href": "", "detail_href": "",
            })
            continue

        for xml in xmls:
            component = derive_component_from_path(xml, sd)
            try:
                records = _parse_junit_testcases(xml)
            except Exception as e:  # malformed XML must not abort the whole report
                rows.append({
                    "pg": meta["pg"], "family": meta["family"], "arch": meta["arch"],
                    "runner_label": meta["runner_label"],
                    "component": component, "container": "-",
                    "tests": 0, "passed": 0, "failed": 0, "skipped": 0, "time": 0.0,
                    "status": "PARSE ERROR", "status_class": "failed",
                    "report_href": "", "detail_href": "",
                    "note": str(e),
                })
                continue
            groups = _summarise_by_container(records)
            html_path = xml.with_suffix(".html")
            href = html_path.relative_to(aggregated_dir).as_posix() if html_path.is_file() else ""
            if not groups:
                # XML parsed but had zero <testcase> elements — surface it as a
                # report-integrity issue rather than silently emitting nothing.
                rows.append({
                    "pg": meta["pg"], "family": meta["family"], "arch": meta["arch"],
                    "runner_label": meta["runner_label"],
                    "component": component, "container": "-",
                    "tests": 0, "passed": 0, "failed": 0, "skipped": 0, "time": 0.0,
                    "status": "NO TESTCASES", "status_class": "noreports",
                    "report_href": href, "detail_href": "",
                })
                continue
            # Group records by container ONCE so each row can carry only its
            # own records (cheap; main's renderer also filters defensively).
            records_by_container = {}
            for rec in records:
                records_by_container.setdefault(rec.container, []).append(rec)
            for container in sorted(groups.keys()):
                stats = groups[container]
                if container == NO_CONTAINERS_LABEL:
                    # Matrix target resolved zero containers (NOTSET placeholder
                    # skips). Surface the row but zero the counts so it does not
                    # inflate real test totals; render_html flags it as an
                    # attention row and counts it under Report Issues.
                    rows.append({
                        "pg": meta["pg"], "family": meta["family"], "arch": meta["arch"],
                        "runner_label": meta["runner_label"],
                        "component": component, "container": container,
                        "tests": 0, "passed": 0, "failed": 0, "skipped": 0, "time": 0.0,
                        "status": "NO CONTAINERS SELECTED", "status_class": "noreports",
                        "report_href": href, "detail_href": "",
                    })
                    continue
                status, status_class = _status_for(stats)
                rows.append({
                    "pg": meta["pg"], "family": meta["family"], "arch": meta["arch"],
                    "runner_label": meta["runner_label"],
                    "component": component, "container": container,
                    "tests": stats["tests"], "passed": stats["passed"],
                    "failed": stats["failed"], "skipped": stats["skipped"],
                    "time": stats["time"],
                    "status": status, "status_class": status_class,
                    "report_href": href, "detail_href": "",
                    "_records": records_by_container.get(container, []),
                })

    # Enforce row-key uniqueness before assigning filenames -- a true
    # duplicate would otherwise be silently masked by the -2/-3 suffix
    # discipline of _assign_unique_detail_filenames.
    _assert_unique_real_row_keys(rows)

    # Filename pass: real container rows get a stable, collision-safe
    # detail_href. Skip-set rows already have detail_href == "".
    real_rows = [r for r in rows if _is_real_container_row(r)]
    keys = [_real_row_key(r) for r in real_rows]
    names = _assign_unique_detail_filenames(keys)
    for r, name in zip(real_rows, names):
        r["detail_href"] = f"details/{name}"

    return rows


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


# Statuses that demand attention: test failures AND data/report problems.
# Used both for sorting (these float to the top) and for the failures-only
# toggle (these stay visible).
_ATTENTION_STATUSES = (
    "FAILED", "PARSE ERROR", "NO REPORTS", "NO TESTCASES", "NO CONTAINERS SELECTED",
)
# Statuses with no real test data — numeric cells render as a dash.
_NO_DATA_STATUSES = ("NO REPORTS", "NO TESTCASES", "NO CONTAINERS SELECTED")
# Statuses that represent a report/coverage problem (Report Issues card).
_ISSUE_STATUSES = ("NO REPORTS", "PARSE ERROR", "NO TESTCASES", "NO CONTAINERS SELECTED")


# Component slug helpers.
#
# Component names go into HTML element IDs (anchors for the heatmap → section
# jump). The slug must be a valid HTML id and must remain unique across the
# components present in one report — even if two raw names normalize to the
# same string ("foo bar" and "foo-bar" both → "foo-bar"). The unattributed
# bucket (component == '-') gets a fixed, reserved slug.
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
UNATTRIBUTED_COMPONENT = "-"
UNATTRIBUTED_SLUG = "unattributed"


def _component_slug(name: str) -> str:
    """Normalize a single component name to an HTML-id-safe slug.

    Does NOT guarantee uniqueness — use _assign_unique_slugs for that.
    """
    if name == UNATTRIBUTED_COMPONENT:
        return UNATTRIBUTED_SLUG
    s = _SLUG_RE.sub("-", name.lower()).strip("-")
    return s or "unnamed"


def _assign_unique_slugs(names) -> dict:
    """Map distinct component names (in iteration order) to unique slugs.

    First claimant wins the base slug; collisions get -2, -3, ... suffixes.
    Deterministic given a deterministic input order.
    """
    used = set()
    out = {}
    for name in names:
        base = _component_slug(name)
        slug = base
        n = 2
        while slug in used:
            slug = f"{base}-{n}"
            n += 1
        used.add(slug)
        out[name] = slug
    return out


# Container alias helpers.
#
# Containers live in containers_list.json with both a canonical NAME used
# everywhere as an identifier (e.g. "auto-alma9-arm", "my-rocky9-amd") and
# a short, human-friendly ALIAS used for display (e.g. "alma9-arm64",
# "rocky9-amd64"). The renderer reads the catalog once and shows the alias
# wherever the container appears as visible text; the actual name moves to
# a `title=` tooltip and remains the value of stable identifiers like the
# row's `container` field, the `data-container` sort attribute, and the
# detail_href filename.

def load_container_aliases(path: Path = None) -> dict:
    """Map container NAME -> short display ALIAS from containers_list.json.

    Returns an empty dict if the catalog isn't present (renderer then falls
    back to displaying the actual name). Defaults to the repo's catalog at
    ../configuration/containers_list.json relative to this script.
    """
    if path is None:
        path = Path(__file__).resolve().parent.parent / "configuration" / "containers_list.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    out = {}
    for family_key in ("rhel", "deb"):
        for entry in data.get(family_key, []) or []:
            name = entry.get("name")
            alias = entry.get("alias")
            if name and alias:
                out[name] = alias
    return out


# Lazily-loaded (module, catalog) pair from container_resolver, used ONLY to
# derive a display alias for synthesized opposite-arch counterparts (implicit
# targets like "auto-ubuntu2404-amd", which have no entry in containers_list.json
# and therefore no alias in the dict above). Loaded via importlib-by-path so it
# works whether or not utillities/ is on sys.path. Cached after first attempt.
_RESOLVER = None  # None = not yet attempted; else (module_or_None, catalog_or_None)


def _get_resolver():
    """Return (resolver_module, catalog) or (None, None). Fully defensive:
    a missing/malformed catalog or any import error yields (None, None) so
    report generation never fails on account of alias prettification."""
    global _RESOLVER
    if _RESOLVER is not None:
        return _RESOLVER
    try:
        import importlib.util
        import sys
        resolver_path = Path(__file__).resolve().parent / "container_resolver.py"
        catalog_path = (Path(__file__).resolve().parent.parent
                        / "configuration" / "containers_list.json")
        spec = importlib.util.spec_from_file_location(
            "pep_container_resolver_for_report", resolver_path
        )
        mod = importlib.util.module_from_spec(spec)
        # Register in sys.modules BEFORE exec so @dataclass field-type
        # resolution can find the module (Python 3.9 importlib gotcha).
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        catalog = mod.load_catalog(catalog_path)
        _RESOLVER = (mod, catalog)
    except Exception:
        _RESOLVER = (None, None)
    return _RESOLVER


def container_display(name: str, aliases: dict) -> str:
    """Short label for one container: alias if present, otherwise the
    actual name (defensive -- a future container missing from the catalog
    must not disappear from the report).

    For synthesized opposite-arch counterparts (implicit targets absent from
    containers_list.json), fall back to the resolver to derive the alias
    (e.g. "auto-ubuntu2404-amd" -> "ubuntu2404-amd64"). Any failure in that
    path is swallowed and the raw name is shown, so a catalog/resolver problem
    never breaks report generation."""
    alias = aliases.get(name)
    if alias:
        return alias
    try:
        mod, catalog = _get_resolver()
        # Only attempt synthesis for names that are NOT real catalog
        # identifiers. A real name the caller simply omitted from `aliases`
        # must still fall back to the raw name (preserves the dict-driven
        # contract); synthesis is reserved for genuine implicit counterparts.
        if (mod is not None and catalog is not None
                and name.lower() not in catalog.lookup_index):
            entry = mod.resolve_token(catalog, name)
            if entry is not None:
                return entry.alias
    except Exception:
        pass
    return name


# Per-container detail-page filename helpers.
#
# Each real container row in the consolidated report gets its own static
# HTML detail page under <output_dir>/details/. The filename embeds all
# five row-key dimensions so collisions can only arise from slug aliasing
# between distinct row keys (e.g. two components whose names normalise to
# the same slug), not from true row-key duplicates -- build_rows asserts
# that invariant.

def _detail_filename(component: str, pg: str, family: str,
                     arch: str, container: str) -> str:
    """Deterministic filename for a single container's detail page.

    All five dimensions are baked in. Component / container / pg / family /
    arch each pass through _component_slug so the result is filesystem-safe
    even if a future container or component name contains whitespace,
    slashes, or other unsafe characters.
    """
    return (
        f"detail-{_component_slug(component)}"
        f"-pg{_component_slug(pg)}"
        f"-{_component_slug(family)}"
        f"-{_component_slug(arch)}"
        f"-{_component_slug(container)}.html"
    )


def _assign_unique_detail_filenames(keys) -> list:
    """Deterministic collision-safe parallel list of filenames for row keys.

    Takes an iterable of (component, pg, family, arch, container) tuples in
    row order. Returns a list of filenames of the same length and order.
    First claimant of a base filename wins; subsequent slug-identical keys
    get -2, -3, ... inserted before the .html suffix.

    Returns a list (not a dict keyed by tuple) so duplicate-shaped keys are
    preserved in row order rather than collapsed by dict-key semantics. In
    practice real row keys are unique -- build_rows asserts that invariant
    -- so this suffix discipline only defends against slug aliasing
    between distinct components, not against true row-key duplicates.
    """
    used = set()
    out = []
    for k in keys:
        base = _detail_filename(*k)
        name = base
        n = 2
        while name in used:
            name = base[:-5] + f"-{n}.html"   # insert before ".html"
            n += 1
        used.add(name)
        out.append(name)
    return out


def aggregate_heatmap(rows: list):
    """Aggregate flat rows into the heatmap grid.

    Returns (components, targets, cells, component_totals).

    components       : list[str] in display order. Attention-first by fail-rate,
                       then clean alpha. Unattributed rows (component == '-')
                       are NOT included — they belong to the trailing
                       unattributed section, not the heatmap.
    targets          : list of (pg, family, arch) tuples, sorted lexicographically.
                       Tuple keys are kept tuple internally; rendering converts.
    cells            : dict { (component, (pg, family, arch)) -> {
                           'tests': int, 'failed': int,
                           'has_issue': bool, 'present': True
                       } }
                       A pair absent from this dict means no row exists for
                       that combination (rendered as an 'empty' cell).
    component_totals : dict { component -> {
                           'tests': int, 'failed': int, 'has_issue': bool,
                           'has_attention': bool, 'fail_rate': float
                       } }
    """
    cells = {}
    components_seen = []
    components_set = set()
    targets_set = set()

    for r in rows:
        comp = r["component"]
        if comp == UNATTRIBUTED_COMPONENT:
            continue
        target = (r["pg"], r["family"], r["arch"])
        targets_set.add(target)
        if comp not in components_set:
            components_set.add(comp)
            components_seen.append(comp)
        key = (comp, target)
        cell = cells.get(key)
        if cell is None:
            cell = {"tests": 0, "failed": 0, "has_issue": False, "present": True}
            cells[key] = cell
        cell["tests"] += r["tests"]
        cell["failed"] += r["failed"]
        if r["status"] in _ISSUE_STATUSES:
            cell["has_issue"] = True

    component_totals = {}
    for c in components_seen:
        tests = 0
        failed = 0
        has_issue = False
        for tg in targets_set:
            cell = cells.get((c, tg))
            if cell is None:
                continue
            tests += cell["tests"]
            failed += cell["failed"]
            if cell["has_issue"]:
                has_issue = True
        rate = (failed / tests) if tests > 0 else 0.0
        component_totals[c] = {
            "tests": tests,
            "failed": failed,
            "has_issue": has_issue,
            "has_attention": (failed > 0) or has_issue,
            "fail_rate": rate,
        }

    def comp_key(c):
        ct = component_totals[c]
        return (
            0 if ct["has_attention"] else 1,
            -ct["fail_rate"],
            c,
        )

    components = sorted(components_seen, key=comp_key)
    targets = sorted(targets_set)
    return components, targets, cells, component_totals


def _compute_totals(rows: list) -> dict:
    """Aggregate the run-level numbers shown in the summary cards."""
    return {
        "total_tests": sum(r["tests"] for r in rows),
        "total_passed": sum(r["passed"] for r in rows),
        "total_failed": sum(r["failed"] for r in rows),
        "total_skipped": sum(r["skipped"] for r in rows),
        "total_time": sum(r["time"] for r in rows),
        # Report-data problems are counted separately so a run with missing/
        # broken reports but no test failures does not look all-green.
        "report_issues": sum(1 for r in rows if r["status"] in _ISSUE_STATUSES),
    }


def _sort_rows(rows: list) -> list:
    """Attention rows (failures, missing/broken reports) first, then by
    pg/family/arch/component/container."""
    def sort_key(r):
        return (0 if r["status"] in _ATTENTION_STATUSES else 1,
                r["pg"], r["family"], r["arch"], r["component"], r["container"])
    return sorted(rows, key=sort_key)


def _sel(ctx: dict, key: str, fallback: str = "—") -> str:
    v = ctx.get(key)
    return _esc(v) if v else fallback


def _render_css() -> str:
    return """
      body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; margin: 20px; background:#f5f5f5; }
      .header { background: linear-gradient(135deg,#667eea,#764ba2); color:#fff; padding:24px; border-radius:8px; }
      .header h1 { margin:0 0 8px 0; }
      .context { font-size:13px; opacity:.95; line-height:1.6; }
      .banner { background:#fff8e1; border:1px solid #f0d98c; color:#7a5b00; padding:12px 16px; border-radius:8px; margin:16px 0; font-size:14px; }
      .banner.banner-issue { background:#fde68a; border-color:#d97706; color:#7c2d12; }
      .banner a { color:inherit; text-decoration:underline; }
      .summary { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin:16px 0; }
      .card { background:#fff; padding:16px; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,.1); }
      .card h3 { margin:0 0 6px 0; font-size:12px; color:#666; text-transform:uppercase; }
      .card .value { font-size:28px; font-weight:bold; }
      .card.total .value{color:#667eea;} .card.passed .value{color:#10b981;}
      .card.failed .value{color:#ef4444;} .card.skipped .value{color:#f59e0b;}
      .card.issues .value{color:#b45309;}
      .controls { margin:12px 0; font-size:14px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
      .controls button { padding:6px 10px; border:1px solid #d9dee7; border-radius:6px; background:#fff; cursor:pointer; font-size:13px; }
      .controls button:hover { background:#f8f9fa; }
      table { width:100%; border-collapse:collapse; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 2px 4px rgba(0,0,0,.1); }
      th { background:#f8f9fa; padding:10px; text-align:left; border-bottom:2px solid #dee2e6; font-size:13px; }
      th button { background:transparent; border:0; font:inherit; font-weight:600; color:#344054; cursor:pointer; padding:0; }
      th button:hover { color:#667eea; }
      td { padding:10px; border-bottom:1px solid #dee2e6; font-size:13px; }
      tr:hover { background:#f8f9fa; }
      .badge { padding:3px 10px; border-radius:12px; font-size:11px; font-weight:600; text-transform:uppercase; }
      .badge.passed{background:#d1fae5;color:#065f46;} .badge.failed{background:#fee2e2;color:#991b1b;}
      .badge.skipped{background:#fef3c7;color:#92400e;} .badge.noreports{background:#e5e7eb;color:#374151;}
      .mono { font-family:monospace; }
      a.report-link { color:#667eea; text-decoration:none; } a.report-link:hover{text-decoration:underline;}
      .footer { margin-top:20px; text-align:center; color:#666; font-size:12px; }
      /* Heatmap */
      .heat-wrap { overflow:auto; border:1px solid #d9dee7; border-radius:8px; background:#fff; margin:16px 0; }
      .heat { display:grid; grid-template-columns: 220px repeat(var(--cols), minmax(92px, 1fr)); gap:1px; background:#d9dee7; min-width:980px; }
      .heat .h { background:#fff; padding:9px 10px; font-size:13px; min-height:42px; }
      .heat .head { font-weight:700; background:#eef2f7; text-align:center; }
      .heat .head span { display:block; font-weight:500; color:#667085; font-size:12px; }
      .heat .sticky-left { position:sticky; left:0; z-index:2; }
      .heat .comp { text-align:left; cursor:pointer; color:#2447a8; border:0; background:#fff; font:inherit; font-weight:700; }
      .heat .comp:hover { background:#f1f5ff; text-decoration:underline; }
      .heat .cell { text-align:center; font-variant-numeric:tabular-nums; }
      .heat .ok { background:#dff7e8; }
      .heat .warn { background:#fff2cc; }
      .heat .bad { background:#ffd8d5; }
      .heat .severe { background:#fda29b; font-weight:800; }
      .heat .issue { background:#fde68a; color:#7c2d12; }
      .heat .empty { background:#f8fafc; color:#98a2b3; }
      .heat .issue-marker { box-shadow: inset 0 0 0 2px #d97706; }
      .heat .total { font-weight:700; background:#f8fafc; text-align:center; }
      .heat .failtotal { color:#b42318; }
      /* Component sections */
      details.component { background:#fff; border:1px solid #d9dee7; border-radius:8px; margin:10px 0; overflow:hidden; box-shadow:0 2px 4px rgba(0,0,0,.05); }
      details.component summary { cursor:pointer; padding:13px 15px; display:flex; gap:14px; align-items:center; flex-wrap:wrap; list-style:none; }
      details.component summary::-webkit-details-marker { display:none; }
      details.component summary::before { content:'\\25B6'; color:#667085; margin-right:8px; font-size:11px; }
      details.component[open] summary::before { content:'\\25BC'; }
      details.component summary .chip { border:1px solid #d9dee7; border-radius:999px; padding:2px 8px; color:#475467; font-size:12px; }
      details.component summary .chip.fail { background:#fee4e2; color:#b42318; border-color:#fecdca; }
      details.component summary .chip.issue { background:#fde68a; color:#7c2d12; border-color:#d97706; }
      details.component[open] summary { background:#f8fafc; border-bottom:1px solid #d9dee7; }
      details.component .table-wrap { overflow:auto; }
      details.component table { box-shadow:none; border-radius:0; }
      /* Failures-only toggle: CSS-driven, does NOT mutate <details>.open state */
      body.failures-only details.component tbody tr[data-fail="0"] { display:none; }
      body.failures-only details.component:not([data-has-attention]) { display:none; }
    """


def _render_header(ctx: dict) -> str:
    return f"""<div class="header">
  <h1>PEP Regression Consolidated Report</h1>
  <div class="context">
    Run #{_esc(ctx.get('run_number'))} (attempt {_esc(ctx.get('run_attempt'))}) &middot;
    run_id {_esc(ctx.get('run_id'))} &middot;
    event {_esc(ctx.get('event_name'))} &middot;
    by {_esc(ctx.get('actor'))} &middot;
    branch {_sel(ctx, 'ref')} &middot; sha {_sel(ctx, 'sha')} &middot;
    {_esc(ctx.get('slice_count'))} matrix target(s)
  </div>
  <div class="context" style="margin-top:8px">
    <strong>Effective selection:</strong>
    PG {_sel(ctx, 'pg_versions')} &middot; families {_sel(ctx, 'families')} &middot;
    arches {_sel(ctx, 'arches')} &middot; components {_sel(ctx, 'components')} &middot;
    repo {_sel(ctx, 'repo')} &middot; mode {_sel(ctx, 'execution_mode')}
  </div>
</div>"""


def _render_banner() -> str:
    return """<div class="banner">
  <strong>Note:</strong> A matrix target (runner) showing green in GitHub Actions
  reflects workflow completion, not whether every component test passed. Per-row
  PASS/FAILED/SKIPPED counts below are the source of truth for test outcomes.
</div>"""


def _render_summary_cards(totals: dict) -> str:
    return f"""<div class="summary">
  <div class="card total"><h3>Total Tests</h3><div class="value">{totals['total_tests']}</div></div>
  <div class="card passed"><h3>Passed</h3><div class="value">{totals['total_passed']}</div></div>
  <div class="card failed"><h3>Failed</h3><div class="value">{totals['total_failed']}</div></div>
  <div class="card skipped"><h3>Skipped</h3><div class="value">{totals['total_skipped']}</div></div>
  <div class="card issues"><h3>Report Issues</h3><div class="value">{totals['report_issues']}</div></div>
</div>"""


def _band(tests: int, failed: int, has_issue: bool, present: bool) -> str:
    """Return the CSS class for the rate band of a heatmap cell.

    Bands (failure rate = failed/tests):
      no row             -> 'empty'
      tests==0 + issue   -> 'issue'   (NO CONTAINERS / NO REPORTS / etc.)
      tests==0 (no data) -> 'empty'
      failed == 0        -> 'ok'
      0 < rate <= 5%     -> 'warn'
      5% < rate <= 15%   -> 'bad'
      rate > 15%         -> 'severe'

    Refinement 4: if a cell has BOTH real tests AND an issue, this returns the
    rate band only — the renderer is responsible for overlaying an 'issue-marker'
    class so the report problem stays visible behind the band color.
    """
    if not present:
        return "empty"
    if tests == 0:
        return "issue" if has_issue else "empty"
    if failed == 0:
        return "ok"
    rate = failed / tests
    if rate <= 0.05:
        return "warn"
    if rate <= 0.15:
        return "bad"
    return "severe"


def target_containers(rows: list) -> dict:
    """Map each heatmap target (pg, family, arch) to the sorted list of
    distinct containers that roll up into it. A single column aggregates many
    containers (e.g. a deb/arm64 target spans debian + ubuntu images), so this
    drives the header tooltip that tells the reader exactly which containers a
    column covers. Unattributed rows (component == '-') are excluded — they are
    not part of the heatmap."""
    out = {}
    for r in rows:
        if r["component"] == UNATTRIBUTED_COMPONENT:
            continue
        target = (r["pg"], r["family"], r["arch"])
        out.setdefault(target, set()).add(r["container"])
    return {k: sorted(v) for k, v in out.items()}


def _render_heatmap(components: list, targets: list,
                    cells: dict, component_totals: dict,
                    slugs: dict, tgt_containers: dict = None,
                    aliases: dict = None) -> str:
    if not components or not targets:
        return ""
    tgt_containers = tgt_containers or {}
    aliases = aliases or {}
    # +2 columns for the two right-edge totals (Failed total, Tests total).
    n_cols = len(targets) + 2
    parts = ['<div class="heat-wrap">',
             f'<div class="heat" style="--cols:{n_cols}">']
    # Header row
    parts.append('<div class="h head sticky-left">Component</div>')
    for (pg, family, arch) in targets:
        conts = tgt_containers.get((pg, family, arch), [])
        if conts:
            # Show short alias display names in the tooltip; sort the
            # display labels so the order is stable regardless of catalog
            # entry order. Falls back per-container to the actual name when
            # an alias isn't known.
            display_conts = sorted(container_display(c, aliases) for c in conts)
            n = len(display_conts)
            plural = "s" if n != 1 else ""
            head_tip = (
                f"PG{pg} {family} {arch} — {n} container{plural}: "
                + ", ".join(display_conts)
            )
        else:
            head_tip = f"PG{pg} {family} {arch}"
        parts.append(
            f'<div class="h head" title="{_esc(head_tip)}">'
            f'PG{_esc(pg)}<span>{_esc(family)} {_esc(arch)}</span></div>'
        )
    parts.append('<div class="h head">Failed<span>total</span></div>')
    parts.append('<div class="h head">Tests<span>total</span></div>')
    # Component rows
    for c in components:
        slug = slugs[c]
        # Component label is a button that opens + scrolls to the section.
        # Both the on-screen text and the onclick arg are individually escaped.
        parts.append(
            f'<button class="h comp sticky-left" '
            f'onclick="openComponent({_esc(slug)!r})">{_esc(c)}</button>'
        )
        for tg in targets:
            cell = cells.get((c, tg))
            present = cell is not None
            tests = cell["tests"] if present else 0
            failed = cell["failed"] if present else 0
            has_issue = cell["has_issue"] if present else False
            band = _band(tests, failed, has_issue, present)
            classes = ["h", "cell", band]
            issue_overlay = (tests > 0 and has_issue)
            if issue_overlay:
                classes.append("issue-marker")
            (pg, family, arch) = tg
            tooltip_bits = [f"{c} PG{pg} {family} {arch}:",
                            f"{failed} failed of {tests}"]
            if has_issue:
                tooltip_bits.append("(report issue present)")
            tooltip = " ".join(tooltip_bits)
            if present:
                content = f"{failed}/{tests}"
                if issue_overlay:
                    content = "&#9888; " + content
            else:
                content = "&mdash;"
            parts.append(
                f'<div class="{" ".join(classes)}" title="{_esc(tooltip)}">'
                f'{content}</div>'
            )
        ct = component_totals[c]
        parts.append(f'<div class="h total failtotal">{ct["failed"]}</div>')
        parts.append(f'<div class="h total">{ct["tests"]}</div>')
    parts.append('</div></div>')
    return "\n".join(parts)


def _render_unattributed_banner(rows: list, slug: str = UNATTRIBUTED_SLUG) -> str:
    """Banner above the heatmap when any rows have component == '-' (NO REPORTS
    rows that can't be aggregated under a real component).

    The slug for the bucket section must be passed in from the run-wide slug map
    so the banner link resolves to the actual section anchor even when a real
    component normalizes to the reserved "unattributed" slug and the bucket gets
    bumped to "unattributed-2" by _assign_unique_slugs.
    """
    n = sum(1 for r in rows if r["component"] == UNATTRIBUTED_COMPONENT)
    if n == 0:
        return ""
    plural = "s" if n != 1 else ""
    return (
        f'<div class="banner banner-issue">'
        f'<strong>&#9888;</strong> {n} matrix target{plural} produced no '
        f'reports &mdash; see the '
        f'<a href="#comp-{_esc(slug)}">unattributed section</a> '
        f'below for details.</div>'
    )


def _render_controls() -> str:
    return """<div class="controls">
  <label><input type="checkbox" id="failuresOnly" onclick="toggleFailures()"> Show attention rows only (failures, missing &amp; broken reports)</label>
  <button type="button" onclick="openFailing()">Open failing components</button>
  <button type="button" onclick="expandAll()">Expand all</button>
  <button type="button" onclick="collapseAll()">Collapse all</button>
</div>"""


def _render_component_section(name: str, slug: str, rows_for_comp: list,
                              aliases: dict = None) -> str:
    """Render one <details> block for a component (or the unattributed bucket).

    The Container column shows the container's short alias (from
    containers_list.json) when known; the actual name is kept as a title=
    tooltip on the cell. The row's data-container attribute stays the actual
    name so JS sorting and filtering keep a stable identifier.

    All visible text is escaped via _esc(). Per-row data-* attributes also go
    through _esc() so a hostile component / container value can't break the
    surrounding HTML.
    """
    aliases = aliases or {}
    row_count = len(rows_for_comp)
    tests = sum(r["tests"] for r in rows_for_comp)
    failed = sum(r["failed"] for r in rows_for_comp)
    skipped = sum(r["skipped"] for r in rows_for_comp)
    has_issue = any(r["status"] in _ISSUE_STATUSES for r in rows_for_comp)
    has_attention = any(r["status"] in _ATTENTION_STATUSES for r in rows_for_comp)

    # Sort within the section: attention rows first, then pg/family/arch/container.
    section_rows = sorted(
        rows_for_comp,
        key=lambda r: (
            0 if r["status"] in _ATTENTION_STATUSES else 1,
            r["pg"], r["family"], r["arch"], r["container"],
        ),
    )

    chips = [
        f'<span class="chip">{row_count} rows</span>',
        f'<span class="chip">{tests} tests</span>',
    ]
    if failed > 0:
        chips.append(f'<span class="chip fail">{failed} failed</span>')
    if skipped > 0:
        chips.append(f'<span class="chip">{skipped} skipped</span>')
    if tests > 0:
        pct = (failed / tests) * 100.0
        chips.append(f'<span class="chip">{pct:.1f}% fail</span>')
    if has_issue and failed == 0:
        chips.append('<span class="chip issue">&#9888; report issue</span>')

    open_attr = " open" if has_attention else ""
    attention_attr = ' data-has-attention="1"' if has_attention else ""
    # Unattributed bucket displays a friendly label instead of the bare '-' marker.
    display_name = "(unattributed)" if name == UNATTRIBUTED_COMPONENT else name

    parts = [
        f'<details class="component" id="comp-{_esc(slug)}"{open_attr}{attention_attr}>',
        '<summary>',
        f'<strong>{_esc(display_name)}</strong>',
    ]
    parts.extend(chips)
    parts.append('</summary>')
    parts.append('<div class="table-wrap"><table>')
    parts.append(
        '<thead><tr>'
        '<th><button onclick="sortGroup(this,\'pg\')">PG</button></th>'
        '<th><button onclick="sortGroup(this,\'family\')">Family</button></th>'
        '<th><button onclick="sortGroup(this,\'arch\')">Arch</button></th>'
        '<th><button onclick="sortGroup(this,\'container\')">Container</button></th>'
        '<th><button onclick="sortGroup(this,\'status\')">Status</button></th>'
        '<th>Tests</th><th>Passed</th><th>Failed</th><th>Skipped</th>'
        '<th>Time (s)</th><th>Report</th>'
        '</tr></thead><tbody>'
    )
    for r in section_rows:
        is_fail = "1" if r["status"] in _ATTENTION_STATUSES else "0"
        # The Report column points at the per-container detail page generated
        # by main (Task 5). report_href stays on the row only as the source
        # for the detail page's back-link to the framework's combined report.
        if r.get("detail_href"):
            link = (f'<a class="report-link" href="{_esc(r["detail_href"])}">'
                    f'View &rarr;</a>')
        else:
            link = "&mdash;"
        if r["status"] in _NO_DATA_STATUSES:
            tcell = pcell = fcell = scell = timecell = "&mdash;"
        else:
            tcell, pcell = str(r["tests"]), str(r["passed"])
            fcell, scell = str(r["failed"]), str(r["skipped"])
            timecell = f"{r['time']:.2f}"
        parts.append(
            f'<tr data-fail="{is_fail}" '
            f'data-pg="{_esc(r["pg"])}" '
            f'data-family="{_esc(r["family"])}" '
            f'data-arch="{_esc(r["arch"])}" '
            f'data-container="{_esc(r["container"])}" '
            f'data-status="{_esc(r["status"])}">'
            f'<td>{_esc(r["pg"])}</td>'
            f'<td>{_esc(r["family"])}</td>'
            f'<td>{_esc(r["arch"])}</td>'
            f'<td class="mono" title="{_esc(r["container"])}">'
            f'{_esc(container_display(r["container"], aliases))}</td>'
            f'<td><span class="badge {_esc(r["status_class"])}">'
            f'{_esc(r["status"])}</span></td>'
            f'<td class="mono">{tcell}</td>'
            f'<td class="mono">{pcell}</td>'
            f'<td class="mono">{fcell}</td>'
            f'<td class="mono">{scell}</td>'
            f'<td class="mono">{timecell}</td>'
            f'<td>{link}</td>'
            f'</tr>'
        )
    parts.append('</tbody></table></div>')
    parts.append('</details>')
    return "\n".join(parts)


def _render_component_sections(rows: list, components: list, slugs: dict,
                               aliases: dict = None) -> str:
    """Render all per-component sections in the given component order, plus
    a trailing unattributed section if any rows have component == '-'.

    The slugs dict should already have a unique slug for UNATTRIBUTED_COMPONENT
    if the caller wants a real component named "unattributed" not to collide
    with the bucket. _assign_unique_slugs handles the collision when called
    with both entries.

    `aliases` is forwarded to each section renderer so the Container column
    shows the short alias from containers_list.json.
    """
    by_comp = {}
    unattributed_rows = []
    for r in rows:
        if r["component"] == UNATTRIBUTED_COMPONENT:
            unattributed_rows.append(r)
        else:
            by_comp.setdefault(r["component"], []).append(r)

    parts = []
    for c in components:
        parts.append(_render_component_section(c, slugs[c], by_comp.get(c, []),
                                               aliases=aliases))
    if unattributed_rows:
        un_slug = slugs.get(UNATTRIBUTED_COMPONENT, UNATTRIBUTED_SLUG)
        parts.append(_render_component_section(
            UNATTRIBUTED_COMPONENT, un_slug, unattributed_rows,
            aliases=aliases,
        ))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Per-container detail page rendering.
#
# One self-contained HTML page per real container row, written under
# <output_dir>/details/. Each page lists ONLY that container's testcases
# (renderer defensively filters), with failure/error/skipped tracebacks
# tucked into collapsed <details> elements so the page stays small until
# the user opens them.
# ---------------------------------------------------------------------------

_DETAIL_STYLE = """<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; margin: 20px; background:#f5f5f5; color:#1e293b; }
  .hd { background: linear-gradient(135deg,#667eea,#764ba2); color:#fff; padding:20px 24px; border-radius:8px; }
  .hd h1 { margin:0 0 6px 0; font-size:22px; }
  .hd .meta { font-size:13px; opacity:.95; }
  .hd .meta .fail { color:#fecaca; font-weight:600; }
  .hd a.back { display:inline-block; margin-top:10px; color:#fff; text-decoration:underline; font-size:13px; }
  .cases { margin:16px 0; display:flex; flex-direction:column; gap:8px; }
  .tcase { background:#fff; border:1px solid #d9dee7; border-radius:8px; padding:12px 14px; box-shadow:0 1px 3px rgba(0,0,0,.06); }
  .tcase.failed  { border-left:4px solid #ef4444; }
  .tcase.skipped { border-left:4px solid #f59e0b; }
  .tcase.passed  { border-left:4px solid #10b981; }
  .tcase .hdr { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  .tcase .nm { font-family:monospace; font-size:13px; color:#1e293b; }
  .tcase .time { margin-left:auto; font-family:monospace; color:#64748b; font-size:12px; }
  .badge { padding:3px 10px; border-radius:12px; font-size:11px; font-weight:600; text-transform:uppercase; }
  .badge.passed  { background:#d1fae5; color:#065f46; }
  .badge.failed  { background:#fee2e2; color:#991b1b; }
  .badge.skipped { background:#fef3c7; color:#92400e; }
  .msg { margin-top:8px; font-family:monospace; font-size:12px; color:#7a2218; background:#fef2f2; border-radius:6px; padding:8px 10px; white-space:pre-wrap; }
  details { margin-top:8px; }
  details summary { cursor:pointer; font-size:12px; color:#475569; }
  details pre { background:#0f172a; color:#e2e8f0; padding:10px 12px; border-radius:6px; overflow:auto; font-size:11.5px; line-height:1.45; max-height:500px; }
  .footer { margin-top:18px; text-align:center; font-size:12px; color:#666; }
  .footer a { color:#667eea; text-decoration:none; }
  .footer a:hover { text-decoration:underline; }
</style>"""


def _detail_badge(outcome: str, detail_tag: str) -> str:
    """Status badge for one testcase. detail_tag picks ERROR vs FAIL for
    failed outcomes; passed/skipped are unambiguous."""
    if outcome == "failed":
        label = "ERROR" if detail_tag == "error" else "FAIL"
        cls = "failed"
    elif outcome == "skipped":
        label, cls = "SKIP", "skipped"
    else:
        label, cls = "PASS", "passed"
    return f'<span class="badge {cls}">{label}</span>'


def render_container_detail_page(component: str, pg: str, family: str,
                                 arch: str, container: str,
                                 records: list,
                                 back_link_href: str,
                                 consolidated_filename: str,
                                 container_alias: str = None) -> str:
    """Render the per-container detail HTML.

    Defensively filters `records` to the requested container -- callers may
    pass a superset (or the full per-XML list) and only the matching
    testcases will appear in the output.

    `back_link_href` is the relative href to the framework's combined
    pytest-html for this target (the row's `report_href`, prefixed `../`).
    `consolidated_filename` is the filename of the consolidated report in
    the parent directory (e.g. "consolidated-report.html"); the footer
    back-link is rendered as `../<consolidated_filename>` so local
    regenerations with a different output filename (e.g.
    `consolidated-report-v24.html`) link correctly.

    `container_alias` is the short display name from containers_list.json
    (e.g. "alma9-arm64"). When provided, the heading shows the alias and
    the actual container name moves to a tooltip on the chip. When
    omitted, the heading falls back to the actual container name.
    """
    # Defensive filter -- DO NOT trust the caller to have pre-filtered.
    scoped = [r for r in records if r.container == container]
    n_total   = len(scoped)
    n_passed  = sum(1 for r in scoped if r.outcome == "passed")
    n_failed  = sum(1 for r in scoped if r.outcome == "failed")
    n_skipped = sum(1 for r in scoped if r.outcome == "skipped")

    # failed -> skipped -> passed, stable within each bucket
    order = {"failed": 0, "skipped": 1, "passed": 2}
    scoped_sorted = sorted(scoped, key=lambda r: order[r.outcome])

    case_blocks = []
    for r in scoped_sorted:
        traceback_block = ""
        if r.outcome != "passed":
            if r.message:
                traceback_block += f'<div class="msg">{_esc(r.message)}</div>'
            if r.body.strip():
                traceback_block += (
                    '<details><summary>Show traceback</summary>'
                    f'<pre>{_esc(r.body)}</pre></details>'
                )
        case_blocks.append(
            f'<div class="tcase {_esc(r.outcome)}">'
            '<div class="hdr">'
            f'{_detail_badge(r.outcome, r.detail_tag)}'
            f'<span class="nm">{_esc(r.name)}</span>'
            f'<span class="time">{r.time:.2f}s</span>'
            '</div>'
            f'{traceback_block}'
            '</div>'
        )

    display = container_alias or container
    title = f"{component} - {display} - PG{pg} {family} {arch}"
    # The visible heading uses the alias; the actual container name is in a
    # tooltip on the chip so it's one hover away (and still appears verbatim
    # in the page so triagers searching the file by raw name still find it).
    heading_container = (
        f'<span title="{_esc(container)}">{_esc(display)}</span>'
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>'
        f'<title>{_esc(title)}</title>{_DETAIL_STYLE}</head><body>'
        '<div class="hd">'
        f'<h1>{_esc(component)} &middot; {heading_container}</h1>'
        f'<div class="meta">PG{_esc(pg)} &middot; {_esc(family)} &middot; {_esc(arch)} '
        f'&middot; {n_total} tests &middot; '
        f'<span class="fail">{n_failed} failed</span> &middot; '
        f'{n_skipped} skipped &middot; {n_passed} passed</div>'
        f'<a class="back" href="{_esc(back_link_href)}">Full pytest-html report &rarr;</a>'
        '</div>'
        '<div class="cases">' + "".join(case_blocks) + '</div>'
        f'<div class="footer"><a href="../{_esc(consolidated_filename)}">&larr; consolidated report</a></div>'
        '</body></html>'
    )


def _render_footer(rows: list, total_time: float) -> str:
    return f"""<div class="footer">
  {len(rows)} row(s) &middot; total execution time {total_time:.2f}s across all matrix targets.
</div>"""


def _render_scripts() -> str:
    # ONE <script> block. Preserves the test invariant
    # `out.count("<script") == 1`. Adding a second block would break it.
    #
    # toggleFailures uses a body class (CSS-driven hiding) instead of
    # mutating details.open — refinement 3 of v2.3. The user's open/closed
    # state survives flipping the toggle.
    return """<script>
const STATUS_RANK = {
  'FAILED':0,'PARSE ERROR':1,'NO REPORTS':2,'NO TESTCASES':3,
  'NO CONTAINERS SELECTED':4,'SKIPPED':5,'PASSED':6
};

function toggleFailures() {
  const on = document.getElementById('failuresOnly').checked;
  document.body.classList.toggle('failures-only', on);
}

function sortGroup(btn, key) {
  const table = btn.closest('table');
  const tbody = table.querySelector('tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const dir = btn.dataset.dir === 'asc' ? 'desc' : 'asc';
  btn.dataset.dir = dir;
  table.querySelectorAll('thead button').forEach(b => {
    if (b !== btn) {
      b.dataset.dir = '';
      b.textContent = b.textContent.replace(/[\\u25B2\\u25BC]\\s*$/, '');
    }
  });
  btn.textContent = btn.textContent.replace(/[\\u25B2\\u25BC]\\s*$/, '') +
                    (dir === 'asc' ? ' \\u25B2' : ' \\u25BC');
  const get = key === 'status'
    ? r => (r.dataset.status in STATUS_RANK ? STATUS_RANK[r.dataset.status] : 99)
    : r => r.dataset[key] || '';
  rows.sort((a, b) => {
    const va = get(a), vb = get(b);
    if (typeof va === 'number' && typeof vb === 'number') {
      return dir === 'asc' ? va - vb : vb - va;
    }
    return dir === 'asc'
      ? String(va).localeCompare(String(vb))
      : String(vb).localeCompare(String(va));
  });
  rows.forEach(r => tbody.appendChild(r));
}

function openComponent(slug) {
  const d = document.getElementById('comp-' + slug);
  if (!d) return;
  d.open = true;
  d.scrollIntoView({behavior:'smooth', block:'start'});
}

function expandAll() {
  document.querySelectorAll('details.component').forEach(d => d.open = true);
}

function collapseAll() {
  document.querySelectorAll('details.component').forEach(d => d.open = false);
}

function openFailing() {
  document.querySelectorAll('details.component').forEach(d => {
    d.open = d.hasAttribute('data-has-attention');
  });
}
</script>"""


def render_html(rows: list, ctx: dict, aliases: dict = None) -> str:
    """Render the consolidated report.

    `aliases` is the container-name -> display-alias map produced by
    load_container_aliases(). Passed through to the Container column. If
    omitted or empty, the column falls back to the actual container name
    (the renderer never drops a row just because the catalog hasn't caught
    up to a new container).
    """
    aliases = aliases or {}
    rows = _sort_rows(rows)
    totals = _compute_totals(rows)
    components, targets, cells, ctotals = aggregate_heatmap(rows)
    tgt_containers = target_containers(rows)
    has_unattributed = any(
        r["component"] == UNATTRIBUTED_COMPONENT for r in rows
    )
    # Slug assignment is run-wide so a real component named "unattributed"
    # cannot collide with the bucket's reserved slug.
    slug_input = list(components)
    if has_unattributed:
        slug_input.append(UNATTRIBUTED_COMPONENT)
    slugs = _assign_unique_slugs(slug_input)

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"/>'
        '<title>PEP Regression Consolidated Report</title>'
        f'<style>{_render_css()}</style></head><body>\n'
        + _render_header(ctx) + "\n"
        + _render_banner() + "\n"
        + _render_summary_cards(totals) + "\n"
        + _render_unattributed_banner(
            rows, slugs.get(UNATTRIBUTED_COMPONENT, UNATTRIBUTED_SLUG)
        ) + "\n"
        + _render_heatmap(components, targets, cells, ctotals, slugs,
                          tgt_containers, aliases=aliases) + "\n"
        + _render_controls() + "\n"
        + _render_component_sections(rows, components, slugs, aliases=aliases) + "\n"
        + _render_footer(rows, totals["total_time"]) + "\n"
        + _render_scripts()
        + "\n</body></html>"
    )


def _run_context(aggregated_dir: Path, rows: list) -> dict:
    """Run-level context + effective selection, derived from the per-target
    workflow-summary.txt files. The 'effective selection' (distinct pg/family/
    arch + components/repo/execution_mode) is what the run actually covered;
    raw workflow_dispatch inputs are not in the artifact (and effective values
    are clearer anyway)."""
    ctx = {"run_number": "", "run_attempt": "", "run_id": "",
           "event_name": "", "actor": "", "slice_count": 0,
           "pg_versions": "", "families": "", "arches": "",
           "components": "", "repo": "", "execution_mode": "",
           "sha": "", "ref": ""}
    slice_dirs = [d for d in aggregated_dir.iterdir()
                  if d.is_dir() and d.name.startswith("test-logs-")]
    ctx["slice_count"] = len(slice_dirs)

    pgs, fams, arches = set(), set(), set()
    run_fields_set = False
    for sd in sorted(slice_dirs):
        meta = parse_slice_metadata(sd)
        if meta["pg"] and meta["pg"] != "unknown":
            pgs.add(meta["pg"])
        if meta["family"] and meta["family"] != "unknown":
            fams.add(meta["family"])
        if meta["arch"] and meta["arch"] != "unknown":
            arches.add(meta["arch"])
        # Run-level + same-across-targets fields: take from the first target
        # that carries them.
        if not run_fields_set and (meta["run_id"] or meta["run_number"]):
            ctx.update({
                "run_number": meta["run_number"], "run_attempt": meta["run_attempt"],
                "run_id": meta["run_id"], "event_name": meta["event_name"],
                "actor": meta["actor"], "repo": meta["repo"],
                "components": meta["components"], "execution_mode": meta["execution_mode"],
                "sha": meta["sha"], "ref": meta["ref"],
            })
            run_fields_set = True

    ctx["pg_versions"] = ", ".join(sorted(pgs))
    ctx["families"] = ", ".join(sorted(fams))
    ctx["arches"] = ", ".join(sorted(arches))
    return ctx


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the cross-slice consolidated CI report.")
    ap.add_argument("--input-dir", required=True, help="Directory of downloaded per-slice artifacts.")
    ap.add_argument("--output", required=True, help="Path to write consolidated-report.html.")
    args = ap.parse_args(argv)

    aggregated = Path(args.input_dir)
    if not aggregated.is_dir():
        print(f"[ci-report] input dir not found: {aggregated}", file=sys.stderr)
        return 1

    slice_dirs = [d for d in aggregated.iterdir()
                  if d.is_dir() and d.name.startswith("test-logs-")]
    rows = build_rows(aggregated)
    ctx = _run_context(aggregated, rows)
    aliases = load_container_aliases()  # {} if catalog missing -> falls back to actual names

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    # ------- Per-container detail pages (Task 5) -------
    # Write one HTML per real container row into <out_dir>/details/. Before
    # writing, remove ONLY files matching `detail-*.html` from a prior
    # regeneration so stale pages don't leak into the new artifact; anything
    # else in details/ (notes, ad-hoc files) is left untouched.
    details_dir = out.parent / "details"
    consolidated_filename = out.name
    if details_dir.is_dir():
        for stale in details_dir.glob("detail-*.html"):
            stale.unlink()
    for r in rows:
        href = r.get("detail_href", "")
        if href:
            details_dir.mkdir(parents=True, exist_ok=True)
            target_path = details_dir / Path(href).name
            # Back-link to the framework's combined report, relative to details/.
            back = ("../" + r["report_href"]) if r.get("report_href") \
                else f"../{consolidated_filename}"
            target_path.write_text(
                render_container_detail_page(
                    component=r["component"], pg=r["pg"], family=r["family"],
                    arch=r["arch"], container=r["container"],
                    records=r.get("_records") or [],
                    back_link_href=back,
                    consolidated_filename=consolidated_filename,
                    container_alias=aliases.get(r["container"]),
                ),
                encoding="utf-8",
            )
        # _records is internal scaffolding -- drop from EVERY row (including
        # skip-set rows that never wrote a page) before render_html sees them.
        r.pop("_records", None)

    out.write_text(render_html(rows, ctx, aliases=aliases), encoding="utf-8")

    fails = sum(r["failed"] for r in rows)
    print(f"[ci-report] targets={len(slice_dirs)} rows={len(rows)} "
          f"tests={sum(r['tests'] for r in rows)} failed={fails} "
          f"-> {out}")
    # Always exit 0: this is a reporting step, not a gate. It must not fail the
    # aggregate job on the basis of underlying test failures.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

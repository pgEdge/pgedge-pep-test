#!/usr/bin/env python3
"""Render a self-contained, browsable PEP certification report.

The certification workflow already produces authoritative JSON (cert-result/1 +
pep-cert-decision/1) and one ``pep-summary`` artifact per test leg. What it did
NOT have was one download that lets a reviewer browse every platform's test
cases and failure reasons. This generator adds that page WITHOUT changing any
status/policy contract:

  * The certification JSON is the SOLE source of truth for the execution, test,
    coverage and policy axes and for every leg's verdict. This generator never
    recomputes them from JUnit and never overwrites them. An observe-mode
    workflow can be green while the product test verdict is fail; the report
    shows both, plainly.
  * The per-test-case DETAIL pages REUSE the established regression report's
    JUnit parser (``_parse_junit_testcases``) and detail renderer
    (``render_container_detail_page``) so both workflows share one test-case
    experience. The OVERVIEW also wears that report's stylesheet and runs its
    interaction script unchanged, but lays out its own model: a platform x PG
    matrix grouped by package family, coloured by each leg's authoritative
    verdict (never a JUnit failure rate), with coverage gaps as separate rows
    that are never drawn as a pass or as a failed test.
  * It is a REPORT, not a gate. It always exits 0. On missing, empty, partial or
    disagreeing per-case evidence it emits a truthful report issue (never a
    false pass), and on a fatal input problem it writes a fallback page that
    never claims a trusted PASS. It never touches the JSON or the decision.

Leg -> JUnit linking (deliberately strict):
  * The collection ledger is used only as an ALLOWLIST: only ``one_summary``,
    non-expired candidates are read. It carries no invocation id, so each accepted
    ``summary.json`` is read and indexed by its ``invocation_id``.
  * Only a MATCHED leg can own per-case evidence, and only the accepted summary
    whose provenance is EXACTLY the leg's own provenance (which the reducer copies
    verbatim from the summary it accepted). This follows the reducer's attempt
    rule: under "re-run failed jobs" the plan's ``provenance.run_attempt`` stays at
    the capture attempt while current legs carry the newer producing attempt, so
    the top-level attempt is never used to select evidence. A synthesized missing
    leg never receives any summary's detail, even when a prior-attempt summary for
    the same invocation exists. Artifact-name text is never the selector, and more
    than one exact match is reported, never silently chosen.
  * Every ledger ``source_path`` and every copied file is constrained (with
    symlink resolution) to stay inside the accepted artifact root.
  * Each leg's report file is taken from its ``current-run.json`` manifest (not a
    hard-coded ``<component>/<PG>/report`` path), with any uploaded ``test-logs/``
    prefix stripped, the path validated to stay inside the artifact, and
    byte-identical duplicate copies collapsed so nothing is counted twice.
  * Historical and unexpected results are excluded from the current test-case
    totals and the per-target table; they are listed separately for audit, and a
    fail-closed (unresolved) result shows the reducer's own errors.

Reducer vocabulary (aligned with pep_cert_result.py):
  * execution_status in {completed, incomplete, infra_failure, preview}
  * test_verdict     in {pass, fail, not_run}
  * a missing leg is execution_status=infra_failure, test_verdict=not_run,
    reason_code=missing_result.
"""
import argparse
import dataclasses
import hashlib
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

# Shared with the regression report: JUnit parsing, the per-test-case detail
# page, and the overview's stylesheet and interaction script. Importing keeps ONE
# test-case rendering path and one look for both workflows.
from ci_consolidated_report import (  # noqa: E402
    _parse_junit_testcases,
    _esc,
    _render_css,
    _render_scripts,
    render_container_detail_page,
)

RESULT_SCHEMA = "cert-result/1"
DECISION_SCHEMA = "pep-cert-decision/1"
CONSOLIDATED_FILENAME = "consolidated-report.html"
_KNOWN_STATES = ("pass", "fail", "incomplete", "preview")


class ReportError(Exception):
    """A fatal input problem: the report cannot be built from these inputs."""


# --------------------------------------------------------------------------- #
# Input loading
# --------------------------------------------------------------------------- #
def _load_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ReportError("missing input: %s" % path) from e
    except (ValueError, OSError) as e:
        raise ReportError("unreadable JSON at %s: %s" % (path, e)) from e


def _as_dict(obj, what: str) -> dict:
    if not isinstance(obj, dict):
        raise ReportError("%s must be a JSON object, got %s" % (what, type(obj).__name__))
    return obj


# --------------------------------------------------------------------------- #
# Leg status vocabulary -- derived ONLY from the leg's authoritative fields.
# --------------------------------------------------------------------------- #
def leg_status(leg: dict) -> tuple:
    """Return (label, css_class) for one cert-result leg, using the reducer's
    real vocabulary. Never recomputed from JUnit; unknown values fall through to
    a visible UNKNOWN rather than being coerced to PASS."""
    leg = leg if isinstance(leg, dict) else {}
    es = leg.get("execution_status")
    tv = leg.get("test_verdict")
    if es == "infra_failure":
        return ("INFRA FAILURE", "infra")
    if es == "incomplete":
        return ("INCOMPLETE", "incomplete")
    if es == "preview":
        return ("PREVIEW", "preview")
    if tv == "not_run":
        return ("NOT RUN", "notrun")
    if tv == "pass":
        return ("PASS", "pass")
    if tv == "fail":
        return ("FAIL", "fail")
    return (str(tv or es or "unknown").upper(), "unknown")


def _auth_counts(leg: dict) -> dict:
    """Authoritative per-leg counts. A failed test case in JUnit is a <failure>
    OR an <error>, so the authoritative 'failed' is failures+errors and 'passed'
    subtracts both (the previous card math dropped errors)."""
    c = (leg or {}).get("counts") or {}
    tests = int(c.get("tests", 0) or 0)
    failed = int(c.get("failures", 0) or 0) + int(c.get("errors", 0) or 0)
    skipped = int(c.get("skipped", 0) or 0)
    return {"tests": tests, "failed": failed, "skipped": skipped,
            "passed": max(0, tests - failed - skipped)}


def _parsed_counts(records: list) -> dict:
    oc = Counter(r.outcome for r in records)
    return {"tests": len(records), "failed": oc["failed"],
            "skipped": oc["skipped"], "passed": oc["passed"]}


# --------------------------------------------------------------------------- #
# Leg -> accepted summary -> JUnit report mapping
# --------------------------------------------------------------------------- #
def _inside(root: Path, path: Path) -> bool:
    """True iff `path`, with symlinks resolved, stays within `root`."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _resolve_reports(root: Path, manifest_reports) -> tuple:
    """Resolve a leg manifest's report paths inside its accepted artifact.

    * Strips a leading ``test-logs/`` segment (the uploaded archive drops it).
    * Rejects anything that escapes ``root`` (path traversal / symlink escape).
    * Collapses byte-identical duplicates (the timestamped and conventional
      copies of the same XML) so a report is never parsed twice.

    Returns (report_paths, problems).
    """
    root = root.resolve()
    seen_digests = set()
    out, problems = [], []
    if not isinstance(manifest_reports, list):
        return [], ["manifest 'reports' is not a list"]
    for raw in manifest_reports:
        if not isinstance(raw, str) or not raw.strip():
            problems.append("blank/non-string report entry")
            continue
        rel = raw.strip()
        for prefix in ("test-logs/", "./"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
        candidate = (root / rel)
        if not _inside(root, candidate):
            problems.append("report path escapes artifact: %s" % raw)
            continue
        candidate = candidate.resolve()
        if not candidate.is_file():
            problems.append("report not found in artifact: %s" % raw)
            continue
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest in seen_digests:
            continue  # byte-identical duplicate copy -> count once
        seen_digests.add(digest)
        out.append(candidate)
    return out, problems


def build_summary_index(legs_dir: Path, ledger: dict) -> dict:
    """Map invocation_id -> [accepted candidate, ...], using the ledger as an
    allowlist only (``one_summary`` and not expired).

    Nothing is filtered by attempt here: one invocation legitimately has several
    accepted summaries across re-run attempts (current + prior). Each candidate
    keeps its summary's provenance so ``select_leg_evidence`` can pick exactly the
    one the reducer accepted for a leg. Candidate = {'artifact_name', 'root',
    'provenance', 'reports', 'report_problems'}.
    """
    legs_dir = Path(legs_dir).resolve()
    index = {}
    candidates = (ledger or {}).get("candidates")
    if not isinstance(candidates, list):
        return {}
    for cand in candidates:
        if not isinstance(cand, dict) or cand.get("expired") is True:
            continue
        if cand.get("extraction") != "one_summary":
            continue  # every other extraction is a collector-rejected anomaly
        source_path = cand.get("source_path")
        if not isinstance(source_path, str) or not source_path.endswith("summary.json"):
            continue
        summary_path = legs_dir / source_path
        # Constrain the ledger-supplied path to the allowlist root (symlink-safe).
        if not _inside(legs_dir, summary_path):
            continue
        summary_path = summary_path.resolve()
        if not summary_path.is_file():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(summary, dict):
            continue
        inv = summary.get("invocation_id")
        if not isinstance(inv, str) or not inv:
            continue
        prov = summary.get("provenance")
        root = summary_path.parent
        reports, problems = [], []
        manifest_path = root / "current-run.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                mr = manifest.get("reports") if isinstance(manifest, dict) else None
                reports, problems = _resolve_reports(root, mr)
            except (ValueError, OSError) as e:
                problems = ["current-run.json unreadable: %s" % e]
        else:
            problems = ["current-run.json manifest missing"]
        index.setdefault(inv, []).append({
            "artifact_name": cand.get("artifact_name", root.name), "root": root,
            "provenance": prov if isinstance(prov, dict) else None,
            "reports": reports, "report_problems": problems})
    return index


def select_leg_evidence(leg: dict, index: dict) -> tuple:
    """Pick the accepted summary artifact that produced THIS leg.

    Returns (candidate_or_None, state, problem_or_None) with state in
    {"none", "found", "no_summary", "ambiguous"}.

    * Only a ``matched`` leg can own evidence. A synthesized ``missing`` leg
      (provenance None) returns "none" even if a prior-attempt summary for the same
      invocation exists -- history is never attached to a current row.
    * The candidate must carry EXACTLY the leg's provenance: the reducer copies the
      accepted summary's provenance onto the leg verbatim, so equality selects that
      summary and excludes every other attempt without any attempt arithmetic.
    """
    if leg.get("reconciliation") != "matched":
        return None, "none", None
    prov = leg.get("provenance")
    if not isinstance(prov, dict) or not prov:
        return None, "no_summary", "matched test run carries no provenance to link its evidence"
    exact = [c for c in index.get(leg.get("invocation_id", ""), [])
             if c.get("provenance") == prov]
    if len(exact) == 1:
        return exact[0], "found", None
    if not exact:
        return None, "no_summary", ("the accepted summary for this matched test run was not "
                                    "found among the ledger-accepted artifacts")
    return None, "ambiguous", "more than one accepted artifact carries this test run's exact provenance"


# --------------------------------------------------------------------------- #
# Per-leg view assembly (writes detail pages; collects report issues)
# --------------------------------------------------------------------------- #
def _safe(text: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "-" for c in str(text))[:120]


def _package_name(pi: dict) -> str:
    return (pi.get("package") or {}).get("name") or pi.get("package_name") or "—"


def _write_detail(leg: dict, inv: str, records: list, reports: list,
                  out_dir: Path, details_dir: Path, legs_out: Path) -> str:
    pi = leg.get("planned_invocation", {}) or {}
    # A cert leg runs one container in isolation. Retag every record to the leg's
    # invocation id so the SHARED renderer (unchanged) keeps them all, and label
    # the heading "<platform> · <package>" with the invocation id in its tooltip:
    # two package invocations on the same platform/PG are never ambiguous.
    records = [dataclasses.replace(r, container=inv) for r in records]
    label = "%s · %s" % (pi.get("container_alias") or "—", _package_name(pi))
    # Deep-dive back-link: copy the leg's own pytest-html when it is present AND
    # stays (symlink-resolved) inside the accepted artifact.
    back = "../%s" % CONSOLIDATED_FILENAME
    root = reports[0].parent
    html_src = reports[0].with_suffix(".html")
    if html_src.is_file() and _inside(root, html_src):
        leg_dir = legs_out / _safe(inv)
        leg_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(html_src.resolve(), leg_dir / "report.html")
        back = "../legs/%s/report.html" % _safe(inv)
    detail_name = "cert-detail-%s.html" % _safe(inv)
    (details_dir / detail_name).write_text(
        render_container_detail_page(
            component=pi.get("component", "—"), pg=pi.get("pg_major", "—"),
            family=pi.get("family", "—"), arch=pi.get("arch", "—"),
            container=inv, records=records,
            back_link_href=back, consolidated_filename=CONSOLIDATED_FILENAME,
            container_alias=label,
        ), encoding="utf-8")
    return "details/%s" % detail_name


_EXC_PREFIX = re.compile(r"^(?:[A-Za-z_][\w.]*(?:Error|Exception)|Failed):\s*")


def _failure_clues(records: list, limit: int = 2, width: int = 160) -> tuple:
    """Up to `limit` (test name, trimmed message) pairs for failed cases, plus the
    total failed count. A pointer into the detail page, never a verdict."""
    failed = [r for r in records if r.outcome == "failed"]
    clues = []
    for r in failed[:limit]:
        text = r.message or ""
        if not text.strip():
            lines = [ln for ln in (r.body or "").splitlines() if ln.strip()]
            text = lines[-1] if lines else ""
        text = _EXC_PREFIX.sub("", " ".join(text.split()))
        if len(text) > width:
            text = text[:width - 1].rstrip() + "…"
        clues.append((r.name.split("[", 1)[0], text))
    return clues, len(failed)


def _build_leg_views(legs: list, index: dict,
                     out_dir: Path, details_dir: Path, legs_out: Path) -> list:
    """One view per current leg: authoritative status + issues + optional detail.

    Evidence comes only from ``select_leg_evidence`` (a matched leg's own accepted
    summary). A leg is flagged with a report issue when it CLAIMS test cases
    (authoritative tests > 0) but that evidence is missing, empty, partial or
    disagrees with the authoritative counts. The JSON verdict is always preserved;
    the issue only says the detail could not be trusted or attached.
    """
    views = []
    for leg in legs:
        inv = leg.get("invocation_id", "")
        claimed = _auth_counts(leg)["tests"]
        entry, state, problem = select_leg_evidence(leg, index)
        issues, detail_href, clues, n_failed = [], None, [], 0
        if problem:
            issues.append(problem)
        elif entry is not None:
            state = "none"
            problems = list(entry.get("report_problems") or [])
            reports = entry.get("reports") or []
            records, parse_problem = [], False
            for xml in reports:
                try:
                    records.extend(_parse_junit_testcases(xml))
                except Exception as e:  # malformed report must not abort
                    problems.append("unparseable report %s: %s" % (xml.name, e))
                    parse_problem = True
            # Per-case detail is only EXPECTED when the leg claims test cases: a
            # preview, not-run or no-tests leg without a report is not a report issue.
            # A report path escaping its artifact is surfaced regardless.
            issues.extend(p for p in problems
                          if claimed > 0 or p.startswith("report path escapes artifact"))
            if claimed > 0 and not reports:
                issues.append("no test-case report listed for this test run (claims %d)" % claimed)
                state = "no_detail"
            elif claimed > 0 and not records:
                issues.append("report parsed zero test cases (the test run claims %d)" % claimed)
                state = "no_detail"
            elif records:
                pc, ac = _parsed_counts(records), _auth_counts(leg)
                if (pc["tests"], pc["failed"], pc["skipped"]) != (
                        ac["tests"], ac["failed"], ac["skipped"]):
                    issues.append(
                        "test-case counts differ from authoritative test-run counts "
                        "(parsed tests/fail/skip=%d/%d/%d vs %d/%d/%d)" % (
                            pc["tests"], pc["failed"], pc["skipped"],
                            ac["tests"], ac["failed"], ac["skipped"]))
                if parse_problem:
                    issues.append("partial report set: some report(s) were unparseable")
                detail_href = _write_detail(leg, inv, records, reports,
                                            out_dir, details_dir, legs_out)
                clues, n_failed = _failure_clues(records)
                state = "ok"
        views.append({"leg": leg, "inv": inv, "detail_href": detail_href,
                      "issues": issues, "state": state,
                      "clues": clues, "n_failed": n_failed})
    return views


_ATTN_RANK = {"fail": 0, "infra": 0, "incomplete": 1, "notrun": 1,
              "unknown": 1, "preview": 3, "pass": 4}


def _view_sort_key(v: dict):
    """Failure-first ordering so OS/platform failures are easy to find: hard
    failures, then attention/issue rows, then preview, then pass; within a band
    by platform then PG."""
    _, cls = leg_status(v["leg"])
    rank = _ATTN_RANK.get(cls, 2)
    if v["issues"] and rank > 2:
        rank = 2  # a clean-verdict row with a report issue floats above pass
    pi = v["leg"].get("planned_invocation") or {}
    return (rank, str(pi.get("container_alias") or ""),
            str(pi.get("pg_major") or ""), v["inv"])


# --------------------------------------------------------------------------- #
# HTML rendering. The page wears the regression report's stylesheet and runs its
# interaction script unchanged (header, cards, heatmap grid, attention toggle,
# expandable groups). Only the overview model is certification's own: its unit is
# a test LEG whose colour is the reducer's verdict, never a JUnit failure rate.
# --------------------------------------------------------------------------- #

# One display category per leg. Execution status wins over test verdict (as in
# leg_status), so the categories never overlap and cards, family chips and matrix
# cells always add up to the number of legs.
_UNFINISHED = ("missing", "infra", "incomplete", "notrun", "unknown")
_CAT_WORD = {"missing": "missing", "infra": "infra failure", "incomplete": "incomplete",
             "notrun": "not run", "unknown": "unknown"}
_CELL_CLASS = {"pass": "ok", "fail": "bad", "missing": "issue", "infra": "issue",
               "incomplete": "issue", "notrun": "c-notrun", "preview": "c-preview",
               "unknown": "c-unknown"}
_CELL_LABEL = {"fail": "FAIL", "missing": "MISSING", "infra": "INFRA", "incomplete": "INCOMPLETE",
               "notrun": "NOT RUN", "preview": "PREVIEW"}
_WORST = {"fail": 0, "missing": 1, "infra": 1, "incomplete": 2, "unknown": 2, "notrun": 3,
          "preview": 4, "pass": 5}
_FAMILY_ORDER = {"rpm": 0, "deb": 1}          # display order only; any family is rendered
_VERDICT_COLOR = {"pass": "#10b981", "fail": "#dc2626", "incomplete": "#d97706",
                  "preview": "#2563eb"}
_POLICY_WORDS = {"report": "never blocks", "block": "blocks the workflow",
                 "allow": "allows the workflow"}
# Reader-facing names for the planner's gap scopes, and plain-language explanations of
# machine reason codes. The JSON keeps the codes; unlisted codes are shown humanized.
_GAP_SCOPE_NOUN = {"cell": ("build cell", "build cells"),
                   "target": ("package target", "package targets"),
                   "member": ("rejected package file", "rejected package files")}
_GAP_REASON_TEXT = {"no_enabled_platform": "No enabled PEP test container for this OS/arch"}


def leg_category(leg: dict) -> str:
    """The leg's single display category: leg_status's class, with a synthesized
    missing leg (infra_failure + missing_result) told apart from a real infra failure."""
    _, cls = leg_status(leg)
    if cls == "infra" and (leg.get("reason_code") == "missing_result"
                           or leg.get("reconciliation") == "missing"):
        return "missing"
    return cls


def _txt(value) -> str:
    """A display token from a JSON scalar; anything else (None, bool, list) is blank."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def _natural(text: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text)]


def _pg_key(pg: str):
    return (0, int(pg), "") if pg.isdigit() else (1, 0, pg)


def _pg_label(pg: str) -> str:
    return "PG%s" % pg if pg else "PG ?"


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _pi(view: dict) -> dict:
    pi = view["leg"].get("planned_invocation")
    return pi if isinstance(pi, dict) else {}


def _has_counts(leg: dict) -> bool:
    return isinstance(leg.get("counts"), dict)


def _annotate(views: list) -> None:
    """Give every view its category and a unique in-page anchor."""
    used = set()
    for v in views:
        base = "leg-" + (_safe(v["inv"]) or "unnamed")
        anchor, n = base, 2
        while anchor in used:
            anchor, n = "%s-%d" % (base, n), n + 1
        used.add(anchor)
        v["anchor"], v["cat"] = anchor, leg_category(v["leg"])


def _stats(views: list, n_gaps: int) -> dict:
    cats = Counter(v["cat"] for v in views)
    tc = [_auth_counts(v["leg"]) for v in views]
    return {"legs": len(views), "cats": cats, "gaps": n_gaps,
            "unfinished": sum(cats[c] for c in _UNFINISHED),
            "issues": sum(1 for v in views if v["issues"]),
            "tests": sum(c["tests"] for c in tc), "passed": sum(c["passed"] for c in tc),
            "failed": sum(c["failed"] for c in tc), "skipped": sum(c["skipped"] for c in tc)}


def _gap_breakdown(scopes: Counter) -> str:
    """'1 build cell · 2 rejected package files · 1 package target' from gap scope counts."""
    order = sorted(scopes, key=lambda s: (list(_GAP_SCOPE_NOUN).index(s) if s in _GAP_SCOPE_NOUN else 9, s))
    return " · ".join("%d %s" % (scopes[s], _GAP_SCOPE_NOUN.get(s, ("unscoped gap", "unscoped gaps"))[scopes[s] != 1])
                      for s in order if scopes[s])


def _breakdown(cats: Counter) -> str:
    return " · ".join("%d %s" % (cats[c], _CAT_WORD[c]) for c in _UNFINISHED if cats[c])


def _layout(views: list, gaps: list) -> dict:
    """Group legs and coverage gaps by package family.

    Matrix rows are platforms (plus the package when the report has more than one),
    columns are the PG majors the legs actually ran. A (row, PG) bucket keeps EVERY
    leg that falls into it; gaps stay separate rows because they carry no PG."""
    # A package that appears only in a coverage gap is still part of the release: count it,
    # so its gap is never shown as though it belonged to the one package that was tested.
    tested = {_package_name(_pi(v)) for v in views}
    gap_only = {_txt(g.get("physical_package")) for g in gaps} - {""} - tested
    packages = sorted(tested | gap_only)
    multi = len(packages) > 1
    pgs = sorted({_txt(_pi(v).get("pg_major")) for v in views}, key=_pg_key)
    fams = {}

    def family(key):
        return fams.setdefault(key, {"key": key, "label": key.upper() if key else "(no family)",
                                     "views": [], "gaps": [], "rows": {}})

    for v in views:
        pi = _pi(v)
        fam = family(_txt(pi.get("family")))
        fam["views"].append(v)
        platform = _txt(pi.get("container_alias")) or _txt(pi.get("source_cell_id")) or v["inv"]
        key = (platform, _package_name(pi) if multi else "")
        row = fam["rows"].setdefault(key, {"platform": platform, "package": key[1], "cells": {}})
        row["cells"].setdefault(_txt(pi.get("pg_major")), []).append(v)
    for i, g in enumerate(gaps):
        family(_txt(g.get("family")))["gaps"].append((i, g))

    out, slugs = [], set()
    for key in sorted(fams, key=lambda k: (_FAMILY_ORDER.get(k, len(_FAMILY_ORDER)), k)):
        fam = fams[key]
        base = "fam-" + ("".join(c if c.isalnum() else "-" for c in key.lower()) or "none")
        slug, n = base, 2
        while slug in slugs:
            slug, n = "%s-%d" % (base, n), n + 1
        slugs.add(slug)
        fam["slug"] = slug
        fam["rows"] = [fam["rows"][k] for k in sorted(fam["rows"], key=lambda k: (_natural(k[0]), k[1]))]
        fam["stats"] = _stats(fam["views"], len(fam["gaps"]))
        out.append(fam)
    return {"pgs": pgs, "multi": multi, "packages": packages, "untested_packages": sorted(gap_only),
            "families": out}


def _chips(st: dict, with_tests: bool = True) -> str:
    cats = st["cats"]
    chips = ['<span class="chip">%s</span>' % _plural(st["legs"], "test run")]
    if cats["pass"]:
        chips.append('<span class="chip ok">%d passed</span>' % cats["pass"])
    if cats["fail"]:
        chips.append('<span class="chip fail">%d failed</span>' % cats["fail"])
    if st["unfinished"]:
        chips.append('<span class="chip issue" title="%s">%d incomplete / not run</span>'
                     % (_esc(_breakdown(cats)), st["unfinished"]))
    if cats["preview"]:
        chips.append('<span class="chip preview">%d preview</span>' % cats["preview"])
    if st["gaps"]:
        chips.append('<span class="chip gap">%d not tested</span>' % st["gaps"])
    if st["issues"]:
        chips.append('<span class="chip issue">&#9888; %s</span>' % _plural(st["issues"], "report issue"))
    if with_tests and st["tests"]:
        chips.append('<span class="chip">%s</span>' % _plural(st["tests"], "test case")
                     + ('<span class="chip fail">%d failed cases</span>' % st["failed"]
                        if st["failed"] else ""))
    return "".join(chips)


def _css() -> str:
    """The regression report's stylesheet, unchanged, plus certification-only rules."""
    return "<style>%s%s</style>" % (_render_css(), """
      h2 { font-size:16px; margin:26px 0 8px; }
      .sub { color:#475569; font-size:13px; margin:0 0 10px; }
      .header .context a { color:#fff; }
      code { font-family:monospace; font-size:12px; }
      .verdict { display:flex; flex-wrap:wrap; align-items:center; gap:6px 16px; background:#fff; border-radius:8px; box-shadow:0 2px 4px rgba(0,0,0,.1); border-left:6px solid var(--vc); padding:12px 16px; margin:16px 0; }
      .verdict .vmain { display:flex; flex-direction:column; }
      .verdict .vlabel { font-size:11px; color:#667085; text-transform:uppercase; font-weight:700; letter-spacing:.04em; }
      .verdict .vstate { font-size:22px; font-weight:800; color:var(--vc); line-height:1.1; }
      .verdict .vsum { font-size:14px; color:#1e293b; flex:1 1 320px; }
      .verdict .vwf { border:1px solid #d0d5dd; background:#f8fafc; color:#344054; border-radius:999px; padding:4px 12px; font-size:12px; white-space:nowrap; }
      .verdict .vwhy { flex-basis:100%; font-size:13px; color:#475467; }
      .card .sub { font-size:12px; color:#667085; margin:4px 0 0; }
      .card.other .value { color:#b45309; } .card.preview .value { color:#2563eb; } .card.gap .value { color:#475467; }
      .tcline { font-size:13px; color:#475467; margin:-4px 0 4px; }
      .heat.cert { min-width:0; grid-template-columns:minmax(150px,230px) repeat(var(--cols), minmax(76px,1fr)); }
      .heat.cert .h { min-height:0; }
      .heat.cert .cell { overflow-wrap:anywhere; }
      .heat .grp { grid-column:1 / -1; display:flex; flex-wrap:wrap; gap:6px 8px; align-items:center; text-align:left; border:0; font:inherit; font-weight:700; background:#eef2f7; color:#2447a8; cursor:pointer; }
      .heat .grp:hover { background:#e2e8f5; }
      .heat .grp .chip, .heat .grp .chip.fail { font-weight:500; }
      .chip { border:1px solid #d9dee7; border-radius:999px; padding:1px 8px; color:#475467; font-size:12px; background:#fff; }
      .chip.fail { background:#fee4e2; color:#b42318; border-color:#fecdca; }
      .chip.issue { background:#fde68a; color:#7c2d12; border-color:#d97706; }
      .chip.ok { background:#ecfdf3; color:#067647; border-color:#abefc6; }
      .chip.preview { background:#dbeafe; color:#1e40af; border-color:#93c5fd; }
      .chip.gap { background:#f2f4f7; color:#344054; border-color:#98a2b3; border-style:dashed; }
      .heat .rowlbl { font-weight:600; font-family:monospace; }
      .heat .rowlbl span, .heat .cell span { display:block; font-weight:500; color:#667085; font-size:11px; font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
      .heat .gaplbl { color:#475467; font-weight:500; }
      .heat a.cell { color:inherit; text-decoration:none; }
      .heat a.cell:hover, .heat .cell.multi a:hover { outline:2px solid #2447a8; outline-offset:-2px; }
      .heat .bad { font-weight:700; color:#912018; }
      .heat .c-notrun { background:#eef2f6; color:#344054; box-shadow:inset 0 0 0 1px #98a2b3; }
      .heat .c-preview { background:#dbeafe; color:#1e40af; }
      .heat .c-unknown { background:#d0d5dd; color:#1d2939; }
      .heat .c-gap { background:repeating-linear-gradient(135deg,#f8fafc 0 7px,#eaecf0 7px 14px); color:#344054; text-align:left; }
      .heat .c-gap b { font-weight:700; }
      .heat .c-gap span { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      .heat .cell.multi a { display:block; color:inherit; text-decoration:none; padding:1px 0; }
      .legend { font-size:12px; color:#475467; margin:-8px 0 14px; display:flex; flex-wrap:wrap; gap:4px 14px; }
      .legend .sw { display:inline-block; width:12px; height:12px; border-radius:3px; vertical-align:-2px; margin-right:4px; border:1px solid #d0d5dd; }
      .sw.ok { background:#dff7e8; } .sw.bad { background:#ffd8d5; } .sw.issue { background:#fde68a; }
      .sw.c-notrun { background:#eef2f6; } .sw.c-preview { background:#dbeafe; } .sw.empty { background:#f8fafc; }
      .sw.c-gap { background:repeating-linear-gradient(135deg,#f8fafc 0 3px,#d0d5dd 3px 6px); }
      .pill { display:inline-block; padding:3px 9px; border-radius:12px; font-size:11px; font-weight:700; text-transform:uppercase; white-space:nowrap; text-decoration:none; }
      .st-pass { background:#d1fae5; color:#065f46; } .st-fail { background:#fee2e2; color:#991b1b; }
      .st-incomplete, .st-infra, .st-issue { background:#fde68a; color:#7c2d12; }
      .st-notrun { background:#eef2f6; color:#344054; box-shadow:inset 0 0 0 1px #98a2b3; }
      .st-preview { background:#dbeafe; color:#1e40af; } .st-unknown, .st-gap { background:#e4e7ec; color:#344054; }
      td .inv { font-size:11px; color:#98a2b3; margin-top:2px; }
      .rc { font-size:11px; color:#64748b; margin-top:3px; }
      .clue { font-size:11px; color:#7a2218; margin-top:3px; max-width:560px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      td.num { font-family:monospace; text-align:right; }
      td.muted { color:#667085; font-style:italic; }
      tr.gaprow td { background:#fafbfc; }
      tr.flash td { animation:certflash 1.8s ease-out; }
      @keyframes certflash { from { background:#fff3bf; } to { background:transparent; } }
      .unresolved { background:#fff7ed; border:1px solid #fdba74; border-radius:8px; padding:4px 16px 8px; margin:14px 0; }
      details.audit { margin:18px 0; background:#fff; border:1px solid #d9dee7; border-radius:8px; padding:10px 14px; }
      details.audit summary { cursor:pointer; font-size:14px; }
      details.audit[open] summary { margin-bottom:10px; }
      details.audit table { box-shadow:none; }
      .axes td:first-child { color:#475569; width:190px; }
      .scroll { overflow-x:auto; }
      @media (max-width:640px) {
        body { margin:10px; }
        .header { padding:16px; }
        .header h1 { font-size:22px; }
        .summary { grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }
        .card { padding:12px; } .card .value { font-size:24px; }
        .heat.cert { grid-template-columns:112px repeat(var(--cols), minmax(58px,1fr)); }
        .verdict .vsum { flex-basis:100%; }
        .verdict .vwf { white-space:normal; }
      }
    """)


def _cert_script() -> str:
    """Certification-only helper, kept out of the shared script: an in-page link
    opens the collapsed group holding its target (and lifts the attention filter
    if it hides it) before scrolling there."""
    return """<script>
function certReveal(id) {
  const el = document.getElementById(id);
  if (!el) return false;
  const d = el.closest('details');
  if (d) d.open = true;
  if (document.body.classList.contains('failures-only') &&
      (el.dataset.fail === '0' || (d && !d.hasAttribute('data-has-attention')))) {
    document.getElementById('failuresOnly').checked = false;
    toggleFailures();
  }
  el.scrollIntoView({block: 'center'});
  el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
  return true;
}
document.addEventListener('click', e => {
  const a = e.target.closest('a[href^="#"]');
  if (a && certReveal(decodeURIComponent(a.getAttribute('href').slice(1)))) {
    e.preventDefault();
    history.replaceState(null, '', a.getAttribute('href'));
  }
});
window.addEventListener('load', () => {
  if (location.hash) certReveal(decodeURIComponent(location.hash.slice(1)));
});
</script>"""


def _pill(label: str, cls: str) -> str:
    return '<span class="pill st-%s">%s</span>' % (_esc(cls), _esc(label))


def _valid_decision(decision) -> bool:
    """A decision is trustworthy only when it is the right schema AND carries a
    recognized certification_state. An invalid decision must never let the
    banner claim a trusted PASS."""
    return (isinstance(decision, dict)
            and decision.get("schema") == DECISION_SCHEMA
            and decision.get("certification_state") in _KNOWN_STATES)


def _summary_line(result: dict, st: dict) -> str:
    """One line of what happened, from the partitioned leg categories and the
    authoritative coverage axis. Empty when no result was given (fallback page)."""
    if not result:
        return ""
    if result.get("result_resolved") is False:
        return "Result <b>unresolved</b>: the reducer failed closed (see below)."
    n, cats, parts = st["legs"], st["cats"], []
    if cats["fail"]:
        parts.append("<b>%d of %d</b> test runs failed" % (cats["fail"], n))
    if st["unfinished"]:
        parts.append("<b>%d of %d</b> test runs incomplete or not run (%s)"
                     % (st["unfinished"], n, _esc(_breakdown(cats))))
    if cats["preview"]:
        parts.append("%d of %d test runs preview only" % (cats["preview"], n))
    if n and cats["pass"] == n:
        parts.append("all <b>%d</b> test runs passed" % n)
    if not n:
        parts.append("no test runs were executed")
    cov = result.get("coverage_status")
    cov_txt = "coverage <b>%s</b>" % _esc(_dash(cov))
    if st["gaps"]:
        cov_txt += ": %s with no test run (%s)" % (_plural(st["gaps"], "coverage gap"),
                                                  _esc(_gap_breakdown(st.get("gap_scopes") or Counter())))
    parts.append(cov_txt)
    if st["issues"]:
        parts.append("&#9888; %s" % _plural(st["issues"], "report issue"))
    return " &middot; ".join(parts)


def _consistent_decision(state: str, conclusion: str, mode: str, policy: str) -> bool:
    """True only for a combination pep_cert_gate actually emits: report = observe mode
    keeping a non-pass run green; block = failure for a non-pass state; allow = a
    successful pass. Anything else is contradictory and gets no explanation."""
    if mode not in ("observe", "gate"):
        return False
    if policy == "report":
        return mode == "observe" and conclusion == "success" and state != "pass"
    if policy == "block":
        return conclusion == "failure" and state != "pass"
    if policy == "allow":
        return conclusion == "success" and state == "pass"
    return False


def _banner(decision, result: dict = None, st: dict = None, trusted: bool = True) -> str:
    """Compact verdict bar: product certification state first, the workflow
    conclusion beside it as a separate, neutral pill, so an observe-mode green
    run can never be read as a certification pass.

    The state is coloured only when it can be trusted: a valid decision whose fields
    agree with each other, on a page built from a readable result. The fallback page
    (``trusted=False``) and a contradictory decision show the recorded state in
    neutral grey, and only a genuine observe/report decision is explained as such."""
    st = st or _stats([], 0)
    line = _summary_line(result or {}, st)
    if not _valid_decision(decision):
        # Untrusted: an explicit unknown state, never green, never PASS.
        return ('<div class="verdict" data-state="unknown" style="--vc:#64748b">'
                '<div class="vmain"><span class="vlabel">Certification</span>'
                '<span class="vstate">UNKNOWN</span></div>'
                '<div class="vsum">%s</div>'
                '<div class="vwhy">The certification decision is missing or invalid; the JSON '
                'evidence in this artifact is authoritative.</div></div>') % line
    state = decision["certification_state"]
    conclusion = _txt(decision.get("workflow_conclusion")) or "unknown"
    mode = _txt(decision.get("requested_mode")) or "unknown"
    policy = _txt(decision.get("policy_decision")) or "unknown"
    reason = _txt(decision.get("reason_code"))
    consistent = _consistent_decision(state, conclusion, mode, policy)
    if not trusted:
        label, color = "Recorded decision (not verified)", "#64748b"
        why = ("This page could not read the certification result, so it cannot show or "
               "confirm test runs, coverage or gaps. The state above is copied from "
               "cert-decision.json as written; check the JSON evidence before relying on it.")
    elif not consistent:
        label, color = "Certification", "#64748b"
        why = ("The decision fields do not agree with each other (state <b>%s</b>, workflow "
               "<b>%s</b>, mode <b>%s</b>, policy <b>%s</b>), so this page does not explain the "
               "workflow result. See cert-decision.json."
               % (_esc(state), _esc(conclusion), _esc(mode), _esc(policy)))
    else:
        label, color = "Certification", _VERDICT_COLOR.get(state, "#64748b")
        why = ""
        if policy == "report":
            why = ("The workflow is green because <b>observe</b> mode only reports; the product "
                   "certification state is <b>%s</b>. The certification JSON is authoritative."
                   % _esc(state.upper()))
    gloss = (" &mdash; %s" % _esc(_POLICY_WORDS[policy])) if consistent and trusted else ""
    return (
        '<div class="verdict" data-state="%s" data-trusted="%s" style="--vc:%s">'
        '<div class="vmain"><span class="vlabel">%s</span>'
        '<span class="vstate">%s</span></div>'
        '<div class="vsum">%s%s</div>'
        '<span class="vwf" title="workflow_conclusion / requested_mode / policy_decision">'
        'Workflow <b>%s</b> &middot; %s mode &middot; policy %s%s</span>%s</div>'
    ) % (_esc(state), "true" if trusted and consistent else "false", color, _esc(label),
         _esc(state.upper()), line,
         ("%sreason <code>%s</code>" % (" &middot; " if line else "", _esc(reason))) if reason else "",
         _esc(conclusion), _esc(mode), _esc(policy), gloss,
         ('<div class="vwhy">%s</div>' % why) if why else "")


def _short_sha(sha) -> str:
    sha = _txt(sha)
    return '<span title="%s">%s</span>' % (_esc(sha), _esc(sha[:12])) if sha else "—"


def _header(result: dict, decision, layout: dict, st: dict) -> str:
    rel = result.get("release") if isinstance(result.get("release"), dict) else {}
    prov = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
    ac = result.get("attempt_context") if isinstance(result.get("attempt_context"), dict) else {}
    repo, run_id = _txt(prov.get("repository")), _txt(prov.get("run_id"))
    run = "workflow run %s" % _esc(run_id or "—")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) and run_id.isdigit():
        run = '<a href="https://github.com/%s/actions/runs/%s">%s</a>' % (repo, run_id, run)
    views = [v for f in layout["families"] for v in f["views"]]
    arches = sorted({_txt(_pi(v).get("arch")) for v in views} - {""})
    platforms = {r["platform"] for f in layout["families"] for r in f["rows"]}
    mode = decision.get("requested_mode") if isinstance(decision, dict) else None
    untested = set(layout["untested_packages"])
    pkgs = ", ".join(_esc(p) + (" <b>(not tested)</b>" if p in untested else "")
                     for p in layout["packages"]) or "—"
    return (
        '<div class="header"><h1>PEP Certification Report</h1>'
        '<div class="context">component <b>%s</b> &middot; version <b>%s</b> (build %s) '
        '&middot; tag %s &middot; channel <b>%s</b> &middot; %s %s</div>'
        '<div class="context">%s (plan attempt %s, aggregation attempt %s) &middot; %s '
        '&middot; ref %s &middot; sha %s &middot; PEP %s</div>'
        '<div class="context" style="margin-top:8px"><strong>Tested:</strong> PG %s &middot; '
        'families %s &middot; arches %s &middot; %s &middot; %s &middot; mode %s</div></div>'
    ) % (_esc(_dash(rel.get("logical_component"))), _esc(_dash(rel.get("intended_version"))),
         _esc(_dash(rel.get("intended_buildnum"))), _esc(_dash(rel.get("effective_tag"))),
         _esc(_dash(rel.get("channel"))), "packages" if len(layout["packages"]) > 1 else "package", pkgs,
         run, _esc(_dash(ac.get("plan_run_attempt", prov.get("run_attempt")))),
         _esc(_dash(ac.get("aggregation_run_attempt"))), _esc(repo or "—"),
         _esc(_dash(prov.get("ref"))), _short_sha(prov.get("sha")), _short_sha(prov.get("pep_resolved_sha")),
         _esc(", ".join(p for p in layout["pgs"] if p) or "—"),
         _esc(", ".join(f["key"] for f in layout["families"] if f["views"] and f["key"]) or "—"),
         _esc(", ".join(arches) or "—"), _plural(len(platforms), "platform"),
         _plural(st["legs"], "test run"), _esc(_dash(mode)))


def _cards(st: dict) -> str:
    """Leg cards partition the legs (passed + failed + incomplete/not run + preview
    == legs). Coverage gaps are planned targets with NO leg, so they are counted
    beside the legs, never inside them; report issues overlay legs of any category."""
    cats, n = st["cats"], st["legs"]
    gap_scopes = st.get("gap_scopes") or Counter()
    items = [("total", "Test runs", n, "planned test runs"),
             ("passed", "Passed", cats["pass"], "of %d test runs" % n),
             ("failed", "Failed", cats["fail"], "of %d test runs" % n),
             ("other", "Incomplete / not run", st["unfinished"], _breakdown(cats) or "missing, infra, incomplete, not run")]
    if cats["preview"]:
        items.append(("preview", "Preview", cats["preview"], "not a certification"))
    items.append(("gap", "Coverage gaps", st["gaps"], _gap_breakdown(gap_scopes) or "none"))
    items.append(("issues", "Report issues", st["issues"], "test runs with unusable detail"))
    cards = "".join('<div class="card %s"><h3>%s</h3><div class="value">%d</div><div class="sub">%s</div></div>'
                    % (cls, _esc(title), value, _esc(sub)) for cls, title, value, sub in items)
    tcline = ('<div class="tcline">Test cases (supporting detail, from each test run\'s authoritative counts): '
              '<b>%d</b> total &middot; <b>%d</b> passed &middot; <b>%d</b> failed &middot; '
              '<b>%d</b> skipped</div>' % (st["tests"], st["passed"], st["failed"], st["skipped"]))
    return '<div class="summary">%s</div>%s' % (cards, tcline)


def _attention_banners(st: dict) -> str:
    out = []
    if st["cats"]["missing"]:
        out.append('<div class="banner banner-issue"><strong>&#9888;</strong> %s produced no result '
                   '&mdash; shown as MISSING below, never as a pass.</div>'
                   % _plural(st["cats"]["missing"], "planned test run"))
    if st["issues"]:
        out.append('<div class="banner banner-issue"><strong>&#9888;</strong> %s %s a report issue '
                   '&mdash; see <a href="#issues">report issues</a>. Their verdicts stay authoritative.</div>'
                   % (_plural(st["issues"], "test run"), "has" if st["issues"] == 1 else "have"))
    return "".join(out)


def _leg_href(v: dict) -> str:
    return v["detail_href"] or "#" + v["anchor"]


def _leg_tip(v: dict, where: str) -> str:
    leg, ac = v["leg"], _auth_counts(v["leg"])
    label, _ = leg_status(leg)
    bits = ["%s — %s" % (where, label)]
    if _has_counts(leg):
        bits.append("%d failed of %d test cases" % (ac["failed"], ac["tests"]))
    if _txt(leg.get("reason_code")):
        bits.append(leg["reason_code"])
    bits.append(v["inv"])
    if v["issues"]:
        bits.append("report issue: " + "; ".join(v["issues"]))
    bits.append("opens the test-case detail" if v["detail_href"] else "no test-case detail; opens its row")
    return " · ".join(bits)


def _cell_text(v: dict, prefix: str = "") -> str:
    leg, cat = v["leg"], v["cat"]
    ac = _auth_counts(leg)
    frac = "%d/%d" % (ac["failed"], ac["tests"]) if _has_counts(leg) else ""
    if cat == "pass":
        main, sub = frac or "PASS", ""
    elif cat in ("fail", "notrun"):
        main, sub = (frac, _CELL_LABEL[cat]) if frac else (_CELL_LABEL[cat], "")
    else:
        main = _CELL_LABEL.get(cat) or leg_status(leg)[0]
        sub = frac if frac and ac["tests"] else ""
    return "%s%s%s%s" % (_esc(prefix), "&#9888; " if v["issues"] else "", _esc(main),
                         ("<span>%s</span>" % _esc(sub)) if sub else "")


def _matrix_cell(vs: list, where: str) -> str:
    if not vs:
        return '<div class="h cell empty" title="%s">&mdash;</div>' % _esc(where + " — no test run planned")
    if len(vs) == 1:
        v = vs[0]
        cls = "h cell %s%s" % (_CELL_CLASS.get(v["cat"], "c-unknown"), " issue-marker" if v["issues"] else "")
        return '<a class="%s" href="%s" title="%s">%s</a>' % (
            cls, _esc(_leg_href(v)), _esc(_leg_tip(v, where)), _cell_text(v))
    # Several legs share this bucket: show each one with its own link; the cell
    # takes the worst colour but never stands in for a single leg.
    worst = min(vs, key=lambda v: _WORST.get(v["cat"], 2))["cat"]
    cls = "h cell multi %s%s" % (_CELL_CLASS.get(worst, "c-unknown"),
                                 " issue-marker" if any(v["issues"] for v in vs) else "")
    links = "".join('<a href="%s" title="%s">%s</a>' % (
        _esc(_leg_href(v)), _esc(_leg_tip(v, where)),
        _cell_text(v, prefix=v["inv"].rsplit("-", 1)[-1][:8] + ": ")) for v in vs)
    return '<div class="%s" title="%s">%s</div>' % (cls, _esc("%s — %d test runs" % (where, len(vs))), links)


def _row_total(row: dict) -> str:
    legs = [v["leg"] for vs in row["cells"].values() for v in vs if _has_counts(v["leg"])]
    if not legs:
        return '<div class="h total">&mdash;</div>'
    failed = sum(_auth_counts(l)["failed"] for l in legs)
    tests = sum(_auth_counts(l)["tests"] for l in legs)
    return '<div class="h total%s">%d/%d</div>' % (" failtotal" if failed else "", failed, tests)


def _gap_label(g: dict) -> str:
    return " · ".join(x for x in (_txt(g.get("os")), _txt(g.get("arch"))) if x) or _dash(_txt(g.get("cell_id")))


def _gap_reason(g: dict) -> str:
    code = _txt(g.get("reason"))
    return _GAP_REASON_TEXT.get(code) or (code or "unknown reason").replace("_", " ")


def _gap_words(g: dict) -> str:
    """'package-target gap: No enabled PEP test container for this OS/arch'."""
    scope = _txt(g.get("scope"))
    noun = _GAP_SCOPE_NOUN.get(scope, ("%s" % scope if scope else "unscoped",))[0]
    return "%s gap: %s" % (noun.replace(" ", "-"), _gap_reason(g))


def _gap_tip(g: dict) -> str:
    keys = ("scope", "cell_id", "target_id", "physical_package", "family", "os", "arch", "reason", "detail")
    return " · ".join("%s=%s" % (k, _txt(g.get(k))) for k in keys if _txt(g.get(k)))


def _matrix(layout: dict) -> str:
    fams, pgs = layout["families"], layout["pgs"]
    if not fams:
        return ""
    cols = len(pgs) + 1 if pgs else 1
    per_pg = Counter(_txt(_pi(v).get("pg_major")) for f in fams for v in f["views"])
    parts = ['<div class="heat-wrap"><div class="heat cert" style="--cols:%d">' % cols,
             '<div class="h head sticky-left">Platform</div>']
    for pg in pgs:
        parts.append('<div class="h head">%s<span>%s</span></div>' % (_esc(_pg_label(pg)), _plural(per_pg[pg], "test run")))
    parts.append('<div class="h head">Total<span>failed / test cases</span></div>' if pgs
                 else '<div class="h head">Result</div>')
    for f in fams:
        parts.append('<button type="button" class="h grp" onclick="openComponent(%s)" '
                     'title="Open the %s section">%s %s</button>' % (
                         _esc(json.dumps(f["slug"])), _esc(f["label"]), _esc(f["label"]),
                         _chips(f["stats"], with_tests=False)))
        for row in f["rows"]:
            cells = [v for vs in row["cells"].values() for v in vs]
            tip = "%s · %s" % (row["platform"], ", ".join(sorted({
                _txt(_pi(v).get("source_cell_id")) or "—" for v in cells})))
            parts.append('<div class="h rowlbl sticky-left" title="%s">%s%s</div>' % (
                _esc(tip), _esc(row["platform"]),
                ("<span>%s</span>" % _esc(row["package"])) if row["package"] else ""))
            for pg in pgs:
                where = "%s %s%s" % (row["platform"], _pg_label(pg),
                                     (" · " + row["package"]) if row["package"] else "")
                parts.append(_matrix_cell(row["cells"].get(pg, []), where))
            parts.append(_row_total(row))
        for i, g in f["gaps"]:
            pkg = _txt(g.get("physical_package"))
            parts.append('<div class="h rowlbl sticky-left gaplbl" title="%s">%s<span>not tested%s</span></div>'
                         % (_esc(_gap_tip(g)), _esc(_gap_label(g)),
                            _esc(" · " + pkg) if layout["multi"] and pkg else ""))
            parts.append('<a class="h cell c-gap" style="grid-column:span %d" href="#gap-%d" title="%s">'
                         '<b>NOT TESTED</b> &middot; %s<span>%s &middot; no test run on any PG</span></a>'
                         % (cols, i, _esc(_gap_tip(g)), _esc(_gap_words(g)), _esc(_dash(g.get("detail")))))
    parts.append("</div></div>")
    parts.append(
        '<div class="legend"><span>Colour = the test run&#39;s certification verdict; numbers are '
        'failed/total test cases.</span><span><i class="sw ok"></i>pass</span>'
        '<span><i class="sw bad"></i>fail</span><span><i class="sw issue"></i>missing / infra / incomplete</span>'
        '<span><i class="sw c-notrun"></i>not run</span><span><i class="sw c-preview"></i>preview</span>'
        '<span><i class="sw c-gap"></i>not tested: a coverage gap (build cell, package target or '
        'rejected file) with no test run on any PG; it is not a PG result</span>'
        '<span><i class="sw empty"></i>&mdash; no test run planned</span><span>&#9888; report issue</span></div>')
    return "".join(parts)


def _controls() -> str:
    # Same element id and functions as the regression report's controls, with labels
    # for certification groups.
    return """<div class="controls">
  <label><input type="checkbox" id="failuresOnly" onclick="toggleFailures()"> Show attention rows only (failures, incomplete, missing, not tested &amp; report issues)</label>
  <button type="button" onclick="openFailing()">Open groups needing attention</button>
  <button type="button" onclick="expandAll()">Expand all</button>
  <button type="button" onclick="collapseAll()">Collapse all</button>
</div>"""


_CELL_STATE = {"ambiguous": ("AMBIGUOUS", "issue"), "no_summary": ("NO SUMMARY", "issue"),
               "no_detail": ("NO DETAIL", "issue")}


def _leg_row(v: dict, multi: bool) -> str:
    leg, pi = v["leg"], _pi(v)
    label, cls = leg_status(leg)
    rc = leg.get("reason_code") or (leg.get("reason") if cls != "pass" else None)
    status = _pill(label, cls)
    if cls != "pass" and rc:
        status += '<div class="rc">%s</div>' % _esc(rc)
    # Concise failing-case clues from this leg's own report (detail, not verdict).
    for name, msg in v.get("clues") or []:
        text = name + ((" — " + msg) if msg else "")
        status += '<div class="clue" title="%s"><code>%s</code>%s</div>' % (
            _esc(text), _esc(name), (" &mdash; " + _esc(msg)) if msg else "")
    more = v.get("n_failed", 0) - len(v.get("clues") or [])
    if more > 0:
        status += '<div class="clue">+%d more failing case(s)</div>' % more
    if _has_counts(leg):
        ac = _auth_counts(leg)
        nums = "".join('<td class="num">%d</td>' % ac[k] for k in ("tests", "passed", "failed", "skipped"))
    else:
        nums = '<td class="num">&mdash;</td>' * 4
    if v["detail_href"]:
        detail = '<a class="report-link" href="%s">View &rarr;</a>' % _esc(v["detail_href"])
    elif v["state"] in _CELL_STATE:
        detail = _pill(*_CELL_STATE[v["state"]])
    else:
        detail = "&mdash;"
    if v["issues"]:
        detail += (' <a class="pill st-issue" href="#issue-%s" title="%s">&#9888; issue</a>'
                   % (_esc(v["anchor"][4:]), _esc("; ".join(v["issues"]))))
    attention = cls not in ("pass", "preview") or bool(v["issues"])
    platform = _txt(pi.get("container_alias")) or _txt(pi.get("source_cell_id")) or v["inv"]
    return (
        '<tr id="%s" data-fail="%s" data-platform="%s" data-pg="%s" data-arch="%s" '
        'data-package="%s" data-status="%s">'
        '<td class="mono"><b>%s</b><div class="inv"><code>%s</code></div></td>'
        '<td>%s</td><td>%s</td>%s<td>%s</td>%s<td>%s</td></tr>'
    ) % (_esc(v["anchor"]), "1" if attention else "0", _esc(platform), _esc(_txt(pi.get("pg_major"))),
         _esc(_txt(pi.get("arch"))), _esc(_package_name(pi)), _esc(v["cat"]),
         _esc(platform), _esc(v["inv"]), _esc(_dash(_txt(pi.get("pg_major")))),
         _esc(_dash(_txt(pi.get("arch")))),
         ('<td class="mono">%s</td>' % _esc(_package_name(pi))) if multi else "",
         status, nums, detail)


def _gap_row(i: int, g: dict, multi: bool) -> str:
    return (
        '<tr class="gaprow" id="gaprow-%d" data-fail="1"><td class="mono"><b>%s</b>'
        '<div class="inv"><code>%s</code></div></td>'
        '<td title="coverage gaps are not PG-specific">&mdash;</td><td>%s</td>%s'
        '<td>%s<div class="rc">%s</div></td>'
        '<td class="muted" colspan="4">no test run on any PG</td>'
        '<td><a class="report-link" href="#gap-%d">Gap detail &darr;</a></td></tr>'
    ) % (i, _esc(_gap_label(g)), _esc(_dash(_txt(g.get("cell_id")))), _esc(_dash(_txt(g.get("arch")))),
         ('<td class="mono">%s</td>' % _esc(_dash(_txt(g.get("physical_package"))))) if multi else "",
         _pill("NOT TESTED", "gap"), _esc(_gap_words(g)), i)


def _section(fam: dict, multi: bool) -> str:
    """One r66-style expandable group per package family. It opens by default when
    a leg needs action (failed, unfinished or with a report issue); a group whose
    only problem is coverage gaps stays closed, since the matrix shows every gap."""
    st = fam["stats"]
    needs_action = st["cats"]["fail"] or st["unfinished"] or st["issues"]
    attention = needs_action or st["gaps"]
    head = ('<thead><tr><th><button onclick="sortGroup(this,\'platform\')">Platform</button></th>'
            '<th><button onclick="sortGroup(this,\'pg\')">PG</button></th>'
            '<th><button onclick="sortGroup(this,\'arch\')">Arch</button></th>%s'
            '<th>Status</th><th>Test cases</th><th>Passed</th><th>Failed</th><th>Skipped</th>'
            '<th>Detail</th></tr></thead>') % (
        '<th><button onclick="sortGroup(this,\'package\')">Package</button></th>' if multi else "")
    legs = "".join(_leg_row(v, multi) for v in sorted(fam["views"], key=_view_sort_key))
    gaps = "".join(_gap_row(i, g, multi) for i, g in fam["gaps"])
    return (
        '<details class="component" id="comp-%s"%s%s><summary><strong>%s</strong>%s</summary>'
        '<div class="table-wrap"><table>%s<tbody>%s</tbody>%s</table></div></details>'
    ) % (_esc(fam["slug"]), " open" if needs_action else "",
         ' data-has-attention="1"' if attention else "", _esc(fam["label"]), _chips(st),
         head, legs, ('<tbody class="gaps">%s</tbody>' % gaps) if gaps else "")


def _axes_table(result: dict, decision: dict) -> str:
    axes = (decision or {}).get("axes") if isinstance(decision, dict) else {}
    axes = axes if isinstance(axes, dict) else {}
    dec = decision if isinstance(decision, dict) else {}
    rows = [
        ("result_resolved", result.get("result_resolved")),
        ("execution_status", axes.get("execution_status", result.get("execution_status"))),
        ("test_verdict", axes.get("test_verdict", result.get("test_verdict"))),
        ("coverage_status", axes.get("coverage_status", result.get("coverage_status"))),
        ("certification_state", dec.get("certification_state")),
        ("policy_decision", dec.get("policy_decision")),
        ("reason_code", dec.get("reason_code")),
        ("workflow_conclusion", dec.get("workflow_conclusion")),
        ("requested_mode", dec.get("requested_mode")),
    ]

    def _fmt(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        return "—" if v is None else v
    body = "".join("<tr><td>%s</td><td><code>%s</code></td></tr>"
                   % (_esc(k), _esc(_fmt(v))) for k, v in rows)
    # Collapsed normally; open when the result or decision cannot be trusted.
    opened = result.get("result_resolved") is not True or not _valid_decision(decision)
    return ('<details class="audit" id="axes"%s><summary><b>Certification axes and policy</b> '
            '&mdash; the authoritative JSON values behind the verdict</summary>'
            '<table class="axes">%s</table></details>' % (" open" if opened else "", body))


def _dash(value) -> str:
    """Display value for an optional gap field: a blank or absent value is an em dash, never 'None'."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "—"
    return str(value)


def _gaps_table(gaps: list) -> str:
    if not gaps:
        return ""
    header = ("<tr><th>Scope</th><th>Cell</th><th>Package</th><th>Family</th><th>OS</th>"
              "<th>Arch</th><th>Reason</th><th>Detail</th></tr>")
    rows = "".join(
        '<tr id="gap-%d"><td>%s</td><td><code>%s</code></td><td><code>%s</code></td><td>%s</td><td>%s</td>'
        "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
            i, _esc(_dash(g.get("scope"))), _esc(_dash(g.get("cell_id"))),
            _esc(_dash(g.get("physical_package"))), _esc(_dash(g.get("family"))),
            _esc(_dash(g.get("os"))), _esc(_dash(g.get("arch"))),
            _pill(_dash(g.get("reason")).upper().replace("_", " "), "gap")
            + ('<div class="rc">%s</div>' % _esc(_GAP_REASON_TEXT[_txt(g.get("reason"))])
               if _txt(g.get("reason")) in _GAP_REASON_TEXT else ""),
            _esc(_dash(g.get("detail"))))
        for i, g in enumerate(gaps) if isinstance(g, dict))
    return ('<h2 id="gaps">Not tested (planned but not certified)</h2>'
            '<p class="sub">Each row is a planned build cell, package target or rejected package '
            'that produced no certification result, so coverage cannot be complete; the '
            '<code>coverage_status</code> axis is authoritative. Source/debug packages '
            'and packages the component does not ship as runtime are excluded by policy and are '
            'not listed.</p><div class="scroll"><table>%s%s</table></div>' % (header, rows))


def _issues_table(views: list) -> str:
    rows = [(v, "; ".join(v["issues"])) for v in views if v["issues"]]
    if not rows:
        return ""
    body = "".join('<tr id="issue-%s"><td><code>%s</code></td><td>%s</td></tr>'
                   % (_esc(v["anchor"][4:]), _esc(v["inv"]), _esc(m)) for v, m in rows)
    return ('<h2 id="issues">Report issues (detail evidence unavailable or inconsistent)</h2>'
            '<p class="sub">These test runs keep their authoritative verdict above; only their '
            'per-test-case detail could not be attached or did not agree with the counts.</p>'
            '<div class="scroll"><table><tr><th>Test run</th><th>Problem</th></tr>%s</table></div>' % body)


def _as_list_of_dicts(value) -> list:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _unresolved_block(result: dict) -> str:
    """A fail-closed (unresolved) result has no legs; show the reducer's own
    actionable errors so the reviewer sees WHY, not an empty table."""
    if result.get("result_resolved") is not False:
        return ""
    errs = [e for e in (result.get("errors") or []) if isinstance(e, str)] \
        if isinstance(result.get("errors"), list) else []
    items = "".join("<li><code>%s</code></li>" % _esc(e) for e in errs) or "<li>(no errors listed)</li>"
    return ('<div class="unresolved"><h2>Result unresolved &mdash; certification failed closed</h2>'
            '<p class="sub">The reducer could not build trustworthy test runs from the collected '
            'evidence (reason_code <code>%s</code>). Its errors:</p><ul>%s</ul></div>'
            % (_esc(result.get("reason_code") or "—"), items))


def _audit_section(result: dict) -> str:
    """Records that never enter current legs or totals, listed for audit: rejected
    (unexpected) summaries and prior-attempt (historical) results."""
    parts = []
    unexpected = _as_list_of_dicts(result.get("unexpected_results"))
    if unexpected:
        rows = []
        for u in unexpected:
            ev = u.get("evidence") if isinstance(u.get("evidence"), dict) else {}
            rec = ev.get("record") if isinstance(ev.get("record"), dict) else {}
            prov = rec.get("provenance") if isinstance(rec.get("provenance"), dict) else {}
            rows.append("<tr><td>%s</td><td><code>%s</code></td><td class=num>%s</td><td>%s</td></tr>" % (
                _pill(str(u.get("kind", "unknown")).replace("_", " ").upper(), "issue"),
                _esc(u.get("invocation_id") or "—"), _esc(prov.get("caller_run_attempt", "—")),
                _esc(ev.get("reason", "—"))))
        parts.append('<h2>Unexpected result records (%d)</h2><p class="sub">Collected summaries '
                     'the reducer rejected; they never count as a test run.</p><div class="scroll"><table><tr><th>Kind</th>'
                     '<th>Test run</th><th>Producing attempt</th><th>Reason</th></tr>%s</table></div>'
                     % (len(unexpected), "".join(rows)))
    hist = _as_list_of_dicts(result.get("historical_results"))
    if hist:
        ac = result.get("attempt_context") if isinstance(result.get("attempt_context"), dict) else {}
        rows = "".join("<tr><td><code>%s</code></td><td class=num>%s</td><td>%s</td><td>%s</td></tr>" % (
            _esc(h.get("invocation_id", "—")), _esc(h.get("producing_attempt", "—")),
            _esc(h.get("execution_status", "—")), _esc(h.get("test_verdict", "—"))) for h in hist)
        parts.append('<details class="audit"><summary><b>Prior-attempt results (%d)</b> &mdash; '
                     'retained for audit, never shown as current detail or counted in totals '
                     '(aggregation attempt %s)</summary><div class="scroll"><table><tr><th>Test run</th>'
                     '<th>Producing attempt</th><th>Execution</th><th>Verdict</th></tr>%s</table></div>'
                     '</details>' % (len(hist), _esc(ac.get("aggregation_run_attempt", "—")), rows))
    return "".join(parts)


_HEAD = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>'
         '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
         '<title>PEP Certification Report</title>%s</head><body>\n')
_FOOTER = ('<div class="footer">Generated from cert-result/1 + pep-cert-decision/1. '
           'The JSON evidence in this artifact is authoritative for status and policy.</div>')


def render_report(result: dict, decision, index: dict, out_dir: Path) -> dict:
    """Write consolidated-report.html + details/ + legs/ into out_dir."""
    out_dir = Path(out_dir)
    result = result if isinstance(result, dict) else {}
    details_dir, legs_out = out_dir / "details", out_dir / "legs"
    for d in (details_dir, legs_out):
        d.mkdir(parents=True, exist_ok=True)
    for stale in details_dir.glob("cert-detail-*.html"):
        stale.unlink()

    legs = _as_list_of_dicts(result.get("legs"))
    views = _build_leg_views(legs, index, out_dir, details_dir, legs_out)
    views.sort(key=_view_sort_key)
    _annotate(views)

    gaps = _as_list_of_dicts(result.get("coverage_gaps"))
    layout = _layout(views, gaps)
    st = _stats(views, len(gaps))
    st["gap_scopes"] = Counter(_txt(g.get("scope")) or "unscoped" for g in gaps)

    doc = (_HEAD % _css()
           + _header(result, decision, layout, st) + "\n"
           + _banner(decision, result, st) + "\n"
           + _unresolved_block(result)
           + _cards(st) + "\n"
           + _attention_banners(st)
           + _matrix(layout) + "\n"
           + (_controls() if layout["families"] else "") + "\n"
           + "\n".join(_section(f, layout["multi"]) for f in layout["families"]) + "\n"
           + _gaps_table(gaps) + _issues_table(views)
           + _axes_table(result, decision) + _audit_section(result)
           + _FOOTER + "\n" + _render_scripts() + "\n" + _cert_script()
           + "\n</body></html>")
    (out_dir / CONSOLIDATED_FILENAME).write_text(doc, encoding="utf-8")
    return {"legs": len(legs), "detail_pages": sum(1 for v in views if v["detail_href"]),
            "coverage_gaps": len(gaps), "report_issues": st["issues"]}


def _fallback(out_dir: Path, message: str, decision=None) -> None:
    """Truthful fallback page. It NEVER shows a trusted pass: the incomplete-report
    warning comes first, and a valid decision's state is copied in neutral grey as a
    recorded, unverified value (UNKNOWN for an invalid decision)."""
    doc = (_HEAD % _css()
           + '<div class="header"><h1>PEP Certification Report</h1></div>'
           + '<div class="banner banner-issue"><strong>&#9888; Human report incomplete.</strong> '
             'The certification result could not be read, so no test runs, coverage or gaps are shown '
             'here and nothing on this page is a verified certification result. See the report '
             'generation issue below.</div>'
           + _banner(decision, trusted=False)
           + '<h2>Report generation issue</h2><p class="sub">%s</p>' % _esc(message)
           + '<div class="footer">JSON evidence is authoritative.</div></body></html>')
    try:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / CONSOLIDATED_FILENAME).write_text(doc, encoding="utf-8")
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Render the PEP certification human report.")
    ap.add_argument("--result", required=True, help="cert-result.json")
    ap.add_argument("--decision", required=True, help="cert-decision.json")
    ap.add_argument("--ledger", required=True, help="collection-ledger.json")
    ap.add_argument("--legs", required=True, help="directory of downloaded pep-summary artifacts")
    ap.add_argument("--out", required=True, help="output directory (the pep-certification artifact dir)")
    args = ap.parse_args(argv)
    out_dir = Path(args.out)

    # Load the decision first and defensively; even a total failure below must
    # still show the authoritative state (or UNKNOWN), never a false pass.
    decision = None
    try:
        decision = _load_json(args.decision)
    except ReportError as e:
        print("[cert-report] WARNING: %s" % e, file=sys.stderr)

    try:
        result = _as_dict(_load_json(args.result), "cert-result")
        ledger = _as_dict(_load_json(args.ledger), "collection-ledger")
    except ReportError as e:
        print("[cert-report] WARNING: %s; writing fallback report" % e, file=sys.stderr)
        _fallback(out_dir, str(e), decision)
        return 0  # report != gate: never fail the artifact upload

    try:
        index = build_summary_index(Path(args.legs), ledger)
        stats = render_report(result, decision, index, out_dir)
    except Exception as e:  # pragma: no cover - defensive last resort
        print("[cert-report] WARNING: render failed: %s; writing fallback" % e, file=sys.stderr)
        _fallback(out_dir, "render failed: %s" % e, decision)
        return 0
    print("[cert-report] legs=%(legs)s detail_pages=%(detail_pages)s "
          "coverage_gaps=%(coverage_gaps)s report_issues=%(report_issues)s -> %(out)s"
          % dict(stats, out=out_dir / CONSOLIDATED_FILENAME))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

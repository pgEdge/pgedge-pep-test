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
    experience. Certification supplies its own OVERVIEW because its identity
    (OS/arch/PG/package/invocation) and decision model differ from the
    regression component heatmap.
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

# Shared with the regression report: JUnit parsing + the per-test-case detail
# page. Importing keeps ONE test-case rendering path for both workflows.
from ci_consolidated_report import (  # noqa: E402
    _parse_junit_testcases,
    _esc,
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
        return None, "no_summary", "matched leg carries no provenance to link its evidence"
    exact = [c for c in index.get(leg.get("invocation_id", ""), [])
             if c.get("provenance") == prov]
    if len(exact) == 1:
        return exact[0], "found", None
    if not exact:
        return None, "no_summary", ("the accepted summary for this matched leg was not "
                                    "found among the ledger-accepted artifacts")
    return None, "ambiguous", "more than one accepted artifact carries this leg's exact provenance"


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
                issues.append("no test-case report listed for this leg (claims %d)" % claimed)
                state = "no_detail"
            elif claimed > 0 and not records:
                issues.append("report parsed zero test cases (leg claims %d)" % claimed)
                state = "no_detail"
            elif records:
                pc, ac = _parsed_counts(records), _auth_counts(leg)
                if (pc["tests"], pc["failed"], pc["skipped"]) != (
                        ac["tests"], ac["failed"], ac["skipped"]):
                    issues.append(
                        "test-case counts differ from authoritative leg counts "
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
# HTML rendering (certification-specific overview)
# --------------------------------------------------------------------------- #
_STATUS_COLORS = {
    "pass": "#10b981", "fail": "#ef4444", "incomplete": "#f59e0b",
    "infra": "#a855f7", "notrun": "#94a3b8", "preview": "#3b82f6",
    "unknown": "#64748b", "gap": "#64748b", "issue": "#ef4444",
}


def _overview_style() -> str:
    swatches = "".join(".st-%s{background:%s;color:#fff;}" % (k, v)
                       for k, v in _STATUS_COLORS.items())
    return (
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
        "margin:20px;background:#f5f5f5;color:#1e293b;}"
        "h1{font-size:24px;margin:0 0 4px;}h2{font-size:16px;margin:24px 0 8px;}"
        ".sub{color:#475569;font-size:13px;margin-bottom:12px;}"
        ".banner{border-radius:10px;padding:16px 20px;color:#fff;margin:14px 0;}"
        ".banner .big{font-size:18px;font-weight:700;}"
        ".banner .row{font-size:13px;margin-top:6px;opacity:.97;}"
        ".cards{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0;}"
        ".card{background:#fff;border:1px solid #d9dee7;border-radius:8px;padding:10px 14px;min-width:92px;"
        "box-shadow:0 1px 3px rgba(0,0,0,.06);}"
        ".card h3{margin:0;font-size:11px;text-transform:uppercase;color:#64748b;font-weight:600;}"
        ".card .v{font-size:22px;font-weight:700;margin-top:2px;}"
        "table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d9dee7;border-radius:8px;overflow:hidden;}"
        "th,td{padding:8px 10px;text-align:left;font-size:13px;border-bottom:1px solid #eef1f5;vertical-align:top;}"
        "th{background:#f8fafc;font-size:11px;text-transform:uppercase;color:#475569;}"
        "td.num{font-family:monospace;text-align:right;}"
        "tr.attn td{background:#fff7f7;}"
        "code{font-family:monospace;font-size:12px;}"
        ".rc{font-size:11px;color:#64748b;margin-top:3px;}"
        ".clue{font-size:11px;color:#7a2218;margin-top:3px;max-width:560px;}"
        ".unresolved{background:#fff7ed;border:1px solid #fdba74;border-radius:8px;"
        "padding:4px 16px 8px;margin:14px 0;}"
        "details.audit{margin:18px 0;}details.audit summary{cursor:pointer;font-size:14px;margin-bottom:8px;}"
        ".pill{padding:3px 9px;border-radius:12px;font-size:11px;font-weight:700;text-transform:uppercase;}"
        + swatches +
        "a{color:#4f46e5;text-decoration:none;}a:hover{text-decoration:underline;}"
        ".axes td:first-child{color:#475569;width:190px;}"
        ".footer{margin-top:20px;text-align:center;color:#666;font-size:12px;}"
        "</style>"
    )


def _pill(label: str, cls: str) -> str:
    return '<span class="pill st-%s">%s</span>' % (_esc(cls), _esc(label))


def _valid_decision(decision) -> bool:
    """A decision is trustworthy only when it is the right schema AND carries a
    recognized certification_state. An invalid decision must never let the
    banner claim a trusted PASS."""
    return (isinstance(decision, dict)
            and decision.get("schema") == DECISION_SCHEMA
            and decision.get("certification_state") in _KNOWN_STATES)


def _banner(decision) -> str:
    if not _valid_decision(decision):
        # Untrusted: show an explicit unknown state, never green, never PASS.
        return ('<div class="banner" style="background:#64748b">'
                '<div class="big">Certification state: UNKNOWN</div>'
                '<div class="row">The certification decision is missing or invalid; '
                'the JSON evidence in this artifact is authoritative.</div></div>')
    state = decision["certification_state"]
    conclusion = decision.get("workflow_conclusion", "unknown")
    mode = decision.get("requested_mode", "unknown")
    policy = decision.get("policy_decision", "unknown")
    reason = decision.get("reason_code")
    color = "#10b981" if state == "pass" else ("#ef4444" if state == "fail" else "#f59e0b")
    note = ""
    if state != "pass" and conclusion == "success":
        note = ("Workflow conclusion is <b>success</b> under <b>%s</b> (report-only) "
                "policy, but the product certification state is <b>%s</b>. The verdict "
                "below is authoritative." % (_esc(mode), _esc(state)))
    return (
        '<div class="banner" style="background:%s">'
        '<div class="big">Certification state: %s</div>'
        '<div class="row">workflow conclusion: <b>%s</b> &middot; policy: <b>%s</b> '
        '&middot; requested mode: <b>%s</b>%s</div>%s</div>'
    ) % (color, _esc(str(state).upper()), _esc(conclusion), _esc(policy), _esc(mode),
         (" &middot; reason: <b>%s</b>" % _esc(reason)) if reason else "",
         ('<div class="row">%s</div>' % note) if note else "")


def _cards(result: dict, n_gaps: int, n_issues: int) -> str:
    counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
    legs = _as_list_of_dicts(result.get("legs"))
    tc = [_auth_counts(l) for l in legs]
    tc_tests = sum(c["tests"] for c in tc)
    tc_failed = sum(c["failed"] for c in tc)   # failures + errors
    tc_skipped = sum(c["skipped"] for c in tc)
    tc_passed = sum(c["passed"] for c in tc)   # tests - failed - skipped, per leg
    items = [
        ("Legs", len(legs), "#1e293b"),
        ("Pass", counts.get("pass", 0), _STATUS_COLORS["pass"]),
        ("Fail", counts.get("fail", 0), _STATUS_COLORS["fail"]),
        ("Incomplete", counts.get("incomplete", 0), _STATUS_COLORS["incomplete"]),
        ("Infra fail", counts.get("infra_failure", 0), _STATUS_COLORS["infra"]),
        ("Not run", counts.get("not_run", 0), _STATUS_COLORS["notrun"]),
        ("Preview", counts.get("preview", 0), _STATUS_COLORS["preview"]),
        ("Coverage gaps", n_gaps, _STATUS_COLORS["gap"]),
        ("Report issues", n_issues, _STATUS_COLORS["issue"] if n_issues else "#1e293b"),
        ("Test cases", tc_tests, "#1e293b"),
        ("TC passed", tc_passed, _STATUS_COLORS["pass"]),
        ("TC failed", tc_failed, _STATUS_COLORS["fail"]),
        ("TC skipped", tc_skipped, _STATUS_COLORS["incomplete"]),
    ]
    cards = "".join(
        '<div class="card"><h3>%s</h3><div class="v" style="color:%s">%s</div></div>'
        % (_esc(label), color, _esc(value)) for label, value, color in items)
    return '<div class="cards">%s</div>' % cards


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
    return '<h2>Certification axes (authoritative)</h2><table class="axes">%s</table>' % body


_CELL_STATE = {"ambiguous": ("AMBIGUOUS", "issue"), "no_summary": ("NO SUMMARY", "issue"),
               "no_detail": ("NO DETAIL", "issue")}


def _target_table(views: list) -> str:
    header = ("<tr><th>Package</th><th>PG</th><th>Family</th><th>Arch</th>"
              "<th>Platform</th><th>Invocation</th><th>Status</th>"
              "<th>Tests</th><th>Pass</th><th>Fail</th><th>Skip</th><th>Detail</th></tr>")
    rows = []
    for v in views:
        leg = v["leg"]
        pi = leg.get("planned_invocation", {}) or {}
        inv = v["inv"]
        pkg = _package_name(pi)
        ac = _auth_counts(leg)
        label, cls = leg_status(leg)
        # Reason / reason_code for failing or incomplete legs, when present.
        rc = leg.get("reason_code") or (leg.get("reason") if cls != "pass" else None)
        status_cell = _pill(label, cls)
        if cls != "pass" and rc:
            status_cell += '<div class="rc">%s</div>' % _esc(rc)
        # Concise failing-case clues from this leg's own report (detail, not verdict).
        for name, msg in v.get("clues") or []:
            status_cell += '<div class="clue"><code>%s</code>%s</div>' % (
                _esc(name), (" &mdash; " + _esc(msg)) if msg else "")
        more = v.get("n_failed", 0) - len(v.get("clues") or [])
        if more > 0:
            status_cell += '<div class="clue">+%d more failing case(s)</div>' % more
        if v["detail_href"]:
            detail_cell = '<a href="%s">view &rarr;</a>' % _esc(v["detail_href"])
            if v["issues"]:
                detail_cell += (' <span class="pill st-issue" title="%s">&#9888;</span>'
                                % _esc("; ".join(v["issues"])))
        elif v["state"] in _CELL_STATE:
            lbl, c = _CELL_STATE[v["state"]]
            detail_cell = _pill(lbl, c)
        else:
            detail_cell = "—"
        attn = "" if cls in ("pass", "preview") and not v["issues"] else ' class="attn"'
        rows.append(
            "<tr%s><td><code>%s</code></td><td class=num>%s</td><td>%s</td><td>%s</td>"
            "<td><code>%s</code></td><td><code>%s</code></td><td>%s</td>"
            "<td class=num>%s</td><td class=num>%s</td><td class=num>%s</td>"
            "<td class=num>%s</td><td>%s</td></tr>" % (
                attn, _esc(pkg), _esc(pi.get("pg_major", "—")), _esc(pi.get("family", "—")),
                _esc(pi.get("arch", "—")), _esc(pi.get("container_alias", "—")),
                _esc(inv), status_cell, _esc(ac["tests"]), _esc(ac["passed"]),
                _esc(ac["failed"]), _esc(ac["skipped"]), detail_cell))
    return '<h2>Tested targets (failures first)</h2><table>%s%s</table>' % (header, "".join(rows))


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
        "<tr><td>%s</td><td><code>%s</code></td><td><code>%s</code></td><td>%s</td><td>%s</td>"
        "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
            _esc(_dash(g.get("scope"))), _esc(_dash(g.get("cell_id"))),
            _esc(_dash(g.get("physical_package"))), _esc(_dash(g.get("family"))),
            _esc(_dash(g.get("os"))), _esc(_dash(g.get("arch"))),
            _pill(_dash(g.get("reason")).upper().replace("_", " "), "gap"),
            _esc(_dash(g.get("detail"))))
        for g in gaps if isinstance(g, dict))
    return ('<h2>Not tested (planned but not certified)</h2>'
            '<p class="sub">Each row is a planned build cell, package target or rejected package '
            'that produced no certification result, so coverage cannot be complete; the '
            '<code>coverage_status</code> axis above is authoritative. Source/debug packages '
            'and packages the component does not ship as runtime are excluded by policy and are '
            'not listed.</p><table>%s%s</table>' % (header, rows))


def _issues_table(views: list) -> str:
    rows = [(v["inv"], "; ".join(v["issues"])) for v in views if v["issues"]]
    if not rows:
        return ""
    body = "".join("<tr><td><code>%s</code></td><td>%s</td></tr>" % (_esc(i), _esc(m))
                   for i, m in rows)
    return ('<h2>Report issues (detail evidence unavailable or inconsistent)</h2>'
            '<p class="sub">These legs keep their authoritative verdict above; only their '
            'per-test-case detail could not be attached or did not agree with the counts.</p>'
            '<table><tr><th>Invocation</th><th>Problem</th></tr>%s</table>' % body)


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
            '<p class="sub">The reducer could not build trustworthy legs from the collected '
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
                     'the reducer rejected; they never fill a leg.</p><table><tr><th>Kind</th>'
                     '<th>Invocation</th><th>Producing attempt</th><th>Reason</th></tr>%s</table>'
                     % (len(unexpected), "".join(rows)))
    hist = _as_list_of_dicts(result.get("historical_results"))
    if hist:
        ac = result.get("attempt_context") if isinstance(result.get("attempt_context"), dict) else {}
        rows = "".join("<tr><td><code>%s</code></td><td class=num>%s</td><td>%s</td><td>%s</td></tr>" % (
            _esc(h.get("invocation_id", "—")), _esc(h.get("producing_attempt", "—")),
            _esc(h.get("execution_status", "—")), _esc(h.get("test_verdict", "—"))) for h in hist)
        parts.append('<details class="audit"><summary><b>Prior-attempt results (%d)</b> &mdash; '
                     'retained for audit, never shown as current detail or counted in totals '
                     '(aggregation attempt %s)</summary><table><tr><th>Invocation</th>'
                     '<th>Producing attempt</th><th>Execution</th><th>Verdict</th></tr>%s</table>'
                     '</details>' % (len(hist), _esc(ac.get("aggregation_run_attempt", "—")), rows))
    return "".join(parts)


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

    gaps = _as_list_of_dicts(result.get("coverage_gaps"))
    n_issues = sum(1 for v in views if v["issues"])
    rel = result.get("release", {}) or {}
    prov = result.get("provenance", {}) or {}
    ac = result.get("attempt_context") if isinstance(result.get("attempt_context"), dict) else {}
    sub = ("component <b>%s</b> &middot; version <b>%s</b> (build %s) &middot; "
           "channel <b>%s</b> &middot; run %s (plan attempt %s, aggregation attempt %s)") % (
        _esc(rel.get("logical_component", "—")), _esc(rel.get("intended_version", "—")),
        _esc(rel.get("intended_buildnum", "—")), _esc(rel.get("channel", "—")),
        _esc(prov.get("run_id", "—")), _esc(ac.get("plan_run_attempt", prov.get("run_attempt", "—"))),
        _esc(ac.get("aggregation_run_attempt", "—")))

    doc = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>'
        '<title>PEP Certification Report</title>%s</head><body>'
        '<h1>PEP Certification Report</h1><div class="sub">%s</div>'
        '%s%s%s%s%s%s%s%s'
        '<div class="footer">Generated from cert-result/1 + pep-cert-decision/1. '
        'The JSON evidence in this artifact is authoritative for status and policy.</div>'
        '</body></html>'
    ) % (_overview_style(), sub, _banner(decision), _unresolved_block(result),
         _cards(result, len(gaps), n_issues), _axes_table(result, decision),
         _target_table(views), _gaps_table(gaps), _issues_table(views), _audit_section(result))
    (out_dir / CONSOLIDATED_FILENAME).write_text(doc, encoding="utf-8")
    return {"legs": len(legs), "detail_pages": sum(1 for v in views if v["detail_href"]),
            "coverage_gaps": len(gaps), "report_issues": n_issues}


def _fallback(out_dir: Path, message: str, decision=None) -> None:
    """Truthful fallback page. It NEVER claims a pass: `_banner` shows the
    authoritative state only for a valid decision, and UNKNOWN otherwise."""
    doc = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>'
           '<title>PEP Certification Report</title>%s</head><body>'
           '<h1>PEP Certification Report</h1>%s'
           '<h2>Report generation issue</h2><p class="sub">%s</p>'
           '<div class="footer">JSON evidence is authoritative.</div></body></html>') % (
        _overview_style(), _banner(decision), _esc(message))
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

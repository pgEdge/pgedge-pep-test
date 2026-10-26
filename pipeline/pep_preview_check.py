#!/usr/bin/env python3
"""Final assertion of the PEP Certification Preview workflow
(.github/workflows/pep-certification-preview.yml).

That workflow builds one tiny RPM and one tiny DEB of the internal fixture component, records
their receipts, and calls the real coordinator (pep-certify.yml) at the revision under test as a
SIMULATED release in preview + observe mode. This script checks what that run really produced:
the coordinator's job result and public outputs, the run's artifact listing, and the documents
downloaded by immutable id (combined evidence, capture evidence, cert-plan). It passes only when
they all describe one genuine preview of this run:
  * the coordinator succeeded with a preview decision (never a pass) and a positive evidence id;
  * both receipts were accepted, the release is simulated and both publications skipped, so
    every target is strictly ineligible (nothing is claimed published) yet preview-eligible;
  * exactly six unique preview runs (both fixture targets on PG 16, 17 and 18), with no planning
    gap, missing result, unexpected or historical summary, no product test and no installed-package
    proof claimed;
  * every document names this repository, run, attempt and the revision under test;
  * cert-result, cert-decision (re-derived with pep_cert_gate), collection ledger, artifact
    listing and the consolidated HTML report agree with each other.
It never reads an artifact by name pattern alone: names only select this attempt's documents,
which are then downloaded by id and cross-checked.

The fixture expectation (cells, PG majors, six previews) is the check's own pinned expectation,
not certification policy: PEP's catalogs at the revision under test decide what the coordinator
plans, and a catalog change that alters the fixture's expansion fails here on purpose.

Subcommands (exit 0 when every check passes, 1 with every failure listed, 2 for misuse):
  coordinator  the coordinator job result and outputs (environment), before any download;
  select       this run's artifact listing -> the ids of this attempt's capture evidence and
               cert-plan, appended to --github-output;
  verify       the downloaded documents, cross-checked with each other and with the run.
Expectations come from the environment: EXPECT_REPOSITORY, EXPECT_RUN_ID, EXPECT_RUN_NUMBER,
EXPECT_RUN_ATTEMPT, EXPECT_SHA, EXPECT_REF, EXPECT_PEP_SHA, plus the CERTIFY_RESULT and CERT_*
coordinator values. Stdlib only, plus pep_cert_gate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import pep_cert_gate as G     # noqa: E402  the decision is re-derived, never trusted as written

# --- the preview fixture (pep-certification-preview.yml) ----------------------------------
COMPONENT = "pgedge-capture-smoke"
PACKAGE = "pgedge-capture-smoke"
FIXTURE_CELLS = ("certpreview-deb-bookworm-amd64", "certpreview-rpm-el9-amd64")
EXPECTED_PG_MAJORS = ("16", "17", "18")
EXPECTED_PREVIEWS = len(FIXTURE_CELLS) * len(EXPECTED_PG_MAJORS)
MODE = "observe"

SUMMARY_PREFIX = "pep-summary-"
CAPTURE_ERROR_PREFIX = "pep-capture-error-"
REPORT_FILE = "consolidated-report.html"
# Nothing persisted may carry a URL or credential material.
_LEAK_MARKERS = ("http://", "https://", "bearer ", "authorization", "x-amz", "sig=", "token=")
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_VERDICT_RE = re.compile(r'<div class="verdict" data-state="([^"]*)" data-trusted="([^"]*)"')
_LEG_ROW_RE = re.compile(r'<tr id="leg-([^"]*)" data-fail="([^"]*)" data-platform="[^"]*" '
                         r'data-pg="([^"]*)" data-arch="[^"]*" data-package="[^"]*" data-status="([^"]*)">')
# Rendered elements (not stylesheet class names) that mean gaps, issues or a fallback page.
_REPORT_FORBIDDEN = (('id="gaps"', "a coverage-gaps section"), ('<tr class="gaprow"', "a coverage-gap row"),
                     ('id="issues"', "a report-issues section"),
                     ('class="banner banner-issue"', "the report's incomplete/fallback banner"))

EXPECT_KEYS = ("EXPECT_REPOSITORY", "EXPECT_RUN_ID", "EXPECT_RUN_NUMBER", "EXPECT_RUN_ATTEMPT",
               "EXPECT_SHA", "EXPECT_REF", "EXPECT_PEP_SHA")


class UsageError(Exception):
    """The check cannot run (missing expectation or unreadable input): never a pass."""


def _is_pos_int_str(v):
    return isinstance(v, str) and re.fullmatch(r"[1-9][0-9]*", v) is not None


def _is_pos_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def expectations(env):
    """The run identity every document must carry (strings, as GitHub provides them)."""
    missing = [k for k in EXPECT_KEYS if not env.get(k)]
    if missing:
        raise UsageError("missing expectation(s): %s" % ", ".join(missing))
    exp = {k: env[k] for k in EXPECT_KEYS}
    if not _FULL_SHA_RE.fullmatch(exp["EXPECT_PEP_SHA"]):
        raise UsageError("EXPECT_PEP_SHA is not a full lowercase commit SHA: %r" % exp["EXPECT_PEP_SHA"])
    return exp


def evidence_name(exp):
    return "pep-certification-r%s-a%s" % (exp["EXPECT_RUN_NUMBER"], exp["EXPECT_RUN_ATTEMPT"])


# --------------------------------------------------------------------------- coordinator
def check_coordinator(env, exp):
    """Failures in the coordinator job result and its public outputs."""
    f = []
    if env.get("CERTIFY_RESULT") != "success":
        f.append("coordinator job result is %r, not success" % env.get("CERTIFY_RESULT"))
    want = {"CERT_CONCLUSION": "success", "CERT_STATE": "preview", "CERT_EXECUTION": "preview",
            "CERT_TESTS": "not_run", "CERT_COVERAGE": "partial", "CERT_REASON": "preview"}
    for key, value in want.items():
        if env.get(key) != value:
            f.append("coordinator output %s is %r, expected %r" % (key, env.get(key), value))
    if not _is_pos_int_str(env.get("CERT_EVIDENCE_ID")):
        f.append("evidence artifact id is not a positive integer: %r" % env.get("CERT_EVIDENCE_ID"))
    if env.get("CERT_EVIDENCE_NAME") != evidence_name(exp):
        f.append("evidence artifact name is %r, expected %r" % (env.get("CERT_EVIDENCE_NAME"), evidence_name(exp)))
    return f


# --------------------------------------------------------------------------- listing
def _listing_problems(listing):
    if not isinstance(listing, list) or not all(isinstance(a, dict) for a in listing):
        return ["the artifact listing is not a list of objects"]
    f = []
    ids = [a.get("id") for a in listing]
    names = [a.get("name") for a in listing]
    if not all(_is_pos_int(i) for i in ids):
        f.append("the artifact listing has an invalid artifact id")
    if len(set(i for i in ids if _is_pos_int(i))) != len(ids):
        f.append("the artifact listing repeats an artifact id")
    if not all(isinstance(n, str) and n for n in names):
        f.append("the artifact listing has a blank artifact name")
    elif len(set(names)) != len(names):
        f.append("the artifact listing repeats an artifact name")
    return f


def _one_named(listing, name, f):
    """The id of the single non-expired artifact called ``name``, else None (with a failure)."""
    hits = [a for a in listing if a.get("name") == name]
    if len(hits) != 1:
        f.append("expected exactly one artifact named %s, found %d" % (name, len(hits)))
        return None
    if hits[0].get("expired") is not False:
        f.append("artifact %s is expired or has no expired flag" % name)
        return None
    return hits[0].get("id") if _is_pos_int(hits[0].get("id")) else None


def select_capture_ids(listing, exp):
    """(ids, failures): this attempt's capture evidence and cert-plan artifact ids."""
    f = _listing_problems(listing)
    if f:
        return {}, f
    suffix = "r%s-a%s" % (exp["EXPECT_RUN_NUMBER"], exp["EXPECT_RUN_ATTEMPT"])
    ids = {"capture_evidence_id": _one_named(listing, "pep-capture-evidence-" + suffix, f),
           "cert_plan_id": _one_named(listing, "pep-cert-plan-" + suffix, f)}
    return ({k: v for k, v in ids.items() if v is not None} if not f else {}), f


# --------------------------------------------------------------------------- documents
def _provenance(label, prov, exp, f, keys=("repository", "run_id", "run_attempt", "sha", "ref")):
    if not isinstance(prov, dict):
        f.append("%s has no provenance object" % label)
        return
    want = {"repository": exp["EXPECT_REPOSITORY"], "run_id": exp["EXPECT_RUN_ID"],
            "run_attempt": exp["EXPECT_RUN_ATTEMPT"], "sha": exp["EXPECT_SHA"], "ref": exp["EXPECT_REF"],
            "pep_requested_ref": exp["EXPECT_PEP_SHA"], "pep_resolved_sha": exp["EXPECT_PEP_SHA"],
            "pep_implementation_ref": exp["EXPECT_PEP_SHA"]}
    for k in keys:
        if prov.get(k) != want[k]:
            f.append("%s provenance.%s is %r, expected %r" % (label, k, prov.get(k), want[k]))


def _check_capture_evidence(ev, exp, f):
    if not isinstance(ev, dict) or ev.get("schema") != "capture-evidence/1":
        f.append("capture evidence is not a capture-evidence/1 document")
        return
    counts = ev.get("counts") if isinstance(ev.get("counts"), dict) else {}
    want = {"planned_cells": len(FIXTURE_CELLS), "accepted_receipt_cells": len(FIXTURE_CELLS),
            "rejected_receipt_cells": 0, "ambiguous_receipt_cells": 0, "absent_receipt_cells": 0}
    for k, v in want.items():
        if counts.get(k) != v:
            f.append("capture evidence counts.%s is %r, expected %r" % (k, counts.get(k), v))
    cells = ev.get("cells") if isinstance(ev.get("cells"), list) else []
    if sorted(str(c.get("cell_id")) for c in cells if isinstance(c, dict)) != sorted(FIXTURE_CELLS):
        f.append("capture evidence cells are not exactly the two fixture cells")
    for c in cells:
        if isinstance(c, dict) and c.get("verdict") != "accepted":
            f.append("receipt for cell %r was not accepted (%r)" % (c.get("cell_id"), c.get("verdict")))
    _provenance("capture evidence", ev.get("provenance"), exp, f,
                ("repository", "run_id", "run_attempt", "sha", "ref", "pep_implementation_ref", "pep_resolved_sha"))


def _check_cert_plan(cp, exp, f):
    """The simulated release, as capture recorded it. Returns {target_id: package sha256}."""
    if not isinstance(cp, dict) or cp.get("schema") != "cert-plan/1":
        f.append("cert-plan is not a cert-plan/1 document")
        return {}
    if cp.get("plan_resolved") is not True or cp.get("errors") != []:
        f.append("cert-plan is not resolved without errors (plan_resolved=%r, errors=%r)"
                 % (cp.get("plan_resolved"), cp.get("errors")))
    if cp.get("execution_mode") != "preview":
        f.append("cert-plan execution_mode is %r, expected 'preview'" % cp.get("execution_mode"))
    intent = cp.get("release_intent") if isinstance(cp.get("release_intent"), dict) else {}
    if intent.get("simulated") is not True:
        f.append("cert-plan release_intent.simulated is %r: the fixture must be a simulated release"
                 % intent.get("simulated"))
    if intent.get("logical_component") != COMPONENT:
        f.append("cert-plan logical_component is %r, expected %r" % (intent.get("logical_component"), COMPONENT))
    cov = cp.get("coverage_denominators") if isinstance(cp.get("coverage_denominators"), dict) else {}
    want = {"planned_build_cells": len(FIXTURE_CELLS), "available_build_cells": len(FIXTURE_CELLS),
            "selected_targets": len(FIXTURE_CELLS), "eligible_targets": 0,
            "preview_eligible_targets": len(FIXTURE_CELLS)}
    for k, v in want.items():
        if cov.get(k) != v:
            f.append("cert-plan coverage_denominators.%s is %r, expected %r" % (k, cov.get(k), v))
    cells = cp.get("cells") if isinstance(cp.get("cells"), list) else []
    if sorted(str(c.get("cell_id")) for c in cells if isinstance(c, dict)) != sorted(FIXTURE_CELLS):
        f.append("cert-plan cells are not exactly the two fixture cells")
    targets = {}
    for c in cells:
        if not isinstance(c, dict):
            continue
        cid = c.get("cell_id")
        for k, v in (("build_state", "available"), ("publication_state", "publish_skipped"),
                     ("publication_reason", "simulated")):
            if c.get(k) != v:
                f.append("cert-plan cell %r %s is %r, expected %r" % (cid, k, c.get(k), v))
        ts = c.get("targets") if isinstance(c.get("targets"), list) else []
        if len(ts) != 1 or not isinstance(ts[0], dict):
            f.append("cert-plan cell %r has %d targets, expected 1" % (cid, len(ts)))
            continue
        t = ts[0]
        if t.get("physical_package") != PACKAGE:
            f.append("cert-plan target %r package is %r, expected %r" % (t.get("target_id"), t.get("physical_package"), PACKAGE))
        if t.get("eligibility") != "ineligible":
            f.append("cert-plan target %r is strictly %r: an unpublished fixture must never be eligible"
                     % (t.get("target_id"), t.get("eligibility")))
        if t.get("preview_eligibility") != "eligible":
            f.append("cert-plan target %r preview_eligibility is %r, expected 'eligible'"
                     % (t.get("target_id"), t.get("preview_eligibility")))
        pkg = t.get("package") if isinstance(t.get("package"), dict) else {}
        targets[t.get("target_id")] = pkg.get("sha256")
    _provenance("cert-plan", cp.get("provenance"), exp, f)
    return targets


def _check_leg(leg, exp, targets, f):
    iid = leg.get("invocation_id")
    want = {"reconciliation": "matched", "execution_status": "preview", "test_verdict": "not_run",
            "enforcement_mode": MODE, "reason_code": None, "package_digest": "not_required",
            "installed_package_sha256": None, "unproven_identity_rungs": []}
    for k, v in want.items():
        if leg.get(k) != v:
            f.append("run %r %s is %r, expected %r" % (iid, k, leg.get(k), v))
    ev = leg.get("identity_evidence") if isinstance(leg.get("identity_evidence"), dict) else {}
    if sorted(ev) != ["l1", "l2a", "l2b"] or any(v != "not_attempted" for v in ev.values()):
        f.append("run %r claims identity evidence %r (a preview attempts none)" % (iid, ev))
    counts = leg.get("counts") if isinstance(leg.get("counts"), dict) else None
    if counts is None or any(counts.get(k) != 0 for k in ("tests", "failures", "errors", "skipped")):
        f.append("run %r reports test counts %r (a preview runs no product test)" % (iid, counts))
    prov = leg.get("provenance") if isinstance(leg.get("provenance"), dict) else {}
    want_prov = {"caller_repo": exp["EXPECT_REPOSITORY"], "caller_run_id": exp["EXPECT_RUN_ID"],
                 "caller_run_attempt": exp["EXPECT_RUN_ATTEMPT"], "caller_sha": exp["EXPECT_SHA"],
                 "caller_ref": exp["EXPECT_REF"], "pep_requested_ref": exp["EXPECT_PEP_SHA"],
                 "pep_resolved_sha": exp["EXPECT_PEP_SHA"]}
    for k, v in want_prov.items():
        if prov.get(k) != v:
            f.append("run %r provenance.%s is %r, expected %r" % (iid, k, prov.get(k), v))
    pi = leg.get("planned_invocation") if isinstance(leg.get("planned_invocation"), dict) else {}
    if pi.get("invocation_id") != iid:
        f.append("run %r planned invocation id is %r" % (iid, pi.get("invocation_id")))
    if pi.get("component") != COMPONENT or pi.get("package_name") != PACKAGE:
        f.append("run %r is for %r/%r, expected %r/%r" % (iid, pi.get("component"), pi.get("package_name"),
                                                        COMPONENT, PACKAGE))
    if pi.get("source_cell_id") not in FIXTURE_CELLS:
        f.append("run %r comes from cell %r, not a fixture cell" % (iid, pi.get("source_cell_id")))
    tid = pi.get("source_target_id")
    pkg = pi.get("package") if isinstance(pi.get("package"), dict) else {}
    if tid not in targets:
        f.append("run %r target %r is not a cert-plan target" % (iid, tid))
    elif not pkg.get("sha256") or pkg.get("sha256") != targets[tid]:
        f.append("run %r package digest %r is not the captured package %r" % (iid, pkg.get("sha256"), targets[tid]))
    return tid, pi.get("pg_major")


def _check_cert_result(cr, exp, targets, f):
    """Returns {invocation_id: pg_major} for the matched runs."""
    if not isinstance(cr, dict) or cr.get("schema") != "cert-result/1":
        f.append("cert-result is not a cert-result/1 document")
        return {}
    want = {"result_resolved": True, "errors": [], "execution_status": "preview", "test_verdict": "not_run",
            "coverage_status": "partial", "reason_code": None, "coverage_gaps": [],
            "historical_results": [], "unexpected_results": []}
    for k, v in want.items():
        if cr.get(k) != v:
            f.append("cert-result %s is %r, expected %r" % (k, cr.get(k), v))
    n = EXPECTED_PREVIEWS
    want_counts = {"expected_invocations": n, "matched": n, "missing": 0, "completed": 0, "incomplete": 0,
                   "infra_failure": 0, "preview": n, "pass": 0, "fail": 0, "not_run": n,
                   "planner_gaps": 0, "unexpected_results": 0}
    counts = cr.get("counts") if isinstance(cr.get("counts"), dict) else {}
    for k, v in want_counts.items():
        if counts.get(k) != v:
            f.append("cert-result counts.%s is %r, expected %r" % (k, counts.get(k), v))
    _provenance("cert-result", cr.get("provenance"), exp, f,
                ("repository", "run_id", "run_attempt", "sha", "ref", "pep_requested_ref", "pep_resolved_sha"))
    ac = cr.get("attempt_context") if isinstance(cr.get("attempt_context"), dict) else {}
    for k in ("plan_run_attempt", "aggregation_run_attempt"):
        if ac.get(k) != exp["EXPECT_RUN_ATTEMPT"]:
            f.append("cert-result attempt_context.%s is %r, expected %r" % (k, ac.get(k), exp["EXPECT_RUN_ATTEMPT"]))
    release = cr.get("release") if isinstance(cr.get("release"), dict) else {}
    if release.get("logical_component") != COMPONENT:
        f.append("cert-result release component is %r, expected %r" % (release.get("logical_component"), COMPONENT))
    legs = cr.get("legs") if isinstance(cr.get("legs"), list) else []
    if len(legs) != n or not all(isinstance(l, dict) for l in legs):
        f.append("cert-result has %d runs, expected exactly %d" % (len(legs), n))
        return {}
    ids = [l.get("invocation_id") for l in legs]
    if len(set(ids)) != len(ids):
        f.append("cert-result repeats an invocation id")
    coverage = {}
    for leg in legs:
        tid, pg = _check_leg(leg, exp, targets, f)
        coverage.setdefault(tid, []).append(pg)
    if set(coverage) != set(targets) or len(coverage) != len(FIXTURE_CELLS):
        f.append("previews cover targets %r, expected both fixture targets %r"
                 % (sorted(map(str, coverage)), sorted(map(str, targets))))
    for tid, pgs in sorted(coverage.items(), key=lambda kv: str(kv[0])):
        if sorted(pgs, key=str) != sorted(EXPECTED_PG_MAJORS):
            f.append("target %r previewed on PG %r, expected each of %r once"
                     % (tid, sorted(pgs, key=str), list(EXPECTED_PG_MAJORS)))
    return {l.get("invocation_id"): (l.get("planned_invocation") or {}).get("pg_major") for l in legs}


def _check_decision(decision, cr, env, f):
    if not isinstance(decision, dict) or decision.get("schema") != "pep-cert-decision/1":
        f.append("cert-decision is not a pep-cert-decision/1 document")
        return
    want = {"certification_state": "preview", "policy_decision": "report", "workflow_conclusion": "success",
            "requested_mode": MODE, "reason_code": "preview",
            "axes": {"result_resolved": True, "execution_status": "preview", "test_verdict": "not_run",
                     "coverage_status": "partial", "reason_code": None}}
    for k, v in want.items():
        if decision.get(k) != v:
            f.append("cert-decision %s is %r, expected %r" % (k, decision.get(k), v))
    if decision != G.decide(cr, MODE):
        f.append("cert-decision does not match the decision pep_cert_gate derives from cert-result")
    outputs = {"CERT_CONCLUSION": decision.get("workflow_conclusion"),
               "CERT_STATE": decision.get("certification_state"), "CERT_REASON": decision.get("reason_code")}
    axes = decision.get("axes") if isinstance(decision.get("axes"), dict) else {}
    outputs.update({"CERT_EXECUTION": axes.get("execution_status"), "CERT_TESTS": axes.get("test_verdict"),
                    "CERT_COVERAGE": axes.get("coverage_status")})
    for k, v in outputs.items():
        if env.get(k) != v:
            f.append("coordinator output %s %r disagrees with cert-decision %r" % (k, env.get(k), v))


def _check_ledger(ledger, exp, f):
    """Returns the ids of the summary artifacts the collector ingested."""
    if not isinstance(ledger, dict) or ledger.get("schema") != "pep-collection-ledger/1":
        f.append("collection ledger is not a pep-collection-ledger/1 document")
        return set()
    want = {"collection_status": "ok", "listing_ok": True, "prefix_ok": True, "name_prefix": SUMMARY_PREFIX,
            "current_run_attempt": exp["EXPECT_RUN_ATTEMPT"]}
    for k, v in want.items():
        if ledger.get(k) != v:
            f.append("collection ledger %s is %r, expected %r" % (k, ledger.get(k), v))
    n = EXPECTED_PREVIEWS
    want_counts = {"candidates": n, "ingested": n, "expired": 0, "candidate_anomalies": 0, "invalid_entries": 0}
    counts = ledger.get("counts") if isinstance(ledger.get("counts"), dict) else {}
    for k, v in want_counts.items():
        if counts.get(k) != v:
            f.append("collection ledger counts.%s is %r, expected %r" % (k, counts.get(k), v))
    cands = ledger.get("candidates") if isinstance(ledger.get("candidates"), list) else []
    ids = set()
    for c in cands:
        if not isinstance(c, dict):
            f.append("collection ledger has a malformed candidate")
            continue
        if (c.get("extraction"), c.get("summary_count"), c.get("expired")) != ("one_summary", 1, False):
            f.append("summary artifact %r was not ingested as exactly one current summary (%r)"
                     % (c.get("artifact_name"), c.get("extraction")))
        ids.add(c.get("artifact_id"))
    return ids


def _check_listing(listing, exp, env, ledger_ids, f):
    f.extend(_listing_problems(listing))
    if not isinstance(listing, list):
        return
    listing = [a for a in listing if isinstance(a, dict)]
    ev_id = _one_named(listing, evidence_name(exp), f)
    if ev_id is not None and str(ev_id) != env.get("CERT_EVIDENCE_ID"):
        f.append("artifact %s has id %r, but the coordinator reported %r"
                 % (evidence_name(exp), ev_id, env.get("CERT_EVIDENCE_ID")))
    summaries = [a for a in listing if str(a.get("name", "")).startswith(SUMMARY_PREFIX)]
    if len(summaries) != EXPECTED_PREVIEWS:
        f.append("the run has %d %s* artifacts, expected exactly %d" % (len(summaries), SUMMARY_PREFIX, EXPECTED_PREVIEWS))
    if any(a.get("expired") is not False for a in summaries):
        f.append("a %s* artifact is expired" % SUMMARY_PREFIX)
    if {a.get("id") for a in summaries} != ledger_ids:
        f.append("the summaries the collector ingested are not exactly the run's %s* artifacts" % SUMMARY_PREFIX)
    errs = sorted(str(a.get("name")) for a in listing if str(a.get("name", "")).startswith(CAPTURE_ERROR_PREFIX))
    if errs:
        f.append("the run has a capture error artifact: %s" % ", ".join(errs))


def _check_report(html, decision, legs_pg, f):
    if not isinstance(html, str) or not html:
        f.append("the consolidated HTML report is missing or empty")
        return
    verdicts = _VERDICT_RE.findall(html)
    state = decision.get("certification_state") if isinstance(decision, dict) else None
    if verdicts != [(state, "true")]:
        f.append("report verdict banner %r does not show the verified decision state %r" % (verdicts, state))
    for marker, what in _REPORT_FORBIDDEN:
        if marker in html:
            f.append("the report shows %s" % what)
    rows = {iid: (fail, pg, status) for iid, fail, pg, status in _LEG_ROW_RE.findall(html)}
    if len(_LEG_ROW_RE.findall(html)) != len(rows) or set(rows) != set(legs_pg):
        f.append("report run rows %r are not exactly the cert-result runs %r" % (sorted(rows), sorted(map(str, legs_pg))))
    for iid, (fail, pg, status) in sorted(rows.items()):
        if (fail, status) != ("0", "preview") or (iid in legs_pg and pg != legs_pg[iid]):
            f.append("report row %r shows status %r, attention %r, PG %r" % (iid, status, fail, pg))


def _check_no_leaks(docs, f):
    for label, doc in docs:
        blob = json.dumps(doc).lower()
        for bad in _LEAK_MARKERS:
            if bad in blob:
                f.append("%s contains %r (no URL or credential may be persisted)" % (label, bad))


def check_documents(docs, env, exp):
    """Every failure across the downloaded documents. ``docs`` holds the parsed cert_result,
    decision, ledger, capture_evidence, cert_plan and listing, plus the report text."""
    f = []
    _check_capture_evidence(docs.get("capture_evidence"), exp, f)
    targets = _check_cert_plan(docs.get("cert_plan"), exp, f)
    legs_pg = _check_cert_result(docs.get("cert_result"), exp, targets, f)
    _check_decision(docs.get("decision"), docs.get("cert_result"), env, f)
    ledger_ids = _check_ledger(docs.get("ledger"), exp, f)
    _check_listing(docs.get("listing"), exp, env, ledger_ids, f)
    _check_report(docs.get("report_html"), docs.get("decision"), legs_pg, f)
    _check_no_leaks([(k, docs.get(k)) for k in ("cert_result", "decision", "ledger", "capture_evidence", "cert_plan")], f)
    return f


# --------------------------------------------------------------------------- CLI
def _load_json(path, label):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UsageError("%s %s is unreadable: %s" % (label, path, e))


def _report(failures, ok_message):
    if failures:
        for m in failures:
            print("ASSERT FAIL: %s" % m)
        print("%d check(s) failed" % len(failures))
        return 1
    print("OK %s" % ok_message)
    return 0


def main(argv=None, env=None):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description="Check one PEP Certification Preview run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("coordinator", help="check the coordinator job result and outputs (environment)")
    sel = sub.add_parser("select", help="select this attempt's capture document ids from the run listing")
    sel.add_argument("--listing", required=True)
    sel.add_argument("--github-output", required=True)
    ver = sub.add_parser("verify", help="check the downloaded documents")
    ver.add_argument("--evidence-dir", required=True)
    ver.add_argument("--capture-evidence", required=True)
    ver.add_argument("--cert-plan", required=True)
    ver.add_argument("--listing", required=True)
    args = ap.parse_args(argv)
    try:
        exp = expectations(env)
        if args.cmd == "coordinator":
            return _report(check_coordinator(env, exp),
                           "coordinator succeeded with a preview decision; evidence id %s" % env.get("CERT_EVIDENCE_ID"))
        if args.cmd == "select":
            ids, failures = select_capture_ids(_load_json(args.listing, "listing"), exp)
            if not failures:
                with open(args.github_output, "a", encoding="utf-8") as fh:
                    for k in ("capture_evidence_id", "cert_plan_id"):
                        fh.write("%s=%d\n" % (k, ids[k]))
            return _report(failures, "selected capture evidence %s and cert-plan %s"
                           % (ids.get("capture_evidence_id"), ids.get("cert_plan_id")))
        ev = Path(args.evidence_dir)
        report = ev / REPORT_FILE
        docs = {"cert_result": _load_json(ev / "cert-result.json", "cert-result"),
                "decision": _load_json(ev / "cert-decision.json", "cert-decision"),
                "ledger": _load_json(ev / "collection-ledger.json", "collection ledger"),
                "capture_evidence": _load_json(args.capture_evidence, "capture evidence"),
                "cert_plan": _load_json(args.cert_plan, "cert-plan"),
                "listing": _load_json(args.listing, "listing"),
                "report_html": report.read_text(encoding="utf-8") if report.is_file() else None}
    except UsageError as e:
        print("ASSERT FAIL: %s" % e)
        return 2
    return _report(check_documents(docs, env, exp),
                   "%d previews of both fixture targets on PG %s; receipts accepted; simulated, nothing published; "
                   "run %s attempt %s at %s" % (EXPECTED_PREVIEWS, "/".join(EXPECTED_PG_MAJORS),
                                                exp["EXPECT_RUN_ID"], exp["EXPECT_RUN_ATTEMPT"], exp["EXPECT_PEP_SHA"]))


if __name__ == "__main__":
    sys.exit(main())

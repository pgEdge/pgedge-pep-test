#!/usr/bin/env python3
"""TEMPORARY mechanics-spike capture/consumer (branch spike/pep-mechanics only).

Runs INSIDE the workflow_call capture job. It exercises the *shipping* evidence
path against the live GitHub run, feeding the COMMITTED, UNCHANGED offline
adapter + reducer, and it self-checks with an explicit per-attempt oracle.

Subcommands:
  capture   -- live: list jobs/artifacts (REST, forced pagination), validate and
               parse receipts, run the committed adapter + reducer, verify
               digests, apply the per-attempt oracle, and write evidence.
  selftest  -- offline, no network: repeatable checks for the receipt validator,
               the per-attempt oracle, and fixture-drift between the caller
               matrix and spike/planned_matrix.json.

Everything here is throwaway spike scaffolding. The committed adapter/reducer are
imported unchanged; RELEASE_INTENT/COMPONENT_POLICY are synthetic stand-ins for
data that flows from the build-side contract + PEP catalog in production. Stdlib
only. Delete with the spike branch.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "utillities"))
import pep_cert_adapter as A   # noqa: E402
import pep_cert_plan as R      # noqa: E402

API_VERSION = "2022-11-28"
GITHUB_API_HOST = "api.github.com"
MAX_REDIRECTS = 5
PER_PAGE = 1                   # force multi-page listings even for a handful of records
RECEIPT_SCHEMA = "pep-receipt/1"
RPM = "pepcell.v1.rpm.el-9.amd64.pkg"
DEB = "pepcell.v1.deb.bookworm.amd64.pkg"
_MEMBER_FIELDS = ("package_name", "version", "release", "native_arch", "package_class", "sha256")
_SHA_RE = re.compile(r"\A[0-9a-f]{64}\Z")

# Synthetic release intent — MUST match spike/produce.py so a built+published cell
# can reach identity=confirmed. Production: build-side contract + PEP catalog.
RELEASE_INTENT = {
    "logical_component": "rag-server", "intended_version": "1.0.0",
    "intended_buildnum": "1", "effective_tag": "spike", "channel": "daily",
    "simulated": False,
}
COMPONENT_POLICY = {"allowed_runtime_package_names": ["pgedge-rag-server"]}
PUBLICATION_RESULTS = {"rpm": "success", "deb": "success"}


# --- small pure helpers ------------------------------------------------------
def _norm_digest(s):
    """Normalize a documented sha256 digest ('sha256:<hex>' or '<hex>') to lower hex,
    or None if absent/malformed."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if s.lower().startswith("sha256:"):
        s = s[len("sha256:"):]
    s = s.strip().lower()
    return s if _SHA_RE.match(s) else None


def _valid_member_shape(members):
    """Expected valid SYNTHETIC member shape: exactly one runtime member with the six
    string identity fields (nonblank) and a well-formed sha256."""
    if not isinstance(members, list) or len(members) != 1 or not isinstance(members[0], dict):
        return False
    m = members[0]
    if not all(isinstance(m.get(f), str) and m.get(f).strip() for f in _MEMBER_FIELDS):
        return False
    if m.get("package_class") != "runtime":
        return False
    return _norm_digest(m.get("sha256")) is not None


def validate_receipts(candidates, arts_by_id, planned_ids):
    """Validate downloaded receipts BEFORE use. `candidates` is a list of
    {"receipt": dict|None, "json_count": int, "receipt_artifact_name": str}.
    Returns (valid, errors); valid entries carry the fields the adapter + digest
    checks need. Only receipts passing EVERY rule are returned."""
    planned = set(planned_ids)
    errors, prelim = [], []
    for c in candidates:
        ran = c.get("receipt_artifact_name")
        if c.get("json_count") != 1:
            errors.append("receipt artifact %r must contain exactly one receipt.json (found %r)"
                          % (ran, c.get("json_count")))
            continue
        r = c.get("receipt")
        if not isinstance(r, dict):
            errors.append("receipt artifact %r has a non-object receipt" % (ran,))
            continue
        if r.get("schema") != RECEIPT_SCHEMA:
            errors.append("receipt %r has schema %r, want %r" % (ran, r.get("schema"), RECEIPT_SCHEMA))
            continue
        cid = r.get("cell_id")
        if not (isinstance(cid, str) and cid.strip() and cid in planned):
            errors.append("receipt %r has non-planned/blank cell_id %r" % (ran, cid))
            continue
        aid = r.get("artifact_id")
        if not (isinstance(aid, int) and not isinstance(aid, bool) and aid >= 1):
            errors.append("receipt %r has non-positive-int artifact_id %r" % (ran, aid))
            continue
        art = arts_by_id.get(aid)
        if art is None:
            errors.append("receipt %r artifact_id %d does not resolve to a live artifact" % (ran, aid))
            continue
        expired = art.get("expired")
        if not isinstance(expired, bool) or expired:   # missing / non-bool / true -> not live
            errors.append("receipt %r artifact_id %d is not a live artifact (expired=%r)" % (ran, aid, expired))
            continue
        if r.get("artifact_name") != art.get("name"):
            errors.append("receipt %r artifact_name %r != live artifact name %r"
                          % (ran, r.get("artifact_name"), art.get("name")))
            continue
        if not _valid_member_shape(r.get("members")):
            errors.append("receipt %r members has an invalid synthetic shape" % (ran,))
            continue
        rd, ad = _norm_digest(r.get("archive_digest")), _norm_digest(art.get("digest"))
        if rd is None:
            errors.append("receipt %r archive_digest is blank/malformed: %r" % (ran, r.get("archive_digest")))
            continue
        if ad is None:
            errors.append("receipt %r: live artifact has no/invalid digest to match: %r"
                          % (ran, art.get("digest")))
            continue
        if rd != ad:
            errors.append("receipt %r archive_digest %s != live artifact digest %s" % (ran, rd, ad))
            continue
        prelim.append((cid, aid, r, art.get("name")))
    # exactly one receipt per planned cell AND per package artifact — with precise
    # messages for the two distinct failure modes.
    cid_count = Counter(x[0] for x in prelim)
    aid_count = Counter(x[1] for x in prelim)
    cid_to_aids, aid_to_cids = {}, {}
    for cid, aid, _r, _p in prelim:
        cid_to_aids.setdefault(cid, set()).add(aid)
        aid_to_cids.setdefault(aid, set()).add(cid)
    valid = []
    for cid, aid, r, pkg_name in prelim:
        if cid_count[cid] > 1:                    # one cell, one-or-more artifact ids
            errors.append("cell %s referenced by %d receipts (artifact ids %s)"
                          % (cid, cid_count[cid], sorted(cid_to_aids[cid])))
            continue
        if aid_count[aid] > 1:                     # one artifact id, multiple cells
            errors.append("artifact id %d referenced by %d receipts (cells %s)"
                          % (aid, aid_count[aid], sorted(aid_to_cids[aid])))
            continue
        valid.append({"cell_id": cid, "artifact_id": aid, "members": r["members"],
                      "archive_digest": r.get("archive_digest"), "source_pkg_name": pkg_name})
    return valid, errors


def per_attempt_oracle(attempt, plan_resolved, per_cell, valid_count):
    """Explicit expectation per attempt. Returns (expectation_met, reason)."""
    try:
        n = int(attempt)
    except (TypeError, ValueError):
        return False, "unsupported attempt %r (not an integer)" % (attempt,)
    rpm, deb = per_cell.get(RPM, {}), per_cell.get(DEB, {})
    got = ("rpm=%s/%s deb=%s/%s valid_receipts=%d plan_resolved=%s"
           % (rpm.get("build_state"), rpm.get("eligible_targets"),
              deb.get("build_state"), deb.get("eligible_targets"), valid_count, plan_resolved))
    if n == 1:
        ok = (plan_resolved is True
              and rpm.get("build_state") == "available" and rpm.get("eligible_targets") == 1
              and deb.get("build_state") == "failed" and deb.get("eligible_targets") == 0
              and valid_count == 1)
        return ok, ("attempt1 want RPM available/1, DEB failed/0, 1 valid receipt; got %s" % got)
    if n in (2, 3):
        ok = (plan_resolved is True
              and rpm.get("build_state") == "available" and rpm.get("eligible_targets") == 1
              and deb.get("build_state") == "available" and deb.get("eligible_targets") == 1
              and valid_count == 2)
        return ok, ("attempt%d want both available/1, 2 valid receipts; got %s" % (n, got))
    return False, "unsupported attempt %d (spike models attempts 1-3 only)" % n


_EXPECTED_CELLS = {1: [RPM], 2: [RPM, DEB], 3: [RPM, DEB]}


def download_route_ok(outcome, downloads_dir, run_attempt):
    """#2: the pinned actions/download-artifact route is an observed acceptance
    condition, INDEPENDENT of receipt validation so receipt corruption cannot let
    it pass vacuously. Expected cells derive from the attempt (attempt 1: RPM only;
    attempts 2/3: RPM + DEB); each expected cell must have package + receipt files
    from the action download, and the action outcome must be success."""
    try:
        expected = _EXPECTED_CELLS.get(int(run_attempt))
    except (TypeError, ValueError):
        expected = None
    dl = Path(downloads_dir)
    files = sorted(str(p.relative_to(dl)) for p in dl.rglob("*") if p.is_file()) if dl.is_dir() else []
    if expected is None:
        return False, {"outcome": outcome, "expected_cells": None, "files": files,
                       "missing": ["unsupported attempt %r" % (run_attempt,)]}
    missing = []
    for cid in expected:
        pkg_dirs = [d for d in dl.glob("pkg-%s-*" % cid) if d.is_dir()] if dl.is_dir() else []
        if not any(any(p.is_file() for p in d.rglob("*")) for d in pkg_dirs):
            missing.append("package files for %s" % cid)
        if not (dl / ("pep-receipt-%s" % cid) / "receipt.json").is_file():
            missing.append("receipt.json for %s" % cid)
    ok = (outcome == "success" and not missing)
    return ok, {"outcome": outcome, "expected_cells": expected, "files": files, "missing": missing}


def classify_gate(run_attempt, exp_met, dl_ok, errors, fully_covered):
    """Final gate. PASS only when the attempt oracle holds, the download route is ok,
    there are zero validation errors, and coverage is complete. A FAIL is 'intentional'
    (the expected attempt-1 partial that lets rerun-failed proceed) ONLY when
    run_attempt == 1, the attempt-1 oracle is met, the download route is ok, and there
    are no errors — so a download-route or validation failure is NEVER intentional, and
    attempts 2/3 are never intentional."""
    gate = "PASS" if (exp_met and dl_ok and not errors and fully_covered) else "FAIL"
    intentional = (str(run_attempt) == "1" and exp_met and dl_ok and not errors and gate == "FAIL")
    return gate, intentional


# --- HTTP (capture only) -----------------------------------------------------
def _req(url, token, accept="application/vnd.github+json"):
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", API_VERSION)
    return req


def _api_get(url, token):
    with urllib.request.urlopen(_req(url, token), timeout=60) as resp:   # nosec B310 - fixed api.github.com host
        return json.loads(resp.read().decode("utf-8"))


# --- safe artifact-zip download (auth never forwarded cross-origin) -----------
# GitHub's archive_download_url (on api.github.com) answers an authenticated
# request with a 302 to a short-lived, pre-signed blob URL on a SEPARATE storage
# host. The GITHUB_TOKEN must reach ONLY the api.github.com origin; the signed
# blob is fetched with NO credentials. urllib's default opener re-sends
# Authorization across the redirect, which the storage host rejects (HTTP 401) —
# so redirects are followed manually with per-hop header control.
_GITHUB_ONLY_HEADERS = ("authorization", "accept", "x-github-api-version")


def _is_github_api_url(url):
    """True only for an HTTPS URL on the GitHub API host — the sole origin the
    GITHUB_TOKEN may be sent to."""
    p = urllib.parse.urlsplit(url)
    return p.scheme == "https" and (p.hostname or "").lower() == GITHUB_API_HOST


def _same_origin(u1, u2):
    a, b = urllib.parse.urlsplit(u1), urllib.parse.urlsplit(u2)
    return ((a.scheme, (a.hostname or "").lower(), a.port)
            == (b.scheme, (b.hostname or "").lower(), b.port))


def _initial_archive_headers(token):
    return {"Authorization": "Bearer %s" % token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION}


def _next_hop_headers(from_url, to_url, headers):
    """Headers for a redirect target. Reject an HTTPS->non-HTTPS downgrade. Keep
    headers on a same-origin hop; on any cross-origin hop drop Authorization and
    the GitHub-API-specific headers so credentials never leave the API origin."""
    src, dst = urllib.parse.urlsplit(from_url), urllib.parse.urlsplit(to_url)
    if src.scheme == "https" and dst.scheme != "https":
        raise A.AdapterError("refusing HTTPS->non-HTTPS redirect: %s -> %s" % (from_url, to_url))
    if _same_origin(from_url, to_url):
        return dict(headers)
    return {k: v for k, v in headers.items() if k.lower() not in _GITHUB_ONLY_HEADERS}


class _NoAutoRedirect(urllib.request.HTTPRedirectHandler):
    """Suppress urllib's automatic redirect following so each hop's headers can be
    chosen explicitly (a 3xx is surfaced as HTTPError carrying Location)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NOREDIR_OPENER = urllib.request.build_opener(_NoAutoRedirect)


def _download_zip(url, token):
    """Fetch an artifact ZIP by its GitHub API archive URL, following the redirect
    to signed blob storage WITHOUT ever forwarding the token cross-origin."""
    if not _is_github_api_url(url):    # validate BEFORE sending any credential
        raise A.AdapterError("refusing to send credentials to a non-GitHub archive URL: %r" % (url,))
    headers = _initial_archive_headers(token)
    cur = url
    for _ in range(MAX_REDIRECTS + 1):
        req = urllib.request.Request(cur, headers=headers, method="GET")
        try:
            resp = _NOREDIR_OPENER.open(req, timeout=120)   # nosec B310 - origin validated, no cross-origin creds
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                nxt = urllib.parse.urljoin(cur, e.headers["Location"])
                headers = _next_hop_headers(cur, nxt, headers)   # strips creds cross-origin / rejects downgrade
                cur = nxt
                continue
            raise
        with resp:
            return zipfile.ZipFile(io.BytesIO(resp.read()))
    raise A.AdapterError("too many redirects fetching artifact zip from %r" % (url,))


# --- ZIP content helpers (shared by capture + selftest) ----------------------
def _zip_read_receipt(zf):
    """Return (receipt_dict_or_None, json_count) from a receipt artifact ZIP."""
    names = [n for n in zf.namelist() if n.endswith("receipt.json")]
    if len(names) != 1:
        return None, len(names)
    return json.loads(zf.read(names[0]).decode("utf-8")), 1


def _zip_member_sha(zf):
    """SHA-256 of the single file in a package artifact ZIP, or None if not exactly one."""
    files = [n for n in zf.namelist() if not n.endswith("/")]
    return hashlib.sha256(zf.read(files[0])).hexdigest() if len(files) == 1 else None


def _list_paginated(repo, run_id, kind, items_key, token, evidence_dir, extra=""):
    pages, page = [], 1
    while True:
        url = ("https://api.github.com/repos/%s/actions/runs/%s/%s?per_page=%d&page=%d%s"
               % (repo, run_id, kind, PER_PAGE, page, extra))
        env = _api_get(url, token)
        (evidence_dir / ("%s-page-%d.json" % (kind, page))).write_text(
            json.dumps(env, indent=2, sort_keys=True) + "\n")
        items = env.get(items_key)
        if not isinstance(items, list) or not items:
            break
        pages.append(env)
        if len(items) < PER_PAGE:
            break
        page += 1
    return pages


def _reduce_from(planned_cells, planned_ids, jobs, inventory, receipts_for_adapter, repo, run_id, run_attempt):
    job_records = A.job_records_from_jobs(jobs, planned_ids)
    artifacts = A.artifact_records(inventory, planned_ids, receipts=receipts_for_adapter)
    env = A.assemble_reducer_input(
        planned_cells=planned_cells, job_records=job_records, artifacts=artifacts,
        publication_results=PUBLICATION_RESULTS, release_intent=RELEASE_INTENT,
        component_policy=COMPONENT_POLICY,
        provenance={"repository": repo, "run_id": run_id, "run_attempt": run_attempt,
                    "sha": os.environ.get("GITHUB_SHA"), "ref": os.environ.get("GITHUB_REF")})
    return env, R.reduce(env)


def _per_cell(plan, planned_ids):
    by = {c.get("cell_id"): c for c in plan.get("cells", [])}
    out = {}
    for cid in planned_ids:
        c = by.get(cid, {})
        out[cid] = {
            "build_state": c.get("build_state"),
            "publication_state": c.get("publication_state"),
            "available": c.get("build_state") == "available",
            "eligible_targets": sum(1 for t in c.get("targets", []) if t.get("eligibility") == "eligible"),
        }
    return out


def _planned(matrix_path):
    m = json.loads(Path(matrix_path).read_text())
    cells = A.planned_cells_from_detector(m["rpm_matrix"], m["deb_matrix"])
    return cells, [c["cell_id"] for c in cells if isinstance(c.get("cell_id"), str)]


# --- capture (live) ----------------------------------------------------------
def cmd_capture(a):
    ev = Path(a.evidence)
    ev.mkdir(parents=True, exist_ok=True)
    errors = []
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        (ev / "result.json").write_text(json.dumps(
            {"gate": "FAIL", "expectation_met": False,
             "errors": ["no GITHUB_TOKEN available for the REST API"]}, indent=2) + "\n")
        return 0

    job_pages = _list_paginated(repo, run_id, "jobs", "jobs", token, ev, extra="&filter=all")
    art_pages = _list_paginated(repo, run_id, "artifacts", "artifacts", token, ev)
    try:
        jobs = A.combine_pages(job_pages, "jobs")
        arts = A.combine_pages(art_pages, "artifacts")
    except A.AdapterError as e:
        (ev / "result.json").write_text(json.dumps(
            {"gate": "FAIL", "expectation_met": False, "errors": ["combine_pages: %s" % e]}, indent=2) + "\n")
        return 0
    arts_by_id = {x["id"]: x for x in arts if isinstance(x.get("id"), int)}

    cells, planned_ids = _planned(a.matrix)

    # Build receipt candidates by downloading each receipt artifact via REST.
    candidates = []
    for art in arts:
        name = art.get("name") or ""
        if not name.startswith("pep-receipt-") or art.get("expired"):
            continue
        try:
            zf = _download_zip(art.get("archive_download_url"), token)
            rec, jn = _zip_read_receipt(zf)
            candidates.append({"receipt": rec, "json_count": jn, "receipt_artifact_name": name})
        except Exception as e:   # noqa: BLE001
            errors.append("receipt download failed for %r: %s" % (name, e))
    valid, verr = validate_receipts(candidates, arts_by_id, planned_ids)
    errors.extend(verr)

    receipts_for_adapter = [{"artifact_id": v["artifact_id"], "cell_id": v["cell_id"]} for v in valid]
    member_by_aid = {v["artifact_id"]: v["members"] for v in valid}

    inventory = []                                  # COMPLETE inventory; enrich referenced pkgs
    for art in arts:
        rec = dict(art)
        if isinstance(rec.get("id"), int) and rec["id"] in member_by_aid:
            rec["members"] = member_by_aid[rec["id"]]
        inventory.append(rec)
    (ev / "inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")

    try:
        env, plan = _reduce_from(cells, planned_ids, jobs, inventory, receipts_for_adapter,
                                 repo, run_id, run_attempt)
    except A.AdapterError as e:
        (ev / "result.json").write_text(json.dumps(
            {"gate": "FAIL", "expectation_met": False, "errors": ["adapter raised: %s" % e]}, indent=2) + "\n")
        return 0
    (ev / "env.json").write_text(json.dumps(env, indent=2, sort_keys=True) + "\n")
    (ev / "plan.json").write_text(R.to_json(plan))
    (ev / "receipts.json").write_text(json.dumps([c["receipt"] for c in candidates], indent=2, sort_keys=True) + "\n")

    # Independent member-sha recompute from package bytes (separate from archive digest).
    sha_checks = []
    for v in valid:
        art = arts_by_id.get(v["artifact_id"])
        recomputed = None
        try:
            recomputed = _zip_member_sha(_download_zip(art.get("archive_download_url"), token))
        except Exception as e:   # noqa: BLE001
            errors.append("package download failed for %s: %s" % (v["cell_id"], e))
        want = v["members"][0].get("sha256")
        match = (recomputed is not None and recomputed == want)
        sha_checks.append({"cell_id": v["cell_id"], "artifact_id": v["artifact_id"],
                           "member_sha256": want, "recomputed": recomputed, "match": match,
                           "archive_digest": v["archive_digest"], "api_digest": (art or {}).get("digest")})
        if not match:
            errors.append("member sha256 mismatch for %s" % v["cell_id"])

    per_cell = _per_cell(plan, planned_ids)
    exp_met, exp_reason = per_attempt_oracle(run_attempt, plan.get("plan_resolved"), per_cell, len(valid))
    dl_ok, dl_info = download_route_ok(a.download_outcome, a.downloads, run_attempt)
    if not dl_ok:                                   # a broken action route is a real error, never intentional
        errors.append("download-artifact route failed: outcome=%r missing=%s"
                      % (dl_info.get("outcome"), dl_info.get("missing")))
    fully_covered = all(per_cell[c]["available"] and per_cell[c]["eligible_targets"] >= 1 for c in planned_ids)
    gate, intentional = classify_gate(run_attempt, exp_met, dl_ok, errors, fully_covered)

    result = {
        "gate": gate, "intentional_partial": intentional,
        "expectation_met": exp_met, "expectation_reason": exp_reason,
        "run_attempt": run_attempt, "plan_resolved": plan.get("plan_resolved"),
        "planned_cells": len(planned_ids), "per_cell": per_cell,
        "valid_receipts": [{"cell_id": v["cell_id"], "artifact_id": v["artifact_id"]} for v in valid],
        "valid_receipt_count": len(valid),
        "download_route_ok": dl_ok, "download_route": dl_info,
        "jobs_seen": len(jobs), "artifacts_seen": len(arts),
        "sha_checks": sha_checks, "errors": errors,
        "note": ("attempt 1 is expected PARTIAL (expectation_met true, gate FAIL, "
                 "intentional_partial true) so rerun-failed can proceed; attempts 2 and 3 "
                 "expected gate PASS. A download-route or validation failure is never intentional."),
    }
    (ev / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print("capture: gate=%s intentional=%s expectation_met=%s dl_ok=%s valid_receipts=%d errors=%d"
          % (gate, intentional, exp_met, dl_ok, len(valid), len(errors)))
    return 0   # COLLECT mode; the gate workflow step enforces result.json


# --- selftest (offline, repeatable) ------------------------------------------
def _art(aid, name, digest, expired=False):
    return {"id": aid, "name": name, "expired": expired, "digest": digest,
            "archive_download_url": "https://example/%d" % aid}


def _member(family, os_token, native):
    rel = ("1." + os_token.replace("-", "")) if family == "rpm" else ("1." + os_token)
    body = ("pep-spike synthetic package\ncell=x\nname=pgedge-rag-server\n"
            "version=1.0.0\nrelease=%s\narch=%s\n" % (rel, native)).encode()
    return {"package_name": "pgedge-rag-server", "package_class": "runtime", "version": "1.0.0",
            "release": rel, "native_arch": native, "sha256": hashlib.sha256(body).hexdigest()}


def _receipt(cid, aid, name, digest, members):
    return {"schema": RECEIPT_SCHEMA, "cell_id": cid, "artifact_id": aid,
            "artifact_name": name, "archive_digest": digest, "members": members}


def _cand(receipt, json_count=1, ran="pep-receipt-x"):
    return {"receipt": receipt, "json_count": json_count, "receipt_artifact_name": ran}


def _extract_caller_matrix(caller_path):
    """Extract the caller workflow's matrix include as structured data using ONLY the
    stdlib. The include is authored in flow (JSON-compatible) style, so the balanced
    '[' .. ']' region after 'include:' is valid JSON and json.loads parses it — no
    PyYAML dependency."""
    text = Path(caller_path).read_text()
    start = text.index("[", text.index("include:"))
    depth = 0
    for k in range(start, len(text)):
        if text[k] == "[":
            depth += 1
        elif text[k] == "]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:k + 1])
    raise ValueError("no matching ] for the caller matrix include list")


def cmd_selftest(a):
    fails = []

    def check(cond, msg):
        print(("  ok  " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    cells, planned_ids = _planned(a.matrix)
    check(set(planned_ids) == {RPM, DEB}, "fixture yields exactly the two planned cells")

    RM, DM = _member("rpm", "el-9", "x86_64"), _member("deb", "bookworm", "amd64")
    D1 = "sha256:" + "a" * 64
    D2 = "b" * 64
    arts_full = {
        11: _art(11, "pkg-%s-9" % RPM, D1), 21: _art(21, "pkg-%s-9" % DEB, D2),
    }

    # ---- receipt validator: positive ----
    good = [_cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [RM])),
            _cand(_receipt(DEB, 21, "pkg-%s-9" % DEB, D2, [DM]))]
    valid, errs = validate_receipts(good, arts_full, planned_ids)
    check(len(valid) == 2 and not errs, "validator accepts two well-formed receipts")

    # ---- receipt validator: each negative rejected ----
    negatives = {
        "two receipt.json": _cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [RM]), json_count=2),
        "wrong schema": _cand({**_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [RM]), "schema": "x/9"}),
        "blank cell_id": _cand(_receipt("   ", 11, "pkg-%s-9" % RPM, D1, [RM])),
        "unplanned cell": _cand(_receipt("pepcell.v1.rpm.el-9.amd64.other", 11, "pkg-%s-9" % RPM, D1, [RM])),
        "nonpositive id": _cand(_receipt(RPM, 0, "pkg-%s-9" % RPM, D1, [RM])),
        "bool id": _cand(_receipt(RPM, True, "pkg-%s-9" % RPM, D1, [RM])),
        "id not live": _cand(_receipt(RPM, 999, "pkg-%s-9" % RPM, D1, [RM])),
        "name mismatch": _cand(_receipt(RPM, 11, "pkg-WRONG", D1, [RM])),
        "empty members": _cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [])),
        "two members": _cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [RM, RM])),
        "non-runtime member": _cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [{**RM, "package_class": "source"}])),
        "bad member sha": _cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [{**RM, "sha256": "zz"}])),
        "blank digest": _cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, "   ", [RM])),
        "digest mismatch": _cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, "sha256:" + "c" * 64, [RM])),
    }
    for label, cand in negatives.items():
        v, e = validate_receipts([cand], arts_full, planned_ids)
        check(len(v) == 0 and len(e) >= 1, "validator rejects: %s" % label)

    # digest matches after normalizing the sha256: prefix (D1 has prefix, artifact stores prefix)
    v, e = validate_receipts([_cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, "a" * 64, [RM]))], arts_full, planned_ids)
    check(len(v) == 1 and not e, "validator matches prefixed vs bare sha256 digest")

    # expired / malformed-expiry package artifact -> "not a live artifact"
    for label, pkgart in {
        "expired true": _art(11, "pkg-%s-9" % RPM, D1, expired=True),
        "expired missing": {"id": 11, "name": "pkg-%s-9" % RPM, "digest": D1, "archive_download_url": "x"},
        "expired nonbool": {"id": 11, "name": "pkg-%s-9" % RPM, "digest": D1, "expired": "no", "archive_download_url": "x"},
    }.items():
        v, e = validate_receipts([_cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [RM]))], {11: pkgart}, planned_ids)
        check(len(v) == 0 and any("not a live artifact" in x for x in e), "validator rejects package: %s" % label)

    # missing/invalid API digest on the live artifact
    v, e = validate_receipts([_cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [RM]))],
                             {11: _art(11, "pkg-%s-9" % RPM, "not-a-digest")}, planned_ids)
    check(len(v) == 0 and any("has no/invalid digest" in x for x in e), "validator rejects missing/invalid API digest")

    # uniqueness: exact duplicate, same cell/different aid, and one aid/two cells
    exact_dup = [_cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [RM])),
                 _cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [RM]))]
    v, e = validate_receipts(exact_dup, arts_full, planned_ids)
    check(len(v) == 0 and any("referenced by" in x for x in e), "validator rejects exact duplicate receipts")

    arts_two_rpm = {11: _art(11, "pkg-%s-9" % RPM, D1), 12: _art(12, "pkg-%s-alt" % RPM, D1)}
    same_cell_diff_aid = [_cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [RM])),
                          _cand(_receipt(RPM, 12, "pkg-%s-alt" % RPM, D1, [RM]))]
    v, e = validate_receipts(same_cell_diff_aid, arts_two_rpm, planned_ids)
    check(len(v) == 0 and any(("cell %s referenced by" % RPM) in x for x in e),
          "validator rejects one cell with two artifact ids")

    two_cells_one_aid = [_cand(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [RM])),
                         _cand(_receipt(DEB, 11, "pkg-%s-9" % RPM, D1, [DM]))]
    v, e = validate_receipts(two_cells_one_aid, arts_full, planned_ids)
    check(len(v) == 0 and any("artifact id 11 referenced by" in x for x in e),
          "validator rejects one artifact id shared by two cells")

    # ---- per-attempt oracle via the real adapter+reducer ----
    def build(jobs, arts_list, valid_receipts, member_map):
        inv = []
        for art in arts_list:
            rec = dict(art)
            if rec["id"] in member_map:
                rec["members"] = member_map[rec["id"]]
            inv.append(rec)
        env, plan = _reduce_from(cells, planned_ids, jobs, inv,
                                 [{"artifact_id": x, "cell_id": c} for x, c in valid_receipts],
                                 "r", "1", "1")
        return plan, _per_cell(plan, planned_ids)

    def job(cid, jid, concl):
        return {"name": "cell %s [pep-cell:%s]" % (cid, cid), "id": jid,
                "run_attempt": 1, "status": "completed", "conclusion": concl}

    # attempt 2/3 full
    arts_ok = [_art(11, "pkg-%s-9" % RPM, D1), _art(12, "pep-receipt-%s" % RPM, D1),
               _art(21, "pkg-%s-9" % DEB, D2), _art(22, "pep-receipt-%s" % DEB, D2),
               _art(90, "probe-[pep-cell.__sentinel__]", D1)]
    plan_ok, pc_ok = build([job(RPM, 1, "success"), job(DEB, 2, "success")], arts_ok,
                           [(11, RPM), (21, DEB)], {11: [RM], 21: [DM]})
    for n in (2, 3):
        met, why = per_attempt_oracle(n, plan_ok["plan_resolved"], pc_ok, 2)
        check(met, "oracle attempt %d met on full success (%s)" % (n, why if not met else "ok"))
    check(pc_ok[RPM]["available"] and pc_ok[DEB]["available"], "full: both cells available (sentinel/receipts ignored)")

    # attempt 1 partial
    arts_a1 = [_art(11, "pkg-%s-9" % RPM, D1), _art(12, "pep-receipt-%s" % RPM, D1),
               _art(90, "probe-[pep-cell.__sentinel__]", D1)]
    plan_a1, pc_a1 = build([job(RPM, 1, "success"), job(DEB, 2, "failure")], arts_a1, [(11, RPM)], {11: [RM]})
    met1, why1 = per_attempt_oracle(1, plan_a1["plan_resolved"], pc_a1, 1)
    check(met1, "oracle attempt 1 met on partial (%s)" % (why1 if not met1 else "ok"))
    check(pc_a1[DEB]["build_state"] == "failed" and pc_a1[DEB]["eligible_targets"] == 0, "partial: DEB failed/0 eligible")

    # oracle rejects wrong shapes + unsupported attempts
    check(not per_attempt_oracle(1, True, pc_ok, 2)[0], "oracle attempt 1 fails if DEB unexpectedly available")
    check(not per_attempt_oracle(2, True, pc_a1, 1)[0], "oracle attempt 2 fails on partial shape")
    check(not per_attempt_oracle(4, True, pc_ok, 2)[0], "oracle rejects unsupported attempt 4")
    check(not per_attempt_oracle("x", True, pc_ok, 2)[0], "oracle rejects non-integer attempt")

    # ---- download-route acceptance (independent of receipts) ----
    def mk_dl(pkg_cells, receipt_cells):
        d = tempfile.mkdtemp()
        for cid in pkg_cells:
            pd = Path(d) / ("pkg-%s-9" % cid)
            pd.mkdir(parents=True)
            (pd / "f.pkg").write_text("x")
        for cid in receipt_cells:
            rd = Path(d) / ("pep-receipt-%s" % cid)
            rd.mkdir(parents=True)
            (rd / "receipt.json").write_text("{}")
        return d

    ok, _ = download_route_ok("success", mk_dl([RPM], [RPM]), 1)
    check(ok, "download route: attempt1 success with RPM package+receipt")
    ok, _ = download_route_ok("failure", mk_dl([RPM], [RPM]), 1)
    check(not ok, "download route: failed action outcome -> not ok")
    ok, _ = download_route_ok("success", mk_dl([], [RPM]), 1)
    check(not ok, "download route: missing package -> not ok")
    ok, _ = download_route_ok("success", mk_dl([RPM], []), 1)
    check(not ok, "download route: missing receipt -> not ok")
    ok, _ = download_route_ok("success", mk_dl([RPM, DEB], [RPM, DEB]), 2)
    check(ok, "download route: attempt2 success with both cells")
    ok, _ = download_route_ok("success", mk_dl([RPM], [RPM]), 2)
    check(not ok, "download route: attempt2 missing DEB -> not ok")
    ok, _ = download_route_ok("success", mk_dl([RPM, DEB], [RPM, DEB]), 9)
    check(not ok, "download route: unsupported attempt -> not ok")

    # ---- final-gate classification (a download/validation failure is NEVER intentional) ----
    g, it = classify_gate(1, True, True, [], False)
    check(g == "FAIL" and it, "gate: attempt1 oracle+dl_ok+no errors -> FAIL, intentional")
    g, it = classify_gate(1, True, False, ["download-artifact route failed"], False)
    check(g == "FAIL" and not it, "gate: attempt1 download failure -> FAIL, NOT intentional")
    g, it = classify_gate(1, True, True, ["some error"], False)
    check(g == "FAIL" and not it, "gate: attempt1 with validation errors -> NOT intentional")
    g, it = classify_gate(1, False, True, [], False)
    check(g == "FAIL" and not it, "gate: attempt1 oracle unmet -> NOT intentional")
    g, it = classify_gate(2, True, True, [], True)
    check(g == "PASS" and not it, "gate: attempt2 full -> PASS, not intentional")
    g, it = classify_gate(2, False, True, [], False)
    check(g == "FAIL" and not it, "gate: attempt2 partial -> FAIL, never intentional")
    g, it = classify_gate(3, True, False, ["download-artifact route failed"], True)
    check(not it, "gate: attempt3 download failure -> never intentional")

    # ---- redirect safety (credentials never forwarded cross-origin) ----
    h0 = _initial_archive_headers("TKN")
    check(h0.get("Authorization") == "Bearer TKN", "initial GitHub API archive request carries Authorization")
    api_a, api_b = "https://api.github.com/x/y", "https://api.github.com/x/z"
    check("Authorization" in _next_hop_headers(api_a, api_b, h0), "same-origin redirect may retain Authorization")
    hx = _next_hop_headers(api_a, "https://blob.example.net/pkg.zip?sig=abc", h0)
    check(not any(k.lower() in _GITHUB_ONLY_HEADERS for k in hx),
          "cross-origin HTTPS redirect drops Authorization + GitHub API headers")
    try:
        _next_hop_headers(api_a, "http://blob.example.net/pkg.zip", h0)
        dgok = False
    except A.AdapterError:
        dgok = True
    check(dgok, "HTTPS->HTTP downgrade redirect rejected")
    check(_is_github_api_url("https://api.github.com/x")
          and not _is_github_api_url("https://evil.example/x")
          and not _is_github_api_url("http://api.github.com/x"),
          "_is_github_api_url requires https + the GitHub API host")
    for bad in ("https://evil.example/x", "http://api.github.com/x", "ftp://api.github.com/x"):
        try:
            _download_zip(bad, "TKN")
            rej = False
        except A.AdapterError:
            rej = True
        check(rej, "download rejects non-GitHub/insecure initial URL before sending creds: %s" % bad)

    # ---- ZIP path still supports receipt parsing + member SHA recompute ----
    def mkzip(entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n, b in entries.items():
                z.writestr(n, b)
        buf.seek(0)
        return zipfile.ZipFile(buf)

    rec, jn = _zip_read_receipt(mkzip({"receipt.json": json.dumps(_receipt(RPM, 11, "pkg-%s-9" % RPM, D1, [RM]))}))
    check(jn == 1 and rec and rec.get("cell_id") == RPM, "zip receipt parsing works")
    rec, jn = _zip_read_receipt(mkzip({"a.txt": "x"}))
    check(rec is None and jn == 0, "zip without receipt.json -> json_count 0")
    body = b"pkgbytes-xyz"
    check(_zip_member_sha(mkzip({"p.rpm": body})) == hashlib.sha256(body).hexdigest(), "zip member SHA recompute works")
    check(_zip_member_sha(mkzip({"a": "1", "b": "2"})) is None, "zip member SHA None when not exactly one file")

    # ---- fixture-drift: caller matrix vs planned_matrix.json (clarification #3) ----
    inc = _extract_caller_matrix(a.caller)          # stdlib-only; no PyYAML
    wf_dims = {(e["cell_id"], e["family"], e["os"], e["arch"]) for e in inc}
    fx = json.loads(Path(a.matrix).read_text())
    fx_entries = fx["rpm_matrix"]["include"] + fx["deb_matrix"]["include"]
    fx_dims = {(e["cell_id"], e["family"], e["os"], e["normalized_arch"]) for e in fx_entries}
    check(wf_dims == fx_dims, "caller matrix cell_ids/dimensions match planned_matrix.json (no drift)")

    print("\nSELFTEST: %s (%d checks failed)" % ("PASS" if not fails else "FAIL", len(fails)))
    return 0 if not fails else 1


def main(argv=None):
    p = argparse.ArgumentParser(description="mechanics-spike capture/selftest")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--matrix", required=True)
    c.add_argument("--downloads", required=True)
    c.add_argument("--evidence", required=True)
    c.add_argument("--download-outcome", dest="download_outcome", default="")
    c.set_defaults(func=cmd_capture)
    s = sub.add_parser("selftest")
    s.add_argument("--matrix", default=str(_REPO_ROOT / "spike/planned_matrix.json"))
    s.add_argument("--caller", default=str(_REPO_ROOT / ".github/workflows/spike-caller.yml"))
    s.set_defaults(func=cmd_selftest)
    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())

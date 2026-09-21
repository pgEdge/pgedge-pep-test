#!/usr/bin/env python3
"""Per-family all-or-nothing reconciliation for the PEP published-package REPLAY.

Family publication status is derived from THREE independent signals, and a
non-empty family is `success` ONLY when all three agree:
  1. the family's retrieval matrix-job RESULT is exactly ``success`` (a failed or
     cancelled matrix must NEVER become success just because some ledgers exist);
  2. every intended detector cell for that family has exactly one STRICTLY-VALID
     per-cell success ledger (schema/type/membership checked; nothing is filtered
     away before it is validated); and
  3. there are no global ledger faults (unparseable, non-object, wrong-schema,
     malformed, or unknown-family records anywhere in the set).
A genuinely absent family (no intended cells and no records claiming it) is
`skipped`, consistent with the publication-result contract the cert-plan reducer
consumes. Any deviation -> `failure`, fail closed.

Also composes the certification inputs AFTER reconciliation:
  * release_intent (simulated=false -- the retrieved packages are real; see note);
  * publication_results (per-family, from reconciliation);
  * a SEPARATE, allowlisted, scalar pep-replay-metadata/1 audit artifact.

Replay truthfulness: simulated=false is required by strict full-mode eligibility
because the packages are real, already-published artifacts. This run does NOT
build or publish anything; publication_results are SYNTHESIZED from verified
repository availability. That fact lives only in the replay metadata / workflow
identity, never as an invented field inside release_intent.

Stdlib only, plus the single shared cell-id grammar from pep_staging_fetch
(imported so there is ONE parser; both live in utillities/ on the path).
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

from pep_staging_fetch import parse_cell_identity, FetchError

FAMILIES = ("rpm", "deb")
PUB_SUCCESS, PUB_FAILURE, PUB_SKIPPED = "success", "failure", "skipped"
_JOB_RESULTS = ("success", "failure", "cancelled", "skipped", "")   # '' = job did not run
REPLAY_METADATA_SCHEMA = "pep-replay-metadata/1"
LEDGER_SCHEMA = "pep-replay-ledger/1"
_LEDGER_KEYS = frozenset({"schema", "cell_id", "family", "verified", "receipt_artifact_name"})

# Identity input patterns (scalar, conservative -- reject anything that could
# break a script or leak an unsafe value into evidence).
_RE_COMPONENT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_RE_VERSION = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9.~_+]{0,63}\Z")
_RE_BUILDNUM = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._]{0,63}\Z")
_RE_TAG = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+~/-]{0,63}\Z")
_CHANNELS = ("release", "staging", "daily")
# A conservative Docker image reference: registry/repo[:tag][@digest]. Must NOT
# begin with '-' (option injection) and must have no whitespace/shell metachars.
_RE_IMAGE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}\Z")


class ReconcileError(Exception):
    """A malformed input the caller must fix (matrices/identity) -- not a per-family
    reconciliation outcome, which is always returned and never raised."""


def _nonblank_unpadded(value):
    return isinstance(value, str) and value != "" and value == value.strip()


def parse_include(matrix, what):
    """STRICT: require a JSON object with an explicit ``include`` list. An absent
    family is represented ONLY as ``{"include":[]}``; blank strings, ``null`` and
    ``{}`` are rejected (never silently treated as empty). Returns the include list."""
    if matrix is None:
        raise ReconcileError("%s must be a JSON object (got null)" % what)
    if isinstance(matrix, str):
        if not matrix.strip():
            raise ReconcileError("%s must be a JSON object (got blank)" % what)
        try:
            matrix = json.loads(matrix)
        except (ValueError, TypeError):
            raise ReconcileError("%s is not valid JSON" % what)
    if not isinstance(matrix, dict):
        raise ReconcileError("%s must be a JSON object" % what)
    include = matrix.get("include")
    if not isinstance(include, list):
        raise ReconcileError("%s must have an explicit 'include' list "
                             "(absent family must be {\"include\":[]})" % what)
    return include


def cell_ids_from_matrix(matrix, what):
    """Strictly parse a matrix and return its cell_ids (within-family dup-checked).
    Used by reconciliation, which only needs the intended cell_ids (full per-cell
    consistency is enforced by validate_matrices at the plan stage)."""
    include = parse_include(matrix, what)
    ids = []
    for i, cell in enumerate(include):
        if not isinstance(cell, dict):
            raise ReconcileError("%s.include[%d] is not an object" % (what, i))
        cid = cell.get("cell_id")
        if not _nonblank_unpadded(cid):
            raise ReconcileError("%s.include[%d].cell_id must be a nonblank, unpadded string" % (what, i))
        ids.append(cid)
    dups = sorted(c for c, n in Counter(ids).items() if n > 1)
    if dups:
        raise ReconcileError("%s has duplicate cell_id(s): %s" % (what, dups))
    return ids


def _cell_identity(cell_id):
    try:
        return parse_cell_identity(cell_id)
    except FetchError as e:
        raise ReconcileError("cell_id %r: %s" % (cell_id, e))


def validate_cell(cell, expected_family, what, i):
    """Full consistency validation for ONE detector cell. Returns its cell_id.

    Requires every transport field the workflow relies on (cell_id, family, os,
    normalized_arch, arch, image) as nonblank, unpadded strings; verifies they
    AGREE with the cell identity and with this family matrix; validates the image
    (rejecting option-like values that begin with '-'); and rejects any
    PG-coupling metadata that contradicts the decoupled-only boundary. Generic --
    no hardcoded OS list or fixed cells; everything is consistency-driven."""
    where = "%s.include[%d]" % (what, i)
    if not isinstance(cell, dict):
        raise ReconcileError("%s is not an object" % where)
    for f in ("cell_id", "family", "os", "normalized_arch", "arch", "image"):
        if not _nonblank_unpadded(cell.get(f)):
            raise ReconcileError("%s.%s must be a nonblank, unpadded string" % (where, f))
    cid = cell["cell_id"]
    ident = _cell_identity(cid)               # rejects malformed + PG-coupled cell_id
    if expected_family not in FAMILIES:
        raise ReconcileError("%s internal: bad expected_family" % where)
    if ident["family"] != expected_family:
        raise ReconcileError("%s cell_id family %s is not in the %s matrix"
                             % (where, ident["family"], expected_family))
    if cell["family"] != ident["family"]:
        raise ReconcileError("%s family %r disagrees with cell_id (%s)" % (where, cell["family"], ident["family"]))
    if cell["os"] != ident["os_token"]:
        raise ReconcileError("%s os %r disagrees with cell_id (%s)" % (where, cell["os"], ident["os_token"]))
    if cell["normalized_arch"] != ident["arch"]:
        raise ReconcileError("%s normalized_arch %r disagrees with cell_id (%s)" % (where, cell["normalized_arch"], ident["arch"]))
    if cell["arch"] != ident["arch"]:
        raise ReconcileError("%s arch %r disagrees with cell_id (%s)" % (where, cell["arch"], ident["arch"]))
    if not _RE_IMAGE.match(cell["image"]):
        raise ReconcileError("%s image %r is unsafe or option-like" % (where, cell["image"]))
    # AUTHORITATIVE build-PG identity is ONLY the explicit detector fields
    # pg_coupled / build_pg_major / build_pg_version (per the committed cert adapter).
    # For a decoupled package these must be present and exactly false/null/null. The
    # cell_id was already required to have no `.pg<major>` segment above.
    # Legacy pg_major/pg_version may carry REPRESENTATIVE values even for a decoupled
    # package, and per_pg/pg_in_name are producer hints -- none of them are the
    # certification identity contract, so none are used to decide coupling.
    if cell.get("pg_coupled") is not False:
        raise ReconcileError("%s pg_coupled must be present and exactly boolean false (got %r)"
                             % (where, cell.get("pg_coupled")))
    if "build_pg_major" not in cell or cell["build_pg_major"] is not None:
        raise ReconcileError("%s build_pg_major must be present and exactly null (got %r)"
                             % (where, cell.get("build_pg_major")))
    if "build_pg_version" not in cell or cell["build_pg_version"] is not None:
        raise ReconcileError("%s build_pg_version must be present and exactly null (got %r)"
                             % (where, cell.get("build_pg_version")))
    return cid


def validate_matrices(rpm_matrix, deb_matrix):
    """Full plan-stage validation of BOTH matrices before any fan-out. Returns
    {has_rpm, has_deb, rpm_count, deb_count}. Raises on any inconsistency, unsafe
    value, contradictory coupling, or duplicate cell_id within OR across families."""
    all_ids = []
    counts = {}
    for matrix, fam in ((rpm_matrix, "rpm"), (deb_matrix, "deb")):
        include = parse_include(matrix, "%s_matrix" % fam)
        ids = [validate_cell(cell, fam, "%s_matrix" % fam, i) for i, cell in enumerate(include)]
        counts[fam] = len(ids)
        all_ids.extend(ids)
    cross = sorted(c for c, n in Counter(all_ids).items() if n > 1)
    if cross:
        raise ReconcileError("duplicate cell_id(s) across matrices: %s" % cross)
    return {"has_rpm": counts["rpm"] > 0, "has_deb": counts["deb"] > 0,
            "rpm_count": counts["rpm"], "deb_count": counts["deb"]}


def _classify_ledger(entry):
    """Classify ONE ledger entry WITHOUT discarding it first. `entry` is either a
    structural fault ({"source", "structural_fault"}) or {"source","parse_ok",
    "parsed"}. Returns (ok, family, cell_id, fault_reason). A fault (ok=False) is a
    GLOBAL fault the reconciliation cannot trust."""
    if entry.get("structural_fault"):
        return False, None, None, entry["structural_fault"]
    if not entry.get("parse_ok"):
        return False, None, None, "unparseable_json"
    rec = entry.get("parsed")
    if not isinstance(rec, dict):
        return False, None, None, "not_an_object"
    if set(rec.keys()) != _LEDGER_KEYS:
        return False, None, None, "unexpected_or_missing_fields"
    if rec.get("schema") != LEDGER_SCHEMA:
        return False, None, None, "wrong_schema"
    cid = rec.get("cell_id")
    fam = rec.get("family")
    if not _nonblank_unpadded(cid):
        return False, None, None, "bad_cell_id"
    if fam not in FAMILIES:
        return False, None, None, "unknown_family"
    if entry.get("source") != cid + ".json":
        return False, fam, cid, "source_filename_mismatch"
    if rec.get("verified") is not True:
        return False, fam, cid, "not_verified"
    rn = rec.get("receipt_artifact_name")
    if not _nonblank_unpadded(rn):
        return False, fam, cid, "missing_receipt"
    if rn != "pep-receipt-" + cid:
        return False, fam, cid, "receipt_name_mismatch"
    return True, fam, cid, None


def reconcile_family(family, intended_cell_ids, valid_cells, has_global_faults, matrix_result):
    """Per-family result. `valid_cells` = list of cell_ids of STRICTLY-VALID ledgers
    for this family. status in success|failure|skipped."""
    intended = sorted(set(intended_cell_ids))
    intended_set = set(intended)
    reasons = []

    by_cell = Counter(valid_cells)
    duplicates = sorted(c for c, n in by_cell.items() if n > 1)
    unexpected = sorted(c for c in by_cell if c not in intended_set)
    missing = sorted(c for c in intended_set if by_cell.get(c, 0) == 0)
    if duplicates:
        reasons.append("duplicate ledger(s): %s" % duplicates)
    if unexpected:
        reasons.append("unexpected cell(s): %s" % unexpected)
    if missing:
        reasons.append("missing verified retrieval: %s" % missing)

    if not intended:
        # Absent family: skipped, unless records claim it (a fault).
        if by_cell:
            return _fam(family, PUB_FAILURE, intended, by_cell,
                        reasons + ["ledger records present for an absent family"], matrix_result)
        return _fam(family, PUB_SKIPPED, intended, by_cell, reasons, matrix_result)

    # Non-empty family: success requires clean ledgers, a successful matrix job,
    # and no global faults anywhere.
    if matrix_result != "success":
        reasons.append("retrieval matrix-job result is %r (require success)" % (matrix_result or "<none>"))
    if has_global_faults:
        reasons.append("global ledger fault(s) present")
    status = PUB_FAILURE if reasons else PUB_SUCCESS
    return _fam(family, status, intended, by_cell, reasons, matrix_result)


def _fam(family, status, intended, by_cell, reasons, matrix_result):
    return {"family": family, "status": status, "intended_cells": intended,
            "intended_count": len(intended),
            "verified_count": len([c for c in intended if by_cell.get(c, 0) == 1]),
            "matrix_result": matrix_result, "reasons": reasons}


def reconcile(rpm_matrix, deb_matrix, ledger_entries, rpm_result="", deb_result=""):
    """Reconcile both families. `ledger_entries` is a list of
    {"source","parse_ok","parsed"} (NOT pre-filtered)."""
    if not isinstance(ledger_entries, list):
        raise ReconcileError("ledger_entries must be a list")
    for r, name in ((rpm_result, "rpm"), (deb_result, "deb")):
        if r not in _JOB_RESULTS:
            raise ReconcileError("%s matrix-job result %r is not a known conclusion" % (name, r))
    rpm_cells = cell_ids_from_matrix(rpm_matrix, "rpm_matrix")
    deb_cells = cell_ids_from_matrix(deb_matrix, "deb_matrix")

    valid = {"rpm": [], "deb": []}
    faults = []
    for entry in ledger_entries:
        ok, fam, cid, reason = _classify_ledger(entry)
        if ok:
            valid[fam].append(cid)
        else:
            faults.append({"source": entry.get("source"), "reason": reason,
                           "family": fam, "cell_id": cid})
    has_global_faults = len(faults) > 0

    rpm_res = reconcile_family("rpm", rpm_cells, valid["rpm"], has_global_faults, rpm_result)
    deb_res = reconcile_family("deb", deb_cells, valid["deb"], has_global_faults, deb_result)
    return {
        "publication_results": {"rpm": rpm_res["status"], "deb": deb_res["status"]},
        "per_family": {"rpm": rpm_res, "deb": deb_res},
        "cell_counts": {"rpm": len(rpm_cells), "deb": len(deb_cells),
                        "total": len(rpm_cells) + len(deb_cells)},
        "ledger_faults": faults,
        "expected_cells": sorted(set(rpm_cells) | set(deb_cells)),
    }


def family_presence(rpm_matrix, deb_matrix):
    """Full validation of both matrices (see validate_matrices); returns presence +
    counts. Rejects malformed/inconsistent/unsafe matrices BEFORE any retrieval."""
    return validate_matrices(rpm_matrix, deb_matrix)


def validate_from_env(env):
    """Plan-job entrypoint: validate BOTH matrices AND the release identity from env
    (reusing the one identity validator), so malformed release identity fails before
    fan-out rather than after package retrieval. Emits presence/counts."""
    validate_identity({
        "logical_component": env.get("LOGICAL_COMPONENT", ""),
        "intended_version": env.get("VERSION", ""),
        "intended_buildnum": env.get("BUILDNUM", ""),
        "effective_tag": env.get("TAG", ""),
        "channel": env.get("CHANNEL", ""),
    })
    presence = validate_matrices(env.get("RPM_MATRIX", ""), env.get("DEB_MATRIX", ""))
    gho = env.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a") as fh:
            fh.write("has_rpm=%s\n" % ("true" if presence["has_rpm"] else "false"))
            fh.write("has_deb=%s\n" % ("true" if presence["has_deb"] else "false"))
            fh.write("rpm_count=%d\n" % presence["rpm_count"])
            fh.write("deb_count=%d\n" % presence["deb_count"])
    return presence


def _require_scalar(value, rx, field):
    if not isinstance(value, str) or not rx.match(value):
        raise ReconcileError("release identity field %r is missing or unsafe" % field)


def validate_identity(identity):
    _require_scalar(identity.get("logical_component"), _RE_COMPONENT, "logical_component")
    _require_scalar(identity.get("intended_version"), _RE_VERSION, "intended_version")
    _require_scalar(identity.get("intended_buildnum"), _RE_BUILDNUM, "intended_buildnum")
    _require_scalar(identity.get("effective_tag"), _RE_TAG, "effective_tag")
    if identity.get("channel") not in _CHANNELS:
        raise ReconcileError("channel must be one of %s" % (_CHANNELS,))


def compose_release_intent(identity):
    """release_intent with simulated=false and NO replay-only fields."""
    validate_identity(identity)
    return {
        "logical_component": identity["logical_component"],
        "intended_version": identity["intended_version"],
        "intended_buildnum": identity["intended_buildnum"],
        "effective_tag": identity["effective_tag"],
        "channel": identity["channel"],
        "simulated": False,
    }


def _scalar(v):
    return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)


def compose_replay_metadata(identity, reconciled, provenance):
    """Separate audit artifact. Allowlisted + scalar; no URLs/creds/paths."""
    pf = reconciled["per_family"]
    prov = {k: _scalar(provenance.get(k)) for k in
            ("workflow", "workflow_ref", "run_id", "run_number", "run_attempt", "repository")}
    prov["replay"] = True
    return {
        "schema": REPLAY_METADATA_SCHEMA,
        "replay": True,
        "note": ("Controlled published-package replay: packages were retrieved from an existing "
                 "repository channel and tested; this run did not build or publish anything. "
                 "publication_results are synthesized from verified repository availability."),
        "logical_component": identity["logical_component"],
        "intended_version": identity["intended_version"],
        "intended_buildnum": identity["intended_buildnum"],
        "effective_tag": identity["effective_tag"],
        "channel": identity["channel"],
        "detector_cell_counts": reconciled["cell_counts"],
        "per_family_retrieval": {
            fam: {"status": pf[fam]["status"], "intended": pf[fam]["intended_count"],
                  "verified": pf[fam]["verified_count"], "matrix_result": pf[fam]["matrix_result"]}
            for fam in ("rpm", "deb")},
        "ledger_fault_count": len(reconciled["ledger_faults"]),
        "provenance": prov,
    }


# --- env-driven entrypoint (no shell interpolation, no cwd-dependent imports) --
def _load_ledger_entries(ledger_dir):
    """Account for EVERY entry in the ledger dir. Anything that is not a top-level
    regular, non-symlink ``.json`` file becomes a structural FAULT (never silently
    ignored)."""
    entries = []
    if not ledger_dir or not os.path.isdir(ledger_dir):
        return entries
    for name in sorted(os.listdir(ledger_dir)):
        path = os.path.join(ledger_dir, name)
        if os.path.islink(path):
            entries.append({"source": name, "structural_fault": "symlink_entry"})
        elif os.path.isdir(path):
            entries.append({"source": name, "structural_fault": "directory_entry"})
        elif not os.path.isfile(path):
            entries.append({"source": name, "structural_fault": "non_regular_entry"})
        elif not name.endswith(".json"):
            entries.append({"source": name, "structural_fault": "non_json_entry"})
        else:
            try:
                with open(path) as fh:
                    entries.append({"source": name, "parse_ok": True, "parsed": json.load(fh)})
            except (ValueError, OSError):
                entries.append({"source": name, "parse_ok": False, "parsed": None})
    return entries


def run_from_env(env):
    """Read every value from `env` (data, never source), reconcile, and write outputs.
    Returns (exit_code, summary_dict)."""
    def need(k):
        v = env.get(k)
        if v is None or v == "":
            raise ReconcileError("required env %s is missing" % k)
        return v

    out_dir = need("OUT_DIR")
    identity = {
        "logical_component": env.get("LOGICAL_COMPONENT", ""),
        "intended_version": env.get("VERSION", ""),
        "intended_buildnum": env.get("BUILDNUM", ""),
        "effective_tag": env.get("TAG", ""),
        "channel": env.get("CHANNEL", ""),
    }
    reconciled = reconcile(
        env.get("RPM_MATRIX", ""), env.get("DEB_MATRIX", ""),
        _load_ledger_entries(env.get("LEDGER_DIR", "")),
        env.get("RPM_JOB_RESULT", ""), env.get("DEB_JOB_RESULT", ""))
    release_intent = compose_release_intent(identity)
    provenance = {k.lower().replace("github_", ""): env.get(k, "") for k in
                  ("GITHUB_WORKFLOW", "GITHUB_WORKFLOW_REF", "GITHUB_RUN_ID",
                   "GITHUB_RUN_NUMBER", "GITHUB_RUN_ATTEMPT", "GITHUB_REPOSITORY")}
    replay_metadata = compose_replay_metadata(identity, reconciled, provenance)

    os.makedirs(out_dir, exist_ok=True)
    _w = lambda n, o, **k: json.dump(o, open(os.path.join(out_dir, n), "w"), **k)
    _w("release_intent.json", release_intent, sort_keys=True)
    _w("publication_results.json", reconciled["publication_results"], sort_keys=True)
    _w("replay-metadata.json", replay_metadata, indent=2, sort_keys=True)
    _w("reconciliation.json", reconciled, indent=2, sort_keys=True)

    gho = env.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a") as fh:
            fh.write("release_intent=%s\n" % json.dumps(release_intent, separators=(",", ":")))
            fh.write("publication_results=%s\n" % json.dumps(reconciled["publication_results"], separators=(",", ":")))
            fh.write("expected_cells=%s\n" % json.dumps(reconciled["expected_cells"], separators=(",", ":")))
    # Evidence is fully written above. A GLOBAL ledger fault makes the run's evidence
    # untrustworthy, so the step FAILS (exit 4) -- this also guarantees a fault can
    # never surface as an apparently clean all-skipped reconciliation. Zero ledgers
    # with no faults is NOT a fault: it exits 0 with a truthful failure/skipped result.
    faults = len(reconciled["ledger_faults"])
    return (4 if faults else 0), {"publication_results": reconciled["publication_results"],
                                  "cell_counts": reconciled["cell_counts"], "ledger_faults": faults}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(description="Replay reconciliation (env-driven).")
    ap.add_argument("mode", nargs="?", default="reconcile", choices=["reconcile", "validate"],
                    help="validate = plan-job matrix validation; reconcile (default) = full reconcile")
    args = ap.parse_args(argv)
    try:
        if args.mode == "validate":
            presence = validate_from_env(os.environ)
            sys.stdout.write(json.dumps(presence, sort_keys=True) + "\n")
            return 0
        rc, summary = run_from_env(os.environ)
    except ReconcileError as e:
        sys.stderr.write("::error::pep_replay_reconcile: %s\n" % e)
        return 3
    sys.stdout.write(json.dumps(summary, sort_keys=True) + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())

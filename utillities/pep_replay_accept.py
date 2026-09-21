#!/usr/bin/env python3
"""Nested-marker acceptance for the PEP published-package REPLAY.

Because the retrieval + receipt jobs run NESTED inside the reusable replay
workflow (not at the top level), this module turns "did pep-capture actually see
those same-run [pep-cell:<cell_id>] jobs and their artifacts through the current
run's Jobs/Artifacts API" into an AUTOMATED acceptance condition.

pep-certify does not re-expose capture counts as workflow outputs, so acceptance
reads the capture-evidence/1 and cert-plan/1 EVIDENCE ARTIFACTS the run produced:

  select_current_run_artifact()  -- from the run's paginated Artifacts API listing,
      pick the ONE non-expired artifact whose name is exactly the expected
      current-run/attempt name; zero or many is a hard failure (never a prefix-only
      guess). The caller downloads it by immutable id and validates its content.

  assert_capture_evidence()      -- schema, current-run provenance identity, and:
      planned_cells == intended count, accepted_receipt_cells == intended count,
      rejected/ambiguous/absent == 0, every intended cell present exactly once with
      verdict 'accepted', and no unexpected cell.

  assert_cert_plan()             -- schema + every intended cell build_state ==
      'available' (and no unexpected cell).

Stdlib only. Returns problem lists; the CLI exits non-zero if any are non-empty.
"""
import argparse
import json
import sys
import zipfile
from collections import Counter

CAPTURE_EVIDENCE_SCHEMA = "capture-evidence/1"
CERT_PLAN_SCHEMA = "cert-plan/1"
_KIND_PREFIX = {"capture-evidence": "pep-capture-evidence", "cert-plan": "pep-cert-plan"}
# The exact root payload filename each evidence kind must contain.
_KIND_PAYLOAD = {"capture-evidence": "capture-evidence.json", "cert-plan": "cert-plan.json"}
_EXPECTED_PAYLOADS = frozenset(_KIND_PAYLOAD.values())


class AcceptError(Exception):
    """Malformed acceptance input the caller must fix."""


def expected_artifact_name(kind, run_number, run_attempt):
    prefix = _KIND_PREFIX.get(kind)
    if prefix is None:
        raise AcceptError("unknown artifact kind %r" % kind)
    return "%s-r%s-a%s" % (prefix, run_number, run_attempt)


def _positive_int_id(value):
    """Return value as a positive int, or None. REST artifact ids must be ACTUAL
    positive integers: booleans (an int subclass) and numeric STRINGS are rejected."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _nonneg_int(value):
    """True iff value is an actual non-negative integer (booleans excluded)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def select_current_run_artifact(artifacts, kind, run_number, run_attempt):
    """Return the single matching artifact record, or raise AcceptError. Selection
    requires the EXACT current-run/attempt name, ``expired`` exactly ``False`` (a
    missing/true/other value is rejected), and a positive-integer id -- so a prior
    attempt's, another run's, expired, or malformed artifact can never slip in."""
    if not isinstance(artifacts, list):
        raise AcceptError("artifacts listing must be a list")
    want = expected_artifact_name(kind, run_number, run_attempt)
    matches = []
    for a in artifacts:
        if not isinstance(a, dict):
            continue
        if a.get("name") == want and a.get("expired") is False and _positive_int_id(a.get("id")) is not None:
            matches.append(a)
    if not matches:
        raise AcceptError("no non-expired artifact with a positive id named %r in this run" % want)
    if len(matches) > 1:
        raise AcceptError("ambiguous: %d artifacts named %r in this run" % (len(matches), want))
    a = dict(matches[0])
    a["id"] = _positive_int_id(a["id"])
    return a


def read_single_root_json(zip_path, expected_name):
    """Safely read the evidence payload from an artifact ZIP. The caller states the
    EXPECTED payload name (``capture-evidence.json`` or ``cert-plan.json``); the ZIP
    must contain EXACTLY ONE entry, named EXACTLY that, at the root, a regular
    non-symlink file. Any extra entry, a differently-named (even correctly-shaped)
    document, a nested/absolute/traversal path, a symlink, a directory, or invalid
    JSON is rejected deterministically. Returns the parsed object."""
    if expected_name not in _EXPECTED_PAYLOADS:
        raise AcceptError("internal: unknown expected payload name %r" % expected_name)
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise AcceptError("artifact is not a readable ZIP")
    with zf:
        infos = zf.infolist()
        if len(infos) != 1:
            raise AcceptError("expected exactly one entry in ZIP, found %d" % len(infos))
        zi = infos[0]
        name = zi.filename
        if name != expected_name:
            raise AcceptError("ZIP entry %r is not the expected payload %r" % (name, expected_name))
        if name.startswith("/") or "\\" in name or "/" in name or ".." in name.split("/"):
            raise AcceptError("unsafe path in ZIP: %r" % name)
        if (zi.external_attr >> 16) & 0o170000 == 0o120000:
            raise AcceptError("symlink entry not allowed in ZIP: %r" % name)
        if zi.is_dir():
            raise AcceptError("directory entry not allowed in ZIP: %r" % name)
        try:
            return json.loads(zf.read(name))
        except (ValueError, OSError):
            raise AcceptError("payload %r is not valid JSON" % name)


def _prov_matches(provenance, run_id, run_attempt, repository):
    problems = []
    if str(provenance.get("run_id")) != str(run_id):
        problems.append("provenance.run_id %r != current run %r" % (provenance.get("run_id"), run_id))
    if str(provenance.get("run_attempt")) != str(run_attempt):
        problems.append("provenance.run_attempt %r != current attempt %r"
                        % (provenance.get("run_attempt"), run_attempt))
    if repository is not None and provenance.get("repository") != repository:
        problems.append("provenance.repository %r != current repo %r"
                        % (provenance.get("repository"), repository))
    return problems


def assert_capture_evidence(doc, expected_cell_ids, run_id, run_attempt, repository=None):
    """Return a list of problems (empty == acceptance passed)."""
    problems = []
    expected = set(expected_cell_ids)
    n = len(expected)
    if not isinstance(doc, dict):
        return ["capture-evidence is not an object"]
    if doc.get("schema") != CAPTURE_EVIDENCE_SCHEMA:
        problems.append("schema is %r, expected %r" % (doc.get("schema"), CAPTURE_EVIDENCE_SCHEMA))
    prov = doc.get("provenance")
    if not isinstance(prov, dict):
        problems.append("provenance missing")
    else:
        problems += _prov_matches(prov, run_id, run_attempt, repository)

    counts = doc.get("counts")
    if not isinstance(counts, dict):
        problems.append("counts missing")
    else:
        count_keys = ("planned_cells", "accepted_receipt_cells", "rejected_receipt_cells",
                      "ambiguous_receipt_cells", "absent_receipt_cells")
        typed_ok = True
        for k in count_keys:
            if not _nonneg_int(counts.get(k)):
                problems.append("counts.%s must be a non-negative integer (got %r)" % (k, counts.get(k)))
                typed_ok = False
        if typed_ok:
            if counts["planned_cells"] != n:
                problems.append("planned_cells %d != intended %d" % (counts["planned_cells"], n))
            if counts["accepted_receipt_cells"] != n:
                problems.append("accepted_receipt_cells %d != intended %d" % (counts["accepted_receipt_cells"], n))
            for k in ("rejected_receipt_cells", "ambiguous_receipt_cells", "absent_receipt_cells"):
                if counts[k] != 0:
                    problems.append("%s is %d, expected 0" % (k, counts[k]))

    cells = doc.get("cells")
    if not isinstance(cells, list):
        problems.append("cells missing")
    else:
        seen = Counter()
        for c in cells:
            if not isinstance(c, dict):
                problems.append("a cell entry is not an object")
                continue
            cid = c.get("cell_id")
            seen[cid] += 1
            if cid not in expected:
                problems.append("unexpected cell %r" % cid)
            elif c.get("verdict") != "accepted":
                problems.append("cell %r verdict is %r, expected accepted" % (cid, c.get("verdict")))
        for cid in sorted(expected):
            if seen.get(cid, 0) == 0:
                problems.append("intended cell %r absent from capture evidence" % cid)
            elif seen[cid] > 1:
                problems.append("intended cell %r appears %d times" % (cid, seen[cid]))
    return problems


def assert_cert_plan(doc, expected_cell_ids):
    """Return a list of problems (empty == every intended cell build_state=available)."""
    problems = []
    expected = set(expected_cell_ids)
    if not isinstance(doc, dict):
        return ["cert-plan is not an object"]
    if doc.get("schema") != CERT_PLAN_SCHEMA:
        problems.append("schema is %r, expected %r" % (doc.get("schema"), CERT_PLAN_SCHEMA))
    if doc.get("plan_resolved") is not True:
        problems.append("cert-plan plan_resolved is %r, expected true (cells cannot be "
                        "accepted from an unresolved plan)" % doc.get("plan_resolved"))
    cells = doc.get("cells")
    if not isinstance(cells, list):
        return problems + ["cells missing"]
    seen = Counter()
    for c in cells:
        if not isinstance(c, dict):
            problems.append("a cert-plan cell is not an object")
            continue
        cid = c.get("cell_id")
        seen[cid] += 1
        if cid not in expected:
            problems.append("unexpected cert-plan cell %r" % cid)
        elif c.get("build_state") != "available":
            problems.append("cell %r build_state is %r, expected available" % (cid, c.get("build_state")))
    for cid in sorted(expected):
        if seen.get(cid, 0) == 0:
            problems.append("intended cell %r absent from cert-plan" % cid)
        elif seen[cid] > 1:
            problems.append("intended cell %r appears %d times in cert-plan" % (cid, seen[cid]))
    return problems


# --- CLI --------------------------------------------------------------------
def _load(path):
    return json.load(open(path))


def _cmd_select(args):
    artifacts = _load(args.artifacts_listing)
    if isinstance(artifacts, dict) and "artifacts" in artifacts:
        artifacts = artifacts["artifacts"]
    a = select_current_run_artifact(artifacts, args.kind, args.run_number, args.run_attempt)
    sys.stdout.write(str(a["id"]))
    return 0


def validate_expected_cells(cells):
    """expected cells must be a unique list of nonblank, unpadded strings."""
    if not isinstance(cells, list):
        return ["expected cells must be a list"]
    if not all(isinstance(c, str) and c and c == c.strip() for c in cells):
        return ["expected cells must be nonblank, unpadded strings"]
    if len(set(cells)) != len(cells):
        return ["expected cells must be unique"]
    return []


def _cmd_extract(args):
    payload = read_single_root_json(args.zip, _KIND_PAYLOAD[args.kind])
    with open(args.out, "w") as fh:
        json.dump(payload, fh)
    return 0


def _cmd_assert(args):
    expected = _load(args.expected_cells)
    if isinstance(expected, dict):
        expected = expected.get("cell_ids", [])
    ec_problems = validate_expected_cells(expected)
    if ec_problems:
        for p in ec_problems:
            sys.stderr.write("::error::nested-marker acceptance FAILED: %s\n" % p)
        return 1
    problems = []
    problems += assert_capture_evidence(_load(args.capture_evidence), expected,
                                        args.run_id, args.run_attempt, args.repository)
    problems += assert_cert_plan(_load(args.cert_plan), expected)
    if problems:
        for p in problems:
            sys.stderr.write("::error::nested-marker acceptance FAILED: %s\n" % p)
        return 1
    sys.stdout.write("nested-marker acceptance PASSED: %d intended cells accepted & available\n"
                     % len(expected))
    return 0


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(description="Nested-marker acceptance for the replay.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("select", help="Pick the one current-run/attempt evidence artifact id.")
    s.add_argument("--artifacts-listing", required=True)
    s.add_argument("--kind", required=True, choices=sorted(_KIND_PREFIX))
    s.add_argument("--run-number", required=True)
    s.add_argument("--run-attempt", required=True)
    s.set_defaults(func=_cmd_select)

    e = sub.add_parser("extract", help="Safely read the one expected root JSON from an evidence ZIP.")
    e.add_argument("--zip", required=True)
    e.add_argument("--kind", required=True, choices=sorted(_KIND_PAYLOAD))
    e.add_argument("--out", required=True)
    e.set_defaults(func=_cmd_extract)

    a = sub.add_parser("assert", help="Assert capture-evidence + cert-plan acceptance.")
    a.add_argument("--capture-evidence", required=True)
    a.add_argument("--cert-plan", required=True)
    a.add_argument("--expected-cells", required=True)
    a.add_argument("--run-id", required=True)
    a.add_argument("--run-attempt", required=True)
    a.add_argument("--repository", default=None)
    a.set_defaults(func=_cmd_assert)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except AcceptError as e:
        sys.stderr.write("::error::pep_replay_accept: %s\n" % e)
        return 3


if __name__ == "__main__":
    sys.exit(main())

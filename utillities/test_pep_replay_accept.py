"""Offline tests for pep_replay_accept (nested-marker acceptance + safe ZIP)."""
import tempfile
import zipfile

import pytest

import pep_replay_accept as A


CELLS = ["pepcell.v1.rpm.el-9.amd64.pkg", "pepcell.v1.deb.trixie.arm64.pkg"]


def _capture_evidence(cells=CELLS, run_id=5, run_attempt=1, repo="pgEdge/x"):
    n = len(cells)
    return {
        "schema": "capture-evidence/1",
        "provenance": {"run_id": run_id, "run_attempt": run_attempt, "repository": repo},
        "counts": {"planned_cells": n, "accepted_receipt_cells": n,
                   "rejected_receipt_cells": 0, "ambiguous_receipt_cells": 0,
                   "absent_receipt_cells": 0},
        "cells": [{"cell_id": c, "verdict": "accepted"} for c in cells],
    }


def _cert_plan(cells=CELLS, plan_resolved=True):
    return {"schema": "cert-plan/1", "plan_resolved": plan_resolved,
            "cells": [{"cell_id": c, "build_state": "available"} for c in cells]}


# --- artifact selection -----------------------------------------------------
def test_expected_artifact_name():
    assert A.expected_artifact_name("capture-evidence", 17, 1) == "pep-capture-evidence-r17-a1"
    assert A.expected_artifact_name("cert-plan", 3, 2) == "pep-cert-plan-r3-a2"


def test_select_exact_match():
    arts = [{"id": 9, "name": "pep-cert-plan-r17-a1", "expired": False},
            {"id": 8, "name": "pep-capture-evidence-r17-a1", "expired": False}]
    got = A.select_current_run_artifact(arts, "capture-evidence", 17, 1)
    assert got["id"] == 8


def test_select_none_raises():
    with pytest.raises(A.AcceptError):
        A.select_current_run_artifact([], "capture-evidence", 17, 1)


def test_select_ignores_expired_and_other_runs():
    arts = [{"id": 1, "name": "pep-capture-evidence-r17-a1", "expired": True},
            {"id": 2, "name": "pep-capture-evidence-r16-a1", "expired": False}]
    with pytest.raises(A.AcceptError):
        A.select_current_run_artifact(arts, "capture-evidence", 17, 1)


def test_select_ambiguous_raises():
    arts = [{"id": 1, "name": "pep-capture-evidence-r17-a1", "expired": False},
            {"id": 2, "name": "pep-capture-evidence-r17-a1", "expired": False}]
    with pytest.raises(A.AcceptError):
        A.select_current_run_artifact(arts, "capture-evidence", 17, 1)


@pytest.mark.parametrize("art", [
    {"id": 8, "name": "pep-capture-evidence-r17-a1"},                       # expired missing
    {"id": 8, "name": "pep-capture-evidence-r17-a1", "expired": "false"},   # expired not a bool
    {"id": 0, "name": "pep-capture-evidence-r17-a1", "expired": False},     # non-positive id
    {"id": -3, "name": "pep-capture-evidence-r17-a1", "expired": False},    # negative id
    {"id": True, "name": "pep-capture-evidence-r17-a1", "expired": False},  # bool id
    {"id": "8", "name": "pep-capture-evidence-r17-a1", "expired": False},   # numeric-string id
    {"name": "pep-capture-evidence-r17-a1", "expired": False},              # no id
])
def test_select_requires_expired_false_and_positive_int_id(art):
    with pytest.raises(A.AcceptError):
        A.select_current_run_artifact([art], "capture-evidence", 17, 1)


def test_select_returns_int_id():
    got = A.select_current_run_artifact(
        [{"id": 42, "name": "pep-cert-plan-r5-a2", "expired": False}], "cert-plan", 5, 2)
    assert got["id"] == 42


# --- safe ZIP extraction (told the expected payload name) -------------------
CE = "capture-evidence.json"


def _mkzip(entries, symlink=None):
    p = tempfile.mktemp(suffix=".zip")
    with zipfile.ZipFile(p, "w") as z:
        for name, data in entries:
            z.writestr(name, data)
        if symlink:
            zi = zipfile.ZipInfo(symlink[0])
            zi.external_attr = (0o120777 << 16)
            z.writestr(zi, symlink[1])
    return p


def test_read_single_root_json_ok():
    p = _mkzip([(CE, '{"schema":"capture-evidence/1"}')])
    assert A.read_single_root_json(p, CE) == {"schema": "capture-evidence/1"}


@pytest.mark.parametrize("mk,exp", [
    (lambda: _mkzip([("sub/" + CE, "{}")]), CE),                      # nested path
    (lambda: _mkzip([(CE, "{}"), ("extra.txt", "x")]), CE),          # extra entry
    (lambda: _mkzip([(CE, "{}"), ("cert-plan.json", "{}")]), CE),    # two json entries
    (lambda: _mkzip([("cert-plan.json", "{}")]), CE),                # correctly-shaped, wrong name
    (lambda: _mkzip([("a.txt", "x")]), CE),                          # wrong name / no json
    (lambda: _mkzip([("../" + CE, "{}")]), CE),                      # traversal
    (lambda: _mkzip([(CE, "{}")], symlink=(CE, "/etc/passwd")), CE),  # symlink present (extra entry)
    (lambda: _mkzip([(CE, "{not json")]), CE),                       # right name, bad json
])
def test_read_single_root_json_hostile_rejected(mk, exp):
    with pytest.raises(A.AcceptError):
        A.read_single_root_json(mk(), exp)


def test_read_single_root_json_lone_symlink_named_as_expected_rejected():
    p = tempfile.mktemp(suffix=".zip")
    with zipfile.ZipFile(p, "w") as z:
        zi = zipfile.ZipInfo(CE)
        zi.external_attr = (0o120777 << 16)
        z.writestr(zi, "/etc/passwd")
    with pytest.raises(A.AcceptError):
        A.read_single_root_json(p, CE)


def test_read_single_root_json_not_a_zip():
    p = tempfile.mktemp(suffix=".zip")
    with open(p, "w") as fh:
        fh.write("not a zip")
    with pytest.raises(A.AcceptError):
        A.read_single_root_json(p, CE)


# --- expected-cells validation ----------------------------------------------
@pytest.mark.parametrize("cells", [
    ["a", "a"],           # duplicate
    ["a", ""],            # blank
    ["a", " b"],          # padded
    ["a", 1],             # non-string
    "notalist",           # not a list
])
def test_validate_expected_cells_rejects(cells):
    assert A.validate_expected_cells(cells)


def test_validate_expected_cells_ok():
    assert A.validate_expected_cells(["a", "b"]) == []


# --- capture-evidence acceptance --------------------------------------------
def test_capture_evidence_passes():
    assert A.assert_capture_evidence(_capture_evidence(), CELLS, 5, 1, "pgEdge/x") == []


def test_capture_evidence_wrong_schema():
    d = _capture_evidence(); d["schema"] = "capture-evidence/2"
    assert A.assert_capture_evidence(d, CELLS, 5, 1, "pgEdge/x")


@pytest.mark.parametrize("rid,att,repo", [(999, 1, "pgEdge/x"), (5, 9, "pgEdge/x"), (5, 1, "pgEdge/other")])
def test_capture_evidence_wrong_provenance(rid, att, repo):
    problems = A.assert_capture_evidence(_capture_evidence(), CELLS, rid, att, repo)
    assert problems


def test_capture_evidence_count_mismatch():
    d = _capture_evidence(); d["counts"]["accepted_receipt_cells"] = 1
    assert A.assert_capture_evidence(d, CELLS, 5, 1, "pgEdge/x")


@pytest.mark.parametrize("k", ["rejected_receipt_cells", "ambiguous_receipt_cells", "absent_receipt_cells"])
def test_capture_evidence_nonzero_bad_counts(k):
    d = _capture_evidence(); d["counts"][k] = 1
    assert A.assert_capture_evidence(d, CELLS, 5, 1, "pgEdge/x")


@pytest.mark.parametrize("k", ["planned_cells", "accepted_receipt_cells", "rejected_receipt_cells"])
def test_capture_evidence_boolean_or_noninteger_counts_rejected(k):
    d = _capture_evidence(); d["counts"][k] = True   # bool must not satisfy an int count
    assert A.assert_capture_evidence(d, CELLS, 5, 1, "pgEdge/x")
    d2 = _capture_evidence(); d2["counts"][k] = "2"   # numeric string
    assert A.assert_capture_evidence(d2, CELLS, 5, 1, "pgEdge/x")


def test_capture_evidence_unexpected_cell():
    d = _capture_evidence(cells=CELLS + ["pepcell.v1.rpm.el-99.amd64.pkg"])
    d["counts"]["planned_cells"] = len(CELLS)  # keep intended count
    assert A.assert_capture_evidence(d, CELLS, 5, 1, "pgEdge/x")


def test_capture_evidence_missing_cell():
    d = _capture_evidence(cells=[CELLS[0]])
    d["counts"] = {"planned_cells": 2, "accepted_receipt_cells": 2,
                   "rejected_receipt_cells": 0, "ambiguous_receipt_cells": 0, "absent_receipt_cells": 0}
    assert A.assert_capture_evidence(d, CELLS, 5, 1, "pgEdge/x")


def test_capture_evidence_non_accepted_verdict():
    d = _capture_evidence(); d["cells"][0]["verdict"] = "rejected"
    assert A.assert_capture_evidence(d, CELLS, 5, 1, "pgEdge/x")


def test_capture_evidence_duplicate_cell():
    d = _capture_evidence()
    d["cells"].append({"cell_id": CELLS[0], "verdict": "accepted"})
    assert A.assert_capture_evidence(d, CELLS, 5, 1, "pgEdge/x")


# --- cert-plan acceptance ---------------------------------------------------
def test_cert_plan_passes():
    assert A.assert_cert_plan(_cert_plan(), CELLS) == []


def test_cert_plan_build_state_not_available():
    d = _cert_plan(); d["cells"][0]["build_state"] = "incomplete"
    assert A.assert_cert_plan(d, CELLS)


@pytest.mark.parametrize("pr", [False, None, "true", 1])
def test_cert_plan_unresolved_rejected(pr):
    assert A.assert_cert_plan(_cert_plan(plan_resolved=pr), CELLS)


def test_cert_plan_missing_and_unexpected():
    assert A.assert_cert_plan(_cert_plan(cells=[CELLS[0]]), CELLS)
    assert A.assert_cert_plan(_cert_plan(cells=CELLS + ["pepcell.v1.rpm.zzz.amd64.pkg"]), CELLS)


def test_cert_plan_duplicate():
    d = _cert_plan(); d["cells"].append({"cell_id": CELLS[0], "build_state": "available"})
    assert A.assert_cert_plan(d, CELLS)

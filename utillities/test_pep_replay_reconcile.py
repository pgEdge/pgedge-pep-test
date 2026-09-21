"""Offline tests for pep_replay_reconcile.

Covers strict matrix validation (validate_matrices: fields, agreement, unsafe
image, coupling, cross-matrix duplicates), the strict absent-family contract
({"include":[]} only), fail-closed ledger handling incl. structural faults + exact
filename/receipt-name, the three-signal family rule, adversarial identity inputs,
zero-ledger handling, replay-metadata sanitation, and rerun characterization.
"""
import json
import tempfile

import pytest

import pep_replay_reconcile as R

EMPTY = {"include": []}


def _cell(fam, os_t, arch, image=None):
    image = image or ("almalinux:9" if fam == "rpm" else "debian:trixie")
    # A real decoupled detector cell: the AUTHORITATIVE build-PG fields are
    # pg_coupled=false / build_pg_major=null / build_pg_version=null. Legacy
    # pg_major/pg_version/per_pg/pg_in_name are representative producer hints.
    return {"cell_id": "pepcell.v1.%s.%s.%s.pkg" % (fam, os_t, arch), "family": fam,
            "os": os_t, "normalized_arch": arch, "arch": arch, "image": image,
            "pg_coupled": False, "build_pg_major": None, "build_pg_version": None,
            "pg_major": "", "pg_version": "", "per_pg": "false", "pg_in_name": "false"}


RPM = {"include": [_cell("rpm", "el-9", "amd64"), _cell("rpm", "el-9", "arm64")]}
DEB = {"include": [_cell("deb", "trixie", "amd64", "debian:trixie")]}
RPM_IDS = [c["cell_id"] for c in RPM["include"]]
DEB_IDS = [c["cell_id"] for c in DEB["include"]]


def _entry(cell_id, family, verified=True, receipt=None, schema=R.LEDGER_SCHEMA,
           parse_ok=True, extra=None, drop=None, source=None, structural=None):
    if structural:
        return {"source": source or (cell_id + ".json"), "structural_fault": structural}
    rec = {"schema": schema, "cell_id": cell_id, "family": family, "verified": verified,
           "receipt_artifact_name": receipt if receipt is not None else "pep-receipt-" + cell_id}
    if extra:
        rec.update(extra)
    if drop:
        rec.pop(drop, None)
    return {"source": source or (cell_id + ".json"), "parse_ok": parse_ok,
            "parsed": rec if parse_ok else None}


def _full_rpm():
    return [_entry(c, "rpm") for c in RPM_IDS]


# --- strict matrix parsing ---------------------------------------------------
def test_cell_ids_absent_is_include_empty_only():
    assert R.cell_ids_from_matrix(EMPTY, "rpm") == []
    for blank in ["", "   ", None, {}, {"nope": []}]:
        with pytest.raises(R.ReconcileError):
            R.cell_ids_from_matrix(blank, "rpm")


def test_cell_ids_extract_and_dupcheck():
    assert R.cell_ids_from_matrix(RPM, "rpm") == RPM_IDS
    with pytest.raises(R.ReconcileError):
        R.cell_ids_from_matrix({"include": [{"cell_id": "a"}, {"cell_id": "a"}]}, "rpm")


# --- validate_matrices (plan stage) -----------------------------------------
def test_validate_matrices_ok():
    assert R.validate_matrices(RPM, DEB) == {"has_rpm": True, "has_deb": True,
                                             "rpm_count": 2, "deb_count": 1}
    assert R.validate_matrices(RPM, EMPTY)["has_deb"] is False


@pytest.mark.parametrize("mut", [
    lambda c: c.pop("image"),
    lambda c: c.update(image="-v/etc:/x"),           # option-like
    lambda c: c.update(image="bad image"),            # space
    lambda c: c.update(arch=" amd64"),                # padded
    lambda c: c.update(arch="arm64", normalized_arch="arm64"),  # arch disagrees cell_id
    lambda c: c.update(os="el-10"),                   # os disagrees cell_id
    lambda c: c.update(family="deb"),                 # family disagrees cell_id
    lambda c: c.update(cell_id="pepcell.v1.rpm.el-9.amd64.pg16.x"),  # coupled cell_id
    lambda c: c.update(cell_id="not-a-cell"),         # malformed cell_id
])
def test_validate_matrices_rejects_bad_cell(mut):
    c = _cell("rpm", "el-9", "amd64")
    mut(c)
    with pytest.raises(R.ReconcileError):
        R.validate_matrices({"include": [c]}, EMPTY)


# --- PG build-identity contract: ONLY pg_coupled/build_pg_major/build_pg_version
@pytest.mark.parametrize("mut", [
    lambda c: c.pop("pg_coupled"),                    # (1) missing explicit field
    lambda c: c.pop("build_pg_major"),               # (1) missing explicit field
    lambda c: c.pop("build_pg_version"),             # (1) missing explicit field
    lambda c: c.update(build_pg_major="16"),         # (2) non-null build_pg_major
    lambda c: c.update(build_pg_version="16.4"),     # (3) non-null build_pg_version
    lambda c: c.update(pg_coupled=True),             # (4) pg_coupled true
    lambda c: c.update(pg_coupled="false"),          # (4) non-boolean (string)
    lambda c: c.update(pg_coupled=0),                # (4) non-boolean (int 0)
    lambda c: c.update(pg_coupled=None),             # (4) non-boolean (null)
])
def test_validate_matrices_rejects_bad_pg_identity(mut):
    c = _cell("rpm", "el-9", "amd64")
    mut(c)
    with pytest.raises(R.ReconcileError):
        R.validate_matrices({"include": [c]}, EMPTY)


def test_decoupled_cell_with_representative_legacy_pg_is_valid():
    # (5) Representative legacy pg_major/pg_version on a truly decoupled cell must
    # NOT be promoted into build identity or rejected.
    c = _cell("rpm", "el-9", "amd64")
    c.update(pg_major="16", pg_version="16.4")
    assert R.validate_matrices({"include": [c]}, EMPTY)["rpm_count"] == 1


def test_legacy_per_pg_hints_do_not_override_explicit_identity():
    # (6) per_pg / pg_in_name are legacy producer hints, not the identity contract.
    c = _cell("rpm", "el-9", "amd64")
    c.update(per_pg="true", pg_in_name="true")
    assert R.validate_matrices({"include": [c]}, EMPTY)["rpm_count"] == 1


def test_validate_matrices_wrong_family_matrix():
    with pytest.raises(R.ReconcileError):
        R.validate_matrices({"include": [_cell("deb", "trixie", "amd64", "debian:trixie")]}, EMPTY)


def test_validate_matrices_cross_matrix_duplicate():
    dc = _cell("rpm", "el-9", "amd64")
    with pytest.raises(R.ReconcileError):
        R.validate_matrices({"include": [dc]}, {"include": [dict(dc, family="deb")]})


def test_family_presence_delegates_to_validation():
    assert R.family_presence(RPM, EMPTY)["has_rpm"] is True
    with pytest.raises(R.ReconcileError):
        R.family_presence("{bad", EMPTY)


# --- three-signal family rule ------------------------------------------------
def test_full_success_requires_all_three_signals():
    out = R.reconcile(RPM, DEB, _full_rpm() + [_entry(DEB_IDS[0], "deb")], "success", "success")
    assert out["publication_results"] == {"rpm": "success", "deb": "success"}
    assert out["ledger_faults"] == [] and out["expected_cells"] == sorted(RPM_IDS + DEB_IDS)


@pytest.mark.parametrize("res", ["failure", "cancelled", "skipped", ""])
def test_nonsuccess_matrix_result_never_success(res):
    out = R.reconcile(RPM, EMPTY, _full_rpm(), rpm_result=res, deb_result="skipped")
    assert out["publication_results"]["rpm"] == "failure"


def test_invalid_matrix_result_vocab_raises():
    with pytest.raises(R.ReconcileError):
        R.reconcile(RPM, EMPTY, _full_rpm(), rpm_result="weird", deb_result="skipped")


def test_absent_family_skipped():
    out = R.reconcile(EMPTY, EMPTY, [], "skipped", "skipped")
    assert out["publication_results"] == {"rpm": "skipped", "deb": "skipped"}


def test_records_for_absent_family_fail():
    out = R.reconcile(EMPTY, DEB, [_entry(RPM_IDS[0], "rpm"), _entry(DEB_IDS[0], "deb")],
                      "skipped", "success")
    assert out["publication_results"]["rpm"] == "failure"


# --- fail-closed ledger handling --------------------------------------------
def test_missing_partial_duplicate_unexpected_fail():
    assert R.reconcile(RPM, EMPTY, [_entry(RPM_IDS[0], "rpm")], "success", "skipped")["publication_results"]["rpm"] == "failure"
    assert R.reconcile(RPM, EMPTY, _full_rpm() + [_entry(RPM_IDS[0], "rpm")], "success", "skipped")["publication_results"]["rpm"] == "failure"
    assert R.reconcile(RPM, EMPTY, _full_rpm() + [_entry("pepcell.v1.rpm.el-99.amd64.pkg", "rpm")], "success", "skipped")["publication_results"]["rpm"] == "failure"


@pytest.mark.parametrize("mk", [
    lambda: _entry("x", "rpm", parse_ok=False),                                  # unparseable
    lambda: {"source": "x.json", "parse_ok": True, "parsed": ["not", "obj"]},     # non-object
    lambda: _entry(RPM_IDS[0], "rpm", schema="pep-replay-ledger/2"),             # wrong schema
    lambda: _entry(RPM_IDS[0], "rpm", extra={"surprise": 1}),                    # extra field
    lambda: _entry(RPM_IDS[0], "rpm", drop="receipt_artifact_name"),             # missing field
    lambda: _entry(RPM_IDS[0], "zzz"),                                           # unknown family
    lambda: _entry(RPM_IDS[0], "rpm", verified=False),                           # not verified
    lambda: _entry(RPM_IDS[0], "rpm", receipt=""),                               # blank receipt
    lambda: _entry(RPM_IDS[0], "rpm", receipt="wrong-name"),                     # receipt name mismatch
    lambda: _entry(RPM_IDS[0], "rpm", source="other.json"),                      # source filename mismatch
    lambda: _entry(RPM_IDS[0], "rpm", structural="directory_entry"),             # structural fault
    lambda: _entry(RPM_IDS[0], "rpm", structural="symlink_entry"),
    lambda: _entry(RPM_IDS[0], "rpm", structural="non_json_entry"),
])
def test_any_bad_ledger_fails_family_and_is_a_fault(mk):
    entries = [_entry(RPM_IDS[0], "rpm"), _entry(RPM_IDS[1], "rpm"), mk()]
    out = R.reconcile(RPM, EMPTY, entries, "success", "skipped")
    assert out["publication_results"]["rpm"] == "failure"
    assert len(out["ledger_faults"]) >= 1


def test_zero_ledgers_still_writes_truthful_result():
    out = R.reconcile(RPM, DEB, [], "success", "success")
    assert out["publication_results"] == {"rpm": "failure", "deb": "failure"}
    assert out["ledger_faults"] == []  # zero ledgers is NOT a fault


# --- rerun characterization -------------------------------------------------
def test_rerun_failed_partial_evidence_is_fail_closed():
    # "Re-run failed jobs": only some legs re-run under the new attempt, so the
    # current-attempt ledger set is INCOMPLETE -> failure, never a false green.
    out = R.reconcile(RPM, EMPTY, [_entry(RPM_IDS[0], "rpm")], "success", "skipped")
    assert out["publication_results"]["rpm"] == "failure"


# --- composition / identity validation --------------------------------------
IDENT = {"logical_component": "rag", "intended_version": "2.0.0", "intended_buildnum": "1",
         "effective_tag": "v2.0.0", "channel": "staging"}


def test_release_intent_simulated_false():
    ri = R.compose_release_intent(IDENT)
    assert ri["simulated"] is False
    assert set(ri) == {"logical_component", "intended_version", "intended_buildnum",
                       "effective_tag", "channel", "simulated"}


@pytest.mark.parametrize("field,val", [
    ("intended_version", '2.0.0"; rm -rf /'), ("logical_component", "rag\nimport os"),
    ("intended_buildnum", "1'''+__import__('os')"), ("effective_tag", "v2.0.0$(id)"),
    ("intended_version", "2.0\t0"), ("logical_component", "   "), ("channel", "prod"),
])
def test_release_intent_rejects_hostile(field, val):
    bad = dict(IDENT); bad[field] = val
    with pytest.raises(R.ReconcileError):
        R.compose_release_intent(bad)


def test_replay_metadata_sanitized():
    out = R.reconcile(RPM, DEB, _full_rpm() + [_entry(DEB_IDS[0], "deb")], "success", "success")
    md = R.compose_replay_metadata(IDENT, out, {"run_id": "1", "repository": "pgEdge/x",
                                                "extra_secret": "http://tok@h"})
    assert md["schema"] == R.REPLAY_METADATA_SCHEMA and md["replay"] is True
    assert set(md["provenance"]) == {"workflow", "workflow_ref", "run_id", "run_number",
                                     "run_attempt", "repository", "replay"}
    blob = json.dumps(md).lower()
    for leak in ("http://", "token", "password", "/private/", "/users/", "extra_secret"):
        assert leak not in blob


# --- env-driven run: data-only transport; faults fail the step --------------
def _env(**over):
    e = {"RPM_MATRIX": json.dumps(EMPTY), "DEB_MATRIX": json.dumps(EMPTY),
         "LOGICAL_COMPONENT": "rag", "VERSION": "2.0.0", "BUILDNUM": "1", "TAG": "v2.0.0",
         "CHANNEL": "staging", "LEDGER_DIR": "", "RPM_JOB_RESULT": "skipped", "DEB_JOB_RESULT": "skipped"}
    e.update(over)
    e["OUT_DIR"] = tempfile.mkdtemp()
    return e


def test_run_from_env_empty_families_exit0_skipped():
    rc, summary = R.run_from_env(_env())
    assert rc == 0 and summary["publication_results"] == {"rpm": "skipped", "deb": "skipped"}


def test_run_from_env_fault_exits_nonzero_but_writes_evidence():
    d = tempfile.mkdtemp()
    # one malformed ledger file on disk
    open(d + "/bad.json", "w").write("{not json")
    env = _env(RPM_MATRIX=json.dumps(RPM), RPM_JOB_RESULT="success", LEDGER_DIR=d)
    rc, summary = R.run_from_env(env)
    assert rc == 4 and summary["ledger_faults"] >= 1
    import os
    assert os.path.exists(env["OUT_DIR"] + "/reconciliation.json")  # evidence still written


@pytest.mark.parametrize("over", [
    {"VERSION": '2.0.0"; rm -rf /'}, {"LOGICAL_COMPONENT": "rag\nX"}, {"TAG": "v$(id)"},
    {"CHANNEL": "prod"}, {"BUILDNUM": "1;ls"}, {"RPM_MATRIX": "{bad json"},
])
def test_run_from_env_hostile_inputs_fail_closed(over):
    with pytest.raises(R.ReconcileError):
        R.run_from_env(_env(**over))

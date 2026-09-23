"""End-to-end coverage accounting: structured release evidence -> the REAL cert-plan reducer ->
the REAL invocation planner -> the REAL cert-result reducer -> the REAL gate, in observe AND gate.

The cert-plan cells are the build intent. A planned cell that failed or lost its evidence, an
ineligible target and a rejected runtime package must surface as coverage gaps (never silently
disappear, never a clean pass), while a selective build simply plans fewer cells and policy-excluded
packages (debug/source, not shipped as runtime) never create gaps. Platform data is synthetic (so
these tests are data-driven and independent of which containers are enabled today); only the leg
summaries are synthesized, in the shape the reducer accepts from pep_result_summary.
"""
from pathlib import Path

import pytest

import pep_cert_gate as G
import pep_cert_plan as CP
import pep_cert_report as R
import pep_cert_result as CR
import pep_invocation_plan as P

SHA = "a" * 40
PEP_SHA = "b" * 40
PKG = "pgedge-rag-server2"
POLICY = {"allowed_runtime_package_names": [PKG, "pgedge-rag-server"], "expected_binary_version": ""}
PROVENANCE = {"repository": "pgEdge/pgedge-rag-server", "run_id": "100", "run_attempt": "1",
              "sha": SHA, "ref": "refs/tags/v2.0.0"}
EXEC_CATALOG = {"schema": P.EXEC_CATALOG_SCHEMA, "supported_pg_majors": ["17"], "platforms": [
    {"os_token": "el-9", "family": "rpm", "catalog_os": ["rocky9"]},
    {"os_token": "trixie", "family": "deb", "catalog_os": ["debian13"]},
    {"os_token": "noble", "family": "deb", "catalog_os": ["ubuntu2404"]},   # mapped, never enabled
]}
ENABLED = {("rpm", "amd64", "rocky9"): "rocky9-amd64", ("rpm", "arm64", "rocky9"): "rocky9-arm64",
           ("deb", "amd64", "debian13"): "debian13-amd64", ("deb", "arm64", "debian13"): "debian13-arm64"}


# --------------------------------------------------------------------------- #
# builders: one planned cell = detector cell + its build job + its verified package artifact
# --------------------------------------------------------------------------- #
def pkg(family, os_token, arch, *, name=PKG, cls="runtime", release=None, sha=None, native=None):
    dist = os_token.replace("-", "") if family == "rpm" else os_token
    if native is None:
        native = {"amd64": "x86_64", "arm64": "aarch64"}[arch] if family == "rpm" else arch
    return {"package_name": name, "epoch": None, "version": "2.0.0",
            "release": release if release is not None else "1." + dist,
            "native_arch": native, "package_class": cls,
            "sha256": sha if sha is not None else (name + os_token + arch).encode().hex()[:64].ljust(64, "0")}


def planned(family, os_token, arch, *, job="success", members="default"):
    """job: 'success' | 'failure' | 'in_progress' | None (never ran). members: 'default' (one valid
    runtime package), a list, or None (no verified artifact: an absent/rejected receipt)."""
    cid = "pepcell.v1.%s.%s.%s.pkg" % (family, os_token, arch)
    if members == "default":
        members = [pkg(family, os_token, arch)]
    return {"cell": {"cell_id": cid, "artifact_name": cid, "family": family, "os": os_token,
                     "normalized_arch": arch, "pg_coupled": False},
            "job": job, "members": members}


def certify(cells, *, publication=None, simulated=False, mode="full", outcome=lambda inv: "pass"):
    """Run the real pipeline once per enforcement mode. Returns (cert_plan, plan, {mode: (result, decision)})."""
    jobs, arts = [], []
    for i, c in enumerate(cells):
        cid = c["cell"]["cell_id"]
        if c["job"] is not None:
            done = c["job"] in ("success", "failure")
            jobs.append({"cell_id": cid, "job_id": i + 1, "run_attempt": 1,
                         "status": "completed" if done else c["job"],
                         "conclusion": c["job"] if done else None})
        if c["members"] is not None:
            arts.append({"name": cid, "id": 100 + i, "members": c["members"]})
    if publication is None:
        publication = {"rpm": "skipped", "deb": "skipped"} if simulated else {"rpm": "success", "deb": "success"}
    cert_plan = CP.reduce({
        "provenance": dict(PROVENANCE),
        "release_intent": {"logical_component": "rag", "intended_version": "2.0.0", "intended_buildnum": "1",
                           "effective_tag": "v2.0.0", "channel": "staging", "simulated": simulated},
        "component_policy": dict(POLICY), "planned_cells": [c["cell"] for c in cells],
        "job_records": jobs, "artifacts": arts, "publication_results": publication, "execution_mode": mode})
    assert cert_plan["plan_resolved"] is True, cert_plan["errors"]
    plan = P.build_invocation_plan(cert_plan, EXEC_CATALOG, ENABLED, execution_mode=mode)
    assert plan["plan_resolved"] is True, plan["errors"]
    decided = {}
    for enforcement in ("observe", "gate"):
        summaries = [_summary(inv, outcome(inv), enforcement, preview=(mode == "preview"))
                     for inv in plan["matrix"]["include"] if outcome(inv) != "missing"]
        result = CR.build_cert_result(plan, summaries, "1")
        assert result["result_resolved"] is True, result["errors"]
        decided[enforcement] = (result, G.decide(result, enforcement))
    return cert_plan, plan, decided


def _summary(inv, outcome, enforcement, *, preview):
    fail = outcome == "fail"
    return {
        "invocation_id": inv["invocation_id"],
        "execution_status": "preview" if preview else "completed",
        "test_verdict": "not_run" if preview else ("fail" if fail else "pass"),
        "enforcement_mode": enforcement,
        "identity_evidence": {k: ("not_attempted" if preview else "proven") for k in ("l2a", "l2b", "l1")},
        "counts": ({"tests": 0, "failures": 0, "errors": 0, "skipped": 0} if preview
                   else {"tests": 12, "failures": 1 if fail else 0, "errors": 0, "skipped": 0}),
        "provenance": {"caller_repo": PROVENANCE["repository"], "caller_sha": SHA,
                       "caller_ref": PROVENANCE["ref"], "caller_run_id": PROVENANCE["run_id"],
                       "caller_run_attempt": "1", "pep_requested_ref": PEP_SHA, "pep_resolved_sha": PEP_SHA}}


def outcomes(decided):
    return {m: (d["certification_state"], d["policy_decision"], d["workflow_conclusion"], d["reason_code"])
            for m, (_, d) in decided.items()}


def gaps(plan):
    return sorted((g["scope"], g["cell_id"].split(".", 2)[-1], g["physical_package"], g["reason"], g["detail"])
                  for g in plan["coverage_gaps"])


def assert_reconciles(cert_plan, plan):
    c = plan["counts"]
    assert all(v >= 0 for v in list(c.values()) + list(c["gaps_by_scope"].values()) if isinstance(v, int))
    assert c["planned_cells"] == cert_plan["coverage_denominators"]["planned_build_cells"]
    assert c["selected_targets"] == cert_plan["coverage_denominators"]["selected_targets"]
    assert c["covered_targets"] + c["gaps_by_scope"]["target"] == c["selected_targets"]
    assert sum(c["gaps_by_scope"].values()) == c["coverage_gaps"] == len(plan["coverage_gaps"])
    no_target = sum(1 for cell in cert_plan["cells"] if not cell["targets"])
    assert c["gaps_by_scope"]["cell"] == no_target
    accounted = ({i["source_cell_id"] for i in plan["matrix"]["include"]}
                 | {g["cell_id"] for g in plan["coverage_gaps"]})
    assert accounted == {cell["cell_id"] for cell in cert_plan["cells"]}   # nothing planned vanished


PARTIAL = {"observe": ("incomplete", "report", "success", "partial_coverage"),
           "gate": ("incomplete", "block", "failure", "partial_coverage")}


# --------------------------------------------------------------------------- #
# selective intent vs failed planned cells
# --------------------------------------------------------------------------- #
def test_selective_build_plans_fewer_cells_and_certifies_cleanly():
    cp, plan, decided = certify([planned("rpm", "el-9", "amd64")])     # the caller built only this cell
    assert plan["coverage_gaps"] == [] and plan["counts"]["planned_cells"] == 1
    assert_reconciles(cp, plan)
    clean = ("pass", "allow", "success", "clean_pass")
    assert outcomes(decided) == {"observe": clean, "gate": clean}


def test_failed_and_missing_receipt_cells_are_cell_gaps_not_a_clean_pass():
    cp, plan, decided = certify([
        planned("rpm", "el-9", "amd64"),
        planned("deb", "trixie", "amd64", job="failure", members=None),  # build failed, nothing uploaded
        planned("rpm", "el-10", "amd64", job="failure"),                 # failed after uploading a package
        planned("deb", "trixie", "arm64", members=None),                 # job green, receipt absent/rejected
        planned("rpm", "el-9", "arm64", job=None, members=None),         # never ran
        planned("deb", "noble", "arm64", job="in_progress", members=None),
    ])
    assert gaps(plan) == [
        ("cell", "deb.noble.arm64.pkg", None, "build_incomplete", "in_progress"),
        ("cell", "deb.trixie.amd64.pkg", None, "build_failed", "failure"),
        ("cell", "deb.trixie.arm64.pkg", None, P.GAP_PACKAGE_EVIDENCE_MISSING,
         "build job succeeded but no verified package artifact"),
        ("cell", "rpm.el-9.arm64.pkg", None, "build_never_ran", None),
        # an inspected package from a failed job is a target, so the gap is target-scope
        ("target", "rpm.el-10.amd64.pkg", PKG, P.GAP_TARGET_INELIGIBLE, "build_failed"),
    ]
    assert_reconciles(cp, plan)
    assert len(plan["matrix"]["include"]) == 1
    assert outcomes(decided) == PARTIAL
    result, _ = decided["gate"]
    assert (result["execution_status"], result["test_verdict"], result["coverage_status"]) == (
        "completed", "pass", "partial")


# --------------------------------------------------------------------------- #
# mixed valid / rejected members
# --------------------------------------------------------------------------- #
def test_mixed_members_policy_exclusions_are_silent_each_rejected_file_is_a_gap():
    ok = planned("rpm", "el-9", "amd64", members=[
        pkg("rpm", "el-9", "amd64"),                                                # the valid target
        pkg("rpm", "el-9", "amd64", native="aarch64", sha="c" * 64),                # same name, wrong-arch file
        pkg("rpm", "el-9", "amd64", name="pgedge-rag-server2-debuginfo", cls="debug"),  # policy
        pkg("rpm", "el-9", "amd64", name="pgedge-rag-server2", cls="source", native="src", sha="d" * 64),
        pkg("rpm", "el-9", "amd64", name="some-other-tool"),                        # not shipped as runtime
        pkg("rpm", "el-9", "amd64", name="pgedge-rag-server", sha=""),              # allowed, no checksum
    ])
    only_rejected = planned("deb", "trixie", "arm64", members=[
        pkg("deb", "trixie", "arm64", sha=""),
        pkg("deb", "trixie", "arm64", name="pgedge-rag-server2-dbgsym", cls="debug")])
    cp, plan, decided = certify([ok, only_rejected])
    assert gaps(plan) == [
        ("cell", "deb.trixie.arm64.pkg", None, P.GAP_NO_RUNTIME_TARGET,
         "missing_checksum, non_runtime, package_not_allowed"),
        ("member", "rpm.el-9.amd64.pkg", "pgedge-rag-server", P.GAP_MEMBER_REJECTED,
         "missing_checksum; native_arch=x86_64"),
        ("member", "rpm.el-9.amd64.pkg", PKG, P.GAP_MEMBER_REJECTED,
         "arch_mismatch; native_arch=aarch64; sha256=cccccccccccc"),
    ]
    assert plan["counts"]["gaps_by_scope"] == {"cell": 1, "target": 0, "member": 2}
    assert [i["source_cell_id"] for i in plan["matrix"]["include"]] == ["pepcell.v1.rpm.el-9.amd64.pkg"]
    assert_reconciles(cp, plan)
    assert outcomes(decided) == PARTIAL


@pytest.mark.parametrize("override, detail", [
    ({"sha256": ""}, "missing_checksum; native_arch=x86_64"),
    ({"native_arch": "aarch64", "sha256": "c" * 64}, "arch_mismatch; native_arch=aarch64; sha256=cccccccccccc"),
    ({"version": "", "sha256": "e" * 64}, "missing_version; native_arch=x86_64; sha256=eeeeeeeeeeee"),
])
def test_valid_target_never_hides_a_distinct_rejected_file_of_the_same_package(override, detail):
    # Two VALID same-name files already block the cell (target_ambiguous); one valid file plus a
    # distinct rejected one must not quietly certify clean either, since only the valid file is tested.
    bad = dict(pkg("rpm", "el-9", "amd64"), **override)
    cp, plan, decided = certify([planned("rpm", "el-9", "amd64", members=[pkg("rpm", "el-9", "amd64"), bad])])
    [gap] = plan["coverage_gaps"]
    assert (gap["scope"], gap["physical_package"], gap["reason"], gap["detail"]) == (
        "member", PKG, P.GAP_MEMBER_REJECTED, detail)
    assert len(plan["matrix"]["include"]) == 1                     # the valid file is still tested
    assert_reconciles(cp, plan)
    assert outcomes(decided) == PARTIAL                            # never clean_pass


def test_ambiguous_runtime_packages_are_one_cell_gap():
    dup = planned("deb", "trixie", "amd64", members=[pkg("deb", "trixie", "amd64", sha="1" * 64),
                                                    pkg("deb", "trixie", "amd64", sha="2" * 64)])
    cp, plan, decided = certify([planned("rpm", "el-9", "amd64"), dup])
    assert gaps(plan) == [("cell", "deb.trixie.amd64.pkg", None, P.GAP_TARGET_AMBIGUOUS, PKG)]
    assert_reconciles(cp, plan)
    assert outcomes(decided) == PARTIAL


# --------------------------------------------------------------------------- #
# publication failure, unsupported platform, preview
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("push, detail", [("failure", "publication_publish_unconfirmed"),
                                          ("skipped", "publication_publish_skipped")])
def test_unpublished_family_targets_are_target_gaps(push, detail):
    cp, plan, decided = certify([planned("rpm", "el-9", "amd64"), planned("rpm", "el-9", "arm64"),
                                 planned("deb", "trixie", "amd64")],
                                publication={"rpm": push, "deb": "success"})
    assert gaps(plan) == [
        ("target", "rpm.el-9.amd64.pkg", PKG, P.GAP_TARGET_INELIGIBLE, detail),
        ("target", "rpm.el-9.arm64.pkg", PKG, P.GAP_TARGET_INELIGIBLE, detail),
    ]
    assert_reconciles(cp, plan)
    assert outcomes(decided) == PARTIAL


def test_unsupported_platform_gap_is_preserved_beside_evidence_gaps():
    cp, plan, decided = certify([planned("rpm", "el-9", "amd64"), planned("deb", "noble", "amd64"),
                                 planned("deb", "trixie", "arm64", job="failure", members=None)])
    assert gaps(plan) == [
        ("cell", "deb.trixie.arm64.pkg", None, "build_failed", "failure"),
        ("target", "deb.noble.amd64.pkg", PKG, P.GAP_NO_ENABLED_PLATFORM, "noble"),   # unchanged reason
    ]
    assert plan["counts"]["eligible_targets"] == 2 and plan["counts"]["covered_targets"] == 1
    assert_reconciles(cp, plan)
    assert outcomes(decided) == PARTIAL


def test_preview_runs_preview_eligible_targets_and_reports_preview_reasons():
    mismatch = planned("deb", "trixie", "arm64", members=[pkg("deb", "trixie", "arm64", release="9.trixie")])
    cp, plan, decided = certify([planned("rpm", "el-9", "amd64"),
                                 planned("deb", "trixie", "amd64", job="failure", members=None),
                                 mismatch], simulated=True, mode="preview")
    assert gaps(plan) == [
        ("cell", "deb.trixie.amd64.pkg", None, "build_failed", "failure"),
        ("target", "deb.trixie.arm64.pkg", PKG, P.GAP_TARGET_INELIGIBLE, "identity_mismatch"),
    ]
    assert len(plan["matrix"]["include"]) == 1
    assert_reconciles(cp, plan)
    assert outcomes(decided) == {"observe": ("preview", "report", "success", "preview"),
                                 "gate": ("preview", "block", "failure", "preview")}


# --------------------------------------------------------------------------- #
# leg outcomes with gaps, and zero runnable legs
# --------------------------------------------------------------------------- #
def test_leg_failure_and_missing_leg_with_gaps():
    cells = [planned("rpm", "el-9", "amd64"), planned("deb", "trixie", "amd64", job="failure")]
    _, _, failed = certify(cells, outcome=lambda inv: "fail")
    assert outcomes(failed) == {"observe": ("fail", "report", "success", "product_fail"),
                                "gate": ("fail", "block", "failure", "product_fail")}
    _, _, missing = certify(cells, outcome=lambda inv: "missing")
    blocked = ("incomplete", "block", "failure", "missing_result")
    assert outcomes(missing) == {"observe": blocked, "gate": blocked}


def test_zero_runnable_legs_blocks_in_both_modes(tmp_path):
    cp, plan, decided = certify([planned("rpm", "el-9", "amd64", job="failure"),
                                 planned("deb", "trixie", "arm64", members=None)])
    assert plan["matrix"]["include"] == [] and plan["counts"]["coverage_gaps"] == 2
    assert_reconciles(cp, plan)
    result, decision = decided["observe"]
    assert (result["execution_status"], result["reason_code"], result["coverage_status"]) == (
        "incomplete", "zero_eligible", "none")
    blocked = ("incomplete", "block", "failure", "zero_eligible")
    assert outcomes(decided) == {"observe": blocked, "gate": blocked}
    # the report defers to the authoritative axis instead of claiming "partial" coverage
    R.render_report(result, decision, {}, Path(tmp_path))
    html = (Path(tmp_path) / "consolidated-report.html").read_text()
    assert "<td>coverage_status</td><td><code>none</code></td>" in html
    note = html[html.index("planned but not certified"):html.index("<table>", html.index("planned but not certified"))]
    assert "cannot be complete" in note and "partial" not in note


def test_report_lists_every_gap_scope_with_dashes_for_absent_fields(tmp_path):
    cp, plan, decided = certify([planned("rpm", "el-9", "amd64", members=[
                                     pkg("rpm", "el-9", "amd64"), pkg("rpm", "el-9", "amd64", name="pgedge-rag-server", sha="")]),
                                 planned("deb", "trixie", "amd64", job="failure", members=None)])
    result, decision = decided["gate"]
    R.render_report(result, decision, {}, Path(tmp_path))
    html = (Path(tmp_path) / "consolidated-report.html").read_text()
    table = html[html.index("planned but not certified"):]
    assert "BUILD FAILED" in table and "MEMBER REJECTED" in table and "missing_checksum" in table
    assert "<code>—</code>" in table and ">None<" not in table

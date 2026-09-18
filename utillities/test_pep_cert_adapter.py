"""Offline tests for the PEP evidence adapter (utillities/pep_cert_adapter.py).

Pure/deterministic: no network, no docker, no rpm/dpkg. The adapter STRUCTURES
captured release evidence into a reducer envelope; the committed reducer
(pep_cert_plan.reduce) decides meaning. End-to-end tests therefore run the
adapter output straight through reduce() — there is no second reducer.

Reuses the committed fixtures (cert_plan_fixtures/): the preserved Spike-0
attempt-2/attempt-3 rerun scenarios and the real RAG2 pre-inspected members, plus
the real RAG detector matrix shape captured from pgedge-detect-build-matrix.
"""
import copy
import json
import random
from pathlib import Path

import pytest

import pep_cert_adapter as A
import pep_cert_plan as R

FX = Path(__file__).parent / "cert_plan_fixtures"


def load(name):
    with open(FX / name) as fh:
        return json.load(fh)


def cells_by_id(plan):
    out = {}
    for c in plan["cells"]:
        out.setdefault(c["cell_id"], c)
    return out


# ---- builders for captured-evidence shapes ---------------------------------
def mk_job(cid, job_id, attempt=1, conclusion="success", status="completed", name=None):
    nm = name if name is not None else "build (%s) [pep-cell:%s]" % (cid, cid)
    return {"id": job_id, "name": nm, "run_attempt": attempt, "status": status, "conclusion": conclusion}


def mk_art(cid, art_id, members=None, expired=False, name=None):
    nm = ("pkg-%s [pep-cell.%s]" % (cid, cid)) if name is None else name
    a = {"id": art_id, "name": nm, "expired": expired}
    if members is not None:
        a["members"] = members
    return a


def jobs_page(jobs, total=None):
    return {"total_count": len(jobs) if total is None else total, "jobs": jobs}


def arts_page(arts, total=None):
    return {"total_count": len(arts) if total is None else total, "artifacts": arts}


def det(entries):
    return {"include": entries}


def dcell(cid, family="rpm", os="el-9", arch="amd64", pg_coupled=False, bpm=None, bpv=None, extra=None):
    e = {"cell_id": cid, "family": family, "os": os, "normalized_arch": arch,
         "pg_coupled": pg_coupled, "build_pg_major": bpm, "build_pg_version": bpv}
    if extra:
        e.update(extra)
    return e


def ri(simulated=False, channel="staging", version="2.0.0", buildnum="1", component="rag"):
    return {"logical_component": component, "intended_version": version,
            "intended_buildnum": buildnum, "effective_tag": "v" + version,
            "channel": channel, "simulated": simulated}


def policy(allowed=None, expected="2.0.0"):
    return {"allowed_runtime_package_names": allowed or [], "expected_binary_version": expected}


PROV = {"repository": "pgEdge/pgedge-rag-server", "run_id": "1", "run_attempt": "1"}


def assemble(planned, jobs, arts, *, pubs=None, rel=None, pol=None, prov=None, manifest=None):
    return A.assemble_reducer_input(
        planned_cells=planned, job_records=jobs, artifacts=arts,
        publication_results=pubs if pubs is not None else {},
        release_intent=rel if rel is not None else ri(),
        component_policy=pol if pol is not None else policy(),
        provenance=prov if prov is not None else PROV, manifest=manifest)


# =====================================================================
# 1. planned_cells_from_detector — detector matrix -> planned_cells
# =====================================================================
def test_real_rag_detector_shape_4rpm_12deb_all_decoupled():
    m = load("rag_detector_matrix.json")
    planned = A.planned_cells_from_detector(m["rpm_matrix"], m["deb_matrix"])
    assert len(planned) == 16
    assert sum(1 for c in planned if c["family"] == "rpm") == 4
    assert sum(1 for c in planned if c["family"] == "deb") == 12
    assert all(c["pg_coupled"] is False for c in planned)
    assert all(c["build_pg_major"] is None and c["build_pg_version"] is None for c in planned)
    # artifact join key is the cell's own id; required reducer keys are all present strings
    for c in planned:
        assert c["artifact_name"] == c["cell_id"]
        for k in ("cell_id", "artifact_name", "family", "os", "normalized_arch"):
            assert isinstance(c[k], str) and c[k]
    # the real matrix drives a fully resolved plan through the committed reducer
    plan = R.reduce(assemble(planned, [], [], rel=ri(component="rag")))
    assert plan["plan_resolved"] is True
    assert plan["coverage_denominators"]["planned_build_cells"] == 16


def test_coupled_component_retains_build_pg_identity():
    planned = A.planned_cells_from_detector(det([
        dcell("pepcell.v1.rpm.el-9.amd64.spock60.pg16", pg_coupled=True, bpm="16", bpv="16.4"),
    ]))
    c = planned[0]
    assert c["pg_coupled"] is True
    assert c["build_pg_major"] == "16" and c["build_pg_version"] == "16.4"


def test_decoupled_cell_never_carries_representative_pg():
    # A decoupled detector entry may still carry legacy representative pg_version/pg_major;
    # the adapter must NOT promote them into build-PG identity.
    planned = A.planned_cells_from_detector(det([
        dcell("pepcell.v1.rpm.el-9.amd64.pkg", pg_coupled=False,
              extra={"pg_version": "16.4", "pg_major": "16"}),
    ]))
    c = planned[0]
    assert c["pg_coupled"] is False
    assert c["build_pg_major"] is None and c["build_pg_version"] is None
    assert "pg_version" not in c and "pg_major" not in c


def test_selective_family_and_arch_matrix_preserved():
    # only an RPM matrix (no DEB), single arch
    planned = A.planned_cells_from_detector(det([
        dcell("pepcell.v1.rpm.el-9.amd64.pkg", "rpm", "el-9", "amd64"),
        dcell("pepcell.v1.rpm.el-10.amd64.pkg", "rpm", "el-10", "amd64"),
    ]), det([]))
    assert len(planned) == 2
    assert {c["family"] for c in planned} == {"rpm"}
    assert {c["normalized_arch"] for c in planned} == {"amd64"}


def test_cell_id_is_not_parsed_fields_come_from_explicit_keys():
    # cell_id deliberately disagrees with the explicit fields; adapter trusts the fields.
    planned = A.planned_cells_from_detector(det([
        dcell("pepcell.v1.rpm.el-9.amd64.pkg", family="deb", os="bookworm", arch="arm64"),
    ]))
    c = planned[0]
    assert c["family"] == "deb" and c["os"] == "bookworm" and c["normalized_arch"] == "arm64"


def test_malformed_detector_entries_pass_through_and_fail_closed_no_raise():
    planned = A.planned_cells_from_detector(det([
        dcell("pepcell.v1.rpm.el-9.amd64.ok"),
        {"family": "rpm"},            # missing cell_id
        "not-an-object",              # non-dict entry preserved for the reducer to flag
    ]))
    assert len(planned) == 3          # nothing dropped
    plan = R.reduce(assemble(planned, [], []))
    assert plan["plan_resolved"] is False
    assert plan["coverage_denominators"]["planned_build_cells"] == 3


# =====================================================================
# 2. job_records_from_jobs — marker resolution + fail-closed
# =====================================================================
def test_job_marker_maps_and_preserves_fields():
    jobs = [mk_job("cA", 111, attempt=2, conclusion="success", status="completed")]
    recs = A.job_records_from_jobs(jobs, ["cA"])
    assert recs == [{"cell_id": "cA", "job_id": 111, "run_attempt": 2,
                     "status": "completed", "conclusion": "success"}]


def test_unrelated_jobs_and_nonplanned_markers_ignored():
    jobs = [
        {"id": 1, "name": "housekeeping", "run_attempt": 1, "status": "completed", "conclusion": "success"},
        mk_job("other", 2),            # well-formed marker, but 'other' not planned
        mk_job("cA", 3),
    ]
    recs = A.job_records_from_jobs(jobs, ["cA"])
    assert [r["cell_id"] for r in recs] == ["cA"]


def test_job_marker_malformed_blank_and_conflicting_fail_closed():
    for nm in ("build [pep-cell:]", "x [pep-cell:  ]",
               "x [pep-cell:cA] [pep-cell:cB]", "x [pep-cell:cA][pep-cell:cA]"):
        with pytest.raises(A.AdapterError):
            A.job_records_from_jobs([{"id": 1, "name": nm, "run_attempt": 1,
                                      "status": "completed", "conclusion": "success"}], ["cA", "cB"])


def test_non_dict_job_fails_closed():
    with pytest.raises(A.AdapterError):
        A.job_records_from_jobs(["nope"], ["cA"])


# =====================================================================
# 3. artifact_records — marker/receipt association, expiry, ambiguity
# =====================================================================
def test_artifact_name_marker_associates_and_passes_members():
    mem = [{"package_name": "p", "version": "2.0.0", "release": "1.el9",
            "native_arch": "x86_64", "package_class": "runtime", "sha256": "a" * 64, "epoch": None}]
    recs = A.artifact_records([mk_art("cA", 55, members=mem)], ["cA"])
    # canonical join key is the cell_id; raw source name retained as inert audit
    assert recs == [{"name": "cA", "id": 55, "members": mem,
                     "source_artifact_name": "pkg-cA [pep-cell.cA]"}]


def test_artifact_receipt_association_by_immutable_id():
    inv = [{"id": 77, "name": "rpm__el-9__amd64", "expired": False, "members": []}]
    receipts = [{"artifact_id": 77, "cell_id": "cA", "digest": "sha256:deadbeef"}]
    recs = A.artifact_records(inv, ["cA"], receipts=receipts)
    assert recs == [{"name": "cA", "id": 77, "members": [],
                     "source_artifact_name": "rpm__el-9__amd64"}]


def test_expired_artifact_treated_as_absent():
    recs = A.artifact_records([mk_art("cA", 9, members=[], expired=True)], ["cA"])
    assert recs == []


def test_unrelated_and_nonplanned_artifacts_ignored():
    inv = [
        {"id": 1, "name": "logs.zip", "expired": False},          # no marker
        mk_art("ghost", 2, members=[]),                            # marker for non-planned cell
        mk_art("cA", 3, members=[]),
    ]
    recs = A.artifact_records(inv, ["cA"])
    assert [r["id"] for r in recs] == [3]


def test_two_live_artifacts_one_cell_reduce_to_ambiguous():
    planned = A.planned_cells_from_detector(det([dcell("cA")]))
    inv = [mk_art("cA", 1, members=[]), mk_art("cA", 2, members=[])]
    arts = A.artifact_records(inv, ["cA"])
    assert len(arts) == 2                                          # adapter emits both; reducer decides
    plan = R.reduce(assemble(planned, [mk_and_reduce_job("cA")], arts))
    assert cells_by_id(plan)["cA"]["build_state"] == "ambiguous"


def mk_and_reduce_job(cid):
    # a resolved job_record (adapter output shape) for reducer-facing tests
    return {"cell_id": cid, "job_id": 1, "run_attempt": 1, "status": "completed", "conclusion": "success"}


def test_conflicting_marker_and_receipt_fail_closed():
    inv = [mk_art("cA", 5, members=[])]                            # name marker -> cA
    receipts = [{"artifact_id": 5, "cell_id": "cB"}]               # receipt -> cB
    with pytest.raises(A.AdapterError):
        A.artifact_records(inv, ["cA", "cB"], receipts=receipts)


def test_malformed_receipt_and_conflicting_receipts_fail_closed():
    with pytest.raises(A.AdapterError):
        A.artifact_records([], ["cA"], receipts=["nope"])
    with pytest.raises(A.AdapterError):
        A.artifact_records([], ["cA"], receipts=[{"artifact_id": 1}])   # missing cell_id
    with pytest.raises(A.AdapterError):
        A.artifact_records([], ["cA", "cB"],
                           receipts=[{"artifact_id": 1, "cell_id": "cA"},
                                     {"artifact_id": 1, "cell_id": "cB"}])


def test_malformed_artifact_name_marker_and_non_dict_fail_closed():
    with pytest.raises(A.AdapterError):
        A.artifact_records([{"id": 1, "name": "x [pep-cell.]", "expired": False}], ["cA"])
    with pytest.raises(A.AdapterError):
        A.artifact_records(["nope"], ["cA"])


# =====================================================================
# 4. combine_pages — pagination integrity
# =====================================================================
def test_combine_pages_complete_ok():
    pages = [jobs_page([{"id": 1}, {"id": 2}], total=3), jobs_page([{"id": 3}], total=3)]
    assert [x["id"] for x in A.combine_pages(pages, "jobs")] == [1, 2, 3]


def test_combine_pages_incomplete_fails_closed():
    pages = [jobs_page([{"id": 1}, {"id": 2}], total=3)]           # only 2 of 3
    with pytest.raises(A.AdapterError):
        A.combine_pages(pages, "jobs")


def test_combine_pages_duplicate_ids_fail_closed():
    pages = [arts_page([{"id": 1}, {"id": 1}], total=2)]
    with pytest.raises(A.AdapterError):
        A.combine_pages(pages, "artifacts")


def test_combine_pages_malformed_and_inconsistent_fail_closed():
    with pytest.raises(A.AdapterError):
        A.combine_pages([], "jobs")                               # empty
    with pytest.raises(A.AdapterError):
        A.combine_pages([{"total_count": 1}], "jobs")             # missing items list
    with pytest.raises(A.AdapterError):
        A.combine_pages([{"jobs": [{"id": 1}]}], "jobs")          # missing total_count
    with pytest.raises(A.AdapterError):
        A.combine_pages([jobs_page([{"id": 1}], total=1), jobs_page([{"id": 2}], total=2)], "jobs")


def test_combine_pages_feeds_marker_resolution_end_to_end():
    # pages -> combine -> marker resolution, proving the whole capture path composes
    jpages = [jobs_page([mk_job("cA", 1)], total=2), jobs_page([mk_job("cB", 2)], total=2)]
    jobs = A.combine_pages(jpages, "jobs")
    recs = A.job_records_from_jobs(jobs, ["cA", "cB"])
    assert sorted(r["cell_id"] for r in recs) == ["cA", "cB"]


# =====================================================================
# 5. publication normalization + envelope assembly
# =====================================================================
def test_normalize_publication_validates_and_fails_closed():
    # valid maps pass through; a missing family stays absent (valid)
    assert A.normalize_publication_results({"rpm": "success", "deb": "failure"}) \
        == {"rpm": "success", "deb": "failure"}
    assert A.normalize_publication_results({"rpm": "skipped"}) == {"rpm": "skipped"}
    assert A.normalize_publication_results({}) == {}
    # non-object, accidental key, and malformed value all fail closed
    with pytest.raises(A.AdapterError):
        A.normalize_publication_results("not-a-dict")
    with pytest.raises(A.AdapterError):
        A.normalize_publication_results({"rpm": "success", "typpo": "success"})
    with pytest.raises(A.AdapterError):
        A.normalize_publication_results({"rpm": "bogus"})
    with pytest.raises(A.AdapterError):
        A.normalize_publication_results({"rpm": None})


def test_assemble_rejects_malformed_publication():
    planned = A.planned_cells_from_detector(det([dcell("cA")]))
    with pytest.raises(A.AdapterError):
        A.assemble_reducer_input(
            planned_cells=planned, job_records=[], artifacts=[],
            publication_results={"rpm": "success", "oops": "success"},
            release_intent=ri(), component_policy=policy(), provenance=PROV)


def test_manifest_is_audit_only_and_does_not_override():
    planned = A.planned_cells_from_detector(det([dcell("cA")]))
    env = assemble(planned, [], [], manifest={"anything": "ignored"})
    assert env["audit"]["manifest"] == {"anything": "ignored"}
    plan = R.reduce(env)                                           # reducer ignores audit
    assert "audit" not in plan
    assert cells_by_id(plan)["cA"]["build_state"] == "never_ran"


# =====================================================================
# 6. build-state scenarios end-to-end (reducer decides)
# =====================================================================
def _one_cell_env(jobs_api, arts_api, *, pubs=None, rel=None, pol=None, allowed=None):
    planned = A.planned_cells_from_detector(det([dcell("cA")]))
    pcids = ["cA"]
    jr = A.job_records_from_jobs(jobs_api, pcids)
    ar = A.artifact_records(arts_api, pcids)
    return assemble(planned, jr, ar, pubs=pubs, rel=rel,
                    pol=pol if pol is not None else policy(allowed=allowed or []))


def test_planned_cell_with_no_job_is_never_ran():
    plan = R.reduce(_one_cell_env([], []))
    assert cells_by_id(plan)["cA"]["build_state"] == "never_ran"


def test_latest_success_with_artifact_is_available():
    plan = R.reduce(_one_cell_env([mk_job("cA", 1, conclusion="success")],
                                  [mk_art("cA", 10, members=[])]))
    assert cells_by_id(plan)["cA"]["build_state"] == "available"


def test_carried_forward_success_is_available():
    # attempt-2 present only for another cell; cA carries its attempt-1 success + artifact.
    planned = A.planned_cells_from_detector(det([dcell("cA"), dcell("cB")]))
    pcids = ["cA", "cB"]
    jobs = [mk_job("cA", 1, attempt=1, conclusion="success"),
            mk_job("cB", 2, attempt=1, conclusion="failure"),
            mk_job("cB", 3, attempt=2, conclusion="success")]
    arts = [mk_art("cA", 10, members=[]), mk_art("cB", 11, members=[])]
    plan = R.reduce(assemble(planned, A.job_records_from_jobs(jobs, pcids),
                             A.artifact_records(arts, pcids)))
    by = cells_by_id(plan)
    assert by["cA"]["build_state"] == "available"          # carried forward
    assert by["cB"]["build_state"] == "available"          # rerun succeeded


def test_later_failure_overrides_older_artifact():
    jobs = [mk_job("cA", 1, attempt=1, conclusion="success"),
            mk_job("cA", 2, attempt=2, conclusion="failure")]
    plan = R.reduce(_one_cell_env(jobs, [mk_art("cA", 10, members=[])]))
    assert cells_by_id(plan)["cA"]["build_state"] == "failed"


def test_later_cancelled_overrides_older_artifact():
    jobs = [mk_job("cA", 1, attempt=1, conclusion="success"),
            mk_job("cA", 2, attempt=2, conclusion="cancelled")]
    plan = R.reduce(_one_cell_env(jobs, [mk_art("cA", 10, members=[])]))
    assert cells_by_id(plan)["cA"]["build_state"] == "failed"


def test_success_with_missing_artifact_is_incomplete():
    plan = R.reduce(_one_cell_env([mk_job("cA", 1, conclusion="success")], []))
    assert cells_by_id(plan)["cA"]["build_state"] == "incomplete"


def test_success_with_only_expired_artifact_is_incomplete():
    plan = R.reduce(_one_cell_env([mk_job("cA", 1, conclusion="success")],
                                  [mk_art("cA", 10, members=[], expired=True)]))
    assert cells_by_id(plan)["cA"]["build_state"] == "incomplete"


# =====================================================================
# 7. publication states end-to-end
# =====================================================================
def test_publication_success_confirms_available_cell():
    plan = R.reduce(_one_cell_env([mk_job("cA", 1)], [mk_art("cA", 10, members=[])],
                                  pubs={"rpm": "success"}))
    assert cells_by_id(plan)["cA"]["publication_state"] == "publish_confirmed"


def test_publication_failure_and_cancelled_are_unconfirmed():
    for res in ("failure", "cancelled"):
        plan = R.reduce(_one_cell_env([mk_job("cA", 1)], [mk_art("cA", 10, members=[])],
                                      pubs={"rpm": res}))
        assert cells_by_id(plan)["cA"]["publication_state"] == "publish_unconfirmed"


def test_publication_skipped_and_absent_are_skipped():
    for pubs in ({"rpm": "skipped"}, {}):
        plan = R.reduce(_one_cell_env([mk_job("cA", 1)], [mk_art("cA", 10, members=[])], pubs=pubs))
        assert cells_by_id(plan)["cA"]["publication_state"] == "publish_skipped"


# =====================================================================
# 8. simulated release behavior
# =====================================================================
def test_simulated_release_yields_no_eligible_targets():
    planned = A.planned_cells_from_detector(det([dcell("cA")]))
    mem = [_runtime_member("pgedge-rag-server2", "x86_64")]
    env = assemble(planned,
                   A.job_records_from_jobs([mk_job("cA", 1)], ["cA"]),
                   A.artifact_records([mk_art("cA", 10, members=mem)], ["cA"]),
                   pubs={"rpm": "success"}, rel=ri(simulated=True),
                   pol=policy(allowed=["pgedge-rag-server2"]))
    plan = R.reduce(env)
    # A simulated run cannot have published, so a contradictory push "success" is neither collapsed
    # into a simulated skip nor reported as a confirmed publication: it is preserved fail-closed as
    # publish_unconfirmed/simulated_with_family_push_success. Strict eligibility still yields no
    # eligible targets because the release is simulated.
    assert plan["cells"][0]["publication_state"] == "publish_unconfirmed"
    assert plan["cells"][0]["publication_reason"] == "simulated_with_family_push_success"
    assert plan["coverage_denominators"]["eligible_targets"] == 0


def _runtime_member(name, native_arch, version="2.0.0", release="1.el9", sha=None):
    return {"package_name": name, "epoch": None, "version": version, "release": release,
            "native_arch": native_arch, "package_class": "runtime",
            "source_filename": "%s-%s.%s" % (name, version, native_arch),
            "sha256": sha or ("a" * 64)}


# =====================================================================
# 9. deterministic output regardless of input ordering
# =====================================================================
def test_deterministic_regardless_of_capture_ordering():
    entries = [dcell("pepcell.v1.rpm.el-9.amd64.pkg", "rpm", "el-9", "amd64"),
               dcell("pepcell.v1.deb.bookworm.amd64.pkg", "deb", "bookworm", "amd64")]
    jobs = [mk_job("pepcell.v1.rpm.el-9.amd64.pkg", 1),
            mk_job("pepcell.v1.deb.bookworm.amd64.pkg", 2)]
    arts = [mk_art("pepcell.v1.rpm.el-9.amd64.pkg", 10, members=[]),
            mk_art("pepcell.v1.deb.bookworm.amd64.pkg", 11, members=[])]

    def build(seed):
        e2, j2, a2 = list(entries), list(jobs), list(arts)
        random.Random(seed).shuffle(e2)
        random.Random(seed + 1).shuffle(j2)
        random.Random(seed + 2).shuffle(a2)
        planned = A.planned_cells_from_detector(det(e2))
        pcids = [c["cell_id"] for c in planned]
        return R.to_json(R.reduce(assemble(
            planned, A.job_records_from_jobs(j2, pcids), A.artifact_records(a2, pcids),
            pubs={"rpm": "success", "deb": "success"})))

    outputs = {build(s) for s in range(5)}
    assert len(outputs) == 1


# =====================================================================
# 10. preserved Spike-0 attempt-2 / attempt-3 rerun fixtures
# =====================================================================
def _capture_from_reducer_fixture(fx):
    """Re-derive raw captures (jobs-API + artifact inventory carrying markers)
    from a preserved reducer-input fixture, so the adapter's marker resolution +
    rerun handling can reconstruct the same scenario end-to-end."""
    planned = A.planned_cells_from_detector(det([
        {"cell_id": c["cell_id"], "family": c["family"], "os": c["os"],
         "normalized_arch": c["normalized_arch"]} for c in fx["planned_cells"]]))
    pcids = [c["cell_id"] for c in planned]
    jobs_api = [mk_job(r["cell_id"], r["job_id"], attempt=r["run_attempt"],
                       conclusion=r["conclusion"], status=r["status"]) for r in fx["job_records"]]
    name_to_cell = {c["artifact_name"]: c["cell_id"] for c in fx["planned_cells"]}
    arts_api = [mk_art(name_to_cell[a["name"]], a["id"], members=a.get("members", []))
                for a in fx["artifacts"] if a["name"] in name_to_cell]
    env = assemble(planned,
                   A.job_records_from_jobs(jobs_api, pcids),
                   A.artifact_records(arts_api, pcids),
                   pubs=fx.get("publication_results"), rel=fx["release_intent"],
                   pol=fx["component_policy"], prov=fx["provenance"])
    return planned, env


def test_spike0_attempt2_all_available_via_adapter():
    fx = load("spike0_attempt2.json")
    _, env = _capture_from_reducer_fixture(fx)
    plan = R.reduce(env)
    states = {c["cell_id"]: c["build_state"] for c in plan["cells"]}
    assert set(states.values()) == {"available"}                  # carried-forward attempt-2
    # adapter reconstruction agrees with reducing the preserved fixture directly
    direct = {c["cell_id"]: c["build_state"] for c in R.reduce(fx)["cells"]}
    assert states == direct


def test_spike0_attempt3_fullA_failed_others_available_via_adapter():
    fx = load("spike0_attempt3.json")
    _, env = _capture_from_reducer_fixture(fx)
    plan = R.reduce(env)
    states = {c["cell_id"]: c["build_state"] for c in plan["cells"]}
    assert states["full-A"] == "failed"                           # attempt-3 failed, artifact gone
    assert states["fj-A"] == "available"
    assert states["fj-B"] == "available"
    assert states["full-B"] == "available"
    direct = {c["cell_id"]: c["build_state"] for c in R.reduce(fx)["cells"]}
    assert states == direct


# =====================================================================
# 11. end-to-end cert-plan/1 with pre-inspected RAG2 members
# =====================================================================
def test_rag2_members_end_to_end_two_eligible_targets():
    rag2 = load("rag2_members.json")
    rpm_members = rag2["artifacts"][0]["members"]                 # real RAG2 per-file checksums
    deb_members = rag2["artifacts"][1]["members"]
    rpm_id = "pepcell.v1.rpm.el-9.amd64.pkg"
    deb_id = "pepcell.v1.deb.bookworm.amd64.pkg"
    planned = A.planned_cells_from_detector(det([
        dcell(rpm_id, "rpm", "el-9", "amd64"),
        dcell(deb_id, "deb", "bookworm", "amd64")]))
    pcids = [rpm_id, deb_id]
    inv = [{"id": 9894859799, "name": "rpm__el-9__amd64", "expired": False, "members": rpm_members},
           {"id": 9894899767, "name": "deb__bookworm__amd64", "expired": False, "members": deb_members}]
    receipts = [{"artifact_id": 9894859799, "cell_id": rpm_id, "digest": "sha256:" + "0" * 64},
                {"artifact_id": 9894899767, "cell_id": deb_id, "digest": "sha256:" + "1" * 64}]
    jobs = A.job_records_from_jobs([mk_job(rpm_id, 1), mk_job(deb_id, 2)], pcids)
    arts = A.artifact_records(inv, pcids, receipts=receipts)
    env = assemble(planned, jobs, arts, pubs={"rpm": "success", "deb": "success"},
                   rel=ri(component="rag", version="2.0.0", buildnum="1", channel="staging"),
                   pol=policy(allowed=["pgedge-rag-server2"], expected="2.0.0"))
    plan = R.reduce(env)
    assert plan["plan_resolved"] is True
    eligible = [t for c in plan["cells"] for t in c["targets"] if t.get("eligibility") == "eligible"]
    assert len(eligible) == 2
    assert {t["family"] for t in eligible} == {"rpm", "deb"}
    for t in eligible:
        assert t["package_identity_state"] == "confirmed"
        assert t["package"]["name"] == "pgedge-rag-server2"


def test_rag2_source_member_excluded_only_runtime_selected():
    rag2 = load("rag2_members.json")
    rpm_members = rag2["artifacts"][0]["members"]                 # source + runtime
    rpm_id = "pepcell.v1.rpm.el-9.amd64.pkg"
    planned = A.planned_cells_from_detector(det([dcell(rpm_id, "rpm", "el-9", "amd64")]))
    arts = A.artifact_records(
        [{"id": 1, "name": "rpm__el-9__amd64", "expired": False, "members": rpm_members}],
        [rpm_id], receipts=[{"artifact_id": 1, "cell_id": rpm_id}])
    env = assemble(planned, A.job_records_from_jobs([mk_job(rpm_id, 1)], [rpm_id]), arts,
                   pubs={"rpm": "success"}, pol=policy(allowed=["pgedge-rag-server2"]))
    cell = R.reduce(env)["cells"][0]
    selected = [m for m in cell["members"] if m.get("selected")]
    assert len(selected) == 1 and selected[0]["package_class"] == "runtime"
    assert len(cell["targets"]) == 1


# =====================================================================
# Adversarial-review corrections (fail-open closures)
# =====================================================================

# --- (1) malformed detector matrix must fail closed, never silent-empty -------
def test_malformed_detector_matrix_fails_closed():
    for bad in ({"include": "OOPS"}, {"nope": []}, None, 5, "x", True, 3.5):
        with pytest.raises(A.AdapterError):
            A.planned_cells_from_detector(bad)


def test_explicit_empty_include_is_valid():
    assert A.planned_cells_from_detector({"include": []}) == []
    assert A.planned_cells_from_detector([]) == []


def test_valid_rpm_plus_malformed_deb_cannot_produce_resolved_partial():
    good_rpm = {"include": [dcell("pepcell.v1.rpm.el-9.amd64.pkg", "rpm", "el-9", "amd64")]}
    with pytest.raises(A.AdapterError):
        A.planned_cells_from_detector(good_rpm, {"include": "OOPS"})   # no partial plan possible


# --- (2) explicit PG identity preserved for the reducer to validate -----------
def _explicit(cid, pg_coupled, bpm, bpv):
    return {"cell_id": cid, "family": "rpm", "os": "el-9", "normalized_arch": "amd64",
            "pg_coupled": pg_coupled, "build_pg_major": bpm, "build_pg_version": bpv}


def test_pg_coupled_string_value_preserved_and_fails_closed():
    planned = A.planned_cells_from_detector(det([_explicit("c", "true", "16", "16.4")]))
    assert planned[0]["pg_coupled"] == "true"                 # preserved, not rewritten to False
    assert R.reduce(assemble(planned, [], []))["plan_resolved"] is False


def test_decoupled_with_nonnull_build_pg_fails_closed():
    planned = A.planned_cells_from_detector(det([_explicit("c", False, "16", "16.4")]))
    assert planned[0]["build_pg_major"] == "16"               # preserved, not nulled
    assert R.reduce(assemble(planned, [], []))["plan_resolved"] is False


def test_valid_coupled_and_decoupled_cells_resolve():
    coupled = "pepcell.v1.rpm.el-9.amd64.spock.pg16"
    decoupled = "pepcell.v1.rpm.el-9.amd64.pkg"
    planned = A.planned_cells_from_detector(det([
        _explicit(coupled, True, "16", "16.4"),
        _explicit(decoupled, False, None, None)]))
    plan = R.reduce(assemble(planned, [], []))
    assert plan["plan_resolved"] is True
    by = cells_by_id(plan)
    assert by[coupled]["pg_coupled"] is True and by[coupled]["build_pg_major"] == "16"
    assert by[decoupled]["pg_coupled"] is False and by[decoupled]["build_pg_major"] is None


# --- (3) strict id validation -------------------------------------------------
def test_combine_pages_rejects_nonobject_and_bad_ids():
    with pytest.raises(A.AdapterError):
        A.combine_pages([{"total_count": 1, "jobs": ["nope"]}], "jobs")        # record not object
    with pytest.raises(A.AdapterError):
        A.combine_pages([{"total_count": 1, "jobs": [{"name": "x"}]}], "jobs")  # missing id
    for bad in (0, -1, True, "5", 1.0, None):
        with pytest.raises(A.AdapterError):
            A.combine_pages([{"total_count": 1, "jobs": [{"id": bad}]}], "jobs")


def test_artifact_associated_invalid_id_fails_closed():
    for bad_id in (None, 0, -3, True, "5"):
        inv = [{"id": bad_id, "name": "p [pep-cell.cA]", "expired": False, "members": []}]
        with pytest.raises(A.AdapterError):
            A.artifact_records(inv, ["cA"])
    # receipt path: a receipt with a bad artifact_id also fails closed
    with pytest.raises(A.AdapterError):
        A.artifact_records([{"id": 5, "name": "n", "expired": False}], ["cA"],
                           receipts=[{"artifact_id": True, "cell_id": "cA"}])


def test_unrelated_artifact_with_bad_id_is_ignored_not_raised():
    assert A.artifact_records([{"id": None, "name": "logs.zip", "expired": False}], ["cA"]) == []


def test_job_associated_invalid_id_fails_closed():
    for bad_id in (None, 0, -1, True, "5"):
        jobs = [{"id": bad_id, "name": "b [pep-cell:cA]", "run_attempt": 1,
                 "status": "completed", "conclusion": "success"}]
        with pytest.raises(A.AdapterError):
            A.job_records_from_jobs(jobs, ["cA"])
    # an UNRELATED job with a bad id is still just ignored
    assert A.job_records_from_jobs(
        [{"id": None, "name": "housekeeping", "run_attempt": 1,
          "status": "completed", "conclusion": "success"}], ["cA"]) == []


# --- (4) totality: no raw TypeError/KeyError leaks on malformed top input -----
def _total(fn):
    try:
        fn()
    except A.AdapterError:
        return
    except Exception as e:                                    # pragma: no cover - failure path
        raise AssertionError("leaked %s: %r" % (type(e).__name__, e))


MALFORMED_TOP = [None, 5, 3.5, True, False, "x", {"k": 1}, [1, "a", None], [[]], [{}]]


def test_public_functions_total_on_malformed_toplevel():
    for bad in MALFORMED_TOP:
        _total(lambda b=bad: A.planned_cells_from_detector(b))
        _total(lambda b=bad: A.job_records_from_jobs(b, ["cA"]))
        _total(lambda b=bad: A.job_records_from_jobs([], b))          # planned_cell_ids malformed
        _total(lambda b=bad: A.artifact_records(b, ["cA"]))
        _total(lambda b=bad: A.artifact_records([], ["cA"], receipts=b))
        _total(lambda b=bad: A.combine_pages(b, "jobs"))
        _total(lambda b=bad: A.normalize_publication_results(b))
        _total(lambda b=bad: A.assemble_reducer_input(
            planned_cells=b, job_records=b, artifacts=b, publication_results={},
            release_intent=b, component_policy=b, provenance=b))


# --- (6) marker scanning: 2nd unmatched marker + padded id fail ---------------
def test_job_marker_valid_plus_unmatched_second_fails():
    with pytest.raises(A.AdapterError):
        A.job_records_from_jobs(
            [{"id": 1, "name": "x [pep-cell:cA] [pep-cell:", "run_attempt": 1,
              "status": "completed", "conclusion": "success"}], ["cA"])


def test_job_marker_padded_id_rejected_not_trimmed():
    with pytest.raises(A.AdapterError):
        A.job_records_from_jobs(
            [{"id": 1, "name": "x [pep-cell: cA ]", "run_attempt": 1,
              "status": "completed", "conclusion": "success"}], ["cA", " cA "])


def test_artifact_marker_valid_plus_unmatched_second_fails():
    with pytest.raises(A.AdapterError):
        A.artifact_records([{"id": 1, "name": "p [pep-cell.cA] [pep-cell.", "expired": False}], ["cA"])


def test_artifact_marker_padded_id_rejected():
    with pytest.raises(A.AdapterError):
        A.artifact_records([{"id": 1, "name": "p [pep-cell. cA ]", "expired": False}], ["cA", " cA "])


# --- (7) source artifact name retained as inert audit, join key separate ------
def test_source_artifact_name_is_inert_audit_in_reducer_input():
    planned = A.planned_cells_from_detector(det([dcell("cA")]))
    arts = A.artifact_records([mk_art("cA", 10, members=[])], ["cA"])
    assert arts[0]["source_artifact_name"] == "pkg-cA [pep-cell.cA]"
    assert arts[0]["name"] == "cA"                            # canonical join key stays separate
    plan = R.reduce(assemble(planned, [mk_and_reduce_job("cA")], arts, pubs={"rpm": "success"}))
    assert cells_by_id(plan)["cA"]["build_state"] == "available"


# =====================================================================
# Final fail-closed pass: strict expiry, source name, items_key
# =====================================================================
_RCPT = [{"artifact_id": 1, "cell_id": "cA"}]


# --- (1) expiry must be present and boolean -----------------------------------
def test_expired_false_usable_true_absent():
    assert len(A.artifact_records([mk_art("cA", 1, members=[], expired=False)], ["cA"])) == 1
    assert A.artifact_records([mk_art("cA", 1, members=[], expired=True)], ["cA"]) == []


def test_malformed_expiry_fails_closed_marker_and_receipt():
    for exp in (None, "false", "true", 0, 1, [], {}):
        with pytest.raises(A.AdapterError):    # marker path
            A.artifact_records([{"id": 1, "name": "p [pep-cell.cA]", "expired": exp, "members": []}], ["cA"])
        with pytest.raises(A.AdapterError):    # receipt path
            A.artifact_records([{"id": 1, "name": "rpm__el-9__amd64", "expired": exp, "members": []}],
                               ["cA"], receipts=_RCPT)
    # expiry key entirely missing
    with pytest.raises(A.AdapterError):
        A.artifact_records([{"id": 1, "name": "p [pep-cell.cA]", "members": []}], ["cA"])
    with pytest.raises(A.AdapterError):
        A.artifact_records([{"id": 1, "name": "rpm__el-9__amd64", "members": []}], ["cA"], receipts=_RCPT)


def test_malformed_expiry_cannot_yield_eligible_target():
    # An otherwise fully-eligible RAG2 artifact with a malformed expiry must fail
    # closed, so no plan and no eligible target can be produced from it.
    rag2 = load("rag2_members.json")
    rpm_id = "pepcell.v1.rpm.el-9.amd64.pkg"
    inv = [{"id": 9894859799, "name": "rpm__el-9__amd64", "expired": "false",
            "members": rag2["artifacts"][0]["members"]}]
    with pytest.raises(A.AdapterError):
        A.artifact_records(inv, [rpm_id], receipts=[{"artifact_id": 9894859799, "cell_id": rpm_id}])


# --- (2) associated artifact needs a nonblank string source name --------------
def test_receipt_only_blank_or_missing_source_name_fails_closed():
    base = {"id": 1, "expired": False, "members": []}
    with pytest.raises(A.AdapterError):                       # missing name
        A.artifact_records([dict(base)], ["cA"], receipts=_RCPT)
    for nm in (None, 5, "", "   "):
        e = dict(base); e["name"] = nm
        with pytest.raises(A.AdapterError):
            A.artifact_records([e], ["cA"], receipts=_RCPT)


def test_source_name_preserved_exactly_not_trimmed():
    inv = [{"id": 1, "name": "  rpm__el-9__amd64  ", "expired": False, "members": []}]
    recs = A.artifact_records(inv, ["cA"], receipts=_RCPT)
    assert recs[0]["source_artifact_name"] == "  rpm__el-9__amd64  "   # exact, no trim
    assert recs[0]["name"] == "cA"                                     # join key separate


# --- (3) combine_pages items_key must be a nonblank string --------------------
def test_combine_pages_items_key_must_be_nonblank_string():
    pages = [{"total_count": 1, "jobs": [{"id": 1}]}]
    for bad in ([], {}, None, "", "   ", 5, True):
        with pytest.raises(A.AdapterError):
            A.combine_pages(pages, bad)

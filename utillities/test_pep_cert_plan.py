"""Offline tests for the cert-plan/1 reducer (utillities/pep_cert_plan.py).

Pure/deterministic: no network, no docker, no rpm/dpkg. Uses committed compact
fixtures (cert_plan_fixtures/) derived from Spike 0 evidence + real RAG2 metadata,
plus small synthetic inputs for the pure-logic branches.
"""
import copy
import json
from pathlib import Path

import pep_cert_plan as R

FX = Path(__file__).parent / "cert_plan_fixtures"


def load(name):
    with open(FX / name) as fh:
        return json.load(fh)


def cells_by_id(plan):
    # first record wins if duplicate ids are emitted (duplicates are all ambiguous)
    out = {}
    for c in plan["cells"]:
        out.setdefault(c["cell_id"], c)
    return out


# ---- helpers to build small synthetic reducer inputs -----------------------
def _cell(cid, family="rpm", os="el-9", arch="amd64", artifact=None):
    return {"cell_id": cid, "artifact_name": artifact or ("art-" + cid),
            "family": family, "os": os, "normalized_arch": arch}


def _job(cid, job_id, attempt, conclusion="success", status="completed"):
    return {"cell_id": cid, "job_id": job_id, "run_attempt": attempt,
            "status": status, "conclusion": conclusion}


def _member(name, native_arch, klass="runtime", version="2.0.0", release="1.el9", sha="0" * 64):
    return {"package_name": name, "epoch": None, "version": version, "release": release,
            "native_arch": native_arch, "package_class": klass,
            "source_filename": "%s-%s.%s" % (name, version, native_arch), "sha256": sha}


def _inp(cells, jobs, artifacts, *, allowed=None, pubs=None, simulated=False,
         version="2.0.0", buildnum="1", component="rag", channel="staging"):
    return {
        "provenance": {"repository": "pgEdge/example"},
        "release_intent": {"logical_component": component, "intended_version": version,
                           "intended_buildnum": buildnum, "effective_tag": "v" + version,
                           "channel": channel, "simulated": simulated},
        "component_policy": {"allowed_runtime_package_names": allowed or [],
                             "expected_binary_version": version},
        "planned_cells": cells, "job_records": jobs, "artifacts": artifacts,
        "publication_results": pubs or {}}


# ---- 1 & 2: Spike 0 attempt-2 / attempt-3 build states ---------------------
def test_attempt2_all_four_available():
    plan = R.reduce(load("spike0_attempt2.json"))
    by = cells_by_id(plan)
    assert set(by) == {"fj-A", "fj-B", "full-A", "full-B"}
    assert all(by[c]["build_state"] == "available" for c in by), \
        {c: by[c]["build_state"] for c in by}


def test_attempt3_fullA_failed_others_available():
    # Attempt 3 was "Re-run all jobs": every leg GENUINELY re-executed (the
    # carried-forward-job case is attempt 2). full-A failed before upload; the other
    # three succeeded and their exact stable artifacts are present.
    plan = R.reduce(load("spike0_attempt3.json"))
    by = cells_by_id(plan)
    assert by["full-A"]["build_state"] == "failed"
    assert by["fj-A"]["build_state"] == "available"
    assert by["fj-B"]["build_state"] == "available"
    assert by["full-B"]["build_state"] == "available"
    # availability derives from the exact stable artifact, never from
    # (producing attempt == jobs-API run_attempt).
    assert by["fj-A"]["build_evidence"]["latest_run_attempt"] == 3


# ---- 3 & 4: failure overrides artifact; success without artifact ------------
def test_latest_failure_overrides_present_artifact():
    cells = [_cell("c", artifact="art-c")]
    jobs = [_job("c", 1, 1, "success"), _job("c", 2, 2, "failure")]
    arts = [{"name": "art-c", "id": 9, "members": []}]        # artifact present but stale
    plan = R.reduce(_inp(cells, jobs, arts))
    c = cells_by_id(plan)["c"]
    assert c["build_evidence"]["artifact_present"] is True
    assert c["build_state"] == "failed"


def test_success_without_artifact_is_incomplete():
    cells = [_cell("c", artifact="art-c")]
    jobs = [_job("c", 1, 1, "success")]
    plan = R.reduce(_inp(cells, jobs, []))                    # no matching artifact
    assert cells_by_id(plan)["c"]["build_state"] == "incomplete"


# ---- 5: malformed / empty plan fails closed (global stop) ------------------
def test_empty_plan_unresolved_not_vacuous_success():
    plan = R.reduce(_inp([], [], []))
    assert plan["plan_resolved"] is False
    assert plan["cells"] == []
    assert plan["coverage_denominators"]["eligible_targets"] == 0


def test_planned_not_a_list_unresolved():
    inp = _inp([], [], [])
    inp["planned_cells"] = "oops"
    plan = R.reduce(inp)
    assert plan["plan_resolved"] is False and plan["cells"] == []
    assert plan["coverage_denominators"]["planned_build_cells"] == 0


def test_malformed_plan_entry_unresolved_and_denominator_honest():
    # 2 valid + 1 malformed: plan_resolved False, denominator honest (=3), no eligible.
    good = [_cell("c1"), _cell("c2")]
    plan = R.reduce(_inp(good + [{"cell_id": "bad"}],
                         [_job("c1", 1, 1), _job("c2", 2, 1)],
                         [{"name": "art-c1", "id": 1, "members": [_member("pkg", "x86_64")]},
                          {"name": "art-c2", "id": 2, "members": [_member("pkg", "x86_64")]}],
                         allowed=["pkg"], pubs={"rpm": "success"}))
    assert plan["plan_resolved"] is False
    assert any("invalid" in e for e in plan["errors"])
    assert plan["coverage_denominators"]["planned_build_cells"] == 3      # honest, not shrunk
    assert plan["coverage_denominators"]["eligible_targets"] == 0         # global stop
    bad = cells_by_id(plan)["bad"]
    assert bad["build_state"] == "ambiguous" and bad["invalid_reasons"]


def test_entry_not_an_object_is_invalid():
    inp = _inp([123], [], [])
    plan = R.reduce(inp)
    assert plan["plan_resolved"] is False
    assert plan["cells"][0]["invalid_reasons"] == ["not_an_object"]


# ---- 6: duplicate cell_id / artifact_name / job / artifact -----------------
def test_duplicate_cell_id_unresolved_and_ambiguous():
    cells = [_cell("c", artifact="a1"), _cell("c", artifact="a2")]
    plan = R.reduce(_inp(cells, [_job("c", 1, 1)], []))
    assert plan["plan_resolved"] is False
    assert [c["cell_id"] for c in plan["cells"]] == ["c", "c"]            # both emitted (honest)
    assert all(c["build_state"] == "ambiguous" for c in plan["cells"])
    assert plan["coverage_denominators"]["planned_build_cells"] == 2


def test_duplicate_artifact_name_across_cells_unresolved():
    cells = [_cell("c1", artifact="shared"), _cell("c2", artifact="shared")]
    plan = R.reduce(_inp(cells, [_job("c1", 1, 1), _job("c2", 2, 1)],
                         [{"name": "shared", "id": 1, "members": []}]))
    assert plan["plan_resolved"] is False
    by = cells_by_id(plan)
    assert by["c1"]["build_state"] == "ambiguous"
    assert by["c2"]["build_state"] == "ambiguous"
    assert by["c1"]["ambiguity_reason"] == "duplicate_artifact_name"
    assert plan["coverage_denominators"]["eligible_targets"] == 0


def test_duplicate_job_at_latest_attempt_is_ambiguous():
    cells = [_cell("c", artifact="art-c")]
    jobs = [_job("c", 10, 2, "success"), _job("c", 11, 2, "success")]   # two job ids @ attempt 2
    plan = R.reduce(_inp(cells, jobs, [{"name": "art-c", "id": 1, "members": []}]))
    assert cells_by_id(plan)["c"]["build_state"] == "ambiguous"


def test_exact_duplicate_job_records_deduplicated():
    cells = [_cell("c", artifact="art-c")]
    jobs = [_job("c", 5, 1, "success"), _job("c", 5, 1, "success")]     # exact dup
    plan = R.reduce(_inp(cells, jobs, [{"name": "art-c", "id": 1, "members": []}]))
    assert cells_by_id(plan)["c"]["build_state"] == "available"


def test_conflicting_duplicate_job_records_ambiguous_both_orders():
    cells = [_cell("c", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": []}]
    recs = [_job("c", 5, 2, "success"), _job("c", 5, 2, "failure")]
    a = cells_by_id(R.reduce(_inp(cells, recs, arts)))["c"]["build_state"]
    b = cells_by_id(R.reduce(_inp(cells, list(reversed(recs)), arts)))["c"]["build_state"]
    assert a == "ambiguous" and b == "ambiguous"


def test_duplicate_artifact_name_in_inventory_is_ambiguous():
    cells = [_cell("c", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": []}, {"name": "art-c", "id": 2, "members": []}]
    plan = R.reduce(_inp(cells, [_job("c", 1, 1, "success")], arts))
    assert cells_by_id(plan)["c"]["build_state"] == "ambiguous"


# ---- invalid run_attempt / job_id fail closed (no raise) -------------------
def test_null_run_attempt_fails_closed():
    cells = [_cell("c", artifact="art-c")]
    jobs = [_job("c", 1, 1), _job("c", 2, None)]              # None attempt
    plan = R.reduce(_inp(cells, jobs, [{"name": "art-c", "id": 1, "members": []}]))
    c = cells_by_id(plan)["c"]
    assert c["build_state"] == "ambiguous"
    assert c["build_evidence"]["invalid_reason"] == "invalid_run_attempt"


def test_zero_and_bool_run_attempt_invalid():
    cells = [_cell("c", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": []}]
    for att in (0, -1, True):                                 # not a positive integer
        plan = R.reduce(_inp(cells, [_job("c", 1, att)], arts))
        assert cells_by_id(plan)["c"]["build_state"] == "ambiguous", att


def test_list_job_id_fails_closed_without_raising():
    cells = [_cell("c", artifact="art-c")]
    plan = R.reduce(_inp(cells, [_job("c", [1, 2], 1)], [{"name": "art-c", "id": 1, "members": []}]))
    c = cells_by_id(plan)["c"]
    assert c["build_state"] == "ambiguous" and c["build_evidence"]["invalid_reason"] == "invalid_job_id"
    assert plan["plan_resolved"] is False


# ---- simulated coercion ----------------------------------------------------
def test_simulated_strict_parsing():
    # only bool / 'true' / 'false' are valid; everything else is invalid -> null + error
    for raw, expected in (("false", False), ("true", True), ("FALSE", False),
                          (False, False), (True, True)):
        plan = R.reduce(_inp([_cell("c")], [], [], simulated=raw))
        assert plan["release_intent"]["simulated"] is expected, raw
    for raw in ("0", "1", "yes", "maybe", "", None):
        plan = R.reduce(_inp([_cell("c")], [], [], simulated=raw))
        assert plan["release_intent"]["simulated"] is None, raw
        assert any("simulated" in e for e in plan["errors"]), raw


def test_simulated_yields_no_eligible_targets():
    inp = load("rag2_members.json")
    inp = dict(inp, release_intent=dict(inp["release_intent"], simulated="true"))
    plan = R.reduce(inp)
    assert plan["release_intent"]["simulated"] is True
    assert plan["coverage_denominators"]["eligible_targets"] == 0
    for c in plan["cells"]:
        for t in c["targets"]:
            assert t["eligibility_reason"] == "simulated_not_eligible"


# ---- publication states ----------------------------------------------------
def test_publication_states():
    cells = [_cell("c", artifact="art-c")]
    jobs = [_job("c", 1, 1, "success")]
    arts = [{"name": "art-c", "id": 1, "members": []}]
    for push, expected in (("success", "publish_confirmed"), ("failure", "publish_unconfirmed"),
                           ("cancelled", "publish_unconfirmed"), ("skipped", "publish_skipped")):
        plan = R.reduce(_inp(cells, jobs, arts, pubs={"rpm": push}))
        assert cells_by_id(plan)["c"]["publication_state"] == expected, push
    plan = R.reduce(_inp(cells, jobs, arts, pubs={}))
    assert cells_by_id(plan)["c"]["publication_state"] == "publish_skipped"


# ---- force-push partial build selects only available cells -----------------
def test_force_push_partial_only_available_eligible():
    cells = [_cell("ok", artifact="art-ok"), _cell("bad", artifact="art-bad")]
    jobs = [_job("ok", 1, 1, "success"), _job("bad", 2, 1, "failure")]
    arts = [{"name": "art-ok", "id": 1, "members": [_member("pgedge-x", "x86_64")]}]
    plan = R.reduce(_inp(cells, jobs, arts, allowed=["pgedge-x"], pubs={"rpm": "success"}))
    by = cells_by_id(plan)
    assert by["ok"]["build_state"] == "available" and by["bad"]["build_state"] == "failed"
    assert by["ok"]["publication_state"] == "publish_confirmed"
    assert by["bad"]["publication_reason"] == "not_built"
    assert plan["coverage_denominators"]["eligible_targets"] == 1
    assert [t["eligibility"] for t in by["ok"]["targets"]] == ["eligible"]
    assert by["bad"]["targets"] == []


# ---- RAG2 member selection, normalization, DEB -----------------------------
def test_rag2_rpm_source_excluded_binary_selected():
    plan = R.reduce(load("rag2_members.json"))
    rpm = cells_by_id(plan)["rpm:el-9:amd64"]
    classes = {(m["native_arch"], m["package_class"], m["selected"]) for m in rpm["members"]}
    assert ("src", "source", False) in classes           # source retained, excluded
    assert ("x86_64", "runtime", True) in classes        # binary selected
    assert len(rpm["targets"]) == 1
    t = rpm["targets"][0]
    assert t["physical_package"] == "pgedge-rag-server2"
    assert t["logical_component"] == "rag"
    assert t["producer_repo"] == "pgEdge/pgedge-rag-server"
    assert t["native_package_arch"] == "x86_64" and t["execution_arch"] == "amd64"
    assert t["target_id"] == "rpm:el-9:amd64::pgedge-rag-server2"    # cell_id::physical
    assert t["package_identity_state"] == "confirmed" and t["eligibility"] == "eligible"


def test_native_arch_normalization():
    assert R.normalize_arch("x86_64") == "amd64"
    assert R.normalize_arch("aarch64") == "arm64"
    cells = [_cell("rpm:el-9:arm64", os="el-9", arch="arm64", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1,
             "members": [_member("pgedge-x", "aarch64"), _member("pgedge-x", "src", klass="source")]}]
    plan = R.reduce(_inp(cells, [_job("rpm:el-9:arm64", 1, 1, "success")], arts,
                         allowed=["pgedge-x"], pubs={"rpm": "success"}))
    t = cells_by_id(plan)["rpm:el-9:arm64"]["targets"][0]
    assert t["native_package_arch"] == "aarch64" and t["execution_arch"] == "arm64"
    assert t["target_id"] == "rpm:el-9:arm64::pgedge-x"


def test_rag2_deb_runtime_selected():
    plan = R.reduce(load("rag2_members.json"))
    deb = cells_by_id(plan)["deb:bookworm:amd64"]
    assert deb["build_state"] == "available"
    t = deb["targets"][0]
    assert t["family"] == "deb" and t["native_package_arch"] == "amd64" and t["execution_arch"] == "amd64"
    assert t["target_id"] == "deb:bookworm:amd64::pgedge-rag-server2"
    assert t["package_identity_state"] == "confirmed"


# ---- family-aware, cell-aware identity: final + prerelease, RPM + DEB -------
def _mem(version, release, epoch=None):
    return {"package_name": "p", "version": version, "release": release,
            "native_arch": "x86_64", "epoch": epoch}


def test_identity_rpm_final_and_prerelease():
    S = R._package_identity_state
    assert S(_mem("2.0.0", "1.el9"), "rpm", "el-9", "2.0.0", "1") == "confirmed"
    assert S(_mem("2.0.0", "test1_1.el9"), "rpm", "el-9", "2.0.0", "test1_1") == "confirmed"
    assert S(_mem("2.0.0", "rc1_1.el10"), "rpm", "el-10", "2.0.0", "rc1_1") == "confirmed"
    assert S(_mem("2.0.0", "beta3_1.el9"), "rpm", "el-9", "2.0.0", "beta3_1") == "confirmed"
    assert S(_mem("2.0.1", "1.el9"), "rpm", "el-9", "2.0.0", "1") == "mismatch"     # wrong version
    assert S(_mem("2.0.0", "2.el9"), "rpm", "el-9", "2.0.0", "1") == "mismatch"     # wrong build


def test_identity_rpm_dist_must_match_cell():        # defect 1 (RPM)
    S = R._package_identity_state
    assert S(_mem("2.0.0", "1.el10"), "rpm", "el-9", "2.0.0", "1") == "mismatch"    # el10 in el-9 cell
    assert S(_mem("2.0.0", "1.el9"), "rpm", "el-10", "2.0.0", "1") == "mismatch"


def test_identity_deb_final_and_prerelease_fold():
    S = R._package_identity_state
    assert S(_mem("2.0.0", "1.bookworm"), "deb", "bookworm", "2.0.0", "1") == "confirmed"
    assert S(_mem("2.0.0~beta3", "1.trixie"), "deb", "trixie", "2.0.0", "beta3_1") == "confirmed"
    assert S(_mem("2.0.0~test1", "1.noble"), "deb", "noble", "2.0.0", "test1_1") == "confirmed"
    assert S(_mem("2.0.0", "beta3_1.bookworm"), "deb", "bookworm", "2.0.0", "beta3_1") == "mismatch"


def test_identity_deb_distro_must_match_cell():      # defect 1 (DEB)
    S = R._package_identity_state
    assert S(_mem("2.0.0", "1.bookworm"), "deb", "bullseye", "2.0.0", "1") == "mismatch"


def test_identity_buildnum_with_dot_keeps_distro():  # defect 8
    S = R._package_identity_state
    assert S(_mem("2.0.0", "1.2.el9"), "rpm", "el-9", "2.0.0", "1.2") == "confirmed"
    assert S(_mem("2.0.0", "1.2.el9"), "rpm", "el-10", "2.0.0", "1.2") == "mismatch"


def test_identity_epoch_unverified():                # defect 7
    S = R._package_identity_state
    assert S(_mem("2.0.0", "1.el9", epoch="1"), "rpm", "el-9", "2.0.0", "1") == "unverified"
    assert S(_mem("2.0.0", "1.el9", epoch=2), "rpm", "el-9", "2.0.0", "1") == "unverified"
    # a zero / empty epoch does not block
    assert S(_mem("2.0.0", "1.el9", epoch="0"), "rpm", "el-9", "2.0.0", "1") == "confirmed"
    assert S(_mem("2.0.0", "1.el9", epoch=0), "rpm", "el-9", "2.0.0", "1") == "confirmed"


def test_identity_unknown_family_and_missing_intent_unverified():
    S = R._package_identity_state
    assert S(_mem("2.0.0", "1.el9"), "exe", "el-9", "2.0.0", "1") == "unverified"
    assert S(_mem("2.0.0", "1.el9"), "rpm", "el-9", "", "1") == "unverified"
    assert S(_mem("2.0.0", "1"), "rpm", "el-9", "2.0.0", "1") == "mismatch"   # missing dist


def test_prerelease_end_to_end_eligible():
    cells = [_cell("rpm:el-9:amd64", os="el-9", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1,
             "members": [_member("pgedge-rag-server2", "x86_64", release="rc1_1.el9")]}]
    plan = R.reduce(_inp(cells, [_job("rpm:el-9:amd64", 1, 1, "success")], arts,
                         allowed=["pgedge-rag-server2"], pubs={"rpm": "success"}, buildnum="rc1_1"))
    t = cells_by_id(plan)["rpm:el-9:amd64"]["targets"][0]
    assert t["package_identity_state"] == "confirmed" and t["eligibility"] == "eligible"


def test_identity_mismatch_blocks_eligibility():
    inp = dict(load("rag2_members.json"))
    inp = dict(inp, release_intent=dict(inp["release_intent"], intended_version="2.0.1"))
    t = cells_by_id(R.reduce(inp))["rpm:el-9:amd64"]["targets"][0]
    assert t["package_identity_state"] == "mismatch" and t["eligibility"] == "ineligible"
    assert t["eligibility_reason"] == "identity_mismatch"


# ---- unsupported architecture never eligible -------------------------------
def test_unsupported_cell_arch_not_eligible():
    cells = [_cell("c", os="weird", arch="mips64", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": [_member("pgedge-x", "mips64")]}]
    plan = R.reduce(_inp(cells, [_job("c", 1, 1, "success")], arts,
                         allowed=["pgedge-x"], pubs={"rpm": "success"}))
    c = cells_by_id(plan)["c"]
    assert c["target_selection_state"] == "target_unresolved"
    assert plan["coverage_denominators"]["eligible_targets"] == 0


def test_unsupported_member_arch_excluded():
    cells = [_cell("c", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": [_member("pgedge-x", "ppc64le")]}]
    plan = R.reduce(_inp(cells, [_job("c", 1, 1, "success")], arts,
                        allowed=["pgedge-x"], pubs={"rpm": "success"}))
    m = cells_by_id(plan)["c"]["members"][0]
    assert m["selected"] is False and "unsupported_arch" in m["exclusion_reasons"]


# ---- evidence required to certify ------------------------------------------
def test_missing_checksum_not_certified():
    cells = [_cell("c", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": [_member("pgedge-x", "x86_64", sha="")]}]
    plan = R.reduce(_inp(cells, [_job("c", 1, 1, "success")], arts,
                        allowed=["pgedge-x"], pubs={"rpm": "success"}))
    c = cells_by_id(plan)["c"]
    assert c["target_selection_state"] == "target_unresolved"
    assert "missing_checksum" in c["members"][0]["exclusion_reasons"]


def test_invalid_sha256_not_selected():                     # defect 6
    cells = [_cell("c", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": [_member("pgedge-x", "x86_64", sha="zzz")]}]
    plan = R.reduce(_inp(cells, [_job("c", 1, 1, "success")], arts,
                         allowed=["pgedge-x"], pubs={"rpm": "success"}))
    c = cells_by_id(plan)["c"]
    assert "invalid_checksum" in c["members"][0]["exclusion_reasons"]
    assert c["target_selection_state"] == "target_unresolved"
    arts2 = [{"name": "art-c", "id": 1, "members": [_member("pgedge-x", "x86_64", sha="A" * 64)]}]
    p2 = R.reduce(_inp(cells, [_job("c", 1, 1, "success")], arts2,
                       allowed=["pgedge-x"], pubs={"rpm": "success"}))
    assert p2["cells"][0]["targets"][0]["eligibility"] == "eligible"   # valid 64-hex accepted


# ---- required simulated + release-context gates ----------------------------
def test_missing_simulated_non_certifiable():               # defect 2
    inp = _inp([_cell("c", artifact="art-c")], [_job("c", 1, 1, "success")],
               [{"name": "art-c", "id": 1, "members": [_member("pgedge-x", "x86_64")]}],
               allowed=["pgedge-x"], pubs={"rpm": "success"})
    del inp["release_intent"]["simulated"]
    plan = R.reduce(inp)
    assert plan["release_intent"]["simulated"] is None
    assert plan["coverage_denominators"]["eligible_targets"] == 0
    assert cells_by_id(plan)["c"]["targets"][0]["eligibility_reason"] == "incomplete_release_context"


def test_incomplete_release_context_blocks_eligibility():   # defect 3
    base = _inp([_cell("c", artifact="art-c")], [_job("c", 1, 1, "success")],
                [{"name": "art-c", "id": 1, "members": [_member("pgedge-x", "x86_64")]}],
                allowed=["pgedge-x"], pubs={"rpm": "success"})
    for over in ({"logical_component": ""}, {"effective_tag": ""}):
        i = dict(base, release_intent=dict(base["release_intent"], **over))
        assert R.reduce(i)["coverage_denominators"]["eligible_targets"] == 0, over
    p = R.reduce(dict(base, provenance={}))               # missing producer repo
    assert p["coverage_denominators"]["eligible_targets"] == 0
    t = p["cells"][0]["targets"][0]
    assert t["producer_repo"] is None and t["eligibility_reason"] == "incomplete_release_context"


# ---- malformed top-level / nested shapes never raise -----------------------
def test_non_dict_input_fails_closed():                     # defect 4
    for bad in ("not-a-dict", None, [1, 2, 3], 7):
        plan = R.reduce(bad)
        assert plan["plan_resolved"] is False and plan["cells"] == []
    assert "input is not an object" in R.reduce("x")["errors"]


def test_non_list_members_keep_build_evidence():            # defect 4
    cells = [_cell("c", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": 123}]     # malformed members value
    plan = R.reduce(_inp(cells, [_job("c", 1, 1, "success")], arts,
                         allowed=["pgedge-x"], pubs={"rpm": "success"}))
    c = cells_by_id(plan)["c"]
    assert c["build_state"] == "available"                  # build evidence not hidden
    assert c["target_selection_state"] == "target_unresolved" and c["members"] == []


# ---- noarch execution arch (defect 5) --------------------------------------
def test_noarch_target_exposes_valid_execution_arch():
    cells = [_cell("rpm:el-9:amd64", os="el-9", arch="amd64", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": [_member("pgedge-x", "noarch")]}]
    plan = R.reduce(_inp(cells, [_job("rpm:el-9:amd64", 1, 1, "success")], arts,
                         allowed=["pgedge-x"], pubs={"rpm": "success"}))
    t = cells_by_id(plan)["rpm:el-9:amd64"]["targets"][0]
    assert t["native_package_arch"] == "noarch"
    assert t["execution_arch"] == "amd64" and "normalized_arch" not in t
    assert t["eligibility"] == "eligible"


# ---- epoch end-to-end (defect 7) -------------------------------------------
def test_epoch_end_to_end_ineligible():
    cells = [_cell("c", artifact="art-c")]
    m = _member("pgedge-x", "x86_64")
    m["epoch"] = "1"
    arts = [{"name": "art-c", "id": 1, "members": [m]}]
    plan = R.reduce(_inp(cells, [_job("c", 1, 1, "success")], arts,
                         allowed=["pgedge-x"], pubs={"rpm": "success"}))
    t = cells_by_id(plan)["c"]["targets"][0]
    assert t["package_identity_state"] == "unverified" and t["eligibility"] == "ineligible"


# ---- zero / multiple / unapproved runtime ----------------------------------
def test_zero_runtime_candidate_unresolved():
    cells = [_cell("c", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": [_member("pgedge-x", "src", klass="source")]}]
    plan = R.reduce(_inp(cells, [_job("c", 1, 1, "success")], arts, allowed=["pgedge-x"]))
    c = cells_by_id(plan)["c"]
    assert c["build_state"] == "available" and c["target_selection_state"] == "target_unresolved"


def test_multiple_runtime_targets_per_cell():
    cells = [_cell("c", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": [
        _member("pgedge-a", "x86_64"), _member("pgedge-b", "x86_64"),
        _member("pgedge-a", "src", klass="source")]}]
    plan = R.reduce(_inp(cells, [_job("c", 1, 1, "success")], arts,
                         allowed=["pgedge-a", "pgedge-b"], pubs={"rpm": "success"}))
    c = cells_by_id(plan)["c"]
    assert c["target_selection_state"] == "resolved"
    assert sorted(t["physical_package"] for t in c["targets"]) == ["pgedge-a", "pgedge-b"]


def test_duplicate_same_runtime_package_is_target_ambiguous():
    cells = [_cell("c", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": [
        _member("pgedge-a", "x86_64", version="2.0.0"),
        _member("pgedge-a", "x86_64", version="2.0.1")]}]
    plan = R.reduce(_inp(cells, [_job("c", 1, 1, "success")], arts, allowed=["pgedge-a"]))
    assert cells_by_id(plan)["c"]["target_selection_state"] == "target_ambiguous"


def test_unapproved_runtime_not_selected():
    cells = [_cell("c", artifact="art-c")]
    arts = [{"name": "art-c", "id": 1, "members": [_member("not-allowed", "x86_64")]}]
    plan = R.reduce(_inp(cells, [_job("c", 1, 1, "success")], arts, allowed=["pgedge-x"]))
    c = cells_by_id(plan)["c"]
    assert all(m["selected"] is False for m in c["members"])
    assert c["target_selection_state"] == "target_unresolved"


# ---- noarch selection + global target_id uniqueness ------------------------
def test_noarch_selected_and_target_ids_globally_unique():
    cells = [_cell("rpm:el-9:amd64", os="el-9", arch="amd64", artifact="a-amd"),
             _cell("rpm:el-9:arm64", os="el-9", arch="arm64", artifact="a-arm")]
    arts = [{"name": "a-amd", "id": 1, "members": [_member("pgedge-x", "noarch")]},
            {"name": "a-arm", "id": 2, "members": [_member("pgedge-x", "noarch")]}]
    plan = R.reduce(_inp(cells, [_job("rpm:el-9:amd64", 1, 1, "success"),
                                 _job("rpm:el-9:arm64", 2, 1, "success")],
                         arts, allowed=["pgedge-x"], pubs={"rpm": "success"}))
    tids = [t["target_id"] for c in plan["cells"] for t in c["targets"]]
    assert tids == ["rpm:el-9:amd64::pgedge-x", "rpm:el-9:arm64::pgedge-x"]
    assert len(tids) == len(set(tids))                       # globally unique incl noarch


# ---- unsupported channel gate ----------------------------------------------
def test_unknown_channel_blocks_eligibility():
    plan = R.reduce(_inp([_cell("c", artifact="art-c")], [_job("c", 1, 1, "success")],
                         [{"name": "art-c", "id": 1, "members": [_member("pgedge-x", "x86_64")]}],
                         allowed=["pgedge-x"], pubs={"rpm": "success"}, channel="nightly"))
    t = cells_by_id(plan)["c"]["targets"][0]
    assert t["eligibility_reason"] == "unsupported_channel"


# ---- determinism / order permutation ---------------------------------------
def test_deterministic_serialization_and_order_permutation():
    inp = load("spike0_attempt3.json")
    a = R.to_json(R.reduce(inp))
    assert a == R.to_json(R.reduce(inp))                     # repeated
    perm = dict(inp, job_records=list(reversed(inp["job_records"])))
    assert R.to_json(R.reduce(perm)) == a                    # order-independent
    plan = R.reduce(inp)
    assert [c["cell_id"] for c in plan["cells"]] == sorted(c["cell_id"] for c in plan["cells"])
    assert plan["cert_result_boundary"]["execution_status_vocab"] == \
        ["completed", "preview", "incomplete", "infra_failure"]
    assert plan["cert_result_boundary"]["test_verdict_vocab"] == ["pass", "fail", "not_run"]


def test_members_sorted_deterministically_under_permutation():
    cells = [_cell("c", artifact="art-c")]
    ms = [_member("pgedge-b", "x86_64"), _member("pgedge-a", "src", klass="source"),
          _member("pgedge-a", "x86_64")]
    j = [_job("c", 1, 1, "success")]
    p1 = R.reduce(_inp(cells, j, [{"name": "art-c", "id": 1, "members": ms}], allowed=["pgedge-a", "pgedge-b"]))
    p2 = R.reduce(_inp(cells, j, [{"name": "art-c", "id": 1, "members": list(reversed(ms))}],
                       allowed=["pgedge-a", "pgedge-b"]))
    assert R.to_json(p1) == R.to_json(p2)
    keys = [(m["package_name"], m["native_arch"]) for m in cells_by_id(p1)["c"]["members"]]
    assert keys == sorted(keys)


# ---- envelope validation boundary ------------------------------------------
def _valid_envelope():
    """A structurally sound input with one eligible target."""
    return _inp([_cell("rpm:el-9:amd64", os="el-9", artifact="art-c")],
                [_job("rpm:el-9:amd64", 1, 1, "success")],
                [{"name": "art-c", "id": 1, "members": [_member("pgedge-x", "x86_64")]}],
                allowed=["pgedge-x"], pubs={"rpm": "success"})


def test_valid_envelope_baseline_is_eligible():
    plan = R.reduce(_valid_envelope())
    assert plan["plan_resolved"] is True
    assert plan["coverage_denominators"]["eligible_targets"] == 1


def _mut(fn):
    d = _valid_envelope()
    fn(d)
    return d


def test_malformed_envelope_table_never_raises_never_eligible():
    def set_member(d, k, v):
        d["artifacts"][0]["members"][0][k] = v
    cases = {
        "release_intent not object":   _mut(lambda d: d.__setitem__("release_intent", [])),
        "component_policy not object":  _mut(lambda d: d.__setitem__("component_policy", 7)),
        "provenance not object":        _mut(lambda d: d.__setitem__("provenance", "x")),
        "job_records not list":         _mut(lambda d: d.__setitem__("job_records", "nope")),
        "artifacts not list":           _mut(lambda d: d.__setitem__("artifacts", {})),
        "publication_results not dict": _mut(lambda d: d.__setitem__("publication_results", [])),
        "allowed has object":           _mut(lambda d: d["component_policy"].__setitem__(
                                            "allowed_runtime_package_names", [{}])),
        "allowed not unique":           _mut(lambda d: d["component_policy"].__setitem__(
                                            "allowed_runtime_package_names", ["pgedge-x", "pgedge-x"])),
        "allowed has blank":            _mut(lambda d: d["component_policy"].__setitem__(
                                            "allowed_runtime_package_names", ["pgedge-x", " "])),
        "job_id is list":               _mut(lambda d: d["job_records"][0].__setitem__("job_id", [1])),
        "run_attempt zero":             _mut(lambda d: d["job_records"][0].__setitem__("run_attempt", 0)),
        "run_attempt string":           _mut(lambda d: d["job_records"][0].__setitem__("run_attempt", "1")),
        "cell_id non-string":           _mut(lambda d: d["job_records"][0].__setitem__("cell_id", 5)),
        "status non-string":            _mut(lambda d: d["job_records"][0].__setitem__("status", None)),
        "member not object":            _mut(lambda d: d["artifacts"][0].__setitem__("members", [7])),
        "members not list":             _mut(lambda d: d["artifacts"][0].__setitem__("members", "oops")),
        "member package_name int":      _mut(lambda d: set_member(d, "package_name", 1)),
        "member version int":           _mut(lambda d: set_member(d, "version", 2)),
        "member epoch object":          _mut(lambda d: set_member(d, "epoch", {})),
        "artifact name int":            _mut(lambda d: d["artifacts"][0].__setitem__("name", 9)),
        "intended_buildnum int":        _mut(lambda d: d["release_intent"].__setitem__("intended_buildnum", 5)),
        "logical_component numeric":    _mut(lambda d: d["release_intent"].__setitem__("logical_component", 123)),
        "effective_tag numeric":        _mut(lambda d: d["release_intent"].__setitem__("effective_tag", 1)),
        "provenance.repository numeric": _mut(lambda d: d["provenance"].__setitem__("repository", 1)),
        "simulated missing":            _mut(lambda d: d["release_intent"].pop("simulated")),
        "simulated bogus":              _mut(lambda d: d["release_intent"].__setitem__("simulated", "maybe")),
    }
    for name, bad in cases.items():
        plan = R.reduce(bad)                                  # must not raise
        assert plan["schema"] == "cert-plan/1", name
        assert plan["coverage_denominators"]["eligible_targets"] == 0, name
        assert plan["errors"], name                           # a deterministic diagnostic exists
        assert R.to_json(R.reduce(bad)) == R.to_json(plan), name   # deterministic


def test_mixed_plan_one_malformed_member_zero_eligible_globally():
    # rag2: two otherwise-valid cells; corrupt ONE artifact's members -> globally 0 eligible,
    # but build evidence for the still-representable cell is preserved.
    d = copy.deepcopy(load("rag2_members.json"))
    d["artifacts"][0]["members"] = "oops"                     # rpm cell malformed
    plan = R.reduce(d)
    assert plan["plan_resolved"] is False
    assert plan["coverage_denominators"]["eligible_targets"] == 0
    deb = cells_by_id(plan)["deb:bookworm:amd64"]
    assert deb["build_state"] == "available"                 # build evidence preserved
    assert all(t["eligibility"] == "ineligible" for t in deb["targets"])
    assert all(t["eligibility_reason"] == "plan_unresolved" for t in deb["targets"])


# ---- validate-then-use-raw-value raise paths (reduce()/to_json() must be total) ----
def _set_pol(v):
    return _mut(lambda d: d["component_policy"].__setitem__("allowed_runtime_package_names", v))


def _set_job(k, v):
    return _mut(lambda d: d["job_records"][0].__setitem__(k, v))


def _set_mem(k, v):
    return _mut(lambda d: d["artifacts"][0]["members"][0].__setitem__(k, v))


def test_raise_path_inputs_safe_and_non_certifiable():
    cases = {
        "allowlist scalar true":   _set_pol(True),
        "allowlist scalar string": _set_pol("pgedge-x"),
        "allowlist mixed object":  _set_pol(["pgedge-x", {}]),
        "allowlist mixed int":     _set_pol(["pgedge-x", 5]),
        "status list":             _set_job("status", ["completed"]),
        "status dict":             _set_job("status", {"x": 1}),
        "conclusion list":         _set_job("conclusion", ["success"]),
        "conclusion dict":         _set_job("conclusion", {"x": 1}),
        "package_name list":       _set_mem("package_name", ["pgedge-x"]),
        "package_name dict":       _set_mem("package_name", {"n": 1}),
        "native_arch list":        _set_mem("native_arch", ["x86_64"]),
        "native_arch dict":        _set_mem("native_arch", {"a": 1}),
    }
    for name, bad in cases.items():
        plan = R.reduce(bad)                                  # must not raise
        js = R.to_json(plan)                                  # must not raise
        assert plan["plan_resolved"] is False, name
        assert plan["errors"], name
        assert plan["coverage_denominators"]["eligible_targets"] == 0, name
        assert js == R.to_json(R.reduce(bad)), name           # deterministic


def test_malformed_job_status_resolves_ambiguously_with_reason():
    plan = R.reduce(_set_job("status", ["completed"]))
    c = cells_by_id(plan)["rpm:el-9:amd64"]
    assert c["build_state"] == "ambiguous"
    assert c["build_evidence"]["invalid_reason"] == "invalid_status_or_conclusion"
    assert c["build_evidence"]["artifact_present"] is True     # representable evidence kept


def test_malformed_member_identity_retained_unselected():
    plan = R.reduce(_set_mem("native_arch", ["x86_64"]))
    c = cells_by_id(plan)["rpm:el-9:amd64"]
    m = c["members"][0]
    assert m["selected"] is False and "non_string_identity_field" in m["exclusion_reasons"]
    assert c["target_selection_state"] == "target_unresolved"


def test_sanitized_allowlist_in_output_dedups_and_drops_nonstrings():
    # emitted component_policy uses the same sanitized string-only value (never the raw list)
    plan = R.reduce(_set_pol(["pgedge-x", "pgedge-x", {}]))
    assert plan["component_policy"]["allowed_runtime_package_names"] == ["pgedge-x"]


# ---- Stage 1.5: build-side PostgreSQL identity (nullable, validated) --------
def _pg_cell(cid="rpm:el-9:amd64", *, coupled=True, major="17", version="17.5",
            os="el-9", arch="amd64", artifact="art-c"):
    """A cell with optional build-PG fields set explicitly (None => key omitted)."""
    c = _cell(cid, os=os, arch=arch, artifact=artifact)
    if coupled is not None:
        c["pg_coupled"] = coupled
    if major is not None:
        c["build_pg_major"] = major
    if version is not None:
        c["build_pg_version"] = version
    return c


def _pg_run(cell):
    return R.reduce(_inp([cell], [_job(cell["cell_id"], 1, 1, "success")],
                         [{"name": cell["artifact_name"], "id": 1,
                           "members": [_member("pgedge-x", "x86_64")]}],
                         allowed=["pgedge-x"], pubs={"rpm": "success"}))


def test_pg_fields_omitted_backward_compatible():
    # legacy/RAG input omits all three -> pg_coupled=false + null build fields, still eligible.
    plan = R.reduce(_valid_envelope())
    c = cells_by_id(plan)["rpm:el-9:amd64"]
    assert c["pg_coupled"] is False and c["build_pg_major"] is None and c["build_pg_version"] is None
    t = c["targets"][0]
    assert t["pg_coupled"] is False and t["build_pg_major"] is None and t["build_pg_version"] is None
    assert plan["plan_resolved"] is True and t["eligibility"] == "eligible"


def test_pg_independent_explicit_and_blank_tolerated():
    # explicit PG-independent (false + null) AND blank detector metadata are both valid;
    # representative PG metadata must not become coupling, and must not fail the plan.
    for cell in (_pg_cell(coupled=False, major=None, version=None),
                 _pg_cell(coupled=False, major="", version="")):
        plan = _pg_run(cell)
        assert plan["plan_resolved"] is True
        c = cells_by_id(plan)["rpm:el-9:amd64"]
        assert c["pg_coupled"] is False and c["build_pg_major"] is None and c["build_pg_version"] is None
        assert c["targets"][0]["eligibility"] == "eligible"


def test_pg_coupled_valid_echoed_into_cell_and_target():
    plan = _pg_run(_pg_cell(coupled=True, major="17", version="17.5"))
    assert plan["plan_resolved"] is True
    c = cells_by_id(plan)["rpm:el-9:amd64"]
    assert c["pg_coupled"] is True and c["build_pg_major"] == "17" and c["build_pg_version"] == "17.5"
    t = c["targets"][0]
    assert t["pg_coupled"] is True and t["build_pg_major"] == "17" and t["build_pg_version"] == "17.5"
    assert t["eligibility"] == "eligible"        # PG identity is passthrough, not an eligibility gate
    # version whose major equals build_pg_major with no minor is also valid
    p2 = _pg_run(_pg_cell(coupled=True, major="18", version="18"))
    assert p2["plan_resolved"] is True and p2["coverage_denominators"]["eligible_targets"] == 1


def test_pg_coupled_invalid_combinations_fail_closed():
    cases = {
        "missing major":        _pg_cell(coupled=True, major=None, version="17.5"),
        "nonnumeric major":     _pg_cell(coupled=True, major="abc", version="abc.5"),
        "missing version":      _pg_cell(coupled=True, major="17", version=None),
        "version major mismatch": _pg_cell(coupled=True, major="17", version="16.2"),
        "pg fields with coupled=false": _pg_cell(coupled=False, major="17", version="17.5"),
    }
    for name, cell in cases.items():
        plan = _pg_run(cell)
        assert plan["plan_resolved"] is False, name
        assert plan["coverage_denominators"]["eligible_targets"] == 0, name
        assert any("pg identity" in e for e in plan["errors"]), name


def test_pg_coupled_non_boolean_and_malformed_never_raise():
    def cell_with(**over):
        c = _cell("rpm:el-9:amd64", os="el-9", artifact="art-c")
        c.update(over)
        return c
    cases = {
        "coupled string true":   cell_with(pg_coupled="true"),
        "coupled int":           cell_with(pg_coupled=1),
        "coupled list":          cell_with(pg_coupled=[]),
        "coupled dict":          cell_with(pg_coupled={}),
        "major list (coupled)":  cell_with(pg_coupled=True, build_pg_major=["17"], build_pg_version="17.5"),
        "major dict (coupled)":  cell_with(pg_coupled=True, build_pg_major={"m": 17}, build_pg_version="17.5"),
        "version list (coupled)": cell_with(pg_coupled=True, build_pg_major="17", build_pg_version=["17.5"]),
        "version dict (coupled)": cell_with(pg_coupled=True, build_pg_major="17", build_pg_version={"v": 1}),
        "major list (coupled=false)": cell_with(pg_coupled=False, build_pg_major=["17"]),
    }
    for name, cell in cases.items():
        plan = _pg_run(cell)
        js = R.to_json(plan)                                    # must not raise
        assert plan["plan_resolved"] is False, name
        assert plan["coverage_denominators"]["eligible_targets"] == 0, name
        assert plan["errors"], name
        assert js == R.to_json(_pg_run(cell)), name             # deterministic
        # malformed values collapse to a JSON-safe echo, never a raw list/dict
        c = cells_by_id(plan)["rpm:el-9:amd64"]
        assert isinstance(c["pg_coupled"], bool)
        assert c["build_pg_major"] is None or isinstance(c["build_pg_major"], str)
        assert c["build_pg_version"] is None or isinstance(c["build_pg_version"], str)


def test_pg_identity_deterministic_under_cell_order():
    a = _pg_cell("rpm:el-9:amd64", coupled=True, major="17", version="17.5",
                 os="el-9", arch="amd64", artifact="art-a")
    b = _pg_cell("rpm:el-10:amd64", coupled=True, major="18", version="18.1",
                 os="el-10", arch="amd64", artifact="art-b")
    jobs = [_job("rpm:el-9:amd64", 1, 1, "success"), _job("rpm:el-10:amd64", 2, 1, "success")]
    arts = [{"name": "art-a", "id": 1, "members": [_member("pgedge-x", "x86_64", release="1.el9")]},
            {"name": "art-b", "id": 2, "members": [_member("pgedge-x", "x86_64", release="1.el10")]}]
    p1 = R.to_json(R.reduce(_inp([a, b], jobs, arts, allowed=["pgedge-x"], pubs={"rpm": "success"})))
    p2 = R.to_json(R.reduce(_inp([b, a], list(reversed(jobs)), arts,
                                 allowed=["pgedge-x"], pubs={"rpm": "success"})))
    assert p1 == p2                                             # order-independent


def test_pg_invalid_makes_plan_unresolved_regardless_of_cell_order():
    # A valid+eligible cell and an invalid-PG cell: the global stop must be settled BEFORE
    # any target eligibility is computed, so BOTH orders give 0 eligible (no valid cell may
    # slip through as eligible just because it was processed first).
    valid = _pg_cell("rpm:el-9:amd64", coupled=True, major="17", version="17.5",
                     os="el-9", arch="amd64", artifact="art-a")
    invalid = _pg_cell("rpm:el-10:amd64", coupled=True, major=None, version="18.1",   # missing major
                       os="el-10", arch="amd64", artifact="art-b")
    jobs = [_job("rpm:el-9:amd64", 1, 1, "success"), _job("rpm:el-10:amd64", 2, 1, "success")]
    arts = [{"name": "art-a", "id": 1, "members": [_member("pgedge-x", "x86_64", release="1.el9")]},
            {"name": "art-b", "id": 2, "members": [_member("pgedge-x", "x86_64", release="1.el10")]}]
    for order in ([valid, invalid], [invalid, valid]):
        plan = R.reduce(_inp(order, jobs, arts, allowed=["pgedge-x"], pubs={"rpm": "success"}))
        assert plan["plan_resolved"] is False
        assert plan["coverage_denominators"]["eligible_targets"] == 0
        emitted = [t for c in plan["cells"] for t in c["targets"]]
        assert emitted                                         # the valid cell still emits a target
        assert all(t["eligibility"] == "ineligible" for t in emitted)
        assert all(t["eligibility_reason"] == "plan_unresolved" for t in emitted)
        # the valid cell keeps its correctly normalized PG identity (evidence not corrupted)
        vc = cells_by_id(plan)["rpm:el-9:amd64"]
        assert vc["pg_coupled"] is True and vc["build_pg_major"] == "17" and vc["build_pg_version"] == "17.5"
        vt = vc["targets"][0]
        assert vt["pg_coupled"] is True and vt["build_pg_major"] == "17" and vt["build_pg_version"] == "17.5"

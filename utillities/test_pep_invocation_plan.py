"""Offline tests for the invocation planner (utillities/pep_invocation_plan.py).

No network, no docker, no rpm/dpkg. The pure core is driven with synthetic cert-plans +
synthetic execution data (so "changing supported OS/PG data changes output" is a data-only
change), plus:
  * one real-catalog integration path (container_resolver + the committed exec catalog) and a
    RAG-derived cert-plan built from the committed detector-matrix fixture, and
  * an end-to-end contract path that takes generated invocations through the REAL
    pep_request.normalize_request + pep_verify.choose_install (still Docker-free) to prove the
    exact-package (L2a) install is attemptable and pinned.

Logical vs physical component: the RAG cert-plan uses the LOGICAL PEP component 'rag' while each
target keeps the PHYSICAL package 'pgedge-rag-server2' — the same split pep_request enforces.
"""
import copy
import json
from pathlib import Path

import pytest

import pep_invocation_plan as P
import pep_request
import pep_verify

HERE = Path(__file__).parent
EXEC_CATALOG_FILE = HERE / "pep_exec_catalog.json"
CONTAINERS_FILE = HERE.parent / "configuration" / "containers_list.json"
RAG_DETECTOR_FIXTURE = HERE / "cert_plan_fixtures" / "rag_detector_matrix.json"

# The reusable workflow contract for an invocation_id (pep-integration.yml preflight).
INVOCATION_ID_RE = P._INVOCATION_ID_RE

# The logical PEP component and its canonical physical package (from the authoritative registry).
RAG_COMPONENT = "rag"
RAG_PACKAGE = "pgedge-rag-server2"


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def target(family, os_token, arch, *, package=RAG_PACKAGE, logical=RAG_COMPONENT, pg_coupled=False,
           build_pg_major=None, version="2.0.0", release=None, epoch=None, ebv="",
           native_arch=None, identity_state="confirmed", cell_id="c", eligibility="eligible",
           preview_eligibility="ineligible"):
    rel = release if release is not None else ("1.el9" if family == "rpm" else "1.bookworm")
    nat = native_arch if native_arch is not None else ("noarch" if family == "rpm" else arch)
    return {
        "target_id": "%s::%s" % (cell_id, package),
        "eligibility": eligibility,
        "preview_eligibility": preview_eligibility,
        "logical_component": logical,
        "physical_package": package,
        "family": family, "os": os_token, "execution_arch": arch,
        "native_package_arch": nat,
        "pg_coupled": pg_coupled, "build_pg_major": build_pg_major, "build_pg_version": None,
        "expected": {"intended_version": version, "intended_buildnum": "1", "expected_binary_version": ebv},
        "package": {"name": package, "epoch": epoch, "version": version, "release": rel, "sha256": "ab" * 32},
        "package_identity_state": identity_state,
    }


def cell(cell_id, targets):
    for t in targets:
        t.setdefault("target_id", "%s::%s" % (cell_id, t.get("physical_package")))
    return {"cell_id": cell_id, "build_state": "available", "targets": targets}


def cert_plan(cells, *, component=RAG_COMPONENT, channel="staging", version="2.0.0",
              buildnum="1", tag="v2.0.0", resolved=True, schema="cert-plan/1",
              execution_mode=None, simulated=False):
    plan = {
        "schema": schema, "plan_resolved": resolved, "errors": [],
        "provenance": {"repository": "pgEdge/pgedge-pep-test", "run_id": "42", "run_attempt": "1",
                       "sha": "deadbeef", "ref": "refs/heads/feature"},
        "release_intent": {"logical_component": component, "channel": channel,
                           "intended_version": version, "intended_buildnum": buildnum,
                           "effective_tag": tag, "simulated": simulated},
        "cells": cells,
    }
    if execution_mode is not None:
        plan["execution_mode"] = execution_mode
    return plan


def exec_catalog(pgs=("16", "17", "18"), platforms=None):
    if platforms is None:
        platforms = [
            {"os_token": "el-9", "family": "rpm", "catalog_os": ["rocky9", "alma9", "oel9"]},
            {"os_token": "bookworm", "family": "deb", "catalog_os": ["debian12"]},
        ]
    return {"schema": P.EXEC_CATALOG_SCHEMA, "supported_pg_majors": list(pgs), "platforms": platforms}


def enabled(*triples):
    """triples of (family, arch, catalog_os) -> alias '<catalog_os>-<arch>'."""
    return {(f, a, c): "%s-%s" % (c, a) for (f, a, c) in triples}


def build(cells, *, ec=None, plats=None, execution_mode="full", **plan_kw):
    plan_kw.setdefault("execution_mode", execution_mode)   # stamp the cert-plan with the same mode
    return P.build_invocation_plan(cert_plan(cells, **plan_kw),
                                   ec if ec is not None else exec_catalog(),
                                   plats if plats is not None else enabled(("rpm", "amd64", "oel9")),
                                   execution_mode=execution_mode)


def ids(plan):
    return [i["invocation_id"] for i in plan["matrix"]["include"]]


def gap_reasons(plan):
    return sorted(g["reason"] for g in plan["coverage_gaps"])


def _assert_ids_workflow_valid(plan):
    for iid in ids(plan):
        assert INVOCATION_ID_RE.match(iid), "id %r violates the workflow contract" % iid


# --------------------------------------------------------------------------- #
# A. selective single-family plans + broad mixed plan (req 2)
# --------------------------------------------------------------------------- #
def test_selective_rpm_only():
    plan = build([cell("c1", [target("rpm", "el-9", "amd64", cell_id="c1")])],
                 plats=enabled(("rpm", "amd64", "oel9")))
    assert plan["plan_resolved"] is True and plan["errors"] == []
    incl = plan["matrix"]["include"]
    assert all(i["family"] == "rpm" and i["component"] == RAG_COMPONENT
               and i["package_name"] == RAG_PACKAGE for i in incl)
    assert sorted(i["pg_major"] for i in incl) == ["16", "17", "18"]
    assert all(i["container_alias"] == "oel9-amd64" for i in incl)
    _assert_ids_workflow_valid(plan)
    assert plan["counts"] == {"planned_cells": 1, "selected_targets": 1, "eligible_targets": 1,
                              "covered_targets": 1, "coverage_gaps": 0,
                              "gaps_by_scope": {"cell": 0, "target": 0, "member": 0}, "invocations": 3}


def test_selective_deb_only():
    plan = build([cell("c1", [target("deb", "bookworm", "arm64", cell_id="c1")])],
                 plats=enabled(("deb", "arm64", "debian12")))
    assert plan["plan_resolved"] is True
    assert all(i["family"] == "deb" and i["container_alias"] == "debian12-arm64" for i in plan["matrix"]["include"])
    assert len(ids(plan)) == 3 and plan["coverage_gaps"] == []
    _assert_ids_workflow_valid(plan)


def test_broad_mixed_family_plan():
    plan = build(
        [cell("r", [target("rpm", "el-9", "amd64", cell_id="r")]),
         cell("d", [target("deb", "bookworm", "arm64", cell_id="d")])],
        plats=enabled(("rpm", "amd64", "oel9"), ("deb", "arm64", "debian12")))
    assert plan["plan_resolved"] is True
    fams = {i["family"] for i in plan["matrix"]["include"]}
    assert fams == {"rpm", "deb"} and plan["counts"]["invocations"] == 6 and plan["counts"]["coverage_gaps"] == 0
    _assert_ids_workflow_valid(plan)


# --------------------------------------------------------------------------- #
# B. PG coupling (req 4)
# --------------------------------------------------------------------------- #
def test_pg_decoupled_expands_across_supported_majors():
    plan = build([cell("c", [target("rpm", "el-9", "amd64", pg_coupled=False, cell_id="c")])])
    assert sorted(i["pg_major"] for i in plan["matrix"]["include"]) == ["16", "17", "18"]


def test_pg_coupled_stays_on_build_major():
    plan = build([cell("c", [target("rpm", "el-9", "amd64", pg_coupled=True, build_pg_major="17", cell_id="c")])])
    assert [i["pg_major"] for i in plan["matrix"]["include"]] == ["17"]
    assert plan["counts"]["invocations"] == 1 and plan["coverage_gaps"] == []


def test_pg_coupled_unsupported_major_is_gap():
    plan = build([cell("c", [target("rpm", "el-9", "amd64", pg_coupled=True, build_pg_major="19", cell_id="c")])])
    assert plan["matrix"]["include"] == []
    assert gap_reasons(plan) == [P.GAP_PG_NOT_SUPPORTED] and plan["counts"]["eligible_targets"] == 1


def test_pg_coupled_missing_major_is_gap():
    plan = build([cell("c", [target("rpm", "el-9", "amd64", pg_coupled=True, build_pg_major=None, cell_id="c")])])
    assert gap_reasons(plan) == [P.GAP_INVALID_PG_COUPLING]


def test_pg_coupled_non_boolean_is_malformed_gap():
    t = target("rpm", "el-9", "amd64", cell_id="c")
    t["pg_coupled"] = "false"        # a string, not a bool -> strict type rejection
    plan = build([cell("c", [t])])
    assert plan["plan_resolved"] is True and gap_reasons(plan) == [P.GAP_MALFORMED_TARGET]


# --------------------------------------------------------------------------- #
# C. multiple compatible platforms (req 3)
# --------------------------------------------------------------------------- #
def test_multiple_compatible_platforms_expand():
    ec = exec_catalog(platforms=[{"os_token": "el-9", "family": "rpm", "catalog_os": ["rocky9", "alma9", "oel9"]}])
    plan = build([cell("c", [target("rpm", "el-9", "arm64", cell_id="c")])],
                 ec=ec, plats=enabled(("rpm", "arm64", "rocky9"), ("rpm", "arm64", "alma9"),
                                      ("rpm", "arm64", "oel9")))
    aliases = sorted({i["container_alias"] for i in plan["matrix"]["include"]})
    assert aliases == ["alma9-arm64", "oel9-arm64", "rocky9-arm64"]        # all three enabled -> all considered
    assert plan["counts"]["invocations"] == 9                              # 3 platforms x 3 pg majors


def test_only_enabled_platforms_are_used():
    ec = exec_catalog(platforms=[{"os_token": "el-9", "family": "rpm", "catalog_os": ["rocky9", "alma9", "oel9"]}])
    # only oel9 enabled -> the disabled/absent siblings must NOT appear
    plan = build([cell("c", [target("rpm", "el-9", "arm64", cell_id="c")])],
                 ec=ec, plats=enabled(("rpm", "arm64", "oel9")))
    assert sorted({i["container_alias"] for i in plan["matrix"]["include"]}) == ["oel9-arm64"]


# --------------------------------------------------------------------------- #
# D. unsupported / EOL / unknown platforms + arch (req 5, 6, 9)
# --------------------------------------------------------------------------- #
def test_unsupported_os_is_coverage_gap_not_dropped():
    # bullseye is intentionally NOT in the exec catalog (EOL); it must surface explicitly.
    plan = build([cell("c", [target("deb", "bullseye", "arm64", cell_id="c")])],
                 plats=enabled(("deb", "arm64", "debian11")))   # even though a container is enabled
    assert plan["matrix"]["include"] == []
    assert gap_reasons(plan) == [P.GAP_UNSUPPORTED_OS]
    assert plan["coverage_gaps"][0]["os"] == "bullseye" and plan["counts"]["eligible_targets"] == 1


def test_unknown_os_is_coverage_gap():
    plan = build([cell("c", [target("deb", "plan9", "arm64", cell_id="c")])])
    assert gap_reasons(plan) == [P.GAP_UNSUPPORTED_OS]


def test_mapped_os_without_enabled_container_is_gap():
    # bookworm is mapped, but no enabled debian12 for amd64 -> explicit no_enabled_platform gap.
    plan = build([cell("c", [target("deb", "bookworm", "amd64", cell_id="c")])],
                 plats=enabled(("deb", "arm64", "debian12")))   # only arm64 enabled
    assert gap_reasons(plan) == [P.GAP_NO_ENABLED_PLATFORM]


def test_unsupported_arch_is_gap():
    plan = build([cell("c", [target("rpm", "el-9", "ppc64le", cell_id="c")])])
    assert gap_reasons(plan) == [P.GAP_UNSUPPORTED_ARCH]


def test_unsupported_family_is_gap():
    t = target("rpm", "el-9", "amd64", cell_id="c")
    t["family"] = "apk"
    plan = build([cell("c", [t])])
    assert gap_reasons(plan) == [P.GAP_UNSUPPORTED_FAMILY]


# --------------------------------------------------------------------------- #
# E. no eligible targets (req 7)
# --------------------------------------------------------------------------- #
def test_ineligible_target_is_a_target_gap_never_silent():
    inelig = target("rpm", "el-9", "amd64", cell_id="c")
    inelig["eligibility"] = "ineligible"
    inelig["eligibility_reason"] = "publication_publish_unconfirmed"
    plan = build([cell("c", [inelig])])
    assert plan["plan_resolved"] is True and plan["matrix"]["include"] == []
    [gap] = plan["coverage_gaps"]
    assert (gap["scope"], gap["reason"], gap["detail"]) == (
        "target", P.GAP_TARGET_INELIGIBLE, "publication_publish_unconfirmed")
    assert gap["target_id"] == "c::%s" % RAG_PACKAGE and gap["physical_package"] == RAG_PACKAGE
    assert plan["counts"] == {"planned_cells": 1, "selected_targets": 1, "eligible_targets": 0,
                              "covered_targets": 0, "coverage_gaps": 1,
                              "gaps_by_scope": {"cell": 0, "target": 1, "member": 0}, "invocations": 0}


def test_eligible_but_all_gapped_is_not_silent_empty():
    # eligible targets exist but none map -> matrix empty AND coverage_gaps non-empty (never silent).
    plan = build([cell("c", [target("deb", "bullseye", "arm64", cell_id="c")])])
    assert plan["matrix"]["include"] == [] and plan["counts"]["eligible_targets"] == 1
    assert plan["counts"]["coverage_gaps"] == 1


# --------------------------------------------------------------------------- #
# E2. every planned cell is accounted for: cell / target / member gap scopes
# --------------------------------------------------------------------------- #
def planned_cell(cell_id, targets=(), *, build_state="available", evidence=None, members=(),
                 selection="resolved", **extra):
    c = {"cell_id": cell_id, "family": "deb", "os": "trixie", "normalized_arch": "arm64",
         "build_state": build_state, "build_evidence": evidence or {}, "members": list(members),
         "targets": list(targets), "target_selection_state": selection}
    c.update(extra)
    return c


def member(name, *reasons, cls="runtime", native=None, sha=None, path=None):
    m = {"package_name": name, "package_class": cls, "selected": not reasons}
    if native is not None:
        m["native_arch"] = native
    if sha is not None:
        m["sha256"] = sha
    if path is not None:
        m["artifact_member_path"] = path
    if reasons:
        m["exclusion_reasons"] = list(reasons)
    return m


def _assert_reconciles(plan):
    c = plan["counts"]
    assert all(v >= 0 for v in list(c.values()) + list(c["gaps_by_scope"].values()) if isinstance(v, int))
    assert c["covered_targets"] + c["gaps_by_scope"]["target"] == c["selected_targets"]
    assert c["eligible_targets"] <= c["selected_targets"] and c["covered_targets"] <= c["eligible_targets"]
    assert sum(c["gaps_by_scope"].values()) == c["coverage_gaps"] == len(plan["coverage_gaps"])
    accounted = ({i["source_cell_id"] for i in plan["matrix"]["include"]}
                 | {g["cell_id"] for g in plan["coverage_gaps"]})
    assert c["planned_cells"] == len(accounted)          # every planned cell: an invocation or a gap


@pytest.mark.parametrize("state, evidence, extra, reason, detail", [
    ("failed", {"latest_status": "completed", "latest_conclusion": "failure"}, {},
     "build_failed", "failure"),
    ("never_ran", {}, {}, "build_never_ran", None),
    ("incomplete", {"latest_status": "in_progress", "latest_conclusion": None}, {},
     "build_incomplete", "in_progress"),
    ("incomplete", {"latest_status": "completed", "latest_conclusion": "success", "artifact_present": False},
     {}, P.GAP_PACKAGE_EVIDENCE_MISSING, "build job succeeded but no verified package artifact"),
    ("ambiguous", {}, {"ambiguity_reason": "duplicate_cell_id"}, "build_ambiguous", "duplicate_cell_id"),
])
def test_unbuilt_cell_is_exactly_one_cell_gap(state, evidence, extra, reason, detail):
    plan = build([planned_cell("c", build_state=state, evidence=evidence, selection="target_unresolved",
                               **extra)])
    [gap] = plan["coverage_gaps"]
    assert gap == {"scope": "cell", "cell_id": "c", "target_id": None, "family": "deb", "os": "trixie",
                   "arch": "arm64", "physical_package": None, "reason": reason, "detail": detail}
    assert plan["counts"]["gaps_by_scope"] == {"cell": 1, "target": 0, "member": 0}
    _assert_reconciles(plan)


def test_built_cell_without_a_certifiable_target_is_one_cell_gap():
    ambiguous = planned_cell("a", selection="target_ambiguous",
                             members=[member(RAG_PACKAGE), member(RAG_PACKAGE)])
    policy_only = planned_cell("p", selection="target_unresolved",
                               members=[member("pgedge-rag-server2-dbgsym", "non_runtime", cls="debug"),
                                        member("other-pkg", "package_not_allowed")])
    rejected = planned_cell("r", selection="target_unresolved",
                            members=[member(RAG_PACKAGE, "missing_checksum"),
                                     member("pgedge-rag-server2-dbgsym", "non_runtime", cls="debug")])
    empty = planned_cell("e", selection="target_unresolved")
    plan = build([ambiguous, policy_only, rejected, empty])
    got = {g["cell_id"]: (g["scope"], g["reason"], g["detail"]) for g in plan["coverage_gaps"]}
    assert got == {
        "a": ("cell", P.GAP_TARGET_AMBIGUOUS, RAG_PACKAGE),
        "p": ("cell", P.GAP_NO_RUNTIME_TARGET, "non_runtime, package_not_allowed"),
        # a cell with no target reports ONE cell gap; its rejected member is not a second gap
        "r": ("cell", P.GAP_NO_RUNTIME_TARGET, "missing_checksum, non_runtime"),
        "e": ("cell", P.GAP_NO_RUNTIME_TARGET, "no inspected packages"),
    }
    assert len(plan["coverage_gaps"]) == 4
    _assert_reconciles(plan)


def test_each_rejected_allowed_file_beside_a_valid_target_is_one_member_gap():
    wrong_arch = member(RAG_PACKAGE, "arch_mismatch", native="aarch64", sha="c" * 64, path="b.aarch64.rpm")
    members = [member(RAG_PACKAGE, native="x86_64", sha="a" * 64, path="a.x86_64.rpm"),  # the selected target
               wrong_arch,                                   # same NAME, distinct file: still uncertified
               dict(wrong_arch, artifact_member_path="c.aarch64.rpm"),   # same metadata, another file
               member("pgedge-rag-server2-dbgsym", "non_runtime", cls="debug", path="dbg.rpm"),  # policy
               member("unrelated-tool", "package_not_allowed", path="tool.rpm"),               # policy
               member("pgedge-rag-server", "missing_checksum", native="x86_64", path="d.x86_64.rpm"),
               member("pgedge-rag-server", "invalid_checksum", "missing_version", native="x86_64", sha="e" * 64,
                      path="e.x86_64.rpm")]
    t = target("rpm", "el-9", "amd64", cell_id="c")
    plan = build([planned_cell("c", [t], members=members, family="rpm", os="el-9", normalized_arch="amd64")])
    assert len(plan["matrix"]["include"]) == 3                                  # the valid target still runs
    got = [(g["scope"], g["reason"], g["physical_package"], g["target_id"], g["detail"])
           for g in plan["coverage_gaps"]]
    assert got == [
        ("member", P.GAP_MEMBER_REJECTED, "pgedge-rag-server", None,
         "invalid_checksum,missing_version; path=e.x86_64.rpm; native_arch=x86_64; sha256=eeeeeeeeeeee"),
        ("member", P.GAP_MEMBER_REJECTED, "pgedge-rag-server", None,
         "missing_checksum; path=d.x86_64.rpm; native_arch=x86_64"),
        ("member", P.GAP_MEMBER_REJECTED, RAG_PACKAGE, None,
         "arch_mismatch; path=b.aarch64.rpm; native_arch=aarch64; sha256=cccccccccccc"),
        ("member", P.GAP_MEMBER_REJECTED, RAG_PACKAGE, None,
         "arch_mismatch; path=c.aarch64.rpm; native_arch=aarch64; sha256=cccccccccccc"),
    ]
    assert plan["counts"]["gaps_by_scope"] == {"cell": 0, "target": 0, "member": 4}
    _assert_reconciles(plan)


def test_preview_non_runnable_target_reports_the_preview_reason():
    t = target("rpm", "el-9", "amd64", cell_id="c", eligibility="ineligible",
               preview_eligibility="ineligible")
    t["eligibility_reason"] = "simulated_not_eligible"
    t["preview_eligibility_reason"] = "build_failed"
    plan = build([planned_cell("c", [t])], execution_mode="preview", simulated=True)
    [gap] = plan["coverage_gaps"]
    assert (gap["reason"], gap["detail"]) == (P.GAP_TARGET_INELIGIBLE, "build_failed")


def test_mixed_plan_counts_reconcile_without_subtraction():
    covered = target("rpm", "el-9", "amd64", cell_id="ok")
    unsupported = target("deb", "bookworm", "amd64", cell_id="unsup")        # no enabled container
    inelig = target("rpm", "el-9", "amd64", cell_id="pub", eligibility="ineligible")
    plan = build([planned_cell("ok", [covered], members=[member("pgedge-rag-server", "missing_checksum")]),
                  planned_cell("unsup", [unsupported]),
                  planned_cell("pub", [inelig]),
                  planned_cell("failed", build_state="failed", selection="target_unresolved")])
    assert plan["counts"] == {"planned_cells": 4, "selected_targets": 3, "eligible_targets": 2,
                              "covered_targets": 1, "coverage_gaps": 4,
                              "gaps_by_scope": {"cell": 1, "target": 2, "member": 1}, "invocations": 3}
    assert gap_reasons(plan) == sorted(["build_failed", P.GAP_NO_ENABLED_PLATFORM,
                                        P.GAP_TARGET_INELIGIBLE, P.GAP_MEMBER_REJECTED])
    _assert_reconciles(plan)


def test_hostile_resolved_cells_never_raise_and_stay_accounted():
    bad_target = target("rpm", "el-9", "amd64", cell_id="t", eligibility="ineligible")
    bad_target["physical_package"] = ["not", "a", "name"]
    bad_target["eligibility_reason"] = {"not": "a string"}
    cells = [planned_cell("t", [bad_target], members=[{"package_name": ["x"], "exclusion_reasons": ["missing_checksum", 7]},
                                                      {"package_name": None, "exclusion_reasons": "junk"}, "junk"]),
             planned_cell("s", build_state={"odd": 1}, evidence=["x"], members="junk"),
             planned_cell("a", selection="target_ambiguous", members=[{"package_name": ["x"]}])]
    plan = build(cells)
    assert plan["plan_resolved"] is True
    assert gap_reasons(plan) == sorted(["build_unknown", P.GAP_TARGET_AMBIGUOUS, P.GAP_TARGET_INELIGIBLE,
                                        P.GAP_MEMBER_REJECTED])
    _assert_reconciles(plan)


@pytest.mark.parametrize("mutate, fragment", [
    (lambda cp: cp.__setitem__("cells", {"c": {}}), "cells must be a list"),
    (lambda cp: cp["cells"].append("not-a-cell"), "cells[1] must be an object with a nonblank cell_id"),
    (lambda cp: cp["cells"].append({"cell_id": " ", "targets": []}), "cells[1] must be an object"),
    (lambda cp: cp["cells"].append({"cell_id": "x", "targets": None}), "cells[1].targets must be a list"),
    (lambda cp: cp["cells"].append({"cell_id": "x", "targets": ["t"]}), "cells[1].targets must be a list"),
])
def test_malformed_cell_structure_fails_closed(mutate, fragment):
    cp = cert_plan([cell("c", [target("rpm", "el-9", "amd64", cell_id="c")])], execution_mode="full")
    mutate(cp)
    plan = P.build_invocation_plan(cp, exec_catalog(), enabled(("rpm", "amd64", "oel9")))
    assert plan["plan_resolved"] is False and plan["matrix"]["include"] == []
    assert any(fragment in e for e in plan["errors"])


# --------------------------------------------------------------------------- #
# F. determinism + stable/unique IDs + carried identity (req 5, 6)
# --------------------------------------------------------------------------- #
def test_deterministic_and_stable_ids():
    cells = [cell("d", [target("deb", "bookworm", "arm64", cell_id="d")]),
             cell("r", [target("rpm", "el-9", "amd64", cell_id="r")])]
    plats = enabled(("rpm", "amd64", "oel9"), ("deb", "arm64", "debian12"))
    a = P.build_invocation_plan(cert_plan(copy.deepcopy(cells)), exec_catalog(), plats)
    b = P.build_invocation_plan(cert_plan(copy.deepcopy(cells)), exec_catalog(), plats)
    assert P.to_json(a) == P.to_json(b)                       # byte-identical across runs
    assert ids(a) == sorted(ids(a)) and len(ids(a)) == len(set(ids(a)))   # sorted + unique
    _assert_ids_workflow_valid(a)


def test_invocation_carries_identity_and_channel():
    plan = build([cell("c", [target("rpm", "el-9", "amd64", version="2.0.0", release="1.el9", cell_id="c")])],
                 channel="daily")
    inv = plan["matrix"]["include"][0]
    assert inv["component"] == RAG_COMPONENT and inv["package_name"] == RAG_PACKAGE
    assert inv["channel"] == "daily" and inv["expected_version"] == "2.0.0"
    assert inv["package"] == {"name": RAG_PACKAGE, "version": "2.0.0", "release": "1.el9",
                              "sha256": "ab" * 32, "native_arch": "noarch"}
    for k in ("component", "package_name", "container_alias", "pg_major", "family", "arch"):
        assert inv[k] not in (None, "")


# --------------------------------------------------------------------------- #
# F2. exact-package expected identity, emitted per-family only (req 1)
# --------------------------------------------------------------------------- #
def test_rpm_invocation_emits_only_expected_rpm_from_inspected_identity():
    plan = build([cell("c", [target("rpm", "el-9", "amd64", version="2.0.0", release="1.el9", cell_id="c")])])
    inv = plan["matrix"]["include"][0]
    assert inv["expected_rpm"] == "2.0.0-1.el9"        # <version>-<release>, from the inspected package
    assert inv["expected_deb"] == ""                   # opposite family empty -> workflow drops it


def test_deb_invocation_emits_only_expected_deb_from_inspected_identity():
    plan = build([cell("c", [target("deb", "bookworm", "arm64", version="2.0.0", release="1.noble", cell_id="c")])],
                 plats=enabled(("deb", "arm64", "debian12")))
    inv = plan["matrix"]["include"][0]
    assert inv["expected_deb"] == "2.0.0-1.noble"
    assert inv["expected_rpm"] == ""


def test_expected_strings_are_not_reconstructed_from_intended_version():
    # The inspected package (version/release) is authoritative for the pinned identity; the release
    # 'intended_version' (which drives expected_version) is deliberately DIVERGENT here to prove
    # expected_rpm comes from package.version/release, not from the intended version.
    t = target("rpm", "el-9", "amd64", version="2.0.0", release="1.el9", cell_id="c")   # package.* = 2.0.0-1.el9
    plan = build([cell("c", [t])], version="7.7.7")                                     # release intended = 7.7.7
    inv = plan["matrix"]["include"][0]
    assert inv["expected_rpm"] == "2.0.0-1.el9"         # from package.*, NOT the 7.7.7 intended version
    assert inv["expected_version"] == "7.7.7"           # expected_version carries the release intent


# --------------------------------------------------------------------------- #
# F3. eligible-target identity invariants: epoch + confirmed state (req 1, 4)
# --------------------------------------------------------------------------- #
def test_epoch_bearing_eligible_target_is_gap_not_invocation():
    t = target("rpm", "el-9", "amd64", epoch="1", cell_id="c")
    plan = build([cell("c", [t])])
    assert plan["plan_resolved"] is True and plan["matrix"]["include"] == []
    assert gap_reasons(plan) == [P.GAP_PACKAGE_HAS_EPOCH]


def test_only_none_epoch_allowed_noncanonical_rejected():
    # The inspector canonicalizes a missing/zero epoch to None. Only None is accepted here; the
    # alternate representations "", "0", 0 (and any real epoch) are rejected as a gap.
    ok = build([cell("c", [target("rpm", "el-9", "amd64", epoch=None, cell_id="c")])])
    assert ok["counts"]["invocations"] == 3 and ok["coverage_gaps"] == []
    for bad in ("", "0", 0, "1"):
        plan = build([cell("c", [target("rpm", "el-9", "amd64", epoch=bad, cell_id="c")])])
        assert plan["matrix"]["include"] == [] and gap_reasons(plan) == [P.GAP_PACKAGE_HAS_EPOCH]


def test_identity_unconfirmed_eligible_target_is_gap():
    t = target("rpm", "el-9", "amd64", identity_state="ambiguous", cell_id="c")
    plan = build([cell("c", [t])])
    assert plan["matrix"]["include"] == [] and gap_reasons(plan) == [P.GAP_IDENTITY_UNCONFIRMED]


# --------------------------------------------------------------------------- #
# F4. malformed eligible-target shapes never raise / never partial (req 4)
# --------------------------------------------------------------------------- #
def test_malformed_target_shapes_are_gaps_never_raise():
    def over(**changes):
        t = target("rpm", "el-9", "amd64", cell_id="c")
        t.update(changes)
        return t
    ok_pkg = {"name": "pgedge-rag-server2", "epoch": None, "version": "2.0.0", "release": "1.el9",
              "sha256": "ab" * 32}
    bad_targets = [
        over(package=None),                                              # package not a dict
        over(expected=["not", "an", "object"]),                         # expected not a dict
        over(package={k: v for k, v in ok_pkg.items() if k != "version"}),   # missing version
        over(package={**ok_pkg, "name": "different-name"}),             # name disagrees
        over(package={**ok_pkg, "sha256": "nothex"}),                   # bad sha
        over(package={**ok_pkg, "release": "1 el9"}),                   # non-canonical release
    ]
    for bt in bad_targets:
        plan = build([cell("c", [bt])])
        assert plan["plan_resolved"] is True                             # never raised
        assert plan["matrix"]["include"] == []                           # no partial/null entry
        assert plan["counts"]["coverage_gaps"] == 1
        assert gap_reasons(plan) == [P.GAP_MALFORMED_TARGET]


def test_logical_component_disagreement_is_malformed_gap():
    t = target("rpm", "el-9", "amd64", cell_id="c")
    t["logical_component"] = "rag"          # target says rag
    plan = build([cell("c", [t])], component="rag")   # release also rag -> agrees -> ok baseline
    assert plan["counts"]["invocations"] == 3
    t2 = target("rpm", "el-9", "amd64", cell_id="c")
    t2["logical_component"] = "somethingelse"          # disagrees with release 'rag'
    plan2 = build([cell("c", [t2])])
    assert plan2["matrix"]["include"] == [] and gap_reasons(plan2) == [P.GAP_MALFORMED_TARGET]


def test_nonstring_binary_expectation_is_malformed_gap():
    for bad in (123, ["x"], {"v": 1}, 1.5):
        t = target("rpm", "el-9", "amd64", cell_id="c")
        t["expected"]["expected_binary_version"] = bad   # neither None nor a string
        plan = build([cell("c", [t])])
        assert plan["matrix"]["include"] == [] and gap_reasons(plan) == [P.GAP_MALFORMED_TARGET]


def test_none_binary_expectation_is_emitted_as_empty_string():
    t = target("rpm", "el-9", "amd64", ebv=None, cell_id="c")   # expected_binary_version = None
    inv = build([cell("c", [t])])["matrix"]["include"][0]
    assert inv["expected_binary"] == ""


def test_hyphenated_debian_version_stays_pinnable():
    # A valid Debian version bearing hyphens must survive per-field validation and pin exactly.
    ec = exec_catalog(platforms=[{"os_token": "trixie", "family": "deb", "catalog_os": ["debian13"]}])
    plan = build([cell("c", [target("deb", "trixie", "arm64", version="2.0.0-beta", release="1.trixie",
                                    native_arch="arm64", cell_id="c")])],
                 ec=ec, plats=enabled(("deb", "arm64", "debian13")))
    inv = plan["matrix"]["include"][0]
    assert inv["expected_deb"] == "2.0.0-beta-1.trixie" and inv["expected_rpm"] == ""
    # ... and it normalizes + pins through the real PEP contract (debian13-arm64 is a real alias).
    req = pep_request.normalize_request(_request_from_invocation(inv))
    kind, token = pep_verify.choose_install(req)
    assert kind == "pinned" and token == "2.0.0-beta-1.trixie"


# --------------------------------------------------------------------------- #
# F5. blank/whitespace optionals canonicalize to "" (never a doomed invocation) (req: this pass)
# --------------------------------------------------------------------------- #
def test_blank_variants_of_optionals_emit_empty_string():
    # effective_tag, expected_buildnum (from intended_buildnum) and expected_binary: every
    # None/empty/whitespace-only form must be emitted as "" (the planner treats blank as absent).
    for blank in (None, "", "   ", "\t", " \n "):
        t = target("rpm", "el-9", "amd64", cell_id="c")
        t["expected"]["expected_binary_version"] = blank
        plan = build([cell("c", [t])], tag=blank, buildnum=blank)
        assert plan["plan_resolved"] is True
        inv = plan["matrix"]["include"][0]
        assert inv["effective_tag"] == ""
        assert inv["expected_buildnum"] == ""
        assert inv["expected_binary"] == ""


def test_normalize_request_rejects_whitespace_optional_documenting_why():
    # WHY the canonicalization matters: a whitespace-only optional reaching the request is rejected
    # by normalize_request as "provided but is empty". The planner must never emit such a value.
    plan = _real_rag_plan()
    inv = next(i for i in plan["matrix"]["include"] if i["family"] == "rpm")
    raw = _request_from_invocation(inv)
    raw["effective_tag"] = "   "                         # simulate a non-canonicalized value slipping in
    with pytest.raises(pep_request.RequestError):
        pep_request.normalize_request(raw)


def test_whitespace_only_optionals_do_not_produce_doomed_invocation():
    # End-to-end: whitespace-only tag/buildnum/binary in the source -> the generated invocation still
    # normalizes cleanly and pins exactly (no "provided but is empty").
    t = target("rpm", "el-9", "amd64", version="2.0.0", release="1.el9", cell_id="c")
    t["expected"]["expected_binary_version"] = "   "
    plan = build([cell("c", [t])], tag="   ", buildnum="   ")
    inv = plan["matrix"]["include"][0]
    raw = _request_from_invocation(inv)
    assert "effective_tag" not in raw and "expected_buildnum" not in raw and "expected_binary" not in raw
    req = pep_request.normalize_request(raw)             # must not raise
    kind, token = pep_verify.choose_install(req)
    assert kind == "pinned" and token == "2.0.0-1.el9"


# --------------------------------------------------------------------------- #
# G. reject: duplicate identity + ambiguous/malformed catalog (req 8) + unresolved source
# --------------------------------------------------------------------------- #
def test_duplicate_invocation_identity_fails_closed():
    # two cells, identical component+package+os+arch+inspected identity -> identical ids -> reject.
    cells = [cell("c1", [target("rpm", "el-9", "amd64", cell_id="c1")]),
             cell("c2", [target("rpm", "el-9", "amd64", cell_id="c2")])]
    plan = build(cells)
    assert plan["plan_resolved"] is False and plan["matrix"]["include"] == []
    assert any("duplicate invocation identity" in e for e in plan["errors"])


def test_ambiguous_exec_catalog_mapping_fails_closed():
    ec = exec_catalog(platforms=[
        {"os_token": "el-9", "family": "rpm", "catalog_os": ["rocky9"]},
        {"os_token": "el-9", "family": "rpm", "catalog_os": ["alma9"]}])   # same (os_token, family)
    plan = build([cell("c", [target("rpm", "el-9", "amd64", cell_id="c")])], ec=ec)
    assert plan["plan_resolved"] is False and plan["matrix"]["include"] == []
    assert any("ambiguous mapping" in e for e in plan["errors"])


def test_malformed_exec_catalog_fails_closed():
    for bad in ({"schema": "wrong", "supported_pg_majors": ["16"], "platforms": []},
                {"schema": P.EXEC_CATALOG_SCHEMA, "supported_pg_majors": [], "platforms": []},
                {"schema": P.EXEC_CATALOG_SCHEMA, "supported_pg_majors": ["x"], "platforms": []},
                {"schema": P.EXEC_CATALOG_SCHEMA, "supported_pg_majors": ["16"], "platforms": "nope"}):
        plan = build([cell("c", [target("rpm", "el-9", "amd64", cell_id="c")])], ec=bad)
        assert plan["plan_resolved"] is False and plan["matrix"]["include"] == []


def test_unresolved_or_wrong_schema_source_fails_closed():
    unresolved = build([cell("c", [target("rpm", "el-9", "amd64", cell_id="c")])], resolved=False)
    assert unresolved["plan_resolved"] is False and unresolved["matrix"]["include"] == []
    wrong = build([cell("c", [target("rpm", "el-9", "amd64", cell_id="c")])], schema="cert-plan/999")
    assert wrong["plan_resolved"] is False and wrong["matrix"]["include"] == []


def test_bad_release_channel_or_version_fails_closed():
    bad_channel = build([cell("c", [target("rpm", "el-9", "amd64", cell_id="c")])], channel="nightly")
    assert bad_channel["plan_resolved"] is False and bad_channel["matrix"]["include"] == []
    assert any("channel" in e for e in bad_channel["errors"])
    blank_ver = build([cell("c", [target("rpm", "el-9", "amd64", cell_id="c")])], version="")
    assert blank_ver["plan_resolved"] is False and blank_ver["matrix"]["include"] == []


def test_bad_effective_tag_fails_closed():
    # a tag missing the required 'v' prefix would be rejected by normalize_request downstream ->
    # fail closed here rather than emit invocations that predictably fail.
    plan = build([cell("c", [target("rpm", "el-9", "amd64", cell_id="c")])], tag="2.0.0")
    assert plan["plan_resolved"] is False and plan["matrix"]["include"] == []
    assert any("effective_tag" in e for e in plan["errors"])


def test_bad_intended_buildnum_fails_closed():
    # '-' is not in the PEP build-number grammar (^[A-Za-z0-9._]+$) -> fail closed.
    plan = build([cell("c", [target("rpm", "el-9", "amd64", cell_id="c")])], buildnum="1-2")
    assert plan["plan_resolved"] is False and plan["matrix"]["include"] == []
    assert any("intended_buildnum" in e for e in plan["errors"])


# --------------------------------------------------------------------------- #
# G2. exec-catalog canonical-string rejection (req 5)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_pgs", [("16 ",), (" 16",), ("016",), ("16", "16")])
def test_padded_or_duplicate_pg_majors_rejected(bad_pgs):
    pgs, os_map, errs = P.validate_exec_catalog(exec_catalog(pgs=bad_pgs))
    assert errs and pgs == []                                  # rejected, not silently stripped


def test_padded_os_token_or_catalog_os_rejected():
    ec_tok = exec_catalog(platforms=[{"os_token": " el-9", "family": "rpm", "catalog_os": ["oel9"]}])
    ec_cos = exec_catalog(platforms=[{"os_token": "el-9", "family": "rpm", "catalog_os": ["oel9 "]}])
    for ec in (ec_tok, ec_cos):
        _, _, errs = P.validate_exec_catalog(ec)
        assert errs
        plan = build([cell("c", [target("rpm", "el-9", "amd64", cell_id="c")])], ec=ec)
        assert plan["plan_resolved"] is False and plan["matrix"]["include"] == []


# --------------------------------------------------------------------------- #
# H. data-only reconfiguration changes output without code changes (req 9)
# --------------------------------------------------------------------------- #
def test_changing_supported_pg_changes_output():
    cells = [cell("c", [target("rpm", "el-9", "amd64", cell_id="c")])]
    wide = P.build_invocation_plan(cert_plan(cells), exec_catalog(pgs=("16", "17", "18")),
                                   enabled(("rpm", "amd64", "oel9")))
    narrow = P.build_invocation_plan(cert_plan(cells), exec_catalog(pgs=("16",)),
                                     enabled(("rpm", "amd64", "oel9")))
    assert wide["counts"]["invocations"] == 3 and narrow["counts"]["invocations"] == 1     # data-only


def test_adding_os_support_changes_output_without_code():
    cells = [cell("c", [target("deb", "bullseye", "arm64", cell_id="c")])]
    plats = enabled(("deb", "arm64", "debian11"))
    without = P.build_invocation_plan(cert_plan(cells), exec_catalog(platforms=[]), plats)
    with_bullseye = P.build_invocation_plan(
        cert_plan(cells),
        exec_catalog(platforms=[{"os_token": "bullseye", "family": "deb", "catalog_os": ["debian11"]}]),
        plats)
    assert without["matrix"]["include"] == [] and gap_reasons(without) == [P.GAP_UNSUPPORTED_OS]
    assert with_bullseye["counts"]["invocations"] == 3 and with_bullseye["coverage_gaps"] == []


# --------------------------------------------------------------------------- #
# I. invocation_id: workflow-valid, deterministic, collision-resistant (req 2)
# --------------------------------------------------------------------------- #
def test_invocation_id_is_workflow_valid_and_repeatable():
    a = P._invocation_id("rag", "pgedge-rag-server2", "oel9-amd64", "16", "2.0.0", "1.el9", "ab" * 32)
    b = P._invocation_id("rag", "pgedge-rag-server2", "oel9-amd64", "16", "2.0.0", "1.el9", "ab" * 32)
    assert a == b and INVOCATION_ID_RE.match(a)               # deterministic + charset/length valid


def test_invocation_id_handles_long_and_unsafe_source_values():
    iid = P._invocation_id("x" * 200, "p::q/../evil", "a b::c" + "z" * 200, "16",
                           "2.0.0", "1.el9", "cd" * 32)
    assert INVOCATION_ID_RE.match(iid) and len(iid) <= 64     # long + unsafe -> still safe & bounded


def test_invocation_id_distinguishes_physical_packages_and_identities():
    base = ("rag", "pgedge-rag-server2", "oel9-amd64", "16", "2.0.0", "1.el9", "ab" * 32)
    # different physical package -> different id
    assert P._invocation_id(*base) != P._invocation_id("rag", "pgedge-rag-server", *base[2:])
    # different inspected sha (same everything else) -> different id
    assert P._invocation_id(*base) != P._invocation_id(*base[:6], "cd" * 32)
    # different release -> different id
    assert P._invocation_id(*base) != P._invocation_id(*base[:5], "2.el9", base[6])


def test_two_distinct_physical_packages_same_dims_get_distinct_invocations():
    # 'rag' accepts both pgedge-rag-server2 and pgedge-rag-server; sharing os/arch/pg they must NOT
    # collide (distinct ids), and both must be emitted -- no fail-closed, no silent merge.
    cells = [cell("c", [target("rpm", "el-9", "amd64", package="pgedge-rag-server2", cell_id="c"),
                        target("rpm", "el-9", "amd64", package="pgedge-rag-server", cell_id="c")])]
    plan = build(cells)
    assert plan["plan_resolved"] is True
    assert plan["counts"]["invocations"] == 6                 # 2 packages x 3 pg majors
    assert len(set(ids(plan))) == 6
    assert {i["package_name"] for i in plan["matrix"]["include"]} == {"pgedge-rag-server2", "pgedge-rag-server"}


# --------------------------------------------------------------------------- #
# J. logical/physical component boundary + PEP-registry capability gating (req 3)
# --------------------------------------------------------------------------- #
def test_unknown_logical_component_is_capability_gap():
    cells = [cell("c", [target("rpm", "el-9", "amd64", logical="not-a-pep-component", cell_id="c")])]
    plan = build(cells, component="not-a-pep-component")       # release + target agree, but unknown
    assert plan["matrix"]["include"] == [] and gap_reasons(plan) == [P.GAP_UNSUPPORTED_COMPONENT]


def test_disallowed_physical_package_is_capability_gap():
    cells = [cell("c", [target("rpm", "el-9", "amd64", package="pgedge-bogus", cell_id="c")])]
    plan = build(cells)                                        # component 'rag' known, package not accepted
    assert plan["matrix"]["include"] == [] and gap_reasons(plan) == [P.GAP_UNSUPPORTED_COMPONENT]


def test_registry_reuses_pep_request_contract_not_a_copy():
    # The planner's registry IS pep_request's authoritative table (same object), not a duplicate.
    assert P.COMPONENT_PACKAGES is pep_request.COMPONENT_PACKAGES
    assert P.VALID_CHANNELS is pep_request.VALID_CHANNELS


# --------------------------------------------------------------------------- #
# K. END-TO-END CONTRACT: generated invocations normalize + pin exactly (req 1, 3)
# --------------------------------------------------------------------------- #
# Coordinator run-level defaults consumed by normalize_request (execution_mode=full is a workflow
# concept, not a normalize_request input). effective_tag/expected_binary etc. are dropped-when-empty
# exactly as pep-integration.yml's framework step does (`[ -n "$IN_..." ] && args+=(...)`).
_RUN_LEVEL_DEFAULTS = {"scenario": "certification", "mode": "observe"}
_OPTIONAL_KEYS = ("expected_buildnum", "effective_tag", "expected_rpm", "expected_deb", "expected_binary")


def _request_from_invocation(inv):
    """Transform a generated invocation into a normalize_request raw dict the way the coordinator +
    pep-integration.yml would: required inputs always present, empty optionals dropped, run-level
    defaults supplied."""
    raw = {k: inv[k] for k in ("component", "package_name", "channel", "expected_version",
                               "container_alias", "pg_major", "family", "arch")}
    for k in _OPTIONAL_KEYS:
        v = inv.get(k)
        if v not in (None, ""):
            raw[k] = v
    raw.update(_RUN_LEVEL_DEFAULTS)
    return raw


def _real_rag_plan():
    cells = _rag_cells_from_fixture()
    ec = json.loads(EXEC_CATALOG_FILE.read_text())
    enabled_platforms, err = P.load_enabled_platforms(str(CONTAINERS_FILE))
    assert err is None and enabled_platforms
    plan = P.build_invocation_plan(cert_plan(cells), ec, enabled_platforms)
    assert plan["plan_resolved"] is True and plan["errors"] == []
    return plan


def test_every_generated_rag_invocation_normalizes_and_pins_exactly():
    plan = _real_rag_plan()
    incl = plan["matrix"]["include"]
    assert len(incl) >= 20 and {i["family"] for i in incl} == {"rpm", "deb"}   # both families, many legs
    for inv in incl:                                          # EVERY invocation, not one per family
        req = pep_request.normalize_request(_request_from_invocation(inv))     # must not raise
        # L2a (exact package-manager identity) is attemptable, and the install is PINNED, never latest.
        assert req["attemptable_now"]["l2a"] is True
        assert req["component"] == RAG_COMPONENT and req["package_name"] == RAG_PACKAGE
        kind, token = pep_verify.choose_install(req)
        assert kind == "pinned" and token
        expected_token = "%s-%s" % (inv["package"]["version"], inv["package"]["release"])
        assert token == expected_token          # exact VERSION-RELEASE from the inspected package


def test_generated_invocation_opposite_family_token_absent():
    # An rpm invocation carries expected_deb == "" (dropped by the workflow), so normalize_request
    # never sees a contradictory opposite-family expected string.
    plan = _real_rag_plan()
    rpm_inv = next(i for i in plan["matrix"]["include"] if i["family"] == "rpm")
    raw = _request_from_invocation(rpm_inv)
    assert "expected_deb" not in raw and raw["expected_rpm"] == rpm_inv["expected_rpm"]
    req = pep_request.normalize_request(raw)      # would raise if expected_deb leaked into an rpm req
    assert req["expected_deb"] is None


# --------------------------------------------------------------------------- #
# L. real committed catalogs + RAG-derived cert-plan (req 11 + import boundary)
# --------------------------------------------------------------------------- #
def _rag_release(fam, os_tok):
    """Realistic native release string for a detector os token: EL9->1.el9, EL10->1.el10, and a
    Debian/Ubuntu codename token->1.<codename> (e.g. noble->1.noble, trixie->1.trixie)."""
    if fam == "rpm":
        return "1." + os_tok.replace("-", "")     # el-9 -> 1.el9, el-10 -> 1.el10
    return "1." + os_tok                            # deb codename token -> 1.<codename>


def _rag_native_arch(fam, arch):
    """Realistic native package arch: RPM uses x86_64/aarch64, DEB uses amd64/arm64."""
    return {"amd64": "x86_64", "arm64": "aarch64"}[arch] if fam == "rpm" else arch


def _rag_cells_from_fixture():
    """Build eligible, PG-decoupled cert-plan cells from the committed RAG detector matrix.

    The detector fixture's physical package is the RAG 2.x package; the LOGICAL PEP component is
    'rag' (release-level), matching the authoritative registry. Native release + arch use realistic
    per-OS values so the generated pins resemble real inspected packages."""
    doc = json.loads(RAG_DETECTOR_FIXTURE.read_text())
    seen, cells = set(), []
    for m in _iter_matrix_cells(doc):
        fam, os_tok, arch = m.get("family"), m.get("os"), m.get("normalized_arch")
        cid = m.get("cell_id")
        if not (fam and os_tok and arch and cid) or cid in seen:
            continue
        seen.add(cid)
        cells.append(cell(cid, [target(fam, os_tok, arch, package=RAG_PACKAGE, logical=RAG_COMPONENT,
                                        pg_coupled=False, release=_rag_release(fam, os_tok),
                                        native_arch=_rag_native_arch(fam, arch), cell_id=cid)]))
    return cells


def _iter_matrix_cells(obj):
    if isinstance(obj, dict):
        if {"family", "os", "normalized_arch"} <= set(obj):
            yield obj
        for v in obj.values():
            yield from _iter_matrix_cells(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_matrix_cells(v)


def test_real_catalogs_load_and_plan_rag():
    cells = _rag_cells_from_fixture()
    assert len(cells) >= 12                                   # the RAG matrix is non-trivial
    ec = json.loads(EXEC_CATALOG_FILE.read_text())
    enabled_platforms, err = P.load_enabled_platforms(str(CONTAINERS_FILE))
    assert err is None and enabled_platforms                 # real container_resolver import boundary
    plan = P.build_invocation_plan(cert_plan(cells), ec, enabled_platforms)
    assert plan["plan_resolved"] is True and plan["errors"] == []
    # bullseye (EOL) always surfaces as an explicit unsupported gap, never a silent drop.
    bull = [g for g in plan["coverage_gaps"] if g["os"] == "bullseye"]
    assert bull and all(g["reason"] == P.GAP_UNSUPPORTED_OS for g in bull)
    # every emitted invocation targets a CURRENTLY-ENABLED alias, a supported PG major, logical rag.
    alias_set = set(enabled_platforms.values())
    for inv in plan["matrix"]["include"]:
        assert inv["container_alias"] in alias_set
        assert inv["pg_major"] in ("16", "17", "18")
        assert inv["component"] == RAG_COMPONENT and inv["package_name"] == RAG_PACKAGE
    _assert_ids_workflow_valid(plan)
    assert ids(plan) == sorted(ids(plan)) and len(ids(plan)) == len(set(ids(plan)))
    # nothing planned silently vanished: every selected target is covered or has one target gap.
    c = plan["counts"]
    assert c["planned_cells"] == c["selected_targets"] == c["eligible_targets"] == len(cells)
    assert c["covered_targets"] + c["gaps_by_scope"]["target"] == c["selected_targets"]
    assert c["gaps_by_scope"] == {"cell": 0, "target": c["coverage_gaps"], "member": 0}


def test_committed_exec_catalog_is_valid_and_omits_bullseye():
    ec = json.loads(EXEC_CATALOG_FILE.read_text())
    pgs, os_map, errs = P.validate_exec_catalog(ec)
    assert errs == [] and pgs == ["16", "17", "18"]
    assert ("bullseye", "deb") not in os_map                 # EOL: intentionally unmapped
    assert ("el-9", "rpm") in os_map and ("bookworm", "deb") in os_map


def test_main_writes_plan_and_exit_code(tmp_path):
    cells = [cell("c", [target("rpm", "el-9", "amd64", cell_id="c")])]
    cp = tmp_path / "cert-plan.json"; cp.write_text(json.dumps(cert_plan(cells)))
    out = tmp_path / "inv.json"
    rc = P.main(["--cert-plan", str(cp), "--exec-catalog", str(EXEC_CATALOG_FILE),
                 "--containers", str(CONTAINERS_FILE), "--out", str(out)])
    doc = json.loads(out.read_text())
    assert rc == 0 and doc["schema"] == P.SCHEMA and doc["plan_resolved"] is True
    # el-9 amd64 -> oel9-amd64 is enabled in the committed catalog -> real invocations exist.
    assert any(i["container_alias"] == "oel9-amd64" for i in doc["matrix"]["include"])


# --------------------------------------------------------------------------- #
# execution_mode binding: preview admits preview_eligible; a plan is mode-bound
# --------------------------------------------------------------------------- #
def _preview_cell():
    # a preview-only target: strict ineligible (unpublished), separate preview_eligibility eligible
    return [cell("c", [target("rpm", "el-9", "amd64",
                              eligibility="ineligible", preview_eligibility="eligible")])]


def _preview_cert_plan(**kw):
    """A real producible preview cert-plan: a SIMULATED release stamped execution_mode=preview whose
    target is strict-ineligible (unpublished) yet additively preview-eligible (built + identity
    confirmed, dry-run). This is exactly what capture emits for a simulated `--execution-mode preview`
    run -- not a non-simulated plan artificially carrying preview eligibility."""
    kw.setdefault("execution_mode", "preview")
    kw.setdefault("simulated", True)
    return cert_plan(_preview_cell(), **kw)


def test_preview_admits_preview_eligible_targets():
    cp = _preview_cert_plan()                        # models actual simulated-preview capture output
    assert cp["execution_mode"] == "preview" and cp["release_intent"]["simulated"] is True
    t = cp["cells"][0]["targets"][0]
    assert t["eligibility"] == "ineligible" and t["preview_eligibility"] == "eligible"
    plan = P.build_invocation_plan(cp, exec_catalog(), enabled(("rpm", "amd64", "oel9")),
                                   execution_mode="preview")
    assert plan["plan_resolved"] is True and plan["execution_mode"] == "preview"
    # decoupled -> pg 16/17/18 on the enabled oel9-amd64 alias
    assert len(plan["matrix"]["include"]) == 3
    assert all(i["container_alias"] == "oel9-amd64" for i in plan["matrix"]["include"])
    assert sorted(str(i["pg_major"]) for i in plan["matrix"]["include"]) == ["16", "17", "18"]
    _assert_ids_workflow_valid(plan)


def test_full_never_runs_preview_eligible_targets():
    # DELIBERATELY CONTRADICTORY hand-built plan (capture never emits preview_eligibility in full):
    # a full-stamped plan whose target still carries preview_eligibility=eligible must prove the
    # planner IGNORES that field in full mode. Not a producible plan -- a planner-gating guard.
    plan = build(_preview_cell(), execution_mode="full")
    assert plan["plan_resolved"] is True and plan["execution_mode"] == "full"
    assert plan["matrix"]["include"] == []          # preview_eligible is not runnable in full


def test_published_eligible_runs_in_both_modes():
    cells = [cell("c", [target("rpm", "el-9", "amd64")])]   # strict eligible
    for m in ("full", "preview"):
        plan = build(cells, execution_mode=m)
        assert plan["plan_resolved"] is True and len(plan["matrix"]["include"]) == 3


def test_preview_plan_cannot_be_consumed_as_full():
    cp = _preview_cert_plan()                        # a real simulated preview plan
    plan = P.build_invocation_plan(cp, exec_catalog(), enabled(("rpm", "amd64", "oel9")),
                                   execution_mode="full")
    assert plan["plan_resolved"] is False and plan["matrix"]["include"] == []
    assert any("execution_mode mismatch" in e for e in plan["errors"])


def test_full_plan_cannot_be_consumed_as_preview():
    cp = cert_plan([cell("c", [target("rpm", "el-9", "amd64")])], execution_mode="full")
    plan = P.build_invocation_plan(cp, exec_catalog(), enabled(("rpm", "amd64", "oel9")),
                                   execution_mode="preview")
    assert plan["plan_resolved"] is False
    assert any("execution_mode mismatch" in e for e in plan["errors"])


def test_legacy_cert_plan_without_stamp_is_full_by_default():
    # an unstamped cert-plan is treated as full; a full planner accepts it (back-compat).
    cp = cert_plan([cell("c", [target("rpm", "el-9", "amd64")])])   # no execution_mode key
    assert "execution_mode" not in cp
    plan = P.build_invocation_plan(cp, exec_catalog(), enabled(("rpm", "amd64", "oel9")),
                                   execution_mode="full")
    assert plan["plan_resolved"] is True and len(plan["matrix"]["include"]) == 3
    # ...but a preview planner must reject the unstamped (==full) plan.
    bad = P.build_invocation_plan(cp, exec_catalog(), enabled(("rpm", "amd64", "oel9")),
                                  execution_mode="preview")
    assert bad["plan_resolved"] is False and any("execution_mode mismatch" in e for e in bad["errors"])


def test_invalid_execution_mode_is_unresolved():
    cp = cert_plan([cell("c", [target("rpm", "el-9", "amd64")])], execution_mode="full")
    plan = P.build_invocation_plan(cp, exec_catalog(), enabled(("rpm", "amd64", "oel9")),
                                   execution_mode="bogus")
    assert plan["plan_resolved"] is False and plan["execution_mode"] is None
    assert any("invalid execution_mode" in e for e in plan["errors"])


def test_execution_mode_is_stamped_on_the_plan():
    assert build([cell("c", [target("rpm", "el-9", "amd64")])], execution_mode="full")["execution_mode"] == "full"
    assert build(_preview_cell(), execution_mode="preview", simulated=True)["execution_mode"] == "preview"


def test_preview_matrix_is_deterministic():
    a = P.to_json(build(_preview_cell(), execution_mode="preview", simulated=True))
    b = P.to_json(build(_preview_cell(), execution_mode="preview", simulated=True))
    assert a == b


# --------------------------------------------------------------------------- #
# M. certification-only counterparts (exec catalog opt-in, resolved by container_resolver)
# --------------------------------------------------------------------------- #
import container_resolver as CR

COUNTERPARTS = ["alma10-amd64", "debian12-amd64", "ubuntu2204-amd64", "ubuntu2404-amd64"]


def _shipped_exec_catalog():
    return json.loads(EXEC_CATALOG_FILE.read_text())


def _write(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return str(p)


def _plan(tmp_path, cells, *, ec=None, containers=None):
    """The certification entry point pep-certify uses: plan_from_sources with files on disk."""
    ec_path = _write(tmp_path, "ec.json", ec) if ec is not None else str(EXEC_CATALOG_FILE)
    ct_path = _write(tmp_path, "ct.json", containers) if containers is not None else str(CONTAINERS_FILE)
    return P.plan_from_sources(cert_plan(cells, execution_mode="full"), ec_path, ct_path)


def _without_counterparts():
    ec = _shipped_exec_catalog()
    del ec["certification_counterparts"]
    return ec


def test_shipped_counterparts_are_the_four_proven_implicit_amd64_containers():
    ec = _shipped_exec_catalog()
    assert ec["certification_counterparts"] == COUNTERPARTS
    catalog = CR.load_catalog(CONTAINERS_FILE)
    base = P.enabled_platforms_from_catalog(catalog)
    admitted, errors = P.load_certification_platforms(str(CONTAINERS_FILE), ec)
    assert errors == []
    added = {k: v for k, v in admitted.items() if k not in base}
    assert added == {("rpm", "amd64", "alma10"): "alma10-amd64", ("deb", "amd64", "debian12"): "debian12-amd64",
                     ("deb", "amd64", "ubuntu2204"): "ubuntu2204-amd64", ("deb", "amd64", "ubuntu2404"): "ubuntu2404-amd64"}
    assert {k: admitted[k] for k in base} == base                  # nothing already enabled changes
    physical = {e.name for e in catalog.entries}
    for alias in COUNTERPARTS:                                     # each one is a synthesized counterpart
        entry = CR.resolve_token(catalog, alias)
        assert entry is not None and entry.name not in physical and entry.alias == alias


def test_counterparts_turn_the_four_amd64_gaps_into_legs_and_keep_every_other_invocation(tmp_path):
    cells = _rag_cells_from_fixture()                              # 16 RAG cells, incl. 2 EOL bullseye
    before = _plan(tmp_path, cells, ec=_without_counterparts())
    after = _plan(tmp_path, cells)
    assert before["plan_resolved"] is True and after["plan_resolved"] is True
    four = {("deb", "bookworm"), ("deb", "jammy"), ("deb", "noble"), ("rpm", "el-10")}
    was = [(g["family"], g["os"], g["arch"], g["reason"]) for g in before["coverage_gaps"]]
    assert sorted(x for x in was if x[3] == P.GAP_NO_ENABLED_PLATFORM) == sorted(
        (f, o, "amd64", P.GAP_NO_ENABLED_PLATFORM) for f, o in four)
    assert all(g["reason"] == P.GAP_UNSUPPORTED_OS and g["os"] == "bullseye" for g in after["coverage_gaps"])
    assert len(after["coverage_gaps"]) == len(before["coverage_gaps"]) - 4
    old = {i["invocation_id"]: i for i in before["matrix"]["include"]}
    new = {i["invocation_id"]: i for i in after["matrix"]["include"]}
    assert all(new[k] == v for k, v in old.items())               # identical, not merely present
    extra = [new[k] for k in sorted(set(new) - set(old))]
    assert len(extra) == 12 and sorted({i["container_alias"] for i in extra}) == COUNTERPARTS
    assert sorted(i["pg_major"] for i in extra) == ["16"] * 4 + ["17"] * 4 + ["18"] * 4
    assert after["counts"]["covered_targets"] == before["counts"]["covered_targets"] + 4
    _assert_ids_workflow_valid(after)


def test_without_the_opt_in_the_planner_behaves_as_before(tmp_path):
    cells = _rag_cells_from_fixture()
    enabled_platforms, err = P.load_enabled_platforms(str(CONTAINERS_FILE))   # the shared enabled set
    assert err is None
    legacy = P.build_invocation_plan(cert_plan(cells, execution_mode="full"), _without_counterparts(),
                                     enabled_platforms)
    assert P.to_json(_plan(tmp_path, cells, ec=_without_counterparts())) == P.to_json(legacy)


def test_counterparts_never_enter_the_regression_selection():
    """containers_list.json is untouched, so the older workflow's default (empty) and 'all'
    selections stay exactly as before for every family/arch."""
    catalog = CR.load_catalog(CONTAINERS_FILE)
    names = {CR.resolve_token(catalog, a).name for a in COUNTERPARTS}
    assert not names & {e.name for e in catalog.entries}
    for fam in ("rpm", "deb"):
        for arch in ("amd64", "arm64"):
            default, _, src = CR.resolve_for_target(catalog, "", None, fam, arch)
            everything, _, _ = CR.resolve_for_target(catalog, "all", None, fam, arch)
            assert src == "default" and not names & set(default) and not names & set(everything)
            assert default == [e.name for e in catalog.entries if e.enabled and e.family == fam and e.arch == arch]


def _containers(mutate):
    doc = json.loads(CONTAINERS_FILE.read_text())
    mutate(doc)
    return doc


def _deb_entry(doc, alias):
    return next(e for e in doc["deb"] if e["alias"] == alias)


@pytest.mark.parametrize("counterparts, containers, expect", [
    (["ubuntu2004-amd64"], None, "is unknown"),
    (["debian12-arm64"], None, "is a physical containers_list.json entry (auto-debian12-arm, enabled=True)"),
    (["rocky9-amd64"], None, "is a physical containers_list.json entry (my-rocky9-amd, enabled=False)"),
    (["auto-debian12-amd"], None, "must be written as the alias 'debian12-amd64'"),
    (["oel10-amd64"], None, "is no longer eligible: its arm64 sibling oel10-arm64 is not an enabled"),
    (["debian11-amd64"], None, "is no longer eligible: its arm64 sibling debian11-arm64"),
    (COUNTERPARTS, lambda d: _deb_entry(d, "debian12-arm64").update(enabled=False),
     "'debian12-amd64' is no longer eligible"),
    (COUNTERPARTS, lambda d: d["deb"].append({"name": "auto-debian12-amd", "alias": "debian12-amd64",
                                              "description": "Debian 12 / AMD64", "enabled": False}),
     "'debian12-amd64' is a physical containers_list.json entry (auto-debian12-amd, enabled=False)"),
], ids=["unknown", "wrong-arch-physical", "physical-disabled", "canonical-name", "sibling-disabled",
        "eol-sibling-disabled", "sibling-later-disabled", "became-physical"])
def test_ineligible_counterparts_fail_the_plan_closed(tmp_path, counterparts, containers, expect):
    ec = dict(_shipped_exec_catalog(), certification_counterparts=counterparts)
    plan = _plan(tmp_path, _rag_cells_from_fixture(), ec=ec,
                 containers=_containers(containers) if containers else None)
    assert plan["plan_resolved"] is False and plan["matrix"]["include"] == []
    assert any(expect in e for e in plan["errors"]), plan["errors"]


def test_unmapped_counterpart_fails_the_plan_closed(tmp_path):
    ec = _shipped_exec_catalog()
    ec["platforms"] = [p for p in ec["platforms"] if p["os_token"] != "bookworm"]
    plan = _plan(tmp_path, _rag_cells_from_fixture(), ec=ec)
    assert plan["plan_resolved"] is False and plan["matrix"]["include"] == []
    assert plan["errors"] == ["certification counterpart 'debian12-amd64' is unmapped: no exec catalog "
                              "platforms entry lists 'debian12' for deb"]


@pytest.mark.parametrize("value, expect", [
    (["debian12-amd64", "debian12-amd64"], "lists 'debian12-amd64' more than once"),
    (["debian12-amd64", "Debian12-amd64"], "lists 'Debian12-amd64' more than once"),
    ("debian12-amd64", "must be a list of container aliases"),
    ([1], "certification_counterparts[0] must be a canonical nonblank alias string"),
    ([" debian12-amd64"], "certification_counterparts[0] must be a canonical nonblank alias string"),
    ([""], "certification_counterparts[0] must be a canonical nonblank alias string"),
], ids=["duplicate", "duplicate-case", "not-a-list", "not-a-string", "padded", "blank"])
def test_malformed_counterpart_lists_fail_closed_on_every_path(tmp_path, value, expect):
    ec = dict(_shipped_exec_catalog(), certification_counterparts=value)
    via_sources = _plan(tmp_path, _rag_cells_from_fixture(), ec=ec)
    enabled_platforms, _ = P.load_enabled_platforms(str(CONTAINERS_FILE))
    via_core = P.build_invocation_plan(cert_plan(_rag_cells_from_fixture(), execution_mode="full"), ec,
                                       enabled_platforms)
    for plan in (via_sources, via_core):
        assert plan["plan_resolved"] is False and plan["matrix"]["include"] == []
        assert any(expect in e for e in plan["errors"]), plan["errors"]


def test_empty_opt_in_list_admits_nothing(tmp_path):
    plan = _plan(tmp_path, _rag_cells_from_fixture(),
                 ec=dict(_shipped_exec_catalog(), certification_counterparts=[]))
    assert plan["plan_resolved"] is True
    assert P.to_json(plan) == P.to_json(_plan(tmp_path, _rag_cells_from_fixture(), ec=_without_counterparts()))


def test_counterpart_colliding_with_an_already_enabled_key_fails_closed():
    """Defensive (hand-built map): a caller-supplied platform map that already runs the same
    family/arch/OS is never silently overwritten by a counterpart."""
    catalog = CR.load_catalog(CONTAINERS_FILE)
    base = P.enabled_platforms_from_catalog(catalog)
    base[("deb", "amd64", "debian12")] = "other-alias"
    ec = dict(_shipped_exec_catalog(), certification_counterparts=["debian12-amd64"])
    out, errors = P.admit_certification_counterparts(ec, catalog, base)
    assert out == base and errors == ["certification counterpart 'debian12-amd64' duplicates the "
                                      "enabled container 'other-alias'"]

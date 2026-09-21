"""Offline tests for pep_staging_fetch (exact staging retrieval helper).

No network, docker, rpm or dpkg. Command construction, canonical policy
resolution, cell-id parsing, candidate selection and identity verification are
driven with synthetic data.
"""
import pytest

import pep_staging_fetch as F


RAG_POLICY = {"allowed_runtime_package_names": ["pgedge-rag-server2", "pgedge-rag-server"],
              "expected_binary_version": ""}


# --- canonical package resolution -------------------------------------------
def test_canonical_package_is_first_allowed():
    assert F.canonical_package_name(RAG_POLICY) == "pgedge-rag-server2"


def test_resolve_component_policy_unknown_component_fails():
    doc = {"components": {"rag": RAG_POLICY}}
    with pytest.raises(F.FetchError):
        F.resolve_component_policy(doc, "nope")


def test_resolve_component_policy_no_components_fails():
    with pytest.raises(F.FetchError):
        F.resolve_component_policy({}, "rag")


def test_canonical_does_not_infer_from_suffix():
    # No ad hoc suffix inference: the policy's own ordering is trusted. (Coupling
    # is rejected authoritatively at the CELL via parse_cell_identity's .pg guard.)
    assert F.canonical_package_name(
        {"allowed_runtime_package_names": ["pgedge-lolor_16", "pgedge-lolor_17"]}) == "pgedge-lolor_16"


def test_empty_allowlist_rejected():
    with pytest.raises(F.FetchError):
        F.canonical_package_name({"allowed_runtime_package_names": []})


# --- safe input handling ----------------------------------------------------
@pytest.mark.parametrize("bad", [
    {"version": "2.0.0; rm -rf /"},
    {"version": "2.0.0 && echo x"},
    {"os_token": "el 9"},
    {"os_token": "el-9$(id)"},
    {"arch": "amd64; ls"},
    {"channel": "staging|x"},
    {"family": "rpm2"},
    {"buildnum": "1`id`"},
])
def test_validate_identity_rejects_unsafe(bad):
    kw = {"family": "rpm", "package_name": "pgedge-rag-server2", "os_token": "el-9",
          "arch": "amd64", "version": "2.0.0", "buildnum": "1", "channel": "staging"}
    kw.update(bad)
    with pytest.raises(F.FetchError):
        F.validate_identity(kw["family"], kw["package_name"], kw["os_token"], kw["arch"],
                            kw["version"], kw["buildnum"], kw["channel"])


def test_validate_identity_accepts_clean():
    F.validate_identity("deb", "pgedge-rag-server2", "trixie", "arm64", "2.0.0", "1", "staging")


# --- download target construction (never a filename, never arch-qualified) --
def test_download_target_rpm_is_nvr():
    assert F.download_target("rpm", "pgedge-rag-server2", "2.0.0", "1.el9") == "pgedge-rag-server2-2.0.0-1.el9"


def test_download_target_deb_is_name_eq_version():
    assert F.download_target("deb", "pgedge-rag-server2", "2.0.0", "1.trixie") == "pgedge-rag-server2=2.0.0-1.trixie"


def test_build_plan_rpm_and_deb():
    p = F.build_plan(RAG_POLICY, "rpm", "el-10", "arm64", "2.0.0", "1", "staging")
    assert p["download_target"] == "pgedge-rag-server2-2.0.0-1.el10"
    assert p["os_token"] == "el-10" and p["family"] == "rpm" and p["arch"] == "arm64"
    assert p["expected"] == {"package_name": "pgedge-rag-server2", "version": "2.0.0",
                             "release": "1.el10", "arch": "arm64"}
    d = F.build_plan(RAG_POLICY, "deb", "jammy", "amd64", "2.0.0", "beta1_1", "daily")
    assert d["download_target"] == "pgedge-rag-server2=2.0.0~beta1-1.jammy"


# --- plan_for_cell (single structured entry point; no shell eval) -----------
def test_plan_for_cell_resolves_from_cell_id():
    p = F.plan_for_cell(RAG_POLICY, "pepcell.v1.deb.trixie.amd64.pkg", "2.0.0", "1", "staging",
                        expect_family="deb", expect_arch="amd64")
    assert p["download_target"] == "pgedge-rag-server2=2.0.0-1.trixie"
    assert p["os_token"] == "trixie"


def test_plan_for_cell_rejects_pg_coupled_cell():
    with pytest.raises(F.FetchError):
        F.plan_for_cell(RAG_POLICY, "pepcell.v1.rpm.el-9.amd64.pg16.lolor", "2.0.0", "1", "staging")


@pytest.mark.parametrize("ef,ea", [("deb", "amd64"), ("rpm", "arm64")])
def test_plan_for_cell_rejects_matrix_mismatch(ef, ea):
    with pytest.raises(F.FetchError):
        F.plan_for_cell(RAG_POLICY, "pepcell.v1.rpm.el-9.amd64.pkg", "2.0.0", "1", "staging",
                        expect_family=ef, expect_arch=ea)


# --- cell-id parsing --------------------------------------------------------
def test_parse_cell_identity_decoupled():
    assert F.parse_cell_identity("pepcell.v1.rpm.el-9.amd64.pkg") == {
        "family": "rpm", "os_token": "el-9", "arch": "amd64"}
    assert F.parse_cell_identity("pepcell.v1.deb.trixie.arm64.pkg") == {
        "family": "deb", "os_token": "trixie", "arch": "arm64"}


def test_parse_cell_identity_rejects_pg_coupled():
    with pytest.raises(F.FetchError):
        F.parse_cell_identity("pepcell.v1.rpm.el-9.amd64.pg16.lolor")


@pytest.mark.parametrize("bad", ["", "pepcell.v2.rpm.el-9.amd64.pkg", "pepcell.v1.msi.el-9.amd64.pkg",
                                 "pepcell.v1.rpm.el-9.ppc64le.pkg", "not-a-cell"])
def test_parse_cell_identity_rejects_malformed(bad):
    with pytest.raises(F.FetchError):
        F.parse_cell_identity(bad)


# --- candidate selection ----------------------------------------------------
def _m(cls, name="pgedge-rag-server2"):
    return {"package_class": cls, "package_name": name}


def test_select_single_runtime_ok():
    assert F.select_single_runtime([_m("runtime"), _m("source"), _m("debug")])["package_class"] == "runtime"


def test_select_single_runtime_none_rejected():
    with pytest.raises(F.FetchError):
        F.select_single_runtime([_m("source"), _m("debug")])


def test_select_single_runtime_ambiguous_rejected():
    with pytest.raises(F.FetchError):
        F.select_single_runtime([_m("runtime"), _m("runtime")])


# --- identity verification --------------------------------------------------
def _member(**over):
    m = {"package_class": "runtime", "package_name": "pgedge-rag-server2",
         "version": "2.0.0", "release": "1.el9", "native_arch": "x86_64"}
    m.update(over)
    return m


EXPECT = {"package_name": "pgedge-rag-server2", "version": "2.0.0", "release": "1.el9", "arch": "amd64"}


def test_verify_member_exact_match():
    ok, reason = F.verify_member(_member(), EXPECT)
    assert ok, reason


@pytest.mark.parametrize("over", [
    {"package_name": "pgedge-rag-server"},
    {"version": "2.0.1"},
    {"release": "1.el10"},
    {"native_arch": "aarch64"},
    {"package_class": "source"},
    {"package_class": "debug"},
])
def test_verify_member_mismatch(over):
    ok, _ = F.verify_member(_member(**over), EXPECT)
    assert not ok

"""Unit tests for utillities.pep_verify (pure install-decision + identity
assertion; no Docker)."""
import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "pep_verify", str(Path(__file__).parent / "pep_verify.py"))
pv = importlib.util.module_from_spec(_spec)
sys.modules["pep_verify"] = pv
_spec.loader.exec_module(pv)


def _req(**o):
    base = {"family": "rpm", "expected_version": "1.0.0", "package_name": "pgedge-rag-server",
            "attemptable_now": {"l2a": False, "l2b": False, "l1": True},
            "expected_rpm": None, "expected_deb": None, "expected_binary": None}
    base.update(o)
    return base


def test_choose_install_pinned_when_explicit_rpm():
    r = _req(attemptable_now={"l2a": True, "l2b": False, "l1": True}, expected_rpm="1.0.0-1.el9")
    assert pv.choose_install(r) == ("pinned", "1.0.0-1.el9")


def test_choose_install_reduces_full_rpm_nvr_to_bare_token():
    # A full NVR (name + arch, per spec 5) must be reduced to the bare
    # VERSION-RELEASE that `dnf install pkg-<token>` needs -- not passed whole.
    r = _req(attemptable_now={"l2a": True, "l2b": False, "l1": True},
             expected_rpm="pgedge-rag-server-1.0.0-1.el9.x86_64")
    assert pv.choose_install(r) == ("pinned", "1.0.0-1.el9")


def test_choose_install_pinned_when_explicit_deb():
    # DEB Version is already the bare install token -- used verbatim (no reduction).
    r = _req(family="deb", attemptable_now={"l2a": True, "l2b": False, "l1": True},
             expected_deb="2.0.0~beta1-1.trixie")
    assert pv.choose_install(r) == ("pinned", "2.0.0~beta1-1.trixie")


def test_choose_install_latest_when_l1():
    assert pv.choose_install(_req()) == ("latest", None)


def test_assert_identity_mismatch_is_not_proven():
    r = _req(attemptable_now={"l2a": True, "l2b": False, "l1": True}, expected_rpm="1.0.0-1.el9")
    ev = pv.assert_identity({"rpm": "1.0.0-2.el9", "component_version": "1.0.0-2.el9"}, r, "pgedge-rag-server")
    assert ev["l2a"] == "not_proven"


def test_assert_identity_missing_observation_is_not_proven():
    r = _req(attemptable_now={"l2a": True, "l2b": False, "l1": True}, expected_rpm="1.0.0-1.el9")
    ev = pv.assert_identity({"rpm": None, "component_version": "1.0.0-1.el9"}, r, "pgedge-rag-server")
    assert ev["l2a"] == "not_proven"          # attemptable + unobserved -> failure


def test_assert_identity_non_attemptable_stays_not_attempted():
    ev = pv.assert_identity({"rpm": "1.0.0-1.el9", "component_version": "1.0.0-1.el9"}, _req(), "pgedge-rag-server")
    assert ev["l2a"] == "not_attempted" and ev["l1"] == "proven"


# NOTE: "" (empty) is NOT in this list. choose_install treats an empty explicit
# token as ABSENCE of a pinned token -> ('latest', None), not an unsafe value; an
# inconsistent request (l2a attemptable yet empty expected_*) is still caught later
# by assert_identity marking L2a not_proven (no silent L2->L1 downgrade). The
# primitive's rejection of "" is covered separately below.
@pytest.mark.parametrize("bad", [
    "1.0.0; rm -rf /", "1.0.0 && id", "$(id)", "`id`", "1.0.0|cat /etc/passwd",
    "1.0.0\nrm x", "1.0.0 rocky", "1.0.0#", "-1.0.0",
])
def test_choose_install_rejects_unsafe_version(bad):
    r = _req(attemptable_now={"l2a": True, "l2b": False, "l1": True}, expected_rpm=bad)
    with pytest.raises(pv.UnsafeVersionError):
        pv.choose_install(r)


def test_choose_install_empty_explicit_token_is_latest_not_unsafe():
    # An inconsistent request (l2a=True but expected_rpm="") is NOT a pinned install:
    # empty == absent -> ('latest', None). The inconsistency surfaces at assert_identity.
    r = _req(attemptable_now={"l2a": True, "l2b": False, "l1": True}, expected_rpm="")
    assert pv.choose_install(r) == ("latest", None)
    ev = pv.assert_identity({"rpm": "1.0.0-1.el9", "component_version": "1.0.0"}, r, "pgedge-rag-server")
    assert ev["l2a"] == "not_proven"          # empty expected -> not proven (a failure)


def test_assert_safe_version_rejects_empty():
    with pytest.raises(pv.UnsafeVersionError):
        pv.assert_safe_version("")


@pytest.mark.parametrize("good", ["1.0.0-1.el9", "16.11-1.bullseye", "1.0.0.beta2", "2:1.0.0-1"])
def test_assert_safe_version_accepts_real_versions(good):
    assert pv.assert_safe_version(good) == good

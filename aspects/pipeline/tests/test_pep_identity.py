"""Unit tests for pep_identity."""
import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "pep_identity", str(Path(__file__).resolve().parents[1] / "pep_identity.py")
)
pid = importlib.util.module_from_spec(_spec)
sys.modules["pep_identity"] = pid
_spec.loader.exec_module(pid)


def test_rpm_matches_bare_version_release():
    assert pid.rpm_identity_matches("1.0.0-1.el9", "1.0.0-1.el9") is True


def test_rpm_matches_full_nvra_against_bare_expected():
    # observed is full NVRA, expected is bare VERSION-RELEASE
    assert pid.rpm_identity_matches(
        "1.0.0-1.el9", "pgedge-rag-server-1.0.0-1.el9.x86_64",
        package_name="pgedge-rag-server",
    ) is True


def test_rpm_prerelease_release_field():
    assert pid.rpm_identity_matches("1.0.0-test1_1.el9", "1.0.0-test1_1.el9") is True


def test_rpm_mismatch_on_buildnum():
    assert pid.rpm_identity_matches("1.0.0-1.el9", "1.0.0-2.el9") is False


def test_deb_matches_stable():
    assert pid.deb_identity_matches("1.0.0-1.noble", "1.0.0-1.noble") is True


def test_deb_matches_prerelease_tilde():
    assert pid.deb_identity_matches("1.0.0~test1-1.noble", "1.0.0~test1-1.noble") is True


def test_deb_mismatch_tilde_vs_stable():
    assert pid.deb_identity_matches("1.0.0-1.noble", "1.0.0~test1-1.noble") is False


def test_parse_binary_version():
    out = "pgedge-rag-server\n  Version:    1.0.0-test1\n  GitCommit:  abc123\n"
    assert pid.parse_binary_version(out) == "1.0.0-test1"


def test_parse_binary_version_ignores_other_version_fields():
    # A Go-style binary may print GoVersion:/GitCommit: lines; only the real
    # `Version:` line (first token on the line) must be captured.
    out = "pgedge-rag-server\n  GoVersion:  go1.21.0\n  Version:    1.0.0-test1\n  GitCommit:  abc123\n"
    assert pid.parse_binary_version(out) == "1.0.0-test1"


def test_binary_version_matches():
    out = "  Version:    1.0.0\n"
    assert pid.binary_version_matches("1.0.0", out) is True
    assert pid.binary_version_matches("1.0.0-test1", out) is False


def test_binary_version_missing_returns_false():
    assert pid.binary_version_matches("1.0.0", "no version here") is False


def test_merge_evidence_not_proven_dominates():
    merged = pid.merge_evidence([
        {"l2a": "proven", "l2b": "not_attempted", "l1": "proven"},
        {"l2a": "not_proven", "l2b": "proven", "l1": "proven"},
    ])
    assert merged == {"l2a": "not_proven", "l2b": "proven", "l1": "proven"}


def test_merge_evidence_all_not_attempted():
    merged = pid.merge_evidence([{"l1": "not_attempted"}, {}])
    assert merged == {"l2a": "not_attempted", "l2b": "not_attempted", "l1": "not_attempted"}


def test_merge_evidence_rejects_unknown_value():
    # A supplied value outside the vocabulary is a contract error — it must raise,
    # not be silently downgraded to 'not_attempted' (a missing key still means that).
    with pytest.raises(ValueError):
        pid.merge_evidence([{"l2a": "definitely", "l2b": "proven", "l1": "proven"}])


def test_component_version_matches_beta_substring():
    # expected carries its own '-beta2'; the release is everything after the LAST '-'.
    assert pid.component_version_matches("1.0.0-beta2", "1.0.0-beta2-1.el9") is True


def test_component_version_matches_padding_mismatch():
    # A two-part version is compared as two parts: 16.12 is not 16.11.
    assert pid.component_version_matches("16.12", "16.11-1.bullseye") is False


# --- L1: the component version inside a package-manager identity -------------------------------
# The identities are the ones the release plan pins (pipeline/pep_cert_plan.py::_expected_native):
# RPM VERSION-RELEASE <version>-<buildnum>.<dist> and DEB Version <version>[~<pretag>]-<n>.<codename>,
# where a pre-release build number is <pretag>_<n>. The component version is <version> exactly, in
# the component's own format: two parts for a major.minor product, three for major.minor.patch.
@pytest.mark.parametrize("expected,observed", [
    # two-part (major.minor) component
    ("1.1", "1.1-1.el9"), ("1.1", "1.1-1.el10"), ("1.1", "1.1-1.jammy"), ("1.0", "1.0-1.bookworm"),
    ("1.1", "1.1-test1_1.el9"), ("1.1", "1.1-test1_1.el10"),
    ("1.1", "1.1~test1-1.jammy"), ("1.1", "1.1~test1-1.bookworm"), ("1.1", "1.1~test1-1.resolute"),
    ("1.1", "1.1-rc1_1.el9"), ("1.1", "1.1~rc1-1.noble"),
    ("1.1", "1.1-alpha1_1.el9"), ("1.1", "1.1~alpha1-1.trixie"),
    ("1.1", "1.1-beta1_1.el9"), ("1.1", "1.1~beta1-1.trixie"), ("1.0", "1.0~beta12-1.noble"),
    # three-part (major.minor.patch) component
    ("1.2.4", "1.2.4-1.el9"), ("1.2.4", "1.2.4-1.noble"),
    ("1.2.4", "1.2.4-test1_1.el9"), ("1.2.4", "1.2.4~test1-1.jammy"),
    ("2.6.1", "2.6.1-rc2_1.el10"), ("2.6.1", "2.6.1~rc2-1.noble"),
    ("2.0.0", "2.0.0-beta3_1.el9"), ("2.0.0", "2.0.0~beta3-1.noble"),
    # a dotted build number keeps its dist (release 1.1.el9 of version 1.1)
    ("1.1", "1.1-1.1.el9"),
])
def test_component_version_matches_the_version_in_each_build_form(expected, observed):
    assert pid.component_version_matches(expected, observed) is True


@pytest.mark.parametrize("expected,observed", [
    ("1.1", "11.1-1.jammy"), ("1.1", "11.1-1.el9"), ("1.1", "21.1-test1_1.el9"),
    ("1.1", "1.10~test1-1.noble"), ("1.1", "1.10-1.el9"),
    ("1.1", "1.1.1-1.el9"), ("1.1", "1.1.1~test1-1.jammy"), ("1.1", "1.2~test1-1.jammy"),
    ("2.6.1", "2.6.10-1.el9"), ("2.6.1", "12.6.1~test1-1.jammy"), ("2.6.1", "2.6.1.1-1.el9"),
    ("1.2.4", "1.2.40-test1_1.el9"), ("1.2.4", "1.2-1.el9"),
    # a missing patch field is not supplied: 1.1 and 1.1.0 are different versions
    ("1.1", "1.1.0-1.el9"), ("1.1.0", "1.1-1.el9"), ("1.1", "1.1.0~test1-1.jammy"),
    # only the recognised decorations may follow the version
    ("1.1", "1.1-1-1.jammy"), ("1.1", "1.1+dfsg-1.jammy"), ("1.1", "1.1~test1~x-1.jammy"),
    ("1.1", "1.1-"), ("1.1", ""), ("", "1.1-1.el9"),
])
def test_component_version_matches_rejects_another_version(expected, observed):
    assert pid.component_version_matches(expected, observed) is False

"""Unit tests for utillities.pep_identity."""
import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "pep_identity", str(Path(__file__).parent / "pep_identity.py")
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
    # observed carries a beta suffix; expected is the bare beta version. Both
    # normalize to "1.0.0.beta2", so expected is a substring of observed.
    assert pid.component_version_matches("1.0.0-beta2", "1.0.0-beta2-1.el9") is True


def test_component_version_matches_padding_mismatch():
    # "16.11" normalizes to "16.11.0"; "16.12" normalizes to "16.12.0" — not a substring.
    assert pid.component_version_matches("16.12", "16.11-1.bullseye") is False

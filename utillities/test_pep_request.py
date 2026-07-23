"""Unit tests for utillities.pep_request."""
import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "pep_request", str(Path(__file__).parent / "pep_request.py")
)
pr = importlib.util.module_from_spec(_spec)
sys.modules["pep_request"] = pr
_spec.loader.exec_module(pr)


def _base(**over):
    raw = {
        "component": "rag",
        "package_name": "pgedge-rag-server",
        "channel": "daily",
        "expected_version": "1.0.0",
        "family": "rpm",
        "arch": "amd64",
        "pg_major": "17",
        "container_alias": "rocky9-amd64",
    }
    raw.update(over)
    return raw


def test_minimal_valid_request_is_l1():
    req = pr.normalize_request(_base())
    assert req["identity_target"] == "L1"
    assert req["attemptable_now"] == {"l2a": False, "l2b": False, "l1": True}
    assert req["derivation_pending"] == {"l2a": False, "l2b": False}
    assert req["scenario"] == "certification"
    assert req["mode"] == "observe"


def test_target_fields_normalized():
    req = pr.normalize_request(_base())
    assert (req["family"], req["arch"], req["pg_major"], req["container_alias"]) == (
        "rpm", "amd64", "17", "rocky9-amd64")


def test_expected_version_absent_is_invalid():
    raw = _base(); del raw["expected_version"]
    with pytest.raises(pr.RequestError) as e:
        pr.normalize_request(raw)
    assert "expected_version" in e.value.reason


def test_invalid_channel_rejected():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_base(channel="prod"))


def test_invalid_family_rejected():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_base(family="apk"))


def test_invalid_arch_rejected():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_base(arch="x86_64"))


def test_missing_container_alias_rejected():
    raw = _base(); del raw["container_alias"]
    with pytest.raises(pr.RequestError):
        pr.normalize_request(raw)


def test_non_numeric_pg_major_rejected():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_base(pg_major="seventeen"))


def test_buildnum_alone_is_derivation_pending_not_attemptable():
    # A buildnum without an explicit expected string cannot be asserted before G6
    # decides the derivation owner. It is derivation_pending, NOT attemptable, and
    # does NOT raise identity_target.
    req = pr.normalize_request(_base(expected_buildnum="test1_1"))
    assert req["attemptable_now"]["l2a"] is False
    assert req["derivation_pending"]["l2a"] is True
    assert req["identity_target"] == "L1"


def test_effective_tag_alone_is_derivation_pending_not_attemptable():
    req = pr.normalize_request(_base(effective_tag="v1.0.0-test1"))
    assert req["attemptable_now"]["l2b"] is False
    assert req["derivation_pending"]["l2b"] is True
    assert req["identity_target"] == "L1"


def test_explicit_expected_rpm_is_attemptable_l2a():
    req = pr.normalize_request(_base(expected_rpm="1.0.0-test1_1.el9"))
    assert req["attemptable_now"]["l2a"] is True
    assert req["derivation_pending"]["l2a"] is False
    assert req["identity_target"] == "L2"


def test_explicit_expected_binary_is_attemptable_l2b():
    req = pr.normalize_request(_base(expected_binary="1.0.0-test1"))
    assert req["attemptable_now"]["l2b"] is True
    assert req["identity_target"] == "L2"


def test_empty_l2_input_is_malformed():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_base(expected_buildnum="   "))


def test_bad_effective_tag_rejected():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_base(effective_tag="1.0.0-test1"))  # missing leading v


def test_bad_mode_rejected():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_base(mode="enforce"))


def test_unknown_component_rejected():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_base(component="mcp", package_name="pgedge-postgres-mcp"))


def test_package_name_not_matching_component_rejected():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_base(package_name="pgedge-wrong"))


def test_expected_deb_with_rpm_family_rejected():
    # family is rpm (from _base); a DEB expected string is contradictory.
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_base(expected_deb="1.0.0~test1-1.noble"))


def test_expected_rpm_with_deb_family_rejected():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_base(
            family="deb", arch="amd64", container_alias="debian13-amd64",
            expected_rpm="1.0.0-test1_1.el9"))


def test_non_dict_request_rejected():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(None)


def test_invalid_scenario_rejected():
    with pytest.raises(pr.RequestError):
        pr.normalize_request(_base(scenario="rollback"))

"""Offline tests for the shared capture evidence classification (utillities/pep_evidence_class.py).

The one table and the one verdict/code/class rule that the capture core, the cert-plan reducer and
the invocation planner all use. Pure: no filesystem, network or clock.
"""
import pytest

import pep_capture as C
import pep_evidence_class as E


def test_capture_uses_the_shared_definition():
    # capture re-exports the shared table and function rather than keeping its own copy
    assert C.EVIDENCE_CLASS_BY_CODE is E.CLASS_BY_CODE
    assert C.evidence_class is E.evidence_class
    assert set(E.CLASS_BY_CODE) == set(C.REJECTION_CODES)          # no drift from capture's codes


@pytest.mark.parametrize("verdict, code", [
    ([], None), ({}, None), (1, None), (None, "MEMBER_SHA_MISMATCH"),
    ("rejected", []), ("rejected", {}), ("rejected", 7), ("ambiguous", ["CELL_AMBIGUOUS_ASSOCIATIONS"]),
])
def test_evidence_class_is_invalid_for_non_string_inputs_and_never_raises(verdict, code):
    assert E.evidence_class(verdict, code) == E.INVALID


@pytest.mark.parametrize("verdict, code, cls", [
    ("accepted", None, "accepted"),
    ("absent", None, "unavailable"),
    ("rejected", "PACKAGE_ARTIFACT_EXPIRED", "unavailable"),
    ("rejected", "PACKAGE_ARTIFACT_ABSENT", "unavailable"),
    ("rejected", "MEMBER_SHA_MISMATCH", "invalid"),
    ("rejected", "UNKNOWN_REASON", "invalid"),          # an unknown code is invalid, and says so
    ("ambiguous", "CELL_AMBIGUOUS_ASSOCIATIONS", "invalid"),
    ("ambiguous", "RECEIPT_EXPIRY_MALFORMED", "invalid"),
])
def test_consistent_outcomes_have_no_errors(verdict, code, cls):
    assert E.outcome_errors(verdict, code, "some detail", cls) == []


@pytest.mark.parametrize("verdict, code, detail, cls", [
    # a downgrade to an ordinary gap is refused, whatever the code
    ("rejected", "MEMBER_SHA_MISMATCH", "", "unavailable"),
    ("rejected", "UNKNOWN_REASON", "", "unavailable"),
    ("ambiguous", "PACKAGE_ARTIFACT_EXPIRED", "", "unavailable"),
    ("absent", None, "", "invalid"),
    ("accepted", None, "", "invalid"),
    ("rejected", "MEMBER_SHA_MISMATCH", "", "accepted"),
    # code presence must match the verdict
    ("rejected", None, "", "invalid"),
    ("absent", "PACKAGE_ARTIFACT_ABSENT", "", "unavailable"),
    ("accepted", "MEMBER_SHA_MISMATCH", "", "invalid"),
    # malformed values are reported, never raised
    ([], "MEMBER_SHA_MISMATCH", "", "invalid"),
    ({}, "MEMBER_SHA_MISMATCH", "", "invalid"),
    ("rejected", [], "", "invalid"),
    ("rejected", {}, "", "invalid"),
    ("rejected", "member sha", "", "invalid"),
    ("rejected", "MEMBER_SHA_MISMATCH", None, "invalid"),
    ("rejected", "MEMBER_SHA_MISMATCH", "", []),
    ("rejected", "MEMBER_SHA_MISMATCH", "", "harmless"),
    ("lost", None, "", "unavailable"),
])
def test_inconsistent_or_malformed_outcomes_are_errors(verdict, code, detail, cls):
    errors = E.outcome_errors(verdict, code, detail, cls)
    assert errors and all(isinstance(e, str) for e in errors)


def test_the_module_imports_nothing_from_the_repository():
    # stdlib only, so the reducer, the planner and capture can all share it without a cycle
    import ast
    from pathlib import Path
    tree = ast.parse(Path(E.__file__).read_text())
    imported = {n.names[0].name.split(".")[0] if isinstance(n, ast.Import) else (n.module or "").split(".")[0]
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert imported <= {"__future__", "re"}, imported

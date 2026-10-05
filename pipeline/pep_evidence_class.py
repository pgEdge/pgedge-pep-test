"""Capture evidence classes: the one definition of what a capture outcome means for certification.

PURE and DETERMINISTIC, stdlib only, and importing nothing from this repository, so the capture
core (``pep_capture``), the cert-plan reducer (``pep_cert_plan``) and the invocation planner
(``pep_invocation_plan``) share one table and one rule without an import cycle or a second copy.

Each planned cell's capture outcome is a verdict (accepted | rejected | ambiguous | absent) plus,
for a rejected or ambiguous cell, a stable code. Its evidence class is:
  * ``accepted``    -- verified evidence; the package is planned for testing.
  * ``unavailable`` -- evidence that is simply not there: an absent receipt (none, or only expired
    ones) or a receipt whose package artifact expired or vanished. An ordinary coverage gap.
  * ``invalid``     -- evidence that is present but malformed, unsafe, ambiguous or contradictory.
    It blocks certification in both modes.
The 'rejected' verdict alone is NOT the class: capture also rejects a missing or expired package,
so the class comes from the code through ``CLASS_BY_CODE``. Anything unknown, inconsistent or of
the wrong type is invalid, never harmless.

Unit-testable via ``pytest pipeline/tests/test_pep_evidence_class.py``.
"""
from __future__ import annotations

import re

ACCEPTED, UNAVAILABLE, INVALID = "accepted", "unavailable", "invalid"
CLASSES = (ACCEPTED, UNAVAILABLE, INVALID)
VERDICTS = ("accepted", "rejected", "ambiguous", "absent")

# Every capture rejection code (pep_capture.REJECTION_CODES) and its class.
CLASS_BY_CODE = {
    "RECEIPT_ZIP_UNSAFE": INVALID,
    "RECEIPT_ZIP_NOT_SINGLE": INVALID,
    "RECEIPT_ARCHIVE_DIGEST_MISMATCH": INVALID,
    "RECEIPT_JSON_MALFORMED": INVALID,
    "RECEIPT_SCHEMA_INVALID": INVALID,
    "RECEIPT_FIELD_INVALID": INVALID,
    "RECEIPT_EXPIRY_MALFORMED": INVALID,
    "PACKAGE_ARTIFACT_ABSENT": UNAVAILABLE,
    "PACKAGE_ARTIFACT_EXPIRED": UNAVAILABLE,
    "PACKAGE_ARCHIVE_DIGEST_MISMATCH": INVALID,
    "PACKAGE_ZIP_UNSAFE": INVALID,
    "PACKAGE_MEMBER_SET_MISMATCH": INVALID,
    "PACKAGE_BINDING_MISMATCH": INVALID,
    "MEMBER_SHA_MISMATCH": INVALID,
    "IDENTITY_MISMATCH": INVALID,
    "CELL_AMBIGUOUS_ASSOCIATIONS": INVALID,
}

_CODE_RE = re.compile(r"\A[A-Z][A-Z0-9_]{0,63}\Z")      # the shape of a stable capture code


def evidence_class(verdict, code):
    """The evidence class of one capture verdict + code. Accepted evidence carries no code and
    an absent receipt carries none either; a 'rejected' verdict is unavailable only for a code
    the table marks so. Anything else -- an ambiguous verdict, an invalid code, an unknown or
    inconsistent verdict/code, or a value that is not a string -- is invalid. Never raises."""
    if not isinstance(verdict, str) or not (code is None or isinstance(code, str)):
        return INVALID
    if verdict == "accepted" and code is None:
        return ACCEPTED
    if verdict == "absent" and code is None:
        return UNAVAILABLE
    if verdict == "rejected" and CLASS_BY_CODE.get(code) == UNAVAILABLE:
        return UNAVAILABLE
    return INVALID


def outcome_errors(verdict, code, detail, cls):
    """Why ``(verdict, code, detail, evidence_class)`` is not a well-formed capture outcome whose
    class is exactly ``evidence_class(verdict, code)``; ``[]`` when it is. A consumer that finds an
    error must fail closed. Type-checked before any lookup, so it never raises."""
    if not (isinstance(verdict, str) and verdict in VERDICTS):
        return ["verdict %.80r is not one of %s" % (verdict, list(VERDICTS))]
    if verdict in ("accepted", "absent"):
        if code is not None:
            return ["verdict %r must not carry a code, got %.80r" % (verdict, code)]
    elif not (isinstance(code, str) and _CODE_RE.match(code)):
        return ["verdict %r needs a stable code, got %.80r" % (verdict, code)]
    if not isinstance(detail, str):
        return ["detail must be a string, got %.80r" % (detail,)]
    expected = evidence_class(verdict, code)
    if not (isinstance(cls, str) and cls == expected):
        return ["evidence_class %.80r is not %r for verdict %r and code %r" % (cls, expected, verdict, code)]
    return []

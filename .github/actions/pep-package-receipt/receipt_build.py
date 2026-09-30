"""Build a deterministic ``pep-receipt/2`` from a producer's freshly-uploaded
package artifact (the engine of the PEP-owned pep-package-receipt composite action).

The producer uploads its package artifact FIRST (unchanged), then hands this
builder the flat package directory plus the artifact's immutable binding
(id + exact name + archive digest). This builder:

  * validates every input fail-closed;
  * enumerates the flat package directory, rejecting nested entries, symlinks,
    non-regular files, duplicate (case-insensitive) names and emptiness;
  * inspects every file with the committed ``pep_pkg_inspect`` implementation,
    enforcing the requested family by MAGIC BYTES (so a sidecar / wrong-family /
    non-package file fails the whole receipt) — identity is NEVER reconstructed
    from filenames or release conventions;
  * emits ``pep-receipt/2`` = {schema, cell_id, artifact_id, artifact_name,
    archive_digest, members} where members are the COMPLETE ``pep-members/1``
    records (epoch and exact ``artifact_member_path`` retained).

Canonical archive_digest: bare lowercase 64-hex SHA-256. A documented optional
``sha256:`` prefix on the input is normalized away. This is the digest of the
uploaded ARCHIVE (from upload-artifact), NOT any member's package-byte SHA-256.

TRUST BOUNDARY (documented, honest): the files inspected here are the producer's
LOCAL copies. This builder does NOT and CANNOT prove those local bytes are
byte-identical to the uploaded archive. The later capture stage downloads the
package artifact by its immutable id and INDEPENDENTLY re-verifies member paths
and SHA-256 against the receipt; only that step closes the loop.

The inspector is resolved from the action's own repository checkout (via this
file's location), so a remote consumer needs no separate PEP checkout and the
inspector stays centralized in ``utillities/``. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# Resolve the centralized inspector from the action's own checkout:
# <repo>/.github/actions/pep-package-receipt/receipt_build.py -> <repo>/utillities
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_UTIL_DIR = os.path.join(_REPO_ROOT, "utillities")
if _UTIL_DIR not in sys.path:
    sys.path.insert(0, _UTIL_DIR)

import pep_pkg_inspect as I  # noqa: E402  (path set above)

SCHEMA = "pep-receipt/2"

# cell_id charset only. This action imposes NO local, action-specific length cap:
# the detector is the single owner of cell_id generation and may legitimately
# produce long ids (e.g. long component names), and downstream treats the value as
# OPAQUE and preserves it verbatim. (Not adding a cap here is not a claim that any
# external service accepts an unbounded value. GitHub's authoritative artifact-name
# validation, actions/toolkit validateArtifactName, rejects invalid characters and
# empty names; this charset is a strict subset of what it allows.)
_CELL_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
_DIGITS_RE = re.compile(r"[0-9]+")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ReceiptError(Exception):
    """Single fail-closed boundary for receipt construction. Any inspector
    failure is wrapped as one of these; no partial receipt is ever produced."""


# --- input validation -------------------------------------------------------
def validate_cell_id(cell_id):
    """A safe, OPAQUE slug used VERBATIM in the receipt and (prefixed) as the
    receipt artifact name. Never parsed and never rewritten; this action adds no
    local length cap of its own. Only a nonblank, unpadded, artifact-safe
    [A-Za-z0-9._-] token is required (rejecting '.', '..', separators, whitespace
    and unsafe characters). Platform and component semantics are the detector's
    concern, not this action's."""
    if not isinstance(cell_id, str):
        raise ReceiptError("cell_id must be a string, got %s" % (type(cell_id).__name__,))
    if cell_id == "" or cell_id.strip() != cell_id:
        raise ReceiptError("cell_id must be nonblank and unpadded: %r" % (cell_id,))
    if cell_id in (".", ".."):
        raise ReceiptError("cell_id must not be '.' or '..'")
    if not _CELL_ID_RE.fullmatch(cell_id):
        raise ReceiptError("cell_id must be an artifact-safe [A-Za-z0-9._-] token (safe, not rewritten): %r" % (cell_id,))
    return cell_id


def validate_family(family):
    if family not in (I.RPM, I.DEB):
        raise ReceiptError("family must be %r or %r: %r" % (I.RPM, I.DEB, family))
    return family


def validate_artifact_id(value):
    """Positive integer package-artifact id. Accepts an int or an all-digit
    string; rejects zero, negative, padded, float or arbitrary text."""
    if isinstance(value, bool):
        raise ReceiptError("artifact_id must be a positive integer: %r" % (value,))
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and _DIGITS_RE.fullmatch(value):
        n = int(value)
    else:
        raise ReceiptError("artifact_id must be a positive integer: %r" % (value,))
    if n <= 0:
        raise ReceiptError("artifact_id must be > 0: %r" % (value,))
    return n


def validate_artifact_name(value):
    if not isinstance(value, str):
        raise ReceiptError("artifact_name must be a string, got %s" % (type(value).__name__,))
    if value == "" or value.strip() != value:
        raise ReceiptError("artifact_name must be nonblank and unpadded: %r" % (value,))
    return value


def normalize_archive_digest(value):
    """Canonical archive digest = bare lowercase 64-hex SHA-256. A documented
    optional ``sha256:`` prefix is normalized away. Blank/malformed fails closed.
    This is the ARCHIVE digest, never a member's package-byte SHA-256."""
    if not isinstance(value, str):
        raise ReceiptError("artifact_digest must be a string, got %s" % (type(value).__name__,))
    s = value.strip()
    if s == "":
        raise ReceiptError("artifact_digest is blank")
    if s.lower().startswith("sha256:"):
        s = s[len("sha256:"):].strip()
    s = s.lower()
    if not _SHA256_RE.fullmatch(s):
        raise ReceiptError("artifact_digest must be a sha256 hex (optionally 'sha256:'-prefixed): %r" % (value,))
    return s


# --- flat package directory enumeration -------------------------------------
def enumerate_package_dir(package_dir):
    """Return the sorted top-level file names of a v1 FLAT package directory.

    Fail closed on: missing/not-a-directory, empty, any nested directory, any
    symlink, any non-regular entry, or a case-insensitive duplicate name."""
    if not isinstance(package_dir, str) or package_dir == "":
        raise ReceiptError("package_dir must be a non-empty string: %r" % (package_dir,))
    # Reject a symlinked package_dir itself (os.path.isdir would follow it),
    # not only symlink entries inside it.
    if os.path.islink(package_dir):
        raise ReceiptError("package_dir must not be a symlink: %r" % (package_dir,))
    if not os.path.isdir(package_dir):
        raise ReceiptError("package_dir is missing or not a directory: %r" % (package_dir,))
    names = []
    try:
        with os.scandir(package_dir) as it:
            for de in it:
                if de.is_symlink():
                    raise ReceiptError("symlink not allowed in flat package artifact: %r" % (de.name,))
                if de.is_dir(follow_symlinks=False):
                    raise ReceiptError("nested directory not allowed in flat package artifact: %r" % (de.name,))
                if not de.is_file(follow_symlinks=False):
                    raise ReceiptError("non-regular entry not allowed in flat package artifact: %r" % (de.name,))
                names.append(de.name)
    except OSError as e:
        raise ReceiptError("cannot read package_dir %r: %s" % (package_dir, e))
    if not names:
        raise ReceiptError("package_dir is empty: %r" % (package_dir,))
    if len({n.lower() for n in names}) != len(names):
        raise ReceiptError("duplicate (case-insensitive) package names in flat artifact: %r" % (sorted(names),))
    return sorted(names)


# --- receipt construction ---------------------------------------------------
def build_receipt(cell_id, family, package_dir, artifact_id, artifact_name, artifact_digest):
    """Validate everything and return the deterministic ``pep-receipt/2`` dict.

    Members are the complete pep-members/1 records (sorted by artifact_member_path
    by the inspector). Requires at least one package member; multiple members
    (including runtime + source RPMs) are supported and no single runtime member
    is required at receipt time — policy selection happens later."""
    cid = validate_cell_id(cell_id)
    fam = validate_family(family)
    aid = validate_artifact_id(artifact_id)
    aname = validate_artifact_name(artifact_name)
    adigest = normalize_archive_digest(artifact_digest)
    names = enumerate_package_dir(package_dir)
    entries = [(os.path.join(package_dir, n), n) for n in names]
    try:
        members = I.inspect_members(entries, expected_family=fam)
    except I.InspectError as e:
        raise ReceiptError("package inspection failed: %s" % (e,))
    if not members:
        raise ReceiptError("receipt must not be empty: no package members in %r" % (package_dir,))
    return {
        "schema": SCHEMA,
        "cell_id": cid,
        "artifact_id": aid,
        "artifact_name": aname,
        "archive_digest": adigest,
        "members": members,
    }


def receipt_json(receipt):
    """Deterministic serialization: fixed top-level key order, members already
    sorted by artifact_member_path, member field order stable, trailing newline."""
    return json.dumps(receipt, indent=2, sort_keys=False) + "\n"


def receipt_artifact_name(cell_id):
    """The receipt artifact name, derived from the UNCHANGED cell_id."""
    return "pep-receipt-" + cell_id


# --- CLI --------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Build a pep-receipt/2 for a package artifact.")
    ap.add_argument("--cell-id", required=True)
    ap.add_argument("--family", required=True)
    ap.add_argument("--package-dir", required=True)
    ap.add_argument("--artifact-id", required=True)
    ap.add_argument("--artifact-name", required=True)
    ap.add_argument("--artifact-digest", required=True)
    ap.add_argument("--out-dir", required=True, help="directory to write receipt.json into")
    ap.add_argument("--github-output", default=None, help="path of $GITHUB_OUTPUT to append action outputs")
    args = ap.parse_args(argv)

    try:
        receipt = build_receipt(args.cell_id, args.family, args.package_dir,
                                args.artifact_id, args.artifact_name, args.artifact_digest)
    except ReceiptError as e:
        print("pep-package-receipt: FAIL: %s" % (e,), file=sys.stderr)
        return 3   # validation/inspection rejection (matches the project's exit-3 convention)

    os.makedirs(args.out_dir, exist_ok=True)
    path = os.path.join(args.out_dir, "receipt.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(receipt_json(receipt))

    name = receipt_artifact_name(receipt["cell_id"])
    outputs = {
        "receipt_artifact_name": name,
        "receipt_path": os.path.abspath(path),
        "member_count": str(len(receipt["members"])),
    }
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            for k, v in outputs.items():
                fh.write("%s=%s\n" % (k, v))
    print("pep-package-receipt: wrote %s (%d member(s)) for cell %r -> receipt artifact %r"
          % (path, len(receipt["members"]), receipt["cell_id"], name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

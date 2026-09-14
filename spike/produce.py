#!/usr/bin/env python3
"""TEMPORARY mechanics-spike producer helper (branch spike/pep-mechanics only).

Synthesises the *evidence* a real pgEdge build cell would emit, WITHOUT any real
package build or inspector:

  * make-pkg     -> writes a deterministic fake package file and records the
                    synthetic member metadata (the stand-in for a future offline
                    RPM/DEB inspector's output). The member ``sha256`` is the
                    SHA-256 of the fake package file's own bytes.
  * make-receipt -> writes a ``pep-receipt/1`` JSON binding the immutable
                    upload-artifact id to the cell_id, carrying the member
                    metadata, and recording the upload-artifact archive digest
                    as a value DISTINCT from the member sha256.

The member (version, release) are reconstructed with the SAME shared pgEdge
convention the committed reducer validates in ``_expected_native`` so the cell
can reach ``identity=confirmed``:
    RPM  release = "<buildnum>.<dist>"   dist = os with '-' removed (el-9 -> el9)
    DEB  release = "<buildnum>.<distro>" distro = os codename (bookworm)

Stdlib only. Delete with the spike branch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

# Shared synthetic release identity — kept in lockstep with spike/capture.py's
# release_intent so the reconstructed member matches the intended version/build.
PACKAGE_NAME = "pgedge-rag-server"
VERSION = "1.0.0"
BUILDNUM = "1"
RECEIPT_SCHEMA = "pep-receipt/1"

# upload-artifact's artifact-digest is a SHA-256, optionally documented with a
# "sha256:" prefix. Accept either form; reject anything else as malformed.
_DIGEST_RE = re.compile(r"\A(?:sha256:)?[0-9a-fA-F]{64}\Z")


def _native_arch(family: str, norm_arch: str) -> str:
    """Normalized cell arch -> the family's native package arch token."""
    if family == "rpm":
        return {"amd64": "x86_64", "arm64": "aarch64"}.get(norm_arch, norm_arch)
    return {"amd64": "amd64", "arm64": "arm64"}.get(norm_arch, norm_arch)  # deb


def _release(family: str, os_token: str) -> str:
    """Reconstruct the EXACT native release string for buildnum=BUILDNUM (no pretag)."""
    if family == "rpm":
        return "%s.%s" % (BUILDNUM, os_token.replace("-", ""))   # 1.el9
    return "%s.%s" % (BUILDNUM, os_token)                        # 1.bookworm


def _ext(family: str) -> str:
    return ".rpm" if family == "rpm" else ".deb"


def cmd_make_pkg(a: argparse.Namespace) -> int:
    native = _native_arch(a.family, a.arch)
    release = _release(a.family, a.os)
    fname = "%s-%s-%s.%s%s" % (PACKAGE_NAME, VERSION, release, native, _ext(a.family))
    # Deterministic fake package bytes (no real package build).
    body = ("pep-spike synthetic package\n"
            "cell=%s\nname=%s\nversion=%s\nrelease=%s\narch=%s\n"
            % (a.cell, PACKAGE_NAME, VERSION, release, native)).encode("utf-8")

    pkg_dir = Path(a.pkg_out)
    pkg_dir.mkdir(parents=True, exist_ok=True)
    pkg_path = pkg_dir / fname
    pkg_path.write_bytes(body)

    member_sha = hashlib.sha256(body).hexdigest()   # sha256 of the package file bytes
    member = {
        "package_name": PACKAGE_NAME,
        "package_class": "runtime",
        "version": VERSION,
        "release": release,
        "native_arch": native,
        "sha256": member_sha,
    }
    Path(a.member_out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.member_out).write_text(json.dumps(member, indent=2, sort_keys=True) + "\n")
    print("make-pkg: wrote %s (member sha256=%s)" % (pkg_path, member_sha))
    return 0


def cmd_make_receipt(a: argparse.Namespace) -> int:
    member = json.loads(Path(a.member_in).read_text())
    try:
        aid = int(a.artifact_id)
    except (TypeError, ValueError):
        print("make-receipt: artifact-id is not an integer: %r" % (a.artifact_id,))
        return 2
    if aid < 1:                                    # #4: reject nonpositive ids
        print("make-receipt: artifact-id must be a positive integer, got %d" % aid)
        return 2
    if not (isinstance(a.archive_digest, str) and _DIGEST_RE.match(a.archive_digest)):
        print("make-receipt: archive-digest is blank/malformed: %r" % (a.archive_digest,))
        return 2
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "cell_id": a.cell,
        "artifact_id": aid,                 # immutable id of the PACKAGE artifact
        "artifact_name": a.artifact_name,   # exact producer name (audit)
        "archive_digest": a.archive_digest, # upload-artifact ARCHIVE digest (distinct from member sha)
        "members": [member],
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print("make-receipt: wrote %s (artifact_id=%d)" % (a.out, aid))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="mechanics-spike producer helper")
    sub = p.add_subparsers(dest="cmd", required=True)

    mp = sub.add_parser("make-pkg")
    mp.add_argument("--cell", required=True)
    mp.add_argument("--family", required=True, choices=["rpm", "deb"])
    mp.add_argument("--os", required=True)
    mp.add_argument("--arch", required=True)
    mp.add_argument("--pkg-out", required=True)
    mp.add_argument("--member-out", required=True)
    mp.set_defaults(func=cmd_make_pkg)

    mr = sub.add_parser("make-receipt")
    mr.add_argument("--cell", required=True)
    mr.add_argument("--artifact-id", required=True)
    mr.add_argument("--artifact-name", required=True)
    mr.add_argument("--archive-digest", required=True)
    mr.add_argument("--member-in", required=True)
    mr.add_argument("--out", required=True)
    mr.set_defaults(func=cmd_make_receipt)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

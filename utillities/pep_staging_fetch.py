#!/usr/bin/env python3
"""Exact published-package retrieval for the PEP published-package REPLAY (spike).

This helper does NOT talk to a repository itself. It owns the parts that must be
provably safe and are testable OFFLINE:

  * strict input validation (nothing unsafe ever reaches a command line);
  * construction of the EXACT package-manager target from repository identity
    (package name + version + release/distribution), never a constructed
    filename and never an arch-qualified guess -- the download runs in an
    arch-matched container, and the arch is VERIFIED after the fact;
  * selection of exactly one RUNTIME package from what was retrieved (source and
    debug packages are rejected as the target);
  * identity verification of the retrieved bytes against the requested cell and
    the release identity, reusing the SAME expected-native reconstruction the
    cert-plan reducer uses (pep_cert_plan._expected_native), so the replay can
    never diverge from what certification will later assert.

The actual `dnf`/`apt-get` download runs inside the cell's OS container (the
workflow's in-container fetch step); this module builds the validated target for
it and verifies the result. Outputs are sanitized: only package basenames and
package identity fields, never repository URLs, credentials, absolute paths or
temp locations.

Stdlib only. Reuses pep_pkg_inspect (the committed inspector) and pep_cert_plan
(expected-native + arch/vocab constants).
"""
import argparse
import json
import os
import re
import sys

import pep_pkg_inspect
from pep_cert_plan import _expected_native, normalize_arch, FAMILIES, CHANNELS, SUPPORTED_ARCHES


class FetchError(Exception):
    """A validation, selection or identity failure. The message is safe to print
    (it never contains a URL, credential, token or absolute path)."""


# --- strict input validation ------------------------------------------------
# Every value below can reach a container env var and, through the in-container
# template, a package-manager argument. We accept only conservative character
# sets. These are intentionally tighter than what the ecosystems technically
# allow; a real pgEdge identity always fits.
_RE_PACKAGE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_RE_VERSION = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9.~_+]{0,63}\Z")
_RE_RELEASE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9.~_+]{0,63}\Z")
_RE_OSTOKEN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9-]{0,31}\Z")
_RE_BUILDNUM = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._]{0,63}\Z")


def _require(cond, msg):
    if not cond:
        raise FetchError(msg)


def validate_identity(family, package_name, os_token, arch, version, buildnum, channel):
    """Validate every field before any of it is used to build a command. Raises
    FetchError with a safe message on the first problem."""
    _require(family in FAMILIES, "family must be one of %s" % (FAMILIES,))
    _require(channel in CHANNELS, "channel must be one of %s" % (CHANNELS,))
    _require(arch in SUPPORTED_ARCHES, "arch must be one of %s" % (SUPPORTED_ARCHES,))
    _require(isinstance(package_name, str) and _RE_PACKAGE.match(package_name),
             "package_name is missing or has unsafe characters")
    _require(isinstance(os_token, str) and _RE_OSTOKEN.match(os_token),
             "os token is missing or has unsafe characters")
    _require(isinstance(version, str) and _RE_VERSION.match(version),
             "intended_version is missing or has unsafe characters")
    _require(isinstance(buildnum, str) and _RE_BUILDNUM.match(buildnum),
             "intended_buildnum is missing or has unsafe characters")


_RE_CELL = re.compile(
    r"\Apepcell\.v1\.(?P<family>rpm|deb)\.(?P<os>[A-Za-z0-9-]+)\.(?P<arch>amd64|arm64)"
    r"(?P<pg>\.pg[0-9]+)?\.(?P<comp>[A-Za-z0-9_-]+)\Z")


def parse_cell_identity(cell_id):
    """Parse the detector cell_id grammar
    'pepcell.v1.<family>.<os>.<arch>[.pg<major>].<component>' into its parts.

    A PG-coupled cell (carrying a '.pg<major>.' segment) is REJECTED here: this
    decoupled replay does not resolve per-PG package names. Returns
    {family, os_token, arch}."""
    if not isinstance(cell_id, str):
        raise FetchError("cell_id must be a string")
    m = _RE_CELL.match(cell_id)
    if not m:
        raise FetchError("cell_id %r does not match the expected pepcell.v1 grammar" % cell_id)
    if m.group("pg"):
        raise FetchError("cell_id %r is PG-coupled; the decoupled replay cannot handle it" % cell_id)
    return {"family": m.group("family"), "os_token": m.group("os"), "arch": m.group("arch")}


def canonical_package_name(component_policy):
    """Resolve the ACTIVE runtime package from PEP's component policy:
    allowed_runtime_package_names[0]. This is the single canonical source; the
    consumer never supplies a package name.

    Only the decoupled single-active-package case is supported. The AUTHORITATIVE
    coupled signal is the detector cell itself (a ``.pg<major>`` segment), which
    ``parse_cell_identity`` rejects up front -- so a PG-coupled component never
    reaches retrieval. We do NOT infer coupling from package-name suffixes (no
    authoritative policy sanctions that); the policy's own ordering is trusted and
    the first allowed runtime package is the active one."""
    _require(isinstance(component_policy, dict), "component_policy must be an object")
    names = component_policy.get("allowed_runtime_package_names")
    _require(isinstance(names, list) and names and all(isinstance(n, str) and n for n in names),
             "component_policy.allowed_runtime_package_names must be a non-empty list of strings")
    return names[0]


def expected_native(family, os_token, version, buildnum):
    """(version, release) the retrieved package MUST carry, per the shared pgEdge
    convention -- delegated to the cert-plan reducer's own reconstruction."""
    exp = _expected_native(family, os_token, version, buildnum)
    _require(exp is not None, "unsupported family/os for expected-native reconstruction")
    return exp


def download_target(family, package_name, exp_version, exp_release):
    """The EXACT package-manager target string (never a filename, never arch-
    qualified -- the container is arch-matched and the arch is verified after).
      rpm -> '<name>-<version>-<release>'   (dnf resolves from repo metadata)
      deb -> '<name>=<version>-<release>'   (apt resolves the exact version)"""
    if family == "rpm":
        return "%s-%s-%s" % (package_name, exp_version, exp_release)
    return "%s=%s-%s" % (package_name, exp_version, exp_release)


def build_plan(component_policy, family, os_token, arch, version, buildnum, channel):
    """Full validated retrieval plan for one cell. Returns a sanitized dict the
    workflow's in-container fetch step consumes (values passed as env vars, never
    interpolated into script source)."""
    package_name = canonical_package_name(component_policy)
    validate_identity(family, package_name, os_token, arch, version, buildnum, channel)
    exp_version, exp_release = expected_native(family, os_token, version, buildnum)
    return {
        "family": family,
        "os_token": os_token,
        "arch": arch,
        "channel": channel,
        "package_name": package_name,
        "download_target": download_target(family, package_name, exp_version, exp_release),
        "expected": {
            "package_name": package_name,
            "version": exp_version,
            "release": exp_release,
            "arch": arch,
        },
    }


def plan_for_cell(component_policy, cell_id, version, buildnum, channel,
                  expect_family=None, expect_arch=None):
    """Parse the cell_id (rejecting PG-coupled), cross-check the matrix-provided
    family/arch, and build the retrieval plan. This is the single structured
    entry point the workflow uses -- no shell `eval` of a separate identity step."""
    ident = parse_cell_identity(cell_id)
    if expect_family and ident["family"] != expect_family:
        raise FetchError("cell_id family %s != matrix family %s" % (ident["family"], expect_family))
    if expect_arch and ident["arch"] != expect_arch:
        raise FetchError("cell_id arch %s != matrix arch %s" % (ident["arch"], expect_arch))
    return build_plan(component_policy, ident["family"], ident["os_token"], ident["arch"],
                      version, buildnum, channel)


# --- retrieval verification (post-download, host side) ----------------------
def _list_package_files(package_dir, family):
    ext = ".rpm" if family == "rpm" else ".deb"
    out = []
    for entry in sorted(os.listdir(package_dir)):
        full = os.path.join(package_dir, entry)
        if os.path.isfile(full) and entry.endswith(ext):
            out.append(entry)
    return out


def select_single_runtime(members):
    """From inspected members, return the sole runtime member or raise. Source and
    debug packages are never a valid target; zero or multiple runtime members are
    ambiguous and rejected fail-closed."""
    runtime = [m for m in members if m.get("package_class") == "runtime"]
    nonruntime = [m for m in members if m.get("package_class") != "runtime"]
    _require(runtime, "no runtime package retrieved (only %d non-runtime file(s))" % len(nonruntime))
    _require(len(runtime) == 1, "ambiguous retrieval: %d runtime packages present" % len(runtime))
    return runtime[0]


def verify_member(member, expected):
    """Return (ok, reason). Requires an EXACT match on name, version, release and
    normalized architecture, and that the package is runtime."""
    if member.get("package_class") != "runtime":
        return False, "package_class is %r, not runtime" % member.get("package_class")
    if member.get("package_name") != expected["package_name"]:
        return False, "package name mismatch (expected %s)" % expected["package_name"]
    if member.get("version") != expected["version"]:
        return False, "version mismatch (expected %s)" % expected["version"]
    if member.get("release") != expected["release"]:
        return False, "release/distribution mismatch (expected %s)" % expected["release"]
    got_arch = normalize_arch(member.get("native_arch"))
    if got_arch != expected["arch"]:
        return False, "architecture mismatch (expected %s, got %s)" % (expected["arch"], got_arch)
    return True, "ok"


def _sanitized_member(member, package_file):
    return {
        "package_file": package_file,   # basename only
        "package_name": member.get("package_name"),
        "epoch": member.get("epoch"),
        "version": member.get("version"),
        "release": member.get("release"),
        "native_arch": member.get("native_arch"),
        "package_class": member.get("package_class"),
        "sha256": member.get("sha256"),
    }


def verify_dir(package_dir, family, expected):
    """Inspect the flat package_dir, require exactly one runtime package matching
    `expected`, and return a sanitized result dict. Raises FetchError (safe
    message) on any failure. Never emits paths beyond basenames."""
    _require(family in FAMILIES, "family must be one of %s" % (FAMILIES,))
    _require(os.path.isdir(package_dir), "package_dir does not exist")
    files = _list_package_files(package_dir, family)
    _require(files, "no %s package present in the output directory" % family)
    entries = [(os.path.join(package_dir, f), f) for f in files]
    members = []
    for full, base in entries:
        m = pep_pkg_inspect.inspect_package(full, base, expected_family=family)
        members.append(m)
    member = select_single_runtime(members)
    ok, reason = verify_member(member, expected)
    package_file = member.get("artifact_member_path")
    if not ok:
        raise FetchError("retrieved package failed identity check: %s" % reason)
    return {"ok": True, "family": family, "member": _sanitized_member(member, package_file)}


# --- CLI --------------------------------------------------------------------
def resolve_component_policy(policy_doc, logical_component):
    """Extract one component's policy object from the canonical capture-policy
    document ({"components": {"<name>": {...}}}). Fails clearly on an unknown
    component."""
    _require(isinstance(policy_doc, dict), "capture policy must be an object")
    components = policy_doc.get("components")
    _require(isinstance(components, dict), "capture policy has no 'components' object")
    policy = components.get(logical_component)
    _require(isinstance(policy, dict),
             "no component policy registered for logical component %r" % logical_component)
    return policy


def _cmd_plan(args):
    policy = resolve_component_policy(json.load(open(args.component_policy)), args.logical_component)
    plan = plan_for_cell(policy, args.cell_id, args.version, args.buildnum, args.channel,
                         expect_family=args.expect_family, expect_arch=args.expect_arch)
    sys.stdout.write(json.dumps(plan, sort_keys=True))
    return 0


def _cmd_verify(args):
    expected = {
        "package_name": args.package_name,
        "version": args.exp_version,
        "release": args.exp_release,
        "arch": args.arch,
    }
    res = verify_dir(args.package_dir, args.family, expected)
    sys.stdout.write(json.dumps(res, sort_keys=True))
    return 0


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(description="Exact staging-package retrieval helper (replay).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="Resolve+validate the retrieval plan for one cell (by cell_id).")
    p.add_argument("--component-policy", required=True)
    p.add_argument("--logical-component", required=True)
    p.add_argument("--cell-id", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--buildnum", required=True)
    p.add_argument("--channel", required=True)
    p.add_argument("--expect-family", default=None)
    p.add_argument("--expect-arch", default=None)
    p.set_defaults(func=_cmd_plan)

    v = sub.add_parser("verify", help="Verify a retrieved flat package dir.")
    v.add_argument("--package-dir", required=True)
    v.add_argument("--family", required=True)
    v.add_argument("--package-name", required=True)
    v.add_argument("--exp-version", required=True)
    v.add_argument("--exp-release", required=True)
    v.add_argument("--arch", required=True)
    v.set_defaults(func=_cmd_verify)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except FetchError as e:
        sys.stderr.write("::error::pep_staging_fetch: %s\n" % e)
        return 3


if __name__ == "__main__":
    sys.exit(main())

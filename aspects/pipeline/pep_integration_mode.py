"""Integration-mode install and identity steps shared by PEP component tests.

Moved unchanged from component-test/test_pep_rag.py so another component test (test_pep_spock.py)
installs and proves identity the same way. Only genuinely shared handling lives here:
  * install_for_request: install the requested package as the request decides (an exact pinned
    install that returns the installed file's SHA-256, or the degraded latest install), then write
    the install-evidence scope marker for this run and target;
  * record_identity: enforce install-before-identity for this run and target, gather the
    package-manager (and optional binary) observations, and persist the identity verdict.
Component specifics (which packages and platforms, binary and SBOM paths, product checks) stay in
each test. The helper modules are loaded by path, as the component tests load them; a component
test uses THIS module's instances (pep_request_env, pep_verify, pep_evidence), so tests that patch
them see one object. package_management is the same ``aspects`` module the component tests import.
"""
from __future__ import annotations

import importlib.util as _ilu
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from aspects import package_management  # noqa: E402


def _load(name):
    spec = _ilu.spec_from_file_location(name, str(Path(__file__).with_name(name + ".py")))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pep_request_env = _load("pep_request_env")
pep_verify = _load("pep_verify")
pep_evidence = _load("pep_evidence")


def install_out_path():
    return os.getenv("PEP_INSTALL_OUT", "test-logs/install-evidence.json")


def install_for_request(container, request):
    """Install ``request["package_name"]`` as the request decides and record the install scope
    marker. Returns ``(kind, exact, installed_sha256)``. A failed install raises AssertionError and
    writes no marker, so the identity step then fails its install-before-identity check."""
    kind, exact = pep_verify.choose_install(request)
    installed_sha256 = None
    if kind == "pinned":
        # EXACT / L2a -- assert the result so a failed exact-version install fails loudly here
        # instead of being papered over by identity later. The verified install also returns the
        # SHA-256 of the package file it installed, recorded below for the certification digest check.
        ok, out, installed_sha256 = package_management.install_pinned(
            container, request["package_name"], exact)
        assert ok, f"pinned install of {request['package_name']}={exact} failed: {out}"
        print(f"✅ pinned install: {request['package_name']}={exact} sha256={installed_sha256}")
    else:
        # L1 degraded path -- latest, not pinned. Install the REQUEST package
        # (caller-authoritative), never a config-named component.
        ok, platform, message = package_management.install_package(container, request["package_name"])
        assert ok, f"Package installation failed: {message}"
        print(f"✅ {message} (L1/latest -- degraded, not pinned)")
    # Record the install scope marker AFTER a successful install: binds this run + target so the
    # identity step can enforce install-before-identity.
    pep_evidence.write_install_evidence(
        request, os.environ.get("PEP_RUN_TOKEN", ""), kind, exact, install_out_path(),
        installed_sha256=installed_sha256)
    return kind, exact, installed_sha256


def record_identity(container, request, resolve_binary=None):
    """One consolidated identity verdict for ``request``. Returns ``(evidence, problems)``;
    ``evidence`` is None when the install-before-identity precondition failed (schema-valid
    zero-observation evidence is persisted instead, so the run reports a truthful failure). The
    evidence is always written before problems are returned. ``resolve_binary`` is called with no
    arguments, and only AFTER the precondition passed, to get the binary path to probe; None (or a
    blank path) means the component has no binary (L2b is then only a problem if the request asks
    for it)."""
    identity_out = os.getenv("PEP_IDENTITY_OUT", "test-logs/identity-evidence.json")
    run_token = os.environ.get("PEP_RUN_TOKEN", "")
    pre = pep_evidence.install_precondition_problems(
        pep_evidence.load_json_object(install_out_path()), request, run_token)
    if pre:
        pep_evidence.record_precondition_failure(request, identity_out)
        return None, ["install-before-identity precondition failed: " + "; ".join(pre)]
    fam = request["family"]
    pm_ver = package_management.query_installed_version(container, request["package_name"])
    binary_path = resolve_binary() if resolve_binary is not None else None
    binary_missing = not binary_path
    bin_ver = None if binary_missing else package_management.query_binary_version(container, binary_path)
    observed = {
        "rpm": pm_ver if fam == "rpm" else None,
        "deb": pm_ver if fam == "deb" else None,
        "binary": bin_ver,
        "component_version": pm_ver,
    }
    # The audit-only observed-identity.json is a SEPARATE file that never affects the verdict.
    observed_out = os.getenv("PEP_OBSERVED_OUT", "test-logs/observed-identity.json")
    return pep_evidence.record_identity_verdict(
        observed, request, identity_out, binary_missing=binary_missing,
        run_token=run_token, observed_out=observed_out)

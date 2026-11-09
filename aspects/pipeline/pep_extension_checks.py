"""Integration-mode checks shared by the PG-coupled extension component tests.

Used by component-test/test_pep_{snowflake,lolor,safesession}.py, which install and verify an
extension package the same way. Spock (test_pep_spock.py) keeps its own copies unchanged. Only the
steps that are identical for these extensions live here:
  * install_requested_extension: the PostgreSQL server for the requested major first, unpinned,
    from the request's channel; then the requested package through the shared pinned install
    (pep_integration_mode.install_for_request, which records the checksum evidence); then the
    installed versions (audit only); then a check that no other package of the component is
    installed, including uncertified companions such as -llvmjit;
  * verify_requested_extension: the running server's library belongs to the requested package,
    every node runs the requested PostgreSQL major, and the SQL extension version is the one the
    release facts allow;
  * stripped_problem: one ELF file is stripped (`file` prints "not stripped" otherwise, which a
    plain substring check for "stripped" would accept).
Product checks stay in each test. The module loads pep_integration_mode by path; a component test
uses THIS module's instance (and its pep_request_env, pep_verify, pep_evidence), so tests that patch
them see one object.
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import os
import re
from pathlib import Path

import pytest


def _load(name):
    spec = _ilu.spec_from_file_location(name, str(Path(__file__).with_name(name + ".py")))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pep_integration_mode = _load("pep_integration_mode")
package_management = pep_integration_mode.package_management
_pr = pep_integration_mode.pep_request_env._pr
_pp = _pr._pp


def skip_or_fail(request, reason):
    """A standalone run (``request`` is None) skips when an input is missing. In integration mode
    the check fails instead: skips are reported separately, so the verdict could still pass with
    the check skipped."""
    if request is not None:
        pytest.fail(reason)
    pytest.skip(reason)


def server_companions(container_type, pg_major):
    """The PostgreSQL server package an extension package depends on, for the requested major."""
    if container_type == "rhel":
        return [f"pgedge-postgresql{pg_major}-server"]
    return [f"pgedge-postgresql-{pg_major}"]


def installed_component_packages(container, container_type, request):
    """Installed packages that belong to the request's component for ANY family or PG major,
    certified or not (an uncertified companion such as -llvmjit counts), read from the package
    manager. Returns None if the query fails."""
    if container_type == "rhel":
        cmd = ["rpm", "-qa", "--queryformat", "%{NAME}\\n"]
    else:
        cmd = ["dpkg-query", "-W", "-f=${Package} ${Status}\\n"]
    exit_code, output = container.exec_run(cmd, user="root")
    if exit_code != 0:
        return None
    names = []
    for line in output.decode().splitlines():
        parts = line.split()
        if not parts or (container_type != "rhel" and "installed" not in parts[-1:]):
            continue
        names.append(parts[0])
    policy = _pr.COMPONENT_POLICY.get(request["component"])
    return sorted(n for n in names
                  if _pp.classify(policy, n, request["family"], request["pg_major"],
                                  request["expected_version"]) != _pp.R_NOT_ALLOWED)


def record_prerequisite_versions(container, container_type, request, pg_major, channel):
    """Audit only: the installed versions of the server companion and of the requested package,
    printed and written to PEP_PREREQ_OUT. Never part of the verdict."""
    names = server_companions(container_type, pg_major) + [request["package_name"]]
    versions = {n: package_management.query_installed_version(container, n) for n in names}
    out = os.getenv("PEP_PREREQ_OUT", "test-logs/prerequisite-versions.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"channel": channel, "pg_major": str(pg_major),
                                     "requested_package": request["package_name"],
                                     "installed_versions": versions}, indent=2, sort_keys=True) + "\n")
    for n, v in versions.items():
        print(f"   installed {n} = {v}")
    return versions


def install_requested_extension(container, container_type, request, pg_major, channel):
    """Install the server companion, then exactly the requested package (pinned, with checksum
    evidence), then record versions and require that it is the only package of its component.
    The companion goes first so installing it can never replace the pinned package, and a missing
    server is reported as a prerequisite failure, not as a failed extension install."""
    requested = request["package_name"]
    companions = server_companions(container_type, pg_major)
    print(f"\n--- Installing PostgreSQL {pg_major} companion: {', '.join(companions)} ---")
    try:
        success, _platform, message = package_management.install_package(
            container=container, package_name=companions, pg_major_version=pg_major,
            install_pg_server=False)
    except Exception as e:  # install_package raises on a failed install
        success, message = False, str(e)
    if not success:
        pytest.fail(f"PostgreSQL {pg_major} prerequisite unavailable from the {channel!r} channel "
                    f"({', '.join(companions)}); {requested} was not installed: {message}")
    print(f"\n--- Installing requested {requested} ---")
    pep_integration_mode.install_for_request(container, request)
    versions = record_prerequisite_versions(container, container_type, request, pg_major, channel)
    present = installed_component_packages(container, container_type, request)
    assert present == [requested], (
        f"expected exactly the requested {request['component']} package {requested!r} to be "
        f"installed, found {present!r}")
    print(f"✅ Only {requested} is installed among {request['component']} packages")
    return versions


def psql_value(container, port, pguser, sql, dbname="postgres"):
    """``(exit_code, text)`` of one unaligned, tuples-only psql query."""
    exit_code, output = container.exec_run(
        ["psql", "-h", "localhost", "-p", str(port), "-U", pguser, "-d", dbname,
         "-t", "-A", "-c", sql], user="root")
    return exit_code, output.decode().strip()


def exact_version(expected):
    """An extension-version check that accepts ``expected`` only (no prefix or substring match)."""
    def problem(observed):
        if observed != expected:
            return f"extension version is {observed!r}, expected exactly {expected!r}"
        return None
    return problem


def series_version(expected):
    """An extension-version check for a product whose release workflow requires only the numeric
    series of default_version to equal the release version: the part before the first '-' must be
    ``expected`` (the workflow's ``${default_version%%-*}``), and a '-' must be followed by
    something (the product's own pre-release marker, e.g. 1.1-alpha1). Nothing else is required of
    the marker, exactly as in the release workflow; PostgreSQL validates extension version names."""
    def problem(observed):
        series, sep, marker = (observed or "").partition("-")
        if series == expected and (not sep or marker):
            return None
        return (f"extension version is {observed!r}, expected {expected!r} "
                f"or {expected!r} with a pre-release marker")
    return problem


def library_owner(container, container_type, path):
    """``(owners, raw)``: the packages the package manager says own ``path`` (sorted; [] when it
    is unowned or the query fails) and the raw answer."""
    if container_type == "rhel":
        cmd = ["rpm", "-qf", "--queryformat", "%{NAME}\\n", path]
    else:
        cmd = ["dpkg-query", "-S", path]
    exit_code, output = container.exec_run(cmd, user="root")
    raw = output.decode().strip()
    if exit_code != 0:
        return [], raw
    if container_type == "rhel":
        return sorted({line.strip() for line in raw.splitlines() if line.strip()}), raw
    owners = set()
    for line in raw.splitlines():
        if line.startswith("diversion by "):    # "diversion by <pkg> from|to: <path>" is not ownership
            continue
        packages, sep, owned = line.partition(": ")
        if sep and owned.strip() == path:
            owners.update(p.strip().split(":", 1)[0] for p in packages.split(","))
    return sorted(owners), raw


def verify_requested_extension(container, container_type, request, *, ports, pguser, library,
                               extname, pg_major, version_problem, dbname="postgres"):
    """Integration mode: the running server's ``library`` (in its own PKGLIBDIR) belongs to the
    requested package, and on every port the server runs the requested PostgreSQL major and the
    extension version passes ``version_problem`` (None means accepted). Returns the observed
    ``{port: (server_version_num, extversion)}``."""
    ec, libdir = psql_value(container, ports[0], pguser,
                            "SELECT setting FROM pg_config WHERE name = 'PKGLIBDIR';", dbname)
    assert ec == 0 and libdir.startswith("/"), f"PKGLIBDIR query failed on port {ports[0]}: {libdir}"
    path = f"{libdir}/{library}"
    owners, raw = library_owner(container, container_type, path)
    assert owners == [request["package_name"]], (
        f"{path} belongs to {raw!r}, not the requested {request['package_name']!r}")
    print(f"✅ {path} belongs to {request['package_name']}")
    observed = {}
    for port in ports:
        ec, num = psql_value(container, port, pguser, "SHOW server_version_num;", dbname)
        assert ec == 0 and num.isdigit(), f"server_version_num query failed on port {port}: {num}"
        assert int(num) // 10000 == int(pg_major), (
            f"port {port} runs PostgreSQL {num}, not the requested PG{pg_major}")
        ec, ver = psql_value(container, port, pguser,
                             f"SELECT extversion FROM pg_extension WHERE extname = '{extname}';", dbname)
        assert ec == 0, f"Failed to query the {extname} extension version on port {port}: {ver}"
        problem = version_problem(ver)
        assert problem is None, f"port {port}: {extname} {problem}"
        print(f"✅ port {port}: PostgreSQL {num}, {extname} {ver}")
        observed[port] = (num, ver)
    return observed


def stripped_problem(container, path):
    """None if ``path`` is a stripped ELF file; otherwise why not. ``file`` prints "not stripped"
    for an unstripped file, so "stripped" alone proves nothing."""
    exit_code, output = container.exec_run(["file", "-b", "-L", path], user="root")
    out = output.decode().strip()
    if exit_code != 0:
        return f"file {path} failed: {out}"
    if not out.startswith("ELF"):
        return f"{path} is not an ELF file: {out}"
    if "not stripped" in out or not re.search(r"\bstripped\b", out):
        return f"{path} is not stripped: {out}"
    return None

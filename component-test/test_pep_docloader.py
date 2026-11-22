import importlib.util as _ilu
import os
import re
import sys
import subprocess
from pathlib import Path
from datetime import datetime

import pytest
import docker
from dotenv import load_dotenv

# Add the parent directory to sys.path to import from aspects
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from aspects import configure_repository, package_management, pg_server_management, machine_cleanup, machine_prereq_setup, file_management, container_management


def _load_util(name):
    """Load a shared integration helper from aspects/pipeline/ by path (no package
    __init__ there), so these imports work however this file is loaded."""
    spec = _ilu.spec_from_file_location(
        name, os.path.join(os.path.dirname(__file__), '..', 'aspects', 'pipeline', name + '.py'))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The shared integration-mode install/identity steps, and through them the request contract and the
# shared component policy (the same ones the radar, ACE, RAG and Spock tests use).
pep_integration_mode = _load_util('pep_integration_mode')
pep_request_env = pep_integration_mode.pep_request_env
_pr = pep_request_env._pr

load_dotenv()
client = docker.from_env()

# Integration request built from the PEP_* env contract (validated by normalize_request against the
# shared component policy, which maps docloader to pgedge-docloader). None in standalone mode, which
# keeps the standalone options below (DOCLOADER_PACKAGE, DEB_DOCLOADER_PACKAGE, PLATFORM_FILTER,
# PGEDGE_DOCLOADER_VERSION, DOCLOADER_BINARY_VERSION) unchanged.
INTEGRATION_REQUEST = pep_request_env.build_request_from_env()

# Load values from env
rhel_containers = [c.strip() for c in os.getenv("CONTAINERS", "").split(",") if c.strip()]
deb_containers = [c.strip() for c in os.getenv("DEB_CONTAINERS", "").split(",") if c.strip()]

# Combine all containers with their type
all_containers = [(c, "rhel") for c in rhel_containers] + [(c, "deb") for c in deb_containers]

# Filter containers based on platform filter (if set). In integration mode the request family is
# authoritative and overrides any configured PLATFORM_FILTER.
platform_filter = (INTEGRATION_REQUEST["family"] if INTEGRATION_REQUEST is not None
                   else os.getenv("PLATFORM_FILTER", "").lower())
if platform_filter == "rpm":
    all_containers = [(c, t) for c, t in all_containers if t == "rhel"]
elif platform_filter == "deb":
    all_containers = [(c, t) for c, t in all_containers if t == "deb"]
# If platform_filter is empty or "all", use all containers

# Common configuration
repo = os.getenv("REPO", "release")
upgrade_repo = os.getenv("UPGRADE_REPO", "staging")
skip_cleanup = os.getenv("SKIP_CLEANUP", "false").lower() == "true"
pgport = os.getenv("PG_PORT", "5432")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")
# The PACKAGE version the standalone package-version check expects (skipped when unset). It is never
# used as the binary version: a prerelease binary reports 1.0.1-rc1 while its packages are
# 1.0.1-rc1_1 (RPM) and 1.0.1~rc1-1 (DEB).
docloader_version = os.getenv("PGEDGE_DOCLOADER_VERSION", "")

# The binary both pgedge-docloader packages install (pgEdge/pgedge-docloader pkg/rpm/docloader.spec
# %install/%files: %{_bindir}/%{sname}; pkg/deb/debian/rules override_dh_auto_install). Its `version`
# subcommand prints "pgedge-docloader <version> (commit: <commit>, built: <date>)"
# (cmd/pgedge-docloader/main.go). A standalone run checks it only when DOCLOADER_BINARY_VERSION is set
# (a docloader-specific variable: the generic COMPONENT_BINARY/COMPONENT_BINARY_VERSION are shared
# last-wins across the config sections, which set COMPONENT_BINARY=/usr/bin/radar). Integration mode
# always checks it and derives the expectation from the request (expected_docloader_binary_version).
DOCLOADER_PACKAGED_BINARY = "/usr/bin/pgedge-docloader"
docloader_binary_version = os.getenv("DOCLOADER_BINARY_VERSION", "")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")
# Docloader is a standalone/decoupled package (no PG version suffix)
rhel_docloader_package = os.getenv("DOCLOADER_PACKAGE", "pgedge-docloader")

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_docloader_package = os.getenv("DEB_DOCLOADER_PACKAGE", "pgedge-docloader")

# In integration mode the package under test is exactly the requested (validated) package.
if INTEGRATION_REQUEST is not None:
    rhel_docloader_package = deb_docloader_package = INTEGRATION_REQUEST["package_name"]

# Decoupled components SBOM path
decoupled_sbom_path = os.getenv("DECOUPLED_COMPONENTS_SBOM", "")

# Additional configuration for extension tests
check_extensions = os.getenv("CHECK_EXTENSIONS", "false").lower() == "true"
base_extensions = [ext.strip() for ext in os.getenv("BASE_EXTENSIONS", "").split(",") if ext.strip()]


def integration_problem(request):
    """Why this test cannot serve ``request`` (None when it can, or in standalone mode): it must ask
    for the docloader component and for one of docloader's packages in the shared policy. The test
    never uses PostgreSQL, so the leg's PG major does not change what it can serve. Checked again
    before anything is installed."""
    if request is None:
        return None
    if request["component"] != "docloader":
        return f"component {request['component']!r} is not docloader"
    allowed = _pr.COMPONENT_PACKAGES.get("docloader", ())
    if request["package_name"] not in allowed:
        return (f"package {request['package_name']!r} is not a docloader package "
                f"(expected one of {list(allowed)})")
    return None


INTEGRATION_PROBLEM = integration_problem(INTEGRATION_REQUEST)


# docloader RPMs built by pgedge-enterprise-packages before its d80f6b7 (2026-04-17) put the SBOM
# directly under /usr/share; that commit, and docloader's own pkg/rpm/docloader.spec from its first
# version, install it under /usr/share/pgedge-docloader/. Channel metadata snapshots dated 2026-11-09
# (daily, staging and release; el9 and el10) listed exactly these two docloader version-releases, and
# the release and staging el9 file lists in those snapshots showed both in the old layout. That is an
# observation from dated snapshots, not a statement of what any channel serves now; it makes the legacy
# inventory (expected-output/rpm/docloader-enterprise-legacy) genuinely version-specific. A
# version-release does not say which packaging built the bytes (docloader's own pipeline also produces
# 1.0.0-1), so the package's own file list decides; see rpm_layout_decision. Integration mode certifies
# docloader's own build and always demands the in-repo layout (expected-output/rpm/docloader).
ENTERPRISE_LEGACY_RPM_RELEASES = frozenset({"1.0.0-1", "1.0.0-beta1_1"})
ENTERPRISE_LEGACY_RPM_REFERENCE = "docloader-enterprise-legacy"
IN_REPO_RPM_REFERENCE = "docloader"


def _rpm_reference(name):
    path = Path(__file__).parent.parent / "expected-output" / "rpm" / name
    return frozenset(line.strip() for line in path.read_text().splitlines() if line.strip())


def rpm_layout_decision(integration, version_release, owned_files):
    """Choose the RPM inventory an installed docloader is checked against: (layout, problem, note).

    ``owned_files`` is the package's own ``rpm -ql`` list; ``version_release`` is ``rpm -q --qf
    %{VERSION}-%{RELEASE}`` (None when not asked or unreadable).
    * problem: the package owns SBOM paths of both layouts. No packaging produces that mixture, and the
      shared bundled-file check tolerates extra files, so it fails here in every mode.
    * "enterprise-legacy": only a standalone run, only for one of ENTERPRISE_LEGACY_RPM_RELEASES (with
      its .el<N> dist tag), and only when the package owns every path of the complete legacy inventory.
      The SBOM check then verifies the signed SBOM where that inventory puts it.
    * "in-repo" otherwise: integration mode always; any other or unreadable version-release; a legacy
      version-release with the in-repo files (an in-repo build of the same version) or an incomplete
      legacy inventory. For the last case ``note`` names what the legacy inventory lacks.
    """
    in_repo = _rpm_reference(IN_REPO_RPM_REFERENCE)
    legacy = _rpm_reference(ENTERPRISE_LEGACY_RPM_REFERENCE)
    owned = {f.strip() for f in owned_files if f and f.strip()}
    in_repo_only, legacy_only = owned & (in_repo - legacy), owned & (legacy - in_repo)
    if in_repo_only and legacy_only:
        return "in-repo", (f"the package mixes two SBOM layouts: it owns {sorted(in_repo_only)} (in-repo) "
                           f"and {sorted(legacy_only)} (enterprise-legacy)"), None
    if integration or not version_release:
        return "in-repo", None, None
    m = re.fullmatch(r"(.+)\.el\d+", version_release.strip())
    if not (m and m.group(1) in ENTERPRISE_LEGACY_RPM_RELEASES):
        return "in-repo", None, None
    lacking = sorted(legacy - owned)
    if not lacking:
        return "enterprise-legacy", None, None
    return "in-repo", None, (f"{version_release.strip()} is not the complete enterprise-legacy inventory "
                             f"either (missing: {', '.join(lacking)})")


def _rpm_layout(container, container_type, package):
    """(layout, problem, note) for this container. DEB packages have one layout; integration runs never
    query the version-release."""
    if container_type != "rhel":
        return "in-repo", None, None
    rc, out = container.exec_run(f"rpm -ql {package}", user="root")
    owned = out.decode().splitlines() if rc == 0 and isinstance(out, (bytes, bytearray)) else []
    version_release = None
    if INTEGRATION_REQUEST is None:
        rc, out = container.exec_run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", package], user="root")
        version_release = out.decode().strip() if rc == 0 and isinstance(out, (bytes, bytearray)) else None
    layout, problem, note = rpm_layout_decision(INTEGRATION_REQUEST is not None, version_release, owned)
    print(f"RPM {package} {version_release or '(version not asked or unreadable)'}: {layout} inventory"
          + (f"; {problem}" if problem else ""))
    return layout, problem, note


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "docloader_package": rhel_docloader_package,
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "docloader_package": deb_docloader_package,
        }


def installed_docloader_packages(container, container_type):
    """Installed packages that belong to docloader, read from the package manager: the names
    docloader's policy entry accepts, and any other pgedge-docloader* package (a debug or renamed
    companion, for example). Returns None if the query fails."""
    if container_type == "rhel":
        cmd = ["rpm", "-qa", "--queryformat", "%{NAME}\\n"]
    else:
        cmd = ["dpkg-query", "-W", "-f=${Package} ${Status}\\n"]
    exit_code, output = container.exec_run(cmd, user="root")
    if exit_code != 0:
        return None
    names = set()
    for line in output.decode().splitlines():
        parts = line.split()
        if not parts or (container_type != "rhel" and "installed" not in parts[-1:]):
            continue
        names.add(parts[0])
    allowed = _pr.COMPONENT_PACKAGES.get("docloader", ())
    return sorted(n for n in names if n in allowed or n.startswith("pgedge-docloader"))


# A release tag: v<major>.<minor>.<patch>, optionally -<suffix> (rc1, rc.1, beta2, test1).
_DOCLOADER_TAG = re.compile(r"v(\d+\.\d+\.\d+)(?:-([0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?")


def expected_docloader_binary_version(request):
    """``(version, problems)``: the version `pgedge-docloader version` must report for ``request``, or
    None with the reasons it cannot be derived.

    This is the rule test_pep_radar.expected_radar_binary_version applies to radar; docloader's release
    build (pgEdge/pgedge-docloader at 7ce99c8) follows the same scheme:
      * binary: .goreleaser.yaml stamps -X main.version={{.Version}}, GoReleaser's tag without the
        leading "v", prerelease punctuation kept (v1.0.1 -> 1.0.1, v1.0.1-rc1 -> 1.0.1-rc1,
        v1.0.1-rc.1 -> 1.0.1-rc.1). This holds for a real tag build; a simulate_tag (--snapshot) build
        is stamped "<incpatch>-next" and publishes nothing to install;
      * the package versions come from pgedge-parse-release-tag (version = the tag's x.y.z; build
        number = <suffix without dots>_1, or 1 for a final release): RPM pkg/rpm/docloader.spec
        Version=<version>, Release=<build number>.<dist>; DEB pkg/common.sh moves the part of the build
        number before "_" into the version after "~" and pkg/build-deb.sh writes
        <version>[~<suffix without dots>]-1.<codename>.
    The other release facts in the request must agree with the tag; a request whose facts disagree is
    reported, not trusted."""
    tag = request.get("effective_tag")
    if not tag:
        return None, ["effective_tag is required to derive the expected docloader version"]
    match = _DOCLOADER_TAG.fullmatch(tag)
    if not match:
        return None, [f"effective_tag {tag!r} is not v<major>.<minor>.<patch>[-<suffix>]"]
    base, suffix = match.group(1), match.group(2)
    compact = suffix.replace(".", "") if suffix else None
    buildnum = f"{compact}_1" if suffix else "1"
    deb_upstream = f"{base}~{compact}" if suffix else base
    problems = []
    if base != request.get("expected_version"):
        problems.append(f"effective_tag {tag!r} does not match expected_version "
                        f"{request.get('expected_version')!r}")
    if request.get("expected_buildnum") is not None and request["expected_buildnum"] != buildnum:
        problems.append(f"expected_buildnum {request['expected_buildnum']!r} does not match "
                        f"effective_tag {tag!r} (expected {buildnum!r})")
    rpm = request.get("expected_rpm")
    if rpm is not None and not re.fullmatch(re.escape(f"{base}-{buildnum}.") + r"[0-9A-Za-z_+]+", rpm):
        problems.append(f"expected_rpm {rpm!r} does not match effective_tag {tag!r} "
                        f"(expected {base}-{buildnum}.<dist>)")
    deb = request.get("expected_deb")
    if deb is not None and not re.fullmatch(re.escape(f"{deb_upstream}-1.") + r"[0-9A-Za-z+~]+", deb):
        problems.append(f"expected_deb {deb!r} does not match effective_tag {tag!r} "
                        f"(expected {deb_upstream}-1.<codename>)")
    if problems:
        return None, problems
    return tag[1:], []


def expected_docloader_version():
    """``(version, problem)``: the version this run expects `pgedge-docloader version` to report.
    Integration mode derives it from the request (expected_docloader_binary_version) and returns a
    problem when it cannot. Standalone it is DOCLOADER_BINARY_VERSION, else None (no expectation; the
    package version is not the binary version)."""
    if INTEGRATION_REQUEST is not None:
        version, problems = expected_docloader_binary_version(INTEGRATION_REQUEST)
        if problems:
            return None, "cannot derive the expected docloader version from the request: " + "; ".join(problems)
        return version, None
    return (docloader_binary_version or None), None


def docloader_binary_owners(container, container_type):
    """``(owners, raw)``: the packages the package manager says own the docloader binary (sorted; []
    when it is unowned or the query fails) and the raw answer."""
    if container_type == "rhel":
        cmd = ["rpm", "-qf", "--queryformat", "%{NAME}\\n", DOCLOADER_PACKAGED_BINARY]
    else:
        cmd = ["dpkg-query", "-S", DOCLOADER_PACKAGED_BINARY]
    exit_code, output = container.exec_run(cmd, user="root")
    raw = output.decode().strip()
    if exit_code != 0:
        return [], raw
    if container_type == "rhel":
        return sorted({line.strip() for line in raw.splitlines() if line.strip()}), raw
    owners = set()
    for line in raw.splitlines():
        packages, sep, path = line.partition(": ")
        if sep and path.strip() == DOCLOADER_PACKAGED_BINARY:
            owners.update(p.strip().split(":", 1)[0] for p in packages.split(","))
    return sorted(owners), raw


# `pgedge-docloader version` prints exactly one line on stdout (cmd/pgedge-docloader/main.go):
# "pgedge-docloader <version> (commit: <commit>, built: <date>)".
_DOCLOADER_VERSION_LINE = re.compile(r"pgedge-docloader (\S+) \(commit: ([^\s,()]+), built: ([^\s()]+)\)")
# main.go's defaults, which only a build without the release ldflags keeps (.goreleaser.yaml stamps
# -X main.commit={{.Commit}} -X main.date={{.Date}} on every release build).
_UNSTAMPED_COMMIT, _UNSTAMPED_DATE = "none", "unknown"


def parse_docloader_version(stdout):
    """The version on the one "pgedge-docloader <version> (commit: ..., built: ...)" line of
    `pgedge-docloader version` stdout, as printed (a leading "v" or any suffix is kept, so the caller's
    exact comparison sees it).

    Raises ValueError when there is no such line, more than one, the line is malformed, or it carries
    main.go's unstamped commit/date defaults (a build without the release ldflags, whose default
    version could otherwise pass for a release)."""
    lines = [line.strip() for line in stdout.splitlines()
             if re.match(r"pgedge-docloader(\s|$)", line.strip())]
    if len(lines) != 1:
        raise ValueError(f"expected one 'pgedge-docloader <version> (commit: ..., built: ...)' line on "
                         f"stdout, found {len(lines)}")
    match = _DOCLOADER_VERSION_LINE.fullmatch(lines[0])
    if not match:
        raise ValueError(f"malformed version line {lines[0]!r}")
    if match.group(2) == _UNSTAMPED_COMMIT or match.group(3) == _UNSTAMPED_DATE:
        raise ValueError(f"the binary is not release-stamped (commit {match.group(2)!r}, built "
                         f"{match.group(3)!r}): {lines[0]!r}")
    return match.group(1)


def _skip_or_fail(reason):
    """A standalone run skips when an input is missing. In integration mode the check fails
    instead: skips are reported separately, but the certification verdict could still pass with
    the check skipped."""
    if INTEGRATION_REQUEST is not None:
        pytest.fail(reason)
    pytest.skip(reason)


def _running_container(container_name):
    """The named container, which must be running. A missing container skips a standalone run
    and fails an integration run."""
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        _skip_or_fail(f"Container {container_name} not found or not running.")
    assert container.status == "running"
    return container


# ============================================================================
# Test Functions
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_install_prerequisites(container_name, container_type):
    """Step 0: Install prerequisites using machine_prereq_setup module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Ensure container exists and is running - create if not available
    container, created, message = container_management.ensure_container_running(
        client, container_name, container_type
    )
    print(f"{'🆕 ' if created else ''}{message}")

    assert container.status == "running", f"Container {container_name} is not running (status: {container.status})"

    print(f"\n--- Installing prerequisites on {container_name} ({container_type}) ---")

    try:
        success, os_info, message = machine_prereq_setup.install_prerequisites_on_container(container)
        assert success, f"Prerequisite installation failed: {message}"
        print(f"✅ Prerequisite installation completed on {container_name} ({os_info})")
        print(f"   {message}")
    except Exception as e:
        pytest.fail(f"Failed to install prerequisites: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_configure_repository(container_name, container_type):
    """Step 1: Configure the repository file using configure_repository module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    print(f"\n--- Configuring repository in {container_name} ---")

    try:
        success, platform, message = configure_repository.configure_pgedge_repository(container, repo)
        assert success, f"Repository configuration failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to configure repository: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_install(container_name, container_type):
    """Step 2: Install pgedge-docloader using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    docloader_package = config["docloader_package"]

    container = _running_container(container_name)

    if INTEGRATION_REQUEST is not None:
        # Execution-side backstop: never install for a request this test cannot serve.
        assert INTEGRATION_PROBLEM is None, f"integration request cannot be served: {INTEGRATION_PROBLEM}"
        requested = INTEGRATION_REQUEST["package_name"]
        # docloader needs no PostgreSQL server or other pgEdge package (this test never starts one),
        # so the requested package is the only install, exactly as the request decides (pinned to its
        # exact version, with the installed file's checksum recorded as evidence).
        print(f"\n--- Installing requested {requested} on {container_name} ---")
        pep_integration_mode.install_for_request(container, INTEGRATION_REQUEST)
        present = installed_docloader_packages(container, container_type)
        assert present == [requested], (
            f"expected exactly the requested docloader package {requested!r} to be installed, "
            f"found {present!r}")
        print(f"✅ Only {requested} is installed among docloader packages")
        return

    print(f"\n--- Installing {docloader_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.install_package(container, docloader_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {docloader_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_upgrade(container_name, container_type):
    """Upgrade component package if UPGRADE=true"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: certification never upgrades; the requested package is pinned")
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    docloader_package = config["docloader_package"]

    container = _running_container(container_name)

    print(f"\n--- Upgrading {docloader_package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    try:
        success, platform, message = package_management.upgrade_package(container, docloader_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{docloader_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {docloader_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: superseded by test_docloader_identity, which checks the exact "
                    "requested version")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not docloader_version:
        pytest.skip("No PGEDGE_DOCLOADER_VERSION defined in env, skipping version check")

    config = get_container_config(container_type)
    docloader_package = config["docloader_package"]

    container = _running_container(container_name)

    print(f"\n--- Verifying {docloader_package} version on {container_name} ({container_type}) ---")

    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, docloader_package, docloader_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {docloader_package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_docloader_identity(container_name, container_type):
    """Integration mode: one consolidated identity verdict for the requested docloader package
    (install-before-identity, then the exact package-manager version). The shared binary probe
    (`<binary> -version`, read as a "Version:" line) cannot read docloader, whose version is a
    subcommand, so no binary is passed here; test_docloader_binary_version checks the binary's owner
    and exact version. The evidence is persisted before any failure. Skipped in standalone mode."""
    req = INTEGRATION_REQUEST
    if req is None:
        pytest.skip("integration mode only")

    container = _running_container(container_name.strip())

    ev, problems = pep_integration_mode.record_identity(container, req)
    if ev is None:
        pytest.fail("; ".join(problems))
    print(f"identity evidence ({req['package_name']}): {ev}")
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files for docloader match expected files

    This compares the installed files from rpm/deb with expected files in
    expected-output/rpm/docloader or expected-output/deb/docloader, which list the
    payload pgEdge/pgedge-docloader's packaging defines (pkg/rpm/docloader.spec
    %files; pkg/deb/debian/rules, debian/docs and the changelog build-deb.sh writes).
    A standalone run of an enterprise-built legacy RPM (ENTERPRISE_LEGACY_RPM_RELEASES) that owns the
    complete legacy inventory is compared with expected-output/rpm/docloader-enterprise-legacy instead;
    an RPM owning SBOM paths of both layouts fails (rpm_layout_decision).
    """
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    container = _running_container(container_name)

    config = get_container_config(container_type)
    actual_package = config["docloader_package"]

    project_root = Path(__file__).parent.parent

    layout, problem, note = _rpm_layout(container, container_type, actual_package)
    if problem:
        pytest.fail(problem)
    expected_name = ENTERPRISE_LEGACY_RPM_REFERENCE if layout == "enterprise-legacy" else None

    try:
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=actual_package,
            package_name=actual_package,
            project_root=project_root,
            expected_name=expected_name,
        )

        if not success:
            details_str = ""
            if details:
                if "missing_files" in details and details["missing_files"]:
                    details_str += f"\n\nMissing files ({len(details['missing_files'])}):\n"
                    for file in details["missing_files"]:
                        details_str += f"  - {file}\n"
                if "extra_files" in details and details["extra_files"]:
                    details_str += f"\nExtra files ({len(details['extra_files'])}):\n"
                    for file in details["extra_files"]:
                        details_str += f"  + {file}\n"
            if note:
                details_str += f"\n{note}\n"
            pytest.fail(f"{message}{details_str}")

    except Exception as e:
        # A missing expected-output reference skips a standalone run and fails an integration
        # run, where the requested release package would otherwise never be compared.
        if "No expected file found" in str(e):
            _skip_or_fail(str(e))
        pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_sbom(container_name, container_type):
    """Verify SBOM signature files located under the decoupled components SBOM directory

    Both docloader packages ship the SBOM and its detached signature as
    {DECOUPLED_COMPONENTS_SBOM}/pgedge-docloader/pgedge-docloader-sbom.json{,.asc}
    (pkg/rpm/docloader.spec %install/%files; pkg/deb/debian/rules), so a missing
    file is a package defect and fails the check. RPMs built by pgedge-enterprise-packages
    before its d80f6b7 (2026-04-17), seen in dated channel snapshots, put them directly under
    {DECOUPLED_COMPONENTS_SBOM} instead; a standalone run looks there only for those
    version-releases (ENTERPRISE_LEGACY_RPM_RELEASES) when the package owns the complete legacy
    inventory, and every other run, integration mode always, requires the subdirectory. An RPM
    owning SBOM paths of both layouts fails (rpm_layout_decision).
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not decoupled_sbom_path:
        _skip_or_fail("DECOUPLED_COMPONENTS_SBOM is not set, so the SBOM signature cannot be verified")

    container = _running_container(container_name)

    config = get_container_config(container_type)
    docloader_package = config["docloader_package"]
    # The SBOM file keeps the full package name (pgedge-docloader-sbom.json) and sits in the same
    # directory on both families: {decoupled_sbom_path}/pgedge-docloader/pgedge-docloader-sbom.json,
    # except in an enterprise-built legacy RPM that owns the complete legacy inventory (standalone only),
    # where it is directly under it. An RPM owning SBOM paths of both layouts fails before any check.
    sbom_name = docloader_package
    layout, problem, _note = _rpm_layout(container, container_type, docloader_package)
    if problem:
        pytest.fail(problem)
    if layout == "enterprise-legacy":
        sbom_dir = decoupled_sbom_path
    else:
        sbom_dir = f"{decoupled_sbom_path}/{docloader_package}"

    for name in (f"{sbom_name}-sbom.json", f"{sbom_name}-sbom.json.asc"):
        exit_code, _ = container.exec_run(["test", "-f", f"{sbom_dir}/{name}"], user="root")
        assert exit_code == 0, (f"{sbom_dir}/{name} is missing, but the docloader package ships it "
                                f"(docloader.spec %files, debian/rules)")

    if container_type == "rhel":
        print(f"\n--- Verifying SBOM on {container_name} (RHEL) in {sbom_dir} ---")

        exit_code, output = container.exec_run(
            f"wget -q -O {sbom_dir}/pgedge-rsa.pub https://dnf.pgedge.com/keys/pgedge-rsa.pub",
            user="root",
        )
        assert exit_code == 0, f"Failed to download pgedge-rsa.pub: {output.decode()}"
        print(f"✅ Downloaded pgedge-rsa.pub to {sbom_dir}")

        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"--signature-file {sbom_name}-sbom.json.asc "
            f"--signer-file pgedge-rsa.pub "
            f"{sbom_name}-sbom.json'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 authenticated signature." in output_str, \
            f"Expected '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (RHEL)")
        print(f"   {output_str.strip()}")

    else:  # deb
        print(f"\n--- Verifying SBOM on {container_name} (Deb) in {sbom_dir} ---")

        machine_prereq_setup.ensure_sq_installed(container)
        _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
        _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
        _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"
        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"{_sq_signer_flag} /etc/apt/keyrings/pgedge-rsa.gpg "
            f"{_sq_sig_flag} {sbom_name}-sbom.json.asc "
            f"{sbom_name}-sbom.json'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_docloader_binary_version(container_name, container_type):
    """Verify that `pgedge-docloader version` reports exactly the expected version.

    Standalone, the expectation is DOCLOADER_BINARY_VERSION (skipped when it is
    unset; the package version is not used). In integration mode it is the
    request's effective tag without the "v" (expected_docloader_binary_version,
    which cross-checks the other release facts), the config values are ignored,
    and /usr/bin/pgedge-docloader must first belong to the requested package, so
    the probed binary is the one under test.

    Only stdout is parsed, so a warning on stderr cannot be taken for the
    version, and the match is exact: "1.0.1" fails on any other build ("v1.0.1",
    "1.0.1-dirty", "1.0.2-next", "1.0.1-rc1"), and a pre-release passes only
    when it is the expected version. A binary carrying main.go's unstamped
    commit/date defaults fails, because its default version is not a release
    stamp.
    """
    expected_version, problem = expected_docloader_version()
    if problem:
        pytest.fail(problem)
    if INTEGRATION_REQUEST is not None:
        hint = f"from effective_tag {INTEGRATION_REQUEST['effective_tag']!r}"
    else:
        if not expected_version:
            pytest.skip("DOCLOADER_BINARY_VERSION not set — skipping binary version check")
        hint = "set DOCLOADER_BINARY_VERSION to a pre-release to expect one"

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    if INTEGRATION_REQUEST is not None:
        requested = INTEGRATION_REQUEST["package_name"]
        owners, raw = docloader_binary_owners(container, container_type)
        assert owners == [requested], (
            f"{DOCLOADER_PACKAGED_BINARY} is owned by {owners or 'no package'}, not only the requested "
            f"{requested!r} (package manager said: {raw!r})")
        print(f"✅ {DOCLOADER_PACKAGED_BINARY} belongs to {requested}")

    print(f"\n--- Checking {DOCLOADER_PACKAGED_BINARY} version on {container_name} ({container_type}) ---")

    # demux returns stdout and stderr separately; without it docker merges them.
    exit_code, (stdout, stderr) = container.exec_run(
        [DOCLOADER_PACKAGED_BINARY, "version"],
        user="root",
        demux=True,
    )
    stdout = (stdout or b"").decode()
    stderr = (stderr or b"").decode()
    print(f"   stdout: {stdout.strip()}")
    if stderr.strip():
        print(f"   stderr: {stderr.strip()}")

    assert exit_code == 0, (
        f"{DOCLOADER_PACKAGED_BINARY} version failed with exit code {exit_code}:\n"
        f"  stdout: {stdout!r}\n  stderr: {stderr!r}"
    )
    try:
        reported_version = parse_docloader_version(stdout)
    except ValueError as e:
        pytest.fail(f"Cannot read the docloader version: {e}\n"
                    f"  stdout: {stdout!r}\n  stderr: {stderr!r}")

    assert reported_version == expected_version, (
        f"docloader version mismatch: expected exactly '{expected_version}', got "
        f"'{reported_version}' ({hint})"
    )
    print(f"✅ {DOCLOADER_PACKAGED_BINARY} reports version {reported_version} (expected {expected_version})")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_package_uninstall(container_name, container_type):
    """Uninstall docloader package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    docloader_package = config["docloader_package"]

    container = _running_container(container_name)

    print(f"\n--- Uninstalling {docloader_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.uninstall_package(container, docloader_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {docloader_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Full cleanup using machine_cleanup module: remove all pgedge packages + leftover data"""
    if skip_cleanup:
        pytest.skip("Skipping cleanup: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")

    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata=None, pguser=None
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")

import importlib.util as _ilu
import json
import os
import re
import sys
from pathlib import Path

import pytest
import docker
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from aspects import configure_repository, package_management, machine_cleanup, machine_prereq_setup, file_management, container_management


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
# shared component policy, which certifies only pgedge-ai-dba-server for ai_db_workbench and names the
# upgrade-check, collector, alerter and client packages as built but not certified). None in
# standalone mode, which keeps the standalone options below (AI_DBA_*_PACKAGE, AI_DBA_*_BINARY,
# PLATFORM_FILTER, PGEDGE_AI_DBA_VERSION) and all four packages unchanged.
INTEGRATION_REQUEST = pep_request_env.build_request_from_env()

# Container configuration
rhel_containers = [c.strip() for c in os.getenv("CONTAINERS", "").split(",") if c.strip()]
deb_containers = [c.strip() for c in os.getenv("DEB_CONTAINERS", "").split(",") if c.strip()]
all_containers = [(c, "rhel") for c in rhel_containers] + [(c, "deb") for c in deb_containers]

# In integration mode the request family is authoritative and overrides any configured PLATFORM_FILTER.
platform_filter = (INTEGRATION_REQUEST["family"] if INTEGRATION_REQUEST is not None
                   else os.getenv("PLATFORM_FILTER", "").lower())
if platform_filter == "rpm":
    all_containers = [(c, t) for c, t in all_containers if t == "rhel"]
elif platform_filter == "deb":
    all_containers = [(c, t) for c, t in all_containers if t == "deb"]

# Common configuration
repo = os.getenv("REPO", "release")
upgrade_repo = os.getenv("UPGRADE_REPO", "staging")
skip_cleanup = os.getenv("SKIP_CLEANUP", "false").lower() == "true"

# AI DB Workbench package configuration (same names for RHEL and DEB — decoupled)
ai_db_workbench_server_package = os.getenv("AI_DBA_SERVER_PACKAGE", "pgedge-ai-dba-server")
ai_db_workbench_alerter_package = os.getenv("AI_DBA_ALERTER_PACKAGE", "pgedge-ai-dba-alerter")
ai_db_workbench_collector_package = os.getenv("AI_DBA_COLLECTOR_PACKAGE", "pgedge-ai-dba-collector")
ai_db_workbench_client_package = os.getenv("AI_DBA_CLIENT_PACKAGE", "pgedge-ai-dba-client")

# The server's own binary (packaging/rpm/ai-dba-workbench.spec %files -n pgedge-ai-dba-server and
# packaging/deb/debian/rules in pgEdge/ai-dba-workbench).
AI_DBA_SERVER_PACKAGED_BINARY = "/usr/bin/ai-dba-server"
ai_db_workbench_server_binary = os.getenv("AI_DBA_SERVER_BINARY", AI_DBA_SERVER_PACKAGED_BINARY)

# Every other package of the suite needs pgedge-ai-dba-upgrade-check before it unpacks: the server's
# RPM declares `Requires(pre): pgedge-ai-dba-upgrade-check = %{version}-%{release}` (spec), its DEB
# `Pre-Depends: pgedge-ai-dba-upgrade-check (>= ${binary:Version})` (debian/control). Installing the
# pinned server therefore pulls it in from the same channel; integration mode records its version
# and never certifies it. The standalone dependency expectations below do not name it: the release
# channel's 1.0.0-1 predates it (2026-11-09 metadata), while staging's 2.0.0~beta1 has it (2026-11-20).
AI_DBA_UPGRADE_CHECK_PACKAGE = "pgedge-ai-dba-upgrade-check"

# In integration mode the package under test is exactly the requested (validated) server package and
# its binary is the packaged one; the configured names and paths are not used for it.
if INTEGRATION_REQUEST is not None:
    ai_db_workbench_server_package = INTEGRATION_REQUEST["package_name"]
    ai_db_workbench_server_binary = AI_DBA_SERVER_PACKAGED_BINARY

# Install order: server first (base dependency), then dependents
ai_db_workbench_packages = [
    ai_db_workbench_server_package,
    ai_db_workbench_alerter_package,
    ai_db_workbench_collector_package,
    ai_db_workbench_client_package,
]
# Integration mode exercises ONLY the requested package (the certified server, one per cell); the
# collector, alerter and client are built beside it but not certified, so they are never installed.
if INTEGRATION_REQUEST is not None:
    ai_db_workbench_packages = [INTEGRATION_REQUEST["package_name"]]

# Version shared across all packages
ai_db_workbench_version = os.getenv("PGEDGE_AI_DBA_VERSION", "1.0.0-alpha3")
ai_db_workbench_version_map = {pkg: ai_db_workbench_version for pkg in ai_db_workbench_packages}

# Binaries — client has no standalone binary
ai_db_workbench_binary_map = {
    ai_db_workbench_alerter_package: os.getenv("AI_DBA_ALERTER_BINARY", "/usr/bin/ai-dba-alerter"),
    ai_db_workbench_collector_package: os.getenv("AI_DBA_COLLECTOR_BINARY", "/usr/bin/ai-dba-collector"),
    ai_db_workbench_server_package: ai_db_workbench_server_binary,
}

# Systemd service files — client has no service
ai_db_workbench_service_map = {
    ai_db_workbench_alerter_package: "/lib/systemd/system/pgedge-ai-dba-alerter.service",
    ai_db_workbench_collector_package: "/lib/systemd/system/pgedge-ai-dba-collector.service",
    ai_db_workbench_server_package: "/lib/systemd/system/pgedge-ai-dba-server.service",
}

# SBOM file locations per package (server uses a subdirectory; others are flat under /usr/share)
ai_db_workbench_sbom_info = {
    ai_db_workbench_alerter_package: {
        "dir": "/usr/share/pgedge-ai-dba-alerter",
        "json_file": "pgedge-ai-dba-alerter-sbom.json",
        "asc_file": "pgedge-ai-dba-alerter-sbom.json.asc",
    },
    ai_db_workbench_client_package: {
        "dir": "/usr/share/pgedge-ai-dba-client",
        "json_file": "pgedge-ai-dba-client-sbom.json",
        "asc_file": "pgedge-ai-dba-client-sbom.json.asc",
    },
    ai_db_workbench_collector_package: {
        "dir": "/usr/share/pgedge-ai-dba-collector",
        "json_file": "pgedge-ai-dba-collector-sbom.json",
        "asc_file": "pgedge-ai-dba-collector-sbom.json.asc",
    },
    ai_db_workbench_server_package: {
        "dir": "/usr/share/pgedge-ai-dba-server",
        "json_file": "pgedge-ai-dba-server-sbom.json",
        "asc_file": "pgedge-ai-dba-server-sbom.json.asc",
    },
}

# Package-level dependency declarations
ai_db_workbench_dependency_map = {
    ai_db_workbench_alerter_package: [ai_db_workbench_server_package],
    ai_db_workbench_client_package: [ai_db_workbench_server_package],
    ai_db_workbench_collector_package: [ai_db_workbench_server_package],
    ai_db_workbench_server_package: [],
}
# Integration mode tests the release just built from the base packaging, whose server declares its
# upgrade-check dependency (spec Requires(pre), debian/control Pre-Depends), so that is checked
# instead of skipping the server's dependency check.
if INTEGRATION_REQUEST is not None:
    ai_db_workbench_dependency_map[ai_db_workbench_server_package] = [AI_DBA_UPGRADE_CHECK_PACKAGE]


def generate_container_package_combinations():
    combinations = []
    for container_name, container_type in all_containers:
        for package in ai_db_workbench_packages:
            combinations.append((container_name, container_type, package))
    return combinations


all_container_package_combinations = generate_container_package_combinations()


def generate_container_binary_combinations():
    combinations = []
    for container_name, container_type in all_containers:
        for pkg, binary in ai_db_workbench_binary_map.items():
            if pkg in ai_db_workbench_packages:      # integration mode: the requested package only
                combinations.append((container_name, container_type, pkg, binary))
    return combinations


all_container_binary_combinations = generate_container_binary_combinations()


def integration_problem(request):
    """Why this test cannot serve ``request`` (None when it can, or in standalone mode): it must ask
    for the ai_db_workbench component and for the package the shared policy certifies for it. The
    test never uses PostgreSQL, so the request's PG major has no bearing on what it checks. Checked
    again before anything is installed."""
    if request is None:
        return None
    if request["component"] != "ai_db_workbench":
        return f"component {request['component']!r} is not ai_db_workbench"
    allowed = _pr.COMPONENT_PACKAGES.get("ai_db_workbench", ())
    if request["package_name"] not in allowed:
        return (f"package {request['package_name']!r} is not an ai_db_workbench package this test "
                f"certifies (expected one of {list(allowed)})")
    return None


INTEGRATION_PROBLEM = integration_problem(INTEGRATION_REQUEST)


def installed_workbench_packages(container, container_type):
    """Installed packages that belong to the workbench, read from the package manager: the name the
    policy certifies and any other pgedge-ai-dba-* package (the uncertified siblings, a debug or a
    renamed package). Returns None if the query fails."""
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
    allowed = _pr.COMPONENT_PACKAGES.get("ai_db_workbench", ())
    return sorted(n for n in names if n in allowed or n.startswith("pgedge-ai-dba-"))


def record_prerequisite_versions(container):
    """Integration mode, audit only: the installed versions of the requested server package and of
    the upgrade-check package its dependency pulled in from the same channel, printed and written to
    PEP_PREREQ_OUT. Never part of the verdict, and upgrade-check is never certified (the DEB
    dependency is `>=`, so the channel's newest one may be installed)."""
    names = [AI_DBA_UPGRADE_CHECK_PACKAGE, INTEGRATION_REQUEST["package_name"]]
    versions = {n: package_management.query_installed_version(container, n) for n in names}
    out = os.getenv("PEP_PREREQ_OUT", "test-logs/prerequisite-versions.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"channel": repo,
                                     "requested_package": INTEGRATION_REQUEST["package_name"],
                                     "installed_as_dependency": [AI_DBA_UPGRADE_CHECK_PACKAGE],
                                     "installed_versions": versions}, indent=2, sort_keys=True) + "\n")
    for n, v in versions.items():
        print(f"   installed {n} = {v}")
    return versions


# A release tag: v<major>.<minor>.<patch>, optionally -<suffix> (rc1, beta3, test1, rc.1).
_WORKBENCH_TAG = re.compile(r"v(\d+\.\d+\.\d+)(?:-([0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?")


def release_version_problems(request):
    """Why the request's release facts do not tie its package version to its release tag ([] when
    they do), by the rule the workbench release applies: pgedge-parse-release-tag gives version = the
    tag's x.y.z and build number = <suffix without dots>_1 (1 for a final release); the RPM spec uses
    Version: <version> and Release: <build number><dist>; the DEB build (packaging/common.sh,
    build-deb.sh) moves the part of the build number before "_" into the version after "~" and writes
    <version>[~<suffix>]-1.<codename>. So the tag, version, build number and package strings must
    agree; a request whose facts disagree is reported, not trusted.

    Only the PACKAGE version follows the tag. The binaries print hand-maintained constants that do not
    (see test_binary_version), so no binary version is derived here."""
    tag = request.get("effective_tag")
    if not tag:
        return ["effective_tag is required to tie the package version to the release tag"]
    match = _WORKBENCH_TAG.fullmatch(tag)
    if not match:
        return [f"effective_tag {tag!r} is not v<major>.<minor>.<patch>[-<suffix>]"]
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
    return problems


def elf_strip_problem(file_output):
    """Why `file -b <binary>` output does not describe a stripped ELF binary (None if it does).
    `file` reports "not stripped" for a binary that keeps its symbol table, and that phrase contains
    "stripped", so it is rejected first."""
    text = file_output.strip()
    if not text.startswith("ELF "):
        return "it is not an ELF binary"
    if re.search(r"\bnot stripped\b", text):
        return "it is not stripped"
    if not re.search(r"\bstripped\b", text):
        return "`file` does not report it as stripped"
    return None


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
    """Step 1: Configure the pgEdge repository using configure_repository module"""
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


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_ai_db_workbench_install(container_name, container_type, package):
    """Step 2: Install AI DB Workbench package using package_management module"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    container = _running_container(container_name)

    if INTEGRATION_REQUEST is not None:
        # Execution-side backstop: never install for a request this test cannot serve.
        assert INTEGRATION_PROBLEM is None, f"integration request cannot be served: {INTEGRATION_PROBLEM}"
        requested = INTEGRATION_REQUEST["package_name"]
        assert package == requested, f"integration mode installs only the requested {requested!r}, not {package!r}"
        # No prerequisite: the test never uses PostgreSQL. The requested server package is installed
        # exactly as the request decides (pinned + checksum evidence); its declared dependency
        # pulls pgedge-ai-dba-upgrade-check from the same channel.
        print(f"\n--- Installing requested {requested} on {container_name} ---")
        pep_integration_mode.install_for_request(container, INTEGRATION_REQUEST)
        record_prerequisite_versions(container)
        present = installed_workbench_packages(container, container_type)
        expected = sorted([requested, AI_DBA_UPGRADE_CHECK_PACKAGE])
        assert present == expected, (
            f"expected exactly the requested {requested} and its dependency {AI_DBA_UPGRADE_CHECK_PACKAGE} "
            f"among the workbench packages, found {present!r}")
        print(f"✅ Only {requested} and its dependency {AI_DBA_UPGRADE_CHECK_PACKAGE} are installed "
              f"among workbench packages")
        return

    print(f"\n--- Installing {package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.install_package(container, package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_ai_db_workbench_upgrade(container_name, container_type, package):
    """Upgrade AI DB Workbench package if UPGRADE=true"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: certification never upgrades; the requested package is pinned")
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    container = _running_container(container_name)

    print(f"\n--- Upgrading {package} on {container_name} ({container_type}) ---")

    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    try:
        success, platform, message = package_management.upgrade_package(container, package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_ai_db_workbench_package_version(container_name, container_type, package):
    """Step 3: Verify AI DB Workbench package version"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: superseded by test_ai_db_workbench_identity, which checks the "
                    "exact requested version")

    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    expected_version = ai_db_workbench_version_map.get(package, "")
    if not expected_version:
        pytest.skip(f"No version defined for {package}, skipping version check")

    container = _running_container(container_name)

    print(f"\n--- Verifying {package} version on {container_name} ({container_type}) ---")

    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, package, expected_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ai_db_workbench_identity(container_name, container_type):
    """Integration mode: one consolidated identity verdict for the requested server package
    (install-before-identity, then the exact package-manager version), persisted before any
    failure, and the request's release facts must tie that version to the release tag
    (release_version_problems). No binary is probed: ai-dba-server prints no version, and the
    binaries' version constants do not follow the tag. Skipped in standalone mode."""
    req = INTEGRATION_REQUEST
    if req is None:
        pytest.skip("integration mode only")

    container = _running_container(container_name.strip())

    ev, problems = pep_integration_mode.record_identity(container, req)
    if ev is None:
        pytest.fail("; ".join(problems))
    print(f"identity evidence ({req['package_name']}): {ev}")
    assert not problems, "; ".join(problems)
    facts = release_version_problems(req)
    assert not facts, "the request's release facts disagree: " + "; ".join(facts)
    print(f"✅ {req['package_name']} {req.get('expected_rpm') or req.get('expected_deb') or ''} follows "
          f"the release tag {req['effective_tag']}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_verify_bundled_files(container_name, container_type, package):
    """Verify bundled files for each AI DB Workbench package match expected files

    Compares installed files from rpm/deb against expected-output/rpm/ or expected-output/deb/.
    Only the server has a reference (expected-output/{rpm,deb}/ai-dba-server, from the packaging:
    the spec's %files -n pgedge-ai-dba-server and the Debian rules); the other packages have none
    and skip in standalone runs, as before.
    """
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("Invalid container or package")

    container = _running_container(container_name)

    project_root = Path(__file__).parent.parent

    try:
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=package,
            package_name=package,
            project_root=project_root
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
            pytest.fail(f"{message}{details_str}")

    except Exception as e:
        # A missing expected-output reference skips a standalone run and fails an integration
        # run, where the requested release package would otherwise never be compared. The fail
        # below is unconditional, so the check fails closed even if _skip_or_fail ever returns.
        if "No expected file found" in str(e):
            _skip_or_fail(str(e))
        pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_verify_sbom(container_name, container_type, package):
    """Verify SBOM signature files for each AI DB Workbench package"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    sbom_info = ai_db_workbench_sbom_info.get(package)
    if not sbom_info:
        _skip_or_fail(f"No SBOM info configured for {package}")

    container = _running_container(container_name)

    sbom_dir = sbom_info["dir"]
    json_file = sbom_info["json_file"]
    asc_file = sbom_info["asc_file"]

    if container_type == "rhel":
        print(f"\n--- Verifying SBOM for {package} on {container_name} (RHEL) in {sbom_dir} ---")

        exit_code, output = container.exec_run(
            f"wget -q -O {sbom_dir}/pgedge-rsa.pub https://dnf.pgedge.com/keys/pgedge-rsa.pub",
            user="root",
        )
        assert exit_code == 0, f"Failed to download pgedge-rsa.pub: {output.decode()}"
        print(f"✅ Downloaded pgedge-rsa.pub to {sbom_dir}")

        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"--signature-file {asc_file} "
            f"--signer-file pgedge-rsa.pub "
            f"{json_file}'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed for {package}: {output_str}"
        assert "1 authenticated signature." in output_str, \
            f"Expected '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified for {package} on {container_name} (RHEL)")
        print(f"   {output_str.strip()}")

    else:  # deb
        print(f"\n--- Verifying SBOM for {package} on {container_name} (Deb) in {sbom_dir} ---")

        machine_prereq_setup.ensure_sq_installed(container)
        _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
        _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
        _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"
        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"{_sq_signer_flag} /etc/apt/keyrings/pgedge-rsa.gpg "
            f"{_sq_sig_flag} {asc_file} "
            f"{json_file}'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed for {package}: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good/authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified for {package} on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_verify_license_file(container_name, container_type, package):
    """Verify LICENSE file is present at /usr/share/doc/<package>/LICENSE"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    container = _running_container(container_name)

    license_path = f"/usr/share/doc/{package}/LICENSE.md"
    print(f"\n--- Verifying license file for {package} on {container_name} ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"test -f {license_path} && echo EXISTS || echo MISSING"],
        user="root",
    )
    result = output.decode().strip()
    assert result == "EXISTS", f"LICENSE file not found at {license_path} for {package}"
    print(f"✅ LICENSE file found at {license_path}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_verify_readme_file(container_name, container_type, package):
    """Verify README.md file is present at /usr/share/doc/<package>/README.md.gz"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    container = _running_container(container_name)

    # RPM installs README.md; DEB compresses it as README.md.gz
    readme_path = (
        f"/usr/share/doc/{package}/README.md"
        if container_type == "rhel"
        else f"/usr/share/doc/{package}/README.md.gz"
    )
    print(f"\n--- Verifying README for {package} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"test -f {readme_path} && echo EXISTS || echo MISSING"],
        user="root",
    )
    result = output.decode().strip()
    assert result == "EXISTS", f"README not found at {readme_path} for {package}"
    print(f"✅ README found at {readme_path}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_verify_systemd_service(container_name, container_type, package):
    """Verify systemd service file is present for packages that ship a service unit"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    service_path = ai_db_workbench_service_map.get(package)
    if not service_path:
        pytest.skip(f"{package} does not ship a systemd service file")

    container = _running_container(container_name)

    print(f"\n--- Verifying systemd service for {package} on {container_name} ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"test -f {service_path} && echo EXISTS || echo MISSING"],
        user="root",
    )
    result = output.decode().strip()
    assert result == "EXISTS", f"Systemd service file not found at {service_path} for {package}"
    print(f"✅ Systemd service file found at {service_path}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_verify_package_dependencies(container_name, container_type, package):
    """Verify package-level dependencies are declared in package metadata"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    expected_deps = ai_db_workbench_dependency_map.get(package, [])
    if not expected_deps:
        pytest.skip(f"No dependency mapping defined for {package}")

    container = _running_container(container_name)

    print(f"\n--- Verifying package dependencies for {package} on {container_name} ({container_type}) ---")

    for dep in expected_deps:
        if container_type == "rhel":
            exit_code, output = container.exec_run(
                ["bash", "-c", f"rpm -qR {package} | grep -q '{dep}'"],
                user="root",
            )
        else:  # deb
            exit_code, output = container.exec_run(
                ["bash", "-c", f"dpkg -s {package} 2>/dev/null | grep -i Depends | grep -q '{dep}'"],
                user="root",
            )
        assert exit_code == 0, (
            f"Dependency '{dep}' not declared in {package} package metadata "
            f"on {container_name} ({container_type}). Output: {output.decode().strip()}"
        )
        print(f"✅ Dependency '{dep}' declared in {package}")


@pytest.mark.parametrize("container_name,container_type,package,binary_path", all_container_binary_combinations)
def test_binary_version(container_name, container_type, package, binary_path):
    """Verify each AI DB Workbench binary reports the expected version string

    Integration mode skips this check: the binaries print hand-maintained version constants
    (alerter/collector `Version`, server `ServerVersion`) that the release does not stamp from
    the tag (release.yml builds with `-ldflags="-s -w"` only), so no binary version follows from
    the release request. A release commit sets them by hand: v1.0.0-alpha2 still carried 1.0.0-alpha1,
    v1.0.0-rc1 and v1.0.0-test1 carried 1.0.0, and v2.0.0-beta1 builds the same way. Identity there is the
    exact package version plus the installed file's checksum (test_ai_db_workbench_identity).
    """
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: the workbench binaries print a hand-maintained version "
                    "constant that is not tied to the release tag; identity is the exact package "
                    "version and checksum (test_ai_db_workbench_identity)")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # ai-dba-server has no version flag and prints no version banner (it reports its version only
    # over MCP and /health), so there is nothing to read here
    if "ai-dba-server" in binary_path:
        pytest.skip(f"Binary version check not available for {binary_path}")

    container = _running_container(container_name)

    print(f"\n--- Checking {binary_path} version on {container_name} ({container_type}) ---")

    # These binaries print version in the startup banner then exit non-zero when no
    # database is available — don't assert exit_code; just verify the version string.
    _, output = container.exec_run(
        ["bash", "-c", f"{binary_path} 2>&1; true"],
        user="root",
    )
    version_output = output.decode().strip()
    print(f"   Output: {version_output[:200]}")

    assert ai_db_workbench_version in version_output, (
        f"Expected version '{ai_db_workbench_version}' not found in binary output:\n  {version_output[:200]}"
    )
    print(f"✅ {binary_path} reports version {ai_db_workbench_version}")


@pytest.mark.parametrize("container_name,container_type,package,binary_path", all_container_binary_combinations)
def test_binary_stripped(container_name, container_type, package, binary_path):
    """Verify each AI DB Workbench binary is a stripped ELF binary

    Runs `file -b <binary>` and requires an ELF binary that `file` reports as
    stripped and not as "not stripped" (release.yml builds with -ldflags="-s -w").
    A substring match on "stripped" alone also accepted "not stripped".
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    print(f"\n--- Checking ELF strip status of {binary_path} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(["file", "-b", binary_path], user="root")
    file_output = output.decode().strip()
    assert exit_code == 0, f"'file -b {binary_path}' failed: {file_output}"
    print(f"   Output: {file_output}")

    problem = elf_strip_problem(file_output)
    assert problem is None, (
        f"Binary {binary_path} is not a stripped ELF binary: {problem}.\n"
        f"'file' output: {file_output}"
    )
    print(f"✅ {binary_path} is stripped")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_ai_db_workbench_uninstall(container_name, container_type, package):
    """Uninstall AI DB Workbench package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    container = _running_container(container_name)

    print(f"\n--- Uninstalling {package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.uninstall_package(container, package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Full cleanup using machine_cleanup module: remove all pgedge packages + leftover data"""
    if skip_cleanup:
        pytest.skip("Skipping cleanup: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

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
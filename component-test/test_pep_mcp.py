import importlib.util as _ilu
import os
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
# shared component policy, which certifies only pgedge-postgres-mcp for mcp and rejects a request for
# the built-but-uncertified pgedge-nla-cli or pgedge-nla-web). None in standalone mode, which keeps
# the standalone options below (MCP_COMPONENTS, DEB_MCP_COMPONENTS, PLATFORM_FILTER and the version
# variables) unchanged.
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

# MCP Components - standalone packages (no PG version suffix)
rhel_mcp_components = [c.strip() for c in os.getenv("MCP_COMPONENTS", "").split(",") if c.strip()]
deb_mcp_components = [c.strip() for c in os.getenv("DEB_MCP_COMPONENTS", "").split(",") if c.strip()]

# In integration mode the only package exercised is the requested (validated) one: the server package
# pgedge-postgres-mcp. The CLI, the web UI and the legacy knowledge-base package in MCP_COMPONENTS are
# neither installed nor tested, and the requested package is installed and tested even when
# MCP_COMPONENTS does not name it.
if INTEGRATION_REQUEST is not None:
    rhel_mcp_components = deb_mcp_components = [INTEGRATION_REQUEST["package_name"]]

# Only the integration backstop reads the PostgreSQL major: the MCP tests install and use no
# PostgreSQL, so each PG leg of a certification repeats the same package test.
pg_major_version = os.getenv("PG_MAJOR_VERSION", "")

# MCP Component versions
mcp_version = os.getenv("PGEDGE_POSTGRES_MCP_VERSION", "")
mcp_kb_version = os.getenv("PGEDGE_POSTGRES_MCP_KB_VERSION", "")
nla_cli_version = os.getenv("PGEDGE_NLA_CLI_VERSION", "")
nla_web_version = os.getenv("PGEDGE_NLA_WEB_VERSION", "")

# Version mapping for MCP components
mcp_version_map = {
    "pgedge-postgres-mcp": mcp_version,
    "pgedge-postgres-mcp-kb": mcp_kb_version,
    "pgedge-nla-cli": nla_cli_version,
    "pgedge-nla-web": nla_web_version,
}

# Decoupled components SBOM path
decoupled_sbom_path = os.getenv("DECOUPLED_COMPONENTS_SBOM", "")

# The server package's SBOM (pgEdge/pgedge-postgres-mcp pkg/rpm/pgedge-nla.spec %files and
# pkg/deb/debian/rules ship /usr/share/pgedge-postgres-mcp/pgedge-postgres-mcp-sbom.json and .asc).
mcp_sbom_package = "pgedge-postgres-mcp"

# Name prefixes of the packages the MCP repository builds or built: the server and the legacy
# pgedge-postgres-mcp-kb, and pgedge-nla-cli / pgedge-nla-web (source package pgedge-nla).
_MCP_PACKAGE_PREFIXES = ("pgedge-postgres-mcp", "pgedge-nla")


def integration_problem(request, pg_major_env):
    """Why this test cannot serve ``request`` (None when it can, or in standalone mode): it must ask
    for the mcp component, for one of mcp's certified packages in the shared policy, and for the
    PostgreSQL major of the configuration this run loaded (PG_MAJOR_VERSION). Checked again before
    anything is installed."""
    if request is None:
        return None
    if request["component"] != "mcp":
        return f"component {request['component']!r} is not mcp"
    allowed = _pr.COMPONENT_PACKAGES.get("mcp", ())
    if request["package_name"] not in allowed:
        return (f"package {request['package_name']!r} is not an mcp package PEP certifies "
                f"(expected one of {list(allowed)})")
    if str(request["pg_major"]) != str(pg_major_env):
        return f"request PG{request['pg_major']} disagrees with PG_MAJOR_VERSION={pg_major_env}"
    return None


INTEGRATION_PROBLEM = integration_problem(INTEGRATION_REQUEST, pg_major_version)


def installed_mcp_packages(container, container_type):
    """Installed packages that belong to the MCP repository, read from the package manager: the
    names mcp's policy entry certifies or lists as built but not certified, and any other
    pgedge-postgres-mcp* or pgedge-nla* package. Returns None if the query fails."""
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
    policy = _pr.COMPONENT_POLICY.get("mcp", {})
    known = set(_pr.COMPONENT_PACKAGES.get("mcp", ())) | set(_pr._pp.flat_uncertified_names(policy))
    return sorted(n for n in names if n in known or n.startswith(_MCP_PACKAGE_PREFIXES))


def path_owners(container, container_type, path):
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
        packages, sep, owned = line.partition(": ")
        if sep and owned.strip() == path:
            owners.update(p.strip().split(":", 1)[0] for p in packages.split(","))
    return sorted(owners), raw


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


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "mcp_components": rhel_mcp_components,
        }
    else:  # deb
        return {
            "mcp_components": deb_mcp_components,
        }


def get_mcp_components_for_container(container_type):
    """Get list of MCP components for the container type"""
    config = get_container_config(container_type)
    return config["mcp_components"]


# Generate all container-component combinations for parametrization
def generate_container_component_combinations():
    """Generate all combinations of containers and their MCP components"""
    combinations = []
    for container_name, container_type in all_containers:
        components = get_mcp_components_for_container(container_type)
        for component in components:
            combinations.append((container_name, container_type, component))
    return combinations


all_container_component_combinations = generate_container_component_combinations()


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

    # Use the machine_prereq_setup module
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

    # Use the configure_repository module
    try:
        success, platform, message = configure_repository.configure_pgedge_repository(container, repo)
        assert success, f"Repository configuration failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to configure repository: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_mcp_component_install(container_name, container_type, component):
    """Step 2: Install MCP component using package_management module"""
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    container = _running_container(container_name)

    if INTEGRATION_REQUEST is not None:
        # Execution-side backstop: never install for a request this test cannot serve, and never a
        # package other than the requested one.
        assert INTEGRATION_PROBLEM is None, f"integration request cannot be served: {INTEGRATION_PROBLEM}"
        requested = INTEGRATION_REQUEST["package_name"]
        assert component == requested, (
            f"integration mode installs only the requested {requested!r}, not {component!r}")
        # No prerequisite: the server package depends on no other pgEdge package (pgedge-nla.spec
        # declares no Requires for it; debian/control: ${shlibs:Depends}, ${misc:Depends}). The
        # requested package is installed exactly as the request decides (pinned + checksum evidence).
        print(f"\n--- Installing requested {requested} on {container_name} ---")
        pep_integration_mode.install_for_request(container, INTEGRATION_REQUEST)
        present = installed_mcp_packages(container, container_type)
        assert present == [requested], (
            f"expected exactly the requested MCP package {requested!r} to be installed, "
            f"found {present!r}")
        print(f"✅ Only {requested} is installed among MCP packages")
        return

    print(f"\n--- Installing {component} on {container_name} ({container_type}) ---")

    # Use the package_management module to install the package
    try:
        success, platform, message = package_management.install_package(container, component)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {component}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_mcp_component_upgrade(container_name, container_type, component):
    """Upgrade MCP component if UPGRADE=true"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: certification never upgrades; the requested package is pinned")
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    container = _running_container(container_name)

    print(f"\n--- Upgrading {component} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    try:
        success, platform, message = package_management.upgrade_package(container, component)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{component} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {component}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_mcp_component_version(container_name, container_type, component):
    """Step 3: Check the MCP component version using package_management module"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: superseded by test_mcp_identity, which checks the exact "
                    "requested version")

    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    # Get expected version for this component
    expected_version = mcp_version_map.get(component, "")
    if not expected_version:
        pytest.skip(f"No version defined for {component} in env, skipping version check")

    container = _running_container(container_name)

    print(f"\n--- Verifying {component} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, component, expected_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {component} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_mcp_identity(container_name, container_type):
    """Integration mode: one consolidated identity verdict for the requested MCP package
    (install-before-identity, then the exact package-manager version, with the checksum of the
    pinned install recorded by test_mcp_component_install). No binary version is probed: the server
    has no --version flag (none through v1.2.0-beta1), and the version it reports elsewhere (-openapi,
    /health) is the source constant ServerVersion (internal/mcp/server.go), set by hand in a release
    commit and not derived from the release tag: GoReleaser's -X main.version names no variable of the
    server's main package, and v1.1.0-rc1 reported 1.1.0. So the release request implies no binary
    version. The evidence is persisted before any failure. Skipped in standalone mode."""
    req = INTEGRATION_REQUEST
    if req is None:
        pytest.skip("integration mode only")

    container = _running_container(container_name.strip())

    ev, problems = pep_integration_mode.record_identity(container, req)
    if ev is None:
        pytest.fail("; ".join(problems))
    print(f"identity evidence ({req['package_name']}): {ev}")
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_verify_bundled_files(container_name, container_type, component):
    """Verify bundled files for each MCP component match expected files

    This compares the installed files from rpm/deb with expected files
    in expected-output/rpm/ or expected-output/deb/ directory
    """
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("Invalid container or component")

    container = _running_container(container_name)

    # Get project root directory (parent of component-test/)
    project_root = Path(__file__).parent.parent

    try:
        # Call reusable verification function
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=component,
            package_name=component,
            project_root=project_root
        )

        # If verification failed, fail the test with details
        if not success:
            # Format details for display
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
        # run, where the requested release package would otherwise never be compared.
        if "No expected file found" in str(e):
            _skip_or_fail(str(e))
        pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_sbom(container_name, container_type):
    """Verify SBOM signature files located under the decoupled components SBOM directory"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not decoupled_sbom_path:
        # Integration mode fails instead: the requested package's SBOM would otherwise go unchecked.
        _skip_or_fail("DECOUPLED_COMPONENTS_SBOM not defined in env: no directory to locate the "
                      "SBOM in, so SBOM verification cannot run")

    container = _running_container(container_name)

    # The SBOM basename is not uniform across builds/platforms: it ships either
    # with the full package name (pgedge-postgres-mcp-sbom.json) or with the
    # vendor prefix stripped (postgres-mcp-sbom.json), and it may sit directly
    # under {decoupled_sbom_path} or in a per-package subdir. Locate it instead
    # of hardcoding the name and directory.
    _candidates = [
        f"{mcp_sbom_package}-sbom.json",
        f"{mcp_sbom_package.removeprefix('pgedge-')}-sbom.json",
    ]
    _find_expr = " -o ".join(f"-name '{c}'" for c in _candidates)
    _rc_find, _found = container.exec_run(
        ["bash", "-c",
         f"find {decoupled_sbom_path} -maxdepth 3 \\( {_find_expr} \\) 2>/dev/null | head -1"],
        user="root",
    )
    found_path = _found.decode().strip()
    assert found_path, (
        f"SBOM file (tried {', '.join(_candidates)}) not found anywhere under "
        f"{decoupled_sbom_path} (is {mcp_sbom_package} installed?)"
    )
    sbom_dir = os.path.dirname(found_path)
    sbom_file = os.path.basename(found_path)
    print(f"Located SBOM: {found_path}")

    if INTEGRATION_REQUEST is not None:
        # The located SBOM must be the one the requested package installed, not a file another
        # package (or an earlier install) left under the search directory.
        requested = INTEGRATION_REQUEST["package_name"]
        owners, raw = path_owners(container, container_type, found_path)
        assert owners == [requested], (
            f"{found_path} is owned by {owners or 'no package'}, not only the requested "
            f"{requested!r} (package manager said: {raw!r})")
        print(f"✅ {found_path} belongs to {requested}")

    if container_type == "rhel":
        print(f"\n--- Verifying SBOM on {container_name} (RHEL) in {sbom_dir} ---")

        # Download pgedge-rsa.pub signing key into the sbom directory
        exit_code, output = container.exec_run(
            f"wget -q -O {sbom_dir}/pgedge-rsa.pub https://dnf.pgedge.com/keys/pgedge-rsa.pub",
            user="root",
        )
        assert exit_code == 0, f"Failed to download pgedge-rsa.pub: {output.decode()}"
        print(f"✅ Downloaded pgedge-rsa.pub to {sbom_dir}")

        # Verify SBOM signature
        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"--signature-file {sbom_file}.asc "
            f"--signer-file pgedge-rsa.pub "
            f"{sbom_file}'",
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

        # Verify SBOM signature using the distro keyring
        # Detect sq signer flag (older sq uses --signer-cert, newer uses --signer-file)
        machine_prereq_setup.ensure_sq_installed(container)
        _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
        _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
        _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"
        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"{_sq_signer_flag} /etc/apt/keyrings/pgedge-rsa.gpg "
            f"{_sq_sig_flag} {sbom_file}.asc "
            f"{sbom_file}'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_mcp_component_uninstall(container_name, container_type, component):
    """Uninstall MCP component using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    container = _running_container(container_name)

    print(f"\n--- Uninstalling {component} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, component)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {component}: {str(e)}")


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

    # Use the machine_cleanup module to perform comprehensive cleanup
    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata=None, pguser=None  # MCP components don't use pgdata/pguser
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        # Display cleanup details
        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")
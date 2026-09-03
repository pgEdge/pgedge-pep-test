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
from aspects import configure_repository, package_management, machine_cleanup, machine_prereq_setup, file_management, container_management

load_dotenv()
client = docker.from_env()

# Load values from env
rhel_containers = [c.strip() for c in os.getenv("CONTAINERS", "").split(",") if c.strip()]
deb_containers = [c.strip() for c in os.getenv("DEB_CONTAINERS", "").split(",") if c.strip()]

# Combine all containers with their type
all_containers = [(c, "rhel") for c in rhel_containers] + [(c, "deb") for c in deb_containers]

# Filter containers based on platform filter (if set)
platform_filter = os.getenv("PLATFORM_FILTER", "").lower()
if platform_filter == "rpm":
    all_containers = [(c, t) for c, t in all_containers if t == "rhel"]
elif platform_filter == "deb":
    all_containers = [(c, t) for c, t in all_containers if t == "deb"]
# If platform_filter is empty or "all", use all containers

# Common configuration
repo = os.getenv("REPO", "release")
upgrade_repo = os.getenv("UPGRADE_REPO", "staging")
skip_cleanup = os.getenv("SKIP_CLEANUP", "false").lower() == "true"

# RAG Components - standalone packages (no PG version suffix)
rhel_rag_components = [c.strip() for c in os.getenv("RAG_COMPONENTS", "").split(",") if c.strip()]
deb_rag_components = [c.strip() for c in os.getenv("DEB_RAG_COMPONENTS", "").split(",") if c.strip()]

# RAG Component versions
rag_server_version = os.getenv("PGEDGE_RAG_SERVER_VERSION", "")

# Every RAG package follows the same layout: the binary is /usr/bin/<package>,
# the SBOM lives under /usr/share/<package>/, and the expected-output file is
# the package name minus the 'pgedge-' prefix. Both maps are therefore keyed off
# the configured component list rather than hardcoded names, so switching
# RAG_COMPONENTS / DEB_RAG_COMPONENTS between pgedge-rag-server and
# pgedge-rag-server2 needs no edit here.
#
# Hardcoding "pgedge-rag-server" previously meant a server2 run silently skipped
# the package-version, binary-version and binary-stripped checks — the map
# lookups missed, and each test skips on an empty value.
_rag_components_all = list(dict.fromkeys(rhel_rag_components + deb_rag_components))

# pgedge-rag-server and pgedge-rag-server2 are the same component and share
# PGEDGE_RAG_SERVER_VERSION. Give a future RAG package that versions
# independently its own env var and list it here.
_rag_version_overrides = {}

# Version mapping for RAG components
rag_version_map = {
    component: _rag_version_overrides.get(component, rag_server_version)
    for component in _rag_components_all
}

# Binary path mapping for RAG components
rag_binary_map = {
    component: f"/usr/bin/{component}"
    for component in _rag_components_all
}

# Decoupled components SBOM path
decoupled_sbom_path = os.getenv("DECOUPLED_COMPONENTS_SBOM", "")

# The SBOM directory is package-specific (/usr/share/pgedge-rag-server2/) but
# the file inside keeps the pgedge-rag-server basename even in the server2
# package — confirmed against expected-output/{rpm,deb}/rag-server2.
rag_sbom_basename = os.getenv("RAG_SBOM_BASENAME", "pgedge-rag-server-sbom.json")


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "rag_components": rhel_rag_components,
        }
    else:  # deb
        return {
            "rag_components": deb_rag_components,
        }


def get_rag_components_for_container(container_type):
    """Get list of RAG components for the container type"""
    config = get_container_config(container_type)
    return config["rag_components"]


# Generate all container-component combinations for parametrization
def generate_container_component_combinations():
    """Generate all combinations of containers and their RAG components"""
    combinations = []
    for container_name, container_type in all_containers:
        components = get_rag_components_for_container(container_type)
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

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

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
def test_rag_component_install(container_name, container_type, component):
    """Step 2: Install RAG component using package_management module"""
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

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
def test_rag_component_upgrade(container_name, container_type, component):
    """Upgrade RAG component if UPGRADE=true"""
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

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
def test_rag_component_version(container_name, container_type, component):
    """Step 3: Check the RAG component version using package_management module"""
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    # Get expected version for this component
    expected_version = rag_version_map.get(component, "")
    if not expected_version:
        pytest.skip(f"No version defined for {component} in env, skipping version check")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

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


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_verify_bundled_files(container_name, container_type, component):
    """Verify bundled files for each RAG component match expected files

    This compares the installed files from rpm/deb with expected files
    in expected-output/rpm/ or expected-output/deb/ directory
    """
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("Invalid container or component")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

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
        # Handle cases like missing expected files
        if "No expected file found" in str(e):
            pytest.skip(str(e))
        else:
            pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_verify_sbom(container_name, container_type, component):
    """Verify SBOM signature files located under the decoupled components SBOM directory

    Parametrized per component: each RAG package ships its SBOM under
    /usr/share/<package>/, so the directory has to follow the configured
    package name rather than a fixed pgedge-rag-server.
    """
    container_name = container_name.strip()
    component = component.strip()
    if not container_name or not component:
        pytest.skip("Invalid container or component")

    if not decoupled_sbom_path:
        pytest.skip("DECOUPLED_COMPONENTS_SBOM not defined in env, skipping SBOM verification")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    sbom_dir = f"{decoupled_sbom_path}/{component}"

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
            f"--signature-file {rag_sbom_basename}.asc "
            f"--signer-file pgedge-rsa.pub "
            f"{rag_sbom_basename}'",
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
            f"{_sq_sig_flag} {rag_sbom_basename}.asc "
            f"{rag_sbom_basename}'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_rag_binary_version(container_name, container_type, component):
    """Verify RAG binary -version output matches expected version"""
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    binary_path = rag_binary_map.get(component, "")
    if not binary_path:
        pytest.skip(f"No binary path defined for {component}")

    expected_version = rag_version_map.get(component, "")
    if not expected_version:
        pytest.skip(f"No version defined for {component} in env, skipping binary version check")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {component} binary version on {container_name} ({container_type}) ---")

    try:
        exit_code, output = container.exec_run(
            [binary_path, "-version"],
            user="root"
        )
        output_text = output.decode().strip()
        print(f"Binary output:\n{output_text}")

        assert exit_code == 0, f"Binary {binary_path} -version returned exit code {exit_code}: {output_text}"

        # Parse the Version line from the output
        actual_version = None
        for line in output_text.splitlines():
            if "Version:" in line:
                actual_version = line.split("Version:")[-1].strip()
                break

        assert actual_version is not None, f"Could not find 'Version:' in binary output:\n{output_text}"
        assert actual_version == expected_version, (
            f"Binary version mismatch for {component}: "
            f"expected '{expected_version}', got '{actual_version}'"
        )
        print(f"✅ Binary version matches: {actual_version}")
    except Exception as e:
        pytest.fail(f"Failed to verify {component} binary version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_rag_binary_stripped(container_name, container_type, component):
    """Verify RAG binary is stripped (debugging symbols removed)"""
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    binary_path = rag_binary_map.get(component, "")
    if not binary_path:
        pytest.skip(f"No binary path defined for {component}")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Extract directory and binary name from full path
    binary_dir = os.path.dirname(binary_path)
    binary_name = os.path.basename(binary_path)

    print(f"\n--- Verifying {component} binary is stripped on {container_name} ({container_type}) ---")
    print(f"Binary: {binary_path}")

    try:
        success, details, message = file_management.verify_binaries_stripped(
            container=container,
            binary_path=binary_dir,
            container_name=container_name,
            binary_names=binary_name
        )

        print(f"Total binaries checked: {details['total_binaries']}")
        print(f"Stripped binaries: {details['stripped_binaries']}")

        if not success:
            print(f"⚠️ Unstripped binaries found: {len(details['unstripped_binaries'])}")
            for binary in details['unstripped_binaries']:
                print(f"  - {binary}")

        assert success, f"Binary stripping verification failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to verify {component} binary is stripped: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,component", all_container_component_combinations)
def test_rag_component_uninstall(container_name, container_type, component):
    """Uninstall RAG component using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("No container or component defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

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

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")

    # Use the machine_cleanup module to perform comprehensive cleanup
    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata=None, pguser=None  # RAG components don't use pgdata/pguser
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        # Display cleanup details
        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")
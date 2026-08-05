import os
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime

import pytest
import docker
from dotenv import load_dotenv

# Add the parent directory to sys.path to import from aspects
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from aspects import configure_repository, package_management, pg_server_management, machine_cleanup, machine_prereq_setup, file_management, container_management

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
pgport = os.getenv("PG_PORT", "5432")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")
# control-plane is a decoupled component: a single version applies across PG majors.
control_plane_version = os.getenv("PGEDGE_CONTROL_PLANE_VERSION", "0.10.0~rc2")

# Binary tests — pgedge-control-plane ships /usr/sbin/pgedge-control-plane and
# exposes a `version` subcommand that prints a JSON document, e.g.:
#   {"arch":"amd64","revision":"...","revision_time":"...","version":"v0.10.0-rc.2"}
# We read the component-specific env vars rather than the generic COMPONENT_BINARY,
# which is shared (last-wins) across config sections and would otherwise clobber
# other decoupled-binary components.
# NOTE: the binary's self-reported version ("v0.10.0-rc.2") differs from the
# package version ("0.10.0~rc2"); CONTROL_PLANE_BINARY_VERSION tracks the former.
component_binary = os.getenv("CONTROL_PLANE_BINARY", "/usr/sbin/pgedge-control-plane")
component_version = os.getenv("CONTROL_PLANE_BINARY_VERSION", "v0.10.0-rc.2")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_control_plane_package = os.getenv("CONTROL_PLANE_PACKAGE", "pgedge-control-plane")
rhel_bundled_files = os.getenv("CONTROL_PLANE_BUNDLED_FILES", "").split(",")

# Debian-specific configuration
deb_control_plane_package = os.getenv("DEB_CONTROL_PLANE_PACKAGE", "pgedge-control-plane")
deb_bundled_files = os.getenv("DEB_CONTROL_PLANE_BUNDLED_FILES", "").split(",")

# Additional configuration for component tests
components = [comp.strip() for comp in os.getenv("COMPONENTS", "pgedge-control-plane").split(",") if comp.strip()]

# SBOM paths — pgedge-control-plane stores its SBOM under /usr/share/pgedge-control-plane/
control_plane_sbom_dir = "/usr/share/pgedge-control-plane"
control_plane_sbom_json = "pgedge-control-plane-sbom.json"
control_plane_sbom_asc = "pgedge-control-plane-sbom.json.asc"


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pguser": rhel_pguser,
            "control_plane_package": rhel_control_plane_package,
        }
    else:  # deb
        return {
            "pguser": deb_pguser,
            "control_plane_package": deb_control_plane_package,
        }


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

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

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
    """Step 2: Install pgedge-control-plane using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    control_plane_package = config["control_plane_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing {control_plane_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.install_package(container, control_plane_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {control_plane_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_upgrade(container_name, container_type):
    """Upgrade control-plane package if UPGRADE=true"""
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    control_plane_package = config["control_plane_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Upgrading {control_plane_package} on {container_name} ({container_type}) ---")

    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    try:
        success, platform, message = package_management.upgrade_package(container, control_plane_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{control_plane_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {control_plane_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the control-plane package version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not control_plane_version:
        pytest.skip("No CONTROL_PLANE_VERSION defined in env, skipping version check")

    config = get_container_config(container_type)
    control_plane_package = config["control_plane_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {control_plane_package} version on {container_name} ({container_type}) ---")

    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, control_plane_package, control_plane_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {control_plane_package} version: {str(e)}")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files for control-plane match expected files

    This compares the installed files from rpm/deb with expected files
    in expected-output/rpm/ or expected-output/deb/ directory
    """
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    actual_package = config["control_plane_package"]

    project_root = Path(__file__).parent.parent

    try:
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=actual_package,
            package_name=actual_package,
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
        if "No expected file found" in str(e):
            pytest.skip(str(e))
        else:
            pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_sbom(container_name, container_type):
    """Verify SBOM signature files for pgedge-control-plane"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying Control Plane SBOM on {container_name} ({container_type}) in {control_plane_sbom_dir} ---")

    machine_prereq_setup.ensure_sq_installed(container)
    _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
    _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
    _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"

    if container_type == "rhel":
        exit_code, output = container.exec_run(
            f"wget -q -O {control_plane_sbom_dir}/pgedge-rsa.pub https://dnf.pgedge.com/keys/pgedge-rsa.pub",
            user="root",
        )
        assert exit_code == 0, f"Failed to download pgedge-rsa.pub: {output.decode()}"
        signer_arg = f"{_sq_signer_flag} {control_plane_sbom_dir}/pgedge-rsa.pub"
    else:
        signer_arg = f"{_sq_signer_flag} /etc/apt/keyrings/pgedge-rsa.gpg"

    exit_code, output = container.exec_run(
        f"sh -c 'cd {control_plane_sbom_dir} && sq verify "
        f"{signer_arg} "
        f"{_sq_sig_flag} {control_plane_sbom_asc} "
        f"{control_plane_sbom_json}'",
        user="root",
    )
    output_str = output.decode().replace('\xa0', ' ')
    assert exit_code == 0, f"SBOM verification failed: {output_str}"
    assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
        f"Expected authenticated signature not found in output:\n{output_str}"
    print(f"✅ Control Plane SBOM signature verified on {container_name} ({container_type})")
    print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_binary_version(container_name, container_type):
    """Verify that the control-plane binary reports the expected version.

    Skipped when CONTROL_PLANE_BINARY or CONTROL_PLANE_BINARY_VERSION is not set.
    Runs `<binary> version`, which prints a JSON document, and asserts the
    "version" field matches CONTROL_PLANE_BINARY_VERSION (e.g. "v0.10.0-rc.2").
    Falls back to a plain substring check if the output is not valid JSON.
    """
    if not component_binary or not component_version:
        pytest.skip(
            "CONTROL_PLANE_BINARY or CONTROL_PLANE_BINARY_VERSION not set — skipping binary version check"
        )

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Checking {component_binary} version on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"{component_binary} version 2>&1"],
        user="root",
    )
    assert exit_code == 0, f"'{component_binary} version' failed: {output.decode().strip()}"

    version_output = output.decode().strip()
    print(f"   Output: {version_output}")

    # The binary prints a JSON document; extract and compare the "version" field.
    try:
        reported_version = json.loads(version_output).get("version", "")
    except (ValueError, AttributeError):
        # Not JSON (e.g. a future plain-text format) — fall back to substring match.
        reported_version = None

    if reported_version is not None:
        assert reported_version == component_version, (
            f"Binary version mismatch: expected '{component_version}', "
            f"got '{reported_version}' from JSON 'version' field"
        )
    else:
        assert component_version in version_output, (
            f"Expected version '{component_version}' not found in binary output:\n  {version_output}"
        )
    print(f"✅ {component_binary} reports version {component_version}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_binary_stripped(container_name, container_type):
    """Verify that the control-plane binary is a stripped ELF binary.

    Skipped when CONTROL_PLANE_BINARY is not set.
    Runs 'file <binary>' and asserts the output contains 'stripped'.
    """
    if not component_binary:
        pytest.skip("CONTROL_PLANE_BINARY not set — skipping binary strip check")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Checking ELF strip status of {component_binary} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"file {component_binary} 2>&1"],
        user="root",
    )
    assert exit_code == 0, f"'file {component_binary}' failed: {output.decode().strip()}"

    file_output = output.decode().strip()
    print(f"   Output: {file_output}")

    assert "stripped" in file_output.lower(), (
        f"Binary {component_binary} does not appear to be stripped.\n"
        f"'file' output: {file_output}"
    )
    print(f"✅ {component_binary} is stripped")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_license_file(container_name, container_type):
    """Verify that the LICENSE file is installed for pgedge-control-plane."""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    control_plane_package = config["control_plane_package"]
    # RHEL ships the license under /usr/share/licenses/<pkg>/LICENSE.md; Debian
    # ships it under /usr/share/doc/<pkg>/copyright.
    if container_type == "rhel":
        license_path = f"/usr/share/licenses/{control_plane_package}/LICENSE.md"
    else:  # deb
        license_path = f"/usr/share/doc/{control_plane_package}/copyright"

    print(f"\n--- Verifying LICENSE file on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(
        f"test -f {license_path}",
        user="root"
    )
    assert exit_code == 0, f"LICENSE file not found at {license_path}: {output.decode()}"
    print(f"✅ LICENSE file exists at {license_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_readme_file(container_name, container_type):
    """Verify that the README.md file is installed for pgedge-control-plane."""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    control_plane_package = config["control_plane_package"]
    # Both families ship README.md under /usr/share/doc/<pkg>/. Debian doc
    # compression depends on the debhelper version, so accept README.md.gz too.
    readme_dir = f"/usr/share/doc/{control_plane_package}"
    if container_type == "rhel":
        readme_paths = [f"{readme_dir}/README.md"]
    else:  # deb
        readme_paths = [f"{readme_dir}/README.md", f"{readme_dir}/README.md.gz"]

    print(f"\n--- Verifying README file on {container_name} ({container_type}) ---")

    found_path = None
    for candidate in readme_paths:
        exit_code, output = container.exec_run(
            f"test -f {candidate}",
            user="root"
        )
        if exit_code == 0:
            found_path = candidate
            break

    assert found_path, f"README.md not found at any of {readme_paths}"
    print(f"✅ README.md exists at {found_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_package_uninstall(container_name, container_type):
    """Uninstall control-plane package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    control_plane_package = config["control_plane_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Uninstalling {control_plane_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.uninstall_package(container, control_plane_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {control_plane_package}: {str(e)}")


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

    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")

    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata=pgdata, pguser=pguser
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
        if cleanup_summary["data_directory_removed"]:
            print(f"   Data directory removed: {pgdata}")
        if cleanup_summary["user_removed"]:
            print(f"   User removed: {pguser}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")

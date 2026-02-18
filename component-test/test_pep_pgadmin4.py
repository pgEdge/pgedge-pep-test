import os
import sys
from pathlib import Path

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
pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")
pgadmin4_version = os.getenv("PGEDGE_PGADMIN4_VERSION", "9.8")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# pgAdmin4 configuration
pgadmin4_package = os.getenv("PGADMIN_PACKAGES", "pgedge-pgadmin4")
pgadmin4_install_dir = "/usr/pgadmin4"
pgadmin4_bin = f"{pgadmin4_install_dir}/bin/pgadmin4"
pgadmin4_setup_web = f"{pgadmin4_install_dir}/bin/setup-web.sh"

# Additional configuration for component tests
components = [comp.strip() for comp in os.getenv("COMPONENTS", "pgedge-pgadmin4").split(",") if comp.strip()]


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pguser": rhel_pguser,
            "pgadmin4_package": pgadmin4_package,
        }
    else:  # deb
        return {
            "pguser": deb_pguser,
            "pgadmin4_package": pgadmin4_package,
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


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_install(container_name, container_type):
    """Step 2: Install pgedge-pgadmin4 using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pkg = config["pgadmin4_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing {pkg} on {container_name} ({container_type}) ---")

    # Use the package_management module to install the package
    try:
        success, platform, message = package_management.install_package(container, pkg)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {pkg}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not pgadmin4_version:
        pytest.skip("No PGEDGE_PGADMIN4_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pkg = config["pgadmin4_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {pkg} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, pkg, pgadmin4_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {pkg} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files for each component match expected files

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

    # Get the actual package name for the platform
    config = get_container_config(container_type)
    actual_package = config["pgadmin4_package"]

    # Get project root directory (parent of component-test/)
    project_root = Path(__file__).parent.parent

    try:
        # Call reusable verification function
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=actual_package,
            package_name=actual_package,
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


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgadmin4_binary_exists(container_name, container_type):
    """Step 5: Verify pgAdmin4 binary exists and is executable"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying pgAdmin4 binary on {container_name} ({container_type}) ---")

    # Verify pgadmin4 binary exists
    exit_code, output = container.exec_run(
        f"test -f {pgadmin4_bin}",
        user="root"
    )
    assert exit_code == 0, f"pgAdmin4 binary not found at {pgadmin4_bin}"
    print(f"✅ pgAdmin4 binary found at {pgadmin4_bin}")

    # Verify setup-web.sh exists
    exit_code, output = container.exec_run(
        f"test -f {pgadmin4_setup_web}",
        user="root"
    )
    assert exit_code == 0, f"setup-web.sh not found at {pgadmin4_setup_web}"
    print(f"✅ setup-web.sh found at {pgadmin4_setup_web}")

    # Verify install directory exists
    exit_code, output = container.exec_run(
        f"test -d {pgadmin4_install_dir}",
        user="root"
    )
    assert exit_code == 0, f"pgAdmin4 install directory not found at {pgadmin4_install_dir}"
    print(f"✅ pgAdmin4 install directory exists at {pgadmin4_install_dir}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgadmin4_web_config_exists(container_name, container_type):
    """Step 6: Verify platform-specific web server config file exists"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying pgAdmin4 web config on {container_name} ({container_type}) ---")

    # Platform-specific config file paths
    if container_type == "rhel":
        config_path = "/etc/httpd/conf.d/pgadmin4.conf"
    else:  # deb
        config_path = "/etc/apache2/conf-available/pgadmin4.conf"

    exit_code, output = container.exec_run(
        f"test -f {config_path}",
        user="root"
    )
    assert exit_code == 0, f"pgAdmin4 web config not found at {config_path}"
    print(f"✅ pgAdmin4 web config found at {config_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgadmin4_license_exists(container_name, container_type):
    """Step 7: Verify LICENSE file exists in install directory"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying pgAdmin4 LICENSE on {container_name} ({container_type}) ---")

    license_path = f"{pgadmin4_install_dir}/LICENSE"
    exit_code, output = container.exec_run(
        f"test -f {license_path}",
        user="root"
    )
    assert exit_code == 0, f"LICENSE file not found at {license_path}"
    print(f"✅ LICENSE file found at {license_path}")

    # Verify SBOM files exist
    for sbom_file in ["sbom-server.json", "sbom-server.json.asc", "sbom-web.json", "sbom-web.json.asc"]:
        sbom_path = f"{pgadmin4_install_dir}/{sbom_file}"
        exit_code, output = container.exec_run(
            f"test -f {sbom_path}",
            user="root"
        )
        assert exit_code == 0, f"SBOM file not found at {sbom_path}"
        print(f"✅ {sbom_file} found at {sbom_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_package_uninstall(container_name, container_type):
    """Uninstall pgadmin4 package using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pkg = config["pgadmin4_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Uninstalling {pkg} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, pkg)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {pkg}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Full cleanup using machine_cleanup module: remove all pgedge packages + leftover data"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pguser_val = config["pguser"]

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")

    # Use the machine_cleanup module to perform comprehensive cleanup
    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata="/tmp/n1", pguser=pguser_val
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        # Display cleanup details
        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
        if cleanup_summary["data_directory_removed"]:
            print(f"   Data directory removed")
        if cleanup_summary["user_removed"]:
            print(f"   User removed: {pguser_val}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")
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

# Load environment from configuration file
config_file = os.path.join(os.path.dirname(__file__), '..', 'configuration', 'config16.env')
load_dotenv(config_file)
load_dotenv()  # Also load .env if present (can override)
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
ace_version = os.getenv("PGEDGE_ACE_VERSION", "")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_ace_package = os.getenv("ACE_PACKAGE", "pgedge-ace")
rhel_bundled_files = os.getenv(
    "ACE_BUNDLED_FILES",
    "/usr/bin/ace"
).split(",")

# Debian-specific configuration
deb_ace_package = os.getenv("DEB_ACE_PACKAGE", "pgedge-ace")
deb_bundled_files = os.getenv(
    "DEB_ACE_BUNDLED_FILES",
    "/usr/bin/ace"
).split(",")

# Server package configuration
pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")
rhel_server_package = os.getenv("SERVER_PACKAGE", f"pgedge-postgresql{pg_major_version}-server,pgedge-postgresql{pg_major_version}-contrib")
deb_server_package = os.getenv("DEB_SERVER_PACKAGE", f"pgedge-postgresql-{pg_major_version}")

# PostgreSQL binary paths
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")

# Cluster configuration for ACE
ace_no_of_clusters = int(os.getenv("ACE_NO_OF_CLUSTERS", "2"))
base_port = int(os.getenv("BASE_PORT", "5431"))
base_data_dir = os.getenv("PG_DATA_DIR", "/tmp/n1")

# Generate cluster configurations: list of (cluster_index, port, data_dir)
cluster_configs = [
    (i + 1, base_port + i, f"{base_data_dir.rstrip('0123456789')}{i + 1}")
    for i in range(ace_no_of_clusters)
]


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pguser": rhel_pguser,
            "pgbin": rhel_pgbin.rstrip('/'),
            "ace_package": rhel_ace_package,
            "server_package": rhel_server_package,
            "bundled_files": rhel_bundled_files
        }
    else:  # deb
        return {
            "pguser": deb_pguser,
            "pgbin": deb_pgbin.rstrip('/'),
            "ace_package": deb_ace_package,
            "server_package": deb_server_package,
            "bundled_files": deb_bundled_files
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
    """Step 2: Install pgedge-ace using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    ace_package = config["ace_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing {ace_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to install the package
    try:
        success, platform, message = package_management.install_package(container, ace_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {ace_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not ace_version:
        pytest.skip("No PGEDGE_ACE_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    ace_package = config["ace_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {ace_package} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, ace_package, ace_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {ace_package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files for ace match expected files

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
    actual_package = config["ace_package"]

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
def test_ace_help(container_name, container_type):
    """Test that ace --help command works correctly"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Testing ace --help on {container_name} ({container_type}) ---")

    # Run ace --help command
    exit_code, output = container.exec_run("/usr/bin/ace --help")
    output_str = output.decode() if output else ""

    print(f"Output:\n{output_str}")

    assert exit_code == 0, f"ace --help failed with exit code {exit_code}: {output_str}"
    assert len(output_str) > 0, "ace --help returned empty output"
    print(f"✅ ace --help executed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_install_server(container_name, container_type):
    """Install PostgreSQL server package for ACE cluster testing"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    server_package = config["server_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing server packages on {container_name} ({container_type}) ---")

    # Install each server package (may be comma-separated)
    for package in server_package.split(","):
        package = package.strip()
        if not package:
            continue
        try:
            success, platform, message = package_management.install_package(container, package)
            assert success, f"Package installation failed for {package}: {message}"
            print(f"✅ {message}")
        except Exception as e:
            pytest.fail(f"Failed to install {package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
@pytest.mark.parametrize("cluster_idx,cluster_port,cluster_data_dir", cluster_configs)
def test_init_cluster(container_name, container_type, cluster_idx, cluster_port, cluster_data_dir):
    """Initialize PostgreSQL cluster for ACE testing"""
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
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Initializing cluster {cluster_idx} on {container_name} ---")
    print(f"    Data directory: {cluster_data_dir}")
    print(f"    Port: {cluster_port}")

    # Use the pg_server_management module to initialize cluster
    try:
        success, config_content, message = pg_server_management.init_cluster(
            container, pgbin, cluster_data_dir, pguser
        )
        assert success, f"Cluster {cluster_idx} initialization failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to initialize cluster {cluster_idx}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
@pytest.mark.parametrize("cluster_idx,cluster_port,cluster_data_dir", cluster_configs)
def test_start_cluster(container_name, container_type, cluster_idx, cluster_port, cluster_data_dir):
    """Start PostgreSQL cluster for ACE testing"""
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
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Starting cluster {cluster_idx} on {container_name} ---")
    print(f"    Data directory: {cluster_data_dir}")
    print(f"    Port: {cluster_port}")

    # Use the pg_server_management module to start the server
    try:
        success, server_output, message = pg_server_management.start_server(
            container, pgbin, cluster_data_dir, str(cluster_port), pguser
        )
        assert success, f"Cluster {cluster_idx} start failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to start cluster {cluster_idx}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
@pytest.mark.parametrize("cluster_idx,cluster_port,cluster_data_dir", cluster_configs)
def test_check_cluster_connection(container_name, container_type, cluster_idx, cluster_port, cluster_data_dir):
    """Check PostgreSQL connection for each cluster"""
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
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Checking connection to cluster {cluster_idx} on {container_name} ---")
    print(f"    Port: {cluster_port}")

    # Use the pg_server_management module to check connection
    try:
        success, version_output, message = pg_server_management.check_connection(
            container, pgbin, str(cluster_port), pguser
        )
        assert success, f"Cluster {cluster_idx} connection check failed: {message}"
        print(f"✅ Cluster {cluster_idx}: {message}")
        print(f"    Version: {version_output.strip().split(chr(10))[2] if version_output else 'N/A'}")
    except Exception as e:
        pytest.fail(f"Failed to check connection to cluster {cluster_idx}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
@pytest.mark.parametrize("cluster_idx,cluster_port,cluster_data_dir", cluster_configs)
def test_stop_cluster(container_name, container_type, cluster_idx, cluster_port, cluster_data_dir):
    """Stop PostgreSQL cluster"""
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
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Stopping cluster {cluster_idx} on {container_name} ---")

    # Use the pg_server_management module to stop the server
    try:
        success, server_output, message = pg_server_management.stop_server(
            container, pgbin, cluster_data_dir, str(cluster_port), pguser
        )
        assert success, f"Cluster {cluster_idx} stop failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to stop cluster {cluster_idx}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_package_uninstall(container_name, container_type):
    """Uninstall ace package using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    ace_package = config["ace_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Uninstalling {ace_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, ace_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {ace_package}: {str(e)}")


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
    pguser = config["pguser"]

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")

    # Use the machine_cleanup module to perform comprehensive cleanup
    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pguser=pguser
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        # Display cleanup details
        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
        if cleanup_summary["user_removed"]:
            print(f"   User removed: {pguser}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")

"""pgmq component tests.

pgmq is a coupled component (tied to a PostgreSQL major version) that ships a
pure-SQL message-queue extension:

  * Package  — pgedge-pgmq_{PG} (RHEL) / pgedge-postgresql-{PG}-pgmq (Debian)
  * Extension — pgmq
  * SBOM     — <pg_path>/sbom/pgmq-sbom.json (+ .asc detached signature)
  * LICENSE  — /usr/share/licenses/<pkg>/LICENSE (RHEL)
               /usr/share/doc/<pkg>/LICENSE.md.gz (Debian)
  * README   — <rhel_pg_path>/doc/extension/README-pgmq.md (RHEL)
               /usr/share/doc/<pkg>/README.md.gz (Debian)

No standalone binary and no shared library are shipped, so there are no binary
tests and pgmq needs no shared_preload_libraries entry.
"""

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
pgmq_version = os.getenv(f"PGEDGE_PGMQ_{pg_major_version}_VERSION", "1.12.0")

# Extension version as reported by \dx — the .control file's default_version,
# which need not track the package version.
pgmq_extension_version = os.getenv("PGMQ_EXTENSION_VERSION", pgmq_version)

# SBOM basename, found under <pg_path>/sbom/
pgmq_sbom = os.getenv("PGMQ_SBOM", "pgmq-sbom.json")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")
# Coupled component — package name carries the PostgreSQL major version suffix.
rhel_pgmq_package = os.getenv("PGMQ_PACKAGE", f"pgedge-pgmq_{pg_major_version}")
rhel_bundled_files = [f for f in os.getenv("PGMQ_BUNDLED_FILES", "").split(",") if f]

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_pgmq_package = os.getenv("DEB_PGMQ_PACKAGE", f"pgedge-postgresql-{pg_major_version}-pgmq")
deb_bundled_files = [f for f in os.getenv("DEB_PGMQ_BUNDLED_FILES", "").split(",") if f]

# Additional configuration for extension tests
check_extensions = os.getenv("CHECK_EXTENSIONS", "true").lower() == "true"
base_extensions = [ext.strip() for ext in os.getenv("PGMQ_EXTENSIONS", "pgmq").split(",") if ext.strip()]


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "pg_path": rhel_pg_path.rstrip('/'),
            "pgmq_package": rhel_pgmq_package,
            "bundled_files": rhel_bundled_files
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "pg_path": deb_pg_path.rstrip('/'),
            "pgmq_package": deb_pgmq_package,
            "bundled_files": deb_bundled_files
        }


def _get_container(container_name):
    """Fetch a running container, skipping the test when it is unavailable"""
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

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

    container = _get_container(container_name)

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
    """Step 2: Install the pgmq package using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgmq_package = config["pgmq_package"]

    container = _get_container(container_name)

    print(f"\n--- Installing {pgmq_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to install the package
    try:
        success, platform, message = package_management.install_package(container, pgmq_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {pgmq_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_upgrade(container_name, container_type):
    """Upgrade the pgmq package if UPGRADE=true"""
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgmq_package = config["pgmq_package"]

    container = _get_container(container_name)

    print(f"\n--- Upgrading {pgmq_package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    # Use the package_management module to upgrade the package
    try:
        success, platform, message = package_management.upgrade_package(container, pgmq_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{pgmq_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {pgmq_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not pgmq_version:
        pytest.skip(f"No PGEDGE_PGMQ_{pg_major_version}_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgmq_package = config["pgmq_package"]

    container = _get_container(container_name)

    print(f"\n--- Verifying {pgmq_package} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, pgmq_package, pgmq_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {pgmq_package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files for pgmq match expected files

    This compares the installed files from rpm/deb with expected files
    in expected-output/rpm/pgmq or expected-output/deb/pgmq
    """
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)

    # Get the actual package name for the platform
    config = get_container_config(container_type)
    actual_package = config["pgmq_package"]

    # Get project root directory (parent of component-test/)
    project_root = Path(__file__).parent.parent

    try:
        # Call reusable verification function
        # Use actual_package for both component and package_name to ensure
        # correct expected file lookup based on the platform
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
def test_verify_sbom(container_name, container_type):
    """Verify the pgmq SBOM's detached signature.

    The package ships pgmq-sbom.json plus a .asc signature under the
    PostgreSQL tree's sbom/ directory.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    sbom_dir = f"{config['pg_path']}/sbom"

    print(f"\n--- Verifying SBOM {sbom_dir}/{pgmq_sbom} on {container_name} ({container_type}) ---")

    machine_prereq_setup.ensure_sq_installed(container)
    _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
    _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
    _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"

    if container_type == "rhel":
        exit_code, output = container.exec_run(
            f"wget -q -O {sbom_dir}/pgedge-rsa.pub https://dnf.pgedge.com/keys/pgedge-rsa.pub",
            user="root",
        )
        assert exit_code == 0, f"Failed to download pgedge-rsa.pub: {output.decode()}"
        signer = f"{sbom_dir}/pgedge-rsa.pub"
    else:
        # Debian containers already carry the key in the apt keyring
        signer = "/etc/apt/keyrings/pgedge-rsa.gpg"

    exit_code, output = container.exec_run(
        f"sh -c 'cd {sbom_dir} && sq verify "
        f"{_sq_signer_flag} {signer} "
        f"{_sq_sig_flag} {pgmq_sbom}.asc "
        f"{pgmq_sbom}'",
        user="root",
    )
    output_str = output.decode().replace('\xa0', ' ')
    assert exit_code == 0, f"SBOM verification failed: {output_str}"
    assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
        f"Expected a good/authenticated signature, got:\n{output_str}"
    print(f"✅ SBOM signature verified on {container_name} ({container_type})")
    print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_license_file(container_name, container_type):
    """Verify the bundled license file is installed.

    RHEL ships /usr/share/licenses/<pkg>/LICENSE; Debian ships it gzipped as
    /usr/share/doc/<pkg>/LICENSE.md.gz.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    package = config["pgmq_package"]

    if container_type == "rhel":
        license_path = f"/usr/share/licenses/{package}/LICENSE"
    else:  # deb
        license_path = f"/usr/share/doc/{package}/LICENSE.md.gz"

    print(f"\n--- Verifying license file {license_path} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(["test", "-f", license_path], user="root")
    assert exit_code == 0, f"License file not found at {license_path}"
    print(f"✅ License file present at {license_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_readme_file(container_name, container_type):
    """Verify the bundled README file is installed.

    RHEL ships it inside the PostgreSQL tree as
    <pg_path>/doc/extension/README-pgmq.md; Debian ships it gzipped as
    /usr/share/doc/<pkg>/README.md.gz.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    package = config["pgmq_package"]

    if container_type == "rhel":
        readme_path = f"{config['pg_path']}/doc/extension/README-pgmq.md"
    else:  # deb
        readme_path = f"/usr/share/doc/{package}/README.md.gz"

    print(f"\n--- Verifying README file {readme_path} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(["test", "-f", readme_path], user="root")
    assert exit_code == 0, f"README file not found at {readme_path}"
    print(f"✅ README file present at {readme_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_init_cluster(container_name, container_type):
    """Initialize a PostgreSQL cluster for the pgmq tests.

    pgmq is a pure SQL extension, so no shared_preload_libraries entry is
    needed — only the logical-replication GUCs the harness sets everywhere.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Initializing cluster on {container_name} ---")

    guc_parameters = {
        "wal_level": "logical",
        "max_replication_slots": "10",
        "max_wal_senders": "10"
    }

    # Use the pg_server_management module to initialize cluster
    try:
        success, config_content, message = pg_server_management.init_cluster(
            container, pgbin, pgdata, pguser, guc_parameters
        )
        assert success, f"Cluster initialization failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to initialize cluster: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_start_server(container_name, container_type):
    """Start PostgreSQL server using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Starting PostgreSQL server on {container_name} ---")

    # Use the pg_server_management module to start the server
    try:
        success, server_output, message = pg_server_management.start_server(
            container, pgbin, pgdata, pgport, pguser
        )
        assert success, f"Server start failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to start PostgreSQL server: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_check_connection(container_name, container_type):
    """Check PostgreSQL connection using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Checking PostgreSQL connection on {container_name} ---")

    # Use the pg_server_management module to check connection
    try:
        success, version_output, message = pg_server_management.check_connection(
            container, pgbin, pgport, pguser
        )
        assert success, f"Connection check failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to check PostgreSQL connection: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
@pytest.mark.parametrize("extension", base_extensions)
def test_create_extensions(container_name, container_type, extension):
    """Create each extension individually with separate test results

    This creates a separate test for each container-extension combination
    """
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    extension = extension.strip()

    if not container_name or not extension:
        pytest.skip("Invalid container or extension")

    container = _get_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    # Normalize extension (quote if it contains a dash)
    normalized_ext = f'"{extension}"' if "-" in extension else extension

    print(f"\n--- Creating extension {normalized_ext} in {container_name} ---")

    # Create the extension
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
        f"-c 'CREATE EXTENSION IF NOT EXISTS {normalized_ext} CASCADE;'",
        user=pguser,
    )

    assert exit_code == 0, f"Failed to create {normalized_ext}: {output.decode()}"
    print(f"✅ Successfully created extension {normalized_ext}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
@pytest.mark.parametrize("extension", base_extensions)
def test_extension_version(container_name, container_type, extension):
    """Verify the installed extension version matches the expected version via \\dx in psql.

    Runs psql \\dx and greps for the extension name, then checks that the
    reported version column matches pgmq_extension_version.
    Skipped when extension check is disabled.
    """
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    extension = extension.strip()

    if not container_name or not extension:
        pytest.skip("Invalid container or extension")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Verifying extension version for '{extension}' on {container_name} ---")

    exit_code, output = container.exec_run(
        ["bash", "-c",
         f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -c '\\dx' | grep '{extension}'"],
        user=pguser,
    )
    assert exit_code == 0, (
        f"Failed to query extension '{extension}' version via \\dx: {output.decode().strip()}"
    )

    ext_line = output.decode().strip()
    print(f"   \\dx grep output: {ext_line}")

    assert ext_line, f"Extension '{extension}' not found in \\dx output"

    # Parse version from the table row: " name | version | schema | description "
    columns = [col.strip() for col in ext_line.split("|")]
    assert len(columns) >= 2, f"Unexpected \\dx row format: {ext_line}"
    installed_version = columns[1].strip()

    assert pgmq_extension_version in installed_version, (
        f"Extension '{extension}' version mismatch: "
        f"expected '{pgmq_extension_version}', got '{installed_version}'"
    )
    print(f"✅ Extension '{extension}' version {installed_version} matches expected {pgmq_extension_version}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_stop_server(container_name, container_type):
    """Stop PostgreSQL server using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Stopping PostgreSQL server on {container_name} ---")

    # Use the pg_server_management module to stop the server
    try:
        success, server_output, message = pg_server_management.stop_server(
            container, pgbin, pgdata, pgport, pguser
        )
        assert success, f"Server stop failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to stop PostgreSQL server: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_package_uninstall(container_name, container_type):
    """Uninstall the pgmq package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgmq_package = config["pgmq_package"]

    container = _get_container(container_name)

    print(f"\n--- Uninstalling {pgmq_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, pgmq_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {pgmq_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Full cleanup using machine_cleanup module: remove all pgedge packages + leftover data"""
    if skip_cleanup:
        pytest.skip("Skipping cleanup: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")

    # Use the machine_cleanup module to perform comprehensive cleanup
    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata=pgdata, pguser=pguser
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        # Display cleanup details
        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
        if cleanup_summary["data_directory_removed"]:
            print(f"   Data directory removed: {pgdata}")
        if cleanup_summary["user_removed"]:
            print(f"   User removed: {pguser}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")

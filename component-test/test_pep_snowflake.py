import os
import sys
import subprocess
from pathlib import Path

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
snowflake_node = os.getenv("SNOWFLAKE_NODE", "1")
pgport = os.getenv("PG_PORT", "5432")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "17")
snowflake_version = os.getenv(f"PGEDGE_SNOWFLAKE_{pg_major_version}_VERSION", "2.4")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")
rhel_snowflake_package = os.getenv("SNOWFLAKE_PACKAGE", f"pgedge-snowflake_{pg_major_version}")
rhel_snowflake_lib = os.getenv("RHEL_SNOWFLAKE_LIB", f"/usr/pgsql-{pg_major_version}/lib")
rhel_bundled_files = os.getenv(
    "SNOWFLAKE_BUNDLED_FILES",
    f"/usr/pgsql-{pg_major_version}/lib/snowflake.so,/usr/pgsql-{pg_major_version}/sbom/snowflake-sbom.json,/usr/pgsql-{pg_major_version}/sbom/snowflake-sbom.json.asc,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.0--1.1.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.0.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.1--1.2.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.1.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.2--2.0.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.2.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--2.0--2.2.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--2.0.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--2.2.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake.control,/usr/share/doc/pgedge-snowflake_{pg_major_version}/README.md,/usr/share/licenses/pgedge-snowflake_{pg_major_version}/LICENSE.md"
).split(",")

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_snowflake_package = os.getenv("DEB_SNOWFLAKE_PACKAGE", f"pgedge-postgresql-{pg_major_version}-snowflake")
deb_snowflake_lib = os.getenv("DEB_SNOWFLAKE_LIB", f"/usr/lib/postgresql/{pg_major_version}/lib")
deb_bundled_files = os.getenv(
    "DEB_SNOWFLAKE_BUNDLED_FILES",
    f"{deb_pg_path}/lib/snowflake.so,{deb_pg_share_path}/extension/snowflake--1.0--1.1.sql,{deb_pg_share_path}/extension/snowflake--1.0.sql,{deb_pg_share_path}/extension/snowflake--1.1--1.2.sql,{deb_pg_share_path}/extension/snowflake--1.1.sql,{deb_pg_share_path}/extension/snowflake--1.2--2.0.sql,{deb_pg_share_path}/extension/snowflake--1.2.sql,{deb_pg_share_path}/extension/snowflake--2.0--2.2.sql,{deb_pg_share_path}/extension/snowflake--2.0.sql,{deb_pg_share_path}/extension/snowflake--2.2.sql,{deb_pg_share_path}/extension/snowflake.control"
).split(",")

# Snowflake stripped library names (comma-separated list)
snowflake_stripped_lib = os.getenv("SNOWFLAKE_STRIPPED_LIB", "snowflake.so")


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "snowflake_package": rhel_snowflake_package,
            "snowflake_lib": rhel_snowflake_lib,
            "bundled_files": rhel_bundled_files
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "snowflake_package": deb_snowflake_package,
            "snowflake_lib": deb_snowflake_lib,
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
    """Step 2: Install pgedge-snowflake using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    snowflake_package = config["snowflake_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing {snowflake_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to install the package
    try:
        success, platform, message = package_management.install_package(container, snowflake_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {snowflake_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_upgrade(container_name, container_type):
    """Upgrade component package if UPGRADE=true"""
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    snowflake_package = config["snowflake_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Upgrading {snowflake_package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    # Use the package_management module to upgrade the package
    try:
        success, platform, message = package_management.upgrade_package(container, snowflake_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{snowflake_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {snowflake_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not snowflake_version:
        pytest.skip("No SNOWFLAKE_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    snowflake_package = config["snowflake_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {snowflake_package} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, snowflake_package, snowflake_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {snowflake_package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_binaries_stripped(container_name, container_type):
    """Step 4: Verify that Snowflake libraries are stripped using file_management module"""
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
    snowflake_lib_dir = config.get("snowflake_lib", "")

    if not snowflake_lib_dir:
        pytest.skip(f"No snowflake library directory configured for {container_type}")

    print(f"\n--- Verifying Snowflake libraries are stripped in {snowflake_lib_dir} on {container_name} ({container_type}) ---")

    # Use file_management module to verify binaries are stripped
    try:
        success, details, message = file_management.verify_binaries_stripped(
            container=container,
            binary_path=snowflake_lib_dir.rstrip('/'),
            container_name=container_name,
            binary_names=snowflake_stripped_lib
        )

        # If verification failed, fail the test with details
        if not success:
            pytest.fail(f"Failed: {message}\n{details}")

        print(f"✅ {message}")
        if details:
            print(f"   Details: {details}")

    except Exception as e:
        pytest.fail(f"Failed to verify binaries are stripped: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_sbom(container_name, container_type):
    """Verify SBOM signature files located under the PostgreSQL path sbom directory"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    if container_type == "rhel":
        sbom_dir = f"{rhel_pg_path}/sbom"

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
            f"--signature-file snowflake-sbom.json.asc "
            f"--signer-file pgedge-rsa.pub "
            f"snowflake-sbom.json'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 authenticated signature." in output_str, \
            f"Expected '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (RHEL)")
        print(f"   {output_str.strip()}")

    else:  # deb
        sbom_dir = f"{deb_pg_path}/sbom"

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
            f"{_sq_sig_flag} snowflake-sbom.json.asc "
            f"snowflake-sbom.json'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files for the snowflake component match expected files

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

    # Get container-specific configuration
    config = get_container_config(container_type)
    snowflake_package = config["snowflake_package"]

    # Get project root directory (parent of component-test/)
    project_root = Path(__file__).parent.parent

    try:
        # Call reusable verification function
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=snowflake_package,  # Use package name as component name
            package_name=snowflake_package,
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
def test_init_cluster(container_name, container_type):
    """Initialize PostgreSQL cluster with Snowflake-specific GUC parameters using pg_server_management module"""
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

    print(f"\n--- Initializing cluster on {container_name} ---")

    # Define Snowflake-specific GUC parameters
    guc_parameters = {
        "shared_preload_libraries": "'snowflake'",
        "track_commit_timestamp": "on",
        "snowflake.node": "1"
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

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

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

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

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
def test_snowflake_extension_loaded(container_name, container_type):
    """Verify Snowflake extension is loaded via shared_preload_libraries"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Checking if Snowflake extension is loaded on {container_name} ---")

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SHOW shared_preload_libraries;")
    assert exit_code == 0, f"Failed to check shared_preload_libraries: {output}"
    assert "snowflake" in output, f"Snowflake not in shared_preload_libraries: {output}"
    print(f"✅ Snowflake extension is loaded in shared_preload_libraries")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_create_extension(container_name, container_type):
    """Create Snowflake extension in the database"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Creating Snowflake extension on {container_name} ---")

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "CREATE EXTENSION IF NOT EXISTS snowflake;")
    assert exit_code == 0, f"Failed to create Snowflake extension: {output}"
    print(f"✅ Snowflake extension created successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_node_parameter(container_name, container_type):
    """Test Case 1: Verify snowflake.node parameter"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 1: Checking snowflake.node parameter on {container_name} ---")

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SHOW snowflake.node;")

    assert exit_code == 0, f"Failed to show snowflake.node: {output}"
    assert snowflake_node in output, f"Expected snowflake.node={snowflake_node}, got: {output}"

    print(f"Query output:\n{output}")
    print(f"✅ snowflake.node is correctly set to {snowflake_node}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_format_function(container_name, container_type):
    """Test Case 2: Test snowflake.format() function"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 2: Testing snowflake.format() function on {container_name} ---")

    # Test with the sample ID
    test_id = "136169504773242881"
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, f"SELECT * FROM snowflake.format({test_id});")

    assert exit_code == 0, f"Failed to execute snowflake.format(): {output}"

    # Verify output contains expected JSON structure
    assert "id" in output, f"Output missing 'id' field: {output}"
    assert "ts" in output, f"Output missing 'ts' field: {output}"
    assert "count" in output, f"Output missing 'count' field: {output}"

    print(f"Query output:\n{output}")
    print(f"✅ snowflake.format() function executed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_sequence_table_creation(container_name, container_type):
    """Test Case 3: Create sequence, table with snowflake.nextval(), and insert data"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 3: Creating employees table with Snowflake sequence on {container_name} ---")

    # Create sequence
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "CREATE SEQUENCE employee_id_seq;")
    assert exit_code == 0, f"Failed to create sequence: {output}"
    print("✅ Created employee_id_seq sequence")

    # Create table with snowflake.nextval default
    create_table_sql = """CREATE TABLE employees (
        id BIGINT DEFAULT snowflake.nextval('employee_id_seq'::regclass) PRIMARY KEY,
        name TEXT,
        department TEXT,
        salary DECIMAL(10,2),
        created_at TIMESTAMP DEFAULT NOW()
    );"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, create_table_sql)
    assert exit_code == 0, f"Failed to create employees table: {output}"
    print("✅ Created employees table with snowflake.nextval default")

    # Insert test data
    insert_sql = """INSERT INTO employees (name, department, salary) VALUES 
        ('John Doe', 'Engineering', 75000),
        ('Jane Smith', 'Marketing', 65000),
        ('Bob Wilson', 'Sales', 55000);"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, insert_sql)
    assert exit_code == 0, f"Failed to insert data: {output}"
    print("✅ Inserted 3 employees")

    # Verify data and check for snowflake IDs
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT * FROM employees;")
    assert exit_code == 0, f"Failed to select from employees: {output}"

    print(f"Employees table data:\n{output}")

    # Verify we have 3 rows
    assert "John Doe" in output, "John Doe not found in employees"
    assert "Jane Smith" in output, "Jane Smith not found in employees"
    assert "Bob Wilson" in output, "Bob Wilson not found in employees"

    print(f"✅ Employees table created and populated with Snowflake IDs")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_multiple_sequences(container_name, container_type):
    """Test Case 4: Create another sequence/table and verify different sequence IDs"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 4: Creating products table with different Snowflake sequence on {container_name} ---")

    # Create sequence
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "CREATE SEQUENCE products_id_seq;")
    assert exit_code == 0, f"Failed to create products sequence: {output}"
    print("✅ Created products_id_seq sequence")

    # Create products table
    create_table_sql = """CREATE TABLE products (
        product_id BIGINT DEFAULT snowflake.nextval('products_id_seq'::regclass) PRIMARY KEY,
        name TEXT,
        description TEXT,
        price DECIMAL(10,2)
    );"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, create_table_sql)
    assert exit_code == 0, f"Failed to create products table: {output}"
    print("✅ Created products table")

    # Insert test data
    insert_sql = """INSERT INTO products (name, description, price) VALUES
        ('Laptop', 'Computer', 999.99),
        ('Mouse', 'Computer Accessories', 29.99);"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, insert_sql)
    assert exit_code == 0, f"Failed to insert products: {output}"
    print("✅ Inserted 2 products")

    # Verify data
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT * FROM products;")
    assert exit_code == 0, f"Failed to select from products: {output}"

    print(f"Products table data:\n{output}")

    # Verify we have the products
    assert "Laptop" in output, "Laptop not found in products"
    assert "Mouse" in output, "Mouse not found in products"

    # Get employee IDs and product IDs to verify they're different sequences
    exit_code, emp_output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT id FROM employees LIMIT 1;")
    exit_code, prod_output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT product_id FROM products LIMIT 1;")

    print(f"✅ Products table created with different Snowflake sequence IDs")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_table_metadata(container_name, container_type):
    """Test Case 5: Review sequence definitions in table metadata"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 5: Reviewing table metadata on {container_name} ---")

    # Use psql \d+ command
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -c '\\d+ products'",
        user=pguser,
    )
    output_text = output.decode()

    assert exit_code == 0, f"Failed to describe products table: {output_text}"

    print(f"Products table metadata:\n{output_text}")

    # Verify the default value contains snowflake.nextval
    assert "snowflake.nextval" in output_text, "snowflake.nextval not found in table definition"
    assert "products_id_seq" in output_text, "products_id_seq not found in table definition"
    assert "product_id" in output_text, "product_id column not found"

    print(f"✅ Table metadata shows snowflake.nextval as default")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_extension_objects(container_name, container_type):
    """Test Case 6: List all Snowflake extension objects"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 6: Listing Snowflake extension objects on {container_name} ---")

    query = """SELECT c.relname as object_name, 
                      c.relkind as object_type, 
                      n.nspname as schema_name 
               FROM pg_depend d 
               JOIN pg_extension e ON d.refobjid = e.oid 
               JOIN pg_class c ON d.objid = c.oid 
               JOIN pg_namespace n ON c.relnamespace = n.oid 
               WHERE e.extname = 'snowflake' 
               ORDER BY n.nspname, c.relname;"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, query)
    assert exit_code == 0, f"Failed to list extension objects: {output}"

    print(f"Snowflake extension objects:\n{output}")

    # Verify we have snowflake schema objects
    assert "snowflake" in output, "No snowflake schema objects found"

    print(f"✅ Snowflake extension objects listed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_schema_objects(container_name, container_type):
    """Test Case 7: List all objects in snowflake schema"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 7: Listing objects in snowflake schema on {container_name} ---")

    # Use psql \dn+ command
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -c '\\dn+ snowflake'",
        user=pguser,
    )
    output_text = output.decode()

    assert exit_code == 0, f"Failed to describe snowflake schema: {output_text}"

    print(f"Snowflake schema details:\n{output_text}")

    # Verify snowflake schema exists
    assert "snowflake" in output_text, "Snowflake schema not found"

    print(f"✅ Snowflake schema objects listed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_functions(container_name, container_type):
    """Test Case 8: List all Snowflake functions"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]

    pguser = config["pguser"]

    print(f"\n--- Test Case 8: Listing Snowflake functions on {container_name} ---")

    # Use psql \df command
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -c '\\df snowflake.*'",
        user=pguser,
    )
    output_text = output.decode()

    assert exit_code == 0, f"Failed to list snowflake functions: {output_text}"

    print(f"Snowflake functions:\n{output_text}")

    # Verify expected functions exist
    expected_functions = [
        "convert_column_to_int8",
        "convert_sequence_to_snowflake",
        "currval",
        "format",
        "get_count",
        "get_epoch",
        "get_node",
        "nextval"
    ]

    for func in expected_functions:
        assert func in output_text, f"Function '{func}' not found in snowflake schema"

    print(f"✅ All {len(expected_functions)} expected Snowflake functions found")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_convert_existing_sequence(container_name, container_type):
    """Test Case 9: Convert existing sequence to Snowflake-compatible sequence"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 9: Converting existing sequence to Snowflake on {container_name} ---")

    # Step 1: Create a simple sequence
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "CREATE SEQUENCE my_simple_seq;")
    assert exit_code == 0, f"Failed to create sequence: {output}"
    print("✅ Created my_simple_seq sequence")

    # Step 2: Create users table with regular sequence
    create_table_sql = """CREATE TABLE users (
        id INTEGER DEFAULT nextval('my_simple_seq') PRIMARY KEY,
        name TEXT,
        email TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    );"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, create_table_sql)
    assert exit_code == 0, f"Failed to create users table: {output}"
    print("✅ Created users table with regular sequence")

    # Step 3: Insert initial data with regular sequence
    insert_sql = """INSERT INTO users (name, email) VALUES 
        ('John Doe', 'john@example.com'),
        ('Jane Smith', 'jane@example.com'),
        ('Bob Wilson', 'bob@example.com');"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, insert_sql)
    assert exit_code == 0, f"Failed to insert initial users: {output}"
    print("✅ Inserted 3 users with regular sequence IDs")

    # Step 4: Verify initial data (should have IDs 1, 2, 3)
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT * FROM users;")
    assert exit_code == 0, f"Failed to select initial users: {output}"
    print(f"Initial users data (regular sequence):\n{output}")

    # Verify regular sequence IDs (small integers)
    assert " 1 |" in output, "ID 1 not found"
    assert " 2 |" in output, "ID 2 not found"
    assert " 3 |" in output, "ID 3 not found"

    # Step 5: Convert sequence to Snowflake
    print("\n--- Converting sequence to Snowflake ---")
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT snowflake.convert_sequence_to_snowflake('my_simple_seq'::regclass);")
    assert exit_code == 0, f"Failed to convert sequence: {output}"
    print(f"Conversion output:\n{output}")

    # Verify conversion notices
    assert "ALTER TABLE" in output or "convert_sequence_to_snowflake" in output, \
        "Conversion did not execute properly"
    print("✅ Sequence converted to Snowflake")

    # Step 6: Verify existing data is unchanged
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT * FROM users ORDER BY id;")
    assert exit_code == 0, f"Failed to select users after conversion: {output}"
    print(f"Users data after conversion (existing rows unchanged):\n{output}")

    # Step 7: Insert new data with Snowflake sequence
    insert_new_sql = """INSERT INTO users (name, email) VALUES 
        ('Alice Johnson', 'alice@example.com'),
        ('Charlie Brown', 'charlie@example.com'),
        ('Diana Prince', 'diana@example.com');"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, insert_new_sql)
    assert exit_code == 0, f"Failed to insert new users: {output}"
    print("✅ Inserted 3 more users with Snowflake sequence IDs")

    # Step 8: Verify mixed data (old regular IDs + new Snowflake IDs)
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT * FROM users ORDER BY id;")
    assert exit_code == 0, f"Failed to select final users: {output}"
    print(f"Final users data (mixed regular + Snowflake IDs):\n{output}")

    # Verify we have 6 users total
    assert "John Doe" in output, "Original user John Doe not found"
    assert "Alice Johnson" in output, "New user Alice Johnson not found"

    # Verify Snowflake IDs are present (they should be very large numbers)
    # Look for the pattern of long numbers (Snowflake IDs are typically 18+ digits)
    lines = output.split('\n')
    snowflake_id_found = False
    for line in lines:
        if 'Alice Johnson' in line or 'Charlie Brown' in line or 'Diana Prince' in line:
            # Extract the ID (first column)
            parts = line.strip().split('|')
            if len(parts) > 0:
                id_str = parts[0].strip()
                # Snowflake IDs are very large (18+ digits)
                if len(id_str) > 10 and id_str.isdigit():
                    snowflake_id_found = True
                    print(f"✅ Found Snowflake ID: {id_str}")
                    break

    assert snowflake_id_found, "No Snowflake IDs found in new records"

    print(f"✅ Successfully converted existing sequence to Snowflake sequence")
    print(f"✅ Old records retained original IDs, new records have Snowflake IDs")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_stop_server(container_name, container_type):
    """Stop PostgreSQL server using pg_server_management module"""
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
def test_snowflake_uninstall(container_name, container_type):
    """Uninstall snowflake package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    snowflake_package = config["snowflake_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Uninstalling {snowflake_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, snowflake_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {snowflake_package}: {str(e)}")


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
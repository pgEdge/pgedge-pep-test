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
vectorizer_version = os.getenv(f"PGEDGE_VECTORIZER_{pg_major_version}_VERSION", "1.0")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")
# Vectorizer is a decoupled component, no _{pg_major_version} postfix
rhel_vectorizer_package = os.getenv("VECTORIZER_PACKAGE", f"pgedge-vectorizer_{pg_major_version}")
rhel_bundled_files = os.getenv(
    "VECTORIZER_BUNDLED_FILES",
    ""
).split(",") if os.getenv("VECTORIZER_BUNDLED_FILES") else []

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_vectorizer_package = os.getenv("DEB_VECTORIZER_PACKAGE", f"pgedge-postgresql-{pg_major_version}-vectorizer")
deb_bundled_files = os.getenv(
    "DEB_VECTORIZER_BUNDLED_FILES",
    ""
).split(",") if os.getenv("DEB_VECTORIZER_BUNDLED_FILES") else []

# Additional configuration for extension tests
check_extensions = os.getenv("CHECK_EXTENSIONS", "true").lower() == "true"
base_extensions = [ext.strip() for ext in os.getenv("BASE_EXTENSIONS", "vectorizer").split(",") if ext.strip()]
components = [comp.strip() for comp in os.getenv("COMPONENTS", f"pgedge-vectorizer_{pg_major_version}").split(",") if comp.strip()]

# API key file paths
# PGEDGE_OPEN_API_KEY_PATH is relative to component-test/ (e.g. ../keys/open_api_key.txt)
_api_key_env = os.getenv("PGEDGE_OPEN_API_KEY_PATH", "")
LOCAL_API_KEY_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), _api_key_env)
    if _api_key_env
    else os.path.join(os.path.dirname(__file__), '..', 'keys', 'open_api_key')
)
CONTAINER_API_KEY_PATH = "/tmp/api_key_file"


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "vectorizer_package": rhel_vectorizer_package,
            "bundled_files": rhel_bundled_files
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "vectorizer_package": deb_vectorizer_package,
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
    """Step 2: Install pgedge-vectorizer using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    vectorizer_package = config["vectorizer_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing {vectorizer_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to install the package
    try:
        success, platform, message = package_management.install_package(container, vectorizer_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {vectorizer_package}: {str(e)}")


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
    vectorizer_package = config["vectorizer_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Upgrading {vectorizer_package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    # Use the package_management module to upgrade the package
    try:
        success, platform, message = package_management.upgrade_package(container, vectorizer_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{vectorizer_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {vectorizer_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not vectorizer_version:
        pytest.skip("No VECTORIZER_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    vectorizer_package = config["vectorizer_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {vectorizer_package} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, vectorizer_package, vectorizer_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {vectorizer_package} version: {str(e)}")

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
    actual_package = config["vectorizer_package"]

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
            f"--signature-file pgedge-vectorizer-sbom.json.asc "
            f"--signer-file pgedge-rsa.pub "
            f"pgedge-vectorizer-sbom.json'",
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
        _, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
        _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
        _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"
        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"{_sq_signer_flag} /etc/apt/keyrings/pgedge-rsa.gpg "
            f"{_sq_sig_flag} pgedge-vectorizer-sbom.json.asc "
            f"pgedge-vectorizer-sbom.json'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_copy_api_key(container_name, container_type):
    """Step 4: Copy OpenAI API key to container using file_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Copying API key to {container_name} ---")

    # Check if local API key file exists
    if not os.path.exists(LOCAL_API_KEY_PATH):
        pytest.fail(f"API key file not found at {LOCAL_API_KEY_PATH} — set PGEDGE_OPEN_API_KEY_PATH correctly")

    # Use file_management module to copy API key to container
    config = get_container_config(container_type)
    pguser = config["pguser"]
    try:
        success, message = file_management.copy_file_to_container(
            container=container,
            local_file_path=LOCAL_API_KEY_PATH,
            container_file_path=CONTAINER_API_KEY_PATH,
            permissions="600",
            owner=pguser
        )
        assert success, f"Failed to copy API key: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to copy API key: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_init_cluster(container_name, container_type):
    """Step 5: Initialize PostgreSQL cluster with Vectorizer-specific GUC parameters using pg_server_management module"""
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

    # Define Vectorizer-specific GUC parameters
    guc_parameters = {
        "shared_preload_libraries": "'pgedge_vectorizer'",
        "pgedge_vectorizer.databases": "'postgres'",
        "pgedge_vectorizer.provider": "'openai'",
        "pgedge_vectorizer.api_key_file": f"'{CONTAINER_API_KEY_PATH}'",
        "pgedge_vectorizer.model": "'text-embedding-3-small'",
        "pgedge_vectorizer.num_workers": "2",
        "pgedge_vectorizer.batch_size": "10",
        "pgedge_vectorizer.default_chunk_size": "400",
        "pgedge_vectorizer.default_chunk_overlap": "50",
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
    """Step 6: Start PostgreSQL server using pg_server_management module"""
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
    """Step 7: Check PostgreSQL connection using pg_server_management module"""
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

# @pytest.mark.parametrize("container_name,container_type", all_containers)
# @pytest.mark.parametrize("extension", base_extensions)
# def test_create_extensions(container_name, container_type, extension):
#     """Step 8: Create each extension individually with separate test results
#
#     This creates a separate test for each container-extension combination
#     """
#     if not check_extensions:
#         pytest.skip("Extension check disabled via env")
#
#     container_name = container_name.strip()
#     extension = extension.strip()
#
#     if not container_name or not extension:
#         pytest.skip("Invalid container or extension")
#
#     try:
#         container = client.containers.get(container_name)
#     except docker.errors.NotFound:
#         pytest.skip(f"Container {container_name} not found or not running.")
#
#     assert container.status == "running"
#
#     # Get container-specific configuration
#     config = get_container_config(container_type)
#     pgbin = config["pgbin"]
#     pguser = config["pguser"]
#
#     # Normalize extension (quote if it contains a dash)
#     normalized_ext = f'"{extension}"' if "-" in extension else extension
#
#     print(f"\n--- Creating extension {normalized_ext} in {container_name} ---")
#
#     # Create the extension
#     exit_code, output = container.exec_run(
#         f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
#         f"-c 'CREATE EXTENSION IF NOT EXISTS {normalized_ext} CASCADE;'",
#         user=pguser,
#     )
#
#     assert exit_code == 0, f"Failed to create {normalized_ext}: {output.decode()}"
#     print(f"✅ Successfully created extension {normalized_ext}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
@pytest.mark.parametrize("component", components)
def test_component_functional_smoke(container_name, container_type, component):
    """Step 9: Execute functional smoke tests for each component

    This runs SQL test files from sql/<component-name>.sql
    and stores output in actual-output/sql/<component-name>/<pg_major_version>/rpm/<timestamp>.txt
    """
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("Invalid container or component")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    # Extract base component name by removing 'pgedge-' prefix and version suffix
    # Example: pgedge-vectorizer_18 -> vectorizer
    base_name = component.replace("pgedge-", "")
    # Remove version suffix (_17, _16, etc.)
    base_name = base_name.rsplit('_', 1)[0]

    # Path to SQL test file (sql directory is located one level up from component-test)
    sql_file_path = Path(__file__).parent.parent / "sql" / f"{base_name}.sql"

    # Check if SQL test file exists
    if not sql_file_path.exists():
        pytest.skip(f"No SQL test file found for {base_name} at {sql_file_path}")

    print(f"\n--- Running functional smoke test for {component} on {container_name} ---")
    print(f"Executing SQL file: {sql_file_path}")

    # Read SQL file content
    with open(str(sql_file_path), 'r') as f:
        sql_content = f.read()

    # Create temp SQL file in container
    temp_sql_path = f"/tmp/{base_name}_test.sql"

    # Write SQL content to container using heredoc
    exit_code, output = container.exec_run(
        f"bash -c \"cat > {temp_sql_path} << 'EOSQL'\n{sql_content}\nEOSQL\"",
        user=pguser
    )

    if exit_code != 0:
        pytest.fail(f"Failed to create SQL file in container: {output.decode()}")

    # Execute the SQL file (without ON_ERROR_STOP so all statements run)
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -f {temp_sql_path}",
        user=pguser
    )

    # Clean up temp file
    container.exec_run(f"rm -f {temp_sql_path}", user=pguser)

    # Decode output for analysis
    output_text = output.decode()

    # Create output directory structure
    date_part = datetime.now().strftime("%d%m%y")  # ddmmyy format
    time_part = datetime.now().strftime("%H%M%S")  # hhmmss format
    filename = f"{base_name}-{date_part}-{time_part}.txt"

    # Determine platform-specific path (rpm for RHEL, deb for Debian)
    platform_dir = "rpm" if container_type == "rhel" else "deb"
    # Use project root (parent of component-test/) so outputs go to ../actual-output/
    project_root = Path(__file__).parent.parent
    output_dir = project_root / "actual-output" / "sql" / base_name / pg_major_version / platform_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save output to file
    output_file = output_dir / filename
    with open(output_file, 'w') as f:
        f.write(f"# Functional Smoke Test for {component}\n")
        f.write(f"# Container: {container_name}\n")
        f.write(f"# Container Type: {container_type}\n")
        f.write(f"# PostgreSQL Version: {pg_major_version}\n")
        f.write(f"# Date: {date_part} Time: {time_part}\n")
        f.write(f"# SQL File: {sql_file_path}\n")
        f.write("=" * 80 + "\n\n")
        f.write(output_text)

    print(f"Output saved to: {output_file}")

    # Check for errors in output
    has_errors = "ERROR:" in output_text or exit_code != 0

    if has_errors:
        print(f"⚠️ SQL execution had errors (exit code: {exit_code})")
        print(f"Output:\n{output_text}")
        pytest.fail(f"SQL test failed for {component}: See {output_file} for details")

    print(f"✅ Functional smoke test passed: {component}")
    print(f"   Results: {output_file}")





@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_stop_server(container_name, container_type):
    """Step 10: Stop PostgreSQL server using pg_server_management module"""
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
def test_package_uninstall(container_name, container_type):
    """Step 11: Uninstall vectorizer package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    vectorizer_package = config["vectorizer_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Uninstalling {vectorizer_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, vectorizer_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {vectorizer_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Step 12: Full cleanup using machine_cleanup module: remove all pgedge packages + leftover data"""
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
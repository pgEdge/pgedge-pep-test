import os
import sys
import time
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
skip_cleanup = os.getenv("SKIP_CLEANUP", "false").lower() == "true"
pgport = os.getenv("PG_PORT", "5432")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")
postgrest_version = os.getenv("PGEDGE_POSTGREST_VERSION", "14.4")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# Default pguser for simple tests
pguser = rhel_pguser

# PostgREST configuration
postgrest_package = os.getenv("POSTGREST_PACKAGE", "pgedge-postgrest")
postgrest_bin = "/usr/bin/postgrest"
postgrest_conf = "/etc/pgedge/postgrest.conf"
postgrest_service_user = "pgedge"
postgrest_service_group = "pgedge"
postgrest_workdir = "/var/lib/pgedge/postgrest"
postgrest_log_dir = "/var/log/pgedge/postgrest"
postgrest_log_file = f"{postgrest_log_dir}/postgrest.log"
postgrest_port = os.getenv("POSTGREST_SERVER_PORT", "3000")
postgrest_ready_timeout = 30  # seconds to wait for PostgREST schema cache

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_server_package = os.getenv("SERVER_PACKAGE", f"pgedge-postgresql{pg_major_version}-server")

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_server_package = os.getenv("DEB_SERVER_PACKAGE", f"pgedge-postgresql-{pg_major_version}")

# Additional configuration for component tests
components = [comp.strip() for comp in os.getenv("COMPONENTS", "pgedge-postgrest").split(",") if comp.strip()]

# Decoupled components SBOM path
decoupled_sbom_path = os.getenv("DECOUPLED_COMPONENTS_SBOM", "")


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "postgrest_package": postgrest_package,
            "server_package": rhel_server_package,
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "postgrest_package": postgrest_package,
            "server_package": deb_server_package,
        }


def wait_for_postgrest_ready(container, timeout=None):
    """Poll PostgREST root endpoint until schema cache is loaded.

    Returns True if PostgREST is ready, False if timeout reached.
    """
    if timeout is None:
        timeout = postgrest_ready_timeout

    start = time.time()
    while time.time() - start < timeout:
        exit_code, output = container.exec_run(
            f"curl -s http://127.0.0.1:{postgrest_port}/",
            user="root"
        )
        if exit_code == 0:
            response = output.decode()
            if "PGRST" not in response:
                return True
        time.sleep(2)
    return False


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
    """Step 2: Install pgedge-postgrest using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pkg = config["postgrest_package"]

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

    if not postgrest_version:
        pytest.skip("No PGEDGE_POSTGREST_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pkg = config["postgrest_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {pkg} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, pkg, postgrest_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {pkg} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_binaries_stripped(container_name, container_type):
    """Step 4: Verify that PostgREST binary is stripped"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying PostgREST binary is stripped on {container_name} ({container_type}) ---")

    # Use the file_management module to verify the binary is stripped
    try:
        success, details, message = file_management.verify_binaries_stripped(
            container=container,
            binary_path="/usr/bin",
            container_name=container_name,
            binary_names="postgrest"
        )

        print(f"Total binaries checked: {details['total_binaries']}")
        print(f"Stripped binaries: {details['stripped_binaries']}")

        if not success:
            print(f"⚠️ Unstripped binaries found: {len(details['unstripped_binaries'])}")
            for binary in details['unstripped_binaries'][:5]:
                print(f"  - {binary}")

        assert success, f"Binary stripping verification failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to verify binaries are stripped: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files for PostgREST match expected files

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
    actual_package = config["postgrest_package"]

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
    """Verify SBOM signature files located under the decoupled components SBOM directory"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not decoupled_sbom_path:
        pytest.skip("DECOUPLED_COMPONENTS_SBOM not defined in env, skipping SBOM verification")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    sbom_dir = decoupled_sbom_path

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
            f"--signature-file postgrest-sbom.json.asc "
            f"--signer-file pgedge-rsa.pub "
            f"postgrest-sbom.json'",
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
        _, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
        _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
        _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"
        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"{_sq_signer_flag} /etc/apt/keyrings/pgedge-rsa.gpg "
            f"{_sq_sig_flag} postgrest-sbom.json.asc "
            f"postgrest-sbom.json'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_server_install(container_name, container_type):
    """Step 6: Install PostgreSQL server for PostgREST functional testing"""
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

    print(f"\n--- Installing {server_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.install_package(container, server_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {server_package}: {str(e)}")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_init_cluster(container_name, container_type):
    """Initialize PostgreSQL cluster for PostgREST testing"""
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
    pguser_val = config["pguser"]

    print(f"\n--- Initializing cluster on {container_name} ---")

    # Basic GUC parameters
    guc_parameters = {}

    try:
        success, config_content, message = pg_server_management.init_cluster(
            container, pgbin, pgdata, pguser_val, guc_parameters
        )
        assert success, f"Cluster initialization failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to initialize cluster: {str(e)}")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_start_server(container_name, container_type):
    """Start PostgreSQL server"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser_val = config["pguser"]

    print(f"\n--- Starting PostgreSQL server on {container_name} ---")

    try:
        success, server_output, message = pg_server_management.start_server(
            container, pgbin, pgdata, pgport, pguser_val
        )
        assert success, f"Server start failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to start PostgreSQL server: {str(e)}")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_check_connection(container_name, container_type):
    """Check PostgreSQL connection"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser_val = config["pguser"]

    print(f"\n--- Checking PostgreSQL connection on {container_name} ---")

    try:
        success, version_output, message = pg_server_management.check_connection(
            container, pgbin, pgport, pguser_val
        )
        assert success, f"Connection check failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to check PostgreSQL connection: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_execute_setup_sql(container_name, container_type):
    """Execute sql/postgrest.sql to create authenticator role and anon role"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser_val = config["pguser"]

    print(f"\n--- Executing postgrest.sql on {container_name} ({container_type}) ---")

    # Copy sql/postgrest.sql into the container
    project_root = Path(__file__).parent.parent.resolve()
    sql_file = project_root / "sql" / "postgrest.sql"

    try:
        success, files_copied, message = file_management.copy_config_files_to_container(
            container=container,
            container_name=container_name,
            local_config_dir=str(sql_file.parent),
            container_config_dir="/tmp",
            file_mapping={"postgrest.sql": "postgrest.sql"},
            owner=pguser_val,
            group=pguser_val,
            permissions="644"
        )
        assert success, f"Failed to copy postgrest.sql: {message}"
        print(f"✅ Copied postgrest.sql to container")
    except Exception as e:
        pytest.fail(f"Failed to copy postgrest.sql: {str(e)}")

    # Execute the SQL file
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser_val} -d postgres -f /tmp/postgrest.sql",
        user=pguser_val,
    )
    assert exit_code == 0, f"Failed to execute postgrest.sql: {output.decode()}"
    print(f"Output: {output.decode().strip()}")
    print(f"✅ postgrest.sql executed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_start_service(container_name, container_type):
    """Start PostgREST service and verify it is running"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pguser_val = config["pguser"]

    print(f"\n--- Starting PostgREST service on {container_name} ({container_type}) ---")

    # Ensure pgedge user exists
    exit_code, output = container.exec_run(
        "id pgedge",
        user="root"
    )
    if exit_code != 0:
        # Create pgedge user
        exit_code, output = container.exec_run(
            "useradd -r -m -d /var/lib/pgedge -s /sbin/nologin pgedge",
            user="root"
        )
        if exit_code != 0:
            print(f"Warning: Could not create pgedge user: {output.decode()}")
        else:
            print(f"✅ Created pgedge user")

    # Ensure required directories exist with correct ownership
    for dir_path in [postgrest_workdir, postgrest_log_dir]:
        exit_code, output = container.exec_run(
            f"bash -c 'mkdir -p {dir_path} && chown -R {postgrest_service_user}:{postgrest_service_group} {dir_path}'",
            user="root"
        )
        assert exit_code == 0, f"Failed to create/chown {dir_path}: {output.decode()}"

    # Truncate log file before starting
    exit_code, output = container.exec_run(
        f"bash -c 'truncate -s 0 {postgrest_log_file} 2>/dev/null; touch {postgrest_log_file} && chown {postgrest_service_user}:{postgrest_service_group} {postgrest_log_file}'",
        user="root"
    )

    # Update postgrest.conf: replace default placeholders with actual values
    exit_code, output = container.exec_run(
        f"sed -i 's/mysecretpassword/postgres/g; s/mydb/postgres/g; s/server-port = 3000/server-port = {postgrest_port}/g' {postgrest_conf}",
        user="root"
    )
    assert exit_code == 0, f"Failed to update postgrest.conf: {output.decode()}"
    print(f"✅ Updated {postgrest_conf} (db name=postgres, password=postgres, server-port={postgrest_port})")

    # Start PostgREST as pgedge user in the background, mimicking the systemd service
    exit_code, output = container.exec_run(
        f"bash -c 'cd {postgrest_workdir} && nohup {postgrest_bin} {postgrest_conf} >> {postgrest_log_file} 2>&1 &'",
        user=postgrest_service_user
    )
    assert exit_code == 0, f"Failed to start PostgREST: {output.decode()}"

    # Wait for PostgREST to start
    time.sleep(5)

    # Verify PostgREST process is running
    exit_code, output = container.exec_run(
        "pgrep -x postgrest",
        user="root"
    )
    assert exit_code == 0, f"PostgREST process not running: {output.decode()}"
    print(f"✅ PostgREST process is running (PID: {output.decode().strip()})")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_verify_log(container_name, container_type):
    """Verify PostgREST log contains 'Config reloaded'"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying PostgREST log on {container_name} ({container_type}) ---")

    # Allow some extra time for config reload log entry
    time.sleep(3)

    exit_code, output = container.exec_run(
        f"cat {postgrest_log_file}",
        user="root"
    )
    assert exit_code == 0, f"Failed to read PostgREST log: {output.decode()}"

    log_content = output.decode()
    print(f"PostgREST log:\n{log_content.strip()}")

    assert "Config reloaded" in log_content, (
        f"'Config reloaded' not found in PostgREST log.\n"
        f"Log contents:\n{log_content}"
    )
    print(f"✅ PostgREST log contains 'Config reloaded'")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_load_northwind(container_name, container_type):
    """Load Northwind sample database for REST API testing"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser_val = config["pguser"]

    print(f"\n--- Loading Northwind database on {container_name} ({container_type}) ---")

    # Copy utillities/northwind.sql into the container
    project_root = Path(__file__).parent.parent.resolve()
    northwind_file = project_root / "utillities" / "northwind.sql"

    try:
        success, files_copied, message = file_management.copy_config_files_to_container(
            container=container,
            container_name=container_name,
            local_config_dir=str(northwind_file.parent),
            container_config_dir="/tmp",
            file_mapping={"northwind.sql": "northwind.sql"},
            owner=pguser_val,
            group=pguser_val,
            permissions="644"
        )
        assert success, f"Failed to copy northwind.sql: {message}"
        print(f"✅ Copied northwind.sql to container")
    except Exception as e:
        pytest.fail(f"Failed to copy northwind.sql: {str(e)}")

    # Execute northwind.sql
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser_val} -d postgres -f /tmp/northwind.sql",
        user=pguser_val,
    )
    assert exit_code == 0, f"Failed to execute northwind.sql: {output.decode()}"
    print(f"✅ Northwind database loaded successfully")

    # Grant SELECT on new tables to anon role for PostgREST
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser_val} -d postgres "
        f"-c 'GRANT SELECT ON ALL TABLES IN SCHEMA public TO anon;'",
        user=pguser_val,
    )
    assert exit_code == 0, f"Failed to grant permissions to anon: {output.decode()}"
    print(f"✅ Granted SELECT on all tables to anon role")

    # Notify PostgREST to reload schema cache after new tables/grants
    exit_code, output = container.exec_run(
        "bash -c 'kill -s SIGUSR1 $(pgrep -x postgrest) 2>/dev/null || true'",
        user="root"
    )
    print(f"PostgREST schema cache reload signaled, waiting for readiness...")

    # Poll PostgREST until it finishes building the schema cache
    ready = wait_for_postgrest_ready(container)
    assert ready, (
        f"PostgREST did not become ready within {postgrest_ready_timeout}s after schema reload. "
        f"It may still be returning PGRST002 errors."
    )
    print(f"✅ PostgREST is ready and serving requests")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_api_list_tables(container_name, container_type):
    """REST API: List all tables (root endpoint)"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- REST API: List tables on {container_name} ---")

    # Ensure PostgREST schema cache is ready before API tests
    ready = wait_for_postgrest_ready(container)
    assert ready, f"PostgREST not ready within {postgrest_ready_timeout}s (PGRST002 errors)"

    exit_code, output = container.exec_run(
        f"curl -s http://127.0.0.1:{postgrest_port}/",
        user="root"
    )
    assert exit_code == 0, f"curl failed: {output.decode()}"

    response = output.decode()
    print(f"Response: {response[:500]}")

    # Verify Northwind tables are listed
    assert "customers" in response.lower(), f"'customers' table not found in root response"
    assert "products" in response.lower(), f"'products' table not found in root response"
    print(f"✅ Root endpoint lists Northwind tables")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_api_get_customers(container_name, container_type):
    """REST API: Get all customers"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- REST API: Get customers on {container_name} ---")

    exit_code, output = container.exec_run(
        f"curl -s http://127.0.0.1:{postgrest_port}/customers",
        user="root"
    )
    assert exit_code == 0, f"curl failed: {output.decode()}"

    response = output.decode()
    print(f"Response (first 500 chars): {response[:500]}")

    assert "customer_id" in response.lower() or "company_name" in response.lower(), (
        f"Customer data not found in response"
    )
    print(f"✅ GET /customers returned customer data")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_api_get_products(container_name, container_type):
    """REST API: Get all products"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- REST API: Get products on {container_name} ---")

    exit_code, output = container.exec_run(
        f"curl -s http://127.0.0.1:{postgrest_port}/products",
        user="root"
    )
    assert exit_code == 0, f"curl failed: {output.decode()}"

    response = output.decode()
    print(f"Response (first 500 chars): {response[:500]}")

    assert "product_id" in response.lower() or "product_name" in response.lower(), (
        f"Product data not found in response"
    )
    print(f"✅ GET /products returned product data")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_api_filter_products_by_price(container_name, container_type):
    """REST API: Filter products where unit_price > 20"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- REST API: Filter products by price on {container_name} ---")

    exit_code, output = container.exec_run(
        f'curl -s "http://127.0.0.1:{postgrest_port}/products?unit_price=gt.20"',
        user="root"
    )
    assert exit_code == 0, f"curl failed: {output.decode()}"

    response = output.decode()
    print(f"Response (first 500 chars): {response[:500]}")

    # Should return product data (not an error)
    assert "[" in response, f"Expected JSON array response, got: {response[:200]}"
    print(f"✅ GET /products?unit_price=gt.20 returned filtered data")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_api_select_columns(container_name, container_type):
    """REST API: Select specific columns from customers"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- REST API: Select specific columns on {container_name} ---")

    exit_code, output = container.exec_run(
        f'curl -s "http://127.0.0.1:{postgrest_port}/customers?select=customer_id,company_name,country"',
        user="root"
    )
    assert exit_code == 0, f"curl failed: {output.decode()}"

    response = output.decode()
    print(f"Response (first 500 chars): {response[:500]}")

    assert "customer_id" in response.lower(), f"customer_id not found in response"
    assert "company_name" in response.lower(), f"company_name not found in response"
    assert "country" in response.lower(), f"country not found in response"
    print(f"✅ GET /customers?select=... returned selected columns")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_api_filter_customers_by_country(container_name, container_type):
    """REST API: Filter customers from Germany"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- REST API: Filter customers by country on {container_name} ---")

    exit_code, output = container.exec_run(
        f'curl -s "http://127.0.0.1:{postgrest_port}/customers?country=eq.Germany"',
        user="root"
    )
    assert exit_code == 0, f"curl failed: {output.decode()}"

    response = output.decode()
    print(f"Response (first 500 chars): {response[:500]}")

    assert "Germany" in response, f"Germany not found in filtered response"
    print(f"✅ GET /customers?country=eq.Germany returned German customers")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_api_order_products(container_name, container_type):
    """REST API: Order products by price descending, limit 5"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- REST API: Order products by price on {container_name} ---")

    exit_code, output = container.exec_run(
        f'curl -s "http://127.0.0.1:{postgrest_port}/products?order=unit_price.desc&limit=5"',
        user="root"
    )
    assert exit_code == 0, f"curl failed: {output.decode()}"

    response = output.decode()
    print(f"Response: {response[:500]}")

    assert "[" in response, f"Expected JSON array response, got: {response[:200]}"
    print(f"✅ GET /products?order=unit_price.desc&limit=5 returned ordered data")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_api_orders_with_join(container_name, container_type):
    """REST API: Get orders with customer info (relationship join)"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- REST API: Orders with customer join on {container_name} ---")

    exit_code, output = container.exec_run(
        f'curl -s "http://127.0.0.1:{postgrest_port}/orders?select=order_id,order_date,customers(company_name,country)&limit=5"',
        user="root"
    )
    assert exit_code == 0, f"curl failed: {output.decode()}"

    response = output.decode()
    print(f"Response: {response[:500]}")

    assert "order_id" in response.lower(), f"order_id not found in response"
    print(f"✅ GET /orders with customer join returned data")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_postgrest_stop_service(container_name, container_type):
    """Stop PostgREST process"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Stopping PostgREST on {container_name} ---")

    # Kill PostgREST process
    exit_code, output = container.exec_run(
        "bash -c 'pkill -x postgrest || true'",
        user="root"
    )

    time.sleep(2)

    # Verify process is stopped
    exit_code, output = container.exec_run(
        "pgrep -x postgrest",
        user="root"
    )
    assert exit_code != 0, f"PostgREST process still running: {output.decode()}"
    print(f"✅ PostgREST service stopped")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_stop_server(container_name, container_type):
    """Stop PostgreSQL server"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser_val = config["pguser"]

    print(f"\n--- Stopping PostgreSQL server on {container_name} ---")

    try:
        success, server_output, message = pg_server_management.stop_server(
            container, pgbin, pgdata, pgport, pguser_val
        )
        assert success, f"Server stop failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to stop PostgreSQL server: {str(e)}")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_package_uninstall(container_name, container_type):
    """Uninstall postgrest package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    pkg = config["postgrest_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Uninstalling {pkg} on {container_name} ({container_type}) ---")

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
    pguser_val = config["pguser"]

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")

    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata=pgdata, pguser=pguser_val
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
        if cleanup_summary["data_directory_removed"]:
            print(f"   Data directory removed: {pgdata}")
        if cleanup_summary["user_removed"]:
            print(f"   User removed: {pguser_val}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")
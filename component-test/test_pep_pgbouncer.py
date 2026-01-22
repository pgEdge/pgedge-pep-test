import os
import sys
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
    containers = rhel_containers  # Use only RHEL containers
elif platform_filter == "deb":
    all_containers = [(c, t) for c, t in all_containers if t == "deb"]
    containers = deb_containers  # Use only DEB containers
else:
    # If platform_filter is empty or "all", use all containers
    containers = rhel_containers + deb_containers

# Common configuration
repo = os.getenv("REPO", "release")
pgport = os.getenv("PG_PORT", "5432")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")
pgbouncer_version = os.getenv("PGEDGE_PGBOUNCER_VERSION", "1.23.1")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# Default pguser for simple tests (use RHEL default)
pguser = rhel_pguser

# PgBouncer configuration
rhel_pgbouncer_user = os.getenv("PGBOUNCER_USER", "pgbouncer")
deb_pgbouncer_user = os.getenv("DEB_PGBOUNCER_USER", "postgres")
pgbouncer_port = os.getenv("PGBOUNCER_PORT", "6432")
pgbouncer_config_dir = os.getenv("PGBOUNCER_CONFIG_DIR", "/etc/pgbouncer")
pgbouncer_stripped_bin = os.getenv("PGBOUNCER_STRIPPED_BIN", "pgbouncer")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pgbouncer_bin = os.getenv("RHEL_PGBOUNCER_BIN", f"/usr/bin")
rhel_pgbouncer_package = os.getenv("PGBOUNCER_PACKAGE", f"pgedge-pgbouncer")
rhel_server_package = os.getenv("SERVER_PACKAGE", f"pgedge-postgresql{pg_major_version}-server")


# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pgbouncer_bin = os.getenv("DEB_PGBOUNCER_BIN", f"/usr/sbin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_pgbouncer_package = os.getenv("DEB_PGBOUNCER_PACKAGE", f"pgedge-pgbouncer")
deb_server_package = os.getenv("DEB_SERVER_PACKAGE", f"pgedge-postgresql-{pg_major_version}")


# Additional configuration for component tests
check_extensions = os.getenv("CHECK_EXTENSIONS", "false").lower() == "true"
base_extensions = [ext.strip() for ext in os.getenv("BASE_EXTENSIONS", "").split(",") if ext.strip()]
components = [comp.strip() for comp in os.getenv("COMPONENTS", f"pgedge-pgbouncer").split(",") if comp.strip()]


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "pgbouncer_package": rhel_pgbouncer_package,
            "server_package": rhel_server_package,
            "pgbouncer_bin": rhel_pgbouncer_bin,
            "pgbouncer_user": rhel_pgbouncer_user
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "server_package": deb_server_package,
            "pgbouncer_package": deb_pgbouncer_package,
            "pgbouncer_bin": deb_pgbouncer_bin,
            "pgbouncer_user": deb_pgbouncer_user
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
    """Step 2: Install pgedge-pgbouncer using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbouncer_package = config["pgbouncer_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing {pgbouncer_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to install the package
    try:
        success, platform, message = package_management.install_package(container, pgbouncer_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {pgbouncer_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not pgbouncer_version:
        pytest.skip("No PGBOUNCER_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbouncer_package = config["pgbouncer_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {pgbouncer_package} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, pgbouncer_package, pgbouncer_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {pgbouncer_package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_binaries_stripped(container_name, container_type):
    """Step 4: Verify that PgBouncer binaries are stripped using file_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbouncer_bin_dir = config.get("pgbouncer_bin", rhel_pgbouncer_bin if container_type == "rhel" else deb_pgbouncer_bin)

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying PgBouncer binaries are stripped on {container_name} ({container_type}) ---")
    print(f"Binary directory: {pgbouncer_bin_dir}")
    print(f"Binaries to check: {pgbouncer_stripped_bin}")

    # Use the file_management module to verify specific binaries are stripped
    try:
        success, details, message = file_management.verify_binaries_stripped(
            container=container,
            binary_path=pgbouncer_bin_dir,
            container_name=container_name,
            binary_names=pgbouncer_stripped_bin  # Check specific binary from env
        )

        # Display results
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
    actual_package = config["pgbouncer_package"]

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
                if "missing" in details and details["missing"]:
                    details_str += f"\n\nMissing files ({len(details['missing'])}):\n"
                    for file in details["missing"]:
                        details_str += f"  - {file}\n"
                if "extra" in details and details["extra"]:
                    details_str += f"\nExtra files ({len(details['extra'])}):\n"
                    for file in details["extra"]:
                        details_str += f"  + {file}\n"
            pytest.fail(f"{message}{details_str}")

    except Exception as e:
        # Handle cases like missing expected files
        if "No expected file found" in str(e):
            pytest.skip(str(e))
        else:
            pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_server_install(container_name, container_type):
    """Step 2: Install pgedge-pgbouncer using package_management module"""
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

    # Use the package_management module to install the package
    try:
        success, platform, message = package_management.install_package(container, server_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {server_package}: {str(e)}")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_init_cluster(container_name, container_type):
    """Initialize PostgreSQL cluster using pg_server_management module"""
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

    # Basic GUC parameters for pgbouncer testing
    guc_parameters = {}

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

@pytest.mark.parametrize("container_name, container_type", all_containers)
def test_pgbouncer_copy_config_files(container_name, container_type):
    """Step 5: Copy userlist.txt and pgbouncer.ini from config to /etc/pgbouncer/

    Platform-specific behavior:
    - RHEL: Copies pgbouncer.ini as pgbouncer.ini
    - Debian: Copies deb-pgbouncer.ini as pgbouncer.ini
    """
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
    pgbouncer_user = config["pgbouncer_user"]

    # Define platform-specific file mapping (source -> destination)
    if container_type == "rhel":
        file_mapping = {
            "userlist.txt": "userlist.txt",
            "pgbouncer.ini": "pgbouncer.ini"
        }
        print(f"\n--- Copying RHEL pgbouncer config files to {container_name} ---")
    else:  # deb
        file_mapping = {
            "userlist.txt": "userlist.txt",
            "deb-pgbouncer.ini": "pgbouncer.ini"  # Copy deb-specific config as pgbouncer.ini
        }
        print(f"\n--- Copying Debian pgbouncer config files to {container_name} ---")

    # Use the file_management module to copy config files
    try:
        project_root = Path(__file__).parent.parent.resolve()
        local_config_dir = project_root / "config" / "pgbouncer"
        success, files_copied, message = file_management.copy_config_files_to_container(
            container=container,
            container_name=container_name,
            # local_config_dir="./config/pgbouncer",
            local_config_dir=str(local_config_dir),
            container_config_dir=pgbouncer_config_dir,
            file_mapping=file_mapping,
            owner=pgbouncer_user,
            group=pgbouncer_user,
            permissions="600"
        )
        assert success, f"Config file copy failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform: {container_type}, Files copied: {', '.join(files_copied)}")
    except Exception as e:
        pytest.fail(f"Failed to copy config files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def pgbouncer_set_permissions(container_name, container_type):
    """Step 6: Change /etc/pgbouncer/userlist.txt permissions to 600 with pgbouncer:pgbouncer ownership"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    userlist_file = f"{pgbouncer_config_dir}/userlist.txt"

    # Use the file_management module to set permissions and ownership
    try:
        # Get container-specific configuration
        config = get_container_config(container_type)
        pgbouncer_user = config["pgbouncer_user"]

        success, file_info, message = file_management.set_file_permissions(
            container=container,
            file_path=userlist_file,
            owner=pgbouncer_user,
            group=pgbouncer_user,
            permissions="600",
            create_user=True,
            user_options="-r -s /sbin/nologin"
        )
        assert success, f"Failed to set permissions: {message}"

        # Verify permissions are -rw-------
        assert "-rw-------" in file_info, f"Permissions not set correctly: {file_info}"

        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to set file permissions: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgbouncer_start_service(container_name, container_type):
    """Step 7: Switch to pgbouncer user and start pgbouncer daemon"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Starting PgBouncer service on {container_name} ---")
    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbouncer_user = config["pgbouncer_user"]
    # Set ownership of config directory and files to pgbouncer
    exit_code, output = container.exec_run(
        f"chown -R {pgbouncer_user}:{pgbouncer_user} {pgbouncer_config_dir}",
        user="root"
    )
    if exit_code != 0:
        print(f"Warning: Failed to set ownership on config dir: {output.decode()}")

    # Try to find pgbouncer binary (check both common locations)
    pgbouncer_paths = ["/usr/bin/pgbouncer", "/usr/sbin/pgbouncer"]
    pgbouncer_bin = None

    for path in pgbouncer_paths:
        check_exit_code, check_output = container.exec_run(
            f"test -f {path}",
            user="root"
        )
        if check_exit_code == 0:
            pgbouncer_bin = path
            break

    if pgbouncer_bin is None:
        pytest.fail("pgbouncer binary not found in /usr/bin or /usr/sbin")

    print(f"Found pgbouncer at: {pgbouncer_bin}")

    # Start pgbouncer as pgbouncer user
    exit_code, output = container.exec_run(
        f"{pgbouncer_bin} -d {pgbouncer_config_dir}/pgbouncer.ini",
        user=pgbouncer_user
    )
    assert exit_code == 0, f"Failed to start pgbouncer: {output.decode()}"
    print(f"✅ PgBouncer started with daemon mode")

    # Allow some time for process to start
    import time
    time.sleep(2)

    # Verify pgbouncer process is running
    exit_code, output = container.exec_run(
        "pgrep -x pgbouncer",
        user="root"
    )
    assert exit_code == 0, f"PgBouncer process not running: {output.decode()}"
    print(f"✅ PgBouncer process is running (PID: {output.decode().strip()})")

    # Alternative check using ps
    exit_code, output = container.exec_run(
        "ps aux | grep pgbouncer | grep -v grep",
        user="root"
    )
    print(f"Process info: {output.decode().strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgbouncer_connect_psql(container_name, container_type):
    """Step 8: Connect to pgbouncer via psql on port 6432"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"
    # # Get container-specific configuration
    # config = get_container_config(container_type)
    # pgbouncer_user = config["pgbouncer_user"]
    # print(f"\n--- Connecting to PgBouncer via psql on {container_name} ---")

    # Connect to pgbouncer admin database
    exit_code, output = container.exec_run(
        f"psql -h 127.0.0.1 -p {pgbouncer_port}  -d pgbouncer ",
        user=pguser
    )
    assert exit_code == 0, f"Failed to connect to pgbouncer: {output.decode()}"
    print(f"✅ Successfully connected to pgbouncer database")
    print(f"Output: {output.decode().strip()}")


@pytest.mark.parametrize("container_name", containers)
def test_pgbouncer_show_help(container_name):
    """Step 9.1: Run SHOW HELP command"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Running SHOW HELP on PgBouncer ({container_name}) ---")

    exit_code, output = container.exec_run(
        f"psql -h 127.0.0.1 -p {pgbouncer_port}  -d pgbouncer -c 'SHOW HELP;'",
        user=pguser
    )
    assert exit_code == 0, f"SHOW HELP failed: {output.decode()}"

    help_output = output.decode()
    print(f"SHOW HELP output:\n{help_output}")

    # Verify help output contains expected commands
    assert "SHOW" in help_output, "SHOW HELP output doesn't contain expected content"
    print(f"✅ SHOW HELP executed successfully")


@pytest.mark.parametrize("container_name", containers)
def test_pgbouncer_show_version(container_name):
    """Step 9.2: Run SHOW VERSION command and verify it matches config version"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Running SHOW VERSION on PgBouncer ({container_name}) ---")

    exit_code, output = container.exec_run(
        f"psql -h 127.0.0.1 -p {pgbouncer_port} -d pgbouncer -c 'SHOW VERSION;'",
        user=pguser
    )
    assert exit_code == 0, f"SHOW VERSION failed: {output.decode()}"

    version_output = output.decode()
    print(f"SHOW VERSION output:\n{version_output}")

    # Verify version matches expected version from env
    if pgbouncer_version:
        assert pgbouncer_version in version_output, (
            f"Version mismatch!\n"
            f"Expected: {pgbouncer_version}\n"
            f"Got: {version_output}"
        )
        print(f"✅ Version verified: {pgbouncer_version}")
    else:
        print(f"⚠️ PGBOUNCER_VERSION not set in env, skipping version verification")

    print(f"✅ SHOW VERSION executed successfully")


@pytest.mark.parametrize("container_name", containers)
def test_pgbouncer_show_databases(container_name):
    """Step 9.3: Run SHOW DATABASES command"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Running SHOW DATABASES on PgBouncer ({container_name}) ---")

    exit_code, output = container.exec_run(
        f"psql -h 127.0.0.1 -p {pgbouncer_port} -d pgbouncer -c 'SHOW DATABASES;'",
        user=pguser
    )
    assert exit_code == 0, f"SHOW DATABASES failed: {output.decode()}"

    databases_output = output.decode()
    print(f"SHOW DATABASES output:\n{databases_output}")

    # Verify output contains database information (header columns)
    assert "name" in databases_output.lower() or "database" in databases_output.lower(), (
        "SHOW DATABASES output doesn't contain expected content"
    )
    print(f"✅ SHOW DATABASES executed successfully")

@pytest.mark.parametrize("container_name", containers)
def test_stop_pgbouncer(container_name):
    """Step 9.3: Pause and shutdown PgBouncer, tolerate expected 'server closed' outputs"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Running Pause+Shutdown PgBouncer on ({container_name}) ---")

    # Determine container type so we can select the correct pgbouncer/admin user
    if container_name in rhel_containers:
        container_type = "rhel"
    elif container_name in deb_containers:
        container_type = "deb"
    else:
        # fallback - use default pguser
        container_type = None

    psql_user = pguser
    if container_type:
        psql_user = get_container_config(container_type).get("pgbouncer_user", pguser)

    # Pause first
    exit_code, output = container.exec_run(
        f"psql -h 127.0.0.1 -p {pgbouncer_port} -d pgbouncer -c 'PAUSE;'",
        user=psql_user
    )
    pause_out = output.decode(errors='ignore').strip()
    print(pause_out)

    # Then shutdown
    exit_code, output = container.exec_run(
        f"psql -h 127.0.0.1 -p {pgbouncer_port} -d pgbouncer -c 'SHUTDOWN;'",
        user=psql_user
    )
    shutdown_out = output.decode(errors='ignore').strip()
    print(shutdown_out)

    # Accept either zero exit code or common "server closed" style messages as success.
    if exit_code == 0:
        print("✅ PgBouncer shutdown command exited with 0")
    else:
        low = shutdown_out.lower()
        if any(tok in low for tok in ("server closed", "connection to server", "closed", "server closed the connection")):
            print("✅ PgBouncer shutdown produced expected 'server closed' output (treated as success)")
        else:
            pytest.fail(f"PgBouncer shutdown failed (exit {exit_code}): {shutdown_out}")


@pytest.mark.parametrize("container_name", containers)
def pgbouncer_stop_service(container_name):
    """Step 10: Stop PgBouncer service for cleanup"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Stopping PgBouncer service on {container_name} ---")

    # Kill pgbouncer process
    exit_code, output = container.exec_run(
        "pkill pgbouncer",
        user="root"
    )

    # Verify process is stopped
    exit_code, output = container.exec_run(
        "pgrep -x pgbouncer",
        user="root"
    )
    assert exit_code != 0, f"PgBouncer process still running: {output.decode()}"

    print(f"✅ PgBouncer service stopped")





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
def test_pgbouncer_cleanup(container_name, container_type):
    """Step 11: Cleanup PgBouncer environment - process, config, logs, and user"""
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
    pgbouncer_user = config["pgbouncer_user"]

    # Use the machine_cleanup module to perform PgBouncer cleanup
    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgbouncer_environment(
            container=container,
            pgbouncer_config_dir=pgbouncer_config_dir,
            pgbouncer_user=pgbouncer_user,
            pgbouncer_log_dir="/var/log/pgbouncer"
        )
        assert success, f"PgBouncer cleanup failed: {message}"
        print(f"✅ {message}")

        # Display cleanup details
        details = []
        if cleanup_summary["process_stopped"]:
            details.append("Process stopped")
        if cleanup_summary["config_directory_removed"]:
            details.append(f"Config directory removed ({pgbouncer_config_dir})")
        if cleanup_summary["log_directory_removed"]:
            details.append("Log directory removed")
        if cleanup_summary["user_removed"]:
            details.append(f"User removed ({pgbouncer_user})")

        if details:
            print(f"   Cleaned: {', '.join(details)}")

    except Exception as e:
        pytest.fail(f"Failed to cleanup PgBouncer environment: {str(e)}")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_package_uninstall(container_name, container_type):
    """Uninstall pgbouncer package using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbouncer_package = config["pgbouncer_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Uninstalling {pgbouncer_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, pgbouncer_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {pgbouncer_package}: {str(e)}")


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

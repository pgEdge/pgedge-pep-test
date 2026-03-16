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
pgbackrest_version = os.getenv("PGEDGE_PGBACKREST_VERSION", "2.54.0")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# Default pguser for simple tests (use RHEL default)
pguser = rhel_pguser

# pgBackRest configuration
pgbackrest_package = os.getenv("PGBACKREST_PACKAGE", "pgedge-pgbackrest")
pgbackrest_bin = "/usr/bin"
pgbackrest_stripped_bin = "pgbackrest"
pgbackrest_config_file = "/etc/pgbackrest.conf"
pgbackrest_stanza = "main"
pgbackrest_repo_path = "/var/lib/pgbackrest"
pgbackrest_log_dir = "/var/log/pgbackrest"
pgbackrest_spool_dir = "/var/spool/pgbackrest"

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_server_package = os.getenv("SERVER_PACKAGE", f"pgedge-postgresql{pg_major_version}-server")

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_server_package = os.getenv("DEB_SERVER_PACKAGE", f"pgedge-postgresql-{pg_major_version}")

# Additional configuration for component tests
check_extensions = os.getenv("CHECK_EXTENSIONS", "false").lower() == "true"
base_extensions = [ext.strip() for ext in os.getenv("BASE_EXTENSIONS", "").split(",") if ext.strip()]
components = [comp.strip() for comp in os.getenv("COMPONENTS", f"pgedge-pgbackrest").split(",") if comp.strip()]

# Decoupled components SBOM path
decoupled_sbom_path = os.getenv("DECOUPLED_COMPONENTS_SBOM", "")


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "pgbackrest_package": pgbackrest_package,
            "server_package": rhel_server_package,
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "pgbackrest_package": pgbackrest_package,
            "server_package": deb_server_package,
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
    """Step 2: Install pgedge-pgbackrest using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pkg = config["pgbackrest_package"]

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

    if not pgbackrest_version:
        pytest.skip("No PGEDGE_PGBACKREST_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pkg = config["pgbackrest_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {pkg} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, pkg, pgbackrest_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {pkg} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_binaries_stripped(container_name, container_type):
    """Step 4: Verify that pgBackRest binaries are stripped using file_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying pgBackRest binaries are stripped on {container_name} ({container_type}) ---")
    print(f"Binary directory: {pgbackrest_bin}")
    print(f"Binaries to check: {pgbackrest_stripped_bin}")

    # Use the file_management module to verify specific binaries are stripped
    try:
        success, details, message = file_management.verify_binaries_stripped(
            container=container,
            binary_path=pgbackrest_bin,
            container_name=container_name,
            binary_names=pgbackrest_stripped_bin
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
    actual_package = config["pgbackrest_package"]

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
            f"--signature-file pgbackrest-sbom.json.asc "
            f"--signer-file pgedge-rsa.pub "
            f"pgbackrest-sbom.json'",
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
            f"{_sq_sig_flag} pgbackrest-sbom.json.asc "
            f"pgbackrest-sbom.json'",
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
    """Step 5: Install PostgreSQL server for pgBackRest testing"""
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
    """Initialize PostgreSQL cluster with WAL archiving GUC parameters for pgBackRest"""
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

    # GUC parameters required for pgBackRest WAL archiving
    guc_parameters = {
        "archive_mode": "on",
        "archive_command": f"'pgbackrest --stanza={pgbackrest_stanza} archive-push %p'",
        "max_wal_senders": "3",
        "wal_level": "replica",
    }

    # Use the pg_server_management module to initialize cluster
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
    pguser_val = config["pguser"]

    print(f"\n--- Starting PostgreSQL server on {container_name} ---")

    # Use the pg_server_management module to start the server
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
    pguser_val = config["pguser"]

    print(f"\n--- Checking PostgreSQL connection on {container_name} ---")

    # Use the pg_server_management module to check connection
    try:
        success, version_output, message = pg_server_management.check_connection(
            container, pgbin, pgport, pguser_val
        )
        assert success, f"Connection check failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to check PostgreSQL connection: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgbackrest_configure_stanza(container_name, container_type):
    """Configure pgBackRest stanza in /etc/pgbackrest.conf"""
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

    print(f"\n--- Configuring pgBackRest stanza on {container_name} ({container_type}) ---")

    # Build pgbackrest.conf content
    pgbackrest_conf = (
        f"[global]\n"
        f"repo1-path={pgbackrest_repo_path}\n"
        f"repo1-retention-full=2\n"
        f"log-level-console=info\n"
        f"log-level-file=debug\n"
        f"\n"
        f"[{pgbackrest_stanza}]\n"
        f"pg1-path={pgdata}\n"
    )

    # Write pgbackrest.conf
    write_cmd = f"bash -c 'cat > {pgbackrest_config_file} << EOF\n{pgbackrest_conf}EOF'"
    exit_code, output = container.exec_run(write_cmd, user="root")
    assert exit_code == 0, f"Failed to write pgbackrest.conf: {output.decode()}"

    # Set ownership so postgres user can read the config
    exit_code, output = container.exec_run(
        f"chown {pguser_val}:{pguser_val} {pgbackrest_config_file}",
        user="root"
    )
    assert exit_code == 0, f"Failed to set ownership on pgbackrest.conf: {output.decode()}"

    # Ensure pgbackrest directories exist with correct ownership
    for dir_path in [pgbackrest_repo_path, pgbackrest_log_dir, pgbackrest_spool_dir]:
        exit_code, output = container.exec_run(
            f"bash -c 'mkdir -p {dir_path} && chown -R {pguser_val}:{pguser_val} {dir_path}'",
            user="root"
        )
        assert exit_code == 0, f"Failed to create/chown {dir_path}: {output.decode()}"

    # Verify config file was written
    exit_code, output = container.exec_run(f"cat {pgbackrest_config_file}", user="root")
    assert exit_code == 0, f"Failed to read pgbackrest.conf: {output.decode()}"
    print(f"pgbackrest.conf content:\n{output.decode().strip()}")
    print(f"✅ pgBackRest stanza configured successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgbackrest_stanza_create(container_name, container_type):
    """Create pgBackRest stanza"""
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

    print(f"\n--- Creating pgBackRest stanza on {container_name} ---")

    exit_code, output = container.exec_run(
        f"pgbackrest --stanza={pgbackrest_stanza} stanza-create",
        user=pguser_val
    )
    assert exit_code == 0, f"stanza-create failed: {output.decode()}"
    print(f"Output: {output.decode().strip()}")
    print(f"✅ pgBackRest stanza-create completed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgbackrest_check(container_name, container_type):
    """Run pgBackRest check to validate configuration"""
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

    print(f"\n--- Running pgBackRest check on {container_name} ---")

    exit_code, output = container.exec_run(
        f"pgbackrest --stanza={pgbackrest_stanza} check",
        user=pguser_val
    )
    assert exit_code == 0, f"pgbackrest check failed: {output.decode()}"
    print(f"Output: {output.decode().strip()}")
    print(f"✅ pgBackRest check passed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgbackrest_backup(container_name, container_type):
    """Run a full pgBackRest backup"""
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

    print(f"\n--- Running pgBackRest full backup on {container_name} ---")

    exit_code, output = container.exec_run(
        f"pgbackrest --stanza={pgbackrest_stanza} --type=full backup",
        user=pguser_val
    )
    assert exit_code == 0, f"pgbackrest backup failed: {output.decode()}"
    print(f"Output: {output.decode().strip()}")
    print(f"✅ pgBackRest full backup completed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgbackrest_info(container_name, container_type):
    """Run pgBackRest info to verify backup exists"""
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

    print(f"\n--- Running pgBackRest info on {container_name} ---")

    exit_code, output = container.exec_run(
        f"pgbackrest --stanza={pgbackrest_stanza} info",
        user=pguser_val
    )
    assert exit_code == 0, f"pgbackrest info failed: {output.decode()}"

    info_output = output.decode()
    print(f"Output:\n{info_output.strip()}")

    # Verify the info output contains the stanza and a backup entry
    assert pgbackrest_stanza in info_output, (
        f"Stanza '{pgbackrest_stanza}' not found in pgbackrest info output"
    )
    assert "full backup" in info_output.lower() or "full" in info_output.lower(), (
        f"No full backup found in pgbackrest info output"
    )
    print(f"✅ pgBackRest info shows backup data")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgbackrest_version(container_name, container_type):
    """Verify pgBackRest CLI version matches expected version"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not pgbackrest_version:
        pytest.skip("No PGEDGE_PGBACKREST_VERSION defined in env, skipping version check")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying pgBackRest CLI version on {container_name} ---")

    exit_code, output = container.exec_run(
        "pgbackrest version",
        user="root"
    )
    assert exit_code == 0, f"pgbackrest version failed: {output.decode()}"

    version_output = output.decode().strip()
    print(f"pgBackRest version output: {version_output}")

    assert pgbackrest_version in version_output, (
        f"Version mismatch!\n"
        f"Expected: {pgbackrest_version}\n"
        f"Got: {version_output}"
    )
    print(f"✅ pgBackRest version verified: {pgbackrest_version}")


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
    pguser_val = config["pguser"]

    print(f"\n--- Stopping PostgreSQL server on {container_name} ---")

    # Use the pg_server_management module to stop the server
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
    """Uninstall pgbackrest package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pkg = config["pgbackrest_package"]

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
    pguser_val = config["pguser"]

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")

    # Use the machine_cleanup module to perform comprehensive cleanup
    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata=pgdata, pguser=pguser_val
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        # Display cleanup details
        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
        if cleanup_summary["data_directory_removed"]:
            print(f"   Data directory removed: {pgdata}")
        if cleanup_summary["user_removed"]:
            print(f"   User removed: {pguser_val}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")
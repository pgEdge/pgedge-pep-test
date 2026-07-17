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
supautils_version = os.getenv("PGEDGE_SUPAUTILS_VERSION", "3.2.2")

# supautils ships no standalone binary — only the shared library supautils.so.
# Functional config: roles/memberships protected by supautils for the functional
# tests below. These are written into postgresql.conf at cluster init.
supautils_reserved_role = "reserved_role"
supautils_reserved_membership = "pg_read_server_files"

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")
# Coupled component — package name carries the PostgreSQL major version suffix.
rhel_supautils_package = os.getenv("SUPAUTILS_PACKAGE", f"pgedge-supautils_{pg_major_version}")
rhel_bundled_files = [f for f in os.getenv("SUPAUTILS_BUNDLED_FILES", "").split(",") if f]

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_supautils_package = os.getenv("DEB_SUPAUTILS_PACKAGE", f"pgedge-postgresql-{pg_major_version}-supautils")
deb_bundled_files = [f for f in os.getenv("DEB_SUPAUTILS_BUNDLED_FILES", "").split(",") if f]


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "supautils_package": rhel_supautils_package,
            "bundled_files": rhel_bundled_files,
            "lib_path": f"{rhel_pg_path}/lib/supautils.so"
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "supautils_package": deb_supautils_package,
            "bundled_files": deb_bundled_files,
            "lib_path": f"{deb_pg_path}/lib/supautils.so"
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
    """Step 2: Install pgedge-supautils using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    supautils_package = config["supautils_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing {supautils_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to install the package
    try:
        success, platform, message = package_management.install_package(container, supautils_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {supautils_package}: {str(e)}")


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
    supautils_package = config["supautils_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Upgrading {supautils_package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    # Use the package_management module to upgrade the package
    try:
        success, platform, message = package_management.upgrade_package(container, supautils_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{supautils_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {supautils_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not supautils_version:
        pytest.skip("No PGEDGE_SUPAUTILS_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    supautils_package = config["supautils_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {supautils_package} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, supautils_package, supautils_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {supautils_package} version: {str(e)}")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files for supautils match expected files

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
    actual_package = config["supautils_package"]

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
            project_root=project_root,
            pg_major_version=pg_major_version
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
            f"--signature-file supautils-sbom.json.asc "
            f"--signer-file pgedge-rsa.pub "
            f"supautils-sbom.json'",
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
            f"{_sq_sig_flag} supautils-sbom.json.asc "
            f"supautils-sbom.json'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_license_file(container_name, container_type):
    """Verify the bundled license file is installed.

    RHEL ships /usr/share/licenses/<pkg>/LICENSE; Debian ships the license as
    /usr/share/doc/<pkg>/copyright (Debian packaging convention).
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    package = config["supautils_package"]

    if container_type == "rhel":
        license_path = f"/usr/share/licenses/{package}/LICENSE"
    else:  # deb
        license_path = f"/usr/share/doc/{package}/copyright"

    print(f"\n--- Verifying license file {license_path} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(["test", "-f", license_path], user="root")
    assert exit_code == 0, f"License file not found at {license_path}"
    print(f"✅ License file present at {license_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_readme_file(container_name, container_type):
    """Verify the bundled README file is installed.

    RHEL ships /usr/share/doc/<pkg>/README.md; Debian ships it gzipped as
    /usr/share/doc/<pkg>/README.md.gz.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    package = config["supautils_package"]

    if container_type == "rhel":
        readme_path = f"/usr/share/doc/{package}/README.md"
    else:  # deb
        readme_path = f"/usr/share/doc/{package}/README.md.gz"

    print(f"\n--- Verifying README file {readme_path} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(["test", "-f", readme_path], user="root")
    assert exit_code == 0, f"README file not found at {readme_path}"
    print(f"✅ README file present at {readme_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_init_cluster(container_name, container_type):
    """Initialize PostgreSQL cluster with supautils preloaded using pg_server_management module"""
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

    # supautils is a shared_preload_libraries module (not a CREATE EXTENSION);
    # preloading it verifies the supautils.so loads cleanly at server start.
    # The supautils.* GUCs configure the protections exercised by the functional
    # tests below (reserved roles and reserved memberships).
    guc_parameters = {
        "shared_preload_libraries": "'supautils'",
        "supautils.reserved_roles": f"'{supautils_reserved_role}'",
        "supautils.reserved_memberships": f"'{supautils_reserved_membership}'"
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
def test_lib_stripped(container_name, container_type):
    """Verify that the supautils shared library is a stripped ELF binary.

    supautils ships supautils.so (no standalone binary):
      RHEL: /usr/pgsql-<pg>/lib/supautils.so
      Deb:  /usr/lib/postgresql/<pg>/lib/supautils.so
    Runs 'file <lib>' and asserts the output contains 'stripped'.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    lib_path = config["lib_path"]

    print(f"\n--- Checking ELF strip status of {lib_path} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"file {lib_path} 2>&1"],
        user="root",
    )
    assert exit_code == 0, f"'file {lib_path}' failed: {output.decode().strip()}"

    file_output = output.decode().strip()
    print(f"   Output: {file_output}")

    assert "stripped" in file_output.lower(), (
        f"Library {lib_path} does not appear to be stripped.\n"
        f"'file' output: {file_output}"
    )
    print(f"✅ {lib_path} is stripped")


# ============================================================================
# Functional tests — exercise core supautils protections.
# Mirrors the upstream test suite (test/sql/reserved_roles.sql,
# reserved_memberships.sql) at https://github.com/supabase/supautils
# ============================================================================

def _run_sql(container, config, sql, role=None):
    """Run SQL via psql as the bootstrap superuser, optionally SET ROLE first
    to drop down to a non-superuser. Returns (exit_code, combined_output)."""
    pgbin = config["pgbin"]
    pguser = config["pguser"]
    prefix = f"SET ROLE {role}; " if role else ""
    exit_code, output = container.exec_run(
        ["bash", "-c",
         f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -v ON_ERROR_STOP=1 "
         f"-c \"{prefix}{sql}\" 2>&1"],
        user=pguser,
    )
    return exit_code, output.decode()


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_supautils_guc_active(container_name, container_type):
    """Functional: supautils.so is loaded and its custom GUCs are registered.

    Confirms the module initialized by reading back the configured
    supautils.reserved_roles GUC.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)

    print(f"\n--- Verifying supautils GUCs are active on {container_name} ---")

    exit_code, out = _run_sql(container, config, "SHOW supautils.reserved_roles;")
    assert exit_code == 0, f"Failed to read supautils.reserved_roles (module not loaded?):\n{out}"
    assert supautils_reserved_role in out, (
        f"Expected supautils.reserved_roles to contain '{supautils_reserved_role}', got:\n{out}"
    )
    print(f"✅ supautils loaded; reserved_roles = {out.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_reserved_roles(container_name, container_type):
    """Functional: a non-superuser CREATEROLE user cannot drop/alter a role
    listed in supautils.reserved_roles, while a superuser can.

    Mirrors test/sql/reserved_roles.sql.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    rrole = supautils_reserved_role
    creator = "supautils_rolecreator"

    print(f"\n--- Testing supautils.reserved_roles protection on {container_name} ---")

    # Setup as superuser: a reserved role and a CREATEROLE non-superuser that is
    # granted admin on it (so only supautils — not stock PG — blocks the drop).
    rc, out = _run_sql(
        container, config,
        f"DROP ROLE IF EXISTS {creator}; DROP ROLE IF EXISTS {rrole}; "
        f"CREATE ROLE {rrole}; "
        f"CREATE ROLE {creator} WITH CREATEROLE LOGIN; "
        f"GRANT {rrole} TO {creator} WITH ADMIN OPTION;"
    )
    assert rc == 0, f"Setup of reserved-role fixtures failed:\n{out}"

    # Non-superuser attempts a DROP of the reserved role -> supautils must block it
    rc, out = _run_sql(container, config, f"DROP ROLE {rrole};", role=creator)
    assert rc != 0, f"Expected DROP of reserved role '{rrole}' to be blocked, but it succeeded:\n{out}"
    assert "reserved" in out.lower(), (
        f"Expected a supautils 'reserved' error dropping '{rrole}', got:\n{out}"
    )
    print(f"✅ Non-superuser blocked from dropping reserved role: {out.strip().splitlines()[-1]}")

    # Non-superuser attempts to RENAME the reserved role -> also blocked
    rc, out = _run_sql(container, config, f"ALTER ROLE {rrole} RENAME TO {rrole}_x;", role=creator)
    assert rc != 0, f"Expected ALTER RENAME of reserved role to be blocked, but it succeeded:\n{out}"
    assert "reserved" in out.lower(), f"Expected 'reserved' error on rename, got:\n{out}"
    print(f"✅ Non-superuser blocked from renaming reserved role")

    # Superuser bypasses the restriction
    rc, out = _run_sql(container, config, f"ALTER ROLE {rrole} CONNECTION LIMIT 5;")
    assert rc == 0, f"Superuser should bypass reserved-role protection, but failed:\n{out}"
    print(f"✅ Superuser bypass confirmed (ALTER ROLE succeeded)")

    # Cleanup
    _run_sql(container, config, f"DROP ROLE IF EXISTS {creator}; DROP ROLE IF EXISTS {rrole};")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_reserved_memberships(container_name, container_type):
    """Functional: a non-superuser CREATEROLE user cannot GRANT a membership
    listed in supautils.reserved_memberships.

    Mirrors test/sql/reserved_memberships.sql.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    membership = supautils_reserved_membership  # e.g. pg_read_server_files (a predefined role)
    creator = "supautils_memcreator"
    target = "supautils_memtarget"

    print(f"\n--- Testing supautils.reserved_memberships protection on {container_name} ---")

    # Setup as superuser: a CREATEROLE non-superuser granted admin on the reserved
    # membership (so stock PG would allow the grant; only supautils blocks it).
    rc, out = _run_sql(
        container, config,
        f"DROP ROLE IF EXISTS {creator}; DROP ROLE IF EXISTS {target}; "
        f"CREATE ROLE {creator} WITH CREATEROLE LOGIN; "
        f"CREATE ROLE {target}; "
        f"GRANT {membership} TO {creator} WITH ADMIN OPTION;"
    )
    assert rc == 0, f"Setup of reserved-membership fixtures failed:\n{out}"

    # Non-superuser attempts to grant the reserved membership -> supautils blocks it
    rc, out = _run_sql(container, config, f"GRANT {membership} TO {target};", role=creator)
    assert rc != 0, (
        f"Expected GRANT of reserved membership '{membership}' to be blocked, but it succeeded:\n{out}"
    )
    assert "reserved" in out.lower(), (
        f"Expected a supautils 'reserved' error granting '{membership}', got:\n{out}"
    )
    print(f"✅ Non-superuser blocked from granting reserved membership: {out.strip().splitlines()[-1]}")

    # Cleanup
    _run_sql(
        container, config,
        f"REVOKE {membership} FROM {creator}; "
        f"DROP ROLE IF EXISTS {creator}; DROP ROLE IF EXISTS {target};"
    )


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
def test_package_uninstall(container_name, container_type):
    """Uninstall supautils package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    supautils_package = config["supautils_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Uninstalling {supautils_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, supautils_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {supautils_package}: {str(e)}")


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

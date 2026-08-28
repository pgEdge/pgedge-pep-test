"""Component tests for SafeSession.

A coupled component — one package per platform, tied to the PostgreSQL major
version:

    RHEL: pgedge-safesession_{PG}
    DEB:  pgedge-postgresql-{PG}-safesession

It ships no standalone binary; the payload is the pgedge_safesession.so shared
library plus the pgedge_safesession extension. Several paths differ between the
packaging formats and are therefore resolved per platform rather than derived:

  * LICENSE — RHEL: /usr/share/licenses/<pkg>/LICENCE.md  (British spelling)
              DEB:  /usr/share/doc/<pkg>/copyright        (Debian convention)
  * README  — same basename on both, different parent directory
  * SBOM    — <pg_path>/sbom/pgedge-safesession-sbom.json

As of the 1.0 GA release the package version and the extension's
default_version are both '1.0'. They are still read from separate env vars
(PGEDGE_SAFESESSION_<PG>_VERSION and SAFESESSION_EXTENSION_VERSION) because
they are independent values that happened to converge at GA.
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
pg_major_version = os.getenv("PG_MAJOR_VERSION", "18")
safesession_version = os.getenv(f"PGEDGE_SAFESESSION_{pg_major_version}_VERSION", "1.0")

# Extension version as reported by \dx — this is the .control file's
# default_version and need not equal the package version.
safesession_extension_version = os.getenv("SAFESESSION_EXTENSION_VERSION", "1.0")

# SafeSession ships no standalone binary, so the binary tests stay disabled.
component_binary = os.getenv("COMPONENT_BINARY", "")
component_version = os.getenv("COMPONENT_BINARY_VERSION", "")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")
rhel_safesession_package = os.getenv("SAFESESSION_PACKAGE", f"pgedge-safesession_{pg_major_version}")
rhel_safesession_lib = os.getenv("RHEL_SAFESESSION_LIB", f"/usr/pgsql-{pg_major_version}/lib/")

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_safesession_package = os.getenv("DEB_SAFESESSION_PACKAGE", f"pgedge-postgresql-{pg_major_version}-safesession")
deb_safesession_lib = os.getenv("DEB_SAFESESSION_LIB", f"/usr/lib/postgresql/{pg_major_version}/lib/")

# Shared library shipped by the package
safesession_stripped_lib = os.getenv("SAFESESSION_STRIPPED_LIB", "pgedge_safesession.so")

# SBOM basename, found under <pg_path>/sbom/
safesession_sbom = os.getenv("SAFESESSION_SBOM", "pgedge-safesession-sbom.json")

# LICENSE / README paths. Derived here rather than configured, because each
# path follows its packaging format's own fixed convention:
#   RHEL license: /usr/share/licenses/<pkg>/LICENCE.md  (British spelling, as
#                 actually shipped — not the usual LICENSE.md)
#   DEB license:  /usr/share/doc/<pkg>/copyright        (Debian policy)
#   README:       /usr/share/doc/<pkg>/README.md on both platforms
rhel_license_path = f"/usr/share/licenses/{rhel_safesession_package}/LICENCE.md"
deb_license_path = f"/usr/share/doc/{deb_safesession_package}/copyright"
rhel_readme_path = f"/usr/share/doc/{rhel_safesession_package}/README.md"
deb_readme_path = f"/usr/share/doc/{deb_safesession_package}/README.md"

# Additional configuration for extension tests
check_extensions = os.getenv("CHECK_EXTENSIONS", "true").lower() == "true"
base_extensions = [
    ext.strip() for ext in os.getenv("SAFESESSION_EXTENSIONS", "pgedge_safesession").split(",")
    if ext.strip()
]


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "pg_path": rhel_pg_path.rstrip('/'),
            "safesession_package": rhel_safesession_package,
            "lib_dir": rhel_safesession_lib.rstrip('/'),
            "license_path": rhel_license_path,
            "readme_path": rhel_readme_path,
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "pg_path": deb_pg_path.rstrip('/'),
            "safesession_package": deb_safesession_package,
            "lib_dir": deb_safesession_lib.rstrip('/'),
            "license_path": deb_license_path,
            "readme_path": deb_readme_path,
        }


def _get_container(container_name):
    """Fetch a running container, skipping the test when unavailable."""
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

    try:
        success, platform, message = configure_repository.configure_pgedge_repository(container, repo)
        assert success, f"Repository configuration failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to configure repository: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_install(container_name, container_type):
    """Step 2: Install the safesession package using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    safesession_package = config["safesession_package"]

    container = _get_container(container_name)

    print(f"\n--- Installing {safesession_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.install_package(container, safesession_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {safesession_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_upgrade(container_name, container_type):
    """Upgrade the safesession package if UPGRADE=true"""
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    safesession_package = config["safesession_package"]

    container = _get_container(container_name)

    print(f"\n--- Upgrading {safesession_package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    try:
        success, platform, message = package_management.upgrade_package(container, safesession_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{safesession_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {safesession_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not safesession_version:
        pytest.skip("No SAFESESSION version defined in env, skipping version check")

    config = get_container_config(container_type)
    safesession_package = config["safesession_package"]

    container = _get_container(container_name)

    print(f"\n--- Verifying {safesession_package} version on {container_name} ({container_type}) ---")

    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, safesession_package, safesession_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {safesession_package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files match expected-output/{rpm,deb}/safesession.

    Both package names reduce to the short name 'safesession', so the platform
    directory alone distinguishes the two expected file lists.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    actual_package = config["safesession_package"]

    project_root = Path(__file__).parent.parent

    try:
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=actual_package,
            package_name=actual_package,
            project_root=project_root,
            pg_major_version=pg_major_version,
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
def test_verify_shared_library(container_name, container_type):
    """Verify pgedge_safesession.so is present in the PostgreSQL lib directory.

    SafeSession ships no standalone binary, so the shared library is the only
    compiled artifact to check.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    lib_path = f"{config['lib_dir']}/{safesession_stripped_lib}"

    print(f"\n--- Verifying shared library {lib_path} on {container_name} ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"test -s {lib_path} && file {lib_path}"],
        user="root",
    )
    assert exit_code == 0, f"Shared library missing or empty at {lib_path}: {output.decode().strip()}"

    file_output = output.decode().strip()
    print(f"   {file_output}")
    assert "ELF" in file_output, f"{lib_path} is not an ELF shared object: {file_output}"
    print(f"✅ {lib_path} present")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_library_stripped(container_name, container_type):
    """Verify the shared library was stripped of debug symbols at build time."""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    lib_path = f"{config['lib_dir']}/{safesession_stripped_lib}"

    print(f"\n--- Checking ELF strip status of {lib_path} on {container_name} ---")

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


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_license_file(container_name, container_type):
    """Verify the LICENSE file ships at the platform's expected path.

    RHEL uses /usr/share/licenses/<pkg>/LICENCE.md (British spelling as shipped),
    DEB uses Debian's /usr/share/doc/<pkg>/copyright.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    license_path = config["license_path"]
    package = config["safesession_package"]

    print(f"\n--- Verifying LICENSE for {package} at {license_path} on {container_name} ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"test -s {license_path} && echo PRESENT"],
        user="root",
    )
    assert exit_code == 0 and b"PRESENT" in output, (
        f"LICENSE file missing or empty for {package} at {license_path}: "
        f"{output.decode().strip()}"
    )
    print(f"✅ LICENSE present for {package} at {license_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_readme_file(container_name, container_type):
    """Verify the README ships at the platform's expected path."""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    readme_path = config["readme_path"]
    package = config["safesession_package"]

    print(f"\n--- Verifying README for {package} at {readme_path} on {container_name} ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"test -s {readme_path} && echo PRESENT"],
        user="root",
    )
    assert exit_code == 0 and b"PRESENT" in output, (
        f"README file missing or empty for {package} at {readme_path}: "
        f"{output.decode().strip()}"
    )
    print(f"✅ README present for {package} at {readme_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_sbom(container_name, container_type):
    """Verify the SafeSession SBOM's detached signature.

    The package ships pgedge-safesession-sbom.json plus a .asc signature under
    the PostgreSQL tree's sbom/ directory.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    sbom_dir = f"{config['pg_path']}/sbom"

    print(f"\n--- Verifying SBOM {sbom_dir}/{safesession_sbom} on {container_name} ({container_type}) ---")

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
        f"{_sq_sig_flag} {safesession_sbom}.asc "
        f"{safesession_sbom}'",
        user="root",
    )
    output_str = output.decode().replace('\xa0', ' ')
    assert exit_code == 0, f"SBOM verification failed: {output_str}"
    assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
        f"Expected a good/authenticated signature, got:\n{output_str}"
    print(f"✅ SBOM signature verified on {container_name} ({container_type})")
    print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_init_cluster(container_name, container_type):
    """Initialize a PostgreSQL cluster preloading pgedge_safesession"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Initializing cluster on {container_name} ---")

    guc_parameters = {
        "shared_preload_libraries": "'pgedge_safesession'",
    }

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

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Starting PostgreSQL server on {container_name} ---")

    try:
        success, server_output, message = pg_server_management.start_server(
            container, pgbin, pgdata, pgport, pguser
        )
        assert success, f"Server start failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        # Surface the server log — a bad shared_preload_libraries entry only
        # shows up there.
        _, log_output = container.exec_run(f"cat {pgdata}/logfile", user=pguser)
        pg_log = log_output.decode().strip() if log_output else "(log unavailable)"
        pytest.fail(
            f"Failed to start PostgreSQL server: {str(e)}\n\n"
            f"--- PostgreSQL log ({pgdata}/logfile) ---\n{pg_log}"
        )


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_check_connection(container_name, container_type):
    """Check PostgreSQL connection using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Checking PostgreSQL connection on {container_name} ---")

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
    """Create the pgedge_safesession extension"""
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

    # Normalize extension (quote if it contains a dash)
    normalized_ext = f'"{extension}"' if "-" in extension else extension

    print(f"\n--- Creating extension {normalized_ext} in {container_name} ---")

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
    reported version column matches SAFESESSION_EXTENSION_VERSION — the
    .control file's default_version, which is tracked separately from the
    package version.
    """
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    extension = extension.strip()

    if not container_name or not extension:
        pytest.skip("Invalid container or extension")

    if not safesession_extension_version:
        pytest.skip("No SAFESESSION_EXTENSION_VERSION defined in env")

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

    assert safesession_extension_version in installed_version, (
        f"Extension '{extension}' version mismatch: "
        f"expected '{safesession_extension_version}', got '{installed_version}'"
    )
    print(
        f"✅ Extension '{extension}' version {installed_version} "
        f"matches expected {safesession_extension_version}"
    )


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_stop_server(container_name, container_type):
    """Stop PostgreSQL server using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Stopping PostgreSQL server on {container_name} ---")

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
    """Uninstall the safesession package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    safesession_package = config["safesession_package"]

    container = _get_container(container_name)

    print(f"\n--- Uninstalling {safesession_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.uninstall_package(container, safesession_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {safesession_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Full cleanup using machine_cleanup module: remove all pgedge packages + leftover data"""
    if skip_cleanup:
        pytest.skip("Skipping cleanup: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

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

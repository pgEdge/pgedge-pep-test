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


# ---------------------------------------------------------------------------
# Helpers for the functional (behavioural) SafeSession tests below.
#
# SafeSession enforces read-only sessions for the roles named in the session
# GUC pgedge_safesession.roles. The restriction anchors on the session user, so
# the scenarios below set that GUC and then switch identity with SET SESSION
# AUTHORIZATION — all inside ONE psql session, because a plain SET is
# session-scoped and would be lost across separate psql -c invocations.
# ---------------------------------------------------------------------------

def _psql_script(container, pgbin, pguser, sql, dbname="postgres"):
    """Run a multi-statement SQL script in ONE psql session and return the
    combined stdout+stderr as text.

    State such as SET pgedge_safesession.roles and SET SESSION AUTHORIZATION
    persists across the statements of a single session. ON_ERROR_STOP is off so
    an expected failure (e.g. a blocked INSERT) does not abort the rest of the
    script and its ERROR line can be asserted from the returned output.
    """
    cmd = (
        f"{pgbin}/psql -X -q -p {pgport} -U {pguser} -d {dbname} "
        f"-v ON_ERROR_STOP=0 <<'EOSQL'\n{sql}\nEOSQL\n"
    )
    _, output = container.exec_run(["bash", "-c", cmd], user=pguser)
    return output.decode()


def _safesession_cleanup(container, pgbin, pguser, tables=(), roles=()):
    """Best-effort teardown of the roles and tables a functional test created.

    DROP OWNED BY clears schema-level grants (e.g. GRANT CREATE ON SCHEMA
    public) that would otherwise block DROP ROLE. Runs with ON_ERROR_STOP off,
    so a missing object is harmless.
    """
    stmts = []
    if tables:
        stmts.append("DROP TABLE IF EXISTS " + ", ".join(tables) + " CASCADE;")
    for role in roles:
        stmts.append(f"DROP OWNED BY {role};")
        stmts.append(f"DROP ROLE IF EXISTS {role};")
    if stmts:
        _psql_script(container, pgbin, pguser, "\n".join(stmts))


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
    reported version column equals SAFESESSION_EXTENSION_VERSION exactly — the
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

    # Exact match, not a substring check. Both values name the same thing — the
    # .control file's default_version — so there is no formatting gap to absorb,
    # and a substring test would let a longer version slip through: with the GA
    # value of '1.0', "in" would happily pass against an installed '1.0.1'.
    expected_version = safesession_extension_version.strip()
    assert installed_version == expected_version, (
        f"Extension '{extension}' version mismatch: "
        f"expected '{expected_version}', got '{installed_version}'"
    )
    print(
        f"✅ Extension '{extension}' version {installed_version} "
        f"matches expected {safesession_extension_version}"
    )


# ============================================================================
# Functional tests — exercise SafeSession's actual read-only enforcement.
#
# These require the running server (preloaded with pgedge_safesession via
# shared_preload_libraries) and the created extension, so they run after
# test_extension_version and before the server is stopped. Each test is
# self-contained: it creates its own role(s)/table, exercises one behaviour in a
# single psql session, and tears its objects down in a finally block.
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_safesession_restricted_role_blocks_writes(container_name, container_type):
    """A restricted role may read but cannot INSERT/UPDATE/DELETE, and the
    blocked writes leave the data unchanged."""
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)
    config = get_container_config(container_type)
    pgbin, pguser = config["pgbin"], config["pguser"]

    role, table = "pep_ss_readonly", "pep_ss_tbl"

    print(f"\n--- SafeSession: restricted role is read-only on {container_name} ---")

    try:
        output = _psql_script(container, pgbin, pguser, f"""
DROP TABLE IF EXISTS {table} CASCADE;
DROP ROLE IF EXISTS {role};
CREATE ROLE {role};
CREATE TABLE {table} (id int, val text);
INSERT INTO {table} VALUES (1, 'alpha'), (2, 'beta');
GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role};
SET pgedge_safesession.roles = '{role}';
SET SESSION AUTHORIZATION {role};
SELECT val FROM {table} ORDER BY id;
INSERT INTO {table} VALUES (3, 'gamma');
UPDATE {table} SET val = 'changed' WHERE id = 1;
DELETE FROM {table} WHERE id = 2;
RESET SESSION AUTHORIZATION;
SELECT 'FINAL_ROWS=' || count(*) FROM {table};
""")
        print(output)

        # Reads work.
        assert "alpha" in output and "beta" in output, \
            f"Restricted role could not read its table:\n{output}"
        # Every write is blocked by SafeSession (not the core read-only floor).
        for cmd in ("INSERT", "UPDATE", "DELETE"):
            assert f"cannot execute {cmd} in a read-only session" in output, \
                f"Expected {cmd} to be blocked with the SafeSession message:\n{output}"
        # The blocked writes changed nothing: still the original two rows.
        assert "FINAL_ROWS=2" in output, \
            f"Blocked writes must not alter the data (expected 2 rows):\n{output}"
        print("✅ Restricted role is read-only; blocked writes left data intact")
    finally:
        _safesession_cleanup(container, pgbin, pguser, tables=[table], roles=[role])


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_safesession_blocks_ddl(container_name, container_type):
    """A restricted role cannot run DDL (CREATE TABLE is rejected)."""
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)
    config = get_container_config(container_type)
    pgbin, pguser = config["pgbin"], config["pguser"]

    role, table = "pep_ss_ddl", "pep_ss_ddl_tbl"

    print(f"\n--- SafeSession: DDL is blocked for a restricted role on {container_name} ---")

    try:
        output = _psql_script(container, pgbin, pguser, f"""
DROP TABLE IF EXISTS {table} CASCADE;
DROP ROLE IF EXISTS {role};
CREATE ROLE {role};
GRANT CREATE ON SCHEMA public TO {role};
SET pgedge_safesession.roles = '{role}';
SET SESSION AUTHORIZATION {role};
CREATE TABLE {table} (id int);
RESET SESSION AUTHORIZATION;
SELECT 'DDL_TABLE_EXISTS=' || count(*) FROM pg_class WHERE relname = '{table}';
""")
        print(output)

        assert "cannot execute CREATE TABLE in a read-only session" in output, \
            f"Expected CREATE TABLE to be blocked by SafeSession:\n{output}"
        assert "DDL_TABLE_EXISTS=0" in output, \
            f"Blocked DDL must not create the table:\n{output}"
        print("✅ CREATE TABLE blocked for restricted role; no table created")
    finally:
        _safesession_cleanup(container, pgbin, pguser, tables=[table], roles=[role])


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_safesession_is_restricted_status(container_name, container_type):
    """pgedge_safesession_is_restricted() reports false for the superuser session
    and true once the session user is a listed role."""
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)
    config = get_container_config(container_type)
    pgbin, pguser = config["pgbin"], config["pguser"]

    role = "pep_ss_status"

    print(f"\n--- SafeSession: is_restricted() status function on {container_name} ---")

    try:
        output = _psql_script(container, pgbin, pguser, f"""
DROP ROLE IF EXISTS {role};
CREATE ROLE {role};
SET pgedge_safesession.roles = '{role}';
SELECT 'SUPER_RESTRICTED=' || pgedge_safesession_is_restricted()::text;
SET SESSION AUTHORIZATION {role};
SELECT 'ROLE_RESTRICTED=' || pgedge_safesession_is_restricted()::text;
RESET SESSION AUTHORIZATION;
""")
        print(output)

        assert "SUPER_RESTRICTED=false" in output, \
            f"Superuser session must not be reported as restricted:\n{output}"
        assert "ROLE_RESTRICTED=true" in output, \
            f"A listed role's session must be reported as restricted:\n{output}"
        print("✅ is_restricted() is false for superuser, true for the listed role")
    finally:
        _safesession_cleanup(container, pgbin, pguser, roles=[role])


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_safesession_superuser_not_restricted(container_name, container_type):
    """A superuser is never restricted, even when listed in the roles GUC."""
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)
    config = get_container_config(container_type)
    pgbin, pguser = config["pgbin"], config["pguser"]

    role, table = "pep_ss_super", "pep_ss_super_tbl"

    print(f"\n--- SafeSession: superuser bypasses restriction on {container_name} ---")

    try:
        output = _psql_script(container, pgbin, pguser, f"""
DROP TABLE IF EXISTS {table} CASCADE;
DROP ROLE IF EXISTS {role};
CREATE ROLE {role} SUPERUSER;
CREATE TABLE {table} (id int);
SET pgedge_safesession.roles = '{role}';
SET SESSION AUTHORIZATION {role};
INSERT INTO {table} VALUES (1);
RESET SESSION AUTHORIZATION;
SELECT 'SUPER_ROWS=' || count(*) FROM {table};
""")
        print(output)

        assert "cannot execute INSERT in a read-only session" not in output, \
            f"A superuser must not be blocked by SafeSession:\n{output}"
        assert "SUPER_ROWS=1" in output, \
            f"Superuser INSERT should have succeeded:\n{output}"
        print("✅ Superuser is never restricted; INSERT succeeded")
    finally:
        _safesession_cleanup(container, pgbin, pguser, tables=[table], roles=[role])


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_safesession_set_role_cannot_bypass(container_name, container_type):
    """SET ROLE to an unrestricted role does not lift the restriction: it anchors
    on the session user, not the current role."""
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)
    config = get_container_config(container_type)
    pgbin, pguser = config["pgbin"], config["pguser"]

    restricted, unrestricted, table = "pep_ss_byp_ro", "pep_ss_byp_rw", "pep_ss_byp_tbl"

    print(f"\n--- SafeSession: SET ROLE cannot bypass restriction on {container_name} ---")

    try:
        output = _psql_script(container, pgbin, pguser, f"""
DROP TABLE IF EXISTS {table} CASCADE;
DROP ROLE IF EXISTS {restricted};
DROP ROLE IF EXISTS {unrestricted};
CREATE ROLE {unrestricted};
CREATE ROLE {restricted} IN ROLE {unrestricted};
CREATE TABLE {table} (id int);
GRANT INSERT ON {table} TO {unrestricted};
SET pgedge_safesession.roles = '{restricted}';
SET SESSION AUTHORIZATION {restricted};
SET ROLE {unrestricted};
INSERT INTO {table} VALUES (1);
RESET ROLE;
RESET SESSION AUTHORIZATION;
SELECT 'BYPASS_ROWS=' || count(*) FROM {table};
""")
        print(output)

        assert "cannot execute INSERT in a read-only session" in output, \
            f"SET ROLE to an unrestricted role must not bypass SafeSession:\n{output}"
        assert "BYPASS_ROWS=0" in output, \
            f"The bypass attempt must not have written any row:\n{output}"
        print("✅ SET ROLE did not bypass the restriction; no row written")
    finally:
        _safesession_cleanup(
            container, pgbin, pguser, tables=[table], roles=[restricted, unrestricted]
        )


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_safesession_unlisted_role_is_not_restricted(container_name, container_type):
    """A role that is NOT in pgedge_safesession.roles keeps full write access —
    the extension restricts only the roles it is told to."""
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)
    config = get_container_config(container_type)
    pgbin, pguser = config["pgbin"], config["pguser"]

    role, table = "pep_ss_free", "pep_ss_free_tbl"

    print(f"\n--- SafeSession: unlisted role keeps write access on {container_name} ---")

    try:
        output = _psql_script(container, pgbin, pguser, f"""
DROP TABLE IF EXISTS {table} CASCADE;
DROP ROLE IF EXISTS {role};
CREATE ROLE {role};
CREATE TABLE {table} (id int);
GRANT INSERT ON {table} TO {role};
SET pgedge_safesession.roles = 'pep_ss_some_other_role';
SET SESSION AUTHORIZATION {role};
INSERT INTO {table} VALUES (1);
RESET SESSION AUTHORIZATION;
SELECT 'FREE_ROWS=' || count(*) FROM {table};
""")
        print(output)

        assert "cannot execute INSERT in a read-only session" not in output, \
            f"An unlisted role must not be restricted:\n{output}"
        assert "FREE_ROWS=1" in output, \
            f"Unlisted role's INSERT should have succeeded:\n{output}"
        print("✅ Unlisted role retains write access; INSERT succeeded")
    finally:
        _safesession_cleanup(container, pgbin, pguser, tables=[table], roles=[role])


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

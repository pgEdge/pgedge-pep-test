import os
import re
import sys
from pathlib import Path

import pytest
import docker
from dotenv import load_dotenv

# Add the parent directory to sys.path to import from aspects
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from aspects import configure_repository, package_management, machine_cleanup, machine_prereq_setup, container_management

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
pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
pgadmin4_version = os.getenv("PGEDGE_PGADMIN4_VERSION", "9.8")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# pgAdmin4 configuration
pgadmin4_package = os.getenv("PGADMIN_PACKAGES", "pgedge-pgadmin4")
pgadmin4_install_dir = "/usr/pgadmin4"
pgadmin4_bin = f"{pgadmin4_install_dir}/bin/pgadmin4"
pgadmin4_setup_web = f"{pgadmin4_install_dir}/bin/setup-web.sh"

def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pguser": rhel_pguser,
            "pgadmin4_package": pgadmin4_package,
        }
    else:  # deb
        return {
            "pguser": deb_pguser,
            "pgadmin4_package": pgadmin4_package,
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
    """Step 2: Install pgedge-pgadmin4 using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pkg = config["pgadmin4_package"]

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
def test_component_upgrade(container_name, container_type):
    """Upgrade component package if UPGRADE=true"""
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pkg = config["pgadmin4_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Upgrading {pkg} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    # Use the package_management module to upgrade the package
    try:
        success, platform, message = package_management.upgrade_package(container, pkg)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{pkg} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {pkg}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not pgadmin4_version:
        pytest.skip("No PGEDGE_PGADMIN4_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pkg = config["pgadmin4_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {pkg} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, pkg, pgadmin4_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {pkg} version: {str(e)}")


# Regex to detect random/hash-based filenames (8+ contiguous hex chars as the stem
# or embedded between dots, e.g. "a1b2c3d4.map" or "main.a1b2c3d4.chunk.js")
_RANDOM_NAME_RE = re.compile(r'(?:^|(?<=/))[a-f0-9]{8,}(?:\.|$)|(?<=\.)[a-f0-9]{8,}(?=\.)', re.IGNORECASE)

# Regex to detect Python-version-specific paths inside the venv
# e.g. /usr/pgadmin4/venv/lib/python3.13/... or /usr/pgadmin4/venv/bin/pip3.13
_PYTHON_VERSION_RE = re.compile(r'/python3\.\d+(?:[/_.]|$)', re.IGNORECASE)


def _has_random_name(file_path: str) -> bool:
    """Return True if the file's basename looks like it contains a random hash segment."""
    basename = file_path.rstrip('/').rsplit('/', 1)[-1]
    return bool(_RANDOM_NAME_RE.search(basename))


def _has_python_version_path(file_path: str) -> bool:
    """Return True if the path contains a Python minor-version component (e.g. python3.13).

    These paths are venv implementation details that change with every Python minor
    version bump and should not be part of the stable expected-output check.
    """
    return bool(_PYTHON_VERSION_RE.search(file_path))


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify that all expected files are present in each pgAdmin4 sub-package.

    Checks all 4 packages independently:
      - pgadmin4         (meta package — no bundled files expected)
      - pgadmin4-desktop (Electron/Chromium desktop app files)
      - pgadmin4-server  (Flask server, docs, shared libs)
      - pgadmin4-web     (Apache/httpd config, setup script, web SBOMs)

    Only checks that expected files ARE present (subset check).
    Extra installed files are not flagged.
    Files whose basename contains a random hash segment are skipped.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    platform = "deb" if container_type == "deb" else "rpm"
    project_root = Path(__file__).parent.parent
    expected_output_dir = project_root / "expected-output" / platform

    # Mapping: expected-output filename -> actual installed package name
    # The pgedge packages use a "pgedge-" prefix that the expected-output filenames omit.
    sub_packages = [
        ("pgadmin4",         "pgedge-pgadmin4"),
        ("pgadmin4-desktop", "pgedge-pgadmin4-desktop"),
        ("pgadmin4-server",  "pgedge-pgadmin4-server"),
        ("pgadmin4-web",     "pgedge-pgadmin4-web"),
    ]

    packages_checked = 0
    overall_missing: dict = {}

    for expected_name, pkg_name in sub_packages:
        expected_file = expected_output_dir / expected_name
        if not expected_file.exists():
            print(f"   ⚠️  No expected output file for {expected_name} — skipping")
            continue

        # Parse expected paths, dropping blanks
        raw_expected = [
            line.strip() for line in expected_file.read_text().splitlines() if line.strip()
        ]
        if not raw_expected:
            print(f"   ℹ️  {expected_name}: meta package with no expected files — skipping")
            continue

        # Drop entries whose basename looks like a random hash or contain a
        # Python minor-version component (e.g. venv/lib/python3.13/...) — these
        # change with every Python version bump and are not stable expected paths.
        filtered_expected = [
            p for p in raw_expected
            if not _has_random_name(p) and not _has_python_version_path(p)
        ]
        skipped_random = len(raw_expected) - len(filtered_expected)
        if skipped_random:
            print(f"   ℹ️  {expected_name}: skipped {skipped_random} version-specific or random-named expected entries")

        # Count this package as checked — we have expected files for it
        packages_checked += 1

        # Query installed files from the container using the real package name
        if container_type == "deb":
            cmd = f"dpkg -L {pkg_name}"
        else:
            cmd = f"rpm -ql {pkg_name}"

        exit_code, output = container.exec_run(cmd, user="root")
        if exit_code != 0:
            error_msg = output.decode().strip()
            overall_missing[pkg_name] = [f"<package query failed: {error_msg}>"]
            continue

        installed_paths = {
            line.strip().lower()
            for line in output.decode().splitlines()
            if line.strip() and line.strip() != "/."
        }

        # Check every expected path is present (case-insensitive)
        expected_set = {p.lower() for p in filtered_expected}
        missing = sorted(expected_set - installed_paths)

        if missing:
            overall_missing[pkg_name] = missing
            print(f"   ❌ {pkg_name} ({expected_name}): {len(missing)} expected file(s) missing")
        else:
            print(f"   ✅ {pkg_name} ({expected_name}): all {len(expected_set)} expected files present")

    if packages_checked == 0:
        pytest.skip("No expected output files found for any pgAdmin4 sub-package")

    if overall_missing:
        msg_parts = []
        for pkg, files in overall_missing.items():
            msg_parts.append(f"\n\n[{pkg}] missing {len(files)} file(s):")
            for f in files[:25]:
                msg_parts.append(f"  - {f}")
            if len(files) > 25:
                msg_parts.append(f"  ... and {len(files) - 25} more")
        pytest.fail("Missing expected files in pgAdmin4 packages:" + "".join(msg_parts))


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgadmin4_binary_exists(container_name, container_type):
    """Step 5: Verify pgAdmin4 binary exists and is executable"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying pgAdmin4 binary on {container_name} ({container_type}) ---")

    # Verify pgadmin4 binary exists
    exit_code, output = container.exec_run(
        f"test -f {pgadmin4_bin}",
        user="root"
    )
    assert exit_code == 0, f"pgAdmin4 binary not found at {pgadmin4_bin}"
    print(f"✅ pgAdmin4 binary found at {pgadmin4_bin}")

    # Verify setup-web.sh exists
    exit_code, output = container.exec_run(
        f"test -f {pgadmin4_setup_web}",
        user="root"
    )
    assert exit_code == 0, f"setup-web.sh not found at {pgadmin4_setup_web}"
    print(f"✅ setup-web.sh found at {pgadmin4_setup_web}")

    # Verify install directory exists
    exit_code, output = container.exec_run(
        f"test -d {pgadmin4_install_dir}",
        user="root"
    )
    assert exit_code == 0, f"pgAdmin4 install directory not found at {pgadmin4_install_dir}"
    print(f"✅ pgAdmin4 install directory exists at {pgadmin4_install_dir}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgadmin4_web_config_exists(container_name, container_type):
    """Step 6: Verify platform-specific web server config file exists"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying pgAdmin4 web config on {container_name} ({container_type}) ---")

    # Platform-specific config file paths
    if container_type == "rhel":
        config_path = "/etc/httpd/conf.d/pgadmin4.conf"
    else:  # deb
        config_path = "/etc/apache2/conf-available/pgadmin4.conf"

    exit_code, output = container.exec_run(
        f"test -f {config_path}",
        user="root"
    )
    assert exit_code == 0, f"pgAdmin4 web config not found at {config_path}"
    print(f"✅ pgAdmin4 web config found at {config_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgadmin4_license_exists(container_name, container_type):
    """Step 7: Verify LICENSE file exists in install directory"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying pgAdmin4 LICENSE on {container_name} ({container_type}) ---")

    license_path = f"{pgadmin4_install_dir}/LICENSE"
    exit_code, output = container.exec_run(
        f"test -f {license_path}",
        user="root"
    )
    assert exit_code == 0, f"LICENSE file not found at {license_path}"
    print(f"✅ LICENSE file found at {license_path}")

    # Verify SBOM files exist
    for sbom_file in ["sbom-server.json", "sbom-server.json.asc", "sbom-web.json", "sbom-web.json.asc"]:
        sbom_path = f"{pgadmin4_install_dir}/{sbom_file}"
        exit_code, output = container.exec_run(
            f"test -f {sbom_path}",
            user="root"
        )
        assert exit_code == 0, f"SBOM file not found at {sbom_path}"
        print(f"✅ {sbom_file} found at {sbom_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgadmin4_web_setup(container_name, container_type):
    """Functional: Run setup-web.sh to configure pgAdmin4 for web mode via Apache/httpd.

    Pipes all interactive prompts non-interactively:
      - Email  : zaidagilist@gmail.com
      - Password: test123!@#
      - All yes/no questions: y
    Asserts the output contains both success lines:
      'Apache successfully started.'
      'You can now start using pgAdmin 4 in web mode at http://127.0.0.1/pgadmin4'
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Running pgAdmin4 web setup on {container_name} ({container_type}) ---")

    setup_script = f"{pgadmin4_install_dir}/bin/setup-web.sh"

    # Verify the setup script is present before attempting to run it
    exit_code, _ = container.exec_run(f"test -f {setup_script}", user="root")
    assert exit_code == 0, f"setup-web.sh not found at {setup_script}"

    # Pipe all expected interactive inputs in order:
    #   1. email address
    #   2. password
    #   3. confirm password
    #   4-6. 'y' for any yes/no prompts (e.g. start web server, enable on boot, etc.)
    cmd = [
        "bash", "-c",
        (
            f"printf 'zaidagilist@gmail.com\\ntest123!@#\\ntest123!@#\\ny\\ny\\ny\\n'"
            f" | {setup_script} 2>&1"
        ),
    ]

    exit_code, output = container.exec_run(cmd, user="root")
    output_text = output.decode()
    print(f"   Setup output:\n{output_text}")

    success_apache = "Apache successfully started." in output_text
    success_url = (
        "You can now start using pgAdmin 4 in web mode at http://127.0.0.1/pgadmin4"
        in output_text
    )

    if not success_apache:
        pytest.fail(
            f"'Apache successfully started.' not found in setup-web.sh output.\n"
            f"Full output:\n{output_text}"
        )
    if not success_url:
        pytest.fail(
            f"pgAdmin4 web-mode URL message not found in setup-web.sh output.\n"
            f"Full output:\n{output_text}"
        )

    print("✅ pgAdmin4 web setup completed — Apache started")
    print("✅ pgAdmin4 accessible at http://127.0.0.1/pgadmin4")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_package_uninstall(container_name, container_type):
    """Uninstall pgadmin4 package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    pkg = config["pgadmin4_package"]

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
            print(f"   Data directory removed")
        if cleanup_summary["user_removed"]:
            print(f"   User removed: {pguser_val}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")
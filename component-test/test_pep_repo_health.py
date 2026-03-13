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
from aspects import configure_repository, package_management, pg_server_management, machine_cleanup, machine_prereq_setup, container_management

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

# Common configuration
repo = os.getenv("REPO", "release")
custom_repo = os.getenv("CUSTOM_REPO", "false").lower() == "true"
rhel_custom_repo_url = os.getenv("RHEL_CUSTOM_REPO_URL", "")
deb_custom_repo_url = os.getenv("DEB_CUSTOM_REPO_URL", "")
skip_cleanup = os.getenv("SKIP_CLEANUP", "false").lower() == "true"
pgport = os.getenv("PG_PORT", "5432")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")

# All packages to install (RHEL / DEB)
rhel_all_packages = [
    p.strip() for p in os.getenv("ALL_PACKAGES", "").split(",")
    if p.strip() and len(p.strip()) > 4  # skip malformed entries like "pged"
]
deb_all_packages = [
    p.strip() for p in os.getenv("DEB_ALL_PACKAGES", "").split(",")
    if p.strip() and len(p.strip()) > 4
]

# All extensions to create
all_extensions = [
    ext.strip() for ext in os.getenv("All_EXTENSIONS", "").split(",")
    if ext.strip()
]

# Package-to-version mapping for known packages
RHEL_PACKAGE_VERSION_MAP = {
    f"pgedge-enterprise-all_{pg_major_version}":       os.getenv(f"PGEDGE_ENTERPRISE_ALL_{pg_major_version}_VERSION"),
    f"pgedge-enterprise-postgres_{pg_major_version}":  os.getenv(f"PGEDGE_ENTERPRISE_POSTGRES_{pg_major_version}_VERSION"),
    f"pgedge-postgresql{pg_major_version}-server":     os.getenv(f"PGEDGE_POSTGRESQL{pg_major_version}_SERVER_VERSION"),
    f"pgedge-pldebugger_{pg_major_version}":           os.getenv(f"PGEDGE_PLDEBUGGER_{pg_major_version}_VERSION"),
    f"pgedge-snowflake_{pg_major_version}":            os.getenv(f"PGEDGE_SNOWFLAKE_{pg_major_version}_VERSION"),
    f"pgedge-postgis35_{pg_major_version}":            os.getenv(f"PGEDGE_POSTGIS35_{pg_major_version}_VERSION"),
    f"pgedge-lolor_{pg_major_version}":                os.getenv(f"PGEDGE_LOLOR_{pg_major_version}_VERSION"),
    f"pgedge-spock50_{pg_major_version}":              os.getenv(f"PGEDGE_SPOCK50_{pg_major_version}_VERSION"),
    f"pgedge-pgaudit_{pg_major_version}":              os.getenv(f"PGEDGE_PGAUDIT_{pg_major_version}_VERSION"),
    f"pgedge-pgvector_{pg_major_version}":             os.getenv(f"PGEDGE_PGVECTOR_{pg_major_version}_VERSION"),
    f"pgedge-system_stats_{pg_major_version}":         os.getenv("PGEDGE_SYSTEM_STATS_VERSION"),
    f"pgedge-postgrest_{pg_major_version}":            os.getenv("PGEDGE_POSTGREST_VERSION"),
    "pgedge-pgbouncer":                                os.getenv("PGEDGE_PGBOUNCER_VERSION"),
    "pgedge-pgbackrest":                               os.getenv("PGEDGE_PGBACKREST_VERSION"),
    "pgedge-pgadmin4":                                 os.getenv("PGEDGE_PGADMIN4_VERSION"),
}

DEB_PACKAGE_VERSION_MAP = {
    f"pgedge-enterprise-all-{pg_major_version}":              os.getenv(f"PGEDGE_ENTERPRISE_ALL_{pg_major_version}_VERSION"),
    f"pgedge-enterprise-postgres-{pg_major_version}":         os.getenv(f"PGEDGE_ENTERPRISE_POSTGRES_{pg_major_version}_VERSION"),
    f"pgedge-postgresql-{pg_major_version}":                  os.getenv(f"PGEDGE_POSTGRESQL{pg_major_version}_SERVER_VERSION"),
    f"pgedge-postgresql-{pg_major_version}-pldebugger":       os.getenv(f"PGEDGE_PLDEBUGGER_{pg_major_version}_VERSION"),
    f"pgedge-postgresql-{pg_major_version}-snowflake":        os.getenv(f"PGEDGE_SNOWFLAKE_{pg_major_version}_VERSION"),
    f"pgedge-postgresql-{pg_major_version}-postgis-3":        os.getenv(f"PGEDGE_POSTGIS35_{pg_major_version}_VERSION"),
    f"pgedge-postgresql-{pg_major_version}-lolor":            os.getenv(f"PGEDGE_LOLOR_{pg_major_version}_VERSION"),
    f"pgedge-postgresql-{pg_major_version}-spock50":          os.getenv(f"PGEDGE_SPOCK50_{pg_major_version}_VERSION"),
    f"pgedge-postgresql-{pg_major_version}-pgaudit":          os.getenv(f"PGEDGE_PGAUDIT_{pg_major_version}_VERSION"),
    f"pgedge-postgresql-{pg_major_version}-pgvector":         os.getenv(f"PGEDGE_PGVECTOR_{pg_major_version}_VERSION"),
    f"pgedge-postgresql-{pg_major_version}-system-stats":     os.getenv("PGEDGE_SYSTEM_STATS_VERSION"),
    f"pgedge-postgresql-{pg_major_version}-postgrest":        os.getenv("PGEDGE_POSTGREST_VERSION"),
    "pgedge-pgbouncer":                                       os.getenv("PGEDGE_PGBOUNCER_VERSION"),
    "pgedge-pgbackrest":                                      os.getenv("PGEDGE_PGBACKREST_VERSION"),
    "pgedge-pgadmin4":                                        os.getenv("PGEDGE_PGADMIN4_VERSION"),
}


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "all_packages": rhel_all_packages,
            "version_map": RHEL_PACKAGE_VERSION_MAP,
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "all_packages": deb_all_packages,
            "version_map": DEB_PACKAGE_VERSION_MAP,
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

    try:
        success, os_info, message = machine_prereq_setup.install_prerequisites_on_container(container)
        assert success, f"Prerequisite installation failed: {message}"
        print(f"✅ Prerequisite installation completed on {container_name} ({os_info})")
        print(f"   {message}")
    except Exception as e:
        pytest.fail(f"Failed to install prerequisites: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_configure_repository(container_name, container_type):
    """Step 1: Configure the repository file.

    If CUSTOM_REPO=true, installs from RHEL_CUSTOM_REPO_URL (RHEL) or
    DEB_CUSTOM_REPO_URL (DEB) instead of the standard pgedge release RPM/DEB.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Configuring repository in {container_name} (custom_repo={custom_repo}) ---")

    if custom_repo:
        if container_type == "rhel":
            if not rhel_custom_repo_url:
                pytest.fail("CUSTOM_REPO=true but RHEL_CUSTOM_REPO_URL is not set")
            print(f"   Using custom RHEL repo URL: {rhel_custom_repo_url}")
            exit_code, output = container.exec_run(
                f"dnf install -y {rhel_custom_repo_url}", user="root"
            )
            assert exit_code == 0, f"Failed to install custom RHEL repo: {output.decode()}"
            # Switch to staging/daily if needed
            if repo in ["staging", "daily"]:
                exit_code, output = container.exec_run(
                    f"sed -i 's|release|{repo}|g' /etc/yum.repos.d/pgedge.repo", user="root"
                )
                assert exit_code == 0, f"Failed to switch repo to {repo}: {output.decode()}"
                print(f"   Repository switched to {repo}")
        else:  # deb
            if not deb_custom_repo_url:
                pytest.fail("CUSTOM_REPO=true but DEB_CUSTOM_REPO_URL is not set")
            print(f"   Using custom DEB repo URL: {deb_custom_repo_url}")
            install_cmd = (
                f"curl -sSL {deb_custom_repo_url} -o /tmp/pgedge-release.deb && "
                f"dpkg -i /tmp/pgedge-release.deb && "
                f"rm -f /tmp/pgedge-release.deb || true"
            )
            exit_code, output = container.exec_run(
                f"/bin/bash -c \"{install_cmd}\"", user="root"
            )
            assert exit_code == 0, f"Failed to install custom DEB repo: {output.decode()}"
            # Switch to staging/daily if needed
            if repo in ["staging", "daily"]:
                exit_code, output = container.exec_run(
                    f"sed -i 's|release|{repo}|g' /etc/apt/sources.list.d/pgedge.sources",
                    user="root",
                )
                assert exit_code == 0, f"Failed to switch repo to {repo}: {output.decode()}"
                print(f"   Repository switched to {repo}")
            exit_code, output = container.exec_run("apt-get update", user="root")
            assert exit_code == 0, f"apt-get update failed: {output.decode()}"
        print(f"✅ Custom repository configured successfully on {container_name} ({container_type})")
    else:
        try:
            success, platform, message = configure_repository.configure_pgedge_repository(container, repo)
            assert success, f"Repository configuration failed: {message}"
            print(f"✅ {message}")
            print(f"✅ Platform detected: {platform}")
        except Exception as e:
            pytest.fail(f"Failed to configure repository: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_install_all_packages(container_name, container_type):
    """Step 2: Install all packages defined in ALL_PACKAGES / DEB_ALL_PACKAGES"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    packages = config["all_packages"]

    if not packages:
        pytest.skip(f"No packages defined for {container_type}")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing {len(packages)} packages on {container_name} ({container_type}) ---")

    failed_packages = []
    for pkg in packages:
        print(f"   Installing {pkg}...")
        try:
            success, platform, message = package_management.install_package(container, pkg)
            if success:
                print(f"   ✅ {pkg}: {message}")
            else:
                print(f"   ❌ {pkg}: {message}")
                failed_packages.append(pkg)
        except Exception as e:
            print(f"   ❌ {pkg}: {str(e)}")
            failed_packages.append(pkg)

    assert not failed_packages, (
        f"Failed to install the following packages on {container_name}: "
        f"{', '.join(failed_packages)}"
    )
    print(f"✅ All {len(packages)} packages installed successfully on {container_name}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_package_versions(container_name, container_type):
    """Step 3: Verify installed package versions for packages with known expected versions"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    version_map = config["version_map"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying package versions on {container_name} ({container_type}) ---")

    failed_verifications = []
    skipped_packages = []

    for pkg, expected_version in version_map.items():
        if not expected_version:
            skipped_packages.append(pkg)
            continue

        print(f"   Verifying {pkg} == {expected_version}...")
        try:
            success, platform, installed_version, message = package_management.verify_package_version(
                container, pkg, expected_version
            )
            if success:
                print(f"   ✅ {pkg}: {message}")
            else:
                print(f"   ❌ {pkg}: {message}")
                failed_verifications.append(f"{pkg} (expected: {expected_version})")
        except Exception as e:
            print(f"   ❌ {pkg}: {str(e)}")
            failed_verifications.append(f"{pkg} ({str(e)})")

    if skipped_packages:
        print(f"   ⏭ Skipped version check for {len(skipped_packages)} packages (no expected version defined)")

    assert not failed_verifications, (
        f"Version verification failed for the following packages on {container_name}:\n"
        + "\n".join(f"  - {p}" for p in failed_verifications)
    )
    print(f"✅ All verified package versions match on {container_name}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_init_cluster(container_name, container_type):
    """Step 4: Initialize PostgreSQL cluster with GUC parameters for all extensions"""
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
    pguser = config["pguser"]

    print(f"\n--- Initializing cluster on {container_name} ---")

    guc_parameters = {
        "shared_preload_libraries": "'spock,lolor,snowflake'",
        "wal_level": "logical",
        "max_replication_slots": "10",
        "max_wal_senders": "10",
        "track_commit_timestamp":"on"
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
    """Step 5: Start PostgreSQL server"""
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
    pguser = config["pguser"]

    print(f"\n--- Starting PostgreSQL server on {container_name} ---")

    try:
        success, server_output, message = pg_server_management.start_server(
            container, pgbin, pgdata, pgport, pguser
        )
        assert success, f"Server start failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        # Read the PostgreSQL log for the actual error details
        _, log_output = container.exec_run(f"cat {pgdata}/logfile", user=pguser)
        pg_log = log_output.decode().strip() if log_output else "(log unavailable)"
        pytest.fail(
            f"Failed to start PostgreSQL server: {str(e)}\n\n"
            f"--- PostgreSQL log ({pgdata}/logfile) ---\n{pg_log}"
        )


@pytest.mark.parametrize("container_name,container_type", all_containers)
@pytest.mark.parametrize("extension", all_extensions)
def test_create_extensions(container_name, container_type, extension):
    """Step 6: Create each extension individually from All_EXTENSIONS"""
    container_name = container_name.strip()
    extension = extension.strip()

    if not container_name or not extension:
        pytest.skip("Invalid container or extension")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    # Quote extension name if it contains a dash
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
def test_stop_server(container_name, container_type):
    """Step 7: Stop PostgreSQL server"""
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
    """Step 8: Uninstall all installed packages"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    packages = config["all_packages"]

    if not packages:
        pytest.skip(f"No packages defined for {container_type}")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Uninstalling {len(packages)} packages on {container_name} ({container_type}) ---")

    failed_packages = []
    for pkg in packages:
        print(f"   Uninstalling {pkg}...")
        try:
            success, platform, message = package_management.uninstall_package(container, pkg)
            if success:
                print(f"   ✅ {pkg}: {message}")
            else:
                print(f"   ❌ {pkg}: {message}")
                failed_packages.append(pkg)
        except Exception as e:
            print(f"   ❌ {pkg}: {str(e)}")
            failed_packages.append(pkg)

    assert not failed_packages, (
        f"Failed to uninstall the following packages on {container_name}: "
        f"{', '.join(failed_packages)}"
    )
    print(f"✅ All {len(packages)} packages uninstalled successfully on {container_name}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Step 9: Full cleanup using machine_cleanup module"""
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
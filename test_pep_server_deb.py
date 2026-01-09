import os
import subprocess
from pathlib import Path

import pytest
import docker
from dotenv import load_dotenv

from test_pep_server_rhel import upgrade_repo

load_dotenv()
client = docker.from_env()

# Load values from env
containers = os.getenv("DEB_CONTAINERS", "").split(",")
repo = os.getenv("REPO", "release")
upgrade_repo = os.getenv("UPGRADE_REPO", "staging")
## Components for Deb
components = os.getenv("DEB_SERVER_COMPONENTS", "").split(",")
## Components for Rhel
#components = os.getenv("SERVER_COMPONENTS", "").split(",")

pguser = os.getenv("PG_USER", "postgres")
pgport = os.getenv("PG_PORT", "5432")
pgbin = os.getenv("DEB_PG_BIN_PATH", "/usr/lib/postgresql/17/bin/")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
server_version = os.getenv("PG_VERSION", "17.6")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "17")
check_extensions = os.getenv("TEST_EXTENSIONS", "false").lower() == "true"
# Extensions defined in env (core + contrib)
base_extensions = os.getenv(
    "EXTENSIONS",
    "bloom,bool_plperl,btree_gin,btree_gist,citext,cube,dblink,"
    "dict_int,earthdistance,fuzzystrmatch,hstore,intagg,intarray,isn,"
    "jsonb_plperl,lo,ltree,pg_buffercache,pg_prewarm,pg_stat_statements,"
    "pg_trgm,pgcrypto,pgrowlocks,pgstattuple,plperl,plpgsql,postgres_fdw,"
    "seg,sslinfo,tablefunc,tsm_system_rows,tsm_system_time,unaccent,uuid-ossp",
).split(",")

# Extra extensions requiring package installs
pl_packages = os.getenv("PL_PACKAGES", "pgedge-postgresql18-plperl,pgedge-postgresql18-pltcl,pgedge-postgresql18-plpython3").split(",")
pl_extensions = os.getenv("PL_EXTENSIONS", "plperl,plpython3u,pltcl").split(",")


@pytest.mark.parametrize("container_name", containers)
def test_pgedge_install(container_name):
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Detect platform: RHEL (dnf) or Ubuntu (apt-get)
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            platform = "ubuntu"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    print(f"\n--- Installing repo in {container_name} ({platform}) ---")

    if platform == "rhel":
        # Step 1: Install repo
        repo_url = "https://dnf.pgedge.com/reporpm/pgedge-release-latest.noarch.rpm"
        exit_code, output = container.exec_run(
            f"dnf install -y {repo_url}", user="root"
        )
        assert exit_code == 0, f"Failed to install repo: {output.decode()}"

        # Step 2: Switch repo if needed
        if repo in ["staging", "daily"]:
            container.exec_run(
                f"sed -i 's|release|{repo}|g' /etc/yum.repos.d/pgedge.repo", user="root"
            )

    elif platform == "ubuntu":
        # Step 1: Install repo via .deb
        deb_url = "https://apt.pgedge.com/repodeb/pgedge-release_latest_all.deb"
        install_cmd = f"""
            curl -sSL {deb_url} -o /tmp/pgedge-release.deb && \
            dpkg -i /tmp/pgedge-release.deb && \
            rm -f /tmp/pgedge-release.deb || true
        """

        exit_code, output = container.exec_run(
            f"/bin/bash -c \"{install_cmd}\"",
            user="root",
        )
        assert exit_code == 0, f"Failed to install repo: {output.decode()}"

        # Step 2: Switch repo if needed
        if repo in ["staging", "daily"]:
            container.exec_run(
                f"sed -i 's|release|{repo}|g' /etc/apt/sources.list.d/pgedge.sources",
                user="root",
            )

        # Step 3: apt-get update
        exit_code, output = container.exec_run("apt-get update", user="root")
        assert exit_code == 0, f"apt-get update failed: {output.decode()}"

# Simple approach: One test per component with dynamic parametrization
@pytest.mark.parametrize("container_name", containers)
@pytest.mark.parametrize("component", components)
def test_single_component_install(container_name, component):
    """Simple approach: Test each component individually

    This creates separate test for each container-component combination
    """
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("Invalid container or component")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Detect platform
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            platform = "ubuntu"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    print(f"\n--- Installing {component} on {container_name} ({platform}) ---")

    # Install component
    if platform == "rhel":
        exit_code, output = container.exec_run(
            f"dnf install -y {component}", user="root"
        )
    else:  # ubuntu
        exit_code, output = container.exec_run(
            f"apt-get install -y {component}", user="root"
        )

    assert exit_code == 0, f"Failed to install {component}: {output.decode()}"
    print(f"✅ Successfully installed {component}")


@pytest.mark.parametrize("container_name", containers)
@pytest.mark.parametrize("component", components)
def test_single_component_upgrade(container_name, component):
    """Simple approach: Test each component individually

    This creates separate test for each container-component combination
    """
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("Invalid container or component")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"


    # Detect platform
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        platform = "rhel"
        if upgrade_repo in ["staging", "daily"]:
            container.exec_run(
                f"sed -i 's|release|{repo}|g' /etc/yum.repos.d/pgedge.repo", user="root"
            )
        pkg_mgr = "dnf upgrade -y"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            platform = "ubuntu"
            if upgrade_repo in ["staging", "daily"]:
                container.exec_run(
                    f"sed -i 's|release|{repo}|g' /etc/apt/sources.list.d/pgedge.list",
                    user="root",
                )
            # Ensure apt repo is updated (only once per container)
            container.exec_run("apt-get update", user="root")
            pkg_mgr = "apt-get upgrade -y"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    print(f"\n--- Installing {component} on {container_name} ({platform}) ---")

    # Install the component
    exit_code, output = container.exec_run(
        f"{pkg_mgr} {component} ",
        user="root"
    )
    output_text = output.decode("utf-8").lower()
    print(f"<UNK> Successfully installed {component}: {output_text}")
    if exit_code == 0:
        if "already the newest version" in output_text or "already installed" in output_text:
            print(f"ℹ️ {component} version is already installed.")
            pytest.skip(f" {component} upgrade not found, version is already installed. {container_name}")
        elif "upgraded" in output_text or "install" in output_text:
            print(f"✅ Successfully upgraded {component}.")
        else:
            print(f"⚠️ Could not determine status for {component}.")

    assert exit_code == 0, f"Failed to install {component}: {output.decode()}"
    print(f"✅ Successfully installed {component}")


@pytest.mark.parametrize("container_name", containers)
def test_init_cluster(container_name):
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"Initializing cluster on {container_name}")
    container.exec_run(f"rm -rf {pgdata}", user=pguser)
    exit_code, output = container.exec_run(
        f"{pgbin}/initdb -D {pgdata}", user=pguser
    )
    assert exit_code == 0, f"Initdb failed: {output.decode()}"

@pytest.mark.parametrize("container_name", containers)
def test_start_server(container_name):
    container = client.containers.get(container_name.strip())
    assert container.status == "running"
    local_file = f"./config/postgresql_{pg_major_version}_all.conf"
    container_dest = f"{container_name}:/tmp/n1/postgresql.conf"

    # Ensure destination directory exists inside the container
    container.exec_run("mkdir -p /tmp/n1", user="root")

    # Copy file from Mac to container
    subprocess.run(["docker", "cp", local_file, container_dest], check=True)

    # Optionally confirm inside the container
    exit_code, output = container.exec_run("ls -l /tmp/n1", user="postgres")
    print(output.decode())

    if exit_code != 0:
        print("❌ Failed to copy config file")
        print(output.decode(errors="replace"))
    else:
        print("✅ Config file copied successfully")

    print(f"Starting PostgreSQL server on {container_name}")
    exit_code, output = container.exec_run(
        f"{pgbin}/pg_ctl -D {pgdata} -o '-p {pgport}' -l {pgdata}/logfile start",
        user=pguser,
    )
    assert exit_code == 0, f"pg_ctl start failed: {output.decode()}"

@pytest.mark.parametrize("container_name", containers)
def test_check_connection(container_name):
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"Checking PostgreSQL is running on {container_name}")
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -c 'SELECT version();'",
        user=pguser,
    )
    assert exit_code == 0, f"psql failed: {output.decode()}"
    print(f"Postgres running:\n{output.decode()}")

@pytest.mark.parametrize("container_name", containers)
def test_binaries_stripped(container_name):
    """Check all binaries in pgbin are stripped"""
    container = client.containers.get(container_name.strip())
    exit_code, output = container.exec_run(
        f"find {pgbin} -type f -exec file {{}} \\; | grep ELF | grep -v stripped",
        user="root",
    )
    assert exit_code == 1, f"Unstripped binaries found:\n{output.decode()}"


@pytest.mark.parametrize("container_name", containers)
def test_binary_versions(container_name):
    """Check postgres binary version matches expected"""
    container = client.containers.get(container_name.strip())
    exit_code, output = container.exec_run(f"{pgbin}/postgres -V", user=pguser)
    assert exit_code == 0, f"Failed to get postgres version: {output.decode()}"
    version_str = output.decode().strip()
    assert server_version in version_str, f"Version mismatch: {version_str}"
# Extra extensions requiring package installs
pl_packages = os.getenv("PL_PACKAGES", "pgedge-postgresql18-plperl,pgedge-postgresql18-pltcl,pgedge-postgresql18-plpython3").split(",")
pl_extensions = os.getenv("PL_EXTENSIONS", "plperl,plpython3u,pltcl").split(",")




@pytest.mark.parametrize("container_name", containers)
def test_create_pl_extensions(container_name):
    """Install PL packages and create PL extensions"""
    container = client.containers.get(container_name.strip())

    for ext in pl_extensions:
        print(f"Creating PL extension {ext} in {container_name}")
        exit_code, output = container.exec_run(
            f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
            f"-c 'CREATE EXTENSION IF NOT EXISTS {ext};'",
            user=pguser,
        )
        assert exit_code == 0, f"Failed to create {ext}: {output.decode()}"


@pytest.mark.parametrize("container_name", containers)
@pytest.mark.parametrize("extension", base_extensions)
def test_create_single_extension(container_name, extension):
    """Create each extension individually per container"""

    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    extension = extension.strip()
    if not container_name or not extension:
        pytest.skip("Invalid container or extension")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    container.reload()
    if container.status != "running":
        pytest.skip(f"Container {container_name} is not running")

    # Quote extension if it has a dash (Postgres needs double quotes for such identifiers)
    ext_formatted = f'"{extension}"' if "-" in extension else extension

    print(f"\n--- Creating extension {extension} in {container_name} ---")

    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
        f"-c 'CREATE EXTENSION IF NOT EXISTS {ext_formatted};'",
        user=pguser,
    )

    assert exit_code == 0, f"❌ Failed to create extension '{extension}': {output.decode()}"
    print(f"✅ Successfully created extension '{extension}' in {container_name}")



@pytest.mark.parametrize("container_name", containers)
def test_stop_server(container_name):
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"Starting PostgreSQL server on {container_name}")
    exit_code, output = container.exec_run(
        f"{pgbin}/pg_ctl -D {pgdata} -o '-p {pgport}' -l {pgdata}/logfile stop",
        user=pguser,
    )
    assert exit_code == 0, f"pg_ctl start failed: {output.decode()}"


@pytest.mark.parametrize("container_name", containers)
@pytest.mark.parametrize("component", components)
def uninstall_single_component(container_name, component):
    """Simple approach: Uninstall each component individually

    This creates a separate test for each container-component combination
    """
    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("Invalid container or component")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Detect platform: RHEL (dnf) or Ubuntu (apt-get)
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        packager = "dnf"
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            packager = "apt-get"
            platform = "ubuntu"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    print(f"\n--- Uninstalling {component} from {container_name} ({platform}) ---")

    # Uninstall the component
    exit_code, output = container.exec_run(
        f"{packager} remove -y '{component}*'",
        user="root"
    )

    assert exit_code == 0, f"Failed to uninstall {component}: {output.decode()}"
    print(f"✅ Successfully uninstalled {component}")


@pytest.mark.parametrize("container_name", containers)
def cleanup_all_pgedge_packages(container_name):
    """Clean up all remaining pgedge packages after component uninstall"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Detect platform: dnf or apt-get
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        platform = "rhel"
        pkg_list_cmd = "rpm -qa | grep pgedge"
        pkg_remove_cmd = "dnf remove -y 'pgedge-*'"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            platform = "debian"
            pkg_list_cmd = "dpkg-query -W -f='${Package}\n' | grep pgedge"
            pkg_remove_cmd = "apt-get remove -y 'pgedge-*'"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    print(f"\n--- Cleaning up remaining pgedge packages in {container_name} ({platform}) ---")

    # Check if any pgedge packages exist
    exit_code, output = container.exec_run(pkg_list_cmd, user="root")
    packages = output.decode().strip().splitlines()

    if not packages:
        print(f"No pgedge packages found in {container_name}, skipping cleanup.")
        pytest.skip("No packages to clean up")
    else:
        print(f"Found pgedge packages to remove: {len(packages)} packages")
        for pkg in packages[:10]:  # Show first 10
            print(f"  - {pkg}")

        # Remove all pgedge packages
        exit_code, output = container.exec_run(pkg_remove_cmd, user="root")
        output_str = output.decode() if output else ""
        assert exit_code == 0, f"Failed global cleanup: {output_str}"
        print(f"✅ Successfully cleaned up all pgedge packages")


@pytest.mark.parametrize("container_name", containers)
def remove_pgdata_directory(container_name):
    """Remove PGDATA directory if defined"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"


    if not pgdata:
        pytest.skip("PGDATA not defined in env")

    print(f"\n--- Removing PGDATA directory {pgdata} from {container_name} ---")

    # Check if directory exists
    exit_code, _ = container.exec_run(f"test -d {pgdata}", user="root")

    if exit_code != 0:
        print(f"PGDATA directory {pgdata} does not exist, skipping")
        pytest.skip(f"Directory {pgdata} does not exist")

    # Remove PGDATA directory
    exit_code, output = container.exec_run(f"rm -rf {pgdata}", user="root")
    output_str = output.decode() if output else ""

    assert exit_code == 0, f"Failed to remove PGDATA directory: {output_str}"
    print(f"✅ Successfully removed PGDATA directory {pgdata}")



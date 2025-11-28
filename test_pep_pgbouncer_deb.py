import os
import subprocess
from pathlib import Path
import sys

import pytest
import docker
from dotenv import load_dotenv

# Import the prerequisite setup module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'aspects'))
import machine_prereq_setup

load_dotenv()
client = docker.from_env()

# Load values from .env
containers = os.getenv("DEB_CONTAINERS", "").split(",")
repo = os.getenv("REPO", "release")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "17")

# PgBouncer specific configuration
pgbouncer_package = os.getenv("PGBOUNCER_PACKAGE", "pgedge-pgbouncer")
pgbouncer_version = os.getenv("PGEDGE_PGBOUNCER_VERSION", "")
pgbouncer_port = os.getenv("PGBOUNCER_PORT", "6432")
pgbouncer_config_dir = os.getenv("PGBOUNCER_CONFIG_DIR", "/etc/pgbouncer")
pgbouncer_bin = os.getenv("DEB_PGBOUNCER_BIN", "/usr/sbin")
pgbouncer_user = os.getenv("PGBOUNCER_USER", "pgbouncer")
pguser = os.getenv("PG_USER", "postgres")
pgport = os.getenv("PG_PORT", "5432")
pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin/")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")


# Expected bundled files to validate
pgbouncer_bundled_files = os.getenv(
    "PGBOUNCER_BUNDLED_FILES",
    "/usr/sbin/pgbouncer,/usr/share/doc/pgedge-pgbouncer,/etc/pgbouncer/pgbouncer.ini,/etc/pgbouncer/userlist.txt,/usr/lib/systemd/system/pgbouncer.service,/usr/lib/systemd/system/pgbouncer.socket,/usr/share/doc/pgedge-pgbouncer/README.md.gz,/usr/share/pgbouncer-sbom.json,/usr/share/pgbouncer-sbom.json.asc"
).split(",")


@pytest.mark.parametrize("container_name", containers)
def test_install_prerequisites(container_name):
    """Step 0: Install prerequisites using machine_prereq_setup module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing prerequisites on {container_name} ---")

    # Create a container-aware executor
    def container_executor(cmd):
        print(f"\n>>> Running: {cmd}")
        exit_code, output = container.exec_run(cmd, user="root")
        if exit_code != 0:
            print(f"WARNING: Command failed (exit={exit_code})")
        return exit_code

    # Set the custom executor
    machine_prereq_setup.set_executor(container_executor)

    # Detect OS inside container
    exit_code, output = container.exec_run("cat /etc/os-release", user="root")
    if exit_code != 0:
        pytest.fail("Failed to detect OS")

    # Parse OS info
    os_release = output.decode()
    os_id = ""
    version_id = ""
    for line in os_release.split('\n'):
        if line.startswith("ID="):
            os_id = line.split('=')[1].strip('"').lower()
        if line.startswith("VERSION_ID="):
            version_id = line.split('=')[1].strip('"')

    major = version_id.split('.')[0] if version_id else ""
    print(f"Detected OS: {os_id}, Version: {version_id} (major={major})")

    # Call the appropriate setup function
    if os_id in ["debian", "ubuntu"]:
        machine_prereq_setup.setup_debian()
    elif os_id in ["rhel", "redhat", "rhelserver"]:
        if major == "9":
            machine_prereq_setup.setup_rhel9()
        elif major == "10":
            machine_prereq_setup.setup_rhel10()
    elif os_id == "rocky":
        if major == "9":
            machine_prereq_setup.setup_rocky9()
        elif major == "10":
            machine_prereq_setup.setup_rocky10()
    elif os_id in ["ol", "oracle", "oraclelinux"]:
        if major == "9":
            machine_prereq_setup.setup_oracle9()
        elif major == "10":
            machine_prereq_setup.setup_oracle10()
    elif os_id in ["almalinux", "alma"]:
        if major == "9":
            machine_prereq_setup.setup_alma9()
        elif major == "10":
            machine_prereq_setup.setup_alma10()
    else:
        print(f"⚠️ Unsupported OS: {os_id}, skipping prerequisites...")

    print(f"\n✅ Prerequisite installation completed on {container_name}")


@pytest.mark.parametrize("container_name", containers)
def test_pgbouncer_configure_repository(container_name):
    """Step 1: Configure the repository file"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

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

    print(f"\n--- Configuring repository in {container_name} ({platform}) ---")

    if platform == "rhel":
        # Step 1: Install repo
        repo_url = "https://dnf.pgedge.com/reporpm/pgedge-release-latest.noarch.rpm"
        exit_code, output = container.exec_run(
            f"dnf install -y {repo_url}", user="root"
        )
        assert exit_code == 0, f"Failed to install repo: {output.decode()}"

        # Step 2: Switch repo if needed (staging/daily)
        if repo in ["staging", "daily"]:
            exit_code, output = container.exec_run(
                f"sed -i 's|release|{repo}|g' /etc/yum.repos.d/pgedge.repo", user="root"
            )
            assert exit_code == 0, f"Failed to switch repo to {repo}: {output.decode()}"
            print(f"✅ Repository switched to {repo}")

    elif platform == "ubuntu":
        # Step 1: Install repo via .deb
        deb_url = "https://apt.pgedge.com/repodeb/pgedge-release_latest_all.deb"
        install_cmd = f"""
            curl -sSL {deb_url} -o /tmp/pgedge-release.deb && \\
            dpkg -i /tmp/pgedge-release.deb && \\
            rm -f /tmp/pgedge-release.deb || true
        """

        exit_code, output = container.exec_run(
            f"/bin/bash -c \"{install_cmd}\"",
            user="root",
        )
        assert exit_code == 0, f"Failed to install repo: {output.decode()}"

        # Step 2: Switch repo if needed
        if repo in ["staging", "daily"]:
            exit_code, output = container.exec_run(
                f"sed -i 's|release|{repo}|g' /etc/apt/sources.list.d/pgedge.sources",
                user="root",
            )
            assert exit_code == 0, f"Failed to switch repo to {repo}: {output.decode()}"
            print(f"✅ Repository switched to {repo}")

        # Step 3: apt-get update
        exit_code, output = container.exec_run("apt-get update", user="root")
        assert exit_code == 0, f"apt-get update failed: {output.decode()}"

    print(f"✅ Repository configured successfully on {container_name}")


@pytest.mark.parametrize("container_name", containers)
def test_pgbouncer_install(container_name):
    """Step 2: Install pgedge-pgbouncer from staging, release, or daily repository"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Detect package manager inside the container
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        pkg_mgr = "dnf install -y"
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            container.exec_run("apt-get update", user="root")
            pkg_mgr = "apt-get install -y"
            platform = "debian"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    print(f"\n--- Installing {pgbouncer_package} on {container_name} ({platform}) ---")

    # Install pgbouncer package
    exit_code, output = container.exec_run(
        f"{pkg_mgr} {pgbouncer_package}",
        user="root"
    )

    assert exit_code == 0, f"Failed to install {pgbouncer_package}: {output.decode()}"
    print(f"✅ Successfully installed {pgbouncer_package}")

    # Install PostgreSQL server package for Debian
    if platform == "debian":
        server_package = f"pgedge-postgresql-{pg_major_version}"
        print(f"\n--- Installing {server_package} on {container_name} ({platform}) ---")
        exit_code, output = container.exec_run(
            f"{pkg_mgr} {server_package}",
            user="root"
        )
        assert exit_code == 0, f"Failed to install {server_package}: {output.decode()}"
        print(f"✅ Successfully installed {server_package}")


@pytest.mark.parametrize("container_name", containers)
def test_pgbouncer_verify_version(container_name):
    """Step 3: Check the package version matches the version in .env file"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    if not pgbouncer_version:
        pytest.skip("No PGBOUNCER_VERSION defined in .env, skipping version check")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {pgbouncer_package} version on {container_name} ---")
    print(f"Expected version: {pgbouncer_version}")

    # Detect package manager inside the container
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        # RHEL-based: use rpm to query version
        version_cmd = f"rpm -q --queryformat '%{{VERSION}}' {pgbouncer_package}"
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            # Debian-based: use dpkg-query to get version
            version_cmd = f"dpkg-query --showformat='${{Version}}' --show {pgbouncer_package}"
            platform = "debian"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    # Get installed version
    exit_code, output = container.exec_run(version_cmd, user="root")

    if exit_code != 0:
        pytest.fail(f"Failed to query {pgbouncer_package} version: {output.decode()}")

    installed_version = output.decode().strip()
    print(f"Installed version: {installed_version}")

    # Version comparison - check if expected version is contained in installed version
    assert pgbouncer_version in installed_version, (
        f"Version mismatch for {pgbouncer_package} on {container_name} ({platform})\n"
        f"Expected: {pgbouncer_version}\n"
        f"Installed: {installed_version}"
    )

    print(f"✅ Version verified: {pgbouncer_package} {installed_version}")


@pytest.mark.parametrize("container_name", containers)
@pytest.mark.parametrize("bundled_file", pgbouncer_bundled_files)
def test_pgbouncer_validate_bundled_files(container_name, bundled_file):
    """Step 4: Validate bundled files exist"""
    container_name = container_name.strip()
    bundled_file = bundled_file.strip()

    if not container_name or not bundled_file:
        pytest.skip("Invalid container or bundled file path")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Validating bundled file: {bundled_file} on {container_name} ---")

    # Check if file/directory exists
    exit_code, output = container.exec_run(
        f"test -e {bundled_file}",
        user="root"
    )

    assert exit_code == 0, f"Bundled file/directory not found: {bundled_file}"

    # Get file type info
    exit_code, output = container.exec_run(
        f"ls -la {bundled_file}",
        user="root"
    )
    print(f"File info: {output.decode().strip()}")
    print(f"✅ Bundled file validated: {bundled_file}")


@pytest.mark.parametrize("container_name", containers)
def test_pgbouncer_copy_config_files(container_name):
    """Step 5: Copy userlist.txt, pgbouncer.ini from config to /etc/pgbouncer/"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Copying PgBouncer config files to {container_name} ---")

    # Ensure /etc/pgbouncer directory exists
    exit_code, output = container.exec_run(
        f"mkdir -p {pgbouncer_config_dir}",
        user="root"
    )
    assert exit_code == 0, f"Failed to create config directory: {output.decode()}"

    # Config files to copy - map source filename to destination filename
    config_files = {
        "userlist.txt": "userlist.txt",
        "deb-pgbouncer.ini": "pgbouncer.ini"
    }

    for source_file, dest_file in config_files.items():
        local_file = f"./config/pgbouncer/{source_file}"
        container_dest = f"{container_name}:{pgbouncer_config_dir}/{dest_file}"

        # Check if local config file exists
        if not os.path.exists(local_file):
            pytest.fail(f"Local config file not found: {local_file}")

        # Copy file from host to container
        result = subprocess.run(
            ["docker", "cp", local_file, container_dest],
            capture_output=True,
            text=True
        )

        assert result.returncode == 0, (
            f"Failed to copy {source_file} to container: {result.stderr}"
        )
        print(f"✅ Copied {source_file} as {dest_file} to {pgbouncer_config_dir}/")

        # Set correct ownership (postgres:postgres)
        chown_exit_code, chown_output = container.exec_run(
            f"chown postgres:postgres {pgbouncer_config_dir}/{dest_file}",
            user="root"
        )
        assert chown_exit_code == 0, (
            f"Failed to set ownership for {dest_file}: {chown_output.decode()}"
        )

        # Set correct permissions (600)
        chmod_exit_code, chmod_output = container.exec_run(
            f"chmod 600 {pgbouncer_config_dir}/{dest_file}",
            user="root"
        )
        assert chmod_exit_code == 0, (
            f"Failed to set permissions for {dest_file}: {chmod_output.decode()}"
        )
        print(f"✅ Set ownership and permissions for {dest_file}")

    # Verify files were copied
    for source_file, dest_file in config_files.items():
        exit_code, output = container.exec_run(
            f"test -f {pgbouncer_config_dir}/{dest_file}",
            user="root"
        )
        assert exit_code == 0, f"Config file not found after copy: {dest_file}"

    print(f"✅ All config files copied successfully")


@pytest.mark.parametrize("container_name", containers)
def pgbouncer_set_permissions(container_name):
    """Step 6: Change /etc/pgbouncer/userlist.txt permissions to 600 with postgres:postgres ownership"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    userlist_file = f"{pgbouncer_config_dir}/userlist.txt"

    print(f"\n--- Setting permissions on {userlist_file} in {container_name} ---")

    # Ensure postgres user exists
    exit_code, output = container.exec_run(
        f"id {pguser}",
        user="root"
    )
    if exit_code != 0:
        print(f"Creating {pguser} user...")
        exit_code, output = container.exec_run(
            f"useradd -r -s /bin/bash {pguser}",
            user="root"
        )
        # Ignore error if user already exists
        if exit_code != 0 and "already exists" not in output.decode():
            pytest.fail(f"Failed to create {pguser} user: {output.decode()}")

    # Set ownership to postgres:postgres
    exit_code, output = container.exec_run(
        f"chown {pguser}:{pguser} {userlist_file}",
        user="root"
    )
    assert exit_code == 0, f"Failed to change ownership: {output.decode()}"
    print(f"✅ Changed ownership to {pguser}:{pguser}")

    # Set permissions to 600
    exit_code, output = container.exec_run(
        f"chmod 600 {userlist_file}",
        user="root"
    )
    assert exit_code == 0, f"Failed to change permissions: {output.decode()}"
    print(f"✅ Changed permissions to 600")

    # Verify permissions and ownership
    exit_code, output = container.exec_run(
        f"ls -la {userlist_file}",
        user="root"
    )
    assert exit_code == 0, f"Failed to verify permissions: {output.decode()}"
    file_info = output.decode().strip()
    print(f"File info: {file_info}")

    # Verify permissions are -rw-------
    assert "-rw-------" in file_info, f"Permissions not set correctly: {file_info}"
    # Verify ownership
    assert pguser in file_info, f"Ownership not set correctly: {file_info}"

    print(f"✅ Permissions and ownership verified")
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
    # Before line copy the postgresql file incase user need to test all components



    # Ensure destination directory exists inside the container
    container.exec_run("mkdir -p /tmp/n1", user="root")



    # Optionally confirm inside the container
    exit_code, output = container.exec_run("ls -l /tmp/n1", user="postgres")
    print(output.decode())

    if exit_code != 0:
        print("❌ Failed to copy config file")
        print(output.decode(errors="replace"))
    else:
        print("✅ Config file copied successfully")

    # ## Old code ...
    # exit_code, output = container.exec_run(
    #     f"cp /tmp/postgresql_{pg_major_version}_all.conf /tmp/n1/postgresql.conf",
    #     user="postgres"
    # )
    #
    # if exit_code != 0:
    #     print("❌ Failed to copy config file")
    #     print(output.decode(errors="replace"))
    # else:
    #     print("✅ Config file copied successfully")

    print(f"Starting PostgreSQL server on {container_name}")
    exit_code, output = container.exec_run(
        f"{pgbin}/pg_ctl -D {pgdata} -o '-p {pgport}' -l {pgdata}/logfile start",
        user=pguser,
    )
    assert exit_code == 0, f"pg_ctl start failed: {output.decode()}"

@pytest.mark.parametrize("container_name", containers)
def test_check_connection(container_name):


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
def test_pgbouncer_start_service(container_name):
    """Step 7: Switch to postgres user and start pgbouncer daemon"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Starting PgBouncer service on {container_name} ---")

    # Set ownership of config directory and files to postgres
    exit_code, output = container.exec_run(
        f"chown -R {pguser}:{pguser} {pgbouncer_config_dir}",
        user="root"
    )
    if exit_code != 0:
        print(f"Warning: Failed to set ownership on config dir: {output.decode()}")

    # Start pgbouncer as postgres user
    start_cmd = (
        f"setsid {pgbouncer_bin}/pgbouncer "
        f"{pgbouncer_config_dir}/pgbouncer.ini "
    )

    exit_code, output = container.exec_run(
        start_cmd,
        user=pguser
    )
    assert exit_code == 0, f"Failed to start pgbouncer: {output.decode()}"
    print(f"✅ PgBouncer started with daemon mode as {pguser} user")

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


@pytest.mark.parametrize("container_name", containers)
def test_pgbouncer_connect_psql(container_name):
    """Step 8: Connect to pgbouncer via psql on port 6432"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Connecting to PgBouncer via psql on {container_name} ---")

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
        pytest.skip("No container defined in .env")

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
        pytest.skip("No container defined in .env")

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

    # Verify version matches expected version from .env
    if pgbouncer_version:
        assert pgbouncer_version in version_output, (
            f"Version mismatch!\n"
            f"Expected: {pgbouncer_version}\n"
            f"Got: {version_output}"
        )
        print(f"✅ Version verified: {pgbouncer_version}")
    else:
        print(f"⚠️ PGBOUNCER_VERSION not set in .env, skipping version verification")

    print(f"✅ SHOW VERSION executed successfully")


@pytest.mark.parametrize("container_name", containers)
def test_pgbouncer_show_databases(container_name):
    """Step 9.3: Run SHOW DATABASES command"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

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
def test_pgbouncer_stop_service(container_name):
    """Stop PgBouncer service for cleanup"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Stopping PgBouncer service on {container_name} ---")

    # Kill pgbouncer process
    exit_code, output = container.exec_run(
        "pkill -INT pgbouncer",
        user="root"
    )

    # Verify process is stopped
    exit_code, output = container.exec_run(
        "pgrep -x pgbouncer",
        user="root"
    )
    assert exit_code != 0, f"PgBouncer process still running: {output.decode()}"

    print(f"✅ PgBouncer service stopped")

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
def test_pgbouncer_uninstall(container_name):
    """Uninstall pgbouncer package"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Detect package manager
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        pkg_mgr = "dnf remove -y"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            pkg_mgr = "apt purge -y"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    print(f"\n--- Uninstalling {pgbouncer_package} from {container_name} ---")

    exit_code, output = container.exec_run(
        f"{pkg_mgr} {pgbouncer_package}",
        user="root"
    )
    assert exit_code == 0, f"Failed to uninstall {pgbouncer_package}: {output.decode()}"

    print(f"✅ Successfully uninstalled {pgbouncer_package}")

    print(f"\n--- Uninstalling pgedge packages from {container_name} ---")





@pytest.mark.parametrize("container_name", containers)
def test_pgbouncer_cleanup(container_name):
    """Full cleanup: remove config files and pgbouncer user"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Cleaning up PgBouncer in {container_name} ---")

    # Remove config directory
    exit_code, output = container.exec_run(
        f"rm -rf {pgbouncer_config_dir}",
        user="root"
    )
    print(f"Removed config directory: {pgbouncer_config_dir}")

    # Remove pgbouncer user (optional)
    exit_code, output = container.exec_run(
        f"userdel {pgbouncer_user}",
        user="root"
    )

    # Detect package manager
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        pkg_mgr = "dnf remove -y"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            pkg_mgr = "apt-get remove -y"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    if exit_code == 0:
        print(f"Removed user: {pgbouncer_user}")

    exit_code, output = container.exec_run(
        f"{pkg_mgr} pgedge-*",
        user="root"
    )
    assert exit_code == 0, f"Failed to uninstall {pgbouncer_package}: {output.decode()}"

    print(f"✅ Successfully uninstalled all pgedge packages from {container_name}")

    # Step 2: Optionally clean data directory (if defined in .env)
    if pgdata:
        print(f"Removing PGDATA directory {pgdata} in {container_name}")
        container.exec_run(f"rm -rf {pgdata}", user="root")

    # Step 3: Delete user postgres created by automation setup
    print(f"Removing {pguser} User  in {container_name}")
    container.exec_run(f"userdel {pguser}", user="root")


    print(f"✅ Cleanup completed")
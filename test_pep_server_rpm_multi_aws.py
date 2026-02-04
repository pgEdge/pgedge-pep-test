#!/usr/bin/env python3
"""Complete pgedge test suite for multiple AWS EC2 instances - Pytest compatible"""

import os
import sys
import time
import paramiko
import pytest
from dotenv import load_dotenv
import json

# CRITICAL: Load env BEFORE any other imports or code
load_dotenv()

# Verify instances are loaded
if not os.getenv("INSTANCES"):
    print("ERROR: INSTANCES not found in env file!")
    print(f"Current INSTANCES value: {os.getenv('INSTANCES')}")
    sys.exit(1)

# Load values from env
repo = os.getenv("REPO", "release")
components = os.getenv("SERVER_COMPONENTS", "").split(",")
pguser = os.getenv("PG_USER", "postgres")
pgport = os.getenv("PG_PORT", "5432")
pgbin = os.getenv("PG_BIN_PATH", "/usr/pgsql-18/bin")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
server_version = os.getenv("PG_VERSION", "17.6")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "17")
check_extensions = os.getenv("TEST_EXTENSIONS", "false").lower() == "true"
base_extensions = os.getenv("EXTENSIONS", "").split(",")
pl_packages = os.getenv("PL_PACKAGES", "").split(",")
pl_extensions = os.getenv("PL_EXTENSIONS", "").split(",")


def parse_instances():
    """Parse instance configurations from environment variables"""
    instances = []

    # Try JSON format first
    try:
        instances_json = os.getenv("INSTANCES_JSON", "")
        if instances_json:
            instances = json.loads(instances_json)
            if instances:
                print(f"Loaded {len(instances)} instances from INSTANCES_JSON")
                return instances
    except json.JSONDecodeError as e:
        print(f"Failed to parse INSTANCES_JSON: {e}")

    # Fall back to comma-separated format
    instances_str = os.getenv("INSTANCES", "")
    print(f"DEBUG: INSTANCES env var = '{instances_str}'")

    hostnames = [h.strip() for h in instances_str.split(",") if h.strip()]
    usernames = [u.strip() for u in os.getenv("USERNAMES", "").split(",") if u.strip()]
    key_paths = [k.strip() for k in os.getenv("KEY_PATHS", "").split(",") if k.strip()]

    print(f"DEBUG: Parsed {len(hostnames)} hostnames: {hostnames}")

    # If no hostnames found, return empty list
    if not hostnames:
        print("WARNING: No hostnames found in INSTANCES")
        return []

    # If only one username/key_path provided, use it for all instances
    if len(usernames) == 0:
        usernames = ["rocky"] * len(hostnames)
        print(f"DEBUG: Using default username 'rocky' for all instances")
    elif len(usernames) == 1 and len(hostnames) > 1:
        usernames = usernames * len(hostnames)
        print(f"DEBUG: Using username '{usernames[0]}' for all instances")

    if len(key_paths) == 0:
        key_paths = ["keys/default.pem"] * len(hostnames)
        print(f"DEBUG: Using default key path for all instances")
    elif len(key_paths) == 1 and len(hostnames) > 1:
        key_paths = key_paths * len(hostnames)
        print(f"DEBUG: Using key path '{key_paths[0]}' for all instances")

    for i, hostname in enumerate(hostnames):
        instance = {
            "hostname": hostname,
            "username": usernames[i] if i < len(usernames) else "rocky",
            "key_path": key_paths[i] if i < len(key_paths) else "keys/default.pem",
            "name": f"instance-{i + 1}"
        }
        instances.append(instance)
        print(f"DEBUG: Added instance: {instance['name']} -> {instance['hostname']}")

    print(f"Successfully loaded {len(instances)} instances")
    return instances


class SSHConnectionManager:
    """Manages SSH connection with auto-reconnect"""

    def __init__(self, hostname, username, key_path, max_retries=3, instance_name=None):
        self.hostname = hostname
        self.username = username
        self.key_path = key_path
        self.max_retries = max_retries
        self.instance_name = instance_name or hostname
        self.client = None
        self.key = None
        self._connect()

    def _connect(self):
        """Establish SSH connection"""
        print(f"[{self.instance_name}] 🔌 Connecting to {self.hostname}...")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.key = paramiko.RSAKey.from_private_key_file(self.key_path)

        for attempt in range(self.max_retries):
            try:
                self.client.connect(
                    hostname=self.hostname,
                    username=self.username,
                    pkey=self.key,
                    timeout=30,
                    banner_timeout=30,
                    auth_timeout=30,
                    look_for_keys=False,
                    allow_agent=False
                )
                print(f"[{self.instance_name}] ✅ Connected\n")
                return
            except Exception as e:
                print(f"[{self.instance_name}] ⚠️  Connection attempt {attempt + 1} failed: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(2)
                else:
                    raise

    def is_connected(self):
        """Check if connection is active"""
        if self.client is None:
            return False
        transport = self.client.get_transport()
        return transport is not None and transport.is_active()

    def ensure_connected(self):
        """Ensure connection is active, reconnect if needed"""
        if not self.is_connected():
            print(f"[{self.instance_name}] ⚠️  Connection lost, reconnecting...")
            self._connect()

    def exec_command(self, command, user="root", timeout=300):
        """Execute command with auto-reconnect"""
        self.ensure_connected()

        if user != "root":
            cmd = f"sudo -u {user} {command}"
        else:
            cmd = f"sudo {command}"

        for attempt in range(self.max_retries):
            try:
                stdin, stdout, stderr = self.client.exec_command(
                    cmd,
                    get_pty=True,
                    timeout=timeout
                )

                stdout.channel.settimeout(timeout)
                exit_code = stdout.channel.recv_exit_status()
                output = stdout.read()

                return exit_code, output

            except (paramiko.SSHException, OSError, EOFError) as e:
                print(f"[{self.instance_name}] ⚠️  Command execution failed (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    print(f"[{self.instance_name}] Reconnecting...")
                    self._connect()
                    time.sleep(1)
                else:
                    raise

    def open_sftp(self):
        """Open SFTP session with connection check"""
        self.ensure_connected()
        return self.client.open_sftp()

    def close(self):
        """Close connection"""
        if self.client:
            self.client.close()
            print(f"[{self.instance_name}] 🔌 Connection closed")


# Global SSH managers - one per instance
ssh_managers = {}


def get_ssh_manager(instance_config):
    """Get or create SSH manager for an instance"""
    key = instance_config["hostname"]
    if key not in ssh_managers:
        ssh_managers[key] = SSHConnectionManager(
            instance_config["hostname"],
            instance_config["username"],
            instance_config["key_path"],
            instance_name=instance_config["name"]
        )
    return ssh_managers[key]


def exec_run(ssh_manager, command, user="root", timeout=300):
    """Execute command via SSH"""
    return ssh_manager.exec_command(command, user=user, timeout=timeout)


# Parse instances at module level
print("\n" + "=" * 80)
print("LOADING INSTANCE CONFIGURATIONS")
print("=" * 80)
INSTANCES_LIST = parse_instances()
print(f"\nTotal instances to test: {len(INSTANCES_LIST)}")
print("=" * 80 + "\n")

if not INSTANCES_LIST:
    print("WARNING: No instances configured!")
    print("Please check your env file has INSTANCES variable set correctly")


# Pytest configuration
def pytest_configure(config):
    """Pytest configuration hook"""
    if not INSTANCES_LIST:
        print("\n" + "=" * 80)
        print("ERROR: No instances configured in env file")
        print("=" * 80)
        print("\nPlease add the following to your env file:")
        print("INSTANCES=hostname1.com,hostname2.com")
        print("USERNAMES=rocky")
        print("KEY_PATHS=keys/yourkey.pem")
        print("=" * 80 + "\n")


def pytest_generate_tests(metafunc):
    """Dynamically generate tests for each instance"""
    if "ssh_manager" in metafunc.fixturenames:
        if not INSTANCES_LIST:
            # No instances, mark for collection but will skip
            metafunc.parametrize("ssh_manager", [None], indirect=True, ids=["no-instances"])
        else:
            # Parametrize with instance configs
            metafunc.parametrize(
                "ssh_manager",
                INSTANCES_LIST,
                ids=[inst["name"] for inst in INSTANCES_LIST],
                indirect=True
            )


@pytest.fixture
def ssh_manager(request):
    """Create SSH connection for each instance"""
    if request.param is None:
        pytest.skip("No instances configured in env file")

    instance_config = request.param
    manager = get_ssh_manager(instance_config)
    return manager


@pytest.fixture(scope="session", autouse=True)
def cleanup_connections():
    """Cleanup all SSH connections after all tests"""
    yield
    if ssh_managers:
        print("\n\n🧹 Cleaning up SSH connections...")
        for manager in ssh_managers.values():
            manager.close()


# Test functions
def test_pgedge_install(ssh_manager):
    """Install pgedge repository"""

    exit_code, _ = exec_run(ssh_manager, "command -v dnf")
    if exit_code == 0:
        platform = "rhel"
    else:
        exit_code, _ = exec_run(ssh_manager, "command -v apt-get")
        platform = "ubuntu" if exit_code == 0 else None

    assert platform is not None, "No supported package manager found"

    print(f"[{ssh_manager.instance_name}] --- Installing repo ({platform}) ---")

    if platform == "rhel":
        repo_url = "https://dnf.pgedge.com/reporpm/pgedge-release-latest.noarch.rpm"
        exit_code, output = exec_run(ssh_manager, f"dnf install -y {repo_url}")

        assert exit_code == 0, f"Failed to install repo: {output.decode()}"

        if repo in ["staging", "daily"]:
            exec_run(ssh_manager, f"sed -i 's|release|{repo}|g' /etc/yum.repos.d/pgedge.repo")
            print(f"[{ssh_manager.instance_name}] ✅ Switched to {repo} repo")

    elif platform == "ubuntu":
        deb_url = "https://apt.pgedge.com/repodeb/pgedge-release_latest_all.deb"
        exit_code, output = exec_run(ssh_manager,
                                     f"curl -sSL {deb_url} -o /tmp/pgedge-release.deb && "
                                     f"dpkg -i /tmp/pgedge-release.deb && "
                                     f"rm -f /tmp/pgedge-release.deb || true"
                                     )

        assert exit_code == 0, f"Failed to install repo: {output.decode()}"

        if repo in ["staging", "daily"]:
            exec_run(ssh_manager, f"sed -i 's|release|{repo}|g' /etc/apt/sources.list.d/pgedge.list")
            print(f"[{ssh_manager.instance_name}] ✅ Switched to {repo} repo")

        exit_code, output = exec_run(ssh_manager, "apt-get update")
        assert exit_code == 0, f"apt-get update failed: {output.decode()}"

    print(f"[{ssh_manager.instance_name}] ✅ Repository installed\n")


def test_install_components(ssh_manager):
    """Install each component individually"""

    exit_code, _ = exec_run(ssh_manager, "command -v dnf")
    if exit_code == 0:
        pkg_mgr = "dnf install -y"
        platform = "rhel"
    else:
        exit_code, _ = exec_run(ssh_manager, "command -v apt-get")
        if exit_code == 0:
            exec_run(ssh_manager, "apt-get update")
            pkg_mgr = "apt-get install -y"
            platform = "debian"
        else:
            pytest.fail("No supported package manager found")

    print(f"[{ssh_manager.instance_name}] --- Installing components ({platform}) ---")

    for component in components:
        component = component.strip()
        if not component:
            continue

        print(f"[{ssh_manager.instance_name}] Installing {component}...")
        exit_code, output = exec_run(ssh_manager, f"{pkg_mgr} {component}")

        assert exit_code == 0, f"Failed to install {component}: {output.decode()}"
        print(f"[{ssh_manager.instance_name}] ✅ {component} installed")

    print(f"[{ssh_manager.instance_name}] ✅ All components installed\n")


def test_verify_component_versions(ssh_manager):
    """Verify component versions"""

    exit_code, _ = exec_run(ssh_manager, "command -v dnf")
    if exit_code == 0:
        version_cmd_template = "rpm -q --queryformat '%{{VERSION}}' {}"
        platform = "rhel"
    else:
        version_cmd_template = "dpkg-query --showformat='${{Version}}' --show {}"
        platform = "debian"

    print(f"[{ssh_manager.instance_name}] --- Verifying component versions ({platform}) ---")

    for component in components:
        component = component.strip()
        if not component:
            continue

        exit_code, output = exec_run(ssh_manager, version_cmd_template.format(component))

        if exit_code != 0:
            print(f"[{ssh_manager.instance_name}] ⚠️  Failed to query {component} version")
            continue

        installed_version = output.decode().strip()
        print(f"[{ssh_manager.instance_name}] ✅ {component}: {installed_version}")

    print()


def test_init_cluster(ssh_manager):
    """Initialize PostgreSQL cluster"""

    print(f"[{ssh_manager.instance_name}] --- Initializing cluster ---")
    ssh_manager.ensure_connected()

    exec_run(ssh_manager, f"rm -rf {pgdata}", user=pguser)
    exit_code, output = exec_run(ssh_manager, f"{pgbin}/initdb -D {pgdata}", user=pguser)

    assert exit_code == 0, f"initdb failed: {output.decode()}"

    print(f"[{ssh_manager.instance_name}] ✅ Cluster initialized\n")


def test_start_server(ssh_manager):
    """Start PostgreSQL server"""

    print(f"[{ssh_manager.instance_name}] --- Starting PostgreSQL server ---")

    local_file = f"./config/postgresql_{pg_major_version}_all.conf"
    temp_file = "/tmp/postgresql.conf"
    final_dest = f"{pgdata}/postgresql.conf"

    exec_run(ssh_manager, f"mkdir -p {pgdata}")
    exec_run(ssh_manager, f"chown -R {pguser}:{pguser} {pgdata}")

    print(f"[{ssh_manager.instance_name}] Copying config file to AWS /tmp...")
    scp = ssh_manager.open_sftp()
    scp.put(local_file, temp_file)
    scp.close()
    print(f"[{ssh_manager.instance_name}] ✅ Config file uploaded to {temp_file}")

    exec_run(ssh_manager, f"chmod 644 {temp_file}")

    print(f"[{ssh_manager.instance_name}] Copying config to {final_dest} as {pguser}...")
    exit_code, output = exec_run(ssh_manager, f"cp {temp_file} {final_dest}", user=pguser)

    assert exit_code == 0, f"Failed to copy config: {output.decode()}"

    print(f"[{ssh_manager.instance_name}] ✅ Config file copied to {final_dest}")
    exec_run(ssh_manager, f"rm -f {temp_file}")

    exit_code, output = exec_run(ssh_manager,
                                 f"{pgbin}/pg_ctl -D {pgdata} -o '-p {pgport}' -l {pgdata}/logfile start",
                                 user=pguser
                                 )

    assert exit_code == 0, f"pg_ctl start failed: {output.decode()}"

    print(f"[{ssh_manager.instance_name}] ✅ PostgreSQL server started\n")


def test_check_connection(ssh_manager):
    """Check PostgreSQL connection"""

    if not check_extensions:
        print(f"[{ssh_manager.instance_name}] --- Extension check disabled ---\n")
        pytest.skip("Extension check disabled")

    print(f"[{ssh_manager.instance_name}] --- Checking PostgreSQL connection ---")
    exit_code, output = exec_run(ssh_manager,
                                 f"{pgbin}/psql -p {pgport} -U {pguser} -c 'SELECT version();'",
                                 user=pguser
                                 )

    assert exit_code == 0, f"psql failed: {output.decode()}"

    print(f"[{ssh_manager.instance_name}] ✅ PostgreSQL running:\n{output.decode()}\n")


def test_binaries_stripped(ssh_manager):
    """Check all binaries are stripped"""

    print(f"[{ssh_manager.instance_name}] --- Checking binaries are stripped ---")
    exit_code, output = exec_run(ssh_manager,
                                 f"find {pgbin} -type f -exec file {{}} \\; | grep ELF | grep -v stripped"
                                 )

    assert exit_code != 0, f"Unstripped binaries found:\n{output.decode()}"

    print(f"[{ssh_manager.instance_name}] ✅ All binaries are stripped\n")


def test_binary_versions(ssh_manager):
    """Check postgres binary version"""

    print(f"[{ssh_manager.instance_name}] --- Checking binary version ---")
    exit_code, output = exec_run(ssh_manager, f"{pgbin}/postgres -V", user=pguser)

    assert exit_code == 0, f"Failed to get version: {output.decode()}"

    version_str = output.decode().strip()
    assert server_version in version_str, f"Version mismatch: {version_str}"

    print(f"[{ssh_manager.instance_name}] ✅ Version matches: {version_str}\n")


def test_create_pl_extensions(ssh_manager):
    """Install PL packages and create PL extensions"""

    if not pl_packages or not pl_packages[0]:
        pytest.skip("No PL packages configured")

    print(f"[{ssh_manager.instance_name}] --- Installing PL packages ---")

    for pkg in pl_packages:
        pkg = pkg.strip()
        if not pkg:
            continue

        print(f"[{ssh_manager.instance_name}] Installing {pkg}...")
        exit_code, output = exec_run(ssh_manager, f"dnf install -y {pkg}")

        assert exit_code == 0, f"Failed to install {pkg}: {output.decode()}"
        print(f"[{ssh_manager.instance_name}] ✅ {pkg} installed")

    print(f"\n[{ssh_manager.instance_name}] --- Creating PL extensions ---")

    for ext in pl_extensions:
        ext = ext.strip()
        if not ext:
            continue

        print(f"[{ssh_manager.instance_name}] Creating extension {ext}...")
        exit_code, output = exec_run(ssh_manager,
                                     f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
                                     f"-c 'CREATE EXTENSION IF NOT EXISTS {ext};'",
                                     user=pguser
                                     )

        assert exit_code == 0, f"Failed to create {ext}: {output.decode()}"
        print(f"[{ssh_manager.instance_name}] ✅ {ext} created")

    print()


def test_create_extensions(ssh_manager):
    """Create base extensions"""

    if not check_extensions:
        pytest.skip("Extension check disabled")

    print(f"[{ssh_manager.instance_name}] --- Creating base extensions ---")

    for extension in base_extensions:
        extension = extension.strip()
        if not extension:
            continue

        normalized_ext = f'"{extension}"' if "-" in extension else extension

        print(f"[{ssh_manager.instance_name}] Creating extension {normalized_ext}...")
        exit_code, output = exec_run(ssh_manager,
                                     f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
                                     f"-c 'CREATE EXTENSION IF NOT EXISTS {normalized_ext};'",
                                     user=pguser
                                     )

        assert exit_code == 0, f"Failed to create {normalized_ext}: {output.decode()}"
        print(f"[{ssh_manager.instance_name}] ✅ {normalized_ext} created")

    print()


def test_stop_server(ssh_manager):
    """Stop PostgreSQL server"""

    print(f"[{ssh_manager.instance_name}] --- Stopping PostgreSQL server ---")
    exit_code, output = exec_run(ssh_manager,
                                 f"{pgbin}/pg_ctl -D {pgdata} stop",
                                 user=pguser
                                 )

    assert exit_code == 0, f"pg_ctl stop failed: {output.decode()}"

    print(f"[{ssh_manager.instance_name}] ✅ PostgreSQL server stopped\n")


def test_pgedge_uninstall(ssh_manager):
    """Uninstall all components"""

    print(f"[{ssh_manager.instance_name}] --- Uninstalling components ---")

    for pkg in components:
        pkg = pkg.strip()
        if not pkg:
            continue

        print(f"[{ssh_manager.instance_name}] Uninstalling {pkg}...")
        exit_code, output = exec_run(ssh_manager, f"dnf remove -y '{pkg}*'")

        assert exit_code == 0, f"Failed to uninstall {pkg}: {output.decode()}"
        print(f"[{ssh_manager.instance_name}] ✅ {pkg} uninstalled")

    print()


def test_pgedge_cleanup(ssh_manager):
    """Full cleanup"""

    print(f"[{ssh_manager.instance_name}] --- Cleaning up pgedge packages ---")

    exit_code, output = exec_run(ssh_manager, "rpm -qa | grep pgedge")
    packages = output.decode().strip().splitlines()

    if packages:
        print(f"[{ssh_manager.instance_name}] Removing packages: {packages}")
        exit_code, output = exec_run(ssh_manager, "dnf remove -y 'pgedge-*'")
        assert exit_code == 0, f"Failed cleanup: {output.decode()}"
        print(f"[{ssh_manager.instance_name}] ✅ Packages removed")

    if pgdata:
        print(f"[{ssh_manager.instance_name}] Removing PGDATA directory {pgdata}")
        exec_run(ssh_manager, f"rm -rf {pgdata}")

    print(f"[{ssh_manager.instance_name}] Removing {pguser} user")
    exec_run(ssh_manager, f"userdel {pguser}")

    print(f"[{ssh_manager.instance_name}] ✅ Cleanup completed\n")
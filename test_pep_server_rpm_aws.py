#!/usr/bin/env python3
"""Complete pgedge test suite for AWS EC2 instance with connection resilience"""

import os
import time
import paramiko
from dotenv import load_dotenv

load_dotenv()

# Configuration
HOSTNAME = "ec2-65-0-18-65.ap-south-1.compute.amazonaws.com"
USERNAME = "rocky"
KEY_PATH = "keys/zaid_key_official.pem"

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


class SSHConnectionManager:
    """Manages SSH connection with auto-reconnect"""

    def __init__(self, hostname, username, key_path, max_retries=3):
        self.hostname = hostname
        self.username = username
        self.key_path = key_path
        self.max_retries = max_retries
        self.client = None
        self.key = None
        self._connect()

    def _connect(self):
        """Establish SSH connection"""
        print(f"🔌 Connecting to {self.hostname}...")
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
                print(f"✅ Connected to {self.hostname}\n")
                return
            except Exception as e:
                print(f"⚠️  Connection attempt {attempt + 1} failed: {e}")
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
            print("⚠️  Connection lost, reconnecting...")
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

                # Set timeout for reading output
                stdout.channel.settimeout(timeout)

                exit_code = stdout.channel.recv_exit_status()
                output = stdout.read()

                return exit_code, output

            except (paramiko.SSHException, OSError, EOFError) as e:
                print(f"⚠️  Command execution failed (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    print("Reconnecting...")
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
            print("\n🔌 Connection closed")


# Initialize connection manager
ssh_manager = SSHConnectionManager(HOSTNAME, USERNAME, KEY_PATH)


def exec_run(client_manager, command, user="root", timeout=300):
    """Execute command via SSH (mimics Docker's exec_run)"""
    return client_manager.exec_command(command, user=user, timeout=timeout)


def test_pgedge_install():
    """Install pgedge repository"""

    # Detect platform
    exit_code, _ = exec_run(ssh_manager, "command -v dnf")
    if exit_code == 0:
        platform = "rhel"
    else:
        exit_code, _ = exec_run(ssh_manager, "command -v apt-get")
        platform = "ubuntu" if exit_code == 0 else None

    if not platform:
        print("❌ No supported package manager found")
        return False

    print(f"--- Installing repo ({platform}) ---")

    if platform == "rhel":
        repo_url = "https://dnf.pgedge.com/reporpm/pgedge-release-latest.noarch.rpm"
        exit_code, output = exec_run(ssh_manager, f"dnf install -y {repo_url}")

        if exit_code != 0:
            print(f"❌ Failed to install repo: {output.decode()}")
            return False

        if repo in ["staging", "daily"]:
            exec_run(ssh_manager, f"sed -i 's|release|{repo}|g' /etc/yum.repos.d/pgedge.repo")
            print(f"✅ Switched to {repo} repo")

    elif platform == "ubuntu":
        deb_url = "https://apt.pgedge.com/repodeb/pgedge-release_latest_all.deb"
        exit_code, output = exec_run(ssh_manager,
                                     f"curl -sSL {deb_url} -o /tmp/pgedge-release.deb && "
                                     f"dpkg -i /tmp/pgedge-release.deb && "
                                     f"rm -f /tmp/pgedge-release.deb || true"
                                     )

        if exit_code != 0:
            print(f"❌ Failed to install repo: {output.decode()}")
            return False

        if repo in ["staging", "daily"]:
            exec_run(ssh_manager, f"sed -i 's|release|{repo}|g' /etc/apt/sources.list.d/pgedge.list")
            print(f"✅ Switched to {repo} repo")

        exit_code, output = exec_run(ssh_manager, "apt-get update")
        if exit_code != 0:
            print(f"❌ apt-get update failed: {output.decode()}")
            return False

    print("✅ Repository installed\n")
    return True


def test_install_components():
    """Install each component individually"""

    # Detect package manager
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
            print("❌ No supported package manager found")
            return False

    print(f"--- Installing components ({platform}) ---")

    for component in components:
        component = component.strip()
        if not component:
            continue

        print(f"Installing {component}...")
        exit_code, output = exec_run(ssh_manager, f"{pkg_mgr} {component}")

        if exit_code != 0:
            print(f"❌ Failed to install {component}: {output.decode()}")
            return False
        print(f"✅ {component} installed")

    print("✅ All components installed\n")
    return True


def test_verify_component_versions():
    """Verify component versions"""

    exit_code, _ = exec_run(ssh_manager, "command -v dnf")
    if exit_code == 0:
        version_cmd_template = "rpm -q --queryformat '%{{VERSION}}' {}"
        platform = "rhel"
    else:
        version_cmd_template = "dpkg-query --showformat='${{Version}}' --show {}"
        platform = "debian"

    print(f"--- Verifying component versions ({platform}) ---")

    for component in components:
        component = component.strip()
        if not component:
            continue

        exit_code, output = exec_run(ssh_manager, version_cmd_template.format(component))

        if exit_code != 0:
            print(f"❌ Failed to query {component} version")
            continue

        installed_version = output.decode().strip()
        print(f"✅ {component}: {installed_version}")

    print()
    return True


def test_init_cluster():
    """Initialize PostgreSQL cluster"""

    print("--- Initializing cluster ---")

    # Ensure connection before critical operations
    ssh_manager.ensure_connected()

    exec_run(ssh_manager, f"rm -rf {pgdata}", user=pguser)
    exit_code, output = exec_run(ssh_manager, f"{pgbin}/initdb -D {pgdata}", user=pguser)

    if exit_code != 0:
        print(f"❌ initdb failed: {output.decode()}")
        return False

    print("✅ Cluster initialized\n")
    return True


def test_start_server():
    """Start PostgreSQL server"""

    print("--- Starting PostgreSQL server ---")

    # File paths
    local_file = f"./config/postgresql_{pg_major_version}_all.conf"
    temp_file = "/tmp/postgresql.conf"  # Temporary location in AWS /tmp
    final_dest = f"{pgdata}/postgresql.conf"  # Final destination

    # Ensure pgdata directory exists with correct ownership
    exec_run(ssh_manager, f"mkdir -p {pgdata}")
    exec_run(ssh_manager, f"chown -R {pguser}:{pguser} {pgdata}")

    # Step 1: Copy file to AWS /tmp (accessible by rocky user)
    print(f"Copying config file to AWS /tmp...")
    try:
        scp = ssh_manager.open_sftp()
        scp.put(local_file, temp_file)
        scp.close()
        print(f"✅ Config file uploaded to {temp_file}")
    except Exception as e:
        print(f"❌ Failed to upload config: {e}")
        return False

    # Step 2: Make the file readable by postgres user
    exec_run(ssh_manager, f"chmod 644 {temp_file}")

    # Step 3: Copy from /tmp to final destination as postgres user
    print(f"Copying config to {final_dest} as {pguser}...")
    exit_code, output = exec_run(ssh_manager, f"cp {temp_file} {final_dest}", user=pguser)

    if exit_code != 0:
        print(f"❌ Failed to copy config: {output.decode()}")
        return False

    print(f"✅ Config file copied to {final_dest}")

    # Step 4: Clean up temp file (optional)
    exec_run(ssh_manager, f"rm -f {temp_file}")

    # Start server
    exit_code, output = exec_run(ssh_manager,
                                 f"{pgbin}/pg_ctl -D {pgdata} -o '-p {pgport}' -l {pgdata}/logfile start",
                                 user=pguser
                                 )

    if exit_code != 0:
        print(f"❌ pg_ctl start failed: {output.decode()}")
        return False

    print("✅ PostgreSQL server started\n")
    return True


def test_check_connection():
    """Check PostgreSQL connection"""

    if not check_extensions:
        print("--- Extension check disabled ---\n")
        return True

    print("--- Checking PostgreSQL connection ---")
    exit_code, output = exec_run(ssh_manager,
                                 f"{pgbin}/psql -p {pgport} -U {pguser} -c 'SELECT version();'",
                                 user=pguser
                                 )

    if exit_code != 0:
        print(f"❌ psql failed: {output.decode()}")
        return False

    print(f"✅ PostgreSQL running:\n{output.decode()}\n")
    return True


def test_binaries_stripped():
    """Check all binaries are stripped"""

    print("--- Checking binaries are stripped ---")
    exit_code, output = exec_run(ssh_manager,
                                 f"find {pgbin} -type f -exec file {{}} \\; | grep ELF | grep -v stripped"
                                 )

    if exit_code == 0:
        print(f"❌ Unstripped binaries found:\n{output.decode()}")
        return False

    print("✅ All binaries are stripped\n")
    return True


def test_binary_versions():
    """Check postgres binary version"""

    print("--- Checking binary version ---")
    exit_code, output = exec_run(ssh_manager, f"{pgbin}/postgres -V", user=pguser)

    if exit_code != 0:
        print(f"❌ Failed to get version: {output.decode()}")
        return False

    version_str = output.decode().strip()
    if server_version in version_str:
        print(f"✅ Version matches: {version_str}\n")
        return True
    else:
        print(f"❌ Version mismatch: {version_str}\n")
        return False


def test_create_pl_extensions():
    """Install PL packages and create PL extensions"""

    print("--- Installing PL packages ---")

    for pkg in pl_packages:
        pkg = pkg.strip()
        if not pkg:
            continue

        print(f"Installing {pkg}...")
        exit_code, output = exec_run(ssh_manager, f"dnf install -y {pkg}")

        if exit_code != 0:
            print(f"❌ Failed to install {pkg}: {output.decode()}")
            return False
        print(f"✅ {pkg} installed")

    print("\n--- Creating PL extensions ---")

    for ext in pl_extensions:
        ext = ext.strip()
        if not ext:
            continue

        print(f"Creating extension {ext}...")
        exit_code, output = exec_run(ssh_manager,
                                     f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
                                     f"-c 'CREATE EXTENSION IF NOT EXISTS {ext};'",
                                     user=pguser
                                     )

        if exit_code != 0:
            print(f"❌ Failed to create {ext}: {output.decode()}")
            return False
        print(f"✅ {ext} created")

    print()
    return True


def test_create_extensions():
    """Create base extensions"""

    if not check_extensions:
        print("--- Extension check disabled ---\n")
        return True

    print("--- Creating base extensions ---")

    for extension in base_extensions:
        extension = extension.strip()
        if not extension:
            continue

        # Normalize extension (quote if it contains a dash)
        normalized_ext = f'"{extension}"' if "-" in extension else extension

        print(f"Creating extension {normalized_ext}...")
        exit_code, output = exec_run(ssh_manager,
                                     f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
                                     f"-c 'CREATE EXTENSION IF NOT EXISTS {normalized_ext};'",
                                     user=pguser
                                     )

        if exit_code != 0:
            print(f"❌ Failed to create {normalized_ext}: {output.decode()}")
            return False
        print(f"✅ {normalized_ext} created")

    print()
    return True


def test_stop_server():
    """Stop PostgreSQL server"""

    print("--- Stopping PostgreSQL server ---")
    exit_code, output = exec_run(ssh_manager,
                                 f"{pgbin}/pg_ctl -D {pgdata} -o '-p {pgport}' -l {pgdata}/logfile stop",
                                 user=pguser
                                 )

    if exit_code != 0:
        print(f"❌ pg_ctl stop failed: {output.decode()}")
        return False

    print("✅ PostgreSQL server stopped\n")
    return True


def test_pgedge_uninstall():
    """Uninstall all components"""

    print("--- Uninstalling components ---")

    for pkg in components:
        pkg = pkg.strip()
        if not pkg:
            continue

        print(f"Uninstalling {pkg}...")
        exit_code, output = exec_run(ssh_manager, f"dnf remove -y '{pkg}*'")

        if exit_code != 0:
            print(f"❌ Failed to uninstall {pkg}: {output.decode()}")
            return False
        print(f"✅ {pkg} uninstalled")

    print()
    return True


def test_pgedge_cleanup():
    """Full cleanup"""

    print("--- Cleaning up pgedge packages ---")

    exit_code, output = exec_run(ssh_manager, "rpm -qa | grep pgedge")
    packages = output.decode().strip().splitlines()

    if not packages:
        print("No pgedge packages found")
    else:
        print(f"Removing packages: {packages}")
        exit_code, output = exec_run(ssh_manager, "dnf remove -y 'pgedge-*'")
        if exit_code != 0:
            print(f"❌ Failed cleanup: {output.decode()}")
            return False
        print("✅ Packages removed")

    if pgdata:
        print(f"Removing PGDATA directory {pgdata}")
        exec_run(ssh_manager, f"rm -rf {pgdata}")

    print(f"Removing {pguser} user")
    exec_run(ssh_manager, f"userdel {pguser}")

    print("✅ Cleanup completed\n")
    return True


# Run all tests
try:
    test_pgedge_install()
    test_install_components()
    test_verify_component_versions()
    test_init_cluster()
    test_start_server()
    test_check_connection()
    test_binaries_stripped()
    test_binary_versions()
    test_create_pl_extensions()
    test_create_extensions()
    test_stop_server()
    test_pgedge_uninstall()  # Uncomment to test uninstall
    test_pgedge_cleanup()     # Uncomment to test cleanup

    print("=" * 60)
    print("✅ ALL TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 60)

except Exception as e:
    print(f"\n❌ ERROR: {e}")
    import traceback

    traceback.print_exc()

finally:
    ssh_manager.close()
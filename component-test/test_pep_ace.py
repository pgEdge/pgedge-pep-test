import os
import sys
import subprocess
import time
from pathlib import Path
from datetime import datetime

import pytest
import docker
from dotenv import load_dotenv

# Add the parent directory to sys.path to import from aspects
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from aspects import configure_repository, package_management, machine_cleanup, machine_prereq_setup, file_management, container_management, pg_server_management

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
ace_version = os.getenv("PGEDGE_ACE_VERSION", "")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_ace_package = os.getenv("ACE_PACKAGE", "pgedge-ace")
rhel_bundled_files = os.getenv(
    "ACE_BUNDLED_FILES",
    "/usr/bin/ace"
).split(",")

# Debian-specific configuration
deb_ace_package = os.getenv("DEB_ACE_PACKAGE", "pgedge-ace")
deb_bundled_files = os.getenv(
    "DEB_ACE_BUNDLED_FILES",
    "/usr/bin/ace"
).split(",")

# Decoupled components SBOM path
decoupled_sbom_path = os.getenv("DECOUPLED_COMPONENTS_SBOM", "")

# ACE functional test cluster configuration (always 3 nodes)
ace_pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")
ace_base_port = int(os.getenv("BASE_PORT", "5431"))
ace_pg_password = os.getenv("PG_PASSWORD", "postgres")
ace_cluster_name = os.getenv("ACE_CLUSTER_NAME", "demo")
ace_rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{ace_pg_major_version}/bin")
ace_deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{ace_pg_major_version}/bin")

# Spock major used to build the ACE functional cluster. pgedge-ace does not pull
# in PostgreSQL; installing spock provides the server (and the postgres OS user)
# plus the replication engine ACE diffs against.
ace_spock_major = os.getenv("SPOCK_MAJOR", "50")

_ace_spock_guc = {
    "shared_preload_libraries": "'spock'",
    "wal_level": "logical",
    "max_worker_processes": "10",
    "max_replication_slots": "10",
    "max_wal_senders": "10",
    "track_commit_timestamp": "on",
}

# PG >= 16.15 / 17.11 / 18.5 / 19.0beta3 require logical decoding output plugins
# to be allow-listed before they can be loaded. ACE diffs across cross-wired
# n1/n2/n3 nodes, so spock_output must be permitted. Returns {} on older point
# releases, which do not recognise the GUC.
_ace_spock_guc.update(pg_server_management.output_plugin_libraries_guc())


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pguser": rhel_pguser,
            "ace_package": rhel_ace_package,
            "bundled_files": rhel_bundled_files
        }
    else:  # deb
        return {
            "pguser": deb_pguser,
            "ace_package": deb_ace_package,
            "bundled_files": deb_bundled_files
        }


def _ace_pgbin(container_type):
    return ace_rhel_pgbin.rstrip('/') if container_type == "rhel" else ace_deb_pgbin.rstrip('/')


def _ace_pguser(container_type):
    return rhel_pguser if container_type == "rhel" else deb_pguser


def _ace_spock_packages(container_type):
    """Spock + PostgreSQL server packages needed to build the ACE functional
    cluster. Installing spock pulls the pgedge-postgresql server (which creates
    the postgres OS user and provides initdb/psql/pg_ctl)."""
    if container_type == "rhel":
        return [
            f"pgedge-spock{ace_spock_major}_{ace_pg_major_version}",
            f"pgedge-postgresql{ace_pg_major_version}-contrib",
        ]
    else:  # deb (contrib is bundled in the pgedge-postgresql-<xx> server package)
        return [
            f"pgedge-postgresql-{ace_pg_major_version}-spock{ace_spock_major}",
            f"pgedge-postgresql-{ace_pg_major_version}",
        ]


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
    """Step 2: Install pgedge-ace using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    ace_package = config["ace_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing {ace_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to install the package
    try:
        success, platform, message = package_management.install_package(container, ace_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {ace_package}: {str(e)}")


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
    ace_package = config["ace_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Upgrading {ace_package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    # Use the package_management module to upgrade the package
    try:
        success, platform, message = package_management.upgrade_package(container, ace_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{ace_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {ace_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not ace_version:
        pytest.skip("No PGEDGE_ACE_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    ace_package = config["ace_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {ace_package} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, ace_package, ace_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {ace_package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files for ace match expected files

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
    actual_package = config["ace_package"]

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

    config = get_container_config(container_type)
    ace_package = config["ace_package"]
    sbom_dir = f"{decoupled_sbom_path}/{ace_package}"
    sbom_name = ace_package.removeprefix("pgedge-")

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
            f"--signature-file {sbom_name}-sbom.json.asc "
            f"--signer-file pgedge-rsa.pub "
            f"{sbom_name}-sbom.json'",
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
        machine_prereq_setup.ensure_sq_installed(container)
        _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
        _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
        _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"
        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"{_sq_signer_flag} /etc/apt/keyrings/pgedge-rsa.gpg "
            f"{_sq_sig_flag} {sbom_name}-sbom.json.asc "
            f"{sbom_name}-sbom.json'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_help(container_name, container_type):
    """Test that ace --help command works correctly"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Testing ace --help on {container_name} ({container_type}) ---")

    # Run ace --help command
    exit_code, output = container.exec_run("/usr/bin/ace --help")
    output_str = output.decode() if output else ""

    print(f"Output:\n{output_str}")

    assert exit_code == 0, f"ace --help failed with exit code {exit_code}: {output_str}"
    assert len(output_str) > 0, "ace --help returned empty output"
    print(f"✅ ace --help executed successfully")


# ============================================================================
# ACE Functional Tests (3-node cluster)
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_functional_setup_cluster(container_name, container_type):
    """ACE Functional Step 1: Initialize a 3-node Spock cluster for ACE functional testing"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    pgbin = _ace_pgbin(container_type)
    pguser = _ace_pguser(container_type)

    print(f"\n--- ACE: Initializing 3-node Spock cluster on {container_name} ---")

    # pgedge-ace ships only the ACE tool — it does not pull in PostgreSQL or
    # spock. Install the spock package set first: it provides the PostgreSQL
    # server (creating the postgres OS user and initdb/psql/pg_ctl) plus the
    # replication engine the ACE functional tests operate against.
    spock_packages = _ace_spock_packages(container_type)
    print(f"▶️  Installing cluster prerequisites: {', '.join(spock_packages)}")
    try:
        success, platform, message = package_management.install_package(
            container, spock_packages,
            pg_major_version=ace_pg_major_version, install_pg_server=False
        )
        assert success, f"Failed to install spock/server packages for ACE cluster: {message}"
        print(f"✅ Cluster prerequisites installed on {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install spock/server packages for ACE cluster: {str(e)}")

    # Initialize nodes n1, n2, n3
    for node_num in range(1, 4):
        node_name = f"n{node_num}"
        node_port = ace_base_port + node_num - 1
        node_pgdata = f"/tmp/{node_name}"

        print(f"\n▶️  Initializing {node_name} (port {node_port})")

        # Stop and clean up any existing node
        container.exec_run(
            ["bash", "-c",
             f"[ -d {node_pgdata} ] && {pgbin}/pg_ctl -D {node_pgdata} -m fast stop 2>/dev/null; "
             f"rm -rf {node_pgdata}"],
            user="root"
        )
        container.exec_run(
            ["bash", "-c", f"fuser -k {node_port}/tcp 2>/dev/null; true"],
            user="root"
        )

        success, _, message = pg_server_management.init_cluster(
            container, pgbin, node_pgdata, pguser, _ace_spock_guc
        )
        assert success, f"Failed to init {node_name}: {message}"

        success, _, message = pg_server_management.start_server(
            container, pgbin, node_pgdata, str(node_port), pguser
        )
        assert success, f"Failed to start {node_name}: {message}"
        print(f"✅ {node_name} running on port {node_port}")

    # Create Spock extension and node on each instance
    for node_num in range(1, 4):
        node_name = f"n{node_num}"
        node_port = ace_base_port + node_num - 1
        node_dsn = (
            f"host=localhost port={node_port} "
            f"dbname=postgres user={pguser} password={ace_pg_password}"
        )

        for sql in [
            "CREATE EXTENSION IF NOT EXISTS spock;",
            # Native spock.node_create (present in spock50 & spock60).
            # spock.create_node is a zodan-defined procedure, which ACE does not load.
            f"SELECT spock.node_create(node_name := '{node_name}', dsn := '{node_dsn}');",
        ]:
            exit_code, output = container.exec_run(
                ["psql", "-h", "localhost", "-p", str(node_port), "-U", pguser,
                 "-d", "postgres", "-c", sql],
                user="root"
            )
            assert exit_code == 0, f"SQL failed on {node_name}: {sql}\n{output.decode()}"

        print(f"✅ Spock extension and node created on {node_name}")

    n1_port = ace_base_port

    # Create test table public.n1 on n1, add to default repset, seed 10 rows
    setup_sql = (
        "CREATE TABLE IF NOT EXISTS public.n1 (\n"
        "    id SERIAL PRIMARY KEY,\n"
        "    data TEXT,\n"
        "    created_at TIMESTAMP DEFAULT NOW()\n"
        ");\n"
        "SELECT spock.repset_add_table('default', 'public.n1');\n"
        "INSERT INTO public.n1 (data)\n"
        "    SELECT 'initial_row_' || g FROM generate_series(1, 10) g;\n"
    )
    sql_file = "/tmp/ace_create_table.sql"
    container.exec_run(
        ["bash", "-c", f"cat > {sql_file} << 'EOF'\n{setup_sql}\nEOF"],
        user="root"
    )
    exit_code, output = container.exec_run(
        ["bash", "-c",
         f"psql -h localhost -p {n1_port} -U {pguser} -d postgres -f {sql_file}"],
        user="root"
    )
    assert exit_code == 0, f"Failed to create/seed public.n1 on n1: {output.decode()}"
    print(f"✅ Table public.n1 seeded with 10 rows on n1")

    # Subscribe n2 and n3 to n1 for initial data sync
    n1_dsn = (
        f"host=localhost port={n1_port} dbname=postgres "
        f"user={pguser} password={ace_pg_password}"
    )
    for sub_num in [2, 3]:
        sub_port = ace_base_port + sub_num - 1
        sub_name = f"sub_n{sub_num}_n1"
        # Native spock.sub_create (spock50 & spock60). spock.create_subscription
        # is a zodan-defined procedure, which ACE does not load.
        # synchronize_structure := true copies the table schema to the subscriber
        # during initial sync (otherwise public.n1 never gets created on n2/n3).
        sub_sql = (
            f"SELECT spock.sub_create("
            f"subscription_name := '{sub_name}', "
            f"provider_dsn := '{n1_dsn}', "
            f"replication_sets := ARRAY['default'], "
            f"synchronize_structure := true, "
            f"synchronize_data := true);"
        )
        exit_code, output = container.exec_run(
            ["psql", "-h", "localhost", "-p", str(sub_port), "-U", pguser,
             "-d", "postgres", "-c", sub_sql],
            user="root"
        )
        assert exit_code == 0, (
            f"Failed to create subscription {sub_name}: {output.decode()}"
        )
        print(f"✅ n{sub_num} subscribed to n1 ({sub_name})")

    print(f"\n⏳ Waiting 15 seconds for initial replication to n2 and n3...")
    time.sleep(15)

    # Verify table replicated to n2 and n3
    for check_num in [2, 3]:
        check_port = ace_base_port + check_num - 1
        exit_code, output = container.exec_run(
            ["psql", "-h", "localhost", "-p", str(check_port), "-U", pguser,
             "-d", "postgres", "-t", "-c", "SELECT COUNT(*) FROM public.n1;"],
            user="root"
        )
        assert exit_code == 0, f"Failed to query n{check_num}: {output.decode()}"
        count = output.decode().strip()
        assert int(count) >= 10, (
            f"Replication incomplete on n{check_num}: only {count} rows"
        )
        print(f"✅ n{check_num} has {count} rows — initial replication confirmed")

    print(f"\n✅ 3-node Spock cluster ready for ACE functional tests")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_functional_create_divergence(container_name, container_type):
    """ACE Functional Step 2: Disable subscriptions and create data divergence across all nodes"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    pguser = _ace_pguser(container_type)

    print(f"\n--- ACE: Disabling subscriptions and creating data divergence ---")

    # Disable all subscriptions on every node so writes stay local
    print(f"\n▶️  Disabling Spock subscriptions on all nodes")
    disable_sql = (
        "DO $$ DECLARE r RECORD; BEGIN "
        "FOR r IN SELECT sub_name FROM spock.subscription LOOP "
        "PERFORM spock.sub_disable(r.sub_name); "
        "END LOOP; END $$;"
    )
    for node_num in range(1, 4):
        node_port = ace_base_port + node_num - 1
        exit_code, output = container.exec_run(
            ["psql", "-h", "localhost", "-p", str(node_port), "-U", pguser,
             "-d", "postgres", "-c", disable_sql],
            user="root"
        )
        if exit_code != 0:
            out = output.decode()
            if "does not exist" in out or "no subscription" in out.lower():
                print(f"   ℹ️  n{node_num}: no subscriptions to disable")
            else:
                print(f"   ⚠️  Could not disable subscriptions on n{node_num}: {out}")
        else:
            print(f"   ✅ Subscriptions disabled on n{node_num}")

    time.sleep(2)

    # Spock replicates row data but not the sequence, so subscribers' n1_id_seq
    # is still at 1 while ids 1-10 already exist — a local SERIAL insert would
    # collide on the PK. Advance the sequence past the current max id before each
    # node's divergent insert.
    advance_seq = (
        "SELECT setval(pg_get_serial_sequence('public.n1','id'), "
        "(SELECT COALESCE(MAX(id), 1) FROM public.n1)); "
    )

    # Insert 5 unique rows on n2 (these will NOT replicate to n1/n3)
    print(f"\n▶️  Inserting 5 divergent rows on n2")
    exit_code, output = container.exec_run(
        ["psql", "-h", "localhost", "-p", str(ace_base_port + 1), "-U", pguser,
         "-d", "postgres", "-c",
         advance_seq +
         "INSERT INTO public.n1 (data) "
         "SELECT 'n2_divergent_' || g FROM generate_series(1, 5) g;"],
        user="root"
    )
    assert exit_code == 0, f"Failed to insert divergent rows on n2: {output.decode()}"
    print(f"✅ 5 divergent rows inserted on n2")

    # Insert 5 unique rows on n3
    print(f"\n▶️  Inserting 5 divergent rows on n3")
    exit_code, output = container.exec_run(
        ["psql", "-h", "localhost", "-p", str(ace_base_port + 2), "-U", pguser,
         "-d", "postgres", "-c",
         advance_seq +
         "INSERT INTO public.n1 (data) "
         "SELECT 'n3_divergent_' || g FROM generate_series(1, 5) g;"],
        user="root"
    )
    assert exit_code == 0, f"Failed to insert divergent rows on n3: {output.decode()}"
    print(f"✅ 5 divergent rows inserted on n3")

    # Insert 100 rows on n1
    print(f"\n▶️  Adding 100 rows on n1")
    exit_code, output = container.exec_run(
        ["psql", "-h", "localhost", "-p", str(ace_base_port), "-U", pguser,
         "-d", "postgres", "-c",
         advance_seq +
         "INSERT INTO public.n1 (data) "
         "SELECT 'n1_load_' || g FROM generate_series(1, 100) g;"],
        user="root"
    )
    assert exit_code == 0, f"Failed to insert 100 rows on n1: {output.decode()}"
    print(f"✅ 100 rows added on n1")

    # Print row counts per node to confirm divergence
    print(f"\n▶️  Row counts per node:")
    for node_num in range(1, 4):
        node_port = ace_base_port + node_num - 1
        exit_code, output = container.exec_run(
            ["psql", "-h", "localhost", "-p", str(node_port), "-U", pguser,
             "-d", "postgres", "-t", "-c", "SELECT COUNT(*) FROM public.n1;"],
            user="root"
        )
        print(f"   n{node_num}: {output.decode().strip()} rows")

    print(f"\n✅ Data divergence created — each node has unique rows")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_functional_create_config(container_name, container_type):
    """ACE Functional Step 3: Write pg_service.conf, run ace cluster init and config init, create pgcrypto"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    pguser = _ace_pguser(container_type)
    n1_port = ace_base_port
    n2_port = ace_base_port + 1
    n3_port = ace_base_port + 2
    pg_service_path = "/tmp/pg_service.conf"
    ace_yaml_path = "/tmp/ace.yaml"

    print(f"\n--- ACE: Creating configuration files ---")

    # Write pg_service.conf
    pg_service_content = (
        f"[{ace_cluster_name}]\n"
        f"dbname=postgres\n"
        f"user={pguser}\n"
        f"password={ace_pg_password}\n"
        f"\n"
        f"[{ace_cluster_name}.n1]\n"
        f"host=localhost\n"
        f"port={n1_port}\n"
        f"\n"
        f"[{ace_cluster_name}.n2]\n"
        f"host=localhost\n"
        f"port={n2_port}\n"
        f"\n"
        f"[{ace_cluster_name}.n3]\n"
        f"host=localhost\n"
        f"port={n3_port}\n"
    )
    exit_code, output = container.exec_run(
        ["bash", "-c",
         f"cat > {pg_service_path} << 'EOF'\n{pg_service_content}\nEOF"],
        user="root"
    )
    assert exit_code == 0, f"Failed to write pg_service.conf: {output.decode()}"
    print(f"✅ pg_service.conf written to {pg_service_path}")

    # ace cluster init
    print(f"\n▶️  Running: ace cluster init --path {pg_service_path}")
    exit_code, output = container.exec_run(
        ["bash", "-c",
         f"cd /tmp && PGSERVICEFILE={pg_service_path} "
         f"/usr/bin/ace cluster init --path {pg_service_path}"],
        user="root"
    )
    output_str = output.decode()
    print(f"   {output_str[:500]}")
    if exit_code != 0:
        print(f"   ⚠️  ace cluster init exit code {exit_code} (output above)")
    else:
        print(f"✅ ace cluster init completed")

    # ace config init
    print(f"\n▶️  Running: ace config init --path {ace_yaml_path}")
    exit_code, output = container.exec_run(
        ["bash", "-c",
         f"cd /tmp && PGSERVICEFILE={pg_service_path} "
         f"/usr/bin/ace config init --path {ace_yaml_path}"],
        user="root"
    )
    output_str = output.decode()
    print(f"   {output_str[:500]}")
    if exit_code != 0:
        print(f"   ⚠️  ace config init exit code {exit_code} (output above)")
    else:
        print(f"✅ ace config init completed — ace.yaml at {ace_yaml_path}")

    # Create pgcrypto extension on all nodes (required for mtree SHA-256 hashing)
    print(f"\n▶️  Creating pgcrypto extension on all nodes")
    for node_num in range(1, 4):
        node_port = ace_base_port + node_num - 1
        exit_code, output = container.exec_run(
            ["psql", "-h", "localhost", "-p", str(node_port), "-U", pguser,
             "-d", "postgres", "-c",
             "CREATE EXTENSION IF NOT EXISTS pgcrypto;"],
            user="root"
        )
        assert exit_code == 0, (
            f"Failed to create pgcrypto on n{node_num}: {output.decode()}"
        )
        print(f"✅ pgcrypto created on n{node_num}")

    print(f"\n✅ ACE configuration complete")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_table_diff(container_name, container_type):
    """ACE Functional Step 4: Run ace table-diff to detect row differences across nodes"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    pg_service_path = "/tmp/pg_service.conf"

    print(f"\n--- ACE: Running table-diff on public.n1 ---")

    exit_code, output = container.exec_run(
        ["bash", "-c",
         f"cd /tmp && PGSERVICEFILE={pg_service_path} "
         f"/usr/bin/ace table-diff -o html public.n1"],
        user="root"
    )
    output_str = output.decode()
    print(f"   Output:\n{output_str[:1000]}")

    assert exit_code == 0, f"ace table-diff failed (exit {exit_code}):\n{output_str}"
    print(f"✅ ace table-diff completed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_mtree_init(container_name, container_type):
    """ACE Functional Step 5: Initialize Merkle tree infrastructure on all nodes"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    pg_service_path = "/tmp/pg_service.conf"

    print(f"\n--- ACE: Running mtree init for cluster '{ace_cluster_name}' ---")

    exit_code, output = container.exec_run(
        ["bash", "-c",
         f"cd /tmp && PGSERVICEFILE={pg_service_path} "
         f"/usr/bin/ace mtree init --dbname postgres {ace_cluster_name}"],
        user="root"
    )
    output_str = output.decode()
    print(f"   Output:\n{output_str[:1000]}")

    assert exit_code == 0, f"ace mtree init failed (exit {exit_code}):\n{output_str}"
    print(f"✅ ace mtree init completed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_mtree_build(container_name, container_type):
    """ACE Functional Step 6: Build Merkle tree for public.n1"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    pg_service_path = "/tmp/pg_service.conf"

    print(f"\n--- ACE: Running mtree build for public.n1 ---")

    exit_code, output = container.exec_run(
        ["bash", "-c",
         f"cd /tmp && PGSERVICEFILE={pg_service_path} "
         f"/usr/bin/ace mtree build --dbname postgres {ace_cluster_name} public.n1"],
        user="root"
    )
    output_str = output.decode()
    print(f"   Output:\n{output_str[:1000]}")

    assert exit_code == 0, f"ace mtree build failed (exit {exit_code}):\n{output_str}"
    print(f"✅ ace mtree build completed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_mtree_table_diff(container_name, container_type):
    """ACE Functional Step 7: Detect differences using Merkle tree comparison"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    pg_service_path = "/tmp/pg_service.conf"

    print(f"\n--- ACE: Running mtree table-diff for public.n1 ---")

    exit_code, output = container.exec_run(
        ["bash", "-c",
         f"cd /tmp && PGSERVICEFILE={pg_service_path} "
         f"/usr/bin/ace mtree table-diff --dbname postgres {ace_cluster_name} public.n1 -o html"],
        user="root"
    )
    output_str = output.decode()
    print(f"   Output:\n{output_str[:1000]}")

    assert exit_code == 0, f"ace mtree table-diff failed (exit {exit_code}):\n{output_str}"
    print(f"✅ ace mtree table-diff completed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_mtree_update(container_name, container_type):
    """ACE Functional Step 8: Advance the Merkle tree to the current LSN"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    pg_service_path = "/tmp/pg_service.conf"

    print(f"\n--- ACE: Running mtree update for public.n1 ---")

    exit_code, output = container.exec_run(
        ["bash", "-c",
         f"cd /tmp && PGSERVICEFILE={pg_service_path} "
         f"/usr/bin/ace mtree update --dbname postgres {ace_cluster_name} public.n1"],
        user="root"
    )
    output_str = output.decode()
    print(f"   Output:\n{output_str[:1000]}")

    assert exit_code == 0, f"ace mtree update failed (exit {exit_code}):\n{output_str}"
    print(f"✅ ace mtree update completed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_functional_cleanup(container_name, container_type):
    """ACE Functional Step 9: Stop all PostgreSQL nodes used in ACE functional tests"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    pgbin = _ace_pgbin(container_type)
    pguser = _ace_pguser(container_type)

    print(f"\n--- ACE: Stopping all PostgreSQL nodes ---")

    for node_num in range(1, 4):
        node_name = f"n{node_num}"
        node_pgdata = f"/tmp/{node_name}"
        node_port = ace_base_port + node_num - 1

        success, _, message = pg_server_management.stop_server(
            container, pgbin, node_pgdata, str(node_port), pguser
        )
        if success:
            print(f"✅ {node_name} stopped")
        else:
            print(f"⚠️  Could not stop {node_name}: {message}")

    print(f"\n✅ ACE functional test cleanup complete")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_package_uninstall(container_name, container_type):
    """Uninstall ace package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    ace_package = config["ace_package"]

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Uninstalling {ace_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, ace_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {ace_package}: {str(e)}")


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
            container, pguser=pguser
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        # Display cleanup details
        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
        if cleanup_summary["user_removed"]:
            print(f"   User removed: {pguser}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")

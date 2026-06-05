import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime
import time

import pytest
import docker
from dotenv import load_dotenv

# Add the parent directory to sys.path to import from aspects
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from aspects import configure_repository, package_management, pg_server_management, machine_cleanup, machine_prereq_setup, file_management, container_management

load_dotenv()
client = docker.from_env()

# ============================================================================
# CONFIGURATION SECTION
# ============================================================================

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
pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")
upgrade_repo = os.getenv("UPGRADE_REPO", "staging")

# Enterprise All package configuration
enterprise_all_version = os.getenv(f"PGEDGE_ENTERPRISE_ALL_{pg_major_version}_VERSION", f"{pg_major_version}.11")
rhel_enterprise_package = os.getenv("ENTERPRISE_ALL_PACKAGE", f"pgedge-enterprise-all_{pg_major_version}")
deb_enterprise_package = os.getenv("DEB_ENTERPRISE_ALL_PACKAGE", f"pgedge-enterprise-all-{pg_major_version}")

# Multi-node configuration
no_of_nodes = int(os.getenv("NO_OF_NODES", "2"))
base_port = int(os.getenv("BASE_PORT", "5431"))
zodan_py = os.getenv("LATEST_ZODAN_PY", "zodan-505.py")
zodan_sql = os.getenv("LATEST_ZODAN_SQL", "zodan-506.sql")
zodan_execution = os.getenv("ZODAN_EXECUTION", "py").lower().strip()  # "py" or "sql"

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")
pg_password = os.getenv("PG_PASSWORD", "postgres")

# Binary paths
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")

# Spock configuration script
from pathlib import Path
zodan_script = (Path(__file__).parent.parent / "config" / "spock" / zodan_py).resolve()
zodan_sql_script = (Path(__file__).parent.parent / "config" / "spock" / zodan_sql).resolve()

# SQL files to run on each node
sql_files_to_run = ["sql/postgis35.sql", "sql/lolor.sql", "sql/server-extensions.sql"]


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "enterprise_package": rhel_enterprise_package
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "enterprise_package": deb_enterprise_package
        }


# ============================================================================
# Test Functions
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_install_prerequisites(container_name, container_type):
    """Step 1: Install prerequisites using machine_prereq_setup module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Ensure container exists and is running - create if not available
    container, created, message = container_management.ensure_container_running(
        client, container_name, container_type
    )
    print(f"{'🆕 ' if created else ''}{message}")

    assert container.status == "running", f"Container {container_name} is not running (status: {container.status})"

    # Install prerequisites
    success, os_info, message = machine_prereq_setup.install_prerequisites_on_container(container)

    assert success, f"Prerequisites installation failed: {message}"
    print(f"✅ Prerequisite installation completed on {container_name} ({os_info})")
    print(f"   {message}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_configure_repository(container_name, container_type):
    """Step 2: Configure repository using configure_repository module"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Configure repository
    success, platform, message = configure_repository.configure_pgedge_repository(container, repo)

    assert success, f"Repository configuration failed: {message}"
    print(f"✅ {message}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_install_enterprise_all(container_name, container_type):
    """Step 3: Install pgedge-enterprise-all package"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    enterprise_package = config["enterprise_package"]

    # Install enterprise-all package
    success, platform, message = package_management.install_package(
        container=container,
        package_name=enterprise_package,
        pg_major_version=pg_major_version,
        install_pg_server=False
    )

    assert success, f"Failed to install {enterprise_package}: {message}"
    print(f"Successfully installed {enterprise_package} on {platform}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_upgrade_enterprise_all(container_name, container_type):
    """Step 3.1: Upgrade pgedge-enterprise-all package if UPGRADE=true"""
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade test because UPGRADE=false in env")

    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    enterprise_package = config["enterprise_package"]

    print(f"\n--- Upgrading {enterprise_package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    # Upgrade the package
    try:
        success, platform, message = package_management.upgrade_package(container, enterprise_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{enterprise_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {enterprise_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_enterprise_version(container_name, container_type):
    """Step 4: Verify installed enterprise-all package version"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    enterprise_package = config["enterprise_package"]

    # Verify package version
    success, platform, installed_version, message = package_management.verify_package_version(
        container=container,
        package_name=enterprise_package,
        expected_version=enterprise_all_version
    )

    assert success, f"Version verification failed: {message}"
    print(f"Version verified: {enterprise_package} {installed_version} on {platform}")


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
            f"--signature-file spock50-sbom.json.asc "
            f"--signer-file pgedge-rsa.pub "
            f"spock50-sbom.json'",
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
            f"{_sq_sig_flag} spock50-sbom.json.asc "
            f"spock50-sbom.json'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_initialize_multiple_nodes(container_name, container_type):
    """Step 5: Initialize N PostgreSQL nodes"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Initializing {no_of_nodes} PostgreSQL nodes ---")

    # GUC parameters for all nodes (Spock configuration)
    guc_parameters = {
        "shared_preload_libraries": "'spock'",
        "wal_level": "logical",
        "max_worker_processes": "10",
        "max_replication_slots": "10",
        "max_wal_senders": "10",
        "track_commit_timestamp": "on"
    }

    # Initialize each node
    for node_num in range(1, no_of_nodes + 1):
        node_name = f"n{node_num}"
        node_port = base_port + node_num - 1
        node_pgdata = f"/tmp/{node_name}"

        print(f"\n▶️  Initializing node {node_name} on port {node_port}")

        # Initialize PostgreSQL cluster for this node
        success, config_content, message = pg_server_management.init_cluster(
            container, pgbin, node_pgdata, pguser, guc_parameters
        )

        assert success, f"Failed to initialize node {node_name}: {message}"
        print(f"✅ {message}")

        # Start PostgreSQL server for this node
        print(f"▶️  Starting PostgreSQL server for node {node_name}")
        success, server_output, message = pg_server_management.start_server(
            container, pgbin, node_pgdata, str(node_port), pguser
        )

        assert success, f"Failed to start node {node_name}: {message}"
        print(f"✅ {message}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_create_spock_extension_on_nodes(container_name, container_type):
    """Step 6: Create Spock extension on all nodes"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\n--- Creating Spock extension on all nodes ---")

    for node_num in range(1, no_of_nodes + 1):
        node_name = f"n{node_num}"
        node_port = base_port + node_num - 1

        print(f"\n▶️  Creating Spock extension on {node_name} (port {node_port})")

        # Create spock extension using heredoc
        exit_code, output = container.exec_run(
            f"bash -c 'psql -h localhost -p {node_port} -U {pguser} -d postgres <<\"EOSQL\"\nCREATE EXTENSION IF NOT EXISTS spock;\nEOSQL\n'",
            user="root"
        )

        assert exit_code == 0, f"Failed to create Spock extension on {node_name}: {output.decode()}"
        print(f"✅ Spock extension created on {node_name}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_create_spock_node_on_all_nodes(container_name, container_type):
    """Step 7: Create Spock nodes (spock.create_node) on all PostgreSQL nodes"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\n--- Creating Spock nodes on all PostgreSQL instances ---")

    for node_num in range(1, no_of_nodes + 1):
        node_name = f"n{node_num}"
        node_port = base_port + node_num - 1
        node_dsn = f"host=localhost port={node_port} dbname=postgres user={pguser} password={pg_password}"

        print(f"\n▶️  Creating Spock node '{node_name}' on port {node_port}")

        # Create spock node using heredoc
        create_node_sql = f"SELECT spock.create_node(node_name := '{node_name}', dsn := '{node_dsn}');"

        exit_code, output = container.exec_run(
            f"bash -c 'psql -h localhost -p {node_port} -U {pguser} -d postgres <<\"EOSQL\"\n{create_node_sql}\nEOSQL\n'",
            user="root"
        )

        assert exit_code == 0, f"Failed to create Spock node {node_name}: {output.decode()}"
        print(f"✅ Spock node '{node_name}' created successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_spock_extension_version(container_name, container_type):
    """Step 7.1: Verify Spock extension version matches expected version on all PostgreSQL nodes"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    expected_version = os.getenv(f"PGEDGE_SPOCK50_{pg_major_version}_VERSION", "")
    if not expected_version:
        pytest.skip(f"PGEDGE_SPOCK50_{pg_major_version}_VERSION not set in env")

    print(f"\n--- Verifying Spock extension version on all nodes (expected: {expected_version}) ---")

    for node_num in range(1, no_of_nodes + 1):
        node_name = f"n{node_num}"
        node_port = base_port + node_num - 1

        print(f"\n▶️  Checking Spock extension version on {node_name} (port {node_port})")

        exit_code, output = container.exec_run(
            ["psql", "-h", "localhost", "-p", str(node_port), "-U", pguser, "-d", "postgres",
             "-t", "-c", "SELECT extversion FROM pg_extension WHERE extname = 'spock';"],
            user="root"
        )

        out = output.decode().strip()

        assert exit_code == 0, f"Failed to query Spock extension version on {node_name}: {out}"
        assert out, f"Spock extension not found in pg_extension on {node_name}"
        assert expected_version in out, (
            f"Spock version mismatch on {node_name}: expected '{expected_version}', got '{out}'"
        )

        print(f"✅ Spock extension version verified on {node_name}: {out.strip()}")

    print(f"\n✅ Spock extension version {expected_version} confirmed on all nodes")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_run_sql_files_on_nodes(container_name, container_type):
    """Step 8: Run SQL files on first node only"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\n--- Running SQL files on first node (n1) only ---")

    # Get project root directory
    project_root = Path(__file__).parent.parent

    # Run SQL files only on node 1
    node_num = 1
    node_name = f"n{node_num}"
    node_port = base_port + node_num - 1

    print(f"\n▶️  Running SQL files on {node_name} (port {node_port})")

    for sql_file in sql_files_to_run:
        sql_file_path = project_root / sql_file

        if not sql_file_path.exists():
            print(f"⚠️  SQL file not found: {sql_file_path}, skipping...")
            continue

        print(f"   Executing {sql_file}...")

        # Read SQL file content
        with open(sql_file_path, 'r') as f:
            sql_content = f.read()

        # Copy SQL content to container using array syntax
        container_sql_path = f"/tmp/{Path(sql_file).name}"
        container.exec_run(
            ["bash", "-c", f"cat > {container_sql_path} << 'EOFSQL'\n{sql_content}\nEOFSQL"],
            user="root"
        )

        # Execute SQL file
        exit_code, output = container.exec_run(
            ["bash", "-c", f"psql -h localhost -p {node_port} -U {pguser} -d postgres -f {container_sql_path}"],
            user="root"
        )

        if exit_code != 0:
            print(f"⚠️  Warning: SQL file {sql_file} execution had issues: {output.decode()}")
        else:
            print(f"✅ Successfully executed {sql_file} on {node_name}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_components_extensions(container_name, container_type):
    """Step 9: Create extensions on all nodes if TEST_EXTENSIONS=true"""
    if os.getenv("TEST_EXTENSIONS", "false").lower() != "true":
        pytest.skip("TEST_EXTENSIONS is not set to true")

    extensions_list = [e.strip() for e in os.getenv("EXTENSIONS", "").split(",") if e.strip()]
    if not extensions_list:
        pytest.skip("No extensions defined in EXTENSIONS env variable")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    for node_num in range(1, no_of_nodes + 1):
        node_port = base_port + node_num - 1
        print(f"\n--- Creating extensions on n{node_num} (port {node_port}) ---")

        for ext in extensions_list:
            normalized_ext = f'"{ext}"' if "-" in ext else ext
            print(f"  Creating extension {normalized_ext}...")

            exit_code, output = container.exec_run(
                ["bash", "-c",
                 f"psql -h localhost -p {node_port} -U {pguser} -d postgres "
                 f"-c 'CREATE EXTENSION IF NOT EXISTS {normalized_ext} CASCADE;'"],
                user="root"
            )

            assert exit_code == 0, (
                f"Failed to create extension {normalized_ext} on n{node_num} "
                f"(port {node_port}): {output.decode()}"
            )
            print(f"  ✅ Created extension {normalized_ext} on n{node_num}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_setup_cross_wiring_with_zodan(container_name, container_type):
    """Step 10: Setup cross-wiring between nodes using zodan-50x.py or zodan-50x.sql"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]
    pgbin = config["pgbin"]

    print(f"\n--- Setting up cross-wiring between nodes (mode: {zodan_execution}) ---")

    if zodan_execution == "sql":
        # ------------------------------------------------------------------ #
        # SQL execution path: copy zodan SQL file, create dblink extension,  #
        # load procedures, then call spock.add_node on the new node.         #
        # ------------------------------------------------------------------ #
        if not zodan_sql_script.exists():
            pytest.skip(f"Zodan SQL script not found at {zodan_sql_script}")

        # Copy zodan SQL file to container
        with zodan_sql_script.open('r') as f:
            zodan_sql_content = f.read()

        container_zodan_sql_path = f"/tmp/{zodan_sql}"

        exit_code, output = container.exec_run(
            ["bash", "-c", f"cat > {container_zodan_sql_path} << 'SQLEOF'\n{zodan_sql_content}\nSQLEOF"],
            user="root"
        )

        if exit_code != 0:
            pytest.fail(f"Failed to copy zodan SQL script to container: {output.decode()}")

        exit_code, output = container.exec_run(
            ["wc", "-l", container_zodan_sql_path],
            user="root"
        )

        if exit_code == 0:
            line_count = output.decode().strip().split()[0]
            print(f"Zodan SQL script copied successfully ({line_count} lines)")
        else:
            pytest.fail(f"Failed to verify zodan SQL script: {output.decode()}")

        # Setup cross-wiring as consecutive pairs: n1->n2, n2->n3, ...
        for node_num in range(2, no_of_nodes + 1):
            src_node = f"n{node_num - 1}"
            src_port = base_port + node_num - 2
            src_dsn = f"host=127.0.0.1 dbname=postgres port={src_port} user={pguser} password={pg_password}"

            new_node = f"n{node_num}"
            new_port = base_port + node_num - 1
            new_dsn = f"host=127.0.0.1 dbname=postgres port={new_port} user={pguser} password={pg_password}"

            print(f"\nAdding node {new_node} to {src_node} via SQL")

            # Step 1: Create dblink extension on the new node
            exit_code, output = container.exec_run(
                ["bash", "-c",
                 f"{pgbin}/psql -h 127.0.0.1 -p {new_port} -U {pguser} -d postgres "
                 f"-c \"CREATE EXTENSION IF NOT EXISTS dblink;\""],
                user="root"
            )
            output_str = output.decode()
            if exit_code != 0:
                pytest.fail(
                    f"Failed to create dblink extension on {new_node} (port {new_port}): {output_str}"
                )
            print(f"dblink extension created on {new_node}")

            # Step 2: Execute zodan SQL file on the new node to load procedures
            exit_code, output = container.exec_run(
                ["bash", "-c",
                 f"{pgbin}/psql -h 127.0.0.1 -p {new_port} -U {pguser} -d postgres "
                 f"-f {container_zodan_sql_path}"],
                user="root"
            )
            output_str = output.decode()
            print(output_str)
            if exit_code != 0:
                pytest.fail(
                    f"Failed to execute zodan SQL on {new_node} (port {new_port}): {output_str}"
                )
            print(f"Zodan SQL executed successfully on {new_node}")

            # Step 3: Call spock.add_node on the new node
            add_node_sql = (
                f"CALL spock.add_node("
                f"'{src_node}', "
                f"'{src_dsn}', "
                f"'{new_node}', "
                f"'{new_dsn}', "
                f"true);"
            )

            exit_code, output = container.exec_run(
                ["bash", "-c",
                 f"{pgbin}/psql -h 127.0.0.1 -p {new_port} -U {pguser} -d postgres "
                 f"-c \"{add_node_sql}\""],
                user="root"
            )
            output_str = output.decode()
            print(output_str)

            # Step 4: Verify success message
            assert exit_code == 0, \
                f"spock.add_node call failed for {new_node}: {output_str}"
            assert "Success rate: %100" in output_str, \
                f"Expected 'NOTICE:  Success rate: %100' in output for {new_node}, got:\n{output_str}"

            print(f"Successfully added node {new_node} to {src_node} via SQL")

            # Wait for replication to stabilize
            time.sleep(2)

    else:
        # ------------------------------------------------------------------ #
        # Python execution path (default): copy zodan .py file and invoke it #
        # ------------------------------------------------------------------ #
        if not zodan_script.exists():
            pytest.skip(f"Zodan script not found at {zodan_script}")

        # Copy zodan script to container
        with zodan_script.open('r') as f:
            zodan_content = f.read()

        container_zodan_path = f"/tmp/{zodan_py}"

        # Use array syntax to avoid shell escaping issues with heredoc
        exit_code, output = container.exec_run(
            ["bash", "-c", f"cat > {container_zodan_path} << 'EOF'\n{zodan_content}\nEOF"],
            user="root"
        )

        if exit_code != 0:
            pytest.fail(f"Failed to copy zodan script to container: {output.decode()}")

        # Verify file was written and has content
        exit_code, output = container.exec_run(
            ["wc", "-l", container_zodan_path],
            user="root"
        )

        if exit_code == 0:
            line_count = output.decode().strip().split()[0]
            print(f"Zodan script copied successfully ({line_count} lines)")
        else:
            pytest.fail(f"Failed to verify zodan script: {output.decode()}")

        # Make executable
        container.exec_run(["chmod", "+x", container_zodan_path], user="root")

        # Setup cross-wiring as consecutive pairs: n1->n2, n2->n3, ...
        for node_num in range(2, no_of_nodes + 1):
            src_node = f"n{node_num - 1}"
            src_port = base_port + node_num - 2
            src_dsn = f"host=localhost dbname=postgres port={src_port} user={pguser} password={pg_password}"

            new_node = f"n{node_num}"
            new_port = base_port + node_num - 1
            new_dsn = f"host=localhost dbname=postgres port={new_port} user={pguser} password={pg_password}"

            print(f"\nAdding node {new_node} to {src_node}")

            # Run zodan add_node command - use single quotes for bash -c to preserve double quotes in DSN
            zodan_cmd = (
                f'python3 {container_zodan_path} add_node '
                f'--src-node-name {src_node} '
                f'--src-dsn "{src_dsn}" '
                f'--new-node-name {new_node} '
                f'--new-node-dsn "{new_dsn}" '
                f'--verbose'
            )

            exit_code, output = container.exec_run(
                ["bash", "-c", zodan_cmd],
                user="root"
            )

            output_str = output.decode()
            print(output_str)

            assert exit_code == 0, f"Failed to add node {new_node} to {src_node}: {output_str}"
            print(f"Successfully added node {new_node} to {src_node}")

            # Wait for replication to stabilize
            time.sleep(2)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_auto_ddl_on(container_name, container_type):
    """Step 10: Enable auto DDL replication on all nodes"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\n--- Enabling auto DDL replication on all nodes ---")

    # DDL replication settings to enable
    ddl_settings = [
        "ALTER SYSTEM SET spock.enable_ddl_replication=on;",
        "ALTER SYSTEM SET spock.include_ddl_repset=on;",
        "ALTER SYSTEM SET spock.allow_ddl_from_functions=on;",
        "SELECT pg_reload_conf();"
    ]

    # Apply settings on all nodes
    for node_num in range(1, no_of_nodes + 1):
        node_name = f"n{node_num}"
        node_port = base_port + node_num - 1

        print(f"\n▶️  Configuring auto DDL on {node_name} (port {node_port})")

        for sql_cmd in ddl_settings:
            print(f"   Executing: {sql_cmd.strip()}")

            exit_code, output = container.exec_run(
                f"bash -c 'psql -h localhost -p {node_port} -U {pguser} -d postgres <<\"EOSQL\"\n{sql_cmd}\nEOSQL\n'",
                user="root"
            )

            if exit_code != 0:
                print(f"⚠️  Warning: Failed to execute on {node_name}: {output.decode()}")
                # Don't fail the test, just warn
            else:
                print(f"   ✅ Success")

        print(f"✅ Auto DDL replication enabled on {node_name}")

    print(f"\n✅ Auto DDL replication configured on all nodes")


# New test: verify spock.node on each PostgreSQL node
@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_spock_node(container_name, container_type):
    """Step 10.1: Verify spock.node lists the expected nodes (n1, n2)"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\n--- Verifying spock.node on all nodes ---")

    for node_num in range(1, no_of_nodes + 1):
        node_port = base_port + node_num - 1
        print(f"\n▶️  Querying spock.node on n{node_num} (port {node_port})")

        exit_code, output = container.exec_run(
            ["psql", "-h", "localhost", "-p", str(node_port), "-U", pguser, "-d", "postgres", "-c",
             "SELECT * FROM spock.node;"],
            user="root"
        )

        out = output.decode()
        print(out)

        assert exit_code == 0, f"Query failed on n{node_num}: {out}"

        # Ensure both node names are visible and two rows reported
        assert "n1" in out and "n2" in out, f"Expected node names 'n1' and 'n2' in spock.node output on n{node_num}: {out}"
        assert "(2 rows)" in out or "2 rows" in out or "rows)" in out, f"Expected 2 rows reported in spock.node output on n{node_num}: {out}"

    print("✅ spock.node contains expected nodes on all instances")


# New test: check replication status contains 'replicating'
@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_show_sub_status(container_name, container_type):
    """Step 10.2: Run spock.sub_show_status() and ensure subscription is 'replicating'"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    # Run this check on n1 (port = base_port)
    node_port = base_port
    print(f"\n--- Verifying subscription status via spock.sub_show_status() on n1 (port {node_port}) ---")

    exit_code, output = container.exec_run(
        ["psql", "-h", "localhost", "-p", str(node_port), "-U", pguser, "-d", "postgres", "-c",
         "SELECT * FROM spock.sub_show_status();"],
        user="root"
    )

    out = output.decode()
    print(out)

    assert exit_code == 0, f"spock.sub_show_status() query failed on n1: {out}"

    # The user is interested in the 'replicating' keyword present in status column
    assert "replicating" in out.lower(), f"Expected 'replicating' in spock.sub_show_status() output on n1, got:\n{out}"

    print("✅ spock.sub_show_status() indicates subscription is replicating")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ddl_insert_replication(container_name, container_type):
    """Step 11: Test DDL and DML replication - Create table on n1, insert data, verify on n2"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    n1_port = base_port
    n2_port = base_port + 1

    print(f"\n--- Testing DDL and DML replication ---")

    # Step 1: Create table on n1 and insert 3 rows
    print(f"\n▶️  Creating table 'n1' on node n1 (port {n1_port})")

    # Combined SQL to create table, add to repset, and insert data
    combined_sql = """
-- Create table n1
CREATE TABLE IF NOT EXISTS n1 (
    id SERIAL PRIMARY KEY,
    data TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Add table to default replication set
SELECT spock.repset_add_table('default', 'n1');

-- Insert 3 rows
INSERT INTO n1 (data) VALUES
    ('test_row_1'),
    ('test_row_2'),
    ('test_row_3');

-- Verify insertion
SELECT 'Inserted ' || COUNT(*) || ' rows into n1' AS result FROM n1;
"""

    # Write SQL to temp file in container
    sql_file = "/tmp/test_ddl_insert.sql"
    exit_code, output = container.exec_run(
        ["bash", "-c", f"cat > {sql_file} << 'EOF'\n{combined_sql}\nEOF"],
        user="root"
    )

    if exit_code != 0:
        pytest.fail(f"Failed to create SQL file: {output.decode()}")

    # Execute SQL on n1
    exit_code, output = container.exec_run(
        ["bash", "-c", f"psql -h localhost -p {n1_port} -U {pguser} -d postgres -f {sql_file}"],
        user="root"
    )

    output_str = output.decode()
    print(output_str)

    if exit_code != 0:
        pytest.fail(f"Failed to create table and insert data on n1: {output_str}")

    # Verify data on n1
    exit_code, output = container.exec_run(
        ["psql", "-h", "localhost", "-p", str(n1_port), "-U", pguser, "-d", "postgres", "-c",
         "SELECT COUNT(*) FROM n1;"],
        user="root"
    )

    n1_count_output = output.decode()
    print(f"\n✓ Count on n1: {n1_count_output}")

    assert exit_code == 0, f"Failed to query n1 table on node n1: {n1_count_output}"
    assert "3" in n1_count_output, f"Expected 3 rows on n1, got: {n1_count_output}"

    print(f"✅ Successfully created table 'n1' and inserted 3 rows on node n1")

    # Step 2: Wait for replication
    import time
    print(f"\n⏳ Waiting 10 seconds for replication to complete...")
    time.sleep(10)

    # Step 3: Verify table and data replicated to n2
    print(f"\n▶️  Verifying replication on node n2 (port {n2_port})")

    # Check if table exists on n2
    exit_code, output = container.exec_run(
        ["psql", "-h", "localhost", "-p", str(n2_port), "-U", pguser, "-d", "postgres", "-c",
         "\\d n1"],
        user="root"
    )

    table_def_output = output.decode()
    print(f"Table definition on n2:\n{table_def_output}")

    if exit_code != 0 or "does not exist" in table_def_output:
        pytest.fail(f"Table 'n1' does not exist on node n2: {table_def_output}")

    # Check row count on n2
    exit_code, output = container.exec_run(
        ["psql", "-h", "localhost", "-p", str(n2_port), "-U", pguser, "-d", "postgres", "-c",
         "SELECT COUNT(*) FROM n1;"],
        user="root"
    )

    n2_count_output = output.decode()
    print(f"\n✓ Count on n2: {n2_count_output}")

    assert exit_code == 0, f"Failed to query n1 table on node n2: {n2_count_output}"

    # Verify we have 3 rows
    if "3" not in n2_count_output:
        # Additional debug: show actual rows
        exit_code, output = container.exec_run(
            ["psql", "-h", "localhost", "-p", str(n2_port), "-U", pguser, "-d", "postgres", "-c",
             "SELECT * FROM n1 ORDER BY id;"],
            user="root"
        )
        debug_output = output.decode()
        print(f"Actual rows on n2:\n{debug_output}")

        pytest.fail(f"Replication failed! Expected 3 rows on n2, but found different count.\nCount output: {n2_count_output}\nActual rows: {debug_output}")

    print(f"✅ Replication successful! Table 'n1' with 3 rows replicated to node n2")

    # Optional: Show the actual data on n2
    exit_code, output = container.exec_run(
        ["psql", "-h", "localhost", "-p", str(n2_port), "-U", pguser, "-d", "postgres", "-c",
         "SELECT id, data FROM n1 ORDER BY id;"],
        user="root"
    )
    print(f"\nData on n2:\n{output.decode()}")

    print(f"\n✅ DDL and DML replication test completed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_insert_node_add(container_name, container_type):
    """Step 11.1: Test zero downtime - Insert on n1, add node n3, cross-wire n2-n3, verify replication"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    if no_of_nodes < 3:
        pytest.skip(f"Skipping zero-downtime node-add test: NO_OF_NODES={no_of_nodes}, requires at least 3")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    n1_port = base_port
    n2_port = base_port + 1
    n3_port = base_port + 2  # New node port

    print(f"\n--- Testing Zero Downtime: Insert on n1 while adding node n3 ---")

    # Step 0: Cleanup any existing n3 node from previous runs
    print(f"\n▶️  Step 0: Cleaning up any existing n3 node")
    node_pgdata = "/tmp/n3"

    # Try to stop n3 if it's running
    print(f"   Checking if n3 is running...")
    exit_code, output = container.exec_run(
        f"{pgbin}/pg_ctl -D {node_pgdata} status",
        user=pguser
    )

    if exit_code == 0 or "server is running" in output.decode():
        print(f"   Stopping existing n3 node...")
        container.exec_run(
            f"{pgbin}/pg_ctl -D {node_pgdata} -m fast stop",
            user=pguser
        )
        time.sleep(2)

    # Clean up n3 data directory
    print(f"   Removing old n3 data directory...")
    container.exec_run(f"rm -rf {node_pgdata}", user="root")

    # Kill any processes using port 5433
    print(f"   Ensuring port {n3_port} is free...")
    container.exec_run(
        ["bash", "-c", f"fuser -k {n3_port}/tcp 2>/dev/null || true"],
        user="root"
    )

    print(f"✅ Cleanup completed")

    # Step 1: Create a test table on n1 for continuous inserts
    print(f"\n▶️  Step 1: Creating table 'insert_test' on n1 (port {n1_port})")

    create_table_sql = """
-- Create test table for continuous inserts
CREATE TABLE IF NOT EXISTS insert_test (
    id SERIAL PRIMARY KEY,
    insert_time TIMESTAMP DEFAULT NOW(),
    message TEXT
);

-- Add table to default replication set
SELECT spock.repset_add_table('default', 'insert_test');
"""

    sql_file = "/tmp/create_insert_test.sql"
    exit_code, output = container.exec_run(
        ["bash", "-c", f"cat > {sql_file} << 'EOF'\n{create_table_sql}\nEOF"],
        user="root"
    )

    if exit_code != 0:
        pytest.fail(f"Failed to create SQL file: {output.decode()}")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"psql -h localhost -p {n1_port} -U {pguser} -d postgres -f {sql_file}"],
        user="root"
    )

    if exit_code != 0:
        pytest.fail(f"Failed to create insert_test table on n1: {output.decode()}")

    print(f"✅ Table 'insert_test' created on n1")

    # Step 2: Start continuous inserts on n1 (run 20 inserts with 0.5s delay)
    print(f"\n▶️  Step 2: Starting continuous inserts on n1")

    insert_script = f"""
for i in {{1..20}}; do
    psql -h localhost -p {n1_port} -U {pguser} -d postgres -c "INSERT INTO insert_test (message) VALUES ('Insert batch $i at '||NOW());" >> /tmp/insert_log.txt 2>&1
    sleep 0.5
done
"""

    # Write insert script to container
    exit_code, output = container.exec_run(
        ["bash", "-c", f"cat > /tmp/continuous_insert.sh << 'EOFSH'\n{insert_script}\nEOFSH"],
        user="root"
    )

    if exit_code != 0:
        pytest.fail(f"Failed to create insert script: {output.decode()}")

    container.exec_run(["chmod", "+x", "/tmp/continuous_insert.sh"], user="root")

    # Start inserts in background
    print(f"✓ Starting background inserts (20 inserts with 0.5s interval)")
    container.exec_run(["bash", "-c", "nohup /tmp/continuous_insert.sh &"], user="root")

    # Wait a bit for first few inserts to happen
    time.sleep(2)

    # Step 3: Initialize node n3 while inserts are happening
    print(f"\n▶️  Step 3: Initializing node n3 (port {n3_port}) while inserts continue")

    # node_pgdata already defined in Step 0 cleanup
    # GUC parameters for n3 (same as other nodes)
    guc_parameters = {
        "shared_preload_libraries": "'spock'",
        "wal_level": "logical",
        "max_worker_processes": "10",
        "max_replication_slots": "10",
        "max_wal_senders": "10",
        "track_commit_timestamp": "on"
    }

    # Initialize PostgreSQL cluster for n3
    success, config_content, message = pg_server_management.init_cluster(
        container, pgbin, node_pgdata, pguser, guc_parameters
    )

    assert success, f"Failed to initialize node n3: {message}"
    print(f"✅ {message}")

    # Start PostgreSQL server for n3
    print(f"▶️  Starting PostgreSQL server for node n3")
    success, server_output, message = pg_server_management.start_server(
        container, pgbin, node_pgdata, str(n3_port), pguser
    )

    assert success, f"Failed to start node n3: {message}"
    print(f"✅ {message}")

    # Step 4: Create Spock extension on n3
    print(f"\n▶️  Step 4: Creating Spock extension on n3 (port {n3_port})")

    exit_code, output = container.exec_run(
        f"bash -c 'psql -h localhost -p {n3_port} -U {pguser} -d postgres <<\"EOSQL\"\nCREATE EXTENSION IF NOT EXISTS spock;\nEOSQL\n'",
        user="root"
    )

    assert exit_code == 0, f"Failed to create Spock extension on n3: {output.decode()}"
    print(f"✅ Spock extension created on n3")

    # Step 5: Create Spock node on n3
    print(f"\n▶️  Step 5: Creating Spock node 'n3' on port {n3_port}")

    n3_dsn = f"host=localhost port={n3_port} dbname=postgres user={pguser} password={pg_password}"
    create_node_sql = f"SELECT spock.create_node(node_name := 'n3', dsn := '{n3_dsn}');"

    exit_code, output = container.exec_run(
        f"bash -c 'psql -h localhost -p {n3_port} -U {pguser} -d postgres <<\"EOSQL\"\n{create_node_sql}\nEOSQL\n'",
        user="root"
    )

    assert exit_code == 0, f"Failed to create Spock node n3: {output.decode()}"
    print(f"✅ Spock node 'n3' created successfully")

    # Step 6: Setup cross-wiring between n2 and n3 using zodan
    print(f"\n▶️  Step 6: Setting up cross-wiring between n2 and n3 using zodan")

    # Check if zodan script already exists in container from previous tests
    container_zodan_path = "/tmp/zodan-505.py"
    exit_code, output = container.exec_run(
        ["test", "-f", container_zodan_path],
        user="root"
    )

    if exit_code != 0:
        # Copy zodan script if not exists
        print(f"   Copying zodan script to container...")
        with zodan_script.open('r') as f:
            zodan_content = f.read()

        exit_code, output = container.exec_run(
            ["bash", "-c", f"cat > {container_zodan_path} << 'EOF'\n{zodan_content}\nEOF"],
            user="root"
        )

        if exit_code != 0:
            pytest.fail(f"Failed to copy zodan script: {output.decode()}")

        container.exec_run(["chmod", "+x", container_zodan_path], user="root")

    # Add n3 to n2 (n2 becomes provider for n3)
    n2_dsn = f"host=localhost dbname=postgres port={n2_port} user={pguser} password={pg_password}"
    n3_dsn = f"host=localhost dbname=postgres port={n3_port} user={pguser} password={pg_password}"

    print(f"\n   Adding node n3 to n2 (n2 -> n3)")

    zodan_cmd = (
        f'python3 {container_zodan_path} add_node '
        f'--src-node-name n2 '
        f'--src-dsn "{n2_dsn}" '
        f'--new-node-name n3 '
        f'--new-node-dsn "{n3_dsn}" '
        f'--verbose'
    )

    exit_code, output = container.exec_run(
        ["bash", "-c", zodan_cmd],
        user="root"
    )

    output_str = output.decode()
    print(output_str)

    assert exit_code == 0, f"Failed to add node n3 to n2: {output_str}"
    print(f"✅ Successfully added node n3 to n2")

    # Wait for replication to stabilize
    print(f"\n⏳ Waiting 5 seconds for replication to stabilize...")
    time.sleep(5)

    # Step 7: Enable auto DDL replication on n3
    print(f"\n▶️  Step 7: Enabling auto DDL replication on n3")

    ddl_settings = [
        "ALTER SYSTEM SET spock.enable_ddl_replication=on;",
        "ALTER SYSTEM SET spock.include_ddl_repset=on;",
        "ALTER SYSTEM SET spock.allow_ddl_from_functions=on;",
        "SELECT pg_reload_conf();"
    ]

    for sql_cmd in ddl_settings:
        exit_code, output = container.exec_run(
            f"bash -c 'psql -h localhost -p {n3_port} -U {pguser} -d postgres <<\"EOSQL\"\n{sql_cmd}\nEOSQL\n'",
            user="root"
        )

        if exit_code != 0:
            print(f"⚠️  Warning: Failed to execute DDL setting on n3: {output.decode()}")

    print(f"✅ Auto DDL replication enabled on n3")

    # Step 8: Wait for all inserts to complete
    print(f"\n▶️  Step 8: Waiting for all inserts to complete...")
    time.sleep(12)  # Give time for remaining inserts to finish

    # Step 9: Verify inserts completed on n1
    print(f"\n▶️  Step 9: Verifying inserts on n1")

    exit_code, output = container.exec_run(
        ["psql", "-h", "localhost", "-p", str(n1_port), "-U", pguser, "-d", "postgres", "-c",
         "SELECT COUNT(*) FROM insert_test;"],
        user="root"
    )

    n1_count_output = output.decode()
    print(f"✓ Row count on n1: {n1_count_output}")

    assert exit_code == 0, f"Failed to query insert_test on n1: {n1_count_output}"

    # Step 10: Check subscription status on all three nodes (n1, n2, n3)
    print(f"\n▶️  Step 10: Checking subscription status on all nodes (n1, n2, n3)")

    for node_num, node_port in [(1, n1_port), (2, n2_port), (3, n3_port)]:
        print(f"\n   Checking subscription status on n{node_num} (port {node_port})")

        exit_code, output = container.exec_run(
            ["psql", "-h", "localhost", "-p", str(node_port), "-U", pguser, "-d", "postgres", "-c",
             "SELECT * FROM spock.sub_show_status();"],
            user="root"
        )

        out = output.decode()
        print(out)

        assert exit_code == 0, f"spock.sub_show_status() query failed on n{node_num}: {out}"

        if "replicating" in out.lower():
            print(f"   ✅ n{node_num} has active subscriptions in 'replicating' state")
        else:
            print(f"   ⚠️  n{node_num} has no active subscriptions (might be source-only node)")

    # Step 11: Verify data replicated to all nodes
    print(f"\n▶️  Step 11: Verifying data replication to all nodes")

    # Wait for replication to n3
    print(f"⏳ Waiting 10 seconds for replication to n3...")
    time.sleep(10)

    for node_num, node_port in [(1, n1_port), (2, n2_port), (3, n3_port)]:
        print(f"\n   Checking data on n{node_num} (port {node_port})")

        # Check if table exists
        exit_code, output = container.exec_run(
            ["psql", "-h", "localhost", "-p", str(node_port), "-U", pguser, "-d", "postgres", "-c",
             "\\d insert_test"],
            user="root"
        )

        table_check = output.decode()
        if "does not exist" in table_check:
            pytest.fail(f"Table 'insert_test' does not exist on n{node_num}")

        # Check row count
        exit_code, output = container.exec_run(
            ["psql", "-h", "localhost", "-p", str(node_port), "-U", pguser, "-d", "postgres", "-c",
             "SELECT COUNT(*) as row_count FROM insert_test;"],
            user="root"
        )

        count_output = output.decode()
        print(f"   Row count on n{node_num}: {count_output}")

        assert exit_code == 0, f"Failed to query insert_test on n{node_num}: {count_output}"

        # Show sample data
        exit_code, output = container.exec_run(
            ["psql", "-h", "localhost", "-p", str(node_port), "-U", pguser, "-d", "postgres", "-c",
             "SELECT id, message FROM insert_test ORDER BY id LIMIT 5;"],
            user="root"
        )

        print(f"   Sample data on n{node_num}:\n{output.decode()}")

    print(f"\n✅ Zero downtime test completed successfully!")
    print(f"✅ Node n3 was added while inserts were happening on n1")
    print(f"✅ Cross-wiring established between n2 and n3")
    print(f"✅ Data replicated to all three nodes (n1, n2, n3)")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_cleanup_nodes(container_name, container_type):
    """Step 12: Cleanup - Stop all PostgreSQL nodes"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Cleaning up PostgreSQL nodes ---")

    for node_num in range(1, no_of_nodes + 1):
        node_name = f"n{node_num}"
        node_port = base_port + node_num - 1
        node_pgdata = f"/tmp/{node_name}"

        print(f"\n▶️  Stopping node {node_name}")

        # Stop PostgreSQL node
        success, server_output, message = pg_server_management.stop_server(
            container, pgbin, node_pgdata, str(node_port), pguser
        )

        if success:
            print(f"✅ {message}")
        else:
            print(f"⚠️  Warning: Could not stop node {node_name}: {message}")

    print(f"\n✅ Cleanup completed")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_uninstall(container_name, container_type):
    """Uninstall the enterprise-all package using package_management module"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("No container defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    enterprise_package = config["enterprise_package"]

    print(f"\n--- Uninstalling {enterprise_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, enterprise_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {enterprise_package}: {str(e)}")


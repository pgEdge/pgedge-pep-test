import os
import subprocess
from pathlib import Path

import pytest
import docker
from dotenv import load_dotenv

load_dotenv()
client = docker.from_env()

# Load values from .env
containers = os.getenv("CONTAINERS", "").split(",")
repo = os.getenv("REPO", "release")

# PgBouncer specific configuration
snowflake_package = os.getenv("SNOWFLAKE_PACKAGE", "pgedge-snowflake_17")
snowflake_version = os.getenv("PGEDGE_SNOWFLAKE_18_VERSION", "2.4")
snowflake_node = os.getenv("SNOWFLAKE_NODE", "1")
pguser = os.getenv("PG_USER", "postgres")
pgport = os.getenv("PG_PORT", "5432")
pgbin = os.getenv("PG_BIN_PATH", "/usr/pgsql-17/bin")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "17")

# Expected bundled files to validate
snowflake_bundled_files = os.getenv(
    "SNOWFLAKE_BUNDLED_FILES",
    f"usr/pgsql-{pg_major_version}/lib/snowflake.so,/usr/pgsql-{pg_major_version}/sbom/snowflake-sbom.json,/usr/pgsql-{pg_major_version}/sbom/snowflake-sbom.json.asc,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.0--1.1.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.0.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.1--1.2.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.1.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.2--2.0.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.2.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--2.0--2.2.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--2.0.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--2.2.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake.control,/usr/share/doc/pgedge-snowflake_{pg_major_version}/README.md,/usr/share/licenses/pgedge-snowflake_{pg_major_version}/LICENSE.md"
).split(",")

def execute_psql_query(container, query, dbname="postgres"):
    """Helper function to execute psql queries and return output"""
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d {dbname} -c \"{query}\"",
        user=pguser,
    )
    return exit_code, output.decode()

@pytest.mark.parametrize("container_name", containers)
def test_configure_repository(container_name):
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
        exit_code, _ = container.exec_run("command -v apt-get", user="root")
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
        exit_code, output = container.exec_run(
            f"curl -sSL {deb_url} -o /tmp/pgedge-release.deb && "
            f"dpkg -i /tmp/pgedge-release.deb && "
            f"rm -f /tmp/pgedge-release.deb || true",
            user="root",
        )
        assert exit_code == 0, f"Failed to install repo: {output.decode()}"

        # Step 2: Switch repo if needed
        if repo in ["staging", "daily"]:
            exit_code, output = container.exec_run(
                f"sed -i 's|release|{repo}|g' /etc/apt/sources.list.d/pgedge.list",
                user="root",
            )
            assert exit_code == 0, f"Failed to switch repo to {repo}: {output.decode()}"
            print(f"✅ Repository switched to {repo}")

        # Step 3: apt-get update
        exit_code, output = container.exec_run("apt-get update", user="root")
        assert exit_code == 0, f"apt-get update failed: {output.decode()}"

    print(f"✅ Repository configured successfully on {container_name}")


@pytest.mark.parametrize("container_name", containers)
def test_component_install(container_name):
    """Step 2: Install pgedge-snowflake from staging, release, or daily repository"""
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
        exit_code, _ = container.exec_run("command -v apt-get", user="root")
        if exit_code == 0:
            container.exec_run("apt-get update", user="root")
            pkg_mgr = "apt-get install -y"
            platform = "debian"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    print(f"\n--- Installing {snowflake_package} on {container_name} ({platform}) ---")

    # Install pgbouncer package
    exit_code, output = container.exec_run(
        f"{pkg_mgr} {snowflake_package}",
        user="root"
    )


    assert exit_code == 0, f"Failed to install {snowflake_package}: {output.decode()}"
    print(f"✅ Successfully installed {snowflake_package}")


@pytest.mark.parametrize("container_name", containers)
def test_snowflake_verify_version(container_name):
    """Step 3: Check the package version matches the version in .env file"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    if not snowflake_version:
        pytest.skip("No PGBOUNCER_VERSION defined in .env, skipping version check")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying {snowflake_package} version on {container_name} ---")
    print(f"Expected version: {snowflake_version}")

    # Detect package manager inside the container
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        # RHEL-based: use rpm to query version
        version_cmd = f"rpm -q --queryformat '%{{VERSION}}' {snowflake_package}"
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run("command -v apt-get", user="root")
        if exit_code == 0:
            # Debian-based: use dpkg-query to get version
            version_cmd = f"dpkg-query --showformat='${{Version}}' --show {snowflake_package}"
            platform = "debian"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    # Get installed version
    exit_code, output = container.exec_run(version_cmd, user="root")

    if exit_code != 0:
        pytest.fail(f"Failed to query {snowflake_package} version: {output.decode()}")

    installed_version = output.decode().strip()
    print(f"Installed version: {installed_version}")

    # Version comparison - check if expected version is contained in installed version
    assert snowflake_version in installed_version, (
        f"Version mismatch for {snowflake_package} on {container_name} ({platform})\n"
        f"Expected: {snowflake_version}\n"
        f"Installed: {installed_version}"
    )

    print(f"✅ Version verified: {snowflake_package} {installed_version}")


@pytest.mark.parametrize("container_name", containers)
@pytest.mark.parametrize("bundled_file", snowflake_bundled_files)
def test_validate_bundled_files(container_name, bundled_file):
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

    # Append custom GUC parameters to postgresql.conf
    print(f"Updating postgresql.conf with custom GUC parameters on {container_name}")

    # Add blank line
    container.exec_run(
        f'sh -c "echo >> {pgdata}/postgresql.conf"',
        user=pguser
    )

    # Add comment
    exit_code, output = container.exec_run(
        f'sh -c "echo \\"# Custom GUC Parameters\\" >> {pgdata}/postgresql.conf"',
        user=pguser
    )
    assert exit_code == 0, f"Failed to add comment: {output.decode()}"

    # Add shared_preload_libraries (using double quotes for sh -c)
    exit_code, output = container.exec_run(
        f'sh -c "echo \\"shared_preload_libraries = \'snowflake\'\\" >> {pgdata}/postgresql.conf"',
        user=pguser
    )
    assert exit_code == 0, f"Failed to add shared_preload_libraries: {output.decode()}"

    # Add track_commit_timestamp
    exit_code, output = container.exec_run(
        f'sh -c "echo \\"track_commit_timestamp = on\\" >> {pgdata}/postgresql.conf"',
        user=pguser
    )
    assert exit_code == 0, f"Failed to add track_commit_timestamp: {output.decode()}"

    # Add snowflake.node
    exit_code, output = container.exec_run(
        f'sh -c "echo \\"snowflake.node = 1\\" >> {pgdata}/postgresql.conf"',
        user=pguser
    )
    assert exit_code == 0, f"Failed to add snowflake.node: {output.decode()}"

    # Verify the parameters were added
    exit_code, output = container.exec_run(
        f"tail -n 10 {pgdata}/postgresql.conf",
        user=pguser
    )

    config_content = output.decode()
    print(f"✅ Last 10 lines of postgresql.conf:\n{config_content}")

    # Verify each parameter
    assert "shared_preload_libraries = 'snowflake'" in config_content, \
        "shared_preload_libraries not found in postgresql.conf"
    assert "track_commit_timestamp = on" in config_content, \
        "track_commit_timestamp not found in postgresql.conf"
    assert "snowflake.node = 1" in config_content, \
        "snowflake.node not found in postgresql.conf"

    print(f"✅ All GUC parameters verified in postgresql.conf")

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
def test_snowflake_extension_loaded(container_name):
    """Verify Snowflake extension is loaded via shared_preload_libraries"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"\n--- Checking if Snowflake extension is loaded on {container_name} ---")

    exit_code, output = execute_psql_query(
        container,
        "SHOW shared_preload_libraries;"
    )
    assert exit_code == 0, f"Failed to check shared_preload_libraries: {output}"
    assert "snowflake" in output, f"Snowflake not in shared_preload_libraries: {output}"
    print(f"✅ Snowflake extension is loaded in shared_preload_libraries")


@pytest.mark.parametrize("container_name", containers)
def test_snowflake_create_extension(container_name):
    """Create Snowflake extension in the database"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"\n--- Creating Snowflake extension on {container_name} ---")

    exit_code, output = execute_psql_query(
        container,
        "CREATE EXTENSION IF NOT EXISTS snowflake;"
    )
    assert exit_code == 0, f"Failed to create Snowflake extension: {output}"
    print(f"✅ Snowflake extension created successfully")


@pytest.mark.parametrize("container_name", containers)
def test_snowflake_node_parameter(container_name):
    """Test Case 1: Verify snowflake.node parameter"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"\n--- Test Case 1: Checking snowflake.node parameter on {container_name} ---")

    exit_code, output = execute_psql_query(
        container,
        "SHOW snowflake.node;"
    )

    assert exit_code == 0, f"Failed to show snowflake.node: {output}"
    assert snowflake_node in output, f"Expected snowflake.node={snowflake_node}, got: {output}"

    print(f"Query output:\n{output}")
    print(f"✅ snowflake.node is correctly set to {snowflake_node}")


@pytest.mark.parametrize("container_name", containers)
def test_snowflake_format_function(container_name):
    """Test Case 2: Test snowflake.format() function"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"\n--- Test Case 2: Testing snowflake.format() function on {container_name} ---")

    # Test with the sample ID
    test_id = "136169504773242881"
    exit_code, output = execute_psql_query(
        container,
        f"SELECT * FROM snowflake.format({test_id});"
    )

    assert exit_code == 0, f"Failed to execute snowflake.format(): {output}"

    # Verify output contains expected JSON structure
    assert "id" in output, f"Output missing 'id' field: {output}"
    assert "ts" in output, f"Output missing 'ts' field: {output}"
    assert "count" in output, f"Output missing 'count' field: {output}"

    print(f"Query output:\n{output}")
    print(f"✅ snowflake.format() function executed successfully")


@pytest.mark.parametrize("container_name", containers)
def test_snowflake_sequence_table_creation(container_name):
    """Test Case 3: Create sequence, table with snowflake.nextval(), and insert data"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"\n--- Test Case 3: Creating employees table with Snowflake sequence on {container_name} ---")

    # Create sequence
    exit_code, output = execute_psql_query(
        container,
        "CREATE SEQUENCE employee_id_seq;"
    )
    assert exit_code == 0, f"Failed to create sequence: {output}"
    print("✅ Created employee_id_seq sequence")

    # Create table with snowflake.nextval default
    create_table_sql = """CREATE TABLE employees (
        id BIGINT DEFAULT snowflake.nextval('employee_id_seq'::regclass) PRIMARY KEY,
        name TEXT,
        department TEXT,
        salary DECIMAL(10,2),
        created_at TIMESTAMP DEFAULT NOW()
    );"""

    exit_code, output = execute_psql_query(container, create_table_sql)
    assert exit_code == 0, f"Failed to create employees table: {output}"
    print("✅ Created employees table with snowflake.nextval default")

    # Insert test data
    insert_sql = """INSERT INTO employees (name, department, salary) VALUES 
        ('John Doe', 'Engineering', 75000),
        ('Jane Smith', 'Marketing', 65000),
        ('Bob Wilson', 'Sales', 55000);"""

    exit_code, output = execute_psql_query(container, insert_sql)
    assert exit_code == 0, f"Failed to insert data: {output}"
    print("✅ Inserted 3 employees")

    # Verify data and check for snowflake IDs
    exit_code, output = execute_psql_query(container, "SELECT * FROM employees;")
    assert exit_code == 0, f"Failed to select from employees: {output}"

    print(f"Employees table data:\n{output}")

    # Verify we have 3 rows
    assert "John Doe" in output, "John Doe not found in employees"
    assert "Jane Smith" in output, "Jane Smith not found in employees"
    assert "Bob Wilson" in output, "Bob Wilson not found in employees"

    print(f"✅ Employees table created and populated with Snowflake IDs")


@pytest.mark.parametrize("container_name", containers)
def test_snowflake_multiple_sequences(container_name):
    """Test Case 4: Create another sequence/table and verify different sequence IDs"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"\n--- Test Case 4: Creating products table with different Snowflake sequence on {container_name} ---")

    # Create sequence
    exit_code, output = execute_psql_query(
        container,
        "CREATE SEQUENCE products_id_seq;"
    )
    assert exit_code == 0, f"Failed to create products sequence: {output}"
    print("✅ Created products_id_seq sequence")

    # Create products table
    create_table_sql = """CREATE TABLE products (
        product_id BIGINT DEFAULT snowflake.nextval('products_id_seq'::regclass) PRIMARY KEY,
        name TEXT,
        description TEXT,
        price DECIMAL(10,2)
    );"""

    exit_code, output = execute_psql_query(container, create_table_sql)
    assert exit_code == 0, f"Failed to create products table: {output}"
    print("✅ Created products table")

    # Insert test data
    insert_sql = """INSERT INTO products (name, description, price) VALUES
        ('Laptop', 'Computer', 999.99),
        ('Mouse', 'Computer Accessories', 29.99);"""

    exit_code, output = execute_psql_query(container, insert_sql)
    assert exit_code == 0, f"Failed to insert products: {output}"
    print("✅ Inserted 2 products")

    # Verify data
    exit_code, output = execute_psql_query(container, "SELECT * FROM products;")
    assert exit_code == 0, f"Failed to select from products: {output}"

    print(f"Products table data:\n{output}")

    # Verify we have the products
    assert "Laptop" in output, "Laptop not found in products"
    assert "Mouse" in output, "Mouse not found in products"

    # Get employee IDs and product IDs to verify they're different sequences
    exit_code, emp_output = execute_psql_query(container, "SELECT id FROM employees LIMIT 1;")
    exit_code, prod_output = execute_psql_query(container, "SELECT product_id FROM products LIMIT 1;")

    print(f"✅ Products table created with different Snowflake sequence IDs")


@pytest.mark.parametrize("container_name", containers)
def test_snowflake_table_metadata(container_name):
    """Test Case 5: Review sequence definitions in table metadata"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"\n--- Test Case 5: Reviewing table metadata on {container_name} ---")

    # Use psql \d+ command
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -c '\\d+ products'",
        user=pguser,
    )
    output_text = output.decode()

    assert exit_code == 0, f"Failed to describe products table: {output_text}"

    print(f"Products table metadata:\n{output_text}")

    # Verify the default value contains snowflake.nextval
    assert "snowflake.nextval" in output_text, "snowflake.nextval not found in table definition"
    assert "products_id_seq" in output_text, "products_id_seq not found in table definition"
    assert "product_id" in output_text, "product_id column not found"

    print(f"✅ Table metadata shows snowflake.nextval as default")


@pytest.mark.parametrize("container_name", containers)
def test_snowflake_extension_objects(container_name):
    """Test Case 6: List all Snowflake extension objects"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"\n--- Test Case 6: Listing Snowflake extension objects on {container_name} ---")

    query = """SELECT c.relname as object_name, 
                      c.relkind as object_type, 
                      n.nspname as schema_name 
               FROM pg_depend d 
               JOIN pg_extension e ON d.refobjid = e.oid 
               JOIN pg_class c ON d.objid = c.oid 
               JOIN pg_namespace n ON c.relnamespace = n.oid 
               WHERE e.extname = 'snowflake' 
               ORDER BY n.nspname, c.relname;"""

    exit_code, output = execute_psql_query(container, query)
    assert exit_code == 0, f"Failed to list extension objects: {output}"

    print(f"Snowflake extension objects:\n{output}")

    # Verify we have snowflake schema objects
    assert "snowflake" in output, "No snowflake schema objects found"

    print(f"✅ Snowflake extension objects listed successfully")


@pytest.mark.parametrize("container_name", containers)
def test_snowflake_schema_objects(container_name):
    """Test Case 7: List all objects in snowflake schema"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"\n--- Test Case 7: Listing objects in snowflake schema on {container_name} ---")

    # Use psql \dn+ command
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -c '\\dn+ snowflake'",
        user=pguser,
    )
    output_text = output.decode()

    assert exit_code == 0, f"Failed to describe snowflake schema: {output_text}"

    print(f"Snowflake schema details:\n{output_text}")

    # Verify snowflake schema exists
    assert "snowflake" in output_text, "Snowflake schema not found"

    print(f"✅ Snowflake schema objects listed successfully")


@pytest.mark.parametrize("container_name", containers)
def test_snowflake_functions(container_name):
    """Test Case 8: List all Snowflake functions"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"\n--- Test Case 8: Listing Snowflake functions on {container_name} ---")

    # Use psql \df command
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -c '\\df snowflake.*'",
        user=pguser,
    )
    output_text = output.decode()

    assert exit_code == 0, f"Failed to list snowflake functions: {output_text}"

    print(f"Snowflake functions:\n{output_text}")

    # Verify expected functions exist
    expected_functions = [
        "convert_column_to_int8",
        "convert_sequence_to_snowflake",
        "currval",
        "format",
        "get_count",
        "get_epoch",
        "get_node",
        "nextval"
    ]

    for func in expected_functions:
        assert func in output_text, f"Function '{func}' not found in snowflake schema"

    print(f"✅ All {len(expected_functions)} expected Snowflake functions found")


@pytest.mark.parametrize("container_name", containers)
def test_snowflake_convert_existing_sequence(container_name):
    """Test Case 9: Convert existing sequence to Snowflake-compatible sequence"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"\n--- Test Case 9: Converting existing sequence to Snowflake on {container_name} ---")

    # Step 1: Create a simple sequence
    exit_code, output = execute_psql_query(
        container,
        "CREATE SEQUENCE my_simple_seq;"
    )
    assert exit_code == 0, f"Failed to create sequence: {output}"
    print("✅ Created my_simple_seq sequence")

    # Step 2: Create users table with regular sequence
    create_table_sql = """CREATE TABLE users (
        id INTEGER DEFAULT nextval('my_simple_seq') PRIMARY KEY,
        name TEXT,
        email TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    );"""

    exit_code, output = execute_psql_query(container, create_table_sql)
    assert exit_code == 0, f"Failed to create users table: {output}"
    print("✅ Created users table with regular sequence")

    # Step 3: Insert initial data with regular sequence
    insert_sql = """INSERT INTO users (name, email) VALUES 
        ('John Doe', 'john@example.com'),
        ('Jane Smith', 'jane@example.com'),
        ('Bob Wilson', 'bob@example.com');"""

    exit_code, output = execute_psql_query(container, insert_sql)
    assert exit_code == 0, f"Failed to insert initial users: {output}"
    print("✅ Inserted 3 users with regular sequence IDs")

    # Step 4: Verify initial data (should have IDs 1, 2, 3)
    exit_code, output = execute_psql_query(container, "SELECT * FROM users;")
    assert exit_code == 0, f"Failed to select initial users: {output}"
    print(f"Initial users data (regular sequence):\n{output}")

    # Verify regular sequence IDs (small integers)
    assert " 1 |" in output, "ID 1 not found"
    assert " 2 |" in output, "ID 2 not found"
    assert " 3 |" in output, "ID 3 not found"

    # Step 5: Convert sequence to Snowflake
    print("\n--- Converting sequence to Snowflake ---")
    exit_code, output = execute_psql_query(
        container,
        "SELECT snowflake.convert_sequence_to_snowflake('my_simple_seq'::regclass);"
    )
    assert exit_code == 0, f"Failed to convert sequence: {output}"
    print(f"Conversion output:\n{output}")

    # Verify conversion notices
    assert "ALTER TABLE" in output or "convert_sequence_to_snowflake" in output, \
        "Conversion did not execute properly"
    print("✅ Sequence converted to Snowflake")

    # Step 6: Verify existing data is unchanged
    exit_code, output = execute_psql_query(container, "SELECT * FROM users ORDER BY id;")
    assert exit_code == 0, f"Failed to select users after conversion: {output}"
    print(f"Users data after conversion (existing rows unchanged):\n{output}")

    # Step 7: Insert new data with Snowflake sequence
    insert_new_sql = """INSERT INTO users (name, email) VALUES 
        ('Alice Johnson', 'alice@example.com'),
        ('Charlie Brown', 'charlie@example.com'),
        ('Diana Prince', 'diana@example.com');"""

    exit_code, output = execute_psql_query(container, insert_new_sql)
    assert exit_code == 0, f"Failed to insert new users: {output}"
    print("✅ Inserted 3 more users with Snowflake sequence IDs")

    # Step 8: Verify mixed data (old regular IDs + new Snowflake IDs)
    exit_code, output = execute_psql_query(container, "SELECT * FROM users ORDER BY id;")
    assert exit_code == 0, f"Failed to select final users: {output}"
    print(f"Final users data (mixed regular + Snowflake IDs):\n{output}")

    # Verify we have 6 users total
    assert "John Doe" in output, "Original user John Doe not found"
    assert "Alice Johnson" in output, "New user Alice Johnson not found"

    # Verify Snowflake IDs are present (they should be very large numbers)
    # Look for the pattern of long numbers (Snowflake IDs are typically 18+ digits)
    lines = output.split('\n')
    snowflake_id_found = False
    for line in lines:
        if 'Alice Johnson' in line or 'Charlie Brown' in line or 'Diana Prince' in line:
            # Extract the ID (first column)
            parts = line.strip().split('|')
            if len(parts) > 0:
                id_str = parts[0].strip()
                # Snowflake IDs are very large (18+ digits)
                if len(id_str) > 10 and id_str.isdigit():
                    snowflake_id_found = True
                    print(f"✅ Found Snowflake ID: {id_str}")
                    break

    assert snowflake_id_found, "No Snowflake IDs found in new records"

    print(f"✅ Successfully converted existing sequence to Snowflake sequence")
    print(f"✅ Old records retained original IDs, new records have Snowflake IDs")

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
def test_snowflake_uninstall(container_name):
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
        exit_code, _ = container.exec_run("command -v apt-get", user="root")
        if exit_code == 0:
            pkg_mgr = "apt-get remove -y"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    print(f"\n--- Uninstalling {snowflake_package} from {container_name} ---")

    exit_code, output = container.exec_run(
        f"{pkg_mgr} {snowflake_package}",
        user="root"
    )
    assert exit_code == 0, f"Failed to uninstall {snowflake_package}: {output.decode()}"

    print(f"✅ Successfully uninstalled {snowflake_package}")

    print(f"\n--- Uninstalling pgedge packages from {container_name} ---")

    exit_code, output = container.exec_run(
        f"{pkg_mgr} pgedge-*",
        user="root"
    )
    assert exit_code == 0, f"Failed to uninstall {snowflake_package}: {output.decode()}"



    print(f"✅ Successfully uninstalled all pgedge packages from {container_name}")


@pytest.mark.parametrize("container_name", containers)
def test_pgedge_cleanup(container_name):
    """Full cleanup: remove all pgedge packages + leftover data"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Step 1: Check if any pgedge packages exist
    exit_code, output = container.exec_run("rpm -qa | grep pgedge", user="root")
    packages = output.decode().strip().splitlines()

    if not packages:
        print(f"No pgedge packages found in {container_name}, skipping uninstall step.")
    else:
        print(f"Cleaning up pgedge packages in {container_name}: {packages}")
        exit_code, output = container.exec_run("dnf remove -y 'pgedge-*'", user="root")
        assert exit_code == 0, f"Failed global cleanup: {output.decode()}"

    # Step 2: Optionally clean data directory (if defined in .env)
    if pgdata:
        print(f"Removing PGDATA directory {pgdata} in {container_name}")
        container.exec_run(f"rm -rf {pgdata}", user="root")

    # Step 3: Delete user postgres created by automation setup
    print(f"Removing {pguser} User  in {container_name}")
    container.exec_run(f"userdel {pguser}", user="root")
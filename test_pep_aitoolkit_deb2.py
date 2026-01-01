import os
from pathlib import Path
from datetime import datetime

import pytest
import docker
from dotenv import load_dotenv

load_dotenv()
client = docker.from_env()

# Load values from .env
containers = os.getenv("DEB_CONTAINERS", "").split(",")
repo = os.getenv("REPO", "release")
upgrade_repo = os.getenv("UPGRADE_REPO", "release")
components = os.getenv("DEB_SERVER_COMPONENTS", "").split(",")
pguser = os.getenv("PG_USER", "postgres")
pgport = os.getenv("PG_PORT", "5432")
pgbin = os.getenv("DEB_PG_BIN_PATH", "/usr/lib/postgresql/17/bin")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
server_version = os.getenv("PG_VERSION", "17.2")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "17")
check_extensions = os.getenv("TEST_EXTENSIONS", "false").lower() == "true"

# Extensions defined in .env (core + contrib)
base_extensions = os.getenv(
    "EXTENSIONS",
    "bloom,bool_plperl,btree_gin,btree_gist,citext,cube,dblink,"
    "dict_int,earthdistance,fuzzystrmatch,hstore,intagg,intarray,isn,"
    "jsonb_plperl,lo,ltree,pg_buffercache,pg_prewarm,pg_stat_statements,"
    "pg_trgm,pgcrypto,pgrowlocks,pgstattuple,plperl,plpgsql,postgres_fdw,"
    "seg,sslinfo,tablefunc,tsm_system_rows,tsm_system_time,unaccent,uuid-ossp",
).split(",")


@pytest.mark.parametrize("container_name", containers)
def test_pgedge_install(container_name):
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
def test_upgrade_components(container_name, component):
    """Upgrade each component individually with separate test results

    This creates a separate test for each container-component combination
    """
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in .env")

    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("Invalid container or component")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Switch repo if needed
    if upgrade_repo in ["staging", "daily"]:
        container.exec_run(
            f"sed -i 's|release|{upgrade_repo}|g' /etc/apt/sources.list.d/pgedge.list",
            user="root",
        )

    # Update and upgrade
    container.exec_run("apt-get update", user="root")

    print(f"\n--- Upgrading {component} on {container_name} (Debian/Ubuntu) ---")

    # Upgrade the component
    exit_code, output = container.exec_run(
        f"apt-get upgrade -y {component}",
        user="root"
    )

    output_text = output.decode("utf-8").lower()
    print(f"Output: {output_text}")

    if exit_code == 0:
        if "0 upgraded" in output_text or "already the newest version" in output_text:
            print(f"ℹ️ {component} version is already the newest.")
            pytest.skip(f"{component} upgrade not found, version is already the newest. {container_name}")
        elif "upgraded" in output_text or "newly installed" in output_text:
            print(f"✅ Successfully upgraded {component}.")
        else:
            print(f"⚠️ Could not determine status for {component}.")

    assert exit_code == 0, f"Failed to upgrade {component}: {output.decode()}"
    print(f"✅ Successfully upgraded {component}")


@pytest.mark.parametrize("container_name", containers)
@pytest.mark.parametrize("component", components)
def test_verify_component_versions(container_name, component):
    """Verify each component version individually with separate test results

    This creates a separate test for each container-component combination
    and validates the installed version matches the expected version from .env
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

    # Extract base component name from Debian package format
    # Example: pgedge-postgresql-16-pgvector -> PGEDGE_PGVECTOR_16_VERSION
    import re

    # Remove 'pgedge-postgresql-' prefix if present
    base_component = component
    if base_component.startswith("pgedge-postgresql-"):
        base_component = base_component.replace("pgedge-postgresql-", "")
        # Now we have: "16-pgvector" or "plperl-16" etc.

        # Try to extract version and component name
        # Pattern: <version>-<component> or <component>-<version>
        match = re.match(r'^(\d+)-(.+)$', base_component)
        if match:
            version_num = match.group(1)
            comp_name = match.group(2)
            # Construct: PGEDGE_PGVECTOR_16_VERSION
            component_env_key = f"PGEDGE_{comp_name.upper().replace('-', '_')}_{version_num}_VERSION"
        else:
            # Handle cases like "plperl-16" (component-version format)
            match = re.match(r'^(.+)-(\d+)$', base_component)
            if match:
                comp_name = match.group(1)
                version_num = match.group(2)
                component_env_key = f"PGEDGE_{comp_name.upper().replace('-', '_')}_{version_num}_VERSION"
            else:
                # Fallback to original logic for non-versioned packages
                component_env_key = f"{component.upper().replace('-', '_')}_VERSION"
    else:
        # For packages like pgedge-pgbouncer, pgedge-pgbackrest
        component_env_key = f"{component.upper().replace('-', '_')}_VERSION"

    expected_version = os.getenv(component_env_key)

    if not expected_version:
        pytest.skip(f"No expected version defined for {component} (looking for {component_env_key} in .env)")

    print(f"\n--- Verifying {component} version on {container_name} ---")
    print(f"Expected version: {expected_version}")

    # Debian-based: use dpkg-query to get version

    version_cmd = f"dpkg-query --showformat='${{Version}}' --show {component}"

    # Get installed version
    exit_code, output = container.exec_run(version_cmd, user="root")

    if exit_code != 0:
        pytest.fail(f"Failed to query {component} version: {output.decode()}")

    installed_version = output.decode().strip()
    print(f"Installed version: {installed_version}")

    # Version comparison - check if expected version is contained in installed version
    assert expected_version in installed_version, (
        f"Version mismatch for {component} on {container_name} (Debian/Ubuntu)\n"
        f"Expected: {expected_version}\n"
        f"Installed: {installed_version}"
    )

    print(f"✅ Version verified: {component} {installed_version}")


@pytest.mark.parametrize("container_name", containers)
@pytest.mark.parametrize("component", components)
def test_verify_bundled_files(container_name, component):
    """Verify bundled files for each component match expected files

    This compares the installed files from dpkg with expected files
    in expected-output/deb/ directory
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

    # Extract base component name by removing prefixes
    # Example: pgedge-postgresql-17-pg-search -> pg-search
    # Example: pgedge-anonymizer -> anonymizer
    import re
    base_name = component

    # First try to remove pgedge-postgresql-XX- pattern
    if base_name.startswith("pgedge-postgresql-"):
        base_name = base_name.replace("pgedge-postgresql-", "")
        # Remove version prefix (17-, 16-, etc.)
        base_name = re.sub(r'^\d+-', '', base_name)
    # Otherwise just remove pgedge- prefix
    elif base_name.startswith("pgedge-"):
        base_name = base_name.replace("pgedge-", "", 1)

    # Path to expected files
    expected_file_path = f"./expected-output/deb/{base_name}"

    # Check if expected file exists
    if not Path(expected_file_path).exists():
        pytest.skip(f"No expected file found for {base_name} at {expected_file_path}")

    print(f"\n--- Verifying bundled files for {component} on {container_name} ---")

    # Read expected files
    with open(expected_file_path, 'r') as f:
        expected_files = [line.strip() for line in f if line.strip()]

    print(f"Expected {len(expected_files)} files")

    # Get installed files from container
    exit_code, output = container.exec_run(f"dpkg -L {component}", user="root")

    if exit_code != 0:
        pytest.fail(f"Failed to query files for {component}: {output.decode()}")

    installed_files = [line.strip() for line in output.decode().strip().splitlines() if line.strip()]
    print(f"Installed {len(installed_files)} files")

    # Normalize function to remove version-specific suffixes
    def normalize_path(path):
        """Remove version-specific parts like -17, /17/, -16, /16/, etc."""
        import re
        # Replace version numbers in various patterns:
        # - /postgresql/17/ -> /postgresql/
        # - /postgresql-17/ -> /postgresql/
        # - -17- -> -
        # - -17. -> .
        # - _17_ -> _
        # - _17. -> .
        normalized = path
        # Replace /16/, /17/, /18/ in paths
        normalized = re.sub(r'/(16|17|18)/', '/', normalized)
        # Replace /16, /17, /18 at end of path
        normalized = re.sub(r'/(16|17|18)$', '', normalized)
        # Replace -16-, -17-, -18-
        normalized = re.sub(r'-(16|17|18)-', '-', normalized)
        # Replace -16., -17., -18. (before file extension)
        normalized = re.sub(r'-(16|17|18)\.', '.', normalized)
        # Replace _16_, _17_, _18_
        normalized = re.sub(r'_(16|17|18)_', '_', normalized)
        # Replace _16., _17., _18.
        normalized = re.sub(r'_(16|17|18)\.', '.', normalized)
        # Replace -16, -17, -18 at end
        normalized = re.sub(r'-(16|17|18)$', '', normalized)
        # Replace _16, _17, _18 at end
        normalized = re.sub(r'_(16|17|18)$', '', normalized)
        return normalized

    # Normalize both lists
    expected_normalized = sorted([normalize_path(f) for f in expected_files])
    installed_normalized = sorted([normalize_path(f) for f in installed_files])

    # Find differences
    expected_set = set(expected_normalized)
    installed_set = set(installed_normalized)

    missing_files = expected_set - installed_set
    extra_files = installed_set - expected_set

    # Report results
    if missing_files:
        print(f"\n⚠️ Missing files ({len(missing_files)}):")
        for f in sorted(missing_files)[:10]:  # Show first 10
            print(f"  - {f}")
        if len(missing_files) > 10:
            print(f"  ... and {len(missing_files) - 10} more")

    if extra_files:
        print(f"\n⚠️ Extra files ({len(extra_files)}):")
        for f in sorted(extra_files)[:10]:  # Show first 10
            print(f"  + {f}")
        if len(extra_files) > 10:
            print(f"  ... and {len(extra_files) - 10} more")

    # Assert no missing files (extra files are okay)
    assert not missing_files, (
        f"Bundled files verification failed for {component} on {container_name}\n"
        f"Missing {len(missing_files)} expected files. See output above for details."
    )

    if not extra_files:
        print(f"✅ All bundled files verified: {component} ({len(expected_files)} files)")
    else:
        print(f"✅ All expected files present: {component} ({len(expected_files)} files, {len(extra_files)} extra)")


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

    print(f"Appending custom configuration to postgresql.conf in {container_name}")

    # Append the required configuration to the existing postgresql.conf
    exit_code, output = container.exec_run(
        f"bash -c \"cat >> {pgdata}/postgresql.conf << 'EOF'\n"
        f"shared_preload_libraries = 'pgedge_vectorizer,vector'\n"
        #f"shared_preload_libraries = 'pg_tokenizer'\n"
        #f"cron.database_name = 'postgres'\n"
        f"EOF\"",
        user=pguser
    )

    if exit_code != 0:
        print("❌ Failed to append configuration to postgresql.conf")
        print(output.decode(errors="replace"))
    else:
        print("✅ Configuration appended successfully to postgresql.conf")

    print(f"Starting PostgreSQL server on {container_name}")
    exit_code, output = container.exec_run(
        f"{pgbin}/pg_ctl -D {pgdata} -o '-p {pgport}' -l {pgdata}/logfile start",
        user=pguser,
    )
    assert exit_code == 0, f"pg_ctl start failed: {output.decode()}"


@pytest.mark.parametrize("container_name", containers)
def test_check_connection(container_name):
    if not check_extensions:
        pytest.skip("Extension check disabled via .env")

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
@pytest.mark.parametrize("extension", base_extensions)
def test_create_extensions(container_name, extension):
    """Create each extension individually with separate test results

    This creates a separate test for each container-extension combination
    """
    if not check_extensions:
        pytest.skip("Extension check disabled via .env")

    container_name = container_name.strip()
    extension = extension.strip()

    if not container_name or not extension:
        pytest.skip("Invalid container or extension")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Normalize extension (quote if it contains a dash)
    normalized_ext = f'"{extension}"' if "-" in extension else extension

    print(f"\n--- Creating extension {normalized_ext} in {container_name} ---")

    # Create the extension
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
        f"-c 'CREATE EXTENSION IF NOT EXISTS {normalized_ext} CASCADE;'",
        user=pguser,
    )

    assert exit_code == 0, f"Failed to create {normalized_ext}: {output.decode()}"
    print(f"✅ Successfully created extension {normalized_ext}")


@pytest.mark.parametrize("container_name", containers)
@pytest.mark.parametrize("component", components)
def test_component_functional_smoke(container_name, component):
    """Execute functional smoke tests for each component

    This runs SQL test files from sql/<component-name>.sql
    and stores output in actual-output/sql/<component-name>/<pg_major_version>/deb/<timestamp>.txt
    """
    if not check_extensions:
        pytest.skip("Extension check disabled via .env")

    container_name = container_name.strip()
    component = component.strip()

    if not container_name or not component:
        pytest.skip("Invalid container or component")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Extract base component name
    # Example: pgedge-postgresql-17-pg-search -> pg-search
    base_name = component.replace("pgedge-postgresql-", "")
    # Remove version prefix (17-, 16-, etc.)
    import re
    base_name = re.sub(r'^\d+-', '', base_name)

    # Path to SQL test file
    sql_file_path = f"./sql/{base_name}.sql"

    # Check if SQL test file exists
    if not Path(sql_file_path).exists():
        pytest.skip(f"No SQL test file found for {base_name} at {sql_file_path}")

    print(f"\n--- Running functional smoke test for {component} on {container_name} ---")
    print(f"Executing SQL file: {sql_file_path}")

    # Read SQL file content
    with open(sql_file_path, 'r') as f:
        sql_content = f.read()

    # Create temp SQL file in container
    temp_sql_path = f"/tmp/{base_name}_test.sql"

    # Write SQL content to container using heredoc
    exit_code, output = container.exec_run(
        f"bash -c \"cat > {temp_sql_path} << 'EOSQL'\n{sql_content}\nEOSQL\"",
        user=pguser
    )

    if exit_code != 0:
        pytest.fail(f"Failed to create SQL file in container: {output.decode()}")

    # Execute the SQL file
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -f {temp_sql_path}",
        user=pguser
    )

    # Clean up temp file
    container.exec_run(f"rm -f {temp_sql_path}", user=pguser)

    # Create output directory structure
    date_part = datetime.now().strftime("%d%m%y")  # ddmmyy format
    time_part = datetime.now().strftime("%H%M%S")  # hhmmss format
    filename = f"{base_name}-{date_part}-{time_part}.txt"

    output_dir = Path(f"./actual-output/sql/{base_name}/{pg_major_version}/deb")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save output to file
    output_file = output_dir / filename
    with open(output_file, 'w') as f:
        f.write(f"# Functional Smoke Test for {component}\n")
        f.write(f"# Container: {container_name}\n")
        f.write(f"# PostgreSQL Version: {pg_major_version}\n")
        f.write(f"# Date: {date_part} Time: {time_part}\n")
        f.write(f"# SQL File: {sql_file_path}\n")
        f.write("=" * 80 + "\n\n")
        f.write(output.decode())

    print(f"Output saved to: {output_file}")

    if exit_code != 0:
        print(f"⚠️ SQL execution had errors (exit code: {exit_code})")
        print(f"Output:\n{output.decode()}")

        # Capture PostgreSQL server log for debugging
        log_exit, log_output = container.exec_run(
            f"tail -100 {pgdata}/logfile",
            user=pguser
        )
        if log_exit == 0:
            print(f"\n=== PostgreSQL Server Log (last 100 lines) ===")
            print(log_output.decode())

            # Append server log to output file
            with open(output_file, 'a') as f:
                f.write("\n\n" + "=" * 80 + "\n")
                f.write("PostgreSQL Server Log (last 100 lines):\n")
                f.write("=" * 80 + "\n")
                f.write(log_output.decode())

        pytest.fail(f"SQL test failed for {component}: See {output_file} for details")

    print(f"✅ Functional smoke test passed: {component}")
    print(f"   Results: {output_file}")


@pytest.mark.parametrize("container_name", containers)
@pytest.mark.parametrize("extension", base_extensions)
def test_verify_extension_versions(container_name, extension):
    """Verify installed extension versions match expected versions from .env

    This queries PostgreSQL to get the default_version of each extension
    and compares it with the version defined in .env
    """
    if not check_extensions:
        pytest.skip("Extension check disabled via .env")

    container_name = container_name.strip()
    extension = extension.strip()

    if not container_name or not extension:
        pytest.skip("Invalid container or extension")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Map extension names to their environment variable names
    extension_env_map = {
        "vector": f"PGEDGE_PGVECTOR_{pg_major_version}_VERSION",
        "pg_cron": f"PGEDGE_PG_CRON_{pg_major_version}_VERSION",
        "pgmq": f"PGEDGE_PGMQ_{pg_major_version}_VERSION",
        "vectorize": f"PGEDGE_PG_VECTORIZE_{pg_major_version}_VERSION",
        "pg_stat_monitor": f"PGEDGE_PG_STAT_MONITOR_{pg_major_version}_VERSION",
        "pg_tokenizer": f"PGEDGE_PG_TOKENIZER_{pg_major_version}_VERSION",
        "vchord_bm25": f"PGEDGE_VCHORD_BM25_{pg_major_version}_VERSION",
        "pg_search": f"PGEDGE_PG_SEARCH_{pg_major_version}_VERSION",
    }

    # Skip if this extension doesn't have a version mapping
    if extension not in extension_env_map:
        pytest.skip(f"No version mapping defined for extension {extension}")

    env_var_name = extension_env_map[extension]
    expected_version = os.getenv(env_var_name)

    if not expected_version:
        pytest.skip(f"No expected version defined for {extension} (looking for {env_var_name} in .env)")

    print(f"\n--- Verifying extension {extension} version on {container_name} ---")
    print(f"Expected version: {expected_version}")

    # Query the extension version from PostgreSQL
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -t -A "
        f"-c \"SELECT default_version FROM pg_available_extensions WHERE name = '{extension}';\"",
        user=pguser,
    )

    if exit_code != 0:
        pytest.fail(f"Failed to query {extension} version: {output.decode()}")

    installed_version = output.decode().strip()

    if not installed_version:
        pytest.fail(f"Extension {extension} not found in pg_available_extensions")

    print(f"Installed version: {installed_version}")

    # Version comparison
    # For pg_cron and pg_stat_monitor, compare only first two digits (major.minor)
    if extension in ["pg_cron", "pg_stat_monitor"]:
        # Extract first two version segments (major.minor)
        expected_parts = expected_version.split('.')[:2]
        installed_parts = installed_version.split('.')[:2]
        expected_short = '.'.join(expected_parts)
        installed_short = '.'.join(installed_parts)

        assert expected_short == installed_short, (
            f"Version mismatch for extension {extension} on {container_name}\n"
            f"Expected (major.minor): {expected_short}\n"
            f"Installed (major.minor): {installed_short}\n"
            f"Full installed version: {installed_version}"
        )
        print(f"✅ Version verified: {extension} {installed_version} (matched on major.minor: {installed_short})")
    else:
        # For other extensions, check if expected version is contained in installed version
        assert expected_version in installed_version, (
            f"Version mismatch for extension {extension} on {container_name}\n"
            f"Expected: {expected_version}\n"
            f"Installed: {installed_version}"
        )
        print(f"✅ Version verified: {extension} {installed_version}")


@pytest.mark.parametrize("container_name", containers)
def test_stop_server(container_name):
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    print(f"Stopping PostgreSQL server on {container_name}")
    exit_code, output = container.exec_run(
        f"{pgbin}/pg_ctl -D {pgdata} -o '-p {pgport}' -l {pgdata}/logfile stop",
        user=pguser,
    )
    assert exit_code == 0, f"pg_ctl stop failed: {output.decode()}"


@pytest.mark.parametrize("container_name", containers)
def test_pgedge_uninstall(container_name):
    """Uninstall only the listed components from .env"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    for pkg in components:
        pkg = pkg.strip()
        if pkg:
            print(f"Uninstalling {pkg} in {container_name}")
            exit_code, output = container.exec_run(f"apt-get remove -y '{pkg}*'", user="root")
            assert exit_code == 0, f"Failed to uninstall {pkg}: {output.decode()}"


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
    exit_code, output = container.exec_run("dpkg -l | grep pgedge", user="root")
    packages = output.decode().strip().splitlines()

    if not packages:
        print(f"No pgedge packages found in {container_name}, skipping uninstall step.")
    else:
        print(f"Cleaning up pgedge packages in {container_name}: {packages}")
        exit_code, output = container.exec_run("apt-get remove -y 'pgedge-*'", user="root")
        assert exit_code == 0, f"Failed global cleanup: {output.decode()}"

    # Step 2: Optionally clean data directory (if defined in .env)
    if pgdata:
        print(f"Removing PGDATA directory {pgdata} in {container_name}")
        container.exec_run(f"rm -rf {pgdata}", user="root")

    # Step 3: Delete user postgres created by automation setup
    print(f"Removing {pguser} User in {container_name}")
    container.exec_run(f"userdel {pguser}", user="root")
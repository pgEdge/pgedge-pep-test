import os
import pytest
import docker
from dotenv import load_dotenv

load_dotenv()
client = docker.from_env()

# Load values from .env
containers = os.getenv("CONTAINERS", "").split(",")
repo = os.getenv("REPO", "release")
components = os.getenv("SERVER_COMPONENTS", "").split(",")
pguser = os.getenv("PG_USER", "postgres")
pgport = os.getenv("PG_PORT", "5432")
pgbin = os.getenv("PG_BIN_PATH", "/usr/pgsql-18/bin")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
server_version = os.getenv("PG_VERSION", "17.6")
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

# Extra extensions requiring package installs
pl_packages = os.getenv("PL_PACKAGES", "pgedge-postgresql18-plperl,pgedge-postgresql18-pltcl,pgedge-postgresql18-plpython3").split(",")
pl_extensions = os.getenv("PL_EXTENSIONS", "plperl,plpython3u,pltcl").split(",")




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
        exit_code, _ = container.exec_run("command -v apt-get", user="root")
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
        exit_code, output = container.exec_run(
            f"curl -sSL {deb_url} -o /tmp/pgedge-release.deb && "
            f"dpkg -i /tmp/pgedge-release.deb && "
            f"rm -f /tmp/pgedge-release.deb || true",
            user="root",
        )
        assert exit_code == 0, f"Failed to install repo: {output.decode()}"

        # Step 2: Switch repo if needed
        if repo in ["staging", "daily"]:
            container.exec_run(
                f"sed -i 's|release|{repo}|g' /etc/apt/sources.list.d/pgedge.list",
                user="root",
            )

        # Step 3: apt-get update
        exit_code, output = container.exec_run("apt-get update", user="root")
        assert exit_code == 0, f"apt-get update failed: {output.decode()}"


@pytest.mark.parametrize("container_name", containers)
def test_install_components(container_name):
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Detect package manager inside the container
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        pkg_mgr = "dnf install -y"
    else:
        exit_code, _ = container.exec_run("command -v apt-get", user="root")
        if exit_code == 0:
            # ensure apt repo is updated
            container.exec_run("apt-get update", user="root")
            pkg_mgr = "apt-get install -y"
        else:
            pytest.skip(f"No supported package manager found in {container_name}")

    # Install all requested components
    for pkg in components:
        pkg = pkg.strip()
        if pkg:
            print(f"Installing {pkg} in {container_name} using {pkg_mgr}")
            exit_code, output = container.exec_run(
                f"{pkg_mgr} {pkg}", user="root"
            )
            assert exit_code == 0, f"Failed to install {pkg}: {output.decode()}"

### Function only handling the RHEL platform.
# @pytest.mark.parametrize("container_name", containers)
# def test_install_components(container_name):
#     container = client.containers.get(container_name.strip())
#     assert container.status == "running"
#
#     for pkg in components:
#         pkg = pkg.strip()
#         if pkg:
#             print(f"Installing {pkg} in {container_name}")
#             exit_code, output = container.exec_run(
#                 f"dnf install -y {pkg}", user="root"
#             )
#             assert exit_code == 0, f"Failed to install {pkg}: {output.decode()}"

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
def test_pgedge_install(container_name):
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in .env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # Step 1: Install repo
    print(f"\n--- Installing repo in {container_name} ---")
    repo_url = "https://dnf.pgedge.com/reporpm/pgedge-release-latest.noarch.rpm"
    container.exec_run(f"dnf install -y {repo_url}", user="root")

    # Switch repo (release → staging/daily)
    if repo in ["staging", "daily"]:
        container.exec_run(
            f"sed -i 's|release|{repo}|g' /etc/yum.repos.d/pgedge.repo", user="root"
        )

@pytest.mark.parametrize("container_name", containers)
def test_install_components(container_name):
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    for pkg in components:
        pkg = pkg.strip()
        if pkg:
            print(f"Installing {pkg} in {container_name}")
            exit_code, output = container.exec_run(
                f"dnf install -y {pkg}", user="root"
            )
            assert exit_code == 0, f"Failed to install {pkg}: {output.decode()}"

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


@pytest.mark.parametrize("container_name", containers)
def test_create_pl_extensions(container_name):
    """Install PL packages and create PL extensions"""
    container = client.containers.get(container_name.strip())
    for pkg in pl_packages:
        print(f"Installing {pkg} in {container_name}")
        exit_code, output = container.exec_run(
            f"dnf install -y {pkg}", user="root"
        )
        assert exit_code == 0, f"Failed to install {pkg}: {output.decode()}"

    for ext in pl_extensions:
        print(f"Creating PL extension {ext} in {container_name}")
        exit_code, output = container.exec_run(
            f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
            f"-c 'CREATE EXTENSION IF NOT EXISTS {ext};'",
            user=pguser,
        )
        assert exit_code == 0, f"Failed to create {ext}: {output.decode()}"

@pytest.mark.parametrize("container_name", containers)
def test_create_extensions(container_name):
    """Create all base extensions"""

    if not check_extensions:
        pytest.skip("Extension check disabled via .env")

    container = client.containers.get(container_name.strip())
    # Normalize extensions (quote if they contain a dash)
    normalized_exts = [f'"{ext}"' if "-" in ext else ext for ext in base_extensions]
    for ext in normalized_exts:
        ext = ext.strip()
        if ext:
            print(f"Creating extension {ext} in {container_name}")
            exit_code, output = container.exec_run(
                f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
                f"-c 'CREATE EXTENSION IF NOT EXISTS {ext};'",
                user=pguser,
            )
            assert exit_code == 0, f"Failed to create {ext}: {output.decode()}"

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
            exit_code, output = container.exec_run(f"dnf remove -y '{pkg}*'", user="root")
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
    exit_code, output = container.exec_run("rpm -qa | grep pgedge", user="root")
    packages = output.decode().strip().splitlines()

    if not packages:
        print(f"No pgedge packages found in {container_name}, skipping uninstall step.")
    else:
        print(f"Cleaning up pgedge packages in {container_name}: {packages}")
        exit_code, output = container.exec_run("dnf remove -y 'pgedge-*'", user="root")
        assert exit_code == 0, f"Failed global cleanup: {output.decode()}"

    # Step 2: Optionally clean data directory (if defined in .env)
    pgdata = os.getenv("PGDATA", "").strip()
    if pgdata:
        print(f"Removing PGDATA directory {pgdata} in {container_name}")
        container.exec_run(f"rm -rf {pgdata}", user="root")
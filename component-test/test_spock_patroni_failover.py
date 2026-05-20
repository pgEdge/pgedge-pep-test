import os
import sys
import time
from pathlib import Path

import pytest
import docker
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from aspects import (
    configure_repository, package_management, pg_server_management,
    machine_prereq_setup, container_management
)

load_dotenv()
client = docker.from_env()

# ============================================================================
# CONFIGURATION SECTION
# ============================================================================

rhel_containers = [c.strip() for c in os.getenv("CONTAINERS", "").split(",") if c.strip()]
deb_containers = [c.strip() for c in os.getenv("DEB_CONTAINERS", "").split(",") if c.strip()]
all_containers = [(c, "rhel") for c in rhel_containers] + [(c, "deb") for c in deb_containers]

platform_filter = os.getenv("PLATFORM_FILTER", "").lower()
if platform_filter == "rpm":
    all_containers = [(c, t) for c, t in all_containers if t == "rhel"]
elif platform_filter == "deb":
    all_containers = [(c, t) for c, t in all_containers if t == "deb"]

repo = os.getenv("REPO", "release")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")

# Spock-Patroni Failover topology
# n1: standalone Spock node (port n1_port)
# n2: Spock node managed by Patroni as primary (port n2_port)
# n3: Patroni physical standby of n2 (port n3_port)
no_of_nodes       = int(os.getenv("PATRONI_FAILOVER_NO_OF_NODES", "2"))
base_port         = int(os.getenv("PATRONI_FAILOVER_BASE_PORT", "5432"))
standby_port      = int(os.getenv("PATRONI_STANDBY_PORT", "6432"))
patroni_scope     = os.getenv("PATRONI_SCOPE", "zone-n2")
n2_restapi_port   = int(os.getenv("PATRONI_N2_RESTAPI_PORT", "8008"))
n3_restapi_port   = int(os.getenv("PATRONI_N3_RESTAPI_PORT", "8009"))
etcd_host         = os.getenv("PATRONI_ETCD_HOST", "localhost:2379")

# Zodan cross-wiring script
zodan_sql = os.getenv("LATEST_ZODAN_SQL", "zodan-508.sql")
zodan_sql_script = (Path(__file__).parent.parent / "config" / "spock" / zodan_sql).resolve()

# User / auth
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser  = os.getenv("DEB_PG_USER", "postgres")
pg_password = os.getenv("PG_PASSWORD", "postgres")

# Binary paths
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin").rstrip('/')
deb_pgbin  = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin").rstrip('/')

# Derived port constants
n1_port = base_port          # 5432
n2_port = base_port + 1      # 5433
n3_port = standby_port       # 6432


def get_container_config(container_type):
    if container_type == "rhel":
        return {
            "pgbin":  rhel_pgbin,
            "pguser": rhel_pguser,
            "bin_dir": f"/usr/pgsql-{pg_major_version}/bin",
            # Install spock first — it pulls pgedge-postgresql*-server as a dependency.
            # contrib is required separately for dblink (used by zodan cross-wiring).
            "server_packages": [
                f"pgedge-spock50_{pg_major_version}",
                f"pgedge-postgresql{pg_major_version}-contrib",
            ],
            "patroni_packages": [
                p.strip()
                for p in os.getenv(
                    "PATRONI_PACKAGE",
                    "pgedge-patroni-consul,pgedge-patroni-etcd,pgedge-etcd,pgedge-patroni-aws,pgedge-patroni-zookeeper"
                ).split(",")
                if p.strip()
            ],
        }
    else:
        return {
            "pgbin":  deb_pgbin,
            "pguser": deb_pguser,
            "bin_dir": f"/usr/lib/postgresql/{pg_major_version}/bin",
            # Install spock first — it pulls pgedge-postgresql-* as a dependency.
            # contrib is required separately for dblink (used by zodan cross-wiring).
            "server_packages": [
                f"pgedge-postgresql-{pg_major_version}-spock50",
                f"pgedge-postgresql-{pg_major_version}",
            ],
            "patroni_packages": [
                p.strip()
                for p in os.getenv("DEB_PATRONI_PACKAGE", "pgedge-patroni,pgedge-etcd").split(",")
                if p.strip()
            ],
        }


def _get_container(container_name):
    try:
        return client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")


def _exec(container, cmd, user="root", check=True, msg=""):
    exit_code, output = container.exec_run(["bash", "-c", cmd], user=user)
    out = output.decode()
    if check:
        assert exit_code == 0, f"{msg or cmd}\n{out}"
    return exit_code, out


# ============================================================================
# STEP 1 – Prerequisites
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_install_prerequisites(container_name, container_type):
    """Step 1: Install prerequisites"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container, created, message = container_management.ensure_container_running(
        client, container_name, container_type
    )
    print(f"{'Created: ' if created else ''}{message}")
    assert container.status == "running"

    success, os_info, message = machine_prereq_setup.install_prerequisites_on_container(container)
    assert success, f"Prerequisites installation failed: {message}"
    print(f"Prerequisites installed on {container_name} ({os_info}): {message}")


# ============================================================================
# STEP 2 – Repository
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_configure_repository(container_name, container_type):
    """Step 2: Configure pgEdge repository"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    success, platform, message = configure_repository.configure_pgedge_repository(container, repo)
    assert success, f"Repository configuration failed: {message}"
    print(f"Repository configured ({platform}): {message}")


# ============================================================================
# STEP 3 – Install packages
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_install_server_packages(container_name, container_type):
    """Step 3a: Install Spock package (auto-installs PostgreSQL server) and contrib"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    # On DEB systems, prevent apt from auto-initializing a PostgreSQL cluster
    # during package installation — we initialize clusters manually in later steps.
    if container_type == "deb":
        _exec(container, "mkdir -p /etc/postgresql-common", msg="mkdir /etc/postgresql-common")
        _exec(container,
              "echo 'create_main_cluster = false' > /etc/postgresql-common/createcluster.conf",
              msg="Disable auto cluster creation")
        print("DEB: auto cluster creation disabled")

    config = get_container_config(container_type)
    failed = []
    for pkg in config["server_packages"]:
        success, platform, message = package_management.install_package(
            container=container,
            package_name=pkg,
            pg_major_version=pg_major_version,
            install_pg_server=False,
        )
        if success:
            print(f"Installed {pkg} on {platform}")
        else:
            failed.append(f"{pkg}: {message}")

    assert not failed, "Server package installation failures:\n" + "\n".join(failed)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_install_patroni_packages(container_name, container_type):
    """Step 3b: Install Patroni and etcd packages"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config = get_container_config(container_type)
    packages = config["patroni_packages"]

    # Log which env var is driving the package list so misconfigurations are obvious
    if container_type == "rhel":
        env_var = "PATRONI_PACKAGE"
        env_val = os.getenv("PATRONI_PACKAGE", "<default>")
    else:
        env_var = "DEB_PATRONI_PACKAGE"
        env_val = os.getenv("DEB_PATRONI_PACKAGE", "<default>")
    print(f"\n{env_var}={env_val}")
    print(f"Packages to install: {packages}")

    failed = []
    for pkg in packages:
        success, platform, message = package_management.install_package(container, pkg)
        if success:
            print(f"Installed {pkg} on {platform}")
        else:
            failed.append(f"{pkg}: {message}")

    assert not failed, "Patroni package installation failures:\n" + "\n".join(failed)


# ============================================================================
# STEP 4 – Initialize n1 and n2 clusters
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_initialize_clusters(container_name, container_type):
    """Step 4: Initialize and start both n1 (port 5432) and n2 (port 5433)"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config  = get_container_config(container_type)
    pgbin   = config["pgbin"]
    pguser  = config["pguser"]

    guc_parameters = {
        "shared_preload_libraries": "'spock'",
        "wal_level":                "logical",
        "max_worker_processes":     "16",
        "max_replication_slots":    "10",
        "max_wal_senders":          "10",
        "track_commit_timestamp":   "on",
        "hot_standby_feedback":     "on",
    }

    for node_name, pgdata, port in [("n1", "/tmp/n1", n1_port), ("n2", "/tmp/n2", n2_port)]:
        print(f"\nInitializing {node_name} at {pgdata} (port {port})")
        success, _, message = pg_server_management.init_cluster(
            container, pgbin, pgdata, pguser, guc_parameters
        )
        assert success, f"Failed to initialize {node_name}: {message}"
        print(message)

        success, _, message = pg_server_management.start_server(
            container, pgbin, pgdata, str(port), pguser
        )
        assert success, f"Failed to start {node_name}: {message}"
        print(f"{node_name} started on port {port}")


# ============================================================================
# STEP 5 – Setup Spock on n1 and n2, then cross-wire
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_create_spock_extension_on_spock_nodes(container_name, container_type):
    """Step 5a: Create Spock extension on n1 and n2"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    for node_name, port in [("n1", n1_port), ("n2", n2_port)]:
        print(f"\nCreating Spock extension on {node_name} (port {port})")
        _exec(container,
              f'psql -h localhost -p {port} -U {pguser} -d postgres '
              f'-c "CREATE EXTENSION IF NOT EXISTS spock;" 2>&1',
              msg=f"CREATE EXTENSION spock on {node_name}")
        print(f"Spock extension created on {node_name}")


# ============================================================================
# STEP 6 – Configure etcd
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_configure_etcd(container_name, container_type):
    """Step 6: Write etcd configuration file"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    etcd_conf = """\
name: "default"
data-dir: "/var/lib/etcd/default.etcd"
listen-peer-urls: "http://localhost:2380"
listen-client-urls: "http://localhost:2379"
initial-advertise-peer-urls: "http://localhost:2380"
advertise-client-urls: "http://localhost:2379"
initial-cluster: "default=http://localhost:2380"
initial-cluster-state: "new"
"""

    _exec(container, "mkdir -p /etc/etcd", msg="mkdir /etc/etcd")
    _exec(container, "mkdir -p /var/lib/etcd && chmod 700 /var/lib/etcd", msg="mkdir /var/lib/etcd")

    # RHEL uses etcd.yml; DEB uses etcd.conf — same content, different filename
    etcd_conf_path = "/etc/etcd/etcd.conf" if container_type == "deb" else "/etc/etcd/etcd.yml"
    write_cmd = f"cat > {etcd_conf_path} << 'ETCDEOF'\n{etcd_conf}\nETCDEOF"
    _exec(container, write_cmd, msg=f"Write {etcd_conf_path}")
    print(f"etcd configuration written to {etcd_conf_path}")


# ============================================================================
# STEP 7 – Start etcd
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_start_etcd(container_name, container_type):
    """Step 7: Start etcd service"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    container.exec_run(["bash", "-c", "pkill -x etcd 2>/dev/null || true"], user="root")
    time.sleep(1)

    rc, out = container.exec_run(["bash", "-c", "systemctl is-active etcd 2>/dev/null"], user="root")
    if rc == 0 and "active" in out.decode():
        print("etcd already active via systemd")
        return

    rc, _ = container.exec_run(["bash", "-c", "systemctl enable etcd && systemctl start etcd 2>&1"], user="root")
    if rc == 0:
        time.sleep(3)
        rc, out = container.exec_run(["bash", "-c", "systemctl is-active etcd 2>/dev/null"], user="root")
        if rc == 0 and "active" in out.decode():
            print("etcd started via systemctl")
        else:
            container.exec_run(["bash", "-c", "systemctl stop etcd 2>/dev/null || true"], user="root")
            rc = 1

    if rc != 0:
        etcd_bin = "/usr/bin/etcd"
        rc_which, _ = container.exec_run(["bash", "-c", f"command -v {etcd_bin}"], user="root")
        if rc_which != 0:
            rc_which, out = container.exec_run(["bash", "-c", "command -v etcd"], user="root")
            assert rc_which == 0, f"etcd binary not found on PATH: {out.decode()}"
            etcd_bin = out.decode().strip()

        start_cmd = (
            f"nohup {etcd_bin} "
            "--name default "
            "--data-dir /var/lib/etcd/default.etcd "
            "--listen-peer-urls http://localhost:2380 "
            "--listen-client-urls http://localhost:2379 "
            "--advertise-client-urls http://localhost:2379 "
            "--initial-cluster 'default=http://localhost:2380' "
            "--initial-cluster-state new "
            "> /tmp/etcd.log 2>&1 &"
        )
        container.exec_run(["bash", "-c", start_cmd], user="root")
        print("etcd started directly (no systemd)")

    time.sleep(5)
    for attempt in range(6):
        rc, out = container.exec_run(
            ["bash", "-c", "etcdctl endpoint health --endpoints=http://localhost:2379 2>&1"],
            user="root"
        )
        if rc == 0:
            print(f"etcd healthy: {out.decode().strip()}")
            return
        time.sleep(3)

    _, log = container.exec_run(["bash", "-c", "tail -20 /tmp/etcd.log 2>/dev/null || true"], user="root")
    pytest.fail(f"etcd did not become healthy.\nLog:\n{log.decode()}")


# ============================================================================
# STEP 8 – Create Patroni configuration directories and pgpass files
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_create_patroni_directories(container_name, container_type):
    """Step 8: Create /etc/patroni directory and pgpass files"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    _exec(container, "mkdir -p /etc/patroni", msg="mkdir /etc/patroni")
    _exec(container, f"chown {pguser}:{pguser} /etc/patroni", msg="chown /etc/patroni")
    _exec(container, "chmod 750 /etc/patroni", msg="chmod /etc/patroni")

    # n3 data dir (/tmp/n2 already exists from test_initialize_clusters)
    _exec(container, f"mkdir -p /tmp/n3", msg="mkdir /tmp/n3")
    _exec(container, f"chown {pguser}:{pguser} /tmp/n3", msg="chown /tmp/n3")
    _exec(container, f"chmod 700 /tmp/n3", msg="chmod /tmp/n3")


    pgpass_content = f"*:*:*:{pguser}:{pg_password}"
    for node in ["n2", "n3"]:
        pgpass_path = f"/tmp/pgpass_{node}"
        _exec(container, f"echo '{pgpass_content}' > {pgpass_path}", msg=f"Write {pgpass_path}")
        _exec(container, f"chmod 600 {pgpass_path}", msg=f"chmod {pgpass_path}")
        _exec(container, f"chown {pguser}:{pguser} {pgpass_path}", msg=f"chown {pgpass_path}")

    print("Patroni directories and pgpass files created")


# ============================================================================
# STEP 9 – Create Patroni YAML configurations
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_create_patroni_yml_n2(container_name, container_type):
    """Step 9a: Write Patroni configuration for n2 (primary/leader)"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config   = get_container_config(container_type)
    pguser   = config["pguser"]
    bin_dir  = config["bin_dir"]

    n2_yml = f"""\
scope: {patroni_scope}
name: n2
namespace: /service/

restapi:
  listen: 0.0.0.0:{n2_restapi_port}
  connect_address: localhost:{n2_restapi_port}

etcd3:
  hosts:
    - {etcd_host}

bootstrap:
  dcs:
    ttl: 30
    loop_wait: 10
    retry_timeout: 10
    maximum_lag_on_failover: 1048576
    postgresql:
      use_pg_rewind: true
      use_slots: true
      parameters:
        wal_level: logical
        hot_standby: "on"
        hot_standby_feedback: "on"
        max_wal_senders: 10
        max_replication_slots: 10
        max_worker_processes: 16
        track_commit_timestamp: "on"
        shared_preload_libraries: 'spock'
    slots:
      n3:
        type: physical

postgresql:
  listen: 0.0.0.0:{n2_port}
  connect_address: localhost:{n2_port}
  data_dir: /tmp/n2
  bin_dir: {bin_dir}
  pgpass: /tmp/pgpass_n2
  authentication:
    replication:
      username: {pguser}
      password: {pg_password}
    superuser:
      username: {pguser}
      password: {pg_password}
    rewind:
      username: {pguser}
      password: {pg_password}
  parameters:
    unix_socket_directories: '/var/run/postgresql,/tmp'
    port: {n2_port}

tags:
  nofailover: false
  noloadbalance: false
  clonefrom: true
"""

    write_cmd = f"cat > /etc/patroni/n2.yml << 'PATRONIEOF'\n{n2_yml}\nPATRONIEOF"
    _exec(container, write_cmd, msg="Write /etc/patroni/n2.yml")
    print(f"Patroni n2.yml written (port {n2_port}, restapi {n2_restapi_port})")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_create_patroni_yml_n3(container_name, container_type):
    """Step 9b: Write Patroni configuration for n3 (standby/replica)"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config   = get_container_config(container_type)
    pguser   = config["pguser"]
    bin_dir  = config["bin_dir"]

    n3_yml = f"""\
scope: {patroni_scope}
name: n3
namespace: /service/

restapi:
  listen: 0.0.0.0:{n3_restapi_port}
  connect_address: localhost:{n3_restapi_port}

etcd3:
  hosts:
    - {etcd_host}

postgresql:
  listen: 0.0.0.0:{n3_port}
  connect_address: localhost:{n3_port}
  data_dir: /tmp/n3
  bin_dir: {bin_dir}
  pgpass: /tmp/pgpass_n3
  authentication:
    replication:
      username: {pguser}
      password: {pg_password}
    superuser:
      username: {pguser}
      password: {pg_password}
    rewind:
      username: {pguser}
      password: {pg_password}
  parameters:
    unix_socket_directories: '/var/run/postgresql,/tmp'
    port: {n3_port}

tags:
  nofailover: false
  noloadbalance: false
  clonefrom: false
"""

    write_cmd = f"cat > /etc/patroni/n3.yml << 'PATRONIEOF'\n{n3_yml}\nPATRONIEOF"
    _exec(container, write_cmd, msg="Write /etc/patroni/n3.yml")
    print(f"Patroni n3.yml written (port {n3_port}, restapi {n3_restapi_port})")


# ============================================================================
# STEP 10 – Fix Patroni file permissions
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_patroni_file_permissions(container_name, container_type):
    """Step 10: Set correct ownership on Patroni YAML files"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    for yml in ["n2.yml", "n3.yml"]:
        path = f"/etc/patroni/{yml}"
        _exec(container, f"chown {pguser}:{pguser} {path}", msg=f"chown {path}")
        _exec(container, f"chmod 640 {path}", msg=f"chmod {path}")
        print(f"Permissions set on {path}")


# ============================================================================
# STEP 11 – Hand n2 over to Patroni (existing cluster, no re-init)
# ============================================================================
@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_start_patroni_n2(container_name, container_type):
    """Step 11: Start Patroni for n2 from /tmp and wait for it to become leader"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    # cd /tmp inside the sudo context so Patroni's cwd is /tmp (accessible to postgres).
    # Without this, multiprocessing workers inherit whatever cwd the Docker exec had.
    start_cmd = (
        "nohup sudo -u postgres bash -c "
        "'cd /tmp && exec /usr/bin/patroni /etc/patroni/n2.yml' "
        "> /tmp/patroni_n2.log 2>&1 </dev/null &"
    )
    container.exec_run(["bash", "-c", start_cmd], user="root")
    print("Patroni n2 started (sudo -u postgres /usr/bin/patroni /etc/patroni/n2.yml)")

    print("Waiting for n2 to become Patroni leader (up to 90s)...")
    for attempt in range(30):
        time.sleep(3)
        rc, out = container.exec_run(
            ["bash", "-c",
             "timeout 10 patronictl -c /etc/patroni/n2.yml list 2>/dev/null || true"],
            user="root"
        )
        output = out.decode()
        if "Leader" in output and "n2" in output:
            print(f"n2 is now Patroni leader:\n{output}")
            return
        if attempt % 5 == 4:
            _, log = container.exec_run(
                ["bash", "-c", "tail -5 /tmp/patroni_n2.log 2>/dev/null || true"],
                user="root"
            )
            print(f"  Still waiting... ({(attempt + 1) * 3}s)\n{log.decode().strip()}")

    _, log = container.exec_run(["bash", "-c", "tail -30 /tmp/patroni_n2.log 2>/dev/null || true"], user="root")
    pytest.fail(f"n2 did not become Patroni leader within 90s.\nLog:\n{log.decode()}")


# ============================================================================
# STEP 12 – Start Patroni for n3 (clones from n2 as replica)
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_start_patroni_n3(container_name, container_type):
    """Step 12: Start Patroni for n3 from /tmp and wait for it to join as replica"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    # cd /tmp inside the sudo context so Patroni's cwd is /tmp (accessible to postgres).
    # Without this, multiprocessing workers inherit whatever cwd the Docker exec had.
    start_cmd = (
        "nohup sudo -u postgres bash -c "
        "'cd /tmp && exec /usr/bin/patroni /etc/patroni/n3.yml' "
        "> /tmp/patroni_n3.log 2>&1 </dev/null &"
    )
    container.exec_run(["bash", "-c", start_cmd], user="root")
    print("Patroni n3 started (sudo -u postgres /usr/bin/patroni /etc/patroni/n3.yml)")

    # n3 clones via pg_basebackup which can take longer than n2's bootstrap.
    # timeout 10 prevents patronictl from hanging when the REST API is slow.
    print("Waiting for n3 to join as Patroni replica (up to 3 minutes)...")
    for attempt in range(60):
        time.sleep(3)
        rc, out = container.exec_run(
            ["bash", "-c",
             "timeout 10 patronictl -c /etc/patroni/n3.yml list 2>/dev/null || true"],
            user="root"
        )
        output = out.decode()
        if "Replica" in output and "n3" in output:
            print(f"n3 is now Patroni replica:\n{output}")
            print("Waiting 40s for n2/n3 streaming replication to stabilise...")
            time.sleep(40)
            return
        if attempt % 10 == 9:
            _, log = container.exec_run(
                ["bash", "-c", "tail -5 /tmp/patroni_n3.log 2>/dev/null || true"],
                user="root"
            )
            print(f"  Still waiting... ({(attempt + 1) * 3}s)\n{log.decode().strip()}")

    _, log = container.exec_run(["bash", "-c", "tail -30 /tmp/patroni_n3.log 2>/dev/null || true"], user="root")
    pytest.fail(f"n3 did not join as Patroni replica within 180s.\nLog:\n{log.decode()}")


# ============================================================================
# STEP 13 – Validate Patroni cluster (n2=Leader, n3=Replica)
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def validate_patroni_cluster(container_name, container_type):
    """Step 13: Verify patronictl list shows n2 as Leader and n3 as Replica"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    rc, out = container.exec_run(
        ["bash", "-c", "patronictl -c /etc/patroni/n3.yml list 2>&1"],
        user="root"
    )
    output = out.decode()
    print(f"\nPatronictl list output:\n{output}")

    assert rc == 0, f"patronictl list failed: {output}"
    assert "n2" in output and "Leader" in output, \
        f"Expected n2 as Leader in patronictl output:\n{output}"
    assert "n3" in output and "Replica" in output, \
        f"Expected n3 as Replica in patronictl output:\n{output}"
    print("Patroni cluster validated: n2=Leader, n3=Replica")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_cross_wire_n1_n2(container_name, container_type):
    """Step 5c: Cross-wire n1 and n2 via zodan SQL"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    if not zodan_sql_script.exists():
        pytest.skip(f"Zodan SQL script not found: {zodan_sql_script}")

    config = get_container_config(container_type)
    pguser = config["pguser"]
    pgbin  = config["pgbin"]

    with zodan_sql_script.open('r') as f:
        zodan_content = f.read()

    container_zodan_path = f"/tmp/{zodan_sql}"
    exit_code, output = container.exec_run(
        ["bash", "-c", f"cat > {container_zodan_path} << 'SQLEOF'\n{zodan_content}\nSQLEOF"],
        user="root"
    )
    assert exit_code == 0, f"Failed to copy zodan SQL to container: {output.decode()}"

    # Verify the file was written correctly
    exit_code, output = container.exec_run(["wc", "-l", container_zodan_path], user="root")
    if exit_code == 0:
        print(f"Zodan SQL copied ({output.decode().strip().split()[0]} lines)")

    src_node = "n1"
    src_dsn  = f"host=127.0.0.1 dbname=postgres port={n1_port} user={pguser} password={pg_password}"
    new_node = "n2"
    new_dsn  = f"host=127.0.0.1 dbname=postgres port={n2_port} user={pguser} password={pg_password}"

    print(f"\nCross-wiring: {src_node} <-> {new_node}")

    # dblink is required by zodan; contrib package provides it
    _exec(container,
          f'{pgbin}/psql -h 127.0.0.1 -p {n2_port} -U {pguser} -d postgres '
          f'-c "CREATE EXTENSION IF NOT EXISTS dblink;" 2>&1',
          msg="CREATE EXTENSION dblink on n2")

    _exec(container,
          f"{pgbin}/psql -h 127.0.0.1 -p {n2_port} -U {pguser} -d postgres "
          f"-f {container_zodan_path} 2>&1",
          msg="Load zodan procedures on n2")

    add_node_sql = (
        f"CALL spock.add_node("
        f"'{src_node}', '{src_dsn}', '{new_node}', '{new_dsn}', true);"
    )
    rc, out = container.exec_run(
        ["bash", "-c",
         f'{pgbin}/psql -h 127.0.0.1 -p {n2_port} -U {pguser} -d postgres '
         f'-c "{add_node_sql}" 2>&1'],
        user="root"
    )
    output_str = out.decode()
    print(output_str)
    assert rc == 0, f"spock.add_node failed: {output_str}"
    assert "Success rate: %100" in output_str, \
        f"Expected 'Success rate: %100' in output:\n{output_str}"

    time.sleep(5)
    print(f"Cross-wiring complete: {src_node} <-> {new_node}")

    # Enable auto-DDL replication on both nodes.
    # ALTER SYSTEM must be issued in separate psql -c calls — it cannot run
    # inside a multi-statement batch sent via a single -c invocation.
    ddl_statements = [
        "ALTER SYSTEM SET spock.enable_ddl_replication=on",
        "ALTER SYSTEM SET spock.include_ddl_repset=on",
        "ALTER SYSTEM SET spock.allow_ddl_from_functions=on",
        "SELECT pg_reload_conf()",
    ]
    for node_name, port in [("n1", n1_port), ("n2", n2_port)]:
        for sql in ddl_statements:
            _exec(container,
                  f'{pgbin}/psql -h 127.0.0.1 -p {port} -U {pguser} -d postgres '
                  f'-c "{sql}" 2>&1',
                  msg=f"{sql} on {node_name}")
        print(f"Auto-DDL replication enabled on {node_name}")


# ============================================================================
# STEP 14 – Create table on n1, verify replication to n2
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_create_table_n1_verify_n2(container_name, container_type):
    """Step 14: Create 'test' table on n1, insert rows, verify replication to n2"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    setup_sql = """\
CREATE TABLE IF NOT EXISTS test (
    id   SERIAL PRIMARY KEY,
    data TEXT,
    ts   TIMESTAMP DEFAULT NOW()
);
SELECT spock.repset_add_table('default', 'test');
INSERT INTO test (data) VALUES ('from_n1_row1'), ('from_n1_row2'), ('from_n1_row3');
SELECT 'n1 row count: ' || COUNT(*) FROM test;
"""

    sql_file = "/tmp/create_test_table.sql"
    _exec(container,
          f"cat > {sql_file} << 'EOF'\n{setup_sql}\nEOF",
          msg="Write create_test_table.sql")

    print(f"\nCreating and populating 'test' table on n1 (port {n1_port})")
    rc, out = container.exec_run(
        ["bash", "-c", f"psql -h localhost -p {n1_port} -U {pguser} -d postgres -f {sql_file} 2>&1"],
        user="root"
    )
    assert rc == 0, f"Failed to setup test table on n1: {out.decode()}"
    print(out.decode())

    print("Waiting 10s for replication to n2...")
    time.sleep(10)

    rc, out = container.exec_run(
        ["bash", "-c",
         f"psql -h localhost -p {n2_port} -U {pguser} -d postgres "
         f"-t -c 'SELECT COUNT(*) FROM test;' 2>&1"],
        user="root"
    )
    output = out.decode()
    print(f"Count on n2: {output}")
    assert rc == 0, f"Query failed on n2: {output}"
    assert int(output.strip()) == 3, f"Expected 3 rows on n2 after replication, got:\n{output}"
    print("'test' table replicated from n1 to n2 successfully")


# ============================================================================
# STEP 15 – Insert from n2, verify on n1 and n3
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_insert_from_n2_verify_n1_and_n3(container_name, container_type):
    """Step 15: Insert into 'test' from n2; verify replication to n1 (Spock) and n3 (physical)"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\nInserting rows into 'test' from n2 (port {n2_port})")
    # Advance the sequence past rows replicated from n1 to avoid primary-key conflicts.
    # Spock replicates the actual id values (1,2,3) but does not advance n2's sequence.
    _exec(container,
          f"psql -h localhost -p {n2_port} -U {pguser} -d postgres "
          f"-c \"SELECT setval('test_id_seq', (SELECT MAX(id) FROM test));\" 2>&1",
          msg="Advance test_id_seq on n2 past replicated rows")
    _exec(container,
          f"psql -h localhost -p {n2_port} -U {pguser} -d postgres "
          f"-c \"INSERT INTO test (data) VALUES ('from_n2_row1'), ('from_n2_row2');\" 2>&1",
          msg="Insert into test on n2")

    print("Waiting 10s for replication to n1 and n3...")
    time.sleep(10)

    # n1: Spock logical replication — total = 3 from n1 + 2 from n2 = 5
    rc, out = container.exec_run(
        ["bash", "-c",
         f"psql -h localhost -p {n1_port} -U {pguser} -d postgres "
         f"-t -c 'SELECT COUNT(*) FROM test;' 2>&1"],
        user="root"
    )
    n1_output = out.decode()
    print(f"Count on n1: {n1_output}")
    assert rc == 0, f"Query on n1 failed: {n1_output}"
    assert int(n1_output.strip()) == 5, f"Expected 5 rows on n1, got:\n{n1_output}"

    # n3: physical streaming replica of n2 — same rows as n2
    rc, out = container.exec_run(
        ["bash", "-c",
         f"psql -h localhost -p {n3_port} -U {pguser} -d postgres "
         f"-t -c 'SELECT COUNT(*) FROM test;' 2>&1"],
        user="root"
    )
    n3_output = out.decode()
    print(f"Count on n3: {n3_output}")
    assert rc == 0, f"Query on n3 failed: {n3_output}"
    assert int(n3_output.strip()) == 5, f"Expected 5 rows on n3, got:\n{n3_output}"

    print("Insert from n2 replicated to n1 (Spock) and n3 (physical streaming) successfully")


# ============================================================================
# STEP 16 – n3 refuses writes (read-only standby)
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_readonly_n3_refuses_insert(container_name, container_type):
    """Step 16: Confirm n3 is read-only and rejects INSERT"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\nAttempting INSERT on n3 (port {n3_port}) — should be refused")
    rc, out = container.exec_run(
        ["bash", "-c",
         f"psql -h localhost -p {n3_port} -U {pguser} -d postgres "
         f"-c \"INSERT INTO test (data) VALUES ('should_fail');\" 2>&1"],
        user="root"
    )
    output = out.decode()
    print(f"n3 response: {output}")

    assert rc != 0, \
        f"n3 should have refused the INSERT (read-only standby), but got rc={rc}:\n{output}"
    print("n3 correctly refused the INSERT (read-only standby confirmed)")


# ============================================================================
# STEP 17 – Promote n3 to leader via Patroni switchover
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_promote_n3_to_leader(container_name, container_type):
    """Step 17: Promote n3 as new Patroni leader via switchover from n2"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    print(f"\nInitiating Patroni switchover: n2 -> n3")

    # --primary for Patroni >= 3.0 (replaces deprecated --master)
    switchover_cmd = (
        f"patronictl -c /etc/patroni/n2.yml switchover {patroni_scope} "
        f"--primary n2 --candidate n3 --force 2>&1"
    )
    rc, out = container.exec_run(["bash", "-c", switchover_cmd], user="root")
    output = out.decode()
    print(f"Switchover output:\n{output}")

    if rc != 0 and "switchover" not in output.lower() and "failover" not in output.lower():
        failover_cmd = (
            f"patronictl -c /etc/patroni/n2.yml failover {patroni_scope} "
            f"--primary n2 --candidate n3 --force 2>&1"
        )
        rc, out = container.exec_run(["bash", "-c", failover_cmd], user="root")
        output = out.decode()
        print(f"Failover output:\n{output}")

    print("Waiting 20s for switchover to complete...")
    time.sleep(20)

    rc, out = container.exec_run(
        ["bash", "-c", "patronictl -c /etc/patroni/n3.yml list 2>&1"],
        user="root"
    )
    list_output = out.decode()
    print(f"Cluster after switchover:\n{list_output}")

    assert rc == 0, f"patronictl list failed: {list_output}"
    assert "n3" in list_output and "Leader" in list_output, \
        f"n3 should be Leader after switchover:\n{list_output}"

    print("n3 is now the Patroni leader — failover successful")


# ============================================================================
# STEP 17b – Redirect n1's Spock subscription to n3 (new Patroni leader)
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_update_spock_subscription_after_failover(container_name, container_type):
    """Step 17b: Redirect n1's sub_n2_n1 subscription to n3's port, then verify replication."""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    sub_name   = "sub_n2_n1"
    iface_name = f"n2_at_{n3_port}"
    new_dsn    = f"host=localhost port={n3_port} dbname=postgres user={pguser} password={pg_password}"

    def psql_n1(sql, msg):
        _exec(container,
              f'psql -h localhost -p {n1_port} -U {pguser} -d postgres -c "{sql}" 2>&1',
              msg=msg)

    # 1. Disable subscription while we update the interface
    psql_n1(f"SELECT spock.sub_disable('{sub_name}')", f"Disable {sub_name}")

    # 2. Add a new interface pointing to n3's port
    psql_n1(
        f"SELECT spock.node_add_interface('n2', '{iface_name}', '{new_dsn}')",
        f"Add interface {iface_name} for n2 node"
    )

    # 3. Switch the subscription to the new interface
    psql_n1(
        f"SELECT spock.sub_alter_interface('{sub_name}', '{iface_name}')",
        f"Switch {sub_name} to {iface_name}"
    )

    # 4. Re-enable
    psql_n1(f"SELECT spock.sub_enable('{sub_name}')", f"Re-enable {sub_name}")

    print("Waiting 5s for subscription to sync...")
    time.sleep(5)

    # 5. Verify
    rc, out = container.exec_run(
        ["bash", "-c",
         f"psql -h localhost -p {n1_port} -U {pguser} -d postgres "
         f"-c \"SELECT * FROM spock.sub_show_status();\" 2>&1"],
        user="root"
    )
    output = out.decode()
    print(f"\nspock.sub_show_status() on n1:\n{output}")
    assert rc == 0, f"sub_show_status() failed on n1: {output}"
    assert "replicating" in output.lower(), \
        f"Expected 'replicating' in sub_show_status() on n1:\n{output}"
    print(f"Subscription {sub_name} redirected to n3 (port {n3_port}) and replicating")


# ============================================================================
# STEP 18 – Verify n3 accepts writes after promotion and replicates to n1
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_n3_accepts_writes_after_promotion(container_name, container_type):
    """Step 18: Verify n3 accepts writes as new Patroni leader and replication flows back to n1"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    rc, out = container.exec_run(
        ["bash", "-c",
         f"psql -h localhost -p {n1_port} -U {pguser} -d postgres "
         f"-t -c 'SELECT COUNT(*) FROM test;' 2>&1"],
        user="root"
    )
    assert rc == 0, f"Pre-insert count on n1 failed: {out.decode()}"
    n1_count_before = int(out.decode().strip())

    print(f"\nInserting into 'test' from n3 (port {n3_port}) after promotion")
    _exec(container,
          f"psql -h localhost -p {n3_port} -U {pguser} -d postgres "
          f"-c \"INSERT INTO test (data) VALUES ('from_n3_after_promotion');\" 2>&1",
          msg="Insert into test on n3 after promotion")

    print("Waiting 10s for Spock replication back to n1...")
    time.sleep(10)

    rc, out = container.exec_run(
        ["bash", "-c",
         f"psql -h localhost -p {n1_port} -U {pguser} -d postgres "
         f"-t -c 'SELECT COUNT(*) FROM test;' 2>&1"],
        user="root"
    )
    assert rc == 0, f"Post-insert count on n1 failed: {out.decode()}"
    n1_count_after = int(out.decode().strip())

    assert n1_count_after == n1_count_before + 1, (
        f"Expected n1 row count to increase by 1 after n3 insert "
        f"(was {n1_count_before}, got {n1_count_after}) — Spock replication may be broken"
    )
    print(f"n3 insert replicated to n1 via Spock (n1 count: {n1_count_before} -> {n1_count_after})")


# ============================================================================
# CLEANUP
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_cleanup(container_name, container_type):
    """Cleanup: Stop Patroni processes and PostgreSQL nodes"""
    if os.getenv("SKIP_CLEANUP", "false").lower() == "true":
        pytest.skip("SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)
    assert container.status == "running"

    config = get_container_config(container_type)
    pgbin  = config["pgbin"]
    pguser = config["pguser"]

    # Step 1: run reset_patroni.sh — stops Patroni, stops postgres on n2/n3,
    # and clears etcd state for the Patroni scope.
    reset_script = (Path(__file__).parent.parent / "utillities" / "reset_patroni.sh").resolve()
    if reset_script.exists():
        with reset_script.open('r') as f:
            script_content = f.read()
        container_script_path = "/tmp/reset_patroni.sh"
        exit_code, output = container.exec_run(
            ["bash", "-c", f"cat > {container_script_path} << 'RESETEOF'\n{script_content}\nRESETEOF"],
            user="root"
        )
        if exit_code == 0:
            container.exec_run(["bash", "-c", f"chmod +x {container_script_path}"], user="root")
            rc, out = container.exec_run(["bash", container_script_path], user="root")
            print(f"reset_patroni.sh output:\n{out.decode()}")
            if rc != 0:
                print(f"WARNING: reset_patroni.sh exited {rc} — continuing cleanup")
        else:
            print(f"WARNING: failed to copy reset_patroni.sh — continuing cleanup")
    else:
        print(f"WARNING: reset_patroni.sh not found at {reset_script} — skipping")

    # Step 2: stop n1 (managed manually, not by Patroni)
    pg_server_management.stop_server(container, pgbin, "/tmp/n1", str(n1_port), pguser)

    # Step 3: stop etcd service
    container.exec_run(["bash", "-c", "pkill -x etcd 2>/dev/null; systemctl stop etcd 2>/dev/null || true"], user="root")

    # Step 4: remove all data directories
    for node in ["n1", "n2", "n3"]:
        container.exec_run(["bash", "-c", f"rm -rf /tmp/{node}"], user="root")

    # Step 5: remove log and pgpass files
    for f in ["patroni_n2.log", "patroni_n3.log", "pgpass_n2", "pgpass_n3"]:
        container.exec_run(["bash", "-c", f"rm -f /tmp/{f}"], user="root")

    # Step 6: remove all pgedge-* packages
    rc, out = container.exec_run(
        ["bash", "-c",
         "if command -v dnf &>/dev/null; then "
         "  dnf remove -y 'pgedge-*' 2>&1 || true; "
         "elif command -v yum &>/dev/null; then "
         "  yum remove -y 'pgedge-*' 2>&1 || true; "
         "elif command -v apt-get &>/dev/null; then "
         "  apt-get remove -y 'pgedge-*' 2>&1 || true; "
         "fi"],
        user="root"
    )
    print(f"Package removal output:\n{out.decode()}")

    print("Cleanup completed")
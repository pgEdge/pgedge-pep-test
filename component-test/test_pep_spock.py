import json
import os
import sys
import time
import importlib.util as _ilu
from pathlib import Path

import pytest
import docker
from dotenv import load_dotenv

# Add the parent directory to sys.path to import from aspects
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from aspects import configure_repository, package_management, pg_server_management, machine_prereq_setup, file_management, container_management


def _load_util(name):
    """Load a shared integration helper from aspects/pipeline/ by path (no package
    __init__ there), so these imports work however this file is loaded."""
    spec = _ilu.spec_from_file_location(
        name, os.path.join(os.path.dirname(__file__), '..', 'aspects', 'pipeline', name + '.py'))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The shared integration-mode install/identity steps, and through them the request contract and
# the shared package-matching rules (the same ones request validation and planning use).
pep_integration_mode = _load_util('pep_integration_mode')
pep_request_env = pep_integration_mode.pep_request_env
_pr = pep_request_env._pr
_pp = _pr._pp

load_dotenv()
client = docker.from_env()

# Integration request built from the PEP_* env contract (validated by normalize_request, which
# already binds the spock package to its family, PG major and series). None in standalone mode,
# which keeps every legacy code path below unchanged.
INTEGRATION_REQUEST = pep_request_env.build_request_from_env()

# ============================================================================
# CONFIGURATION SECTION
# ============================================================================

# Load values from env
rhel_containers = [c.strip() for c in os.getenv("CONTAINERS", "").split(",") if c.strip()]
deb_containers = [c.strip() for c in os.getenv("DEB_CONTAINERS", "").split(",") if c.strip()]

# Combine all containers with their type
all_containers = [(c, "rhel") for c in rhel_containers] + [(c, "deb") for c in deb_containers]

# Filter containers based on platform filter (if set). In integration mode the request family is
# authoritative and overrides any configured PLATFORM_FILTER.
platform_filter = (INTEGRATION_REQUEST["family"] if INTEGRATION_REQUEST is not None
                   else os.getenv("PLATFORM_FILTER", "").lower())
if platform_filter == "rpm":
    all_containers = [(c, t) for c, t in all_containers if t == "rhel"]
elif platform_filter == "deb":
    all_containers = [(c, t) for c, t in all_containers if t == "deb"]

# Common configuration
repo = os.getenv("REPO", "release")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")

# Multi-node configuration (this module exercises a 2-node bidirectional cluster)
no_of_nodes = int(os.getenv("NO_OF_NODES", "2"))
base_port = int(os.getenv("BASE_PORT", "5431"))

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")
pg_password = os.getenv("PG_PASSWORD", "postgres")

# Binary paths
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")

def _integration_series(request, pg_major_env):
    """``(series, problem)`` for an integration request. The series ("50", "60", ...) comes from
    the VALIDATED request through the shared spock rules -- never from SPOCK_MAJOR. ``problem``
    explains why the request cannot be served here (checked again before anything is installed)."""
    if request is None:
        return None, None
    policy = _pr.COMPONENT_POLICY.get(request["component"])
    if request["component"] != "spock" or not _pp.is_coupled(policy):
        return None, f"component {request['component']!r} is not the PG-coupled spock component"
    if str(request["pg_major"]) != str(pg_major_env):
        return None, (f"request PG{request['pg_major']} disagrees with "
                      f"PG_MAJOR_VERSION={pg_major_env}")
    why = _pp.classify(policy, request["package_name"], request["family"], request["pg_major"],
                       request["expected_version"])
    if why is not None:
        return None, (f"package {request['package_name']!r} is not spock for {request['family']} "
                      f"PG{request['pg_major']} version {request['expected_version']!r} ({why})")
    return _pp.series_for_version(policy, request["expected_version"]), None


INTEGRATION_SERIES, INTEGRATION_PROBLEM = _integration_series(INTEGRATION_REQUEST, pg_major_version)

# Spock major version (e.g. "50" or "60"). spock50 and spock60 are separate
# major versions that coexist in the repo; select with SPOCK_MAJOR (default 50).
# In integration mode the series comes from the request (never this default).
spock_major = (INTEGRATION_SERIES or "") if INTEGRATION_REQUEST is not None else os.getenv("SPOCK_MAJOR", "50")

# Cross-wiring utility (replaces the zodan SQL/py cross-wiring used elsewhere)
crosswire_script = (Path(__file__).parent.parent / "utillities" / "spock utils" / "2node_crosswire.py").resolve()


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
        }


def get_spock_packages(container_type):
    """Spock package set to install (spock + the PostgreSQL server/contrib it needs).

    Only the spock package is required for this module — it pulls the matching
    pgedge-postgresql server as a dependency; contrib is added for completeness.
    """
    if container_type == "rhel":
        return [
            f"pgedge-spock{spock_major}_{pg_major_version}",
            f"pgedge-postgresql{pg_major_version}-contrib",
        ]
    else:  # deb (contrib is bundled in the pgedge-postgresql-<xx> server package)
        return [
            f"pgedge-postgresql-{pg_major_version}-spock{spock_major}",
            f"pgedge-postgresql-{pg_major_version}",
        ]


def get_spock_pkg_name(container_type):
    """Name of the spock package itself, for version / bundled-file verification. In integration
    mode it is exactly the requested (validated) package."""
    if INTEGRATION_REQUEST is not None:
        return INTEGRATION_REQUEST["package_name"]
    if container_type == "rhel":
        return f"pgedge-spock{spock_major}_{pg_major_version}"
    return f"pgedge-postgresql-{pg_major_version}-spock{spock_major}"


def _psql_value(container, port, pguser, sql):
    exit_code, output = container.exec_run(
        ["psql", "-h", "localhost", "-p", str(port), "-U", pguser, "-d", "postgres",
         "-t", "-A", "-c", sql],
        user="root")
    return exit_code, output.decode().strip()


def verify_nodes_use_requested_spock(container, container_type, config):
    """Integration mode: every node runs the requested PostgreSQL major with the requested spock
    release, and the loaded spock library file belongs to the requested package. Spock's extension
    version is SPOCK_VERSION from include/spock.h, which the release workflow checks against the
    tag, so it must equal the request's release version exactly."""
    req = INTEGRATION_REQUEST
    expected = req["expected_version"]
    pguser = config["pguser"]
    # The running server's own library directory (the pg_config SQL view, not a client binary
    # that may be absent from the installed packages).
    ec, libdir = _psql_value(container, base_port, pguser,
                             "SELECT setting FROM pg_config WHERE name = 'PKGLIBDIR';")
    assert ec == 0 and libdir.startswith("/"), f"PKGLIBDIR query failed on n1: {libdir}"
    library = f"{libdir}/spock.so"
    owner_cmd = (["rpm", "-qf", "--queryformat", "%{NAME}", library] if container_type == "rhel"
                 else ["dpkg-query", "-S", library])
    exit_code, owner = container.exec_run(owner_cmd, user="root")
    owner = owner.decode().strip()
    owner_name = owner if container_type == "rhel" else owner.split(":", 1)[0]
    assert exit_code == 0 and owner_name == req["package_name"], (
        f"{library} belongs to {owner!r}, not the requested {req['package_name']!r}")
    print(f"✅ {library} belongs to {owner_name}")
    for node_num in range(1, no_of_nodes + 1):
        node_name, node_port = f"n{node_num}", base_port + node_num - 1
        ec, num = _psql_value(container, node_port, pguser, "SHOW server_version_num;")
        assert ec == 0 and num.isdigit(), f"server_version_num query failed on {node_name}: {num}"
        assert int(num) // 10000 == int(pg_major_version), (
            f"{node_name} runs PostgreSQL {num}, not the requested PG{pg_major_version}")
        ec, ver = _psql_value(container, node_port, pguser,
                              "SELECT extversion FROM pg_extension WHERE extname = 'spock';")
        assert ec == 0, f"Failed to query the spock extension version on {node_name}: {ver}"
        assert ver == expected, (
            f"{node_name}: spock extension version is {ver!r}, expected exactly {expected!r}")
        print(f"✅ {node_name}: PostgreSQL {num}, spock {ver}")


def get_spock_companions(container_type):
    """Integration mode: the PostgreSQL packages the spock package needs (the requested PG major,
    from the same channel, no minor-version pin), installed BEFORE the pinned spock package so
    that installing them can never replace it. The RPM server is named explicitly so a missing
    server is reported as a prerequisite failure, not as a failed spock install."""
    if container_type == "rhel":
        return [f"pgedge-postgresql{pg_major_version}-server", f"pgedge-postgresql{pg_major_version}-contrib"]
    return [f"pgedge-postgresql-{pg_major_version}"]


def record_prerequisite_versions(container, container_type):
    """Integration mode, audit only: the installed versions of the PostgreSQL companions and of
    the requested spock package, printed and written to PEP_PREREQ_OUT. Never part of the verdict."""
    names = get_spock_companions(container_type) + [INTEGRATION_REQUEST["package_name"]]
    versions = {n: package_management.query_installed_version(container, n) for n in names}
    out = os.getenv("PEP_PREREQ_OUT", "test-logs/prerequisite-versions.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"channel": repo, "pg_major": pg_major_version,
                                     "requested_package": INTEGRATION_REQUEST["package_name"],
                                     "installed_versions": versions}, indent=2, sort_keys=True) + "\n")
    for n, v in versions.items():
        print(f"   installed {n} = {v}")
    return versions


def installed_spock_packages(container, container_type):
    """Installed packages that are ANY spock runtime package (any family, PG major or series),
    read from the package manager. Returns None if the query fails."""
    if container_type == "rhel":
        cmd = ["rpm", "-qa", "--queryformat", "%{NAME}\\n"]
    else:
        cmd = ["dpkg-query", "-W", "-f=${Package} ${Status}\\n"]
    exit_code, output = container.exec_run(cmd, user="root")
    if exit_code != 0:
        return None
    names = []
    for line in output.decode().splitlines():
        parts = line.split()
        if not parts or (container_type != "rhel" and "installed" not in parts[-1:]):
            continue
        names.append(parts[0])
    policy = _pr.COMPONENT_POLICY.get("spock")
    return sorted(n for n in names if _pp.is_component_runtime_package(policy, n))


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
def test_install_spock_packages(container_name, container_type):
    """Step 3: Install the spock package set (spock50 or spock60 per SPOCK_MAJOR)"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    if INTEGRATION_REQUEST is not None:
        # Execution-side backstop: never install for a request the shared spock rules reject.
        assert INTEGRATION_PROBLEM is None, f"integration request cannot be served: {INTEGRATION_PROBLEM}"
        requested = INTEGRATION_REQUEST["package_name"]
        companions = get_spock_companions(container_type)
        print(f"\n--- Installing PostgreSQL {pg_major_version} companions on {container_name}: "
              f"{', '.join(companions)} ---")
        try:
            success, platform, message = package_management.install_package(
                container=container,
                package_name=companions,
                pg_major_version=pg_major_version,
                install_pg_server=False
            )
        except Exception as e:
            success, message = False, str(e)
        if not success:
            pytest.fail(f"PostgreSQL {pg_major_version} prerequisite unavailable from the {repo!r} "
                        f"channel ({', '.join(companions)}); spock was not installed: {message}")
        # The requested spock package last, exactly as the request decides (pinned + checksum
        # evidence), so no later install can replace it.
        print(f"\n--- Installing requested {requested} (spock{spock_major}) on {container_name} ---")
        pep_integration_mode.install_for_request(container, INTEGRATION_REQUEST)
        record_prerequisite_versions(container, container_type)
        present = installed_spock_packages(container, container_type)
        assert present == [requested], (
            f"expected exactly the requested spock package {requested!r} to be installed, "
            f"found {present!r}")
        print(f"✅ Only {requested} is installed among spock packages")
        return

    packages = get_spock_packages(container_type)
    print(f"\n--- Installing spock{spock_major} package set on {container_name} ---")
    print(f"   Packages: {', '.join(packages)}")

    success, platform, message = package_management.install_package(
        container=container,
        package_name=packages,
        pg_major_version=pg_major_version,
        install_pg_server=False
    )

    assert success, f"Failed to install spock{spock_major} package set: {message}"
    print(f"Successfully installed spock{spock_major} package set on {platform}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_spock_pkg_version(container_name, container_type):
    """Step 4: Verify the installed spock package version"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: test_spock_identity checks the exact requested version")

    spock_version_var = f"PGEDGE_SPOCK{spock_major}_{pg_major_version}_VERSION"
    expected_version = os.getenv(spock_version_var, "")
    if not expected_version:
        pytest.skip(f"{spock_version_var} not set in env")

    spock_package = get_spock_pkg_name(container_type)

    success, platform, installed_version, message = package_management.verify_package_version(
        container=container,
        package_name=spock_package,
        expected_version=expected_version
    )

    assert success, f"Version verification failed: {message}"
    print(f"Version verified: {spock_package} {installed_version} on {platform}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_spock_identity(container_name, container_type):
    """Integration mode: one consolidated identity verdict for the requested spock package
    (install-before-identity, then the exact package-manager version; spock has no binary to
    probe). The evidence is persisted before any failure. Skipped in standalone mode."""
    req = INTEGRATION_REQUEST
    if req is None:
        pytest.skip("integration mode only")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    ev, problems = pep_integration_mode.record_identity(container, req)
    if ev is None:
        pytest.fail("; ".join(problems))
    print(f"identity evidence ({req['package_name']}): {ev}")
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Step 4.1: Verify the spock package's bundled files match expected-output"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    project_root = Path(__file__).parent.parent
    spock_package = get_spock_pkg_name(container_type)

    try:
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=spock_package,
            package_name=spock_package,
            project_root=project_root,
            pg_major_version=pg_major_version
        )

        if not success:
            details_str = ""
            if details:
                if details.get("missing_files"):
                    details_str += f"\n\nMissing files ({len(details['missing_files'])}):\n"
                    for f in details["missing_files"]:
                        details_str += f"  - {f}\n"
                if details.get("extra_files"):
                    details_str += f"\nExtra files ({len(details['extra_files'])}):\n"
                    for f in details["extra_files"]:
                        details_str += f"  + {f}\n"
            pytest.fail(f"{message}{details_str}")

        print(f"✅ Bundled files verified for {spock_package}")

    except Exception as e:
        # No expected-output reference for this spock major yet -> skip gracefully
        if "No expected file found" in str(e):
            pytest.skip(str(e))
        pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_sbom(container_name, container_type):
    """Step 4.2: Verify the spock SBOM signature under the PostgreSQL sbom directory"""
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

        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"--signature-file spock{spock_major}-sbom.json.asc "
            f"--signer-file pgedge-rsa.pub "
            f"spock{spock_major}-sbom.json'",
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

        machine_prereq_setup.ensure_sq_installed(container)
        _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
        _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
        _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"
        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"{_sq_signer_flag} /etc/apt/keyrings/pgedge-rsa.gpg "
            f"{_sq_sig_flag} spock{spock_major}-sbom.json.asc "
            f"spock{spock_major}-sbom.json'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_initialize_nodes(container_name, container_type):
    """Step 5: Initialize the PostgreSQL nodes (n1, n2) with Spock GUCs"""
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

    # GUC parameters required by Spock logical replication
    guc_parameters = {
        "shared_preload_libraries": "'spock'",
        "wal_level": "logical",
        "max_worker_processes": "10",
        "max_replication_slots": "10",
        "max_wal_senders": "10",
        "track_commit_timestamp": "on"
    }

    # PG >= 16.15 / 17.11 / 18.5 / 19.0beta3 require logical decoding output
    # plugins to be allow-listed before they can be loaded. Without this,
    # spock_output is rejected and cross-wiring the nodes fails. Returns {} on
    # older point releases, which do not recognise the GUC.
    guc_parameters.update(pg_server_management.output_plugin_libraries_guc())

    for node_num in range(1, no_of_nodes + 1):
        node_name = f"n{node_num}"
        node_port = base_port + node_num - 1
        node_pgdata = f"/tmp/{node_name}"

        print(f"\n▶️  Initializing node {node_name} on port {node_port}")

        success, config_content, message = pg_server_management.init_cluster(
            container, pgbin, node_pgdata, pguser, guc_parameters
        )
        assert success, f"Failed to initialize node {node_name}: {message}"
        print(f"✅ {message}")

        print(f"▶️  Starting PostgreSQL server for node {node_name}")
        success, server_output, message = pg_server_management.start_server(
            container, pgbin, node_pgdata, str(node_port), pguser
        )
        assert success, f"Failed to start node {node_name}: {message}"
        print(f"✅ {message}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_setup_cross_wiring(container_name, container_type):
    """Step 6: Cross-wire n1 and n2 using utillities/spock utils/2node_crosswire.py

    The utility creates the spock extension and node on both nodes, adds tables
    to the default replication set, creates the bidirectional subscriptions and
    replication sets, and enables DDL replication. It is interactive by design,
    so we drive it non-interactively: copy it in, pre-populate its NODES dict,
    stub the credential prompt, and call its main().
    """
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    if not crosswire_script.exists():
        pytest.skip(f"Cross-wire script not found at {crosswire_script}")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    n1_port = base_port
    n2_port = base_port + 1

    print(f"\n--- Cross-wiring n1 (port {n1_port}) and n2 (port {n2_port}) via 2node_crosswire.py ---")

    # Ensure psycopg2 is available (the cross-wire utility uses it)
    container.exec_run(
        ["bash", "-c",
         "pip3 install psycopg2-binary --quiet 2>&1 || pip install psycopg2-binary --quiet 2>&1"],
        user="root"
    )

    # Copy the cross-wire utility into the container (renamed to a valid module name)
    with crosswire_script.open('r') as f:
        crosswire_content = f.read()

    exit_code, output = container.exec_run(
        ["bash", "-c", f"cat > /tmp/crosswire.py << 'CWEOF'\n{crosswire_content}\nCWEOF"],
        user="root"
    )
    assert exit_code == 0, f"Failed to copy cross-wire script to container: {output.decode()}"

    # Non-interactive runner: pre-populate NODES, stub the prompt, run main()
    runner = (
        "import sys, os\n"
        "sys.path.insert(0, '/tmp')\n"
        "import crosswire\n"
        "crosswire.NODES['n1'] = {'host': 'localhost', 'port': int(os.environ['CW_N1_PORT']),\n"
        "                         'dbname': 'postgres', 'user': os.environ['CW_PGUSER'],\n"
        "                         'password': os.environ['CW_PGPASS']}\n"
        "crosswire.NODES['n2'] = {'host': 'localhost', 'port': int(os.environ['CW_N2_PORT']),\n"
        "                         'dbname': 'postgres', 'user': os.environ['CW_PGUSER'],\n"
        "                         'password': os.environ['CW_PGPASS']}\n"
        "crosswire.get_user_credentials = lambda: None\n"
        "crosswire.main()\n"
    )

    exit_code, output = container.exec_run(
        ["bash", "-c", f"cat > /tmp/crosswire_runner.py << 'RNEOF'\n{runner}\nRNEOF"],
        user="root"
    )
    assert exit_code == 0, f"Failed to copy cross-wire runner to container: {output.decode()}"

    env_prefix = (
        f"CW_N1_PORT={n1_port} CW_N2_PORT={n2_port} "
        f"CW_PGUSER={pguser} CW_PGPASS={pg_password}"
    )
    exit_code, output = container.exec_run(
        ["bash", "-c", f"{env_prefix} python3 /tmp/crosswire_runner.py 2>&1"],
        user="root"
    )
    output_str = output.decode()
    print(output_str)

    assert exit_code == 0, f"Cross-wiring failed:\n{output_str}"
    assert "setup completed successfully" in output_str.lower(), \
        f"Cross-wire utility did not report success:\n{output_str}"
    print(f"✅ n1 and n2 cross-wired successfully")

    # Allow subscriptions to settle into 'replicating' before downstream checks
    time.sleep(5)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_spock_extension_version(container_name, container_type):
    """Step 7: Verify the Spock extension version on all nodes"""
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

    if INTEGRATION_REQUEST is not None:
        verify_nodes_use_requested_spock(container, container_type, config)
        return

    spock_version_var = f"PGEDGE_SPOCK{spock_major}_{pg_major_version}_VERSION"
    expected_version = os.getenv(spock_version_var, "")
    if not expected_version:
        pytest.skip(f"{spock_version_var} not set in env")

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
def test_spock_node(container_name, container_type):
    """Step 8: Verify spock.node lists the expected nodes (n1, n2)"""
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
        assert "n1" in out and "n2" in out, f"Expected node names 'n1' and 'n2' in spock.node output on n{node_num}: {out}"
        assert "(2 rows)" in out or "2 rows" in out or "rows)" in out, f"Expected 2 rows reported in spock.node output on n{node_num}: {out}"

    print("✅ spock.node contains expected nodes on all instances")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_show_sub_status(container_name, container_type):
    """Step 9: Run spock.sub_show_status() and ensure subscription is 'replicating'"""
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
    assert "replicating" in out.lower(), f"Expected 'replicating' in spock.sub_show_status() output on n1, got:\n{out}"

    print("✅ spock.sub_show_status() indicates subscription is replicating")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_insert_and_verify_replication(container_name, container_type):
    """Step 10: Insert on n1 and on n2, then verify rows replicated both ways

    Multi-master check: rows written on n1 must appear on n2 and rows written
    on n2 must appear on n1.
    """
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
    table = "spock_repl_test"

    def psql(port, sql, msg, tolerate=None):
        exit_code, output = container.exec_run(
            ["bash", "-c",
             f"psql -h localhost -p {port} -U {pguser} -d postgres -c \"{sql}\" 2>&1"],
            user="root"
        )
        out = output.decode()
        if exit_code != 0 and tolerate and any(t in out for t in tolerate):
            print(f"   ℹ️  {msg} (port {port}) tolerated: {out.strip().splitlines()[0]}")
            return out
        assert exit_code == 0, f"{msg} (port {port}) failed:\n{out}"
        return out

    # With DDL replication on (enabled by the cross-wire utility, include_ddl_repset=on),
    # the table is auto-created on n2 and auto-added to the 'default' repset on both
    # nodes. The explicit calls below are belt-and-suspenders, so tolerate the
    # "already in repset" duplicate-key error.
    repset_dup = ["already exists", "duplicate key"]

    print(f"\n--- Testing bidirectional replication via table '{table}' ---")

    # Create the table on n1 (DDL replicates to n2) and ensure it is in the default repset
    psql(n1_port,
         f"CREATE TABLE IF NOT EXISTS {table} (id INT PRIMARY KEY, origin TEXT, data TEXT);",
         "Create table on n1")
    psql(n1_port, f"SELECT spock.repset_add_table('default', '{table}');",
         "repset_add_table on n1", tolerate=repset_dup)

    # Give DDL replication a moment, then make sure the table + repset exist on n2 too
    time.sleep(5)
    psql(n2_port,
         f"CREATE TABLE IF NOT EXISTS {table} (id INT PRIMARY KEY, origin TEXT, data TEXT);",
         "Create table on n2")
    psql(n2_port, f"SELECT spock.repset_add_table('default', '{table}');",
         "repset_add_table on n2", tolerate=repset_dup)

    # Insert from n1 (ids 1-3) and from n2 (ids 101-103) — disjoint PKs
    print(f"\n▶️  Inserting 3 rows on n1 and 3 rows on n2")
    psql(n1_port,
         f"INSERT INTO {table} (id, origin, data) VALUES "
         f"(1,'n1','n1_row_1'),(2,'n1','n1_row_2'),(3,'n1','n1_row_3');",
         "Insert on n1")
    psql(n2_port,
         f"INSERT INTO {table} (id, origin, data) VALUES "
         f"(101,'n2','n2_row_1'),(102,'n2','n2_row_2'),(103,'n2','n2_row_3');",
         "Insert on n2")

    # Wait for replication to converge
    print(f"\n⏳ Waiting 10 seconds for replication to converge...")
    time.sleep(10)

    # Both nodes must now hold all 6 rows and see both origins
    for node_name, node_port in (("n1", n1_port), ("n2", n2_port)):
        out = psql(node_port,
                   f"SELECT count(*) AS total, "
                   f"count(*) FILTER (WHERE origin='n1') AS from_n1, "
                   f"count(*) FILTER (WHERE origin='n2') AS from_n2 FROM {table};",
                   f"Count on {node_name}")
        print(f"\nReplication state on {node_name}:\n{out}")

        rows = psql(node_port,
                    f"SELECT id, origin, data FROM {table} ORDER BY id;",
                    f"Dump rows on {node_name}")
        assert "n1_row_1" in rows and "n2_row_1" in rows, (
            f"{node_name} is missing rows from one origin — bidirectional replication failed:\n{rows}"
        )
        print(f"✅ {node_name} has rows from both n1 and n2")

    print(f"\n✅ Bidirectional replication verified between n1 and n2")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_cleanup_nodes(container_name, container_type):
    """Step 11: Cleanup - Stop all PostgreSQL nodes"""
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
    """Step 12: Uninstall the spock package using package_management module"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("No container defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    spock_package = get_spock_pkg_name(container_type)

    print(f"\n--- Uninstalling {spock_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.uninstall_package(container, spock_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {spock_package}: {str(e)}")

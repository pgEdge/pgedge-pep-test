import os
import re
import sys
import subprocess
import importlib.util as _ilu
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest
import docker
from dotenv import load_dotenv

# Add the parent directory to sys.path to import from aspects
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from aspects import configure_repository, package_management, pg_server_management, machine_cleanup, machine_prereq_setup, file_management, container_management


def _load_util(name):
    """Load a shared integration helper from aspects/pipeline/ by path (no package
    __init__ there), so these imports work however this file is loaded."""
    spec = _ilu.spec_from_file_location(
        name, os.path.join(os.path.dirname(__file__), '..', 'aspects', 'pipeline', name + '.py'))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The integration-mode steps shared by the PG-coupled extension tests, and through them the shared
# install/identity steps, the request contract and the package-matching rules. One instance of each
# (taken from pep_extension_checks), so tests that patch them reach every step.
pep_extension_checks = _load_util('pep_extension_checks')
pep_integration_mode = pep_extension_checks.pep_integration_mode
pep_request_env = pep_integration_mode.pep_request_env
_pr = pep_request_env._pr
_pp = _pr._pp

load_dotenv()
client = docker.from_env()

# Integration request built from the PEP_* env contract (validated by normalize_request, which
# already binds the snowflake package to its family and PG major). None in standalone mode, which
# keeps the standalone code paths below.
INTEGRATION_REQUEST = pep_request_env.build_request_from_env()

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
# If platform_filter is empty or "all", use all containers

# Common configuration
repo = os.getenv("REPO", "release")
upgrade_repo = os.getenv("UPGRADE_REPO", "staging")
skip_cleanup = os.getenv("SKIP_CLEANUP", "false").lower() == "true"
snowflake_node = os.getenv("SNOWFLAKE_NODE", "1").strip()
pgport = os.getenv("PG_PORT", "5432")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "17")
snowflake_version = os.getenv(f"PGEDGE_SNOWFLAKE_{pg_major_version}_VERSION", "2.4")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")
rhel_snowflake_package = os.getenv("SNOWFLAKE_PACKAGE", f"pgedge-snowflake_{pg_major_version}")
rhel_snowflake_lib = os.getenv("RHEL_SNOWFLAKE_LIB", f"/usr/pgsql-{pg_major_version}/lib")
rhel_bundled_files = os.getenv(
    "SNOWFLAKE_BUNDLED_FILES",
    f"/usr/pgsql-{pg_major_version}/lib/snowflake.so,/usr/pgsql-{pg_major_version}/sbom/snowflake-sbom.json,/usr/pgsql-{pg_major_version}/sbom/snowflake-sbom.json.asc,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.0--1.1.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.0.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.1--1.2.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.1.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.2--2.0.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--1.2.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--2.0--2.2.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--2.0.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake--2.2.sql,/usr/pgsql-{pg_major_version}/share/extension/snowflake.control,/usr/share/doc/pgedge-snowflake_{pg_major_version}/README.md,/usr/share/licenses/pgedge-snowflake_{pg_major_version}/LICENSE.md"
).split(",")

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_snowflake_package = os.getenv("DEB_SNOWFLAKE_PACKAGE", f"pgedge-postgresql-{pg_major_version}-snowflake")
deb_snowflake_lib = os.getenv("DEB_SNOWFLAKE_LIB", f"/usr/lib/postgresql/{pg_major_version}/lib")
deb_bundled_files = os.getenv(
    "DEB_SNOWFLAKE_BUNDLED_FILES",
    f"{deb_pg_path}/lib/snowflake.so,{deb_pg_share_path}/extension/snowflake--1.0--1.1.sql,{deb_pg_share_path}/extension/snowflake--1.0.sql,{deb_pg_share_path}/extension/snowflake--1.1--1.2.sql,{deb_pg_share_path}/extension/snowflake--1.1.sql,{deb_pg_share_path}/extension/snowflake--1.2--2.0.sql,{deb_pg_share_path}/extension/snowflake--1.2.sql,{deb_pg_share_path}/extension/snowflake--2.0--2.2.sql,{deb_pg_share_path}/extension/snowflake--2.0.sql,{deb_pg_share_path}/extension/snowflake--2.2.sql,{deb_pg_share_path}/extension/snowflake.control"
).split(",")

# Snowflake stripped library names (comma-separated list)
snowflake_stripped_lib = os.getenv("SNOWFLAKE_STRIPPED_LIB", "snowflake.so")


def _integration_problem(request, pg_major_env):
    """Why an integration request cannot be served by this test (None if it can); checked again
    before anything is installed. normalize_request already rejects a package that is not the
    component's package for the request's family and PG major; this backstop also covers a request
    for another component and a request whose PG major disagrees with PG_MAJOR_VERSION."""
    if request is None:
        return None
    policy = _pr.COMPONENT_POLICY.get(request["component"])
    if request["component"] != "snowflake" or not _pp.is_coupled(policy):
        return f"component {request['component']!r} is not the PG-coupled snowflake component"
    if str(request["pg_major"]) != str(pg_major_env):
        return f"request PG{request['pg_major']} disagrees with PG_MAJOR_VERSION={pg_major_env}"
    why = _pp.classify(policy, request["package_name"], request["family"], request["pg_major"],
                       request["expected_version"])
    if why is not None:
        return (f"package {request['package_name']!r} is not snowflake for {request['family']} "
                f"PG{request['pg_major']} ({why})")
    return None


INTEGRATION_PROBLEM = _integration_problem(INTEGRATION_REQUEST, pg_major_version)

# snowflake.node is the 10-bit node field of every id. snowflake.c defines the GUC with bounds
# 0..1023 (_PG_init) and snowflake_nextval() rejects 0 as "value for snowflake.node is not set", so a
# usable node is 1..1023 (README: "The permitted values are 1 thru 1023"). The configured value is
# written to postgresql.conf and compared exactly with SHOW snowflake.node, so it must be in
# canonical decimal form.
SNOWFLAKE_NODE_MIN, SNOWFLAKE_NODE_MAX = 1, 1023


def snowflake_node_problem(value):
    """None if ``value`` is a usable snowflake.node setting; otherwise why not."""
    if (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value)
            or not SNOWFLAKE_NODE_MIN <= int(value) <= SNOWFLAKE_NODE_MAX):
        return (f"SNOWFLAKE_NODE={value!r} is not a usable snowflake.node: it must be an integer "
                f"{SNOWFLAKE_NODE_MIN}..{SNOWFLAKE_NODE_MAX} without sign, spaces or leading zeros")
    return None


SNOWFLAKE_NODE_PROBLEM = snowflake_node_problem(snowflake_node)


def configured_node():
    """The configured snowflake.node as an int (fails the test if SNOWFLAKE_NODE is not usable)."""
    assert SNOWFLAKE_NODE_PROBLEM is None, SNOWFLAKE_NODE_PROBLEM
    return int(snowflake_node)


# Snowflake id layout (snowflake.c: SNOWFLAKE_*_SHIFT/_MASK and SNOWFLAKE_EPOCH_OFFSET): bits 0-11
# count, bits 12-21 node, bits 22-62 milliseconds since 2023-01-01 00:00:00 UTC.
SNOWFLAKE_EPOCH_OFFSET_MS = 1672531200 * 1000


def decode_snowflake(sf_id):
    """``(msec, node, count)`` fields of a snowflake id, as snowflake.c's int64ToSnowflake."""
    return (sf_id >> 22) & 0x1FFFFFFFFFF, (sf_id >> 12) & 0x3FF, sf_id & 0xFFF


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb). In integration mode the snowflake
    package is exactly the requested (validated) package."""
    requested = INTEGRATION_REQUEST["package_name"] if INTEGRATION_REQUEST is not None else None
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "snowflake_package": requested or rhel_snowflake_package,
            "snowflake_lib": rhel_snowflake_lib,
            "bundled_files": rhel_bundled_files
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "snowflake_package": requested or deb_snowflake_package,
            "snowflake_lib": deb_snowflake_lib,
            "bundled_files": deb_bundled_files
        }


def get_running_container(container_name):
    """The named container, which must be running. A missing container skips a standalone run; in
    integration mode it fails, because skips are reported separately and the verdict could still
    pass with the check skipped."""
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pep_extension_checks.skip_or_fail(INTEGRATION_REQUEST,
                                          f"Container {container_name} not found or not running.")
    assert container.status == "running"
    return container


def psql_session(container, pgbin, pguser, *statements):
    """``(exit_code, lines)`` of ONE psql session running ``statements`` in order, each as its own -c
    (each completes before the next starts, and a SET applies to the later ones), stopping at the
    first error. Unaligned, tuples-only, quiet (no command tags), without psqlrc; it connects like
    pg_server_management.execute_psql_query. ``lines`` are the non-blank output lines, errors
    included."""
    cmd = [f"{pgbin}/psql", "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1",
           "-p", str(pgport), "-U", pguser, "-d", "postgres"]
    for sql in statements:
        cmd += ["-c", sql]
    exit_code, output = container.exec_run(cmd, user=pguser)
    return exit_code, [line for line in output.decode().splitlines() if line.strip()]


def _int_fields(line, count):
    """The ``count`` integer fields of one unaligned row, or None if it is not such a row."""
    parts = line.split("|")
    if len(parts) != count or not all(re.fullmatch(r"-?[0-9]+", p) for p in parts):
        return None
    return [int(p) for p in parts]


def burst_problems(lines, node, size):
    """Problems with a burst of ``size`` ids from ONE sequence, given rows ``n|id|get_node|get_count``
    in call order. snowflake.c compute_next_flake(): the msec is the clock when it has advanced,
    otherwise the previous msec with count + increment (a count past 4095 wraps to msec + 1), and
    the node is always snowflake.node -- so with one node every id is larger than the previous one,
    whatever the clock does, and get_node()/get_count() return the node and count fields."""
    rows = [_int_fields(line, 4) for line in lines]
    if any(r is None for r in rows):
        return [f"unexpected nextval burst output: {lines[:5]!r}"]
    if [r[0] for r in rows] != list(range(1, size + 1)):
        return [f"expected {size} ids in call order, got {len(rows)} rows"]
    problems, prev = [], None
    for n, sf_id, got_node, got_count in rows:
        _msec, bits_node, bits_count = decode_snowflake(sf_id)
        if got_node != node or bits_node != node:
            problems.append(f"id #{n} {sf_id}: get_node()={got_node}, node bits={bits_node}, "
                            f"expected the configured node {node}")
        if got_count != bits_count:
            problems.append(f"id #{n} {sf_id}: get_count()={got_count} disagrees with its count "
                            f"bits {bits_count}")
        if prev is not None and sf_id <= prev:
            problems.append(f"id #{n} {sf_id} is not greater than the previous id {prev}")
        prev = sf_id
    return problems[:10]


def epoch_problems(before_ms, sf_id, after_ms, epoch_ms, got_count, node):
    """Problems with the FIRST id of a fresh sequence, taken between two clock readings (Unix
    milliseconds, truncated) made by separate statements before and after it. snowflake.c: nextval
    stores the CLOCK_REALTIME milliseconds since SNOWFLAKE_EPOCH_OFFSET, truncated (tv_nsec /
    1000000); a fresh sequence's last value (1) has msec 0, so the first id takes the clock msec and
    count 0. get_epoch() returns (msec + offset) / 1000 as numeric, so ``epoch_ms`` (get_epoch * 1000)
    is that Unix millisecond exactly. The bracket only assumes the wall clock does not step back."""
    msec, bits_node, bits_count = decode_snowflake(sf_id)
    unix_ms = msec + SNOWFLAKE_EPOCH_OFFSET_MS
    problems = []
    try:
        if Decimal(epoch_ms) != unix_ms:
            problems.append(f"get_epoch({sf_id}) * 1000 = {epoch_ms}, but its msec bits give {unix_ms}")
    except InvalidOperation:
        problems.append(f"get_epoch({sf_id}) * 1000 is not a number: {epoch_ms!r}")
    if not before_ms <= unix_ms <= after_ms:
        problems.append(f"id {sf_id} carries Unix ms {unix_ms}, outside the clock readings "
                        f"[{before_ms}, {after_ms}] taken before and after it")
    if bits_count != 0 or got_count != 0:
        problems.append(f"the first id of a fresh sequence must have count 0, got get_count()="
                        f"{got_count} (count bits {bits_count})")
    if bits_node != node:
        problems.append(f"id {sf_id} carries node {bits_node}, expected {node}")
    return problems


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

    container = get_running_container(container_name)

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
    """Step 2: Install pgedge-snowflake using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if INTEGRATION_REQUEST is not None:
        # Execution-side backstop: never install for a request this test cannot serve.
        assert INTEGRATION_PROBLEM is None, f"integration request cannot be served: {INTEGRATION_PROBLEM}"
        container = get_running_container(container_name)
        # The PostgreSQL server for the requested major first (unpinned, same channel), then exactly
        # the requested package (pinned, with checksum evidence), then the only-package check.
        pep_extension_checks.install_requested_extension(
            container, container_type, INTEGRATION_REQUEST, pg_major_version, repo)
        return

    # Get container-specific configuration
    config = get_container_config(container_type)
    snowflake_package = config["snowflake_package"]

    container = get_running_container(container_name)

    print(f"\n--- Installing {snowflake_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to install the package
    try:
        success, platform, message = package_management.install_package(container, snowflake_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {snowflake_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_upgrade(container_name, container_type):
    """Upgrade component package if UPGRADE=true"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: the pinned requested package is never upgraded")
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    snowflake_package = config["snowflake_package"]

    container = get_running_container(container_name)

    print(f"\n--- Upgrading {snowflake_package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    # Use the package_management module to upgrade the package
    try:
        success, platform, message = package_management.upgrade_package(container, snowflake_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{snowflake_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {snowflake_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: test_snowflake_identity checks the exact requested version")

    if not snowflake_version:
        pytest.skip("No SNOWFLAKE_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    snowflake_package = config["snowflake_package"]

    container = get_running_container(container_name)

    print(f"\n--- Verifying {snowflake_package} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, snowflake_package, snowflake_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {snowflake_package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_identity(container_name, container_type):
    """Integration mode: one consolidated identity verdict for the requested snowflake package
    (install-before-identity, then the exact package-manager version; snowflake has no binary to
    probe). The evidence is persisted before any failure. Skipped in standalone mode."""
    req = INTEGRATION_REQUEST
    if req is None:
        pytest.skip("integration mode only")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = get_running_container(container_name)

    ev, problems = pep_integration_mode.record_identity(container, req)
    if ev is None:
        pytest.fail("; ".join(problems))
    print(f"identity evidence ({req['package_name']}): {ev}")
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_binaries_stripped(container_name, container_type):
    """Step 4: Verify that each configured Snowflake library is a stripped ELF file. `file` prints
    "not stripped" for an unstripped file, so this uses the strict shared check rather than a
    substring match on "stripped" (which accepted unstripped and missing files)."""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = get_running_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    snowflake_lib_dir = config.get("snowflake_lib", "").rstrip('/')
    libraries = [n.strip() for n in snowflake_stripped_lib.split(",") if n.strip()]

    if not snowflake_lib_dir or not libraries:
        pep_extension_checks.skip_or_fail(
            INTEGRATION_REQUEST, f"No snowflake library directory or library name configured for {container_type}")

    print(f"\n--- Verifying Snowflake libraries are stripped in {snowflake_lib_dir} on {container_name} ({container_type}) ---")

    problems = []
    for name in libraries:
        path = f"{snowflake_lib_dir}/{name}"
        problem = pep_extension_checks.stripped_problem(container, path)
        if problem:
            problems.append(problem)
        else:
            print(f"✅ {path} is stripped")
    if problems:
        pytest.fail("Snowflake libraries are not all stripped:\n" + "\n".join(problems))


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_sbom(container_name, container_type):
    """Verify SBOM signature files located under the PostgreSQL path sbom directory"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = get_running_container(container_name)

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
            f"--signature-file snowflake-sbom.json.asc "
            f"--signer-file pgedge-rsa.pub "
            f"snowflake-sbom.json'",
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
            f"{_sq_sig_flag} snowflake-sbom.json.asc "
            f"snowflake-sbom.json'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files for the snowflake component match expected files

    This compares the installed files from rpm/deb with expected files
    in expected-output/rpm/ or expected-output/deb/ directory
    """
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    container = get_running_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    snowflake_package = config["snowflake_package"]

    # Get project root directory (parent of component-test/)
    project_root = Path(__file__).parent.parent

    try:
        # Call reusable verification function
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=snowflake_package,  # Use package name as component name
            package_name=snowflake_package,
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
        # No expected-output reference -> a standalone run skips; in integration mode it fails,
        # because the verdict could still pass with this verification skipped.
        if "No expected file found" in str(e):
            pep_extension_checks.skip_or_fail(INTEGRATION_REQUEST, str(e))
        else:
            pytest.fail(f"Failed to verify bundled files: {str(e)}")




@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_init_cluster(container_name, container_type):
    """Initialize PostgreSQL cluster with Snowflake-specific GUC parameters using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = get_running_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    # The node every id must carry (SNOWFLAKE_NODE, checked against the product's range first).
    assert SNOWFLAKE_NODE_PROBLEM is None, SNOWFLAKE_NODE_PROBLEM

    print(f"\n--- Initializing cluster on {container_name} ---")

    # Define Snowflake-specific GUC parameters
    guc_parameters = {
        # Test assumption, not a product requirement: snowflake does not need preloading (its
        # _PG_init only defines the snowflake.node GUC, and a postgresql.conf value is applied when
        # the library loads on first use). Preloading is harmless and defines the GUC, with its
        # 0..1023 bounds, from server start.
        "shared_preload_libraries": "'snowflake'",
        "track_commit_timestamp": "on",
        "snowflake.node": snowflake_node
    }

    # Use the pg_server_management module to initialize cluster
    try:
        success, config_content, message = pg_server_management.init_cluster(
            container, pgbin, pgdata, pguser, guc_parameters
        )
        assert success, f"Cluster initialization failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to initialize cluster: {str(e)}")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_start_server(container_name, container_type):
    """Start PostgreSQL server using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = get_running_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Starting PostgreSQL server on {container_name} ---")

    # Use the pg_server_management module to start the server
    try:
        success, server_output, message = pg_server_management.start_server(
            container, pgbin, pgdata, pgport, pguser
        )
        assert success, f"Server start failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to start PostgreSQL server: {str(e)}")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_check_connection(container_name, container_type):
    """Check PostgreSQL connection using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = get_running_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Checking PostgreSQL connection on {container_name} ---")

    # Use the pg_server_management module to check connection
    try:
        success, version_output, message = pg_server_management.check_connection(
            container, pgbin, pgport, pguser
        )
        assert success, f"Connection check failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to check PostgreSQL connection: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_extension_loaded(container_name, container_type):
    """Verify Snowflake extension is loaded via shared_preload_libraries"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Checking if Snowflake extension is loaded on {container_name} ---")

    # Preloading is this test's own setup (see test_init_cluster), not a product requirement.
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SHOW shared_preload_libraries;")
    assert exit_code == 0, f"Failed to check shared_preload_libraries: {output}"
    assert "snowflake" in output, f"Snowflake not in shared_preload_libraries: {output}"
    print(f"✅ Snowflake extension is loaded in shared_preload_libraries")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_create_extension(container_name, container_type):
    """Create Snowflake extension in the database"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Creating Snowflake extension on {container_name} ---")

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "CREATE EXTENSION IF NOT EXISTS snowflake;")
    assert exit_code == 0, f"Failed to create Snowflake extension: {output}"
    print(f"✅ Snowflake extension created successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_requested_extension(container_name, container_type):
    """Integration mode: the running server's snowflake.so belongs to the requested package, the
    server runs the requested PostgreSQL major, and the SQL extension version equals the request's
    expected_version exactly. The snowflake release workflow requires snowflake.control's
    default_version to equal the tag's version (no prefix or pre-release form), so CREATE EXTENSION
    installs exactly that version. Skipped in standalone mode."""
    req = INTEGRATION_REQUEST
    if req is None:
        pytest.skip("integration mode only")

    container = get_running_container(container_name.strip())
    config = get_container_config(container_type)
    pep_extension_checks.verify_requested_extension(
        container, container_type, req, ports=[pgport], pguser=config["pguser"],
        library="snowflake.so", extname="snowflake", pg_major=pg_major_version,
        version_problem=pep_extension_checks.exact_version(req["expected_version"]))


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_node_parameter(container_name, container_type):
    """Test Case 1: SHOW snowflake.node equals the configured SNOWFLAKE_NODE exactly (a substring
    check would accept "10" or "11" for node 1)."""
    node = configured_node()
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 1: Checking snowflake.node parameter on {container_name} ---")

    exit_code, lines = psql_session(container, pgbin, pguser, "SHOW snowflake.node")

    assert exit_code == 0, f"Failed to show snowflake.node: {lines}"
    assert lines == [str(node)], f"Expected snowflake.node={node} exactly, got: {lines}"

    print(f"✅ snowflake.node is correctly set to {node}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_format_function(container_name, container_type):
    """Test Case 2: snowflake.format() reports each field of an id exactly. A fixed synthetic id with
    distinct fields (msec FORMAT_SAMPLE_MSEC, node 513, count 7) must give "id" = the node, "count" =
    the count and "ts" = the id's time (snowflake--2.3.sql: format() builds the jsonb from get_node(),
    get_count() and to_timestamp(get_epoch())); its keys alone prove nothing, they are literals."""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 2: Testing snowflake.format() function on {container_name} ---")

    sample = format_sample_id()
    unix_ms = FORMAT_SAMPLE_MSEC + SNOWFLAKE_EPOCH_OFFSET_MS
    exit_code, lines = psql_session(
        container, pgbin, pguser,
        f"SELECT f->>'id', f->>'count', (f->>'ts')::timestamptz = to_timestamp({unix_ms} / 1000.0) "
        f"FROM snowflake.format({sample}) AS f")

    assert exit_code == 0, f"Failed to execute snowflake.format(): {lines}"
    want = f"{FORMAT_SAMPLE_NODE}|{FORMAT_SAMPLE_COUNT}|t"
    assert lines == [want], (
        f"snowflake.format({sample}) gave id|count|ts-matches {lines}, expected [{want!r}]")
    print(f"✅ snowflake.format({sample}) reports node {FORMAT_SAMPLE_NODE}, count {FORMAT_SAMPLE_COUNT} "
          f"and its time")


# A fixed id for the format() check: 2024-01-11 18:08:59.845 UTC, node 513, count 7.
FORMAT_SAMPLE_MSEC, FORMAT_SAMPLE_NODE, FORMAT_SAMPLE_COUNT = 32465339845, 513, 7


def format_sample_id():
    """The snowflake id with the FORMAT_SAMPLE_* fields (snowflake.c's bit layout)."""
    return (FORMAT_SAMPLE_MSEC << 22) | (FORMAT_SAMPLE_NODE << 12) | FORMAT_SAMPLE_COUNT


# Ids drawn in one burst from one sequence. Enough calls to span many ids within one millisecond
# (the count path) and across milliseconds (the clock path); the result does not depend on timing.
BURST_SIZE = 1000

# The server's wall clock now, in Unix milliseconds truncated like snowflake.c's now_msec.
_NOW_UNIX_MS = "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint"


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_ids_carry_the_configured_node(container_name, container_type):
    """A burst of nextval() ids from one sequence strictly increases, and get_node() returns the
    configured node for every id (as do the id's node bits); get_count() matches the count bits.
    Deterministic: see burst_problems for the product rule each assertion relies on."""
    node = configured_node()
    container = get_running_container(container_name.strip())
    config = get_container_config(container_type)

    print(f"\n--- Drawing {BURST_SIZE} snowflake ids from one sequence on {container_name} ---")

    # ORDER BY puts the volatile nextval() calls in n order (PostgreSQL evaluates volatile output
    # expressions in sorted order), so the rows come back in call order.
    exit_code, lines = psql_session(
        container, config["pgbin"], config["pguser"],
        "CREATE SEQUENCE pep_sf_burst_seq",
        "WITH ids AS (SELECT g.n, snowflake.nextval('pep_sf_burst_seq'::regclass) AS id "
        f"FROM generate_series(1, {BURST_SIZE}) AS g(n) ORDER BY g.n) "
        "SELECT n, id, snowflake.get_node(id), snowflake.get_count(id) FROM ids ORDER BY n")
    assert exit_code == 0, f"nextval burst failed: {lines[-5:]}"
    problems = burst_problems(lines, node, BURST_SIZE)
    assert not problems, "snowflake ids are wrong:\n" + "\n".join(problems)
    print(f"✅ {BURST_SIZE} ids strictly increase and all carry node {node}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_id_time_is_bracketed(container_name, container_type):
    """The first id of a fresh sequence carries the server clock's millisecond, bracketed by clock
    readings taken by the statements just before and after it (one session, in order), with count
    0; get_epoch() * 1000 equals that millisecond exactly. No timing tolerance: see epoch_problems."""
    node = configured_node()
    container = get_running_container(container_name.strip())
    config = get_container_config(container_type)

    print(f"\n--- Bracketing a snowflake id between clock readings on {container_name} ---")

    exit_code, lines = psql_session(
        container, config["pgbin"], config["pguser"],
        "CREATE SEQUENCE pep_sf_epoch_seq", _NOW_UNIX_MS,
        "SELECT snowflake.nextval('pep_sf_epoch_seq'::regclass)", _NOW_UNIX_MS)
    assert exit_code == 0, f"bracketed nextval failed: {lines}"
    values = [_int_fields(line, 1) for line in lines]
    assert len(values) == 3 and None not in values, f"unexpected bracket output: {lines}"
    (before_ms,), (sf_id,), (after_ms,) = values

    exit_code, lines = psql_session(
        container, config["pgbin"], config["pguser"],
        f"SELECT snowflake.get_epoch({sf_id}) * 1000, snowflake.get_count({sf_id})")
    assert exit_code == 0 and len(lines) == 1, f"get_epoch/get_count of {sf_id} failed: {lines}"
    epoch_ms, _, count = lines[0].partition("|")
    assert re.fullmatch(r"-?[0-9]+", count), f"unexpected get_count output: {lines}"
    problems = epoch_problems(before_ms, sf_id, after_ms, epoch_ms, int(count), node)
    assert not problems, "snowflake id time is wrong:\n" + "\n".join(problems)
    print(f"✅ id {sf_id} carries a time within [{before_ms}, {after_ms}] ms and count 0")


# snowflake.c snowflake_nextval(): ereport(ERROR, errmsg("value for snowflake.node is not set")) when
# the node is 0, before the sequence is touched.
NODE_NOT_SET_ERROR = "value for snowflake.node is not set"
# PostgreSQL's range error for the bounds snowflake.c's _PG_init gives the GUC (0 .. 1023).
NODE_RANGE_ERROR = 'outside the valid range for parameter "snowflake.node" (0 .. 1023)'


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_node_setting_is_enforced(container_name, container_type):
    """snowflake.node drives the node field and is enforced (session-level SETs; the server's
    configured node is untouched):
      * node 0 (unset): nextval() fails with the product's own error and issues no id;
      * node 1024: rejected by the bounds the library defines for the GUC (a GUC placeholder, i.e.
        the library not loaded, would accept it);
      * another valid node: the next id carries that node."""
    node = configured_node()
    other = node % SNOWFLAKE_NODE_MAX + 1          # a different valid node
    container = get_running_container(container_name.strip())
    config = get_container_config(container_type)
    pgbin, pguser = config["pgbin"], config["pguser"]

    print(f"\n--- Checking that snowflake.node is enforced on {container_name} ---")

    exit_code, lines = psql_session(container, pgbin, pguser, "CREATE SEQUENCE pep_sf_node_seq",
                                    "SET snowflake.node = 0",
                                    "SELECT snowflake.nextval('pep_sf_node_seq'::regclass)")
    assert exit_code != 0 and any(f"ERROR:  {NODE_NOT_SET_ERROR}" in line for line in lines), (
        f"nextval() with snowflake.node = 0 did not fail with {NODE_NOT_SET_ERROR!r}: {lines}")
    exit_code, lines = psql_session(
        container, pgbin, pguser,
        "SELECT last_value IS NULL FROM pg_sequences "
        "WHERE schemaname = 'public' AND sequencename = 'pep_sf_node_seq'")
    assert exit_code == 0 and lines == ["t"], f"a failed nextval() issued an id: {lines}"
    print(f"✅ snowflake.node = 0 is rejected: {NODE_NOT_SET_ERROR}")

    exit_code, lines = psql_session(container, pgbin, pguser, "SET snowflake.node = 1024")
    assert exit_code != 0 and any(NODE_RANGE_ERROR in line for line in lines), (
        f"SET snowflake.node = 1024 was not rejected with {NODE_RANGE_ERROR!r}: {lines}")
    print("✅ snowflake.node = 1024 is out of range")

    exit_code, lines = psql_session(
        container, pgbin, pguser, f"SET snowflake.node = {other}",
        "SELECT snowflake.get_node(snowflake.nextval('pep_sf_node_seq'::regclass))")
    assert exit_code == 0 and lines == [str(other)], (
        f"an id drawn with snowflake.node = {other} carries node {lines}")
    print(f"✅ an id drawn with snowflake.node = {other} carries node {other}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_sequence_table_creation(container_name, container_type):
    """Test Case 3: Create sequence, table with snowflake.nextval(), and insert data"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 3: Creating employees table with Snowflake sequence on {container_name} ---")

    # Create sequence
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "CREATE SEQUENCE employee_id_seq;")
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

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, create_table_sql)
    assert exit_code == 0, f"Failed to create employees table: {output}"
    print("✅ Created employees table with snowflake.nextval default")

    # Insert test data
    insert_sql = """INSERT INTO employees (name, department, salary) VALUES 
        ('John Doe', 'Engineering', 75000),
        ('Jane Smith', 'Marketing', 65000),
        ('Bob Wilson', 'Sales', 55000);"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, insert_sql)
    assert exit_code == 0, f"Failed to insert data: {output}"
    print("✅ Inserted 3 employees")

    # Verify data and check for snowflake IDs
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT * FROM employees;")
    assert exit_code == 0, f"Failed to select from employees: {output}"

    print(f"Employees table data:\n{output}")

    # Verify we have 3 rows
    assert "John Doe" in output, "John Doe not found in employees"
    assert "Jane Smith" in output, "Jane Smith not found in employees"
    assert "Bob Wilson" in output, "Bob Wilson not found in employees"

    print(f"✅ Employees table created and populated with Snowflake IDs")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_multiple_sequences(container_name, container_type):
    """Test Case 4: Create another sequence/table and verify that each table draws its ids from its
    own sequence: per table, the sequence's last_value is the table's newest id (snowflake.c stores
    every returned id as the sequence's last_value), the row count is the number inserted, and
    every id carries the configured node."""
    node = configured_node()
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 4: Creating products table with different Snowflake sequence on {container_name} ---")

    # Create sequence
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "CREATE SEQUENCE products_id_seq;")
    assert exit_code == 0, f"Failed to create products sequence: {output}"
    print("✅ Created products_id_seq sequence")

    # Create products table
    create_table_sql = """CREATE TABLE products (
        product_id BIGINT DEFAULT snowflake.nextval('products_id_seq'::regclass) PRIMARY KEY,
        name TEXT,
        description TEXT,
        price DECIMAL(10,2)
    );"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, create_table_sql)
    assert exit_code == 0, f"Failed to create products table: {output}"
    print("✅ Created products table")

    # Insert test data
    insert_sql = """INSERT INTO products (name, description, price) VALUES
        ('Laptop', 'Computer', 999.99),
        ('Mouse', 'Computer Accessories', 29.99);"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, insert_sql)
    assert exit_code == 0, f"Failed to insert products: {output}"
    print("✅ Inserted 2 products")

    # Verify data
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT * FROM products;")
    assert exit_code == 0, f"Failed to select from products: {output}"

    print(f"Products table data:\n{output}")

    # Verify we have the products
    assert "Laptop" in output, "Laptop not found in products"
    assert "Mouse" in output, "Mouse not found in products"

    # Each table's ids come from its own sequence (ids from two sequences are not compared with
    # each other: separate sequences can legitimately issue equal ids in the same millisecond).
    for table, column, seq, rows in (("employees", "id", "employee_id_seq", 3),
                                     ("products", "product_id", "products_id_seq", 2)):
        exit_code, lines = psql_session(
            container, pgbin, pguser,
            f"SELECT (SELECT last_value FROM {seq}), max({column}), count(*), "
            f"count(*) FILTER (WHERE snowflake.get_node({column}) = {node}) FROM {table}")
        assert exit_code == 0, f"Failed to read {table} ids: {lines}"
        fields = _int_fields(lines[0], 4) if len(lines) == 1 else None
        assert fields is not None, f"unexpected {table} id summary: {lines}"
        last_value, newest, count, on_node = fields
        assert count == rows, f"{table} has {count} rows, expected {rows}"
        assert last_value == newest, (
            f"{seq}.last_value is {last_value} but the newest {table}.{column} is {newest}: "
            f"{table} does not draw its ids from {seq}")
        assert on_node == rows, f"only {on_node} of {rows} {table} ids carry node {node}"
        print(f"✅ {table} ids come from {seq} (last_value {last_value}) and carry node {node}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_table_metadata(container_name, container_type):
    """Test Case 5: Review sequence definitions in table metadata"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

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


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_extension_objects(container_name, container_type):
    """Test Case 6: List all Snowflake extension objects"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

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

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, query)
    assert exit_code == 0, f"Failed to list extension objects: {output}"

    print(f"Snowflake extension objects:\n{output}")

    # Verify we have snowflake schema objects
    assert "snowflake" in output, "No snowflake schema objects found"

    print(f"✅ Snowflake extension objects listed successfully")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_schema_objects(container_name, container_type):
    """Test Case 7: List all objects in snowflake schema"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

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


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_functions(container_name, container_type):
    """Test Case 8: List all Snowflake functions"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]

    pguser = config["pguser"]

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


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_convert_existing_sequence(container_name, container_type):
    """Test Case 9: Convert existing sequence to Snowflake-compatible sequence"""
    container = client.containers.get(container_name.strip())
    assert container.status == "running"

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Test Case 9: Converting existing sequence to Snowflake on {container_name} ---")

    # Step 1: Create a simple sequence
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "CREATE SEQUENCE my_simple_seq;")
    assert exit_code == 0, f"Failed to create sequence: {output}"
    print("✅ Created my_simple_seq sequence")

    # Step 2: Create users table with regular sequence
    create_table_sql = """CREATE TABLE users (
        id INTEGER DEFAULT nextval('my_simple_seq') PRIMARY KEY,
        name TEXT,
        email TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    );"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, create_table_sql)
    assert exit_code == 0, f"Failed to create users table: {output}"
    print("✅ Created users table with regular sequence")

    # Step 3: Insert initial data with regular sequence
    insert_sql = """INSERT INTO users (name, email) VALUES 
        ('John Doe', 'john@example.com'),
        ('Jane Smith', 'jane@example.com'),
        ('Bob Wilson', 'bob@example.com');"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, insert_sql)
    assert exit_code == 0, f"Failed to insert initial users: {output}"
    print("✅ Inserted 3 users with regular sequence IDs")

    # Step 4: Verify initial data (should have IDs 1, 2, 3)
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT * FROM users;")
    assert exit_code == 0, f"Failed to select initial users: {output}"
    print(f"Initial users data (regular sequence):\n{output}")

    # Verify regular sequence IDs (small integers)
    assert " 1 |" in output, "ID 1 not found"
    assert " 2 |" in output, "ID 2 not found"
    assert " 3 |" in output, "ID 3 not found"

    # Step 5: Convert sequence to Snowflake. The product's convert_sequence_to_snowflake() (the
    # definition in snowflake--2.4--2.5.0.sql, unchanged since) handles a plain sequence used by an
    # explicit nextval() column default by changing that column to int8 and its default to
    # snowflake.nextval(), and returns the number of those changes: 2 here. It then sets the sequence
    # to AS bigint NO CYCLE MAXVALUE 9223372036854775807 (before 2.5.0 it set MAXVALUE to
    # last_value + 1, which broke dump/restore).
    print("\n--- Converting sequence to Snowflake ---")
    exit_code, lines = psql_session(container, pgbin, pguser, "SET client_min_messages = warning",
                                    "SELECT snowflake.convert_sequence_to_snowflake('my_simple_seq'::regclass)")
    assert exit_code == 0, f"Failed to convert sequence: {lines}"
    print(f"Conversion output: {lines}")
    assert lines == ["2"], (
        f"convert_sequence_to_snowflake() reported {lines}, expected 2 changes "
        f"(users.id to int8 and its default to snowflake.nextval)")

    exit_code, lines = psql_session(
        container, pgbin, pguser, "SET search_path = public",
        "SELECT format_type(a.atttypid, a.atttypmod), pg_get_expr(d.adbin, d.adrelid), "
        "s.data_type, s.max_value FROM pg_attribute a "
        "JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "JOIN pg_sequences s ON s.schemaname = 'public' AND s.sequencename = 'my_simple_seq' "
        "WHERE a.attrelid = 'public.users'::regclass AND a.attname = 'id'")
    assert exit_code == 0, f"Failed to read the converted column and sequence: {lines}"
    assert lines == ["bigint|snowflake.nextval('my_simple_seq'::regclass)|bigint|9223372036854775807"], (
        f"after conversion users.id (type|default) and my_simple_seq (type|max_value) are {lines}")
    print("✅ Sequence converted to Snowflake")

    # Step 6: Verify existing data is unchanged
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT * FROM users ORDER BY id;")
    assert exit_code == 0, f"Failed to select users after conversion: {output}"
    print(f"Users data after conversion (existing rows unchanged):\n{output}")

    # Step 7: Insert new data with Snowflake sequence
    insert_new_sql = """INSERT INTO users (name, email) VALUES 
        ('Alice Johnson', 'alice@example.com'),
        ('Charlie Brown', 'charlie@example.com'),
        ('Diana Prince', 'diana@example.com');"""

    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, insert_new_sql)
    assert exit_code == 0, f"Failed to insert new users: {output}"
    print("✅ Inserted 3 more users with Snowflake sequence IDs")

    # Step 8: Verify mixed data (old regular IDs + new Snowflake IDs)
    exit_code, output = pg_server_management.execute_psql_query(container, pgbin, pgport, pguser, "SELECT * FROM users ORDER BY id;")
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

    # Exactly: the original rows keep ids 1, 2, 3 and the rows inserted after the conversion get
    # snowflake ids that carry the configured node and increase in insertion order.
    node = configured_node()
    exit_code, lines = psql_session(container, pgbin, pguser, "SELECT name, id FROM users ORDER BY id")
    assert exit_code == 0, f"Failed to read users: {lines}"
    ids = {}
    for line in lines:
        name, _, value = line.rpartition("|")
        assert re.fullmatch(r"[0-9]+", value), f"unexpected users row: {line!r}"
        ids[name] = int(value)
    original = {"John Doe": 1, "Jane Smith": 2, "Bob Wilson": 3}
    added = ["Alice Johnson", "Charlie Brown", "Diana Prince"]
    assert set(ids) == set(original) | set(added), f"unexpected users: {sorted(ids)}"
    assert {n: ids[n] for n in original} == original, f"original user ids changed: {ids}"
    new_ids = [ids[n] for n in added]
    assert all(decode_snowflake(i)[1] == node for i in new_ids), (
        f"ids after conversion {new_ids} do not all carry node {node}")
    assert 3 < new_ids[0] < new_ids[1] < new_ids[2], (
        f"ids after conversion do not increase in insertion order: {new_ids}")

    print(f"✅ Successfully converted existing sequence to Snowflake sequence")
    print(f"✅ Old records retained original IDs, new records have Snowflake IDs")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_stop_server(container_name, container_type):
    """Stop PostgreSQL server using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = get_running_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Stopping PostgreSQL server on {container_name} ---")

    # Use the pg_server_management module to stop the server
    try:
        success, server_output, message = pg_server_management.stop_server(
            container, pgbin, pgdata, pgport, pguser
        )
        assert success, f"Server stop failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to stop PostgreSQL server: {str(e)}")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_snowflake_uninstall(container_name, container_type):
    """Uninstall snowflake package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    snowflake_package = config["snowflake_package"]

    container = get_running_container(container_name)

    print(f"\n--- Uninstalling {snowflake_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, snowflake_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {snowflake_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Full cleanup using machine_cleanup module: remove all pgedge packages + leftover data"""
    if skip_cleanup:
        pytest.skip("Skipping cleanup: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = get_running_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")

    # Use the machine_cleanup module to perform comprehensive cleanup
    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata=pgdata, pguser=pguser
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        # Display cleanup details
        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
        if cleanup_summary["data_directory_removed"]:
            print(f"   Data directory removed: {pgdata}")
        if cleanup_summary["user_removed"]:
            print(f"   User removed: {pguser}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")
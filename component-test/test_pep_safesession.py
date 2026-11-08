"""Component tests for SafeSession.

A coupled component — one package per platform, tied to the PostgreSQL major
version:

    RHEL: pgedge-safesession_{PG}
    DEB:  pgedge-postgresql-{PG}-safesession

It ships no standalone binary; the payload is the pgedge_safesession.so shared
library plus the pgedge_safesession extension. Several paths differ between the
packaging formats and are therefore resolved per platform rather than derived:

  * LICENSE — RHEL: /usr/share/licenses/<pkg>/LICENCE.md  (British spelling)
              DEB:  /usr/share/doc/<pkg>/copyright        (Debian convention)
  * README  — same basename on both, different parent directory
  * SBOM    — <pg_path>/sbom/pgedge-safesession-sbom.json

In a standalone run the package version and the extension's default_version are
read from separate env vars (PGEDGE_SAFESESSION_<PG>_VERSION and
SAFESESSION_EXTENSION_VERSION) because they are independent values.

Integration mode (PEP_INTEGRATION_MODE=1, as in test_pep_spock.py): the request built
from the PEP_* environment decides the package, family, PG major and channel, and every
expectation comes from it, never from configuration. The PostgreSQL server goes in first,
then exactly the requested package (pinned, with checksum evidence); test_safesession_identity
proves the package version, and test_extension_version proves the running server's library
belongs to the requested package, the server runs the requested major, the library is
preloaded and the extension version is the release's (shared steps in
aspects/pipeline/pep_extension_checks.py). A missing container, expected-output list or other
input fails instead of skipping.

Both modes: test_safesession_enforcement proves the preloaded extension enforces read-only
sessions for a restricted role and a member of it, while a control role with identical
privileges and a superuser can still write.
"""
import importlib.util as _ilu
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
from aspects import configure_repository, package_management, pg_server_management, machine_cleanup, machine_prereq_setup, file_management, container_management


def _load_util(name):
    """Load a shared integration helper from aspects/pipeline/ by path (no package
    __init__ there), so these imports work however this file is loaded."""
    spec = _ilu.spec_from_file_location(
        name, os.path.join(os.path.dirname(__file__), '..', 'aspects', 'pipeline', name + '.py'))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The shared PG-coupled extension steps, and through them ONE instance each of the
# integration-mode install/identity steps, the request contract and the package-matching rules.
pep_extension_checks = _load_util('pep_extension_checks')
pep_integration_mode = pep_extension_checks.pep_integration_mode
pep_request_env = pep_integration_mode.pep_request_env
_pr = pep_request_env._pr
_pp = _pr._pp

load_dotenv()
client = docker.from_env()

# Integration request built from the PEP_* env contract (validated by normalize_request, which
# already binds the safesession package to its family and PG major). None in standalone mode,
# which keeps every standalone code path below unchanged.
INTEGRATION_REQUEST = pep_request_env.build_request_from_env()

# Product facts (the package's files and the extension itself), never configuration.
SAFESESSION_COMPONENT = "safesession"
SAFESESSION_EXTNAME = "pgedge_safesession"        # pgedge_safesession.control
SAFESESSION_PRELOAD = "pgedge_safesession"        # module_pathname '$libdir/pgedge_safesession'
SAFESESSION_LIBRARY = "pgedge_safesession.so"
SAFESESSION_SBOM = "pgedge-safesession-sbom.json"

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

# Common configuration. In integration mode the channel is the request's.
repo = INTEGRATION_REQUEST["channel"] if INTEGRATION_REQUEST is not None else os.getenv("REPO", "release")
upgrade_repo = os.getenv("UPGRADE_REPO", "staging")
skip_cleanup = os.getenv("SKIP_CLEANUP", "false").lower() == "true"
pgport = os.getenv("PG_PORT", "5432")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "18")
safesession_version = os.getenv(f"PGEDGE_SAFESESSION_{pg_major_version}_VERSION", "1.0")

# Extension version as reported by \dx — this is the .control file's
# default_version and need not equal the package version.
safesession_extension_version = os.getenv("SAFESESSION_EXTENSION_VERSION", "1.0")

# SafeSession ships no standalone binary, so the binary tests stay disabled.
component_binary = os.getenv("COMPONENT_BINARY", "")
component_version = os.getenv("COMPONENT_BINARY_VERSION", "")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")
rhel_safesession_package = os.getenv("SAFESESSION_PACKAGE", f"pgedge-safesession_{pg_major_version}")
rhel_safesession_lib = os.getenv("RHEL_SAFESESSION_LIB", f"/usr/pgsql-{pg_major_version}/lib/")

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_safesession_package = os.getenv("DEB_SAFESESSION_PACKAGE", f"pgedge-postgresql-{pg_major_version}-safesession")
deb_safesession_lib = os.getenv("DEB_SAFESESSION_LIB", f"/usr/lib/postgresql/{pg_major_version}/lib/")

# Shared library shipped by the package
safesession_stripped_lib = os.getenv("SAFESESSION_STRIPPED_LIB", "pgedge_safesession.so")

# SBOM basename, found under <pg_path>/sbom/
safesession_sbom = os.getenv("SAFESESSION_SBOM", "pgedge-safesession-sbom.json")

# Additional configuration for extension tests
check_extensions = os.getenv("CHECK_EXTENSIONS", "true").lower() == "true"
base_extensions = [
    ext.strip() for ext in os.getenv("SAFESESSION_EXTENSIONS", "pgedge_safesession").split(",")
    if ext.strip()
]

if INTEGRATION_REQUEST is not None:
    # Exactly the requested (validated) package, and the product's own file and extension names;
    # never the configured ones.
    rhel_safesession_package = deb_safesession_package = INTEGRATION_REQUEST["package_name"]
    safesession_stripped_lib = SAFESESSION_LIBRARY
    safesession_sbom = SAFESESSION_SBOM
    base_extensions = [SAFESESSION_EXTNAME]

# LICENSE / README paths. Derived here rather than configured, because each
# path follows its packaging format's own fixed convention:
#   RHEL license: /usr/share/licenses/<pkg>/LICENCE.md  (British spelling, as
#                 actually shipped — not the usual LICENSE.md)
#   DEB license:  /usr/share/doc/<pkg>/copyright        (Debian policy)
#   README:       /usr/share/doc/<pkg>/README.md on both platforms
rhel_license_path = f"/usr/share/licenses/{rhel_safesession_package}/LICENCE.md"
deb_license_path = f"/usr/share/doc/{deb_safesession_package}/copyright"
rhel_readme_path = f"/usr/share/doc/{rhel_safesession_package}/README.md"
deb_readme_path = f"/usr/share/doc/{deb_safesession_package}/README.md"


def _integration_problem(request, pg_major_env):
    """Why an integration request cannot be served by this test (None if it can). Checked again
    before anything is installed; normalize_request has already validated the request itself."""
    if request is None:
        return None
    policy = _pr.COMPONENT_POLICY.get(request["component"])
    if request["component"] != SAFESESSION_COMPONENT or not _pp.is_coupled(policy):
        return f"component {request['component']!r} is not the PG-coupled safesession component"
    if str(request["pg_major"]) != str(pg_major_env):
        return f"request PG{request['pg_major']} disagrees with PG_MAJOR_VERSION={pg_major_env}"
    why = _pp.classify(policy, request["package_name"], request["family"], request["pg_major"],
                       request["expected_version"])
    if why is not None:
        return (f"package {request['package_name']!r} is not safesession for {request['family']} "
                f"PG{request['pg_major']} ({why})")
    return None


INTEGRATION_PROBLEM = _integration_problem(INTEGRATION_REQUEST, pg_major_version)


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "pg_path": rhel_pg_path.rstrip('/'),
            "safesession_package": rhel_safesession_package,
            "lib_dir": rhel_safesession_lib.rstrip('/'),
            "license_path": rhel_license_path,
            "readme_path": rhel_readme_path,
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "pg_path": deb_pg_path.rstrip('/'),
            "safesession_package": deb_safesession_package,
            "lib_dir": deb_safesession_lib.rstrip('/'),
            "license_path": deb_license_path,
            "readme_path": deb_readme_path,
        }


def _get_container(container_name):
    """Fetch a running container. A missing container skips a standalone run and fails an
    integration run."""
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pep_extension_checks.skip_or_fail(
            INTEGRATION_REQUEST, f"Container {container_name} not found or not running.")
    assert container.status == "running"
    return container


def _psql_as(container, pgbin, role, sql):
    """``(exit_code, output)`` of ONE new psql session authenticated as ``role``, so ``role`` is
    the session user (what SafeSession checks), never a SET ROLE. The cluster from init_cluster
    uses initdb's default trust authentication for local connections, so a LOGIN role needs no
    password. stdout and stderr are combined: a rejection's ERROR and HINT lines are in it."""
    exit_code, output = container.exec_run(
        [f"{pgbin}/psql", "-X", "-q", "-h", "localhost", "-p", str(pgport), "-U", role,
         "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-t", "-A", "-c", sql],
        user="root")
    return exit_code, output.decode().strip()


def preload_problem(container, pgbin, pguser):
    """None if the running server preloads pgedge_safesession, otherwise why not. The product
    works only when preloaded (its _PG_init refuses any other load), so the setting must be in
    force, which needs a server restart."""
    exit_code, out = _psql_as(container, pgbin, pguser, "SHOW shared_preload_libraries;")
    if exit_code != 0:
        return f"SHOW shared_preload_libraries failed: {out}"
    libraries = [lib.strip().strip('"') for lib in out.split(",")]
    if SAFESESSION_PRELOAD not in libraries:
        return f"shared_preload_libraries is {out!r}; it does not include {SAFESESSION_PRELOAD!r}"
    return None


def verify_requested_safesession(container, container_type, config):
    """Integration mode: the server preloads pgedge_safesession, its library belongs to the
    requested package, it runs the requested PostgreSQL major, and the extension version is the
    request's release version. Safesession's release workflow requires only the numeric series of
    default_version to equal the release version (default_version may carry a pre-release marker
    such as 1.0-alpha1), so the series rule applies, not an exact match."""
    req = INTEGRATION_REQUEST
    problem = preload_problem(container, config["pgbin"], config["pguser"])
    assert problem is None, problem
    print(f"✅ shared_preload_libraries includes {SAFESESSION_PRELOAD}")
    pep_extension_checks.verify_requested_extension(
        container, container_type, req, ports=[pgport], pguser=config["pguser"],
        library=SAFESESSION_LIBRARY, extname=SAFESESSION_EXTNAME, pg_major=req["pg_major"],
        version_problem=pep_extension_checks.series_version(req["expected_version"]))


# ---------------------------------------------------------------------------- enforcement
# Test-local names (not configuration). The restricted role, its member and the control role get
# IDENTICAL privileges, so a rejection can only come from SafeSession; the superuser is also a
# member of the restricted role, so its exemption is the product's, not an omission.
SS_SCHEMA = "pep_safesession"
SS_TABLE = f"{SS_SCHEMA}.data"
SS_RESTRICTED = "pep_ss_restricted"          # listed in pgedge_safesession.roles
SS_MEMBER = "pep_ss_member"                  # member of SS_RESTRICTED
SS_CONTROL = "pep_ss_control"                # not listed
SS_SUPERUSER = "pep_ss_superuser"            # superuser, member of SS_RESTRICTED
SS_ROLES = (SS_RESTRICTED, SS_MEMBER, SS_CONTROL, SS_SUPERUSER)
SS_GRANTEES = f"{SS_RESTRICTED}, {SS_MEMBER}, {SS_CONTROL}"
# SafeSession's own texts (src/pgedge_safesession.c): safesession_reject() adds this hint to every
# rejection; INSERT is rejected by the ExecutorStart hook as "cannot execute INSERT in a read-only
# session" and CREATE TABLE by the utility hook's default branch as "cannot execute CREATE TABLE in
# a read-only session". Core's own read-only errors say "transaction" and carry no hint.
SS_HINT = "This session is restricted to read-only access by the pgedge_safesession extension."
SS_FINAL_ROWS = "1|setup\n2|setup\n20|control\n30|superuser"     # setup + the allowed writes only
SS_FINAL_TABLES = "by_control,data"                               # no by_restricted / by_member
SS_RELOAD_ATTEMPTS = 20


def rejection_problem(exit_code, output, command):
    """None if ``command`` was rejected BY SAFESESSION (its error for ``command`` and its hint);
    otherwise why not. Success, or a failure for another reason (a missing privilege, core's
    read-only transaction error, a failed connection), is a problem."""
    if exit_code == 0:
        return f"{command} succeeded: {output!r}"
    lines = [line.strip() for line in output.splitlines()]
    missing = [want for want in (f"ERROR:  cannot execute {command} in a read-only session",
                                 f"HINT:  {SS_HINT}") if want not in lines]
    if missing:
        return f"{command} failed, but not with SafeSession's rejection (missing {missing!r}): {output!r}"
    return None


def verify_enforcement(container, pgbin, pguser):
    """Configure pgedge_safesession.roles as the product documents (ALTER SYSTEM + reload) and
    prove, each statement in its own session authenticated as the acting role, that SafeSession
    blocks writes for the restricted role and its member and nobody else."""
    def run(role, sql):
        exit_code, out = _psql_as(container, pgbin, role, sql)
        print(f"   [{role}] {sql} -> exit {exit_code}: {out}")
        return exit_code, out

    def ok(role, sql):
        exit_code, out = run(role, sql)
        assert exit_code == 0, f"as {role}: {sql} failed: {out}"
        return out

    def rejected(role, sql, command):
        problem = rejection_problem(*run(role, sql), command)
        assert problem is None, f"as {role}: {problem}"

    def session(role, extra=""):
        return ok(role, f"SELECT session_user, pgedge_safesession_is_restricted(){extra};")

    problem = preload_problem(container, pgbin, pguser)
    assert problem is None, problem

    ok(pguser, f"DROP SCHEMA IF EXISTS {SS_SCHEMA} CASCADE; "
               f"DROP ROLE IF EXISTS {', '.join(SS_ROLES)}; "
               f"CREATE ROLE {SS_RESTRICTED} LOGIN; CREATE ROLE {SS_MEMBER} LOGIN; "
               f"CREATE ROLE {SS_CONTROL} LOGIN; CREATE ROLE {SS_SUPERUSER} LOGIN SUPERUSER; "
               f"GRANT {SS_RESTRICTED} TO {SS_MEMBER}, {SS_SUPERUSER}; "
               f"CREATE SCHEMA {SS_SCHEMA}; "
               f"GRANT USAGE, CREATE ON SCHEMA {SS_SCHEMA} TO {SS_GRANTEES}; "
               f"CREATE TABLE {SS_TABLE} (id int PRIMARY KEY, writer text NOT NULL); "
               f"GRANT ALL ON TABLE {SS_TABLE} TO {SS_GRANTEES}; "
               f"INSERT INTO {SS_TABLE} VALUES (1, 'setup'), (2, 'setup');")
    ok(pguser, f"ALTER SYSTEM SET pgedge_safesession.roles = '{SS_RESTRICTED}';")
    assert ok(pguser, "SELECT pg_reload_conf();") == "t", "pg_reload_conf() did not signal a reload"
    # The reload is asynchronous: wait until a NEW session sees the setting.
    seen = None
    for _ in range(SS_RELOAD_ATTEMPTS):
        seen = ok(pguser, "SHOW pgedge_safesession.roles;")
        if seen == SS_RESTRICTED:
            break
        time.sleep(0.5)
    assert seen == SS_RESTRICTED, (
        f"pgedge_safesession.roles is {seen!r} in a new session, not {SS_RESTRICTED!r}")
    print(f"✅ pgedge_safesession.roles = {SS_RESTRICTED} in a new session")

    # The restricted role reads, is restricted, and cannot write or create.
    got = session(SS_RESTRICTED, f", (SELECT count(*) FROM {SS_TABLE})")
    assert got == f"{SS_RESTRICTED}|t|2", f"restricted session: expected '{SS_RESTRICTED}|t|2', got {got!r}"
    rejected(SS_RESTRICTED, f"INSERT INTO {SS_TABLE} VALUES (10, 'restricted');", "INSERT")
    rejected(SS_RESTRICTED, f"CREATE TABLE {SS_SCHEMA}.by_restricted (id int);", "CREATE TABLE")
    print(f"✅ {SS_RESTRICTED}: SELECT allowed; INSERT and CREATE TABLE rejected by SafeSession")

    # A member of the restricted role is restricted too.
    got = session(SS_MEMBER)
    assert got == f"{SS_MEMBER}|t", f"member session: expected '{SS_MEMBER}|t', got {got!r}"
    rejected(SS_MEMBER, f"INSERT INTO {SS_TABLE} VALUES (11, 'member');", "INSERT")
    rejected(SS_MEMBER, f"CREATE TABLE {SS_SCHEMA}.by_member (id int);", "CREATE TABLE")
    print(f"✅ {SS_MEMBER}: INSERT and CREATE TABLE rejected by SafeSession")

    # The control role holds the same privileges and is not restricted, so the privileges suffice.
    got = session(SS_CONTROL)
    assert got == f"{SS_CONTROL}|f", f"control session: expected '{SS_CONTROL}|f', got {got!r}"
    ok(SS_CONTROL, f"INSERT INTO {SS_TABLE} VALUES (20, 'control');")
    ok(SS_CONTROL, f"CREATE TABLE {SS_SCHEMA}.by_control (id int);")
    print(f"✅ {SS_CONTROL}: INSERT and CREATE TABLE allowed")

    # A superuser is never restricted, even as a member of a restricted role.
    got = session(SS_SUPERUSER)
    assert got == f"{SS_SUPERUSER}|f", f"superuser session: expected '{SS_SUPERUSER}|f', got {got!r}"
    ok(SS_SUPERUSER, f"INSERT INTO {SS_TABLE} VALUES (30, 'superuser');")
    print(f"✅ {SS_SUPERUSER}: INSERT allowed")

    # Exactly the setup rows plus the allowed writes, and only the control role's table.
    rows = ok(pguser, f"SELECT id, writer FROM {SS_TABLE} ORDER BY id;")
    assert rows == SS_FINAL_ROWS, f"table contents: expected {SS_FINAL_ROWS!r}, got {rows!r}"
    tables = ok(pguser, f"SELECT string_agg(relname, ',' ORDER BY relname) FROM pg_class "
                        f"WHERE relnamespace = '{SS_SCHEMA}'::regnamespace AND relkind = 'r';")
    assert tables == SS_FINAL_TABLES, f"tables in {SS_SCHEMA}: expected {SS_FINAL_TABLES!r}, got {tables!r}"
    print(f"✅ data verified: {rows.count(chr(10)) + 1} rows; tables {tables}")


def enforcement_cleanup(container, pgbin, pguser):
    """Remove the setting, schema and roles the enforcement check created. Returns the problems
    (never raises), so a cleanup failure cannot hide the check's own result."""
    problems = []
    for sql in ("ALTER SYSTEM RESET pgedge_safesession.roles;",
                "SELECT pg_reload_conf();",
                f"DROP SCHEMA IF EXISTS {SS_SCHEMA} CASCADE; DROP ROLE IF EXISTS {', '.join(SS_ROLES)};"):
        exit_code, out = _psql_as(container, pgbin, pguser, sql)
        if exit_code != 0:
            problems.append(f"{sql} failed: {out}")
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

    container = _get_container(container_name)

    print(f"\n--- Configuring repository in {container_name} ---")

    try:
        success, platform, message = configure_repository.configure_pgedge_repository(container, repo)
        assert success, f"Repository configuration failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to configure repository: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_install(container_name, container_type):
    """Step 2: Install the safesession package using package_management module. In integration
    mode: the PostgreSQL server first, then exactly the requested package (pinned)."""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    safesession_package = config["safesession_package"]

    container = _get_container(container_name)

    if INTEGRATION_REQUEST is not None:
        # Execution-side backstop: never install for a request this test cannot serve.
        assert INTEGRATION_PROBLEM is None, f"integration request cannot be served: {INTEGRATION_PROBLEM}"
        pep_extension_checks.install_requested_extension(
            container, container_type, INTEGRATION_REQUEST, INTEGRATION_REQUEST["pg_major"], repo)
        return

    print(f"\n--- Installing {safesession_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.install_package(container, safesession_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {safesession_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_upgrade(container_name, container_type):
    """Upgrade the safesession package if UPGRADE=true"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: certification never upgrades; the requested package is pinned")
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    safesession_package = config["safesession_package"]

    container = _get_container(container_name)

    print(f"\n--- Upgrading {safesession_package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    try:
        success, platform, message = package_management.upgrade_package(container, safesession_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{safesession_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {safesession_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: test_safesession_identity checks the exact requested version")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not safesession_version:
        pytest.skip("No SAFESESSION version defined in env, skipping version check")

    config = get_container_config(container_type)
    safesession_package = config["safesession_package"]

    container = _get_container(container_name)

    print(f"\n--- Verifying {safesession_package} version on {container_name} ({container_type}) ---")

    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, safesession_package, safesession_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {safesession_package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_safesession_identity(container_name, container_type):
    """Integration mode: one consolidated identity verdict for the requested safesession package
    (install-before-identity, then the exact package-manager version; safesession has no binary
    to probe). The evidence is persisted before any failure. Skipped in standalone mode."""
    req = INTEGRATION_REQUEST
    if req is None:
        pytest.skip("integration mode only")

    container = _get_container(container_name.strip())

    ev, problems = pep_integration_mode.record_identity(container, req)
    if ev is None:
        pytest.fail("; ".join(problems))
    print(f"identity evidence ({req['package_name']}): {ev}")
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files match expected-output/{rpm,deb}/safesession.

    Both package names reduce to the short name 'safesession', so the platform
    directory alone distinguishes the two expected file lists.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    actual_package = config["safesession_package"]

    project_root = Path(__file__).parent.parent

    try:
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=actual_package,
            package_name=actual_package,
            project_root=project_root,
            pg_major_version=pg_major_version,
        )

        if not success:
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
        # No expected-output reference: a standalone run skips; an integration run fails, since
        # the verdict could otherwise pass without the requested package ever being compared.
        if "No expected file found" in str(e):
            pep_extension_checks.skip_or_fail(INTEGRATION_REQUEST, str(e))
        pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_shared_library(container_name, container_type):
    """Verify pgedge_safesession.so is present in the PostgreSQL lib directory.

    SafeSession ships no standalone binary, so the shared library is the only
    compiled artifact to check.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    lib_path = f"{config['lib_dir']}/{safesession_stripped_lib}"

    print(f"\n--- Verifying shared library {lib_path} on {container_name} ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"test -s {lib_path} && file {lib_path}"],
        user="root",
    )
    assert exit_code == 0, f"Shared library missing or empty at {lib_path}: {output.decode().strip()}"

    file_output = output.decode().strip()
    print(f"   {file_output}")
    assert "ELF" in file_output, f"{lib_path} is not an ELF shared object: {file_output}"
    print(f"✅ {lib_path} present")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_library_stripped(container_name, container_type):
    """Verify the shared library was stripped of debug symbols at build time. `file` prints
    "not stripped" for an unstripped library, so the shared strict check is used."""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    lib_path = f"{config['lib_dir']}/{safesession_stripped_lib}"

    print(f"\n--- Checking ELF strip status of {lib_path} on {container_name} ---")

    problem = pep_extension_checks.stripped_problem(container, lib_path)
    assert problem is None, problem
    print(f"✅ {lib_path} is stripped")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_license_file(container_name, container_type):
    """Verify the LICENSE file ships at the platform's expected path.

    RHEL uses /usr/share/licenses/<pkg>/LICENCE.md (British spelling as shipped),
    DEB uses Debian's /usr/share/doc/<pkg>/copyright.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    license_path = config["license_path"]
    package = config["safesession_package"]

    print(f"\n--- Verifying LICENSE for {package} at {license_path} on {container_name} ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"test -s {license_path} && echo PRESENT"],
        user="root",
    )
    assert exit_code == 0 and b"PRESENT" in output, (
        f"LICENSE file missing or empty for {package} at {license_path}: "
        f"{output.decode().strip()}"
    )
    print(f"✅ LICENSE present for {package} at {license_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_readme_file(container_name, container_type):
    """Verify the README ships at the platform's expected path."""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    readme_path = config["readme_path"]
    package = config["safesession_package"]

    print(f"\n--- Verifying README for {package} at {readme_path} on {container_name} ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"test -s {readme_path} && echo PRESENT"],
        user="root",
    )
    assert exit_code == 0 and b"PRESENT" in output, (
        f"README file missing or empty for {package} at {readme_path}: "
        f"{output.decode().strip()}"
    )
    print(f"✅ README present for {package} at {readme_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_sbom(container_name, container_type):
    """Verify the SafeSession SBOM's detached signature.

    The package ships pgedge-safesession-sbom.json plus a .asc signature under
    the PostgreSQL tree's sbom/ directory.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    sbom_dir = f"{config['pg_path']}/sbom"

    print(f"\n--- Verifying SBOM {sbom_dir}/{safesession_sbom} on {container_name} ({container_type}) ---")

    machine_prereq_setup.ensure_sq_installed(container)
    _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
    _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
    _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"

    if container_type == "rhel":
        exit_code, output = container.exec_run(
            f"wget -q -O {sbom_dir}/pgedge-rsa.pub https://dnf.pgedge.com/keys/pgedge-rsa.pub",
            user="root",
        )
        assert exit_code == 0, f"Failed to download pgedge-rsa.pub: {output.decode()}"
        signer = f"{sbom_dir}/pgedge-rsa.pub"
    else:
        # Debian containers already carry the key in the apt keyring
        signer = "/etc/apt/keyrings/pgedge-rsa.gpg"

    exit_code, output = container.exec_run(
        f"sh -c 'cd {sbom_dir} && sq verify "
        f"{_sq_signer_flag} {signer} "
        f"{_sq_sig_flag} {safesession_sbom}.asc "
        f"{safesession_sbom}'",
        user="root",
    )
    output_str = output.decode().replace('\xa0', ' ')
    assert exit_code == 0, f"SBOM verification failed: {output_str}"
    assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
        f"Expected a good/authenticated signature, got:\n{output_str}"
    print(f"✅ SBOM signature verified on {container_name} ({container_type})")
    print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_init_cluster(container_name, container_type):
    """Initialize a PostgreSQL cluster preloading pgedge_safesession"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Initializing cluster on {container_name} ---")

    guc_parameters = {
        "shared_preload_libraries": "'pgedge_safesession'",
    }

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

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Starting PostgreSQL server on {container_name} ---")

    try:
        success, server_output, message = pg_server_management.start_server(
            container, pgbin, pgdata, pgport, pguser
        )
        assert success, f"Server start failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        # Surface the server log — a bad shared_preload_libraries entry only
        # shows up there.
        _, log_output = container.exec_run(f"cat {pgdata}/logfile", user=pguser)
        pg_log = log_output.decode().strip() if log_output else "(log unavailable)"
        pytest.fail(
            f"Failed to start PostgreSQL server: {str(e)}\n\n"
            f"--- PostgreSQL log ({pgdata}/logfile) ---\n{pg_log}"
        )


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_check_connection(container_name, container_type):
    """Check PostgreSQL connection using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Checking PostgreSQL connection on {container_name} ---")

    try:
        success, version_output, message = pg_server_management.check_connection(
            container, pgbin, pgport, pguser
        )
        assert success, f"Connection check failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to check PostgreSQL connection: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
@pytest.mark.parametrize("extension", base_extensions)
def test_create_extensions(container_name, container_type, extension):
    """Create the pgedge_safesession extension"""
    if not check_extensions:
        pep_extension_checks.skip_or_fail(INTEGRATION_REQUEST, "Extension check disabled via env")

    container_name = container_name.strip()
    extension = extension.strip()

    if not container_name or not extension:
        pytest.skip("Invalid container or extension")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    # Normalize extension (quote if it contains a dash)
    normalized_ext = f'"{extension}"' if "-" in extension else extension

    print(f"\n--- Creating extension {normalized_ext} in {container_name} ---")

    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
        f"-c 'CREATE EXTENSION IF NOT EXISTS {normalized_ext} CASCADE;'",
        user=pguser,
    )

    assert exit_code == 0, f"Failed to create {normalized_ext}: {output.decode()}"
    print(f"✅ Successfully created extension {normalized_ext}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
@pytest.mark.parametrize("extension", base_extensions)
def test_extension_version(container_name, container_type, extension):
    """Verify the installed extension version matches the expected version via \\dx in psql.

    Runs psql \\dx and greps for the extension name, then checks that the
    reported version column equals SAFESESSION_EXTENSION_VERSION exactly — the
    .control file's default_version, which is tracked separately from the
    package version.

    Integration mode: instead, the binding of the running server to the request
    (verify_requested_safesession): preload, library owner, PostgreSQL major and
    the extension version by the release workflow's series rule.
    """
    if not check_extensions:
        pep_extension_checks.skip_or_fail(INTEGRATION_REQUEST, "Extension check disabled via env")

    container_name = container_name.strip()
    extension = extension.strip()

    if not container_name or not extension:
        pytest.skip("Invalid container or extension")

    if INTEGRATION_REQUEST is not None:
        container = _get_container(container_name)
        verify_requested_safesession(container, container_type, get_container_config(container_type))
        return

    if not safesession_extension_version:
        pytest.skip("No SAFESESSION_EXTENSION_VERSION defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Verifying extension version for '{extension}' on {container_name} ---")

    exit_code, output = container.exec_run(
        ["bash", "-c",
         f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -c '\\dx' | grep '{extension}'"],
        user=pguser,
    )
    assert exit_code == 0, (
        f"Failed to query extension '{extension}' version via \\dx: {output.decode().strip()}"
    )

    ext_line = output.decode().strip()
    print(f"   \\dx grep output: {ext_line}")

    assert ext_line, f"Extension '{extension}' not found in \\dx output"

    # Parse version from the table row: " name | version | schema | description "
    columns = [col.strip() for col in ext_line.split("|")]
    assert len(columns) >= 2, f"Unexpected \\dx row format: {ext_line}"
    installed_version = columns[1].strip()

    # Exact match, not a substring check. Both values name the same thing — the
    # .control file's default_version — so there is no formatting gap to absorb,
    # and a substring test would let a longer version slip through: with the GA
    # value of '1.0', "in" would happily pass against an installed '1.0.1'.
    expected_version = safesession_extension_version.strip()
    assert installed_version == expected_version, (
        f"Extension '{extension}' version mismatch: "
        f"expected '{expected_version}', got '{installed_version}'"
    )
    print(
        f"✅ Extension '{extension}' version {installed_version} "
        f"matches expected {safesession_extension_version}"
    )


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_safesession_enforcement(container_name, container_type):
    """Both modes: SafeSession enforces read-only sessions. With pgedge_safesession.roles set by
    ALTER SYSTEM + reload, sessions authenticated as the restricted role or a member of it read but
    cannot INSERT or CREATE TABLE (rejected with SafeSession's error and hint), while a control role
    with identical privileges and a superuser member can write; the data is then exactly the setup
    rows plus the allowed writes. The setting, schema and roles are removed afterwards."""
    if not check_extensions:
        pep_extension_checks.skip_or_fail(
            INTEGRATION_REQUEST, "Extension check disabled via env; enforcement needs the extension")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Verifying SafeSession enforcement on {container_name} ---")

    cleanup_problems = []
    try:
        verify_enforcement(container, pgbin, pguser)
    finally:
        cleanup_problems = enforcement_cleanup(container, pgbin, pguser)
        for problem in cleanup_problems:
            print(f"⚠️  cleanup: {problem}")
    assert not cleanup_problems, "enforcement cleanup failed: " + "; ".join(cleanup_problems)
    print(f"✅ SafeSession enforcement verified on {container_name}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_stop_server(container_name, container_type):
    """Stop PostgreSQL server using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Stopping PostgreSQL server on {container_name} ---")

    try:
        success, server_output, message = pg_server_management.stop_server(
            container, pgbin, pgdata, pgport, pguser
        )
        assert success, f"Server stop failed: {message}"
        print(f"✅ {message}")
    except Exception as e:
        pytest.fail(f"Failed to stop PostgreSQL server: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_package_uninstall(container_name, container_type):
    """Uninstall the safesession package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    safesession_package = config["safesession_package"]

    container = _get_container(container_name)

    print(f"\n--- Uninstalling {safesession_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.uninstall_package(container, safesession_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {safesession_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Full cleanup using machine_cleanup module: remove all pgedge packages + leftover data"""
    if skip_cleanup:
        pytest.skip("Skipping cleanup: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")

    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata=pgdata, pguser=pguser
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
        if cleanup_summary["data_directory_removed"]:
            print(f"   Data directory removed: {pgdata}")
        if cleanup_summary["user_removed"]:
            print(f"   User removed: {pguser}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")

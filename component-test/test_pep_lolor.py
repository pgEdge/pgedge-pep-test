import os
import re
import sys
import time
import importlib.util as _ilu
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


# The shared checks for PG-coupled extension tests and, through them, the integration-mode
# install/identity steps, the request contract and the shared package-matching rules. One
# instance of each, so a test that patches one reaches every step that uses it.
pep_extension_checks = _load_util('pep_extension_checks')
pep_integration_mode = pep_extension_checks.pep_integration_mode
pep_request_env = pep_integration_mode.pep_request_env
_pr = pep_request_env._pr
_pp = _pr._pp

load_dotenv()
client = docker.from_env()

# Integration request built from the PEP_* env contract (validated by normalize_request, which
# already binds the lolor package to its family and PG major). None in standalone mode.
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
pgport = os.getenv("PG_PORT", "5432")
pgdata = os.getenv("PG_DATA_DIR", "/tmp/n1")
pg_major_version = os.getenv("PG_MAJOR_VERSION", "16")
# Standalone only: in integration mode the request's version is the only expectation.
lolor_version = os.getenv(f"PGEDGE_LOLOR_{pg_major_version}_VERSION", "1.2.2")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# Server package configuration (needed for initdb/pg_ctl)
rhel_server_package = os.getenv("SERVER_PACKAGE", f"pgedge-postgresql{pg_major_version}-server")
deb_server_package = os.getenv("DEB_SERVER_PACKAGE", f"pgedge-postgresql-{pg_major_version}")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")
rhel_lolor_package = os.getenv("LOLOR_PACKAGE", f"pgedge-lolor_{pg_major_version}")
rhel_lolor_lib = os.getenv("RHEL_LOLOR_LIB", f"/usr/pgsql-{pg_major_version}/lib")
rhel_bundled_files = os.getenv(
    "LOLOR_BUNDLED_FILES",
    f"/usr/pgsql-{pg_major_version}/lib/lolor.so,/usr/pgsql-{pg_major_version}/share/extension/lolor--1.0--1.1.sql,/usr/pgsql-{pg_major_version}/share/extension/lolor--1.0.sql,/usr/pgsql-{pg_major_version}/share/extension/lolor--1.1--1.2.sql,/usr/pgsql-{pg_major_version}/share/extension/lolor--1.1.sql,/usr/pgsql-{pg_major_version}/share/extension/lolor--1.2.sql,/usr/pgsql-{pg_major_version}/share/extension/lolor.control"
).split(",")

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_lolor_package = os.getenv("DEB_LOLOR_PACKAGE", f"pgedge-postgresql-{pg_major_version}-lolor")
deb_lolor_lib = os.getenv("DEB_LOLOR_LIB", f"/usr/lib/postgresql/{pg_major_version}/lib")
deb_bundled_files = os.getenv(
    "DEB_LOLOR_BUNDLED_FILES",
    f"{deb_pg_path}/lib/lolor.so,{deb_pg_share_path}/extension/lolor--1.0--1.1.sql,{deb_pg_share_path}/extension/lolor--1.0.sql,{deb_pg_share_path}/extension/lolor--1.1--1.2.sql,{deb_pg_share_path}/extension/lolor--1.1.sql,{deb_pg_share_path}/extension/lolor--1.2.sql,{deb_pg_share_path}/extension/lolor.control"
).split(",")

# LOLOR stripped library names (comma-separated list)
lolor_stripped_lib = os.getenv("LOLOR_STRIPPED_LIB", "lolor.so")

# Additional configuration for extension tests. Integration mode always runs the product checks
# and creates only the extension it certifies.
check_extensions = (INTEGRATION_REQUEST is not None
                    or os.getenv("CHECK_EXTENSIONS", "true").lower() == "true")
base_extensions = (["lolor"] if INTEGRATION_REQUEST is not None else
                   [ext.strip() for ext in os.getenv("BASE_EXTENSIONS", "lolor").split(",") if ext.strip()])


def _integration_problem(request, pg_major_env):
    """Why an integration request cannot be served by this test (checked again before anything
    is installed), or None. The request is already validated; this backstop names the reason."""
    if request is None:
        return None
    policy = _pr.COMPONENT_POLICY.get(request["component"])
    if request["component"] != "lolor" or not _pp.is_coupled(policy):
        return f"component {request['component']!r} is not the PG-coupled lolor component"
    if str(request["pg_major"]) != str(pg_major_env):
        return f"request PG{request['pg_major']} disagrees with PG_MAJOR_VERSION={pg_major_env}"
    why = _pp.classify(policy, request["package_name"], request["family"], request["pg_major"],
                       request["expected_version"])
    if why is not None:
        return (f"package {request['package_name']!r} is not lolor for {request['family']} "
                f"PG{request['pg_major']} version {request['expected_version']!r} ({why})")
    return None


INTEGRATION_PROBLEM = _integration_problem(INTEGRATION_REQUEST, pg_major_version)

# ============================================================================
# lolor facts, from the source of the release line under test (lolor v1_STABLE, 1.2.x)
# ============================================================================
# Makefile: MODULE_big = lolor; lolor.control: module_pathname = '$libdir/lolor', schema = lolor.
LOLOR_EXTENSION = "lolor"
LOLOR_LIBRARY = "lolor.so"
LOLOR_MODULE_PATHNAME = "$libdir/lolor"
# lolor.node of each test cluster (a test-local value; no config setting exists for it). Its boot
# value 0 means "not set", and lolor then refuses to assign a large object OID
# (src/lolor.c:153-162, src/lolor_largeobject.c:222-226).
PRIMARY_NODE = 1          # n1, the cluster every check runs on (the publisher)
REPLICA_NODE = 2          # n2, the subscriber created by the replication check
SESSION_NODE = 15         # set per session, to show the OID follows the current setting
NODE_UNSET_ERROR = "value for lolor.node is not set"
# A new OID is (counter << 4) | lolor.node with the counter in 1 .. 2^28-1
# (src/lolor_largeobject.c:43-44, 239-243): its low 4 bits are the node.
NODE_BITS = 4
# lo_open modes (libpq-fs.h; the product's regression opens with x'60000' = both).
INV_WRITE = 0x20000
INV_READ = 0x40000
SEEK_SET, SEEK_END = 0, 2
# The pg_catalog large-object functions lolor replaces: CREATE EXTENSION renames each built-in to
# <name>_orig and creates <name> as the lolor C function named here (lolor--1.0.sql:22-180). DROP
# EXTENSION fires lolor's event trigger, which calls lolor.disable() (src/lolor.c:248-252) to
# rename lolor's functions to lolor_<name> and the built-ins back (lolor--1.2.1--1.2.2.sql:16-91);
# the extension then drops its own functions.
SWAPPED_FUNCTIONS = (
    ("lo_open", "oid,integer", "lolor_lo_open"),
    ("lo_close", "integer", "lolor_lo_close"),
    ("lo_creat", "integer", "lolor_lo_creat"),
    ("lo_create", "oid", "lolor_lo_create"),
    ("loread", "integer,integer", "lolor_loread"),
    ("lowrite", "integer,bytea", "lolor_lowrite"),
    ("lo_export", "oid,text", "lolor_lo_export"),
    ("lo_from_bytea", "oid,bytea", "lolor_lo_from_bytea"),
    ("lo_get", "oid", "lolor_lo_get"),
    ("lo_get", "oid,bigint,integer", "lolor_lo_get_fragment"),
    ("lo_import", "text", "lolor_lo_import"),
    ("lo_import", "text,oid", "lolor_lo_import_with_oid"),
    ("lo_lseek", "integer,integer,integer", "lolor_lo_lseek"),
    ("lo_lseek64", "integer,bigint,integer", "lolor_lo_lseek64"),
    ("lo_put", "oid,bigint,bytea", "lolor_lo_put"),
    ("lo_tell", "integer", "lolor_lo_tell"),
    ("lo_tell64", "integer", "lolor_lo_tell64"),
    ("lo_truncate", "integer,integer", "lolor_lo_truncate"),
    ("lo_truncate64", "integer,bigint", "lolor_lo_truncate64"),
    ("lo_unlink", "oid", "lolor_lo_unlink"),
)
# The tables lolor keeps large objects in instead of pg_catalog.pg_largeobject(_metadata), both
# with a primary key (lolor--1.0.sql:7-20); a replicated deployment publishes both
# (docs/install_configure.md:41-46).
LOLOR_TABLES = ("lolor.pg_largeobject", "lolor.pg_largeobject_metadata")

# Round-trip data: binary (every byte value, NULs included) and longer than two large-object pages
# (LOBLKSIZE = BLCKSZ/4), truncated to a length that is not a page multiple.
PAYLOAD = bytes((i * 37 + 11) % 256 for i in range(5000))
TRUNCATE_TO = 3000
EXPLICIT_OID = 200000     # low 4 bits 0, not PRIMARY_NODE: an explicit OID is kept as given

# Native PostgreSQL logical replication of lolor's tables to a second cluster (n2) in the same
# container, as test_pep_spock.py runs n1/n2. Not Spock.
REPLICA_PORT = str(int(pgport) + 1)
REPLICA_PGDATA = f"{pgdata.rstrip('/')}_lolor_subscriber"
PUBLICATION = "lolor_pep_pub"
SUBSCRIPTION = "lolor_pep_sub"
REPLICATION_PAYLOAD = bytes((i * 101 + 7) % 256 for i in range(4500))
REPLICATION_TIMEOUT_S = 90
REPLICATION_POLL_S = 2

PSQL_MARK = "lolor-check"

# Every SQL step is bounded, so a blocked connection, CREATE/DROP SUBSCRIPTION, poll or cleanup
# step fails within minutes instead of waiting for the job's own time limit (exec_run and pytest
# have none). psql runs under coreutils `timeout` (SIGTERM at the limit, SIGKILL 5 s later), which
# bounds the whole client including connection; PGCONNECT_TIMEOUT bounds connecting on its own;
# and statement_timeout (through PGOPTIONS) makes the server cancel a statement 5 s before the
# client limit, so a killed client leaves nothing running. The subscription's connection string
# carries connect_timeout for the same reason. pg_ctl start/stop already wait at most 60 s.
SQL_STEP_TIMEOUT_S = 60           # one script
SQL_POLL_TIMEOUT_S = 15           # one poll inside a bounded wait loop
PG_CONNECT_TIMEOUT_S = 10
TIMEOUT_EXIT = 124                # timeout(1)'s exit status when the limit was reached


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb). In integration mode the lolor
    package is exactly the requested (validated) package."""
    if container_type == "rhel":
        config = {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "lolor_package": rhel_lolor_package,
            "lolor_lib": rhel_lolor_lib,
            "bundled_files": rhel_bundled_files,
            "server_package": rhel_server_package
        }
    else:  # deb
        config = {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "lolor_package": deb_lolor_package,
            "lolor_lib": deb_lolor_lib,
            "bundled_files": deb_bundled_files,
            "server_package": deb_server_package
        }
    if INTEGRATION_REQUEST is not None:
        config["lolor_package"] = INTEGRATION_REQUEST["package_name"]
    return config


def lolor_guc_parameters(node):
    """postgresql.conf settings for a lolor test cluster on lolor node ``node``."""
    return {
        "shared_preload_libraries": "'lolor'",
        "wal_level": "logical",
        "max_replication_slots": "10",
        "max_wal_senders": "10",
        "lolor.node": str(node),
    }


def _running_container(container_name):
    """The named container, which must be running. A missing container skips a standalone run
    and fails in integration mode (a skipped check could otherwise leave the verdict green)."""
    name = container_name.strip()
    if not name:
        pep_extension_checks.skip_or_fail(INTEGRATION_REQUEST, "No container defined in env")
    try:
        container = client.containers.get(name)
    except docker.errors.NotFound:
        pep_extension_checks.skip_or_fail(INTEGRATION_REQUEST, f"Container {name} not found or not running.")
    assert container.status == "running", f"Container {name} is not running (status: {container.status})"
    return container


# ============================================================================
# psql helpers: every script stops at its first error and its results are read back as values
# ============================================================================
_LABEL_RE = re.compile(r"([a-z][a-z0-9_]*)=(.*)")


def run_sql(container, pgbin, port, pguser, sql, label, timeout_s=SQL_STEP_TIMEOUT_S):
    """``(exit_code, output)`` of ``sql`` run as ONE psql script on ``port``: no psqlrc, quiet,
    unaligned tuples only, and ON_ERROR_STOP, so the first failing statement ends the script with
    a non-zero exit. ``label`` names the check in the script and in failure messages. The script,
    including connecting, is bounded by ``timeout_s`` (see SQL_STEP_TIMEOUT_S); a script that hits
    the limit exits TIMEOUT_EXIT (or higher, if it had to be killed) and its output says so."""
    script = f"-- {PSQL_MARK}: {label}\n{sql}"
    statement_ms = max(timeout_s - 5, 1) * 1000
    command = (f"PGCONNECT_TIMEOUT={PG_CONNECT_TIMEOUT_S} PGOPTIONS='-c statement_timeout={statement_ms}' "
               f"timeout -k 5 {timeout_s} "
               f"{pgbin}/psql -X -q -t -A -v ON_ERROR_STOP=1 -h localhost -p {port} -U {pguser} "
               f"-d postgres <<'LOLOR_EOSQL'\n{script}\nLOLOR_EOSQL")
    exit_code, output = container.exec_run(["bash", "-c", command], user=pguser)
    text = output.decode(errors="replace")
    if exit_code in (TIMEOUT_EXIT, 128 + 9):        # timed out (128 + SIGKILL: killed after it)
        text += f"\n[{label}: timed out after {timeout_s}s on port {port}]"
    return exit_code, text


def labelled_values(output):
    """``{key: [value, ...]}`` from the ``key=value`` lines a script prints; other lines (notices,
    errors) are ignored."""
    values = {}
    for line in output.splitlines():
        match = _LABEL_RE.fullmatch(line.strip())
        if match:
            values.setdefault(match.group(1), []).append(match.group(2))
    return values


def sql_values(container, pgbin, port, pguser, sql, label):
    """Run ``sql`` (see run_sql) and return its labelled values. Any SQL error fails the check."""
    exit_code, output = run_sql(container, pgbin, port, pguser, sql, label)
    assert exit_code == 0, f"{label}: psql exited {exit_code} on port {port}:\n{output.strip()}"
    return labelled_values(output)


def _one(values, key):
    got = values.get(key) or []
    return got[0] if len(got) == 1 else None


def _show(value):
    if value is None:
        return "None"
    text = str(value)
    return repr(text) if len(text) <= 48 else f"{text[:32]!r}... ({len(text)} chars)"


def _expect(values, key, want, problems):
    got = _one(values, key)
    if got != str(want):
        problems.append(f"{key} = {_show(got)}, expected {_show(want)}")
    return got


def node_encoding_problem(text, node):
    """None if ``text`` is an OID lolor assigned on lolor node ``node``, else why not: lolor puts
    the node in the low NODE_BITS bits and a counter of at least 1 above them."""
    if text is None or not text.isdigit():
        return f"large object OID {_show(text)} is not an OID"
    oid = int(text)
    mask = (1 << NODE_BITS) - 1
    if oid >= 1 << 32:
        return f"large object OID {oid} is out of the OID range"
    if oid & mask != node:
        return (f"large object OID {oid} carries node {oid & mask} in its low {NODE_BITS} bits, "
                f"not lolor.node {node}")
    if oid >> NODE_BITS < 1:
        return f"large object OID {oid} has no counter above its node bits"
    return None


def function_state_sql():
    """The SQL that lists, as ``fn=<signature>|<language>|<probin>|<prosrc>`` lines, every
    function named like a large-object function lolor swaps, its built-in (<name>_orig) or its
    disabled form (lolor_<name>)."""
    names = sorted({name for name, _args, _symbol in SWAPPED_FUNCTIONS})
    candidates = names + [n + "_orig" for n in names] + ["lolor_" + n for n in names]
    in_list = ", ".join(f"'{n}'" for n in candidates)
    return ("SELECT 'fn=' || p.oid::regprocedure::text || '|' || l.lanname || '|' || "
            "coalesce(p.probin, '') || '|' || p.prosrc "
            "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_language l ON l.oid = p.prolang "
            f"WHERE p.proname IN ({in_list}) ORDER BY 1;")


def _owner(language, probin, prosrc):
    if probin == LOLOR_MODULE_PATHNAME:
        return f"lolor:{prosrc}"
    if language == "internal" and not probin:
        return "built-in"
    return f"other:{language}:{probin}:{prosrc}"


def function_state_problems(lines, enabled):
    """Problems with the observed large-object functions (``fn`` values) against the swap lolor
    performs: ``enabled`` (after CREATE EXTENSION) each function is lolor's C function and the
    built-in is kept as <name>_orig; otherwise (after DROP EXTENSION) each is the built-in again
    and no <name>_orig or lolor_<name> remains."""
    observed = set()
    problems = []
    for line in lines:
        parts = line.split("|", 3)
        if len(parts) != 4:
            problems.append(f"unparseable function row {line!r}")
            continue
        observed.add((parts[0], _owner(parts[1], parts[2], parts[3])))
    expected = set()
    for name, args, symbol in SWAPPED_FUNCTIONS:
        if enabled:
            expected.add((f"{name}({args})", f"lolor:{symbol}"))
            expected.add((f"{name}_orig({args})", "built-in"))
        else:
            expected.add((f"{name}({args})", "built-in"))
    for sig, owner in sorted(expected - observed):
        problems.append(f"missing {sig} as {owner}")
    for sig, owner in sorted(observed - expected):
        problems.append(f"unexpected {sig} as {owner}")
    return problems


def round_trip_sql():
    """One script: write, read, seek, tell and truncate a new large object through a descriptor
    inside ONE explicit transaction (descriptors live only until transaction end), then check
    where the data landed, read it back without a descriptor, unlink it, create one with an
    explicit OID, and create one under a second lolor.node set for this session."""
    core_totals = ("(SELECT count(*) FROM pg_catalog.pg_largeobject) || ',' || "
                   "(SELECT count(*) FROM pg_catalog.pg_largeobject_metadata)")
    lo = ":'loid'::oid"
    return "\n".join([
        "SELECT 'node=' || current_setting('lolor.node');",
        "SELECT 'lob_block=' || (current_setting('block_size')::int / 4);",
        f"SELECT 'core_totals_before=' || {core_totals};",
        "BEGIN;",
        "SELECT lo_creat(-1) AS loid \\gset",
        "SELECT 'loid=' || :'loid';",
        f"SELECT lo_open({lo}, {INV_READ | INV_WRITE}) AS fd \\gset",
        f"SELECT 'written=' || lowrite(:fd, decode('{PAYLOAD.hex()}', 'hex'));",
        "SELECT 'tell_after_write=' || lo_tell(:fd);",
        f"SELECT 'seek_start=' || lo_lseek(:fd, 0, {SEEK_SET});",
        f"SELECT 'read=' || encode(loread(:fd, {len(PAYLOAD) + 100}), 'hex');",
        "SELECT 'tell_after_read=' || lo_tell(:fd);",
        f"SELECT 'truncate=' || lo_truncate(:fd, {TRUNCATE_TO});",
        f"SELECT 'size_after_truncate=' || lo_lseek(:fd, 0, {SEEK_END});",
        "SELECT 'close=' || lo_close(:fd);",
        "COMMIT;",
        f"SELECT 'lolor_meta_rows=' || count(*) FROM lolor.pg_largeobject_metadata WHERE oid = {lo};",
        f"SELECT 'lolor_pages=' || count(*) FROM lolor.pg_largeobject WHERE loid = {lo};",
        "SELECT 'lolor_data=' || coalesce(encode(string_agg(data, ''::bytea ORDER BY pageno), 'hex'), '') "
        f"FROM lolor.pg_largeobject WHERE loid = {lo};",
        f"SELECT 'lo_get=' || encode(lo_get({lo}), 'hex');",
        f"SELECT 'core_rows_for_loid=' || (SELECT count(*) FROM pg_catalog.pg_largeobject WHERE loid = {lo}) "
        f"|| ',' || (SELECT count(*) FROM pg_catalog.pg_largeobject_metadata WHERE oid = {lo});",
        f"SELECT 'core_totals_after=' || {core_totals};",
        f"SELECT 'unlink=' || lo_unlink({lo});",
        f"SELECT 'rows_after_unlink=' || (SELECT count(*) FROM lolor.pg_largeobject_metadata WHERE oid = {lo}) "
        f"|| ',' || (SELECT count(*) FROM lolor.pg_largeobject WHERE loid = {lo});",
        f"SELECT 'explicit_oid=' || lo_create({EXPLICIT_OID});",
        f"SELECT 'explicit_unlink=' || lo_unlink({EXPLICIT_OID});",
        f"SET lolor.node = {SESSION_NODE};",
        "SELECT lo_creat(-1) AS session_loid \\gset",
        "SELECT 'session_node_oid=' || :'session_loid';",
        "SELECT 'session_unlink=' || lo_unlink(:'session_loid'::oid);",
    ])


def round_trip_problems(values):
    """Everything the round trip got wrong, as the v1_STABLE source defines it."""
    problems = []
    _expect(values, "node", PRIMARY_NODE, problems)
    why = node_encoding_problem(_one(values, "loid"), PRIMARY_NODE)
    if why:
        problems.append(why)
    _expect(values, "written", len(PAYLOAD), problems)
    _expect(values, "tell_after_write", len(PAYLOAD), problems)
    _expect(values, "seek_start", 0, problems)
    _expect(values, "read", PAYLOAD.hex(), problems)
    _expect(values, "tell_after_read", len(PAYLOAD), problems)
    _expect(values, "truncate", 0, problems)                    # lolor_fsstubs.c:621
    _expect(values, "size_after_truncate", TRUNCATE_TO, problems)
    _expect(values, "close", 0, problems)                       # lolor_fsstubs.c:179
    _expect(values, "lolor_meta_rows", 1, problems)
    lob_block = _one(values, "lob_block")
    if lob_block is None or not lob_block.isdigit() or int(lob_block) <= 0:
        problems.append(f"lob_block = {_show(lob_block)}, expected the large-object page size")
    else:
        _expect(values, "lolor_pages", -(-TRUNCATE_TO // int(lob_block)), problems)
    _expect(values, "lolor_data", PAYLOAD[:TRUNCATE_TO].hex(), problems)
    _expect(values, "lo_get", PAYLOAD[:TRUNCATE_TO].hex(), problems)
    _expect(values, "core_rows_for_loid", "0,0", problems)
    before, after = _one(values, "core_totals_before"), _one(values, "core_totals_after")
    if before is None or before != after:
        problems.append(f"pg_catalog large-object row counts changed from {_show(before)} to {_show(after)}")
    _expect(values, "unlink", 1, problems)                      # lolor_inv_api.c:367-368
    _expect(values, "rows_after_unlink", "0,0", problems)       # lolor_largeobject.c:117-150
    _expect(values, "explicit_oid", EXPLICIT_OID, problems)     # lolor_largeobject.c:71-72
    _expect(values, "explicit_unlink", 1, problems)
    why = node_encoding_problem(_one(values, "session_node_oid"), SESSION_NODE)
    if why:
        problems.append(why)
    _expect(values, "session_unlink", 1, problems)
    return problems


def replication_write_sql():
    """Write REPLICATION_PAYLOAD to a new large object on the publisher, through a descriptor in
    one transaction."""
    return "\n".join([
        "BEGIN;",
        "SELECT lo_creat(-1) AS loid \\gset",
        f"SELECT lo_open(:'loid'::oid, {INV_WRITE}) AS fd \\gset",
        f"SELECT 'written=' || lowrite(:fd, decode('{REPLICATION_PAYLOAD.hex()}', 'hex'));",
        "SELECT 'close=' || lo_close(:fd);",
        "COMMIT;",
        "SELECT 'loid=' || :'loid';",
    ])


def replication_write_problems(values):
    problems = []
    _expect(values, "written", len(REPLICATION_PAYLOAD), problems)
    _expect(values, "close", 0, problems)
    why = node_encoding_problem(_one(values, "loid"), PRIMARY_NODE)
    if why:
        problems.append(why)
    return problems


def replica_poll_sql(loid):
    lo = f"'{loid}'::oid"
    return "\n".join([
        f"SELECT 'rows=' || (SELECT count(*) FROM lolor.pg_largeobject_metadata WHERE oid = {lo}) "
        f"|| ',' || (SELECT count(*) FROM lolor.pg_largeobject WHERE loid = {lo});",
        "SELECT 'bytes=' || CASE WHEN EXISTS (SELECT 1 FROM lolor.pg_largeobject_metadata "
        f"WHERE oid = {lo}) THEN encode(lo_get({lo}), 'hex') ELSE '' END;",
    ])


def wait_for_replicated_large_object(container, pgbin, port, pguser, loid, payload):
    """Poll the subscriber on ``port`` until it reads exactly ``payload`` from large object
    ``loid`` through lolor, for at most REPLICATION_TIMEOUT_S seconds; fail with the last state
    seen otherwise."""
    want = payload.hex()
    deadline = time.monotonic() + REPLICATION_TIMEOUT_S
    last = "no answer yet"
    while True:
        exit_code, output = run_sql(container, pgbin, port, pguser, replica_poll_sql(loid),
                                    "poll the subscriber", timeout_s=SQL_POLL_TIMEOUT_S)
        if exit_code == 0:
            values = labelled_values(output)
            got = _one(values, "bytes")
            if got == want:
                return
            readable = len(got) // 2 if got else 0
            last = (f"metadata,page rows = {_show(_one(values, 'rows'))}; {readable} bytes readable"
                    + ("" if not got else f", not the {len(payload)} bytes written"))
        else:
            last = f"psql exited {exit_code}: {output.strip()[-300:]}"
        if time.monotonic() >= deadline:
            pytest.fail(
                f"large object {loid} written on the publisher (port {pgport}) was not readable with "
                f"identical bytes on the subscriber (port {port}) within {REPLICATION_TIMEOUT_S}s of "
                f"native logical replication of {', '.join(LOLOR_TABLES)}; last subscriber state: {last}")
        time.sleep(REPLICATION_POLL_S)


def subscription_sync_sql():
    """How many of the subscription's tables have finished their initial copy (state 'r', ready)."""
    return ("SELECT 'ready=' || count(*) FILTER (WHERE r.srsubstate = 'r') || '/' || count(*) "
            "FROM pg_catalog.pg_subscription_rel r JOIN pg_catalog.pg_subscription s ON s.oid = r.srsubid "
            f"WHERE s.subname = '{SUBSCRIPTION}';")


def wait_for_initial_sync(container, pgbin, port, pguser):
    """Poll the subscriber on ``port`` until both lolor tables have finished their initial table
    sync (pg_subscription_rel state 'r'), for at most REPLICATION_TIMEOUT_S seconds. Only then is
    the large object written, so it can reach the subscriber only as a change the apply worker
    decodes from the publisher's WAL, never inside the initial COPY."""
    want = f"{len(LOLOR_TABLES)}/{len(LOLOR_TABLES)}"
    deadline = time.monotonic() + REPLICATION_TIMEOUT_S
    last = "no answer yet"
    while True:
        exit_code, output = run_sql(container, pgbin, port, pguser, subscription_sync_sql(),
                                    "wait for the initial table sync", timeout_s=SQL_POLL_TIMEOUT_S)
        if exit_code == 0:
            got = _one(labelled_values(output), "ready")
            if got == want:
                return
            last = f"tables ready/total = {_show(got)}"
        else:
            last = f"psql exited {exit_code}: {output.strip()[-300:]}"
        if time.monotonic() >= deadline:
            pytest.fail(
                f"subscription {SUBSCRIPTION} on port {port} did not finish the initial sync of "
                f"{', '.join(LOLOR_TABLES)} within {REPLICATION_TIMEOUT_S}s ({last}); nothing was written")
        time.sleep(REPLICATION_POLL_S)


REPLICA_LOG_LINES = 40


def subscriber_log_tail(container):
    """The last REPLICA_LOG_LINES lines of the subscriber's server log, where the apply and
    table-sync workers report their errors (start_server logs to <pgdata>/logfile). Read on failure,
    before the cluster is removed. It is a throwaway test cluster's log using trust authentication,
    so it holds no credentials."""
    exit_code, output = container.exec_run(
        ["tail", "-n", str(REPLICA_LOG_LINES), f"{REPLICA_PGDATA}/logfile"], user="root")
    text = output.decode(errors="replace").strip()
    return text if exit_code == 0 else f"(subscriber log unavailable: {text})"


def replica_read_sql(loid, length):
    lo = f"'{loid}'::oid"
    return "\n".join([
        "BEGIN;",
        f"SELECT lo_open({lo}, {INV_READ}) AS fd \\gset",
        f"SELECT 'read=' || encode(loread(:fd, {length + 100}), 'hex');",
        "SELECT 'close=' || lo_close(:fd);",
        "COMMIT;",
        f"SELECT 'core_rows_for_loid=' || (SELECT count(*) FROM pg_catalog.pg_largeobject WHERE loid = {lo}) "
        f"|| ',' || (SELECT count(*) FROM pg_catalog.pg_largeobject_metadata WHERE oid = {lo});",
        "SELECT 'node=' || current_setting('lolor.node');",
    ])


def replica_read_problems(values):
    problems = []
    _expect(values, "read", REPLICATION_PAYLOAD.hex(), problems)
    _expect(values, "close", 0, problems)
    _expect(values, "core_rows_for_loid", "0,0", problems)
    _expect(values, "node", REPLICA_NODE, problems)
    return problems


def cleanup_replication(container, pgbin, pguser, replica_started):
    """Drop the subscription (which drops its slot on the publisher), stop and remove the
    subscriber cluster, and drop the publication. Every step is attempted; never raises; returns
    what failed."""
    problems = []

    def attempt(what, step):
        try:
            exit_code, output = step()
        except Exception as e:
            exit_code, output = None, str(e)
        if exit_code != 0:
            text = output.decode(errors="replace") if isinstance(output, bytes) else str(output)
            problems.append(f"{what} failed: {text.strip()}")

    def stop_replica():
        success, _output, message = pg_server_management.stop_server(
            container, pgbin, REPLICA_PGDATA, REPLICA_PORT, pguser)
        return (0 if success else 1), message

    if replica_started:
        attempt(f"DROP SUBSCRIPTION on port {REPLICA_PORT}",
                lambda: run_sql(container, pgbin, REPLICA_PORT, pguser,
                                f"DROP SUBSCRIPTION IF EXISTS {SUBSCRIPTION};", "drop the subscription"))
        attempt(f"stopping the subscriber cluster on port {REPLICA_PORT}", stop_replica)
    attempt(f"removing {REPLICA_PGDATA}",
            lambda: container.exec_run(["rm", "-rf", REPLICA_PGDATA], user="root"))
    attempt(f"DROP PUBLICATION on port {pgport}",
            lambda: run_sql(container, pgbin, pgport, pguser,
                            f"DROP PUBLICATION IF EXISTS {PUBLICATION};", "drop the publication"))
    return problems


def verify_binding(container, container_type, config, ports):
    """Integration mode: the running server's lolor.so belongs to the requested package, each
    port runs the requested PostgreSQL major, and the lolor extension version is exactly the
    requested release version. lolor's release workflow fails unless the tag version equals
    lolor.control default_version (release.yml:184-203), which is the version CREATE EXTENSION
    installs."""
    req = INTEGRATION_REQUEST
    return pep_extension_checks.verify_requested_extension(
        container, container_type, req, ports=ports, pguser=config["pguser"],
        library=LOLOR_LIBRARY, extname=LOLOR_EXTENSION, pg_major=req["pg_major"],
        version_problem=pep_extension_checks.exact_version(req["expected_version"]))


def lolor_library_paths(container, container_type, package):
    """Every lolor.so the package manager says ``package`` ships."""
    cmd = ["rpm", "-ql", package] if container_type == "rhel" else ["dpkg", "-L", package]
    exit_code, output = container.exec_run(cmd, user="root")
    text = output.decode(errors="replace").strip()
    assert exit_code == 0, f"listing the files of {package} failed: {text}"
    return sorted({line.strip() for line in text.splitlines()
                   if os.path.basename(line.strip()) == LOLOR_LIBRARY})


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
    container = _running_container(container_name)

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
    """Step 2: Install pgedge-lolor. In integration mode: the PostgreSQL server for the requested
    major first, then exactly the requested package, pinned (with checksum evidence)."""
    container = _running_container(container_name)

    if INTEGRATION_REQUEST is not None:
        # Execution-side backstop: never install for a request the shared lolor rules reject.
        assert INTEGRATION_PROBLEM is None, f"integration request cannot be served: {INTEGRATION_PROBLEM}"
        pep_extension_checks.install_requested_extension(
            container, container_type, INTEGRATION_REQUEST, pg_major_version, repo)
        return

    # Get container-specific configuration
    config = get_container_config(container_type)
    lolor_package = config["lolor_package"]

    print(f"\n--- Installing {lolor_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to install the package
    try:
        success, platform, message = package_management.install_package(container, lolor_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {lolor_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_upgrade(container_name, container_type):
    """Upgrade component package if UPGRADE=true"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: the requested package is installed pinned and never upgraded")
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container = _running_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    lolor_package = config["lolor_package"]

    print(f"\n--- Upgrading {lolor_package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    # Use the package_management module to upgrade the package
    try:
        success, platform, message = package_management.upgrade_package(container, lolor_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{lolor_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {lolor_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: test_lolor_identity checks the exact requested version")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not lolor_version:
        pytest.skip("No LOLOR_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    lolor_package = config["lolor_package"]

    container = _running_container(container_name)

    print(f"\n--- Verifying {lolor_package} version on {container_name} ({container_type}) ---")

    # Use the package_management module to verify the package version
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, lolor_package, lolor_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {lolor_package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_lolor_identity(container_name, container_type):
    """Integration mode: one consolidated identity verdict for the requested lolor package
    (install-before-identity, then the exact package-manager version; lolor has no binary to
    probe). The evidence is persisted before any failure. Skipped in standalone mode."""
    req = INTEGRATION_REQUEST
    if req is None:
        pytest.skip("integration mode only")

    container = _running_container(container_name)

    ev, problems = pep_integration_mode.record_identity(container, req)
    if ev is None:
        pytest.fail("; ".join(problems))
    print(f"identity evidence ({req['package_name']}): {ev}")
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_binaries_stripped(container_name, container_type):
    """Step 4: every lolor library is a stripped ELF file. In integration mode the libraries are
    the ones the requested package ships."""
    container = _running_container(container_name)

    if INTEGRATION_REQUEST is not None:
        package = INTEGRATION_REQUEST["package_name"]
        paths = lolor_library_paths(container, container_type, package)
        assert paths, f"{package} ships no {LOLOR_LIBRARY}"
    else:
        # Get container-specific configuration
        config = get_container_config(container_type)
        lolor_lib_dir = config.get("lolor_lib", "")
        if not lolor_lib_dir:
            pytest.skip(f"No LOLOR library directory configured for {container_type}")
        names = [n.strip() for n in lolor_stripped_lib.split(",") if n.strip()]
        if not names:
            pytest.skip("No LOLOR library names configured (LOLOR_STRIPPED_LIB)")
        paths = [f"{lolor_lib_dir.rstrip('/')}/{name}" for name in names]

    print(f"\n--- Verifying LOLOR libraries are stripped on {container_name} ({container_type}): "
          f"{', '.join(paths)} ---")
    problems = [p for p in (pep_extension_checks.stripped_problem(container, path) for path in paths) if p]
    assert not problems, "; ".join(problems)
    print(f"✅ {len(paths)} LOLOR librar{'y is' if len(paths) == 1 else 'ies are'} stripped")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_sbom(container_name, container_type):
    """Verify SBOM signature files located under the PostgreSQL path sbom directory. Missing SBOM
    files fail in both modes (the package ships them; standalone failed on them before, too)."""
    container = _running_container(container_name)

    sbom_dir = f"{rhel_pg_path}/sbom" if container_type == "rhel" else f"{deb_pg_path}/sbom"
    missing = [name for name in ("lolor-sbom.json", "lolor-sbom.json.asc")
               if container.exec_run(["test", "-f", f"{sbom_dir}/{name}"], user="root")[0] != 0]
    if missing:
        pytest.fail(f"SBOM file(s) {', '.join(missing)} not found in {sbom_dir}")

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
            f"--signature-file lolor-sbom.json.asc "
            f"--signer-file pgedge-rsa.pub "
            f"lolor-sbom.json'",
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
            f"{_sq_sig_flag} lolor-sbom.json.asc "
            f"lolor-sbom.json'",
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
    """Verify bundled files for each component match expected files

    This compares the installed files from rpm/deb with expected files
    in expected-output/rpm/ or expected-output/deb/ directory. A missing reference skips a
    standalone run and fails in integration mode.
    """
    container = _running_container(container_name)

    # Get the actual package name for the platform
    config = get_container_config(container_type)
    actual_package = config["lolor_package"]

    # Get project root directory (parent of component-test/)
    project_root = Path(__file__).parent.parent

    try:
        # Call reusable verification function
        # Use actual_package for both component and package_name to ensure
        # correct expected file lookup based on the platform
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
            pep_extension_checks.skip_or_fail(INTEGRATION_REQUEST, str(e))
        pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_init_cluster(container_name, container_type):
    """Initialize PostgreSQL cluster with LOLOR-specific GUC parameters using pg_server_management module"""
    container = _running_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Initializing cluster on {container_name} ---")

    # LOLOR-specific GUC parameters, including the lolor.node this node assigns OIDs under
    guc_parameters = lolor_guc_parameters(PRIMARY_NODE)

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
    container = _running_container(container_name)

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
    container = _running_container(container_name)

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
@pytest.mark.parametrize("extension", base_extensions)
def test_create_extensions(container_name, container_type, extension):
    """Create each extension individually with separate test results

    This creates a separate test for each container-extension combination
    """
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    extension = extension.strip()
    if not extension:
        pytest.skip("Invalid container or extension")

    container = _running_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

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


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_lolor_extension_binding(container_name, container_type):
    """Integration mode: after CREATE EXTENSION, the running server's lolor.so belongs to the
    requested package, the server runs the requested PostgreSQL major, and the lolor extension
    version is exactly the requested release version. Skipped in standalone mode."""
    if INTEGRATION_REQUEST is None:
        pytest.skip("integration mode only")

    container = _running_container(container_name)
    verify_binding(container, container_type, get_container_config(container_type), [int(pgport)])


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_lolor_replaces_lo_functions(container_name, container_type):
    """CREATE EXTENSION lolor replaced each pg_catalog large-object function with lolor's C
    function and kept the built-in as <name>_orig, exactly for the functions lolor defines."""
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container = _running_container(container_name)
    config = get_container_config(container_type)

    values = sql_values(container, config["pgbin"], pgport, config["pguser"], function_state_sql(),
                        "large-object functions after CREATE EXTENSION")
    problems = function_state_problems(values.get("fn", []), enabled=True)
    assert not problems, "lolor did not swap the large-object functions: " + "; ".join(problems)
    print(f"✅ {len(SWAPPED_FUNCTIONS)} large-object functions are lolor's; the built-ins are kept as *_orig")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_lolor_large_object_round_trip(container_name, container_type):
    """Write, read, seek, tell, truncate and unlink large objects through lolor (descriptors used
    inside one explicit transaction): the OID carries lolor.node, the bytes read back are the
    bytes written, the data lives in lolor's own tables and never in pg_catalog.pg_largeobject,
    and unlink removes lolor's rows."""
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container = _running_container(container_name)
    config = get_container_config(container_type)

    values = sql_values(container, config["pgbin"], pgport, config["pguser"], round_trip_sql(),
                        "large-object round trip")
    problems = round_trip_problems(values)
    assert not problems, "lolor large-object round trip: " + "; ".join(problems)
    print(f"✅ lolor large-object round trip passed (OID {_one(values, 'loid')} on node {PRIMARY_NODE})")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_lolor_node_must_be_set(container_name, container_type):
    """lolor refuses to assign a large object OID while lolor.node is 0 (its boot value, i.e.
    not set), with its own error, and a node outside the GUC's range is rejected."""
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container = _running_container(container_name)
    config = get_container_config(container_type)
    pgbin, pguser = config["pgbin"], config["pguser"]

    values = sql_values(container, pgbin, pgport, pguser,
                        "SELECT 'boot=' || boot_val FROM pg_catalog.pg_settings WHERE name = 'lolor.node';",
                        "lolor.node boot value")
    assert _one(values, "boot") == "0", f"lolor.node boot value is {_one(values, 'boot')!r}, expected '0'"

    exit_code, output = run_sql(container, pgbin, pgport, pguser,
                                "SET lolor.node = 0;\nSELECT lo_creat(-1);", "lo_creat with lolor.node 0")
    assert exit_code != 0 and NODE_UNSET_ERROR in output, (
        f"lo_creat(-1) with lolor.node = 0 must fail with {NODE_UNSET_ERROR!r}; "
        f"psql exited {exit_code}:\n{output.strip()}")

    exit_code, output = run_sql(container, pgbin, pgport, pguser,
                                "SET lolor.node = 17;", "lolor.node out of range")
    assert exit_code != 0 and 'outside the valid range for parameter "lolor.node"' in output, (
        f"SET lolor.node = 17 must be rejected as out of range; psql exited {exit_code}:\n{output.strip()}")
    print("✅ lolor.node 0 is refused with the product's error; 17 is out of range")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_lolor_native_logical_replication(container_name, container_type):
    """Native PostgreSQL logical replication (a publication of lolor's two tables and a
    subscription; not Spock) to a second cluster in the same container. Once both tables have
    finished their initial sync, a large object written through lolor on the publisher must be read
    back with identical bytes through lolor on the subscriber within a fixed time, so it arrived as
    a streamed change. On failure the subscriber's log tail is shown. Both clusters are cleaned up."""
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container = _running_container(container_name)
    config = get_container_config(container_type)
    pgbin, pguser = config["pgbin"], config["pguser"]

    print(f"\n--- Native logical replication of {', '.join(LOLOR_TABLES)}: port {pgport} -> {REPLICA_PORT} ---")
    replica_started = passed = False
    try:
        success, _conf, message = pg_server_management.init_cluster(
            container, pgbin, REPLICA_PGDATA, pguser, lolor_guc_parameters(REPLICA_NODE))
        assert success, f"Subscriber cluster initialization failed: {message}"
        success, _out, message = pg_server_management.start_server(
            container, pgbin, REPLICA_PGDATA, REPLICA_PORT, pguser)
        assert success, f"Subscriber cluster start failed: {message}"
        replica_started = True

        sql_values(container, pgbin, REPLICA_PORT, pguser, f"CREATE EXTENSION {LOLOR_EXTENSION};",
                   "create lolor on the subscriber")
        if INTEGRATION_REQUEST is not None:
            verify_binding(container, container_type, config, [int(REPLICA_PORT)])
        sql_values(container, pgbin, pgport, pguser,
                   f"CREATE PUBLICATION {PUBLICATION} FOR TABLE {', '.join(LOLOR_TABLES)};",
                   "create the publication")
        sql_values(container, pgbin, REPLICA_PORT, pguser,
                   f"CREATE SUBSCRIPTION {SUBSCRIPTION} CONNECTION "
                   f"'host=localhost port={pgport} dbname=postgres user={pguser} "
                   f"connect_timeout={PG_CONNECT_TIMEOUT_S}' PUBLICATION {PUBLICATION};",
                   "create the subscription")
        wait_for_initial_sync(container, pgbin, REPLICA_PORT, pguser)

        written = sql_values(container, pgbin, pgport, pguser, replication_write_sql(),
                             "write a large object on the publisher")
        problems = replication_write_problems(written)
        assert not problems, "writing on the publisher: " + "; ".join(problems)
        loid = int(_one(written, "loid"))

        wait_for_replicated_large_object(container, pgbin, REPLICA_PORT, pguser, loid, REPLICATION_PAYLOAD)
        read = sql_values(container, pgbin, REPLICA_PORT, pguser,
                          replica_read_sql(loid, len(REPLICATION_PAYLOAD)), "read on the subscriber")
        problems = replica_read_problems(read)
        assert not problems, "reading on the subscriber: " + "; ".join(problems)
        print(f"✅ large object {loid} ({len(REPLICATION_PAYLOAD)} bytes) replicated to port {REPLICA_PORT}")
        passed = True
    finally:
        if replica_started and not passed:
            print(f"--- subscriber log, last {REPLICA_LOG_LINES} lines of {REPLICA_PGDATA}/logfile ---\n"
                  f"{subscriber_log_tail(container)}")
        cleanup = cleanup_replication(container, pgbin, pguser, replica_started)
        if cleanup and not passed:          # the check's own failure propagates; still report these
            print("⚠️  replication cleanup also failed: " + "; ".join(cleanup))
    assert not cleanup, "replication cleanup failed: " + "; ".join(cleanup)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_lolor_drop_restores_lo_functions(container_name, container_type):
    """DROP EXTENSION lolor restores the built-in large-object functions, leaves no *_orig or
    lolor_* function, lolor table or event trigger behind, and lo_creat then works without
    lolor.node (the built-in)."""
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container = _running_container(container_name)
    config = get_container_config(container_type)

    sql = "\n".join([
        f"DROP EXTENSION {LOLOR_EXTENSION};",
        function_state_sql(),
        "SELECT 'lolor_tables=' || count(*) FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
        "ON n.oid = c.relnamespace WHERE n.nspname = 'lolor' "
        "AND c.relname IN ('pg_largeobject', 'pg_largeobject_metadata');",
        "SELECT 'event_trigger=' || count(*) FROM pg_catalog.pg_event_trigger WHERE evtname = 'lo_on_drop_extension';",
        "SET lolor.node = 0;",
        "SELECT lo_creat(-1) AS core_loid \\gset",
        "SELECT 'core_meta=' || count(*) FROM pg_catalog.pg_largeobject_metadata WHERE oid = :'core_loid'::oid;",
        "SELECT 'core_unlink=' || lo_unlink(:'core_loid'::oid);",
    ])
    values = sql_values(container, config["pgbin"], pgport, config["pguser"], sql, "DROP EXTENSION lolor")
    problems = function_state_problems(values.get("fn", []), enabled=False)
    for key, want in (("lolor_tables", 0), ("event_trigger", 0), ("core_meta", 1), ("core_unlink", 1)):
        _expect(values, key, want, problems)
    assert not problems, "DROP EXTENSION lolor: " + "; ".join(problems)
    print("✅ DROP EXTENSION lolor restored the built-in large-object functions")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_stop_server(container_name, container_type):
    """Stop PostgreSQL server using pg_server_management module"""
    container = _running_container(container_name)

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
def test_package_uninstall(container_name, container_type):
    """Uninstall lolor package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container = _running_container(container_name)

    # Get container-specific configuration
    config = get_container_config(container_type)
    lolor_package = config["lolor_package"]

    print(f"\n--- Uninstalling {lolor_package} on {container_name} ({container_type}) ---")

    # Use the package_management module to uninstall the package
    try:
        success, platform, message = package_management.uninstall_package(container, lolor_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {lolor_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Full cleanup using machine_cleanup module: remove all pgedge packages + leftover data"""
    if skip_cleanup:
        pytest.skip("Skipping cleanup: SKIP_CLEANUP=true")

    container = _running_container(container_name)

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

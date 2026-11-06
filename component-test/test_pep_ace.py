import importlib.util as _ilu
import json
import os
import re
import shlex
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


def _load_util(name):
    """Load a shared integration helper from aspects/pipeline/ by path (no package
    __init__ there), so these imports work however this file is loaded."""
    spec = _ilu.spec_from_file_location(
        name, os.path.join(os.path.dirname(__file__), '..', 'aspects', 'pipeline', name + '.py'))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The shared integration-mode install/identity steps, and through them the request contract
# (the same ones the RAG and Spock tests use).
pep_integration_mode = _load_util('pep_integration_mode')
pep_request_env = pep_integration_mode.pep_request_env

load_dotenv()
client = docker.from_env()

# Integration request built from the PEP_* env contract (validated by normalize_request against the
# shared component policy, which maps ace to pgedge-ace). None in standalone mode, which keeps the
# standalone options below (ACE_PACKAGE, PLATFORM_FILTER, SPOCK_MAJOR, PGEDGE_ACE_VERSION) unchanged.
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

# In integration mode the package under test is exactly the requested (validated) package.
if INTEGRATION_REQUEST is not None:
    rhel_ace_package = deb_ace_package = INTEGRATION_REQUEST["package_name"]

ACE_BINARY = "/usr/bin/ace"

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
# plus the replication engine ACE diffs against. Standalone selects it with
# SPOCK_MAJOR (default 50). Release certification uses Spock 5 from the request's
# channel and never SPOCK_MAJOR, so an inherited value cannot change what is
# certified; the installed versions are recorded (record_prerequisite_versions).
ACE_CERT_SPOCK_SERIES = "50"
ace_spock_major = (ACE_CERT_SPOCK_SERIES if INTEGRATION_REQUEST is not None
                   else os.getenv("SPOCK_MAJOR", "50"))

# The seeded data. n1 gets the initial rows, which replicate to n2 and n3; then, with
# every subscription disabled, each node inserts its own rows, continuing the same id
# sequence from the initial rows (label, count).
ACE_NODES = ("n1", "n2", "n3")


def _node_pgdata(node):
    return f"/tmp/{node}"


# Every functional run works in a fresh directory: test_ace_functional_create_config
# recreates it, so ACE's init commands never meet an earlier run's files, and
# test_ace_table_diff reads a report only from an emptied subdirectory. Emptying it
# uses rm -rf, so ACE_WORK_DIR must name a clearly test-owned directory
# (ace_work_dir_problem); an unset or empty value means the default.
_ACE_WORK_DIR_NAME = re.compile(r"ace-pep[A-Za-z0-9._-]*")
_SAFE_PATH_PART = re.compile(r"[A-Za-z0-9._-]+")


def ace_work_dir_problem(path):
    """Why ``path`` is not a safe ACE work directory, or None. It must be an absolute,
    normalized path of plain characters (letters, digits, ".", "_", "-") whose last
    component starts with "ace-pep", and it must not overlap a node's data directory.
    So /tmp, /, a relative path, a path through "." or "..", or a path with shell or
    glob characters is refused before anything is deleted."""
    if not path.startswith("/"):
        return f"ACE_WORK_DIR {path!r} must be an absolute path"
    parts = path.split("/")[1:]
    if not all(_SAFE_PATH_PART.fullmatch(part) and part not in (".", "..") for part in parts):
        return (f"ACE_WORK_DIR {path!r} must be a normalized path of letters, digits, '.', '_' "
                f"and '-' (no '.', '..', '//' or other characters)")
    if not _ACE_WORK_DIR_NAME.fullmatch(parts[-1]):
        return (f"ACE_WORK_DIR {path!r} must end in a directory named ace-pep*, so only a "
                f"test-owned directory is ever deleted")
    for node in ACE_NODES:
        pgdata = _node_pgdata(node)
        if path == pgdata or path.startswith(pgdata + "/") or pgdata.startswith(path + "/"):
            return f"ACE_WORK_DIR {path!r} overlaps the {node} data directory {pgdata}"
    return None


_ace_work_dir_env = os.getenv("ACE_WORK_DIR") or "/tmp/ace-pep"
ace_work_dir = _ace_work_dir_env.rstrip("/") or _ace_work_dir_env
ACE_WORK_DIR_PROBLEM = ace_work_dir_problem(ace_work_dir)
ace_pg_service_path = f"{ace_work_dir}/pg_service.conf"
ace_config_path = f"{ace_work_dir}/ace.yaml"
ace_table_diff_dir = f"{ace_work_dir}/table-diff"

SEED_INITIAL_ROWS = 10
SEED_LOCAL_ROWS = {"n1": ("n1_load", 100), "n2": ("n2_divergent", 5), "n3": ("n3_divergent", 5)}
# How long the initial replication may take to reach n2 and n3.
ace_seed_timeout = float(os.getenv("ACE_SEED_TIMEOUT", "120"))

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


def get_ace_prerequisites(container_type):
    """Integration mode: the PostgreSQL server and Spock 5 packages the functional cluster needs,
    for the request's PG major, from the same channel with no version pin. They are installed
    BEFORE the pinned ACE package so installing them can never replace it. The RPM server is
    named explicitly so a missing server is reported as a prerequisite failure."""
    pg = ace_pg_major_version
    if container_type == "rhel":
        return [f"pgedge-postgresql{pg}-server", f"pgedge-postgresql{pg}-contrib",
                f"pgedge-spock{ace_spock_major}_{pg}"]
    return [f"pgedge-postgresql-{pg}", f"pgedge-postgresql-{pg}-spock{ace_spock_major}"]


def _cluster_prerequisites_to_install(container_type):
    """Packages test_ace_functional_setup_cluster installs: the standalone Spock set, or nothing in
    integration mode, where test_component_install already installed them before the pinned ACE."""
    return [] if INTEGRATION_REQUEST is not None else _ace_spock_packages(container_type)


def record_prerequisite_versions(container, container_type):
    """Integration mode, audit only: the installed versions of the PostgreSQL/Spock prerequisites
    and of the requested ACE package, printed and written to PEP_PREREQ_OUT. Never part of the
    verdict."""
    names = get_ace_prerequisites(container_type) + [INTEGRATION_REQUEST["package_name"]]
    versions = {n: package_management.query_installed_version(container, n) for n in names}
    out = os.getenv("PEP_PREREQ_OUT", "test-logs/prerequisite-versions.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"channel": repo, "pg_major": ace_pg_major_version,
                                     "spock_series": ace_spock_major,
                                     "requested_package": INTEGRATION_REQUEST["package_name"],
                                     "installed_versions": versions}, indent=2, sort_keys=True) + "\n")
    for n, v in versions.items():
        print(f"   installed {n} = {v}")
    return versions


# A release tag: v<major>.<minor>.<patch>, optionally -<suffix> (rc.1, beta.2, test1).
_ACE_TAG = re.compile(r"v(\d+\.\d+\.\d+)(?:-([0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?")


def expected_ace_binary_version(request):
    """``(version, problems)``: the version `ace --version` must print for ``request``, or None with
    the reasons it cannot be derived.

    ACE's release build stamps the binary with its tag without the leading "v", prerelease
    punctuation kept (v3.0.0 -> 3.0.0, v3.0.1-rc.1 -> 3.0.1-rc.1), so the expectation is the
    request's effective tag without the "v". The other release facts in the request must agree with
    that tag under the pgEdge release-tag rules: version = the tag's x.y.z; build number =
    <suffix without dots>_1, or 1 for a final release; RPM <version>-<build number>.<dist>; DEB
    <version>[~<suffix without dots>]-1.<codename>. A request whose facts disagree is reported, not
    trusted."""
    tag = request.get("effective_tag")
    if not tag:
        return None, ["effective_tag is required to derive the expected ace version"]
    match = _ACE_TAG.fullmatch(tag)
    if not match:
        return None, [f"effective_tag {tag!r} is not v<major>.<minor>.<patch>[-<suffix>]"]
    base, suffix = match.group(1), match.group(2)
    compact = suffix.replace(".", "") if suffix else None
    buildnum = f"{compact}_1" if suffix else "1"
    deb_upstream = f"{base}~{compact}" if suffix else base
    problems = []
    if base != request.get("expected_version"):
        problems.append(f"effective_tag {tag!r} does not match expected_version "
                        f"{request.get('expected_version')!r}")
    if request.get("expected_buildnum") is not None and request["expected_buildnum"] != buildnum:
        problems.append(f"expected_buildnum {request['expected_buildnum']!r} does not match "
                        f"effective_tag {tag!r} (expected {buildnum!r})")
    rpm = request.get("expected_rpm")
    if rpm is not None and not re.fullmatch(re.escape(f"{base}-{buildnum}.") + r"[0-9A-Za-z_+]+", rpm):
        problems.append(f"expected_rpm {rpm!r} does not match effective_tag {tag!r} "
                        f"(expected {base}-{buildnum}.<dist>)")
    deb = request.get("expected_deb")
    if deb is not None and not re.fullmatch(re.escape(f"{deb_upstream}-1.") + r"[0-9A-Za-z+~]+", deb):
        problems.append(f"expected_deb {deb!r} does not match effective_tag {tag!r} "
                        f"(expected {deb_upstream}-1.<codename>)")
    if problems:
        return None, problems
    return tag[1:], []


def ace_binary_owners(container, container_type):
    """``(owners, raw)``: the packages the package manager says own /usr/bin/ace (sorted; [] when it
    is unowned or the query fails) and the raw answer."""
    if container_type == "rhel":
        cmd = ["rpm", "-qf", "--queryformat", "%{NAME}\\n", ACE_BINARY]
    else:
        cmd = ["dpkg-query", "-S", ACE_BINARY]
    exit_code, output = container.exec_run(cmd, user="root")
    raw = output.decode().strip()
    if exit_code != 0:
        return [], raw
    if container_type == "rhel":
        return sorted({line.strip() for line in raw.splitlines() if line.strip()}), raw
    owners = set()
    for line in raw.splitlines():
        packages, sep, path = line.partition(": ")
        if sep and path.strip() == ACE_BINARY:
            owners.update(p.strip().split(":", 1)[0] for p in packages.split(","))
    return sorted(owners), raw


def _skip_or_fail(reason):
    """A standalone run skips when an input is missing. In integration mode the check fails
    instead: skips are reported separately, but the certification verdict could still pass with
    the check skipped."""
    if INTEGRATION_REQUEST is not None:
        pytest.fail(reason)
    pytest.skip(reason)


def _running_container(container_name):
    """The named container, which must be running. A missing container skips a standalone run
    and fails an integration run."""
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        _skip_or_fail(f"Container {container_name} not found or not running.")
    assert container.status == "running"
    return container


def _sh(container, script):
    return container.exec_run(["bash", "-c", script], user="root")


def _require_safe_work_dir():
    """Fail the step, before anything runs in the container, when ACE_WORK_DIR is unsafe."""
    if ACE_WORK_DIR_PROBLEM is not None:
        pytest.fail(ACE_WORK_DIR_PROBLEM)


def _reset_test_dir(container, path):
    """Empty and recreate ``path``, which must be the validated work directory or a directory
    inside it, and return ``(exit_code, output)``; the output lists anything still there. The
    script refuses (exit 3) when the work directory or ``path`` is a symbolic link, so the
    deletion can never follow a link out of the test-owned directory."""
    _require_safe_work_dir()
    child = path[len(ace_work_dir) + 1:] if path.startswith(ace_work_dir + "/") else None
    inside = path == ace_work_dir or (
        child is not None and _SAFE_PATH_PART.fullmatch(child) is not None and child not in (".", ".."))
    assert inside, f"refusing to empty {path!r}: it is outside the ACE work directory {ace_work_dir}"
    quoted = shlex.quote(path)
    links = " ".join(shlex.quote(p) for p in dict.fromkeys([ace_work_dir, path]))
    return _sh(container,
               f"for p in {links}; do if [ -L \"$p\" ]; then "
               f"echo \"refusing to empty {path}: $p is a symbolic link\"; exit 3; fi; done; "
               f"rm -rf -- {quoted} && mkdir -p -- {quoted} && ls -A -- {quoted}")


def _ace(container, args, cwd):
    """Run `ace <args>` from ``cwd`` with this run's configuration and service file."""
    _require_safe_work_dir()
    return _sh(container,
               f"cd {shlex.quote(cwd)} && ACE_CONFIG={shlex.quote(ace_config_path)} "
               f"PGSERVICEFILE={shlex.quote(ace_pg_service_path)} {ACE_BINARY} {args}")


def _assert_file_written(container, path, command):
    exit_code, output = _sh(container, f"test -s {shlex.quote(path)}")
    assert exit_code == 0, f"{command} reported success but {path} is missing or empty"


def _node_port(node):
    return ace_base_port + ACE_NODES.index(node)


def _psql(container, node, pguser, sql, tuples=False):
    cmd = ["psql", "-h", "localhost", "-p", str(_node_port(node)), "-U", pguser, "-d", "postgres"]
    if tuples:
        cmd += ["-t", "-A", "-F", "|"]
    return container.exec_run(cmd + ["-c", sql], user="root")


def expected_seed_after_setup():
    """{node: {label: rows}} once the initial rows have replicated."""
    return {node: {"initial_row": SEED_INITIAL_ROWS} for node in ACE_NODES}


def expected_seed_after_divergence():
    """{node: {label: rows}} once every node has inserted its own rows."""
    return {node: {"initial_row": SEED_INITIAL_ROWS, label: count}
            for node, (label, count) in SEED_LOCAL_ROWS.items()}


def expected_table_diff_counts():
    """table-diff's diff_rows_count for the seeded data, per sorted node pair. ACE counts a row
    found on one node only once, and a row whose id exists on both nodes with different data once.
    Each node's own rows continue the same id sequence, so two nodes differ in max(their own row
    counts) rows: n1/n2 = n1/n3 = 100 and n2/n3 = 5."""
    counts = {}
    for i, a in enumerate(ACE_NODES):
        for b in ACE_NODES[i + 1:]:
            counts[f"{a}/{b}"] = max(SEED_LOCAL_ROWS[a][1], SEED_LOCAL_ROWS[b][1])
    return counts


# The rows of public.n1 per label (the data without its trailing _<n>).
_LABEL_COUNTS_SQL = ("SELECT regexp_replace(data, '_[0-9]+$', ''), count(*) "
                     "FROM public.n1 GROUP BY 1 ORDER BY 1;")


def node_label_counts(container, node, pguser):
    """``(counts, raw)``: {label: rows} of public.n1 on ``node``, or None when the query fails."""
    exit_code, output = _psql(container, node, pguser, _LABEL_COUNTS_SQL, tuples=True)
    raw = output.decode().strip()
    if exit_code != 0:
        return None, raw
    counts = {}
    for line in raw.splitlines():
        label, sep, rows = line.rpartition("|")
        if not sep or not rows.strip().isdigit():
            return None, raw
        counts[label] = int(rows)
    return counts, raw


def wait_for_seed(container, pguser, expected, timeout=0.0, interval=3.0):
    """Wait until public.n1 on every node in ``expected`` holds exactly the expected rows per label
    and return them; raise AssertionError naming the nodes that differ once ``timeout`` passes."""
    deadline = time.monotonic() + timeout
    while True:
        observed = {}
        for node in expected:
            counts, raw = node_label_counts(container, node, pguser)
            observed[node] = counts if counts is not None else f"query failed: {raw}"
        if observed == expected:
            return observed
        if time.monotonic() >= deadline:
            wrong = sorted(node for node in expected if observed[node] != expected[node])
            raise AssertionError(
                "public.n1 does not hold the seeded rows on " + ", ".join(wrong) + ": " +
                "; ".join(f"{node} expected {expected[node]}, found {observed[node]}" for node in wrong))
        time.sleep(interval)


def table_diff_report_problems(report):
    """Why a table-diff JSON report does not show exactly the seeded differences ([] if it does)."""
    if not isinstance(report, dict) or not isinstance(report.get("summary"), dict):
        return ["the report has no summary object"]
    summary = report["summary"]
    problems = []
    if (summary.get("schema"), summary.get("table")) != ("public", "n1"):
        problems.append(f"the report is for {summary.get('schema')}.{summary.get('table')}, not public.n1")
    if sorted(summary.get("nodes") or []) != list(ACE_NODES):
        problems.append(f"the report compares nodes {summary.get('nodes')}, not {list(ACE_NODES)}")
    if summary.get("incomplete_pairs"):
        problems.append(f"the comparison was incomplete for {summary['incomplete_pairs']}, "
                        f"so its counts are only lower bounds")
    if summary.get("diff_row_limit_reached"):
        problems.append("the diff row limit was reached, so the counts are truncated")
    expected = expected_table_diff_counts()
    if summary.get("diff_rows_count") != expected:
        problems.append(f"diff_rows_count is {summary.get('diff_rows_count')}, "
                        f"expected {expected} from the seeded rows")
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
    """Step 2: Install pgedge-ace using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    ace_package = config["ace_package"]

    container = _running_container(container_name)

    if INTEGRATION_REQUEST is not None:
        # The PostgreSQL/Spock prerequisites first, from the request's channel, then the requested
        # ACE package last, exactly as the request decides (pinned + checksum evidence), so no later
        # install can replace it.
        prerequisites = get_ace_prerequisites(container_type)
        print(f"\n--- Installing PostgreSQL {ace_pg_major_version} / Spock {ace_spock_major} "
              f"prerequisites on {container_name}: {', '.join(prerequisites)} ---")
        try:
            success, platform, message = package_management.install_package(
                container=container,
                package_name=prerequisites,
                pg_major_version=ace_pg_major_version,
                install_pg_server=False
            )
        except Exception as e:
            success, message = False, str(e)
        if not success:
            pytest.fail(f"PostgreSQL {ace_pg_major_version} / Spock {ace_spock_major} prerequisites "
                        f"unavailable from the {repo!r} channel ({', '.join(prerequisites)}); "
                        f"{ace_package} was not installed: {message}")
        print(f"\n--- Installing requested {ace_package} on {container_name} ---")
        pep_integration_mode.install_for_request(container, INTEGRATION_REQUEST)
        record_prerequisite_versions(container, container_type)
        return

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
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: certification never upgrades; the requested package is pinned")
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # Get container-specific configuration
    config = get_container_config(container_type)
    ace_package = config["ace_package"]

    container = _running_container(container_name)

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
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: superseded by test_ace_identity, which checks the exact "
                    "requested version")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not ace_version:
        pytest.skip("No PGEDGE_ACE_VERSION defined in env, skipping version check")

    # Get container-specific configuration
    config = get_container_config(container_type)
    ace_package = config["ace_package"]

    container = _running_container(container_name)

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
def test_ace_identity(container_name, container_type):
    """Integration mode: one consolidated identity verdict for the requested ACE package
    (install-before-identity, then the exact package-manager version). The shared binary probe
    (`<binary> -version`, read as a "Version:" line) cannot read ACE, so no binary is passed here;
    test_ace_binary_version checks the binary's owner and exact version. The evidence is persisted
    before any failure. Skipped in standalone mode."""
    req = INTEGRATION_REQUEST
    if req is None:
        pytest.skip("integration mode only")

    container = _running_container(container_name.strip())

    ev, problems = pep_integration_mode.record_identity(container, req)
    if ev is None:
        pytest.fail("; ".join(problems))
    print(f"identity evidence ({req['package_name']}): {ev}")
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_bundled_files(container_name, container_type):
    """Verify bundled files for ace match expected files

    This compares the installed files from rpm/deb with expected files
    in expected-output/rpm/ or expected-output/deb/ directory
    """
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    container = _running_container(container_name)

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
        # A missing expected-output reference skips a standalone run and fails an integration
        # run, where the requested release package would otherwise never be compared.
        if "No expected file found" in str(e):
            _skip_or_fail(str(e))
        pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_sbom(container_name, container_type):
    """Verify SBOM signature files located under the decoupled components SBOM directory"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not decoupled_sbom_path:
        _skip_or_fail("DECOUPLED_COMPONENTS_SBOM is not set, so the SBOM signature cannot be verified")

    container = _running_container(container_name)

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

    container = _running_container(container_name)

    print(f"\n--- Testing ace --help on {container_name} ({container_type}) ---")

    # Run ace --help command
    exit_code, output = container.exec_run("/usr/bin/ace --help")
    output_str = output.decode() if output else ""

    print(f"Output:\n{output_str}")

    assert exit_code == 0, f"ace --help failed with exit code {exit_code}: {output_str}"
    assert len(output_str) > 0, "ace --help returned empty output"
    print(f"✅ ace --help executed successfully")


# `ace --version` prints "ace version <version>" on stdout, where a release build's
# <version> is its git tag without the leading "v" (3.0.0, 3.0.0-rc.3). A leading "v"
# and text after the version, such as "(commit abc1234)", are tolerated.
_ACE_VERSION_LINE = re.compile(
    r"ace version v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)(?:\s.*)?")


def _parse_ace_version(stdout):
    """The version on the one "ace version ..." line of `ace --version` stdout.

    Raises ValueError when there is no such line, more than one, or the version is
    not major.minor.patch with an optional pre-release or build suffix."""
    lines = [line.strip() for line in stdout.splitlines()
             if re.match(r"ace version(\s|$)", line.strip())]
    if len(lines) != 1:
        raise ValueError(f"expected one 'ace version' line on stdout, found {len(lines)}")
    match = _ACE_VERSION_LINE.fullmatch(lines[0])
    if not match:
        raise ValueError(f"malformed version line {lines[0]!r}")
    return match.group(1)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_binary_version(container_name, container_type):
    """Verify that the ace binary reports exactly the expected version.

    Standalone, the expectation is PGEDGE_ACE_VERSION (skipped when it is not set).
    In integration mode it is the request's effective tag without the "v"
    (expected_ace_binary_version, which cross-checks the other release facts), the
    config value is ignored, and /usr/bin/ace must first belong to the requested
    package, so the probed binary is the one under test.

    Only stdout of `ace --version` is parsed, so a warning on stderr cannot be
    taken for the version. The match is exact on every channel: "3.0.0" fails
    on an RC, dirty or development build (3.0.0-rc.3, 3.0.0-dirty,
    3.0.0-5-gabc1234), and a pre-release passes only when it is the expected
    version (3.0.0-rc.3).
    """
    if INTEGRATION_REQUEST is not None:
        expected_version, problems = expected_ace_binary_version(INTEGRATION_REQUEST)
        if problems:
            pytest.fail("cannot derive the expected ace version from the request: " + "; ".join(problems))
        hint = f"from effective_tag {INTEGRATION_REQUEST['effective_tag']!r}"
    else:
        expected_version = ace_version
        if not expected_version:
            pytest.skip("PGEDGE_ACE_VERSION not set in env, skipping version check")
        hint = "set PGEDGE_ACE_VERSION to a pre-release to expect one"

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    if INTEGRATION_REQUEST is not None:
        requested = INTEGRATION_REQUEST["package_name"]
        owners, raw = ace_binary_owners(container, container_type)
        assert owners == [requested], (
            f"{ACE_BINARY} is owned by {owners or 'no package'}, not only the requested "
            f"{requested!r} (package manager said: {raw!r})")
        print(f"✅ {ACE_BINARY} belongs to {requested}")

    print(f"\n--- Checking ace --version on {container_name} ({container_type}) ---")

    # demux returns stdout and stderr separately; without it docker merges them.
    exit_code, (stdout, stderr) = container.exec_run(
        ["/usr/bin/ace", "--version"],
        user="root",
        demux=True,
    )
    stdout = (stdout or b"").decode()
    stderr = (stderr or b"").decode()
    print(f"   stdout: {stdout.strip()}")
    if stderr.strip():
        print(f"   stderr: {stderr.strip()}")

    assert exit_code == 0, (
        f"ace --version failed with exit code {exit_code}:\n"
        f"  stdout: {stdout!r}\n  stderr: {stderr!r}"
    )
    try:
        reported_version = _parse_ace_version(stdout)
    except ValueError as e:
        pytest.fail(f"Cannot read the ace version: {e}\n"
                    f"  stdout: {stdout!r}\n  stderr: {stderr!r}")

    assert reported_version == expected_version, (
        f"ace version mismatch: expected exactly '{expected_version}', got "
        f"'{reported_version}' ({hint})"
    )
    print(f"✅ ace reports version {reported_version} (expected {expected_version})")


# ============================================================================
# ACE Functional Tests (3-node cluster)
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_functional_setup_cluster(container_name, container_type):
    """ACE Functional Step 1: Initialize a 3-node Spock cluster for ACE functional testing"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    pgbin = _ace_pgbin(container_type)
    pguser = _ace_pguser(container_type)

    print(f"\n--- ACE: Initializing 3-node Spock cluster on {container_name} ---")

    # pgedge-ace ships only the ACE tool — it does not pull in PostgreSQL or
    # spock. Install the spock package set first: it provides the PostgreSQL
    # server (creating the postgres OS user and initdb/psql/pg_ctl) plus the
    # replication engine the ACE functional tests operate against. In integration
    # mode test_component_install installed them before the pinned ACE package.
    spock_packages = _cluster_prerequisites_to_install(container_type)
    if not spock_packages:
        print("▶️  Cluster prerequisites were installed before the requested ACE package")
    else:
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
        node_pgdata = _node_pgdata(node_name)

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
        f"    SELECT 'initial_row_' || g FROM generate_series(1, {SEED_INITIAL_ROWS}) g;\n"
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
    print(f"✅ Table public.n1 seeded with {SEED_INITIAL_ROWS} rows on n1")

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

    # Every node must hold exactly the seeded rows (no more, no fewer) before the
    # divergence step; n2 and n3 get them through the initial replication.
    print(f"\n⏳ Waiting up to {ace_seed_timeout:.0f} seconds for initial replication to n2 and n3...")
    observed = wait_for_seed(container, pguser, expected_seed_after_setup(), timeout=ace_seed_timeout)
    for node, counts in observed.items():
        print(f"✅ {node}: {counts} — initial replication confirmed")

    print(f"\n✅ 3-node Spock cluster ready for ACE functional tests")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_functional_create_divergence(container_name, container_type):
    """ACE Functional Step 2: Disable subscriptions and create data divergence across all nodes"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    pguser = _ace_pguser(container_type)

    print(f"\n--- ACE: Disabling subscriptions and creating data divergence ---")

    # Disable all subscriptions on every node so writes stay local. Each node must
    # report no enabled subscription afterwards; otherwise n1's rows could replicate
    # and the differences ACE is expected to find would not exist.
    print(f"\n▶️  Disabling Spock subscriptions on all nodes")
    disable_sql = (
        "DO $$ DECLARE r RECORD; BEGIN "
        "FOR r IN SELECT sub_name FROM spock.subscription LOOP "
        "PERFORM spock.sub_disable(r.sub_name); "
        "END LOOP; END $$;"
    )
    for node in ACE_NODES:
        exit_code, output = _psql(container, node, pguser, disable_sql)
        assert exit_code == 0, f"Could not disable the subscriptions on {node}: {output.decode()}"
        exit_code, output = _psql(container, node, pguser,
                                  "SELECT count(*) FROM spock.subscription WHERE sub_enabled;",
                                  tuples=True)
        enabled = output.decode().strip()
        assert exit_code == 0 and enabled == "0", (
            f"{node}: subscription(s) still enabled after disabling them: {enabled!r}")
        print(f"   ✅ Subscriptions disabled on {node}")

    time.sleep(2)

    # Spock replicates row data but not the sequence, so subscribers' n1_id_seq
    # is still at 1 while ids 1-10 already exist — a local SERIAL insert would
    # collide on the PK. Advance the sequence past the current max id before each
    # node's divergent insert.
    advance_seq = (
        "SELECT setval(pg_get_serial_sequence('public.n1','id'), "
        "(SELECT COALESCE(MAX(id), 1) FROM public.n1)); "
    )

    # Each node inserts its own rows (these do NOT replicate): n2 and n3 first, then n1.
    for node in ("n2", "n3", "n1"):
        label, count = SEED_LOCAL_ROWS[node]
        print(f"\n▶️  Inserting {count} {label} rows on {node}")
        exit_code, output = _psql(
            container, node, pguser,
            advance_seq +
            "INSERT INTO public.n1 (data) "
            f"SELECT '{label}_' || g FROM generate_series(1, {count}) g;")
        assert exit_code == 0, f"Failed to insert {label} rows on {node}: {output.decode()}"
        print(f"✅ {count} {label} rows inserted on {node}")

    # Every node must hold exactly its seeded rows, so the counts table-diff must
    # report follow from the seed (expected_table_diff_counts).
    observed = wait_for_seed(container, pguser, expected_seed_after_divergence())
    print(f"\n▶️  Rows per node:")
    for node, counts in observed.items():
        print(f"   {node}: {counts}")

    print(f"\n✅ Data divergence created — each node has unique rows")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_functional_create_config(container_name, container_type):
    """ACE Functional Step 3: Write pg_service.conf, run ace cluster init and config init, create pgcrypto"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    _require_safe_work_dir()
    container = _running_container(container_name)

    pguser = _ace_pguser(container_type)
    n1_port = ace_base_port
    n2_port = ace_base_port + 1
    n3_port = ace_base_port + 2
    pg_service_path = ace_pg_service_path
    ace_yaml_path = ace_config_path

    print(f"\n--- ACE: Creating configuration files in {ace_work_dir} ---")

    # A fresh, empty work directory, so neither init command meets an earlier run's file.
    exit_code, output = _reset_test_dir(container, ace_work_dir)
    leftover = output.decode().strip()
    assert exit_code == 0 and not leftover, (
        f"Failed to create an empty work directory {ace_work_dir} (exit {exit_code}): {leftover!r}")

    # ace cluster init writes ACE's sample service file. It must succeed on the fresh
    # path; this run's three nodes then replace the sample entries.
    print(f"\n▶️  Running: ace cluster init --path {pg_service_path}")
    exit_code, output = _ace(container, f"cluster init --path {shlex.quote(pg_service_path)}", ace_work_dir)
    output_str = output.decode()
    print(f"   {output_str[:500]}")
    assert exit_code == 0, f"ace cluster init failed (exit {exit_code}):\n{output_str}"
    _assert_file_written(container, pg_service_path, "ace cluster init")
    print(f"✅ ace cluster init completed")

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

    # ace config init writes the ace.yaml every later ace command uses (ACE_CONFIG).
    print(f"\n▶️  Running: ace config init --path {ace_yaml_path}")
    exit_code, output = _ace(container, f"config init --path {shlex.quote(ace_yaml_path)}", ace_work_dir)
    output_str = output.decode()
    print(f"   {output_str[:500]}")
    assert exit_code == 0, f"ace config init failed (exit {exit_code}):\n{output_str}"
    _assert_file_written(container, ace_yaml_path, "ace config init")
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

    _require_safe_work_dir()
    container = _running_container(container_name)

    print(f"\n--- ACE: Running table-diff on public.n1 "
          f"for cluster '{ace_cluster_name}' ---")

    # table-diff exits 0 whether or not the tables match, so the verdict comes from its JSON
    # report. The report is read only from a directory emptied right before this run, so a
    # report left by an earlier run (same public_n1_diffs-*.json pattern) can never be read.
    report_dir = shlex.quote(ace_table_diff_dir)
    exit_code, output = _reset_test_dir(container, ace_table_diff_dir)
    leftover = output.decode().strip()
    assert exit_code == 0 and not leftover, (
        f"the report directory {ace_table_diff_dir} is not empty after resetting it "
        f"(exit {exit_code}): {leftover!r}")

    exit_code, output = _ace(container, f"table-diff -o html {shlex.quote(ace_cluster_name)} public.n1",
                             ace_table_diff_dir)
    output_str = output.decode()
    print(f"   Output:\n{output_str[:1000]}")
    assert exit_code == 0, f"ace table-diff failed (exit {exit_code}):\n{output_str}"

    exit_code, output = _sh(container, f"cd {report_dir} && ls -1 -- public_n1_diffs-*.json 2>/dev/null; true")
    reports = output.decode().split()
    assert len(reports) == 1, (
        f"expected exactly one public_n1_diffs-*.json report from this run in {ace_table_diff_dir}, "
        f"found {len(reports)}: {reports}. ACE writes no report when the tables match, so the "
        f"seeded differences were not found")
    exit_code, output = container.exec_run(["cat", f"{ace_table_diff_dir}/{reports[0]}"], user="root")
    assert exit_code == 0, f"Failed to read the table-diff report {reports[0]}: {output.decode()}"
    try:
        report = json.loads(output.decode())
    except ValueError as e:
        pytest.fail(f"the table-diff report {reports[0]} is not valid JSON: {e}")

    problems = table_diff_report_problems(report)
    assert not problems, f"table-diff report {reports[0]}: " + "; ".join(problems)
    print(f"✅ ace table-diff found the seeded differences: {report['summary']['diff_rows_count']}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_ace_mtree_init(container_name, container_type):
    """ACE Functional Step 5: Initialize Merkle tree infrastructure on all nodes"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    print(f"\n--- ACE: Running mtree init for cluster '{ace_cluster_name}' ---")

    exit_code, output = _ace(container, f"mtree init --dbname postgres {ace_cluster_name}", ace_work_dir)
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

    container = _running_container(container_name)

    print(f"\n--- ACE: Running mtree build for public.n1 ---")

    exit_code, output = _ace(container, f"mtree build --dbname postgres {ace_cluster_name} public.n1", ace_work_dir)
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

    container = _running_container(container_name)

    print(f"\n--- ACE: Running mtree table-diff for public.n1 ---")

    exit_code, output = _ace(container, f"mtree table-diff --dbname postgres {ace_cluster_name} public.n1 -o html", ace_work_dir)
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

    container = _running_container(container_name)

    print(f"\n--- ACE: Running mtree update for public.n1 ---")

    exit_code, output = _ace(container, f"mtree update --dbname postgres {ace_cluster_name} public.n1", ace_work_dir)
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

    container = _running_container(container_name)

    pgbin = _ace_pgbin(container_type)
    pguser = _ace_pguser(container_type)

    print(f"\n--- ACE: Stopping all PostgreSQL nodes ---")

    for node_num in range(1, 4):
        node_name = f"n{node_num}"
        node_pgdata = _node_pgdata(node_name)
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

    container = _running_container(container_name)

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

    container = _running_container(container_name)

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

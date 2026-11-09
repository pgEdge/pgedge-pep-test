import importlib.util as _ilu
import json
import os
import re
import shlex
import sys
import subprocess
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


# The shared integration-mode install/identity steps, and through them the request contract and the
# shared component policy (the same ones the ACE, RAG and Spock tests use).
pep_integration_mode = _load_util('pep_integration_mode')
pep_request_env = pep_integration_mode.pep_request_env
_pr = pep_request_env._pr

load_dotenv()
client = docker.from_env()

# Integration request built from the PEP_* env contract (validated by normalize_request against the
# shared component policy, which maps radar to pgedge-radar). None in standalone mode, which keeps the
# standalone options below (RADAR_PACKAGE, PLATFORM_FILTER, SERVER_PACKAGE, PGEDGE_RADAR_VERSION,
# COMPONENT_BINARY, COMPONENT_BINARY_VERSION) unchanged.
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
# The package version the standalone package-version check expects. There is no default: when it
# is unset that check skips instead of comparing against a version that was never released.
radar_version = os.getenv("PGEDGE_RADAR_VERSION", "")

# The binary the pgedge-radar package installs (pgEdge/radar packaging/rpm/radar.spec %files and
# packaging/deb/debian/rules). `radar --version` prints "radar version <version>" (radar 0.6.2 and
# later). A standalone run probes COMPONENT_BINARY and expects COMPONENT_BINARY_VERSION; with that
# unset the binary version check skips and the collections only record radar.out's version. The
# package version (PGEDGE_RADAR_VERSION) is never used for it: a prerelease binary reports
# 0.6.4-rc.1 while its packages are 0.6.4-rc1_1 (RPM) and 0.6.4~rc1-1 (DEB). Integration mode always
# probes the packaged path and derives the expectation from the request
# (expected_radar_binary_version).
RADAR_PACKAGED_BINARY = "/usr/bin/radar"
component_binary = os.getenv("COMPONENT_BINARY", RADAR_PACKAGED_BINARY)
component_version = os.getenv("COMPONENT_BINARY_VERSION", "")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")
rhel_radar_package = os.getenv("RADAR_PACKAGE", "pgedge-radar")
rhel_server_package = os.getenv("SERVER_PACKAGE", f"pgedge-postgresql{pg_major_version}-server")
rhel_bundled_files = os.getenv("RADAR_BUNDLED_FILES", "").split(",")

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")
deb_radar_package = os.getenv("DEB_RADAR_PACKAGE", "pgedge-radar")
deb_server_package = os.getenv("DEB_SERVER_PACKAGE", f"pgedge-postgresql-{pg_major_version}")
deb_bundled_files = os.getenv("DEB_RADAR_BUNDLED_FILES", "").split(",")

# In integration mode the package under test is exactly the requested (validated) package, and the
# probed binary is the one that package installs.
if INTEGRATION_REQUEST is not None:
    rhel_radar_package = deb_radar_package = INTEGRATION_REQUEST["package_name"]
    component_binary = RADAR_PACKAGED_BINARY

# Additional configuration for component tests
components = [comp.strip() for comp in os.getenv("COMPONENTS", "pgedge-radar").split(",") if comp.strip()]

# SBOM paths. Both radar packages ship a signed SBOM here: packaging/rpm/radar.spec (%build and
# %files) and packaging/deb/debian/rules (override_dh_auto_install) in pgEdge/radar.
radar_sbom_dir = "/usr/share/pgedge-radar"
radar_sbom_json = "pgedge-radar-sbom.json"
radar_sbom_asc = "pgedge-radar-sbom.json.asc"

# Functional collection runs. radar writes its bundle into the current working directory as
# radar-<hostname>-<YYYYMMDD>-<HHMMSS>.zip, so each case runs in its own scratch directory and claims
# the single zip that appears there. The bundle holds system, configuration and environment data:
# it is read only inside the container (just radar.out and postgresql/version.tsv), never copied out
# or printed, and removed with its directory once the case is checked.
radar_workdir_base = "/tmp/radar-functional"
radar_zip_pattern = re.compile(r"^radar-.+-\d{8}-\d{6}\.zip$")

# Database created for the "PostgreSQL data only" case, matching the documented
# `radar -d mydatabase --skip-system` invocation.
radar_test_database = os.getenv("RADAR_TEST_DATABASE", "mydatabase")


def integration_problem(request, pg_major_env):
    """Why this test cannot serve ``request`` (None when it can, or in standalone mode): it must ask
    for the radar component, for one of radar's packages in the shared policy, and for the
    PostgreSQL major this run installs and starts (PG_MAJOR_VERSION). Checked again before anything
    is installed."""
    if request is None:
        return None
    if request["component"] != "radar":
        return f"component {request['component']!r} is not radar"
    allowed = _pr.COMPONENT_PACKAGES.get("radar", ())
    if request["package_name"] not in allowed:
        return (f"package {request['package_name']!r} is not a radar package "
                f"(expected one of {list(allowed)})")
    if str(request["pg_major"]) != str(pg_major_env):
        return f"request PG{request['pg_major']} disagrees with PG_MAJOR_VERSION={pg_major_env}"
    return None


INTEGRATION_PROBLEM = integration_problem(INTEGRATION_REQUEST, pg_major_version)


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "radar_package": rhel_radar_package,
            "server_package": rhel_server_package,
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "radar_package": deb_radar_package,
            "server_package": deb_server_package,
        }


def get_radar_prerequisites(container_type):
    """Integration mode: the PostgreSQL server for the request's PG major, from the same channel with
    no version pin, installed BEFORE the pinned radar package so installing it can never replace
    radar. radar itself needs no server (the package has no PostgreSQL dependency), but the
    functional steps collect from one, so a missing server is reported as a prerequisite failure,
    not as a radar failure."""
    if container_type == "rhel":
        return [f"pgedge-postgresql{pg_major_version}-server"]
    return [f"pgedge-postgresql-{pg_major_version}"]


def record_prerequisite_versions(container, container_type):
    """Integration mode, audit only: the installed versions of the PostgreSQL server prerequisite and
    of the requested radar package, printed and written to PEP_PREREQ_OUT. Never part of the
    verdict."""
    names = get_radar_prerequisites(container_type) + [INTEGRATION_REQUEST["package_name"]]
    versions = {n: package_management.query_installed_version(container, n) for n in names}
    out = os.getenv("PEP_PREREQ_OUT", "test-logs/prerequisite-versions.json")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"channel": repo, "pg_major": pg_major_version,
                                     "requested_package": INTEGRATION_REQUEST["package_name"],
                                     "installed_versions": versions}, indent=2, sort_keys=True) + "\n")
    for n, v in versions.items():
        print(f"   installed {n} = {v}")
    return versions


def installed_radar_packages(container, container_type):
    """Installed packages that belong to radar, read from the package manager: the names radar's
    policy entry accepts, and any other pgedge-radar* package (a debug or renamed companion, for
    example). Returns None if the query fails."""
    if container_type == "rhel":
        cmd = ["rpm", "-qa", "--queryformat", "%{NAME}\\n"]
    else:
        cmd = ["dpkg-query", "-W", "-f=${Package} ${Status}\\n"]
    exit_code, output = container.exec_run(cmd, user="root")
    if exit_code != 0:
        return None
    names = set()
    for line in output.decode().splitlines():
        parts = line.split()
        if not parts or (container_type != "rhel" and "installed" not in parts[-1:]):
            continue
        names.add(parts[0])
    allowed = _pr.COMPONENT_PACKAGES.get("radar", ())
    return sorted(n for n in names if n in allowed or n.startswith("pgedge-radar"))


# A release tag: v<major>.<minor>.<patch>, optionally -<suffix> (rc.1, beta.2, test1).
_RADAR_TAG = re.compile(r"v(\d+\.\d+\.\d+)(?:-([0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?")


def expected_radar_binary_version(request):
    """``(version, problems)``: the version `radar --version` (and the archive's radar.out) must report
    for ``request``, or None with the reasons it cannot be derived.

    This is the rule test_pep_ace.expected_ace_binary_version applies to ACE; radar's release build
    (pgEdge/radar at v0.6.3) follows the same scheme:
      * binary: .goreleaser.yaml stamps -X main.version={{.Version}}, GoReleaser's tag without the
        leading "v", prerelease punctuation kept (v0.6.3 -> 0.6.3, v0.6.4-rc.1 -> 0.6.4-rc.1), and
        radar.go prints "radar version <version>". This holds for a real tag build; a simulate_tag
        (--snapshot) build is stamped "<incpatch>-next" and publishes nothing to install;
      * the package versions come from pgedge-parse-release-tag (version = the tag's x.y.z; build
        number = <suffix without dots>_1, or 1 for a final release): RPM radar.spec
        Version=<version>, Release=<build number>.<dist>; DEB packaging/common.sh moves the part of the
        build number before "_" into the version after "~" and build-deb.sh writes
        <version>[~<suffix without dots>]-1.<codename>.
    The other release facts in the request must agree with the tag; a request whose facts disagree is
    reported, not trusted."""
    tag = request.get("effective_tag")
    if not tag:
        return None, ["effective_tag is required to derive the expected radar version"]
    match = _RADAR_TAG.fullmatch(tag)
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


def expected_radar_version():
    """``(version, problem)``: the version this run expects radar to report, from `radar --version`
    and in the archive's radar.out. Integration mode derives it from the request
    (expected_radar_binary_version) and returns a problem when it cannot. Standalone it is
    COMPONENT_BINARY_VERSION, else None (no expectation; the package version is not the binary
    version)."""
    if INTEGRATION_REQUEST is not None:
        version, problems = expected_radar_binary_version(INTEGRATION_REQUEST)
        if problems:
            return None, "cannot derive the expected radar version from the request: " + "; ".join(problems)
        return version, None
    return (component_version or None), None


def radar_binary_owners(container, container_type):
    """``(owners, raw)``: the packages the package manager says own the radar binary (sorted; [] when
    it is unowned or the query fails) and the raw answer."""
    if container_type == "rhel":
        cmd = ["rpm", "-qf", "--queryformat", "%{NAME}\\n", component_binary]
    else:
        cmd = ["dpkg-query", "-S", component_binary]
    exit_code, output = container.exec_run(cmd, user="root")
    raw = output.decode().strip()
    if exit_code != 0:
        return [], raw
    if container_type == "rhel":
        return sorted({line.strip() for line in raw.splitlines() if line.strip()}), raw
    owners = set()
    for line in raw.splitlines():
        packages, sep, path = line.partition(": ")
        if sep and path.strip() == component_binary:
            owners.update(p.strip().split(":", 1)[0] for p in packages.split(","))
    return sorted(owners), raw


# `radar --version` prints exactly "radar version <version>" on stdout (radar.go).
_RADAR_VERSION_LINE = re.compile(r"radar version (\S+)")


def parse_radar_version(stdout):
    """The version on the one "radar version ..." line of `radar --version` stdout, as printed (a
    leading "v" or any suffix is kept, so the caller's exact comparison sees it).

    Raises ValueError when there is no such line, more than one, or the line is malformed."""
    lines = [line.strip() for line in stdout.splitlines()
             if re.match(r"radar version(\s|$)", line.strip())]
    if len(lines) != 1:
        raise ValueError(f"expected one 'radar version' line on stdout, found {len(lines)}")
    match = _RADAR_VERSION_LINE.fullmatch(lines[0])
    if not match:
        raise ValueError(f"malformed version line {lines[0]!r}")
    return match.group(1)


def radar_out_version(text):
    """``(version, problem)``: the version the archive's radar.out records. radar writes it as
    "version: <version>" and "commit: <commit>" lines (radar.go writeRadarMeta)."""
    versions = [line[len("version:"):].strip() for line in text.splitlines()
                if line.startswith("version:")]
    if len(versions) != 1 or not versions[0]:
        return None, f"radar.out has {len(versions)} 'version:' line(s), expected one with a value"
    return versions[0], None


def elf_strip_problem(file_output):
    """Why `file -b <binary>` output does not describe a stripped ELF binary (None if it does).
    `file` reports "not stripped" for a binary that keeps its symbol table, and that phrase contains
    "stripped", so it is rejected first."""
    text = file_output.strip()
    if not text.startswith("ELF "):
        return "it is not an ELF binary"
    if re.search(r"\bnot stripped\b", text):
        return "it is not stripped"
    if not re.search(r"\bstripped\b", text):
        return "`file` does not report it as stripped"
    return None


# radar's own console lines, which carry no collected data (radar.go, postgres.go printSummary).
_RADAR_STATUS_LINE = re.compile(
    r"Collecting diagnostic data\.\.\."
    r"|✓ Archive created: radar-\S+\.zip \(\d+ KB\)"
    r"|Total collectors: \d+"
    r"|ERROR: (?:Could not connect to PostgreSQL|Continuing with system data collection only"
    r"|No data collected|Failed to (?:create output file|write radar\.out|close archive|stat archive"
    r"|generate database tasks))\b.*")
_RADAR_TASK_ERROR = re.compile(r"ERROR: ✗ (\S+?):")


def radar_console_summary(output):
    """The lines of radar's console output that are safe to print: radar's own status lines, and
    only the name of each collector that reported an error. A collector error can quote what it was
    collecting (radar appends a failed command's output to it), so nothing else is shown; the number
    of withheld lines is reported instead."""
    shown, withheld = [], 0
    for line in output.splitlines():
        text = line.strip()
        if not text:
            continue
        task = _RADAR_TASK_ERROR.match(text)
        if task:
            shown.append(f"ERROR: ✗ {task.group(1)} (details withheld)")
        elif _RADAR_STATUS_LINE.fullmatch(text):
            shown.append(text)
        else:
            withheld += 1
    if withheld:
        shown.append(f"({withheld} other line(s) withheld)")
    return shown


def running_server_version(container, pgbin, pguser):
    """``(facts, problem)``: what the server radar collects from reports about itself, queried over
    the same connection radar uses (TCP localhost on PG_PORT): server_version_num, server_version and
    version(). ``facts`` is None with the reason when the query fails."""
    cmd = [f"{pgbin}/psql", "-X", "-h", "localhost", "-p", str(pgport), "-U", pguser, "-d", "postgres",
           "-tA", "-c", "SHOW server_version_num", "-c", "SHOW server_version", "-c", "SELECT version()"]
    exit_code, (stdout, stderr) = container.exec_run(cmd, user=pguser, demux=True)
    lines = [line for line in (stdout or b"").decode().splitlines() if line.strip()]
    if exit_code != 0 or len(lines) != 3 or not lines[0].strip().isdigit():
        return None, (f"cannot read the running server's version (exit {exit_code}): "
                      f"{(stderr or b'').decode().strip() or lines!r}")
    return {"server_version_num": int(lines[0].strip()), "server_version": lines[1].strip(),
            "version": lines[2].strip()}, None


def collected_server_version_problems(tsv_text, server, pg_major):
    """Why the archive's postgresql/version.tsv does not report the leg's server ([] if it does).

    radar writes `SELECT version()` there as a TSV with the header row "version" and one value row
    (postgres_tasks.go, radar.go rowsToTSV). The value must be exactly the running server's
    version(), that server's major (server_version_num / 10000) must be the leg's PG major, and
    version() must name the server's server_version."""
    lines = tsv_text.splitlines()
    if len(lines) != 2 or lines[0] != "version":
        return [f"postgresql/version.tsv is not a one-row 'version' table ({len(lines)} line(s))"]
    collected = lines[1]
    problems = []
    if collected != server["version"]:
        problems.append(f"postgresql/version.tsv reports {collected!r}, but the running server's "
                        f"version() is {server['version']!r}")
    major = server["server_version_num"] // 10000
    if str(major) != str(pg_major):
        problems.append(f"the running server is PostgreSQL {major} (server_version_num "
                        f"{server['server_version_num']}), not the leg's PG {pg_major}")
    if not collected.startswith(f"PostgreSQL {server['server_version']} "):
        problems.append(f"postgresql/version.tsv does not name server_version "
                        f"{server['server_version']!r}")
    return problems


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


def _ensure_unzip(container):
    """Make sure `unzip` is available; the prerequisites step does not install it."""
    exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v unzip"], user="root")
    if exit_code == 0:
        return

    print("   unzip not present, installing it...")
    container.exec_run(
        ["/bin/sh", "-c",
         "if command -v dnf >/dev/null 2>&1; then dnf install -y unzip; "
         "elif command -v apt-get >/dev/null 2>&1; then "
         "DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
         "DEBIAN_FRONTEND=noninteractive apt-get install -y unzip; fi"],
        user="root",
    )
    exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v unzip"], user="root")
    assert exit_code == 0, "unzip is required to inspect the radar bundle but could not be installed"


def _run_radar(container, workdir, args):
    """Run `radar <args>` in the empty scratch directory ``workdir`` and return the bundle's path.

    radar writes its bundle to the current working directory, so the single .zip that lands there
    is the bundle. PGPORT is exported because the test cluster does not necessarily listen on 5432.
    Only radar's own status lines are printed (radar_console_summary), never its collector errors.
    """
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", f"rm -rf -- {shlex.quote(workdir)} && mkdir -p -- {shlex.quote(workdir)}"],
        user="root")
    assert exit_code == 0, f"Failed to create {workdir}: {output.decode().strip()}"

    command = " ".join(shlex.quote(a) for a in args)
    print(f"   Running: radar {command}   (cwd={workdir})")
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", f"cd {shlex.quote(workdir)} && PGPORT={shlex.quote(str(pgport))} "
                          f"{shlex.quote(component_binary)} {command} 2>&1"],
        user="root",
    )
    summary = "\n".join(f"      {line}" for line in radar_console_summary(output.decode()))
    print(f"   radar output (status lines only):\n{summary}")
    assert exit_code == 0, f"'radar {command}' failed (exit {exit_code}):\n{summary}"

    # Exactly one bundle should have been produced.
    _, listing = container.exec_run(
        ["/bin/sh", "-c", f"cd {shlex.quote(workdir)} && ls -1 -- *.zip 2>/dev/null"], user="root"
    )
    zips = [z.strip() for z in listing.decode().strip().splitlines() if z.strip()]
    assert len(zips) == 1, (
        f"Expected exactly one .zip in {workdir} after 'radar {command}', found {zips}"
    )

    zip_name = zips[0]
    assert radar_zip_pattern.match(zip_name), (
        f"Bundle name {zip_name!r} does not match the expected "
        f"radar-<hostname>-<YYYYMMDD>-<HHMMSS>.zip form"
    )
    print(f"   ✅ Bundle created: {zip_name}")

    return f"{workdir}/{zip_name}"


def _bundle_entries(container, zip_path):
    """``(prefix, entries)``: the archive's entry names (read with `unzip -Z1`, nothing is extracted)
    and the prefix of its payload root, the directory holding radar.out. Some builds nest the payload
    one level under a directory named after the bundle, so the root is located by finding radar.out
    rather than assuming it sits at the archive root."""
    exit_code, (stdout, stderr) = container.exec_run(["unzip", "-Z1", zip_path], user="root", demux=True)
    assert exit_code == 0, f"Failed to list {zip_path} (exit {exit_code}): {(stderr or b'').decode().strip()}"
    entries = [e for e in (stdout or b"").decode().splitlines() if e.strip()]
    roots = [e for e in entries if e == "radar.out" or re.fullmatch(r"[^/]+/radar\.out", e)]
    assert len(roots) == 1, f"expected one radar.out at the payload root of {zip_path}, found {len(roots)}"
    prefix = roots[0][:-len("radar.out")]
    top = sorted({e[len(prefix):].split("/", 1)[0] for e in entries if e.startswith(prefix)})
    print(f"   Bundle holds {len(entries)} entries; payload top level: {', '.join(top)}")
    return prefix, entries


def _read_bundle_entry(container, zip_path, entry):
    """The text of one archive entry, read inside the container (`unzip -p`) and never printed."""
    exit_code, (stdout, stderr) = container.exec_run(["unzip", "-p", zip_path, entry], user="root", demux=True)
    assert exit_code == 0, f"Failed to read {entry} from the bundle (exit {exit_code})"
    return (stdout or b"").decode()


def _assert_bundle_contents(prefix, entries, expected_dirs, unexpected_dirs):
    """Assert the bundle holds exactly the collectors the flags asked for.

    The unexpected_dirs check is the part that gives --skip-postgres and
    --skip-system their meaning: without it the tests would still pass if the
    skip flags were ignored entirely. A missing postgresql/ in a run that asked
    for PostgreSQL data catches radar's silent fall back to system-only
    collection when it cannot connect.
    """
    assert prefix + "radar.out" in entries, "radar.out missing from the bundle"
    print("   ✅ radar.out present")

    for name in expected_dirs:
        assert any(e.startswith(f"{prefix}{name}/") for e in entries), (
            f"Expected directory '{name}' missing from the bundle"
        )
        print(f"   ✅ {name}/ present")

    for name in unexpected_dirs:
        assert not any(e == f"{prefix}{name}" or e.startswith(f"{prefix}{name}/") for e in entries), (
            f"Directory '{name}' should have been skipped but is present in the bundle"
        )
        print(f"   ✅ {name}/ correctly absent")


def _remove_bundle(container, workdir):
    """Delete the case's scratch directory, and the bundle in it, from the container."""
    exit_code, output = container.exec_run(["rm", "-rf", "--", workdir], user="root")
    assert exit_code == 0, f"Failed to remove {workdir}: {output.decode().strip()}"
    print(f"   Removed {workdir} (the bundle) from the container")


def _radar_collection(container_name, container_type, case, args, expected_dirs, unexpected_dirs,
                      collects_postgres):
    """Run one functional collection and check what it collected: the requested collectors only,
    radar.out reporting the expected radar version and, when PostgreSQL data was collected,
    postgresql/version.tsv reporting the running server of the leg's PG major. The bundle is removed
    afterwards whatever the outcome."""
    container = _running_container(container_name)
    config = get_container_config(container_type)

    expected_version, problem = expected_radar_version()
    if problem:
        pytest.fail(problem)
    server = None
    if collects_postgres:
        server, problem = running_server_version(container, config["pgbin"], config["pguser"])
        assert server is not None, problem

    _ensure_unzip(container)

    workdir = f"{radar_workdir_base}/{case}"
    try:
        zip_path = _run_radar(container, workdir, args)
        prefix, entries = _bundle_entries(container, zip_path)
        _assert_bundle_contents(prefix, entries, expected_dirs, unexpected_dirs)

        recorded, problem = radar_out_version(_read_bundle_entry(container, zip_path, prefix + "radar.out"))
        assert recorded is not None, problem
        if expected_version is not None:
            assert recorded == expected_version, (
                f"radar.out records version {recorded!r}, expected exactly {expected_version!r}")
            print(f"   ✅ radar.out records version {recorded}")
        else:
            print(f"   radar.out records version {recorded} (no expected version set)")

        if collects_postgres:
            entry = prefix + "postgresql/version.tsv"
            assert entry in entries, "postgresql/version.tsv missing from the bundle"
            problems = collected_server_version_problems(
                _read_bundle_entry(container, zip_path, entry), server, pg_major_version)
            assert not problems, "; ".join(problems)
            print(f"   ✅ postgresql/version.tsv reports the running server: PostgreSQL "
                  f"{server['server_version']} (major {server['server_version_num'] // 10000})")
    finally:
        _remove_bundle(container, workdir)


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

    container = _running_container(container_name)

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
    """Step 2: Install pgedge-radar using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    radar_package = config["radar_package"]

    container = _running_container(container_name)

    if INTEGRATION_REQUEST is not None:
        # Execution-side backstop: never install for a request this test cannot serve.
        assert INTEGRATION_PROBLEM is None, f"integration request cannot be served: {INTEGRATION_PROBLEM}"
        requested = INTEGRATION_REQUEST["package_name"]
        # The PostgreSQL server prerequisite first, from the request's channel, then the requested
        # radar package last, exactly as the request decides (pinned + checksum evidence), so no
        # later install can replace it.
        prerequisites = get_radar_prerequisites(container_type)
        print(f"\n--- Installing PostgreSQL {pg_major_version} server prerequisite on {container_name}: "
              f"{', '.join(prerequisites)} ---")
        try:
            success, platform, message = package_management.install_package(
                container=container,
                package_name=prerequisites,
                pg_major_version=pg_major_version,
                install_pg_server=False
            )
        except Exception as e:
            success, message = False, str(e)
        if not success:
            pytest.fail(f"PostgreSQL {pg_major_version} server prerequisite unavailable from the {repo!r} "
                        f"channel ({', '.join(prerequisites)}); {requested} was not installed: {message}")
        print(f"\n--- Installing requested {requested} on {container_name} ---")
        pep_integration_mode.install_for_request(container, INTEGRATION_REQUEST)
        record_prerequisite_versions(container, container_type)
        present = installed_radar_packages(container, container_type)
        assert present == [requested], (
            f"expected exactly the requested radar package {requested!r} to be installed, "
            f"found {present!r}")
        print(f"✅ Only {requested} is installed among radar packages")
        return

    print(f"\n--- Installing {radar_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.install_package(container, radar_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {radar_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_upgrade(container_name, container_type):
    """Upgrade radar package if UPGRADE=true"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: certification never upgrades; the requested package is pinned")
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    radar_package = config["radar_package"]

    container = _running_container(container_name)

    print(f"\n--- Upgrading {radar_package} on {container_name} ({container_type}) ---")

    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    try:
        success, platform, message = package_management.upgrade_package(container, radar_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{radar_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {radar_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the radar package version using package_management module"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: superseded by test_radar_identity, which checks the exact "
                    "requested version")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not radar_version:
        pytest.skip("No PGEDGE_RADAR_VERSION defined in env, skipping version check")

    config = get_container_config(container_type)
    radar_package = config["radar_package"]

    container = _running_container(container_name)

    print(f"\n--- Verifying {radar_package} version on {container_name} ({container_type}) ---")

    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, radar_package, radar_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {radar_package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_radar_identity(container_name, container_type):
    """Integration mode: one consolidated identity verdict for the requested radar package
    (install-before-identity, then the exact package-manager version). The shared binary probe
    (`<binary> -version`, read as a "Version:" line) cannot read radar's "radar version <version>",
    so no binary is passed here; test_binary_version checks the binary's owner and exact version.
    The evidence is persisted before any failure. Skipped in standalone mode."""
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
    """Verify bundled files for radar match expected files

    This compares the installed files from rpm/deb with expected files
    in expected-output/rpm/radar or expected-output/deb/radar, which list the
    payload pgEdge/radar's packaging defines (packaging/rpm/radar.spec and
    packaging/deb/debian/).
    """
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    container = _running_container(container_name)

    config = get_container_config(container_type)
    actual_package = config["radar_package"]

    project_root = Path(__file__).parent.parent

    try:
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=actual_package,
            package_name=actual_package,
            project_root=project_root
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
        # A missing expected-output reference skips a standalone run and fails an integration
        # run, where the requested release package would otherwise never be compared.
        if "No expected file found" in str(e):
            _skip_or_fail(str(e))
        pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_sbom(container_name, container_type):
    """Verify SBOM signature files for pgedge-radar.

    Both radar packages ship the SBOM and its detached signature under
    /usr/share/pgedge-radar (radar.spec %files; debian/rules), so a missing
    file is a package defect and fails the check.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    print(f"\n--- Verifying Radar SBOM on {container_name} ({container_type}) in {radar_sbom_dir} ---")

    for name in (radar_sbom_json, radar_sbom_asc):
        exit_code, _ = container.exec_run(["test", "-f", f"{radar_sbom_dir}/{name}"], user="root")
        assert exit_code == 0, (f"{radar_sbom_dir}/{name} is missing, but the radar package ships it "
                                f"(radar.spec %files, debian/rules)")

    machine_prereq_setup.ensure_sq_installed(container)
    _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
    _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
    _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"

    if container_type == "rhel":
        exit_code, output = container.exec_run(
            f"wget -q -O {radar_sbom_dir}/pgedge-rsa.pub https://dnf.pgedge.com/keys/pgedge-rsa.pub",
            user="root",
        )
        assert exit_code == 0, f"Failed to download pgedge-rsa.pub: {output.decode()}"
        signer_arg = f"{_sq_signer_flag} {radar_sbom_dir}/pgedge-rsa.pub"
    else:
        signer_arg = f"{_sq_signer_flag} /etc/apt/keyrings/pgedge-rsa.gpg"

    exit_code, output = container.exec_run(
        f"sh -c 'cd {radar_sbom_dir} && sq verify "
        f"{signer_arg} "
        f"{_sq_sig_flag} {radar_sbom_asc} "
        f"{radar_sbom_json}'",
        user="root",
    )
    output_str = output.decode().replace('\xa0', ' ')
    assert exit_code == 0, f"SBOM verification failed: {output_str}"
    assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
        f"Expected authenticated signature not found in output:\n{output_str}"
    print(f"✅ Radar SBOM signature verified on {container_name} ({container_type})")
    print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_server_install(container_name, container_type):
    """Install the PostgreSQL server before cluster init.

    pgedge-radar does not pull in PostgreSQL, so without this the postgres OS
    user and initdb/psql binaries are missing and test_init_cluster fails with
    "sudo: user 'postgres' not found". In integration mode test_component_install
    already installed the server for the request's PG major, before the pinned
    radar package.
    """
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: test_component_install installed the PostgreSQL server "
                    "before the requested radar package")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    server_package = config["server_package"]

    container = _running_container(container_name)

    print(f"\n--- Installing {server_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.install_package(
            container, server_package, pg_major_version=pg_major_version, install_pg_server=True
        )
        assert success, f"Server package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {server_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_init_cluster(container_name, container_type):
    """Initialize PostgreSQL cluster using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Initializing cluster on {container_name} ---")

    guc_parameters = {}

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

    container = _running_container(container_name)

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
        pytest.fail(f"Failed to start PostgreSQL server: {str(e)}")

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_check_connection(container_name, container_type):
    """Check PostgreSQL connection using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

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
def test_radar_collect_system_and_postgres(container_name, container_type):
    """Functional 1: `radar -d postgres -U postgres` collects system + PostgreSQL data.

    The bundle must contain every collector: system/, databases/ (with the
    postgres database), postgresql/ and radar.out. radar.out must record the
    expected radar version, and postgresql/version.tsv must report the running
    server of the leg's PG major.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    pguser = get_container_config(container_type)["pguser"]

    print(f"\n--- radar full collection on {container_name} ({container_type}) ---")

    _radar_collection(
        container_name, container_type, "full", ["-d", "postgres", "-U", pguser],
        expected_dirs=["system", "databases", "databases/postgres", "postgresql"],
        unexpected_dirs=[],
        collects_postgres=True,
    )


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_radar_collect_system_only(container_name, container_type):
    """Functional 2: `radar --skip-postgres` collects system data only.

    The bundle must contain system/ and radar.out (recording the expected radar
    version), and must NOT contain the PostgreSQL collectors.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    print(f"\n--- radar system-only collection on {container_name} ({container_type}) ---")

    _radar_collection(
        container_name, container_type, "system-only", ["--skip-postgres"],
        expected_dirs=["system"],
        unexpected_dirs=["postgresql", "databases"],
        collects_postgres=False,
    )


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_radar_collect_postgres_only(container_name, container_type):
    """Functional 3: `radar -d <db> --skip-system` collects PostgreSQL data only.

    The bundle must contain postgresql/, databases/ (with the target database)
    and radar.out, and must NOT contain system/. The target database is created
    first so the run exercises a real non-default database rather than falling
    back to 'postgres'. radar.out and postgresql/version.tsv are checked as in
    the full collection.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- radar postgres-only collection on {container_name} ({container_type}) ---")

    # Create the target database if it is not already there. createdb exits
    # non-zero when the database exists, which is fine on a re-run.
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c",
         f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres -tAc "
         f"\"SELECT 1 FROM pg_database WHERE datname='{radar_test_database}'\""],
        user=pguser,
    )
    if output.decode().strip() != "1":
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c", f"{pgbin}/createdb -p {pgport} -U {pguser} {radar_test_database}"],
            user=pguser,
        )
        assert exit_code == 0, (
            f"Failed to create database {radar_test_database}: {output.decode().strip()}"
        )
        print(f"   Created database {radar_test_database}")
    else:
        print(f"   Database {radar_test_database} already exists")

    _radar_collection(
        container_name, container_type, "postgres-only",
        ["-d", radar_test_database, "-U", pguser, "--skip-system"],
        expected_dirs=["postgresql", "databases", f"databases/{radar_test_database}"],
        unexpected_dirs=["system"],
        collects_postgres=True,
    )


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_binary_version(container_name, container_type):
    """Verify that the radar binary reports exactly the expected version.

    Standalone, the expectation is COMPONENT_BINARY_VERSION (skipped when it
    is unset; the package version is not used). In integration mode it
    is the request's effective tag without the "v" (expected_radar_binary_version,
    which cross-checks the other release facts), the config values are ignored,
    and /usr/bin/radar must first belong to the requested package, so the probed
    binary is the one under test.

    Only stdout of `radar --version` is parsed, so a warning on stderr cannot be
    taken for the version, and the match is exact: "0.6.3" fails on any other
    build ("v0.6.3", "0.6.3-dirty", "0.6.4-rc.1"), and a pre-release passes only
    when it is the expected version.
    """
    expected_version, problem = expected_radar_version()
    if problem:
        pytest.fail(problem)
    if INTEGRATION_REQUEST is not None:
        hint = f"from effective_tag {INTEGRATION_REQUEST['effective_tag']!r}"
    else:
        if not component_binary or not expected_version:
            pytest.skip("COMPONENT_BINARY or COMPONENT_BINARY_VERSION not set — skipping binary "
                        "version check")
        hint = "set COMPONENT_BINARY_VERSION to a pre-release to expect one"

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    if INTEGRATION_REQUEST is not None:
        requested = INTEGRATION_REQUEST["package_name"]
        owners, raw = radar_binary_owners(container, container_type)
        assert owners == [requested], (
            f"{component_binary} is owned by {owners or 'no package'}, not only the requested "
            f"{requested!r} (package manager said: {raw!r})")
        print(f"✅ {component_binary} belongs to {requested}")

    print(f"\n--- Checking {component_binary} --version on {container_name} ({container_type}) ---")

    # demux returns stdout and stderr separately; without it docker merges them.
    exit_code, (stdout, stderr) = container.exec_run(
        [component_binary, "--version"],
        user="root",
        demux=True,
    )
    stdout = (stdout or b"").decode()
    stderr = (stderr or b"").decode()
    print(f"   stdout: {stdout.strip()}")
    if stderr.strip():
        print(f"   stderr: {stderr.strip()}")

    assert exit_code == 0, (
        f"{component_binary} --version failed with exit code {exit_code}:\n"
        f"  stdout: {stdout!r}\n  stderr: {stderr!r}"
    )
    try:
        reported_version = parse_radar_version(stdout)
    except ValueError as e:
        pytest.fail(f"Cannot read the radar version: {e}\n"
                    f"  stdout: {stdout!r}\n  stderr: {stderr!r}")

    assert reported_version == expected_version, (
        f"radar version mismatch: expected exactly '{expected_version}', got "
        f"'{reported_version}' ({hint})"
    )
    print(f"✅ {component_binary} reports version {reported_version} (expected {expected_version})")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_binary_stripped(container_name, container_type):
    """Verify that the radar binary is a stripped ELF binary.

    Skipped when COMPONENT_BINARY is not set. Runs `file -b <binary>` and
    requires an ELF binary that `file` reports as stripped and not as "not
    stripped" (radar's release build links with -s -w, .goreleaser.yaml).
    """
    if not component_binary:
        pytest.skip("COMPONENT_BINARY not set — skipping binary strip check")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    print(f"\n--- Checking ELF strip status of {component_binary} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(["file", "-b", component_binary], user="root")
    file_output = output.decode().strip()
    assert exit_code == 0, f"'file -b {component_binary}' failed: {file_output}"
    print(f"   Output: {file_output}")

    problem = elf_strip_problem(file_output)
    assert problem is None, (
        f"Binary {component_binary} is not a stripped ELF binary: {problem}.\n"
        f"'file' output: {file_output}"
    )
    print(f"✅ {component_binary} is stripped")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_license_file(container_name, container_type):
    """Verify that the LICENSE file is installed for pgedge-radar."""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    config = get_container_config(container_type)
    radar_package = config["radar_package"]
    # RHEL ships the license under /usr/share/licenses/<pkg>/; Debian ships it
    # under /usr/share/doc/<pkg>/.
    if container_type == "rhel":
        license_path = f"/usr/share/licenses/{radar_package}/LICENCE.md"
    else:  # deb
        license_path = f"/usr/share/doc/{radar_package}/LICENCE.md"

    print(f"\n--- Verifying LICENSE file on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(
        f"test -f {license_path}",
        user="root"
    )
    assert exit_code == 0, f"LICENSE file not found at {license_path}: {output.decode()}"
    print(f"✅ LICENSE file exists at {license_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_verify_readme_file(container_name, container_type):
    """Verify that the README.md file is installed for pgedge-radar."""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    config = get_container_config(container_type)
    radar_package = config["radar_package"]
    # RHEL ships README.md uncompressed; Debian ships it gzipped as README.md.gz.
    if container_type == "rhel":
        readme_path = f"/usr/share/doc/{radar_package}/README.md"
    else:  # deb
        readme_path = f"/usr/share/doc/{radar_package}/README.md.gz"

    print(f"\n--- Verifying README file on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(
        f"test -f {readme_path}",
        user="root"
    )
    assert exit_code == 0, f"README.md not found at {readme_path}: {output.decode()}"
    print(f"✅ README.md exists at {readme_path}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_stop_server(container_name, container_type):
    """Stop PostgreSQL server using pg_server_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

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
    """Uninstall radar package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    radar_package = config["radar_package"]

    container = _running_container(container_name)

    print(f"\n--- Uninstalling {radar_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.uninstall_package(container, radar_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {radar_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Full cleanup using machine_cleanup module: remove all pgedge packages + leftover data"""
    if skip_cleanup:
        pytest.skip("Skipping cleanup: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

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

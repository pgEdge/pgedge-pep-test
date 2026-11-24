import importlib.util as _ilu
import os
import re
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
# shared component policy (the same ones the ACE, radar, RAG and Spock tests use).
pep_integration_mode = _load_util('pep_integration_mode')
pep_request_env = pep_integration_mode.pep_request_env
_pr = pep_request_env._pr

load_dotenv()
client = docker.from_env()

# Integration request built from the PEP_* env contract (validated by normalize_request against the
# shared component policy, which maps anonymizer to pgedge-anonymizer). None in standalone mode, which
# keeps the standalone options below (ANONYMIZER_PACKAGE, PLATFORM_FILTER, PGEDGE_ANONYMIZER_VERSION,
# DECOUPLED_COMPONENTS_SBOM) unchanged.
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
anonymizer_version = os.getenv("PGEDGE_ANONYMIZER_VERSION", "")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# RHEL-specific configuration
rhel_pgbin = os.getenv("PG_BIN_PATH", f"/usr/pgsql-{pg_major_version}/bin")
rhel_pg_path = os.getenv("RHEL_PG_PATH", f"/usr/pgsql-{pg_major_version}")

# Debian-specific configuration
deb_pgbin = os.getenv("DEB_PG_BIN_PATH", f"/usr/lib/postgresql/{pg_major_version}/bin")
deb_pg_path = os.getenv("DEB_PG_PATH", f"/usr/lib/postgresql/{pg_major_version}")
deb_pg_share_path = os.getenv("DEB_PG_SHARE_PATH", f"/usr/share/postgresql/{pg_major_version}")

# Anonymizer is a decoupled package — same name on both RHEL and DEB
anonymizer_package = os.getenv("ANONYMIZER_PACKAGE", "pgedge-anonymizer")

# In integration mode the package under test is exactly the requested (validated) package.
if INTEGRATION_REQUEST is not None:
    anonymizer_package = INTEGRATION_REQUEST["package_name"]

# The binary the pgedge-anonymizer package installs (pgEdge/pgedge-anonymizer pkg/rpm/anonymizer.spec
# %install and %files; pkg/deb/debian/rules override_dh_auto_install).
ANONYMIZER_BINARY = "/usr/bin/pgedge-anonymizer"

# Decoupled components SBOM path
decoupled_sbom_path = os.getenv("DECOUPLED_COMPONENTS_SBOM", "")

# Additional configuration for extension tests
check_extensions = os.getenv("CHECK_EXTENSIONS", "false").lower() == "true"
base_extensions = [ext.strip() for ext in os.getenv("BASE_EXTENSIONS", "").split(",") if ext.strip()]


def get_container_config(container_type):
    """Get configuration based on container type (rhel or deb)"""
    if container_type == "rhel":
        return {
            "pgbin": rhel_pgbin.rstrip('/'),
            "pguser": rhel_pguser,
            "anonymizer_package": anonymizer_package,
        }
    else:  # deb
        return {
            "pgbin": deb_pgbin.rstrip('/'),
            "pguser": deb_pguser,
            "anonymizer_package": anonymizer_package,
        }


def integration_problem(request):
    """Why this test cannot serve ``request`` (None when it can, or in standalone mode): it must ask
    for the anonymizer component and for one of anonymizer's packages in the shared policy. The
    request's PG major is not checked: this test never uses PostgreSQL, so every PG major's leg runs
    the same package checks. Checked again before anything is installed."""
    if request is None:
        return None
    if request["component"] != "anonymizer":
        return f"component {request['component']!r} is not anonymizer"
    allowed = _pr.COMPONENT_PACKAGES.get("anonymizer", ())
    if request["package_name"] not in allowed:
        return (f"package {request['package_name']!r} is not an anonymizer package "
                f"(expected one of {list(allowed)})")
    return None


INTEGRATION_PROBLEM = integration_problem(INTEGRATION_REQUEST)


def installed_anonymizer_packages(container, container_type):
    """Installed packages that belong to anonymizer, read from the package manager: the names
    anonymizer's policy entry accepts, and any other pgedge-anonymizer* package (a debug or renamed
    companion, for example). Returns None if the query fails."""
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
    allowed = _pr.COMPONENT_PACKAGES.get("anonymizer", ())
    return sorted(n for n in names if n in allowed or n.startswith("pgedge-anonymizer"))


# A release tag: v<major>.<minor>.<patch>, optionally -<suffix> (rc.1, beta.2, test1).
_ANONYMIZER_TAG = re.compile(r"v(\d+\.\d+\.\d+)(?:-([0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?")


def expected_anonymizer_binary_version(request):
    """``(version, problems)``: the version `pgedge-anonymizer version` must print for ``request``, or
    None with the reasons it cannot be derived.

    This is the rule test_pep_radar.expected_radar_binary_version applies to radar; anonymizer's
    release build (pgEdge/pgedge-anonymizer at 0ba7eef) follows the same scheme:
      * binary: .goreleaser.yaml stamps -X .../internal/version.Version={{.Version}}, GoReleaser's tag
        without the leading "v", prerelease punctuation kept (v1.0.1 -> 1.0.1, v1.0.1-rc.1 ->
        1.0.1-rc.1), and cmd/pgedge-anonymizer/cmd/root.go prints
        "pgedge-anonymizer <version> (built <build time>)". This holds for a real tag build; a
        simulate_tag (--snapshot) build is stamped "<incpatch>-next" and publishes nothing to install;
      * the package versions come from pgedge-parse-release-tag (version = the tag's x.y.z; build
        number = <suffix without dots>_1, or 1 for a final release): RPM pkg/rpm/anonymizer.spec
        Version=<version>, Release=<build number>.<dist>; DEB pkg/common.sh moves the part of the
        build number before "_" into the version after "~" and pkg/build-deb.sh writes
        <version>[~<suffix without dots>]-1.<codename>.
    The other release facts in the request must agree with the tag; a request whose facts disagree is
    reported, not trusted."""
    tag = request.get("effective_tag")
    if not tag:
        return None, ["effective_tag is required to derive the expected anonymizer version"]
    match = _ANONYMIZER_TAG.fullmatch(tag)
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


def anonymizer_binary_owners(container, container_type):
    """``(owners, raw)``: the packages the package manager says own the anonymizer binary (sorted;
    [] when it is unowned or the query fails) and the raw answer."""
    if container_type == "rhel":
        cmd = ["rpm", "-qf", "--queryformat", "%{NAME}\\n", ANONYMIZER_BINARY]
    else:
        cmd = ["dpkg-query", "-S", ANONYMIZER_BINARY]
    exit_code, output = container.exec_run(cmd, user="root")
    raw = output.decode().strip()
    if exit_code != 0:
        return [], raw
    if container_type == "rhel":
        return sorted({line.strip() for line in raw.splitlines() if line.strip()}), raw
    owners = set()
    for line in raw.splitlines():
        packages, sep, path = line.partition(": ")
        if sep and path.strip() == ANONYMIZER_BINARY:
            owners.update(p.strip().split(":", 1)[0] for p in packages.split(","))
    return sorted(owners), raw


# `pgedge-anonymizer version` prints one "pgedge-anonymizer <version> (built <build time>)" line on
# stdout (cmd/pgedge-anonymizer/cmd/root.go versionCmd). A release build stamps the build time with
# GoReleaser's {{.Date}}, the build's UTC time in RFC 3339 (.goreleaser.yaml ldflags); an unstamped
# build prints internal/version's defaults: a hand-set version that the owner bumps with each release
# (so it can equal the tag being certified) and "unknown". Only the build stamp tells them apart.
_ANONYMIZER_VERSION_LINE = re.compile(r"pgedge-anonymizer (\S+) \(built (\S+)\)")
_RFC3339 = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-](\d{2}):(\d{2}))",
                      re.ASCII)


def is_release_build_stamp(built):
    """True when ``built`` is an RFC 3339 date-time naming a real instant: ASCII digits in the
    shape above (fractional seconds of any length and a numeric offset allowed), a real calendar
    date and time, and an offset of at most 23:59. "2026-02-30T10:11:12Z" fails. Go never writes a
    leap second, so ":60" fails too. The fields are checked one by one because Python 3.9's
    datetime.fromisoformat reads neither "Z" nor every fraction length."""
    match = _RFC3339.fullmatch(built)
    if not match:
        return False
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    try:
        datetime(year, month, day, hour, minute, second)
    except ValueError:
        return False
    offset_hour, offset_minute = match.group(7), match.group(8)
    return offset_hour is None or (int(offset_hour) <= 23 and int(offset_minute) <= 59)


def parse_anonymizer_version(stdout):
    """``(version, built)`` from the one "pgedge-anonymizer <version> (built ...)" line of
    `pgedge-anonymizer version` stdout, as printed (a leading "v" or any suffix is kept, so the
    caller's exact comparison sees it). Only lines whose first word is "pgedge-anonymizer" are
    version lines; any other stdout line is ignored.

    Raises ValueError when there is no such line, more than one, the line is malformed, or the
    build time is not a release build's RFC 3339 stamp (is_release_build_stamp; an unstamped build
    prints "unknown", and its hand-set default version, which can equal the release being
    certified, must never pass)."""
    lines = [line.strip() for line in stdout.splitlines()
             if re.match(r"pgedge-anonymizer(\s|$)", line.strip())]
    if len(lines) != 1:
        raise ValueError(f"expected one 'pgedge-anonymizer <version> (built ...)' line on stdout, "
                         f"found {len(lines)}")
    match = _ANONYMIZER_VERSION_LINE.fullmatch(lines[0])
    if not match:
        raise ValueError(f"malformed version line {lines[0]!r}")
    version, built = match.group(1), match.group(2)
    if not is_release_build_stamp(built):
        raise ValueError(f"build time {built!r} is not a release build's RFC 3339 stamp")
    return version, built


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


# ============================================================================
# Test Functions
# ============================================================================

@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_install_prerequisites(container_name, container_type):
    """Step 0: Install prerequisites using machine_prereq_setup module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

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
    """Step 2: Install pgedge-anonymizer using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    anonymizer_package = config["anonymizer_package"]

    container = _running_container(container_name)

    if INTEGRATION_REQUEST is not None:
        # Execution-side backstop: never install for a request this test cannot serve.
        assert INTEGRATION_PROBLEM is None, f"integration request cannot be served: {INTEGRATION_PROBLEM}"
        requested = INTEGRATION_REQUEST["package_name"]
        # No prerequisite: the package checks below use no PostgreSQL server or other package, so the
        # requested package is the only install, exactly as the request decides (pinned + checksum
        # evidence).
        print(f"\n--- Installing requested {requested} on {container_name} ---")
        pep_integration_mode.install_for_request(container, INTEGRATION_REQUEST)
        present = installed_anonymizer_packages(container, container_type)
        assert present == [requested], (
            f"expected exactly the requested anonymizer package {requested!r} to be installed, "
            f"found {present!r}")
        print(f"✅ Only {requested} is installed among anonymizer packages")
        return

    print(f"\n--- Installing {anonymizer_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.install_package(container, anonymizer_package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {anonymizer_package}: {str(e)}")


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

    config = get_container_config(container_type)
    anonymizer_package = config["anonymizer_package"]

    container = _running_container(container_name)

    print(f"\n--- Upgrading {anonymizer_package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    try:
        success, platform, message = package_management.upgrade_package(container, anonymizer_package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{anonymizer_package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {anonymizer_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Check the package version using package_management module"""
    if INTEGRATION_REQUEST is not None:
        pytest.skip("integration mode: superseded by test_anonymizer_identity, which checks the exact "
                    "requested version")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not anonymizer_version:
        pytest.skip("No PGEDGE_ANONYMIZER_VERSION defined in env, skipping version check")

    config = get_container_config(container_type)
    anonymizer_package = config["anonymizer_package"]

    container = _running_container(container_name)

    print(f"\n--- Verifying {anonymizer_package} version on {container_name} ({container_type}) ---")

    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, anonymizer_package, anonymizer_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {anonymizer_package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_anonymizer_identity(container_name, container_type):
    """Integration mode: one consolidated identity verdict for the requested anonymizer package
    (install-before-identity, then the exact package-manager version). The shared binary probe
    (`<binary> -version`, read as a "Version:" line) cannot read anonymizer, which has a `version`
    subcommand and no version flag, so no binary is passed here; test_binary_version checks the
    binary's owner and exact version. The evidence is persisted before any failure. Skipped in
    standalone mode."""
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
    """Verify bundled files for each component match expected files"""
    container_name = container_name.strip()

    if not container_name:
        pytest.skip("Invalid container")

    container = _running_container(container_name)

    config = get_container_config(container_type)
    actual_package = config["anonymizer_package"]

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
    """Verify SBOM signature files located under the decoupled components SBOM directory"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not decoupled_sbom_path:
        # Both anonymizer packages ship the signed SBOM (anonymizer.spec %files; debian/rules), so an
        # integration run without its location fails instead of skipping.
        _skip_or_fail("DECOUPLED_COMPONENTS_SBOM not defined in env, skipping SBOM verification")

    container = _running_container(container_name)

    sbom_dir = f"{decoupled_sbom_path}/{anonymizer_package}"

    if container_type == "rhel":
        print(f"\n--- Verifying SBOM on {container_name} (RHEL) in {sbom_dir} ---")

        exit_code, output = container.exec_run(
            f"wget -q -O {sbom_dir}/pgedge-rsa.pub https://dnf.pgedge.com/keys/pgedge-rsa.pub",
            user="root",
        )
        assert exit_code == 0, f"Failed to download pgedge-rsa.pub: {output.decode()}"
        print(f"✅ Downloaded pgedge-rsa.pub to {sbom_dir}")

        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"--signature-file {anonymizer_package}-sbom.json.asc "
            f"--signer-file pgedge-rsa.pub "
            f"{anonymizer_package}-sbom.json'",
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

        machine_prereq_setup.ensure_sq_installed(container)
        _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
        _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
        _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"
        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"{_sq_signer_flag} /etc/apt/keyrings/pgedge-rsa.gpg "
            f"{_sq_sig_flag} {anonymizer_package}-sbom.json.asc "
            f"{anonymizer_package}-sbom.json'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good signature.' or '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_binary_version(container_name, container_type):
    """Verify the version `/usr/bin/pgedge-anonymizer version` reports (the binary
    has a `version` subcommand and no --version flag).

    Expected output format:
      pgedge-anonymizer <version> (built <timestamp>)

    Standalone (unchanged): the output must contain PGEDGE_ANONYMIZER_VERSION,
    and the check skips when it is unset. In integration mode the configured
    value is ignored: /usr/bin/pgedge-anonymizer must first belong to the
    requested package, so the probed binary is the one under test. Then stdout
    must hold exactly one version line (a line whose first word is
    "pgedge-anonymizer"; other stdout lines and stderr are ignored) whose
    version is the request's effective tag without the "v"
    (expected_anonymizer_binary_version, which cross-checks the other release
    facts) and whose build time is a release build's RFC 3339 stamp. The match
    is exact: "1.0.1" fails on any other build ("v1.0.1", "1.0.1-test1",
    "1.0.2-next"), and an unstamped build, which reports the source defaults
    (internal/version/version.go, bumped with each release) and "unknown",
    never passes, even when that default equals the tag.
    """
    if INTEGRATION_REQUEST is not None:
        expected_version, problems = expected_anonymizer_binary_version(INTEGRATION_REQUEST)
        if problems:
            pytest.fail("cannot derive the expected anonymizer version from the request: "
                        + "; ".join(problems))

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if INTEGRATION_REQUEST is None and not anonymizer_version:
        pytest.skip("No PGEDGE_ANONYMIZER_VERSION defined in env, skipping binary version check")

    container = _running_container(container_name)

    binary = ANONYMIZER_BINARY

    if INTEGRATION_REQUEST is not None:
        requested = INTEGRATION_REQUEST["package_name"]
        owners, raw = anonymizer_binary_owners(container, container_type)
        assert owners == [requested], (
            f"{binary} is owned by {owners or 'no package'}, not only the requested "
            f"{requested!r} (package manager said: {raw!r})")
        print(f"✅ {binary} belongs to {requested}")

        print(f"\n--- Checking {binary} version on {container_name} ({container_type}) ---")
        # demux returns stdout and stderr separately; without it docker merges them. stderr is only
        # printed and quoted, never parsed, so bytes that are not UTF-8 are escaped rather than
        # failing the check; stdout is still decoded strictly.
        exit_code, (stdout, stderr) = container.exec_run([binary, "version"], user="root", demux=True)
        stdout = (stdout or b"").decode()
        stderr = (stderr or b"").decode(errors="backslashreplace")
        print(f"   stdout: {stdout.strip()}")
        if stderr.strip():
            print(f"   stderr: {stderr.strip()}")
        assert exit_code == 0, (
            f"{binary} version failed with exit code {exit_code}:\n"
            f"  stdout: {stdout!r}\n  stderr: {stderr!r}")
        try:
            reported_version, built = parse_anonymizer_version(stdout)
        except ValueError as e:
            pytest.fail(f"Cannot read the anonymizer version: {e}\n"
                        f"  stdout: {stdout!r}\n  stderr: {stderr!r}")
        assert reported_version == expected_version, (
            f"anonymizer version mismatch: expected exactly '{expected_version}', got "
            f"'{reported_version}' (from effective_tag {INTEGRATION_REQUEST['effective_tag']!r})")
        print(f"✅ {binary} reports version {reported_version}, built {built} "
              f"(expected {expected_version})")
        return

    print(f"\n--- Checking {binary} version on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"{binary} version 2>&1"],
        user="root",
    )
    assert exit_code == 0, f"'{binary} version' failed: {output.decode().strip()}"

    version_output = output.decode().strip()
    print(f"   Output: {version_output}")

    assert anonymizer_version in version_output, (
        f"Expected version '{anonymizer_version}' not found in binary output:\n  {version_output}"
    )
    print(f"✅ pgedge-anonymizer reports version {anonymizer_version}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_binary_stripped(container_name, container_type):
    """Verify that /usr/bin/pgedge-anonymizer is a stripped ELF binary.

    Runs `file -b /usr/bin/pgedge-anonymizer` and requires an ELF binary that
    `file` reports as stripped and not as "not stripped" (the release build
    links with -s -w, .goreleaser.yaml). A substring match on "stripped" is not
    enough: "not stripped" contains it.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    binary = ANONYMIZER_BINARY
    print(f"\n--- Checking ELF strip status of {binary} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(["file", "-b", binary], user="root")
    file_output = output.decode().strip()
    assert exit_code == 0, f"'file -b {binary}' failed: {file_output}"
    print(f"   Output: {file_output}")

    problem = elf_strip_problem(file_output)
    assert problem is None, (
        f"Binary {binary} is not a stripped ELF binary: {problem}.\n"
        f"'file' output: {file_output}"
    )
    print(f"✅ {binary} is stripped")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_package_uninstall(container_name, container_type):
    """Uninstall anonymizer package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    anonymizer_package = config["anonymizer_package"]

    container = _running_container(container_name)

    print(f"\n--- Uninstalling {anonymizer_package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.uninstall_package(container, anonymizer_package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {anonymizer_package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Full cleanup using machine_cleanup module: remove all pgedge packages + leftover data"""
    if skip_cleanup:
        pytest.skip("Skipping cleanup: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _running_container(container_name)

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")

    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata=None, pguser=None
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")

"""Component tests for Coldfront.

Coldfront ships five packages, split across two packaging styles:

  Coupled to the PostgreSQL major version
    - pgedge-coldfront_{PG}   / pgedge-postgresql-{PG}-coldfront   (extension: coldfront)
    - pgedge-pg-duckdb_{PG}   / pgedge-postgresql-{PG}-pg-duckdb   (extension: pg_duckdb)

  Decoupled (identical RHEL and DEB package names)
    - pgedge-coldfront                    (archiver / compactor / partitioner binaries)
    - pgedge-coldfront-duckdb-extensions  (bundled .duckdb_extension files)
    - pgedge-lakekeeper                   (lakekeeper binary + systemd unit)

Because the packages differ in layout, versioning and which artifacts they
bundle, the per-package facts live in COLDFRONT_PACKAGES below and every
package-scoped test is parametrized over it. Nothing here is derived by
convention — the coupled pgedge-coldfront_{PG} and the decoupled
pgedge-coldfront would otherwise collide on the same derived short name.
"""
import os
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

load_dotenv()
client = docker.from_env()

# Load values from env
rhel_containers = [c.strip() for c in os.getenv("CONTAINERS", "").split(",") if c.strip()]
deb_containers = [c.strip() for c in os.getenv("DEB_CONTAINERS", "").split(",") if c.strip()]

# Combine all containers with their type
all_containers = [(c, "rhel") for c in rhel_containers] + [(c, "deb") for c in deb_containers]

# Filter containers based on platform filter (if set)
platform_filter = os.getenv("PLATFORM_FILTER", "").lower()
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
pg_major_version = os.getenv("PG_MAJOR_VERSION", "18")

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

# Extension checks
check_extensions = os.getenv("CHECK_EXTENSIONS", "true").lower() == "true"

# Resolved package names
rhel_coldfront_ext_package = os.getenv("COLDFRONT_PACKAGE", f"pgedge-coldfront_{pg_major_version}")
deb_coldfront_ext_package = os.getenv("DEB_COLDFRONT_PACKAGE", f"pgedge-postgresql-{pg_major_version}-coldfront")
rhel_pg_duckdb_package = os.getenv("PG_DUCKDB_PACKAGE", f"pgedge-pg-duckdb_{pg_major_version}")
deb_pg_duckdb_package = os.getenv("DEB_PG_DUCKDB_PACKAGE", f"pgedge-postgresql-{pg_major_version}-pg-duckdb")
coldfront_server_package = os.getenv("COLDFRONT_SERVER_PACKAGE", "pgedge-coldfront")
duckdb_extensions_package = os.getenv("COLDFRONT_DUCKDB_EXTENSIONS_PACKAGE", "pgedge-coldfront-duckdb-extensions")
lakekeeper_package = os.getenv("LAKEKEEPER_PACKAGE", "pgedge-lakekeeper")

# Package versions
coldfront_ext_version = os.getenv(f"PGEDGE_COLDFRONT_{pg_major_version}_VERSION", "1.0.0-beta2")
pg_duckdb_version = os.getenv(f"PGEDGE_PG_DUCKDB_{pg_major_version}_VERSION", "1.5.4-beta2")
coldfront_server_version = os.getenv("PGEDGE_COLDFRONT_SERVER_VERSION", "1.0.0-beta2")
duckdb_extensions_version = os.getenv("PGEDGE_COLDFRONT_DUCKDB_EXTENSIONS_VERSION", "1.5.4-beta2")
# lakekeeper's version string differs by packaging format: RPM uses '-beta2',
# DEB uses '~beta2'. Keep them separate rather than normalizing.
lakekeeper_rhel_version = os.getenv("PGEDGE_LAKEKEEPER_VERSION", "0.13.1-beta2")
lakekeeper_deb_version = os.getenv("PGEDGE_LAKEKEEPER_DEB_VERSION", "0.13.1~beta2")

# Extension versions as reported by \dx — these track each extension's
# default_version from its .control file, not the package version.
coldfront_extension_version = os.getenv("COLDFRONT_EXTENSION_VERSION", "1.0")
pg_duckdb_extension_version = os.getenv("PG_DUCKDB_EXTENSION_VERSION", "1.0.0")


# ============================================================================
# Per-package definitions
# ============================================================================
# Each entry carries everything that varies between the five packages:
#   key           stable id used in test ids
#   rhel / deb    package name per platform
#   expected      expected-output filename (explicit — derivation is ambiguous
#                 for the coldfront pair, see aspects/file_management.py)
#   version       expected package version per platform
#   license       LICENSE path per platform, or None when the package ships none
#   readme        README path per platform, or None when the package ships none
#   sbom          (sbom_json, containing_dir) per platform, or None
#   extensions    PostgreSQL extensions provided by the package
#   binaries      standalone binaries shipped in the package

COLDFRONT_PACKAGES = [
    {
        "key": "coldfront-ext",
        "rhel": rhel_coldfront_ext_package,
        "deb": deb_coldfront_ext_package,
        "expected": "coldfront-ext",
        "version": {"rhel": coldfront_ext_version, "deb": coldfront_ext_version},
        "license": {
            "rhel": f"/usr/share/licenses/{rhel_coldfront_ext_package}/LICENSE.md",
            "deb": None,
        },
        "readme": {
            "rhel": f"/usr/share/doc/{rhel_coldfront_ext_package}/README.md",
            "deb": f"/usr/share/doc/{deb_coldfront_ext_package}/README.md.gz",
        },
        "sbom": {
            "rhel": f"{rhel_pg_path}/sbom/coldfront-sbom.json",
            "deb": f"{deb_pg_path}/sbom/coldfront-sbom.json",
        },
        "extensions": ["coldfront"],
        "binaries": [],
    },
    {
        "key": "pg-duckdb",
        "rhel": rhel_pg_duckdb_package,
        "deb": deb_pg_duckdb_package,
        "expected": "pg-duckdb",
        "version": {"rhel": pg_duckdb_version, "deb": pg_duckdb_version},
        "license": {
            # NOTE: RPM ships a bare 'LICENSE' here, not 'LICENSE.md' like the
            # other coldfront packages. The DEB package ships no license file.
            "rhel": f"/usr/share/licenses/{rhel_pg_duckdb_package}/LICENSE",
            "deb": None,
        },
        "readme": {
            # The RPM ships only the /usr/share/doc/<pkg> directory, no README.
            "rhel": None,
            "deb": f"/usr/share/doc/{deb_pg_duckdb_package}/README.md.gz",
        },
        "sbom": {
            "rhel": f"{rhel_pg_path}/sbom/pg_duckdb-sbom.json",
            "deb": f"{deb_pg_path}/sbom/pg_duckdb-sbom.json",
        },
        "extensions": ["pg_duckdb"],
        "binaries": [],
    },
    {
        "key": "coldfront-server",
        "rhel": coldfront_server_package,
        "deb": coldfront_server_package,
        "expected": "coldfront",
        "version": {"rhel": coldfront_server_version, "deb": coldfront_server_version},
        "license": {
            "rhel": f"/usr/share/licenses/{coldfront_server_package}/LICENSE.md",
            "deb": None,
        },
        "readme": {
            "rhel": f"/usr/share/doc/{coldfront_server_package}/README.md",
            "deb": f"/usr/share/doc/{coldfront_server_package}/README.md.gz",
        },
        "sbom": {
            "rhel": f"/usr/share/{coldfront_server_package}/coldfront-sbom.json",
            "deb": f"/usr/share/{coldfront_server_package}/coldfront-sbom.json",
        },
        "extensions": [],
        "binaries": [
            b.strip() for b in os.getenv(
                "COLDFRONT_SERVER_BINARIES",
                "/usr/bin/archiver,/usr/bin/compactor,/usr/bin/partitioner",
            ).split(",") if b.strip()
        ],
    },
    {
        "key": "coldfront-duckdb-extensions",
        "rhel": duckdb_extensions_package,
        "deb": duckdb_extensions_package,
        "expected": "coldfront-duckdb-extensions",
        "version": {"rhel": duckdb_extensions_version, "deb": duckdb_extensions_version},
        "license": {
            "rhel": f"/usr/share/licenses/{duckdb_extensions_package}/LICENSE.md",
            "deb": None,
        },
        "readme": {
            "rhel": f"/usr/share/doc/{duckdb_extensions_package}/README.md",
            "deb": f"/usr/share/doc/{duckdb_extensions_package}/README.md.gz",
        },
        "sbom": {
            "rhel": f"/usr/share/{duckdb_extensions_package}/coldfront-duckdb-extensions-sbom.json",
            "deb": f"/usr/share/{duckdb_extensions_package}/coldfront-duckdb-extensions-sbom.json",
        },
        "extensions": [],
        "binaries": [],
    },
    {
        "key": "lakekeeper",
        "rhel": lakekeeper_package,
        "deb": lakekeeper_package,
        "expected": "lakekeeper",
        "version": {"rhel": lakekeeper_rhel_version, "deb": lakekeeper_deb_version},
        "license": {
            # lakekeeper puts its license under /usr/share/doc (gzipped) on both
            # platforms, unlike the other RPMs which use /usr/share/licenses.
            "rhel": f"/usr/share/doc/{lakekeeper_package}/LICENSE.gz",
            "deb": f"/usr/share/doc/{lakekeeper_package}/LICENSE.gz",
        },
        "readme": {
            "rhel": f"/usr/share/doc/{lakekeeper_package}/README.md",
            "deb": f"/usr/share/doc/{lakekeeper_package}/README.md",
        },
        "sbom": {
            "rhel": f"/usr/share/{lakekeeper_package}/lakekeeper-sbom.json",
            "deb": f"/usr/share/{lakekeeper_package}/lakekeeper-sbom.json",
        },
        "extensions": [],
        "binaries": [
            b.strip() for b in os.getenv("LAKEKEEPER_BINARY", "/usr/bin/lakekeeper").split(",")
            if b.strip()
        ],
    },
]


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


def package_for(pkg, container_type):
    """Resolve the platform-specific package name for a COLDFRONT_PACKAGES entry."""
    return pkg["rhel"] if container_type == "rhel" else pkg["deb"]


def _combinations(predicate=None):
    """Build (container_name, container_type, pkg) tuples for parametrization.

    predicate, when given, filters which packages participate — used so that
    e.g. the binary tests only generate ids for packages that ship binaries.
    """
    combos = []
    for container_name, container_type in all_containers:
        for pkg in COLDFRONT_PACKAGES:
            if predicate and not predicate(pkg, container_type):
                continue
            combos.append((container_name, container_type, pkg))
    return combos


def _ids(combos):
    """Readable pytest ids: <container>-<package key>."""
    return [f"{c}-{p['key']}" for c, _t, p in combos]


all_package_combinations = _combinations()
package_ids = _ids(all_package_combinations)

# Extension combinations: (container, type, extension_name, expected_version)
all_extension_combinations = []
for _cname, _ctype in all_containers:
    for _pkg in COLDFRONT_PACKAGES:
        for _ext in _pkg["extensions"]:
            _expected = (
                coldfront_extension_version if _ext == "coldfront" else pg_duckdb_extension_version
            )
            all_extension_combinations.append((_cname, _ctype, _ext, _expected))
extension_ids = [f"{c}-{e}" for c, _t, e, _v in all_extension_combinations]

# Binary combinations: (container, type, binary_path, expected_version)
all_binary_combinations = []
for _cname, _ctype in all_containers:
    for _pkg in COLDFRONT_PACKAGES:
        for _bin in _pkg["binaries"]:
            all_binary_combinations.append((_cname, _ctype, _bin, _pkg["version"][_ctype]))
binary_ids = [f"{c}-{os.path.basename(b)}" for c, _t, b, _v in all_binary_combinations]


def _get_container(container_name):
    """Fetch a running container, skipping the test when unavailable."""
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")
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


@pytest.mark.parametrize("container_name,container_type,pkg", all_package_combinations, ids=package_ids)
def test_component_install(container_name, container_type, pkg):
    """Step 2: Install each coldfront package using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    package = package_for(pkg, container_type)
    container = _get_container(container_name)

    print(f"\n--- Installing {package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.install_package(container, package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,pkg", all_package_combinations, ids=package_ids)
def test_component_upgrade(container_name, container_type, pkg):
    """Upgrade each coldfront package if UPGRADE=true"""
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    package = package_for(pkg, container_type)
    container = _get_container(container_name)

    print(f"\n--- Upgrading {package} on {container_name} ({container_type}) ---")

    # Switch to upgrade repo if needed
    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    try:
        success, platform, message = package_management.upgrade_package(container, package)
        if not success:
            if "already" in message.lower() or "newest" in message.lower():
                pytest.skip(f"{package} is already at newest version")
        assert success, f"Package upgrade failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to upgrade {package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,pkg", all_package_combinations, ids=package_ids)
def test_component_package_version(container_name, container_type, pkg):
    """Step 3: Check each package's version using package_management module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    package = package_for(pkg, container_type)
    expected_version = pkg["version"][container_type]
    if not expected_version:
        pytest.skip(f"No expected version configured for {package}, skipping version check")

    container = _get_container(container_name)

    print(f"\n--- Verifying {package} version on {container_name} ({container_type}) ---")

    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, package, expected_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {package} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,pkg", all_package_combinations, ids=package_ids)
def test_verify_bundled_files(container_name, container_type, pkg):
    """Verify bundled files for each coldfront package match expected files.

    Compares the installed file list (rpm -ql / dpkg -L) against
    expected-output/rpm/<expected> or expected-output/deb/<expected>.

    The expected filename is passed explicitly rather than derived: the coupled
    pgedge-coldfront_{PG} and the decoupled pgedge-coldfront both reduce to the
    same short name, so derivation alone cannot tell them apart.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("Invalid container")

    package = package_for(pkg, container_type)
    container = _get_container(container_name)

    project_root = Path(__file__).parent.parent

    try:
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=package,
            package_name=package,
            project_root=project_root,
            pg_major_version=pg_major_version,
            expected_name=pkg["expected"],
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
        if "No expected file found" in str(e):
            pytest.skip(str(e))
        else:
            pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,pkg", all_package_combinations, ids=package_ids)
def test_verify_sbom(container_name, container_type, pkg):
    """Verify each package's SBOM detached signature.

    Every coldfront package ships its own <name>-sbom.json plus a .asc detached
    signature. The coupled packages place them under the PostgreSQL tree; the
    decoupled ones under /usr/share/<package>/.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    sbom_json = pkg["sbom"][container_type]
    if not sbom_json:
        pytest.skip(f"{pkg['key']} ships no SBOM on {container_type}")

    container = _get_container(container_name)

    sbom_dir = os.path.dirname(sbom_json)
    sbom_file = os.path.basename(sbom_json)

    print(f"\n--- Verifying SBOM {sbom_json} on {container_name} ({container_type}) ---")

    machine_prereq_setup.ensure_sq_installed(container)
    _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
    _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
    _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"

    if container_type == "rhel":
        # Download the pgEdge signing key alongside the SBOM
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
        f"{_sq_sig_flag} {sbom_file}.asc "
        f"{sbom_file}'",
        user="root",
    )
    output_str = output.decode().replace('\xa0', ' ')
    assert exit_code == 0, f"SBOM verification failed for {sbom_json}: {output_str}"
    assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
        f"Expected a good/authenticated signature for {sbom_json}, got:\n{output_str}"
    print(f"✅ SBOM signature verified for {sbom_json}")
    print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type,pkg", all_package_combinations, ids=package_ids)
def test_verify_license_file(container_name, container_type, pkg):
    """Verify the package ships its LICENSE file at the expected path.

    Paths are not uniform across coldfront: most RPMs use
    /usr/share/licenses/<pkg>/LICENSE.md, pg-duckdb uses a bare LICENSE, and
    lakekeeper ships a gzipped LICENSE under /usr/share/doc/. Packages with no
    license file recorded for a platform are skipped rather than failed.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    license_path = pkg["license"][container_type]
    if not license_path:
        pytest.skip(f"{pkg['key']} ships no LICENSE file on {container_type}")

    container = _get_container(container_name)
    package = package_for(pkg, container_type)

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


@pytest.mark.parametrize("container_name,container_type,pkg", all_package_combinations, ids=package_ids)
def test_verify_readme_file(container_name, container_type, pkg):
    """Verify the package ships its README at the expected path.

    DEB packages gzip the README (README.md.gz) while RPMs ship it plain, and
    the pg-duckdb RPM ships no README at all — those cases are skipped.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    readme_path = pkg["readme"][container_type]
    if not readme_path:
        pytest.skip(f"{pkg['key']} ships no README file on {container_type}")

    container = _get_container(container_name)
    package = package_for(pkg, container_type)

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
def test_init_cluster(container_name, container_type):
    """Initialize a PostgreSQL cluster preloading the coldfront libraries"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    print(f"\n--- Initializing cluster on {container_name} ---")

    # Both coldfront and pg_duckdb need to be preloaded before CREATE EXTENSION
    guc_parameters = {
        "shared_preload_libraries": "'coldfront,pg_duckdb'",
        "wal_level": "logical",
        "max_replication_slots": "10",
        "max_wal_senders": "10",
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
        pytest.fail(f"Failed to start PostgreSQL server: {str(e)}")


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


@pytest.mark.parametrize(
    "container_name,container_type,extension,expected_version",
    all_extension_combinations, ids=extension_ids
)
def test_create_extensions(container_name, container_type, extension, expected_version):
    """Create each coldfront-provided extension (coldfront, pg_duckdb)"""
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    extension = extension.strip()

    if not container_name or not extension:
        pytest.skip("Invalid container or extension")

    container = _get_container(container_name)

    config = get_container_config(container_type)
    pgbin = config["pgbin"]
    pguser = config["pguser"]

    normalized_ext = f'"{extension}"' if "-" in extension else extension

    print(f"\n--- Creating extension {normalized_ext} in {container_name} ---")

    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d postgres "
        f"-c 'CREATE EXTENSION IF NOT EXISTS {normalized_ext} CASCADE;'",
        user=pguser,
    )

    assert exit_code == 0, f"Failed to create {normalized_ext}: {output.decode()}"
    print(f"✅ Successfully created extension {normalized_ext}")


@pytest.mark.parametrize(
    "container_name,container_type,extension,expected_version",
    all_extension_combinations, ids=extension_ids
)
def test_extension_version(container_name, container_type, extension, expected_version):
    """Verify each extension's installed version via \\dx in psql.

    Compares against the extension's default_version (from its .control file),
    which is distinct from the package version — e.g. the coldfront package is
    1.0.0-beta2 while the coldfront extension reports 1.0.
    """
    if not check_extensions:
        pytest.skip("Extension check disabled via env")

    container_name = container_name.strip()
    extension = extension.strip()

    if not container_name or not extension:
        pytest.skip("Invalid container or extension")

    if not expected_version:
        pytest.skip(f"No expected extension version configured for {extension}")

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

    assert expected_version in installed_version, (
        f"Extension '{extension}' version mismatch: "
        f"expected '{expected_version}', got '{installed_version}'"
    )
    print(f"✅ Extension '{extension}' version {installed_version} matches expected {expected_version}")


@pytest.mark.parametrize(
    "container_name,container_type,binary,expected_version",
    all_binary_combinations, ids=binary_ids
)
def test_binary_version(container_name, container_type, binary, expected_version):
    """Verify each shipped binary reports its expected version string.

    Covers archiver / compactor / partitioner (pgedge-coldfront) and lakekeeper.
    Both '<binary> --version' and '<binary> version' are attempted, since the
    coldfront binaries and lakekeeper do not share a CLI convention.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not expected_version:
        pytest.skip(f"No expected version configured for {binary}")

    container = _get_container(container_name)

    print(f"\n--- Checking {binary} version on {container_name} ({container_type}) ---")

    attempts = []
    for flag in ("--version", "version"):
        exit_code, output = container.exec_run(
            ["bash", "-c", f"{binary} {flag} 2>&1"],
            user="root",
        )
        text = output.decode().strip()
        attempts.append(f"`{binary} {flag}` (exit {exit_code}): {text}")
        if exit_code == 0 and expected_version in text:
            print(f"   Output: {text}")
            print(f"✅ {binary} reports version {expected_version}")
            return

    pytest.fail(
        f"Expected version '{expected_version}' not reported by {binary}.\n"
        + "\n".join(attempts)
    )


@pytest.mark.parametrize(
    "container_name,container_type,binary,expected_version",
    all_binary_combinations, ids=binary_ids
)
def test_binary_stripped(container_name, container_type, binary, expected_version):
    """Verify each shipped binary is a stripped ELF binary.

    Runs 'file <binary>' and asserts the output contains 'stripped',
    confirming debug symbols were removed at build time.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)

    print(f"\n--- Checking ELF strip status of {binary} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"file {binary} 2>&1"],
        user="root",
    )
    assert exit_code == 0, f"'file {binary}' failed: {output.decode().strip()}"

    file_output = output.decode().strip()
    print(f"   Output: {file_output}")

    assert "stripped" in file_output.lower(), (
        f"Binary {binary} does not appear to be stripped.\n"
        f"'file' output: {file_output}"
    )
    print(f"✅ {binary} is stripped")


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


@pytest.mark.parametrize("container_name,container_type,pkg", all_package_combinations, ids=package_ids)
def test_package_uninstall(container_name, container_type, pkg):
    """Uninstall each coldfront package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    package = package_for(pkg, container_type)
    container = _get_container(container_name)

    print(f"\n--- Uninstalling {package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.uninstall_package(container, package)
        assert success, f"Package uninstallation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to uninstall {package}: {str(e)}")


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
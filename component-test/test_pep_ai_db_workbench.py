import os
import sys
from pathlib import Path

import pytest
import docker
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from aspects import configure_repository, package_management, machine_cleanup, machine_prereq_setup, file_management, container_management

load_dotenv()
client = docker.from_env()

# Container configuration
rhel_containers = [c.strip() for c in os.getenv("CONTAINERS", "").split(",") if c.strip()]
deb_containers = [c.strip() for c in os.getenv("DEB_CONTAINERS", "").split(",") if c.strip()]
all_containers = [(c, "rhel") for c in rhel_containers] + [(c, "deb") for c in deb_containers]

platform_filter = os.getenv("PLATFORM_FILTER", "").lower()
if platform_filter == "rpm":
    all_containers = [(c, t) for c, t in all_containers if t == "rhel"]
elif platform_filter == "deb":
    all_containers = [(c, t) for c, t in all_containers if t == "deb"]

# Common configuration
repo = os.getenv("REPO", "release")
upgrade_repo = os.getenv("UPGRADE_REPO", "staging")
skip_cleanup = os.getenv("SKIP_CLEANUP", "false").lower() == "true"

# AI DB Workbench package configuration (same names for RHEL and DEB — decoupled)
ai_db_workbench_server_package = os.getenv("AI_DBA_SERVER_PACKAGE", "pgedge-ai-dba-server")
ai_db_workbench_alerter_package = os.getenv("AI_DBA_ALERTER_PACKAGE", "pgedge-ai-dba-alerter")
ai_db_workbench_collector_package = os.getenv("AI_DBA_COLLECTOR_PACKAGE", "pgedge-ai-dba-collector")
ai_db_workbench_client_package = os.getenv("AI_DBA_CLIENT_PACKAGE", "pgedge-ai-dba-client")

# Install order: server first (base dependency), then dependents
ai_db_workbench_packages = [
    ai_db_workbench_server_package,
    ai_db_workbench_alerter_package,
    ai_db_workbench_collector_package,
    ai_db_workbench_client_package,
]

# Version shared across all packages
ai_db_workbench_version = os.getenv("PGEDGE_AI_DBA_VERSION", "1.0.0-alpha3")
ai_db_workbench_version_map = {pkg: ai_db_workbench_version for pkg in ai_db_workbench_packages}

# Binaries — client has no standalone binary
ai_db_workbench_binary_map = {
    ai_db_workbench_alerter_package: os.getenv("AI_DBA_ALERTER_BINARY", "/usr/bin/ai-dba-alerter"),
    ai_db_workbench_collector_package: os.getenv("AI_DBA_COLLECTOR_BINARY", "/usr/bin/ai-dba-collector"),
    ai_db_workbench_server_package: os.getenv("AI_DBA_SERVER_BINARY", "/usr/bin/ai-dba-server"),
}

# Systemd service files — client has no service
ai_db_workbench_service_map = {
    ai_db_workbench_alerter_package: "/lib/systemd/system/pgedge-ai-dba-alerter.service",
    ai_db_workbench_collector_package: "/lib/systemd/system/pgedge-ai-dba-collector.service",
    ai_db_workbench_server_package: "/lib/systemd/system/pgedge-ai-dba-server.service",
}

# SBOM file locations per package (server uses a subdirectory; others are flat under /usr/share)
ai_db_workbench_sbom_info = {
    ai_db_workbench_alerter_package: {
        "dir": "/usr/share/pgedge-ai-dba-alerter",
        "json_file": "pgedge-ai-dba-alerter-sbom.json",
        "asc_file": "pgedge-ai-dba-alerter-sbom.json.asc",
    },
    ai_db_workbench_client_package: {
        "dir": "/usr/share/pgedge-ai-dba-client",
        "json_file": "pgedge-ai-dba-client-sbom.json",
        "asc_file": "pgedge-ai-dba-client-sbom.json.asc",
    },
    ai_db_workbench_collector_package: {
        "dir": "/usr/share/pgedge-ai-dba-collector",
        "json_file": "pgedge-ai-dba-collector-sbom.json",
        "asc_file": "pgedge-ai-dba-collector-sbom.json.asc",
    },
    ai_db_workbench_server_package: {
        "dir": "/usr/share/pgedge-ai-dba-server",
        "json_file": "pgedge-ai-dba-server-sbom.json",
        "asc_file": "pgedge-ai-dba-server-sbom.json.asc",
    },
}

# Package-level dependency declarations
ai_db_workbench_dependency_map = {
    ai_db_workbench_alerter_package: [ai_db_workbench_server_package],
    ai_db_workbench_client_package: [ai_db_workbench_server_package],
    ai_db_workbench_collector_package: [ai_db_workbench_server_package],
    ai_db_workbench_server_package: [],
}


def generate_container_package_combinations():
    combinations = []
    for container_name, container_type in all_containers:
        for package in ai_db_workbench_packages:
            combinations.append((container_name, container_type, package))
    return combinations


all_container_package_combinations = generate_container_package_combinations()


def generate_container_binary_combinations():
    combinations = []
    for container_name, container_type in all_containers:
        for pkg, binary in ai_db_workbench_binary_map.items():
            combinations.append((container_name, container_type, pkg, binary))
    return combinations


all_container_binary_combinations = generate_container_binary_combinations()


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
    """Step 1: Configure the pgEdge repository using configure_repository module"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Configuring repository in {container_name} ---")

    try:
        success, platform, message = configure_repository.configure_pgedge_repository(container, repo)
        assert success, f"Repository configuration failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to configure repository: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_ai_db_workbench_install(container_name, container_type, package):
    """Step 2: Install AI DB Workbench package using package_management module"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Installing {package} on {container_name} ({container_type}) ---")

    try:
        success, platform, message = package_management.install_package(container, package)
        assert success, f"Package installation failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to install {package}: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_ai_db_workbench_upgrade(container_name, container_type, package):
    """Upgrade AI DB Workbench package if UPGRADE=true"""
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Upgrading {package} on {container_name} ({container_type}) ---")

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


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_ai_db_workbench_package_version(container_name, container_type, package):
    """Step 3: Verify AI DB Workbench package version"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    expected_version = ai_db_workbench_version_map.get(package, "")
    if not expected_version:
        pytest.skip(f"No version defined for {package}, skipping version check")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

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


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_verify_bundled_files(container_name, container_type, package):
    """Verify bundled files for each AI DB Workbench package match expected files

    Compares installed files from rpm/deb against expected-output/rpm/ or expected-output/deb/.
    """
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("Invalid container or package")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    project_root = Path(__file__).parent.parent

    try:
        success, details, message = file_management.verify_bundled_files(
            container=container,
            container_name=container_name,
            container_type=container_type,
            component=package,
            package_name=package,
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
        if "No expected file found" in str(e):
            pytest.skip(str(e))
        else:
            pytest.fail(f"Failed to verify bundled files: {str(e)}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_verify_sbom(container_name, container_type, package):
    """Verify SBOM signature files for each AI DB Workbench package"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    sbom_info = ai_db_workbench_sbom_info.get(package)
    if not sbom_info:
        pytest.skip(f"No SBOM info configured for {package}")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    sbom_dir = sbom_info["dir"]
    json_file = sbom_info["json_file"]
    asc_file = sbom_info["asc_file"]

    if container_type == "rhel":
        print(f"\n--- Verifying SBOM for {package} on {container_name} (RHEL) in {sbom_dir} ---")

        exit_code, output = container.exec_run(
            f"wget -q -O {sbom_dir}/pgedge-rsa.pub https://dnf.pgedge.com/keys/pgedge-rsa.pub",
            user="root",
        )
        assert exit_code == 0, f"Failed to download pgedge-rsa.pub: {output.decode()}"
        print(f"✅ Downloaded pgedge-rsa.pub to {sbom_dir}")

        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"--signature-file {asc_file} "
            f"--signer-file pgedge-rsa.pub "
            f"{json_file}'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed for {package}: {output_str}"
        assert "1 authenticated signature." in output_str, \
            f"Expected '1 authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified for {package} on {container_name} (RHEL)")
        print(f"   {output_str.strip()}")

    else:  # deb
        print(f"\n--- Verifying SBOM for {package} on {container_name} (Deb) in {sbom_dir} ---")

        machine_prereq_setup.ensure_sq_installed(container)
        _sq_rc, _sq_help = container.exec_run("sq verify --help 2>&1", user="root")
        _sq_signer_flag = "--signer-file" if b"--signer-file" in _sq_help else "--signer-cert"
        _sq_sig_flag = "--signature-file" if b"--signature-file" in _sq_help else "--detached"
        exit_code, output = container.exec_run(
            f"sh -c 'cd {sbom_dir} && sq verify "
            f"{_sq_signer_flag} /etc/apt/keyrings/pgedge-rsa.gpg "
            f"{_sq_sig_flag} {asc_file} "
            f"{json_file}'",
            user="root",
        )
        output_str = output.decode().replace('\xa0', ' ')
        assert exit_code == 0, f"SBOM verification failed for {package}: {output_str}"
        assert "1 good signature." in output_str or "1 authenticated signature." in output_str, \
            f"Expected '1 good/authenticated signature.' not found in output:\n{output_str}"
        print(f"✅ SBOM signature verified for {package} on {container_name} (Deb)")
        print(f"   {output_str.strip()}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_verify_license_file(container_name, container_type, package):
    """Verify LICENSE file is present at /usr/share/doc/<package>/LICENSE"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    license_path = f"/usr/share/doc/{package}/LICENSE.md"
    print(f"\n--- Verifying license file for {package} on {container_name} ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"test -f {license_path} && echo EXISTS || echo MISSING"],
        user="root",
    )
    result = output.decode().strip()
    assert result == "EXISTS", f"LICENSE file not found at {license_path} for {package}"
    print(f"✅ LICENSE file found at {license_path}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_verify_readme_file(container_name, container_type, package):
    """Verify README.md file is present at /usr/share/doc/<package>/README.md.gz"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    # RPM installs README.md; DEB compresses it as README.md.gz
    readme_path = (
        f"/usr/share/doc/{package}/README.md"
        if container_type == "rhel"
        else f"/usr/share/doc/{package}/README.md.gz"
    )
    print(f"\n--- Verifying README for {package} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"test -f {readme_path} && echo EXISTS || echo MISSING"],
        user="root",
    )
    result = output.decode().strip()
    assert result == "EXISTS", f"README not found at {readme_path} for {package}"
    print(f"✅ README found at {readme_path}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_verify_systemd_service(container_name, container_type, package):
    """Verify systemd service file is present for packages that ship a service unit"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    service_path = ai_db_workbench_service_map.get(package)
    if not service_path:
        pytest.skip(f"{package} does not ship a systemd service file")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying systemd service for {package} on {container_name} ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"test -f {service_path} && echo EXISTS || echo MISSING"],
        user="root",
    )
    result = output.decode().strip()
    assert result == "EXISTS", f"Systemd service file not found at {service_path} for {package}"
    print(f"✅ Systemd service file found at {service_path}")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_verify_package_dependencies(container_name, container_type, package):
    """Verify package-level dependencies are declared in package metadata"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    expected_deps = ai_db_workbench_dependency_map.get(package, [])
    if not expected_deps:
        pytest.skip(f"No dependency mapping defined for {package}")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Verifying package dependencies for {package} on {container_name} ({container_type}) ---")

    for dep in expected_deps:
        if container_type == "rhel":
            exit_code, output = container.exec_run(
                ["bash", "-c", f"rpm -qR {package} | grep -q '{dep}'"],
                user="root",
            )
        else:  # deb
            exit_code, output = container.exec_run(
                ["bash", "-c", f"dpkg -s {package} 2>/dev/null | grep -i Depends | grep -q '{dep}'"],
                user="root",
            )
        assert exit_code == 0, (
            f"Dependency '{dep}' not declared in {package} package metadata "
            f"on {container_name} ({container_type}). Output: {output.decode().strip()}"
        )
        print(f"✅ Dependency '{dep}' declared in {package}")


@pytest.mark.parametrize("container_name,container_type,package,binary_path", all_container_binary_combinations)
def test_binary_version(container_name, container_type, package, binary_path):
    """Verify each AI DB Workbench binary reports the expected version string"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    # ai-dba-server binary is not yet available; skip until it ships
    if "ai-dba-server" in binary_path:
        pytest.skip(f"Binary version check not available for {binary_path}")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Checking {binary_path} version on {container_name} ({container_type}) ---")

    # These binaries print version in the startup banner then exit non-zero when no
    # database is available — don't assert exit_code; just verify the version string.
    _, output = container.exec_run(
        ["bash", "-c", f"{binary_path} 2>&1; true"],
        user="root",
    )
    version_output = output.decode().strip()
    print(f"   Output: {version_output[:200]}")

    assert ai_db_workbench_version in version_output, (
        f"Expected version '{ai_db_workbench_version}' not found in binary output:\n  {version_output[:200]}"
    )
    print(f"✅ {binary_path} reports version {ai_db_workbench_version}")


@pytest.mark.parametrize("container_name,container_type,package,binary_path", all_container_binary_combinations)
def test_binary_stripped(container_name, container_type, package, binary_path):
    """Verify each AI DB Workbench binary is a stripped ELF binary

    Runs 'file <binary>' and asserts the output contains 'stripped',
    confirming debug symbols were removed at build time.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    print(f"\n--- Checking ELF strip status of {binary_path} on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", f"file {binary_path} 2>&1"],
        user="root",
    )
    assert exit_code == 0, f"'file {binary_path}' failed: {output.decode().strip()}"

    file_output = output.decode().strip()
    print(f"   Output: {file_output}")

    assert "stripped" in file_output.lower(), (
        f"Binary {binary_path} does not appear to be stripped.\n"
        f"'file' output: {file_output}"
    )
    print(f"✅ {binary_path} is stripped")


@pytest.mark.parametrize("container_name,container_type,package", all_container_package_combinations)
def test_ai_db_workbench_uninstall(container_name, container_type, package):
    """Uninstall AI DB Workbench package using package_management module"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

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

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

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
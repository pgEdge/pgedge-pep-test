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

# AI KB package configuration (same names for RHEL and DEB — decoupled).
# Each package ships one embedding-model knowledge-base database; there is no
# standalone binary, no PostgreSQL extension, and no systemd service.
ai_kb_gemini_package = os.getenv("AI_KB_GEMINI_PACKAGE", "pgedge-ai-kb-gemini-gemini-embedding-001")
ai_kb_ollama_package = os.getenv("AI_KB_OLLAMA_PACKAGE", "pgedge-ai-kb-ollama-nomic-embed-text")
ai_kb_openai_package = os.getenv("AI_KB_OPENAI_PACKAGE", "pgedge-ai-kb-openai-text-embedding-3-small")
ai_kb_voyage_package = os.getenv("AI_KB_VOYAGE_PACKAGE", "pgedge-ai-kb-voyage-voyage-3")

# Full component list — env override (AI_KB_COMPONENTS) wins, else the four defaults
_default_components = ",".join([
    ai_kb_gemini_package,
    ai_kb_ollama_package,
    ai_kb_openai_package,
    ai_kb_voyage_package,
])
ai_kb_packages = [c.strip() for c in os.getenv("AI_KB_COMPONENTS", _default_components).split(",") if c.strip()]

# Version shared across all packages
ai_kb_version = os.getenv("PGEDGE_AI_KB_VERSION", "1.0.0")
ai_kb_version_map = {pkg: ai_kb_version for pkg in ai_kb_packages}

# Decoupled components SBOM location — SBOM files live flat under this directory
# as <package>-sbom.json / <package>-sbom.json.asc
decoupled_sbom_path = os.getenv("DECOUPLED_COMPONENTS_SBOM", "/usr/share")


def generate_container_package_combinations():
    combinations = []
    for container_name, container_type in all_containers:
        for package in ai_kb_packages:
            combinations.append((container_name, container_type, package))
    return combinations


all_container_package_combinations = generate_container_package_combinations()


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
def test_ai_kb_install(container_name, container_type, package):
    """Step 2: Install AI KB package using package_management module"""
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
def test_ai_kb_upgrade(container_name, container_type, package):
    """Upgrade AI KB package if UPGRADE=true"""
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
def test_ai_kb_package_version(container_name, container_type, package):
    """Step 3: Verify AI KB package version"""
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    expected_version = ai_kb_version_map.get(package, "")
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
    """Verify bundled files for each AI KB package match expected files

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
    """Verify SBOM signature files for each AI KB package

    SBOM files live flat under DECOUPLED_COMPONENTS_SBOM (e.g. /usr/share) as
    <package>-sbom.json and <package>-sbom.json.asc.
    """
    container_name = container_name.strip()
    package = package.strip()

    if not container_name or not package:
        pytest.skip("No container or package defined")

    if not decoupled_sbom_path:
        pytest.skip("DECOUPLED_COMPONENTS_SBOM not defined in env, skipping SBOM verification")

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")

    assert container.status == "running"

    sbom_dir = decoupled_sbom_path
    json_file = f"{package}-sbom.json"
    asc_file = f"{package}-sbom.json.asc"

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
    """Verify LICENSE file is present at /usr/share/doc/<package>/LICENSE.md"""
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
    """Verify README file is present at /usr/share/doc/<package>/README.md (RPM) or README.md.gz (DEB)"""
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
def test_ai_kb_uninstall(container_name, container_type, package):
    """Uninstall AI KB package using package_management module"""
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
            container, pgdata=None, pguser=None  # AI KB packages don't use pgdata/pguser
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")

        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")
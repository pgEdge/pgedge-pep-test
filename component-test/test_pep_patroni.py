import os
import sys
from pathlib import Path

import pytest
import docker
from dotenv import load_dotenv

# Add the parent directory to sys.path to import from aspects
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from aspects import configure_repository, package_management, machine_cleanup, machine_prereq_setup, container_management

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
patroni_version = os.getenv("PGEDGE_PATRONI_VERSION", "4.1.0")

# User configuration
rhel_pguser = os.getenv("PG_USER", "postgres")
deb_pguser = os.getenv("DEB_PG_USER", "postgres")

# Package lists (one or more packages to install per platform)
rhel_packages = [
    p.strip()
    for p in os.getenv(
        "PATRONI_PACKAGE",
        "pgedge-patroni-consul,pgedge-patroni-etcd,pgedge-patroni-aws,pgedge-patroni-zookeeper",
    ).split(",")
    if p.strip()
]
deb_packages = [
    p.strip()
    for p in os.getenv("DEB_PATRONI_PACKAGE", "pgedge-patroni,pgedge-etcd").split(",")
    if p.strip()
]

# Binary lists to verify after installation
rhel_binaries = [
    b.strip()
    for b in os.getenv(
        "RHEL_PATRONI_BINARIES",
        "patroni,patroni_barman,patroni_raft_controller,patronictl,patroni_aws,consul,etcd,etcdctl,etcdutl",
    ).split(",")
    if b.strip()
]
deb_binaries = [
    b.strip()
    for b in os.getenv(
        "DEB_PATRONI_BINARIES",
        "patroni,patroni_barman,patroni_raft_controller,patronictl,patroni_aws,"
        "consul,etcd,etcdctl,etcdutl,patroni_wale_restore,pg_createconfig_patroni",
    ).split(",")
    if b.strip()
]


def get_container_config(container_type):
    """Return platform-specific configuration."""
    if container_type == "rhel":
        return {
            "pguser": rhel_pguser,
            "packages": rhel_packages,
            "binaries": rhel_binaries,
        }
    else:  # deb
        return {
            "pguser": deb_pguser,
            "packages": deb_packages,
            "binaries": deb_binaries,
        }


def _get_container(container_name):
    """Return a running container or skip the test if not found."""
    try:
        return client.containers.get(container_name)
    except docker.errors.NotFound:
        pytest.skip(f"Container {container_name} not found or not running.")


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
    assert container.status == "running", (
        f"Container {container_name} is not running (status: {container.status})"
    )

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
    """Step 1: Configure the pgEdge repository"""
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)
    assert container.status == "running"

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
    """Step 2: Install all Patroni packages for the platform.

    RHEL: packages from PATRONI_PACKAGE env var
    DEB:  packages from DEB_PATRONI_PACKAGE env var
    Each package is installed individually.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    packages = config["packages"]

    container = _get_container(container_name)
    assert container.status == "running"

    print(f"\n--- Installing Patroni packages on {container_name} ({container_type}) ---")
    print(f"   Packages: {packages}")

    failed = []
    for pkg in packages:
        try:
            success, platform, message = package_management.install_package(container, pkg)
            if success:
                print(f"   ✅ {pkg}: {message}")
            else:
                failed.append(f"{pkg}: {message}")
                print(f"   ❌ {pkg}: {message}")
        except Exception as e:
            failed.append(f"{pkg}: {str(e)}")
            print(f"   ❌ {pkg}: {str(e)}")

    assert not failed, "Some packages failed to install:\n" + "\n".join(failed)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_upgrade(container_name, container_type):
    """Upgrade all Patroni packages if UPGRADE=true"""
    if os.getenv("UPGRADE", "false").lower() != "true":
        pytest.skip("Skipping upgrade tests because UPGRADE=false in env")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    packages = config["packages"]

    container = _get_container(container_name)
    assert container.status == "running"

    print(f"\n--- Upgrading Patroni packages on {container_name} ({container_type}) ---")

    if upgrade_repo in ["staging", "daily"]:
        try:
            configure_repository.configure_pgedge_repository(container, upgrade_repo)
        except Exception as e:
            print(f"Warning: Could not switch to upgrade repo: {e}")

    failed = []
    for pkg in packages:
        try:
            success, platform, message = package_management.upgrade_package(container, pkg)
            if not success:
                if "already" in message.lower() or "newest" in message.lower():
                    print(f"   ℹ️  {pkg}: already at newest version")
                    continue
                failed.append(f"{pkg}: {message}")
            else:
                print(f"   ✅ {pkg}: {message}")
        except Exception as e:
            failed.append(f"{pkg}: {str(e)}")

    assert not failed, "Some packages failed to upgrade:\n" + "\n".join(failed)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_component_package_version(container_name, container_type):
    """Step 3: Verify installed package version matches PGEDGE_PATRONI_VERSION.

    Checks the first package in the platform's package list via the package manager.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not patroni_version:
        pytest.skip("No PGEDGE_PATRONI_VERSION defined in env, skipping version check")

    config = get_container_config(container_type)
    # Use the first package as the canonical version carrier (e.g. pgedge-patroni)
    pkg = config["packages"][0]

    container = _get_container(container_name)
    assert container.status == "running"

    print(f"\n--- Verifying {pkg} version on {container_name} ({container_type}) ---")
    try:
        success, platform, installed_version, message = package_management.verify_package_version(
            container, pkg, patroni_version
        )
        assert success, f"Version verification failed: {message}"
        print(f"✅ {message}")
        print(f"✅ Platform detected: {platform}")
    except Exception as e:
        pytest.fail(f"Failed to verify {pkg} version: {str(e)}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_patroni_binaries_exist(container_name, container_type):
    """Step 4: Verify that all expected Patroni binaries are present and executable.

    Uses 'command -v' so it works regardless of whether the binary is in
    /usr/bin, /usr/local/bin, or any other directory on PATH.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    binaries = config["binaries"]

    container = _get_container(container_name)
    assert container.status == "running"

    print(f"\n--- Checking Patroni binaries on {container_name} ({container_type}) ---")
    print(f"   Expected binaries: {binaries}")

    missing = []
    for binary in binaries:
        exit_code, output = container.exec_run(
            ["bash", "-c", f"command -v {binary}"],
            user="root",
        )
        if exit_code == 0:
            binary_path = output.decode().strip()
            print(f"   ✅ {binary}: {binary_path}")
        else:
            missing.append(binary)
            print(f"   ❌ {binary}: not found on PATH")

    assert not missing, (
        f"The following Patroni binaries are missing on {container_name}:\n"
        + "\n".join(f"  - {b}" for b in missing)
    )


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_patroni_binary_version(container_name, container_type):
    """Step 5: Verify that 'patroni --version' reports PGEDGE_PATRONI_VERSION.

    Greps the version string from the binary output and compares it to the
    value stored in patroni_version (PGEDGE_PATRONI_VERSION env var).
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    if not patroni_version:
        pytest.skip("No PGEDGE_PATRONI_VERSION defined in env, skipping binary version check")

    container = _get_container(container_name)
    assert container.status == "running"

    print(f"\n--- Checking 'patroni --version' on {container_name} ({container_type}) ---")

    exit_code, output = container.exec_run(
        ["bash", "-c", "patroni --version 2>&1"],
        user="root",
    )
    assert exit_code == 0, f"'patroni --version' failed: {output.decode().strip()}"

    version_output = output.decode().strip()
    print(f"   Output: {version_output}")

    assert patroni_version in version_output, (
        f"Expected version '{patroni_version}' not found in 'patroni --version' output:\n"
        f"  {version_output}"
    )
    print(f"✅ patroni --version reports {patroni_version}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_patroni_regression(container_name, container_type):
    """Step 6: Run the Patroni upstream regression test suite inside the container.

    Steps executed inside the container:
      1. Ensure git is installed.
      2. Clone https://github.com/patroni/patroni.git to /tmp/patroni-src.
      3. Check out the tag matching the installed binary version (v<X.Y.Z>).
      4. Create a virtualenv at /tmp/patroni-tests/.venv with --system-site-packages
         so the system-installed patroni package is accessible.
      5. Install test dependencies from requirements.dev.txt.
      6. Run pytest and assert all tests pass.
    """
    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)
    assert container.status == "running"

    print(f"\n--- Running Patroni regression tests on {container_name} ({container_type}) ---")

    # ------------------------------------------------------------------
    # Step 1: Ensure git is installed
    # ------------------------------------------------------------------
    print("   [1/6] Ensuring git is installed...")
    if container_type == "rhel":
        install_git_cmd = "dnf install -y git 2>&1 || yum install -y git 2>&1"
    else:
        install_git_cmd = "apt-get install -y git 2>&1"

    exit_code, output = container.exec_run(
        ["bash", "-c", install_git_cmd], user="root"
    )
    assert exit_code == 0, f"Failed to install git: {output.decode().strip()}"
    print("   ✅ git is available")

    # ------------------------------------------------------------------
    # Step 2: Clone the Patroni source repository
    # ------------------------------------------------------------------
    print("   [2/6] Cloning Patroni source repository...")
    clone_cmd = (
        "rm -rf /tmp/patroni-src && "
        "git clone --depth 50 https://github.com/patroni/patroni.git /tmp/patroni-src 2>&1"
    )
    exit_code, output = container.exec_run(
        ["bash", "-c", clone_cmd], user="root"
    )
    assert exit_code == 0, f"Failed to clone Patroni repository: {output.decode().strip()}"
    print("   ✅ Repository cloned to /tmp/patroni-src")

    # ------------------------------------------------------------------
    # Step 3: Check out the tag matching the installed binary version
    # ------------------------------------------------------------------
    print("   [3/6] Checking out matching version tag...")
    checkout_cmd = (
        "cd /tmp/patroni-src && "
        r"VERSION=$(patroni --version 2>&1 | grep -oP '\d+\.\d+\.\d+') && "
        'echo "Detected version: $VERSION" && '
        "git fetch --tags 2>&1 && "
        'git checkout "v${VERSION}" 2>&1'
    )
    exit_code, output = container.exec_run(
        ["bash", "-c", checkout_cmd], user="root"
    )
    output_text = output.decode().strip()
    print(f"   {output_text}")
    assert exit_code == 0, f"Failed to checkout version tag: {output_text}"
    print("   ✅ Source checked out at matching version tag")

    # ------------------------------------------------------------------
    # Step 4: Locate the Python interpreter that has patroni installed,
    #         install venv support for it, then create the virtualenv.
    # ------------------------------------------------------------------
    # On RHEL 9 the default `python3` is 3.9, but the pgedge patroni package
    # installs into Python 3.12's site-packages.  We therefore probe each
    # candidate interpreter in order and use the first one that can import patroni.
    # On Debian/Ubuntu `python3-venv` must be installed explicitly.
    print("   [4/6] Locating Python interpreter with patroni and creating virtualenv...")

    find_python_cmd = (
        "for py in python3.12 python3.11 python3.10 python3.9 python3; do "
        "  if command -v $py &>/dev/null && $py -c 'import patroni' 2>/dev/null; then "
        "    echo $py; exit 0; "
        "  fi; "
        "done; "
        "echo ''"  # emit empty string if none found
    )
    _, py_out = container.exec_run(["bash", "-c", find_python_cmd], user="root")
    patroni_python = py_out.decode().strip()  # e.g. "python3.12"

    if not patroni_python:
        pytest.fail(
            "Could not find a Python interpreter that can import patroni. "
            "Ensure a pgedge-patroni package is installed before running this test."
        )
    print(f"   ✅ patroni found under: {patroni_python}")

    # On Debian/Ubuntu, python3.X-venv must be installed for that specific version
    if container_type == "deb":
        py_ver = patroni_python.replace("python", "")  # "3.12"
        install_venv_cmd = (
            f"apt-get install -y python3-venv python3{py_ver}-venv python3-pip 2>&1 || "
            "apt-get install -y python3-venv python3-pip 2>&1"
        )
        exit_code, output = container.exec_run(
            ["bash", "-c", install_venv_cmd], user="root"
        )
        assert exit_code == 0, f"Failed to install python3-venv: {output.decode().strip()}"
    else:
        # RHEL: ensure pip is present for the selected interpreter
        container.exec_run(
            ["bash", "-c", "dnf install -y python3-pip 2>&1 || yum install -y python3-pip 2>&1"],
            user="root",
        )

    venv_cmd = (
        "rm -rf /tmp/patroni-tests && "
        "mkdir -p /tmp/patroni-tests && "
        f"{patroni_python} -m venv --system-site-packages /tmp/patroni-tests/.venv 2>&1"
    )
    exit_code, output = container.exec_run(["bash", "-c", venv_cmd], user="root")
    assert exit_code == 0, f"Failed to create virtualenv: {output.decode().strip()}"

    # Confirm patroni is importable inside the new venv (via system-site-packages)
    verify_cmd = (
        "source /tmp/patroni-tests/.venv/bin/activate && "
        'python3 -c "import patroni; print(patroni.__file__)" 2>&1'
    )
    exit_code, output = container.exec_run(["bash", "-c", verify_cmd], user="root")
    patroni_file = output.decode().strip()
    assert exit_code == 0, (
        f"patroni not importable in venv (system-site-packages): {patroni_file}"
    )
    print(f"   ✅ Virtualenv created; patroni at: {patroni_file}")

    # ------------------------------------------------------------------
    # Step 5: Install test dependencies
    # ------------------------------------------------------------------
    # requirements.dev.txt covers most deps, but pysyncobj and kazoo are
    # optional extras not always listed there — install them explicitly so
    # that test_raft.py and test_zookeeper.py can be collected.
    print("   [5/6] Installing test dependencies...")
    pip_cmd = (
        "source /tmp/patroni-tests/.venv/bin/activate && "
        "pip install --upgrade pip 2>&1 && "
        "pip install -r /tmp/patroni-src/requirements.dev.txt 2>&1 && "
        "pip install pytest pytest-cov mock pysyncobj kazoo botocore boto3 python-consul2 2>&1"
    )
    exit_code, output = container.exec_run(["bash", "-c", pip_cmd], user="root")
    assert exit_code == 0, (
        f"Failed to install test dependencies:\n{output.decode().strip()}"
    )
    print("   ✅ Test dependencies installed")

    # ------------------------------------------------------------------
    # Step 6: Run the upstream pytest suite
    # ------------------------------------------------------------------
    print("   [6/6] Running Patroni pytest suite...")
    pytest_cmd = (
        "source /tmp/patroni-tests/.venv/bin/activate && "
        "cd /tmp/patroni-src && "
        "python3 -m pytest tests/ -v --tb=short 2>&1"
    )
    exit_code, output = container.exec_run(
        ["bash", "-c", pytest_cmd], user="root"
    )
    output_text = output.decode().strip()
    print(f"\n{output_text}\n")

    assert exit_code == 0, (
        f"Patroni regression tests FAILED on {container_name}.\n"
        f"Exit code: {exit_code}\n"
        f"Last output (tail):\n"
        + "\n".join(output_text.splitlines()[-40:])
    )
    print(f"✅ All Patroni regression tests passed on {container_name}")


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_package_uninstall(container_name, container_type):
    """Uninstall all Patroni packages"""
    if skip_cleanup:
        pytest.skip("Skipping uninstall: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    config = get_container_config(container_type)
    packages = config["packages"]

    container = _get_container(container_name)
    assert container.status == "running"

    print(f"\n--- Uninstalling Patroni packages on {container_name} ({container_type}) ---")

    failed = []
    for pkg in packages:
        try:
            success, platform, message = package_management.uninstall_package(container, pkg)
            if success:
                print(f"   ✅ {pkg}: {message}")
            else:
                failed.append(f"{pkg}: {message}")
                print(f"   ❌ {pkg}: {message}")
        except Exception as e:
            failed.append(f"{pkg}: {str(e)}")
            print(f"   ❌ {pkg}: {str(e)}")

    assert not failed, "Some packages failed to uninstall:\n" + "\n".join(failed)


@pytest.mark.parametrize("container_name,container_type", all_containers)
def test_pgedge_cleanup(container_name, container_type):
    """Full cleanup: remove all pgedge packages and leftover data"""
    if skip_cleanup:
        pytest.skip("Skipping cleanup: SKIP_CLEANUP=true")

    container_name = container_name.strip()
    if not container_name:
        pytest.skip("No container defined in env")

    container = _get_container(container_name)
    assert container.status == "running"

    config = get_container_config(container_type)
    pguser = config["pguser"]

    print(f"\n--- Full pgEdge cleanup on {container_name} ---")
    try:
        success, cleanup_summary, message = machine_cleanup.cleanup_pgedge_environment(
            container, pgdata=None, pguser=pguser
        )
        assert success, f"Cleanup failed: {message}"
        print(f"✅ {message}")
        if cleanup_summary["packages_removed"]:
            print(f"   Packages removed: {len(cleanup_summary['packages_removed'])}")
        if cleanup_summary.get("user_removed"):
            print(f"   User removed: {pguser}")
    except Exception as e:
        pytest.fail(f"Failed to cleanup pgEdge environment: {str(e)}")

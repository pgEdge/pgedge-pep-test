#!/usr/bin/env python3
import subprocess
import sys
import os
import re

# Default command executor for standalone execution
def default_run(cmd):
    print(f"\n>>> Running: {cmd}")
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0:
        print(f"WARNING: Command failed (exit={result.returncode})")
    return result.returncode

# Global executor that can be overridden
_executor = default_run

def set_executor(executor_func):
    """Set a custom command executor function."""
    global _executor
    _executor = executor_func

def run(cmd):
    """Execute a command using the configured executor."""
    return _executor(cmd)


def get_os_info():
    """
    Parse /etc/os-release and return (id, version_id)
    Example: ("ubuntu", "22.04"), ("rhel", "9"), ("rocky", "10")
    """
    os_id = ""
    version = ""

    if not os.path.exists("/etc/os-release"):
        print("ERROR: /etc/os-release not found. Cannot detect OS.")
        sys.exit(1)

    with open("/etc/os-release") as f:
        for line in f:
            if line.startswith("ID="):
                os_id = line.strip().split("=")[1].strip('"')
            if line.startswith("VERSION_ID="):
                version = line.strip().split("=")[1].strip('"')

    major = re.split(r"[.]", version)[0] if version else ""

    print(f"Detected OS: {os_id}, Version: {version} (major={major})")
    return os_id.lower(), major


def setup_debian():
    print("\n=== Debian/Ubuntu Prerequisites ===")
    run("apt-get update")
    run("apt-get install -y python3")
    run("apt-get install -y curl")
    run("apt-get install -y wget")
    run("apt-get install -y sudo")
    run("apt-get install -y gnupg2")
    run("apt-get install -y lsb-release")
    run("apt-get install -y file")
    run("apt-get install -y sq")

    # Add pgEdge repo
    cmd = (
        "curl -sSL https://apt.pgedge.com/repodeb/pgedge-release_latest_all.deb "
        "-o /tmp/pgedge-release.deb && "
        "sudo dpkg -i /tmp/pgedge-release.deb && "
        "rm -f /tmp/pgedge-release.deb || true"
    )
    run(cmd)


def setup_rhel9():
    print("\n=== RHEL 9 Prerequisites ===")
    run("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm")
    run("sudo dnf config-manager --set-enabled codeready-builder-for-rhel-9-rhui-rpms")
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")




def setup_rhel10():
    print("\n=== RHEL 10 Prerequisites ===")
    run("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm")
    run("sudo subscription-manager repos --enable codeready-builder-for-rhel-10-x86_64-rpms")
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")




def setup_rocky9():
    print("\n=== Rocky Linux 9 Prerequisites ===")
    run("dnf install -y sudo")
    run("sudo dnf install -y epel-release")
    run("sudo dnf config-manager --set-enabled crb")
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")




def setup_rocky10():
    print("\n=== Rocky Linux 10 Prerequisites ===")
    run("dnf install -y sudo")
    run("sudo dnf install -y epel-release")
    run("sudo dnf config-manager --set-enabled crb")
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")




def setup_oracle9():
    print("\n=== Oracle Linux 9 Prerequisites ===")
    run("dnf install -y sudo")
    run("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm")
    run("sudo dnf config-manager --set-enabled ol9_codeready_builder")
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")




def setup_oracle10():
    print("\n=== Oracle Linux 10 Prerequisites ===")
    run("dnf install -y sudo")
    run("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm")
    run("sudo dnf config-manager --set-enabled ol10_codeready_builder")
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")




def setup_alma9():
    print("\n=== AlmaLinux 9 Prerequisites ===")
    run("dnf install -y sudo")
    run("sudo dnf install -y epel-release")
    run("sudo dnf config-manager --set-enabled crb")
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")




def setup_alma10():
    print("\n=== AlmaLinux 10 Prerequisites ===")
    run("dnf install -y sudo")
    run("sudo dnf install -y epel-release")
    run("sudo dnf config-manager --set-enabled crb")
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")


def ensure_wget_installed():
    """Defensive idempotent: verify wget is on PATH after the per-OS prereq
    step ran; if not, install it via the available package manager.

    Guards against silent transient failures: run() only warns on a non-zero
    exit code (it does not abort), so an earlier failing step in a setup_*
    function (e.g., sequoia-sq install) can leave the dnf/apt transaction
    in a state where the subsequent wget install no-ops. test_verify_sbom
    then fails downstream with 'wget: executable file not found in $PATH'.
    """
    if run("command -v wget >/dev/null 2>&1") == 0:
        return  # wget is present, nothing to do
    print("[prereq-fix] wget not found after primary prereq install; retrying via detected package manager")
    if run("command -v apt-get >/dev/null 2>&1") == 0:
        run("apt-get install -y wget")
    elif run("command -v dnf >/dev/null 2>&1") == 0:
        run("sudo dnf install -y wget")
    elif run("command -v yum >/dev/null 2>&1") == 0:
        run("sudo yum install -y wget")
    else:
        print("[prereq-fix] WARNING: no supported package manager found; cannot install wget")


def install_prerequisites_on_container(container):
    """
    Install prerequisites on a Docker container.

    Args:
        container: Docker container object with exec_run method

    Returns:
        tuple: (success: bool, os_id: str, message: str)

    Raises:
        Exception: If prerequisite installation fails
    """

    print(f"\n--- Installing prerequisites on container ---")

    # Create a container-aware executor
    def container_executor(cmd):
        print(f"\n>>> Running: {cmd}")
        exit_code, output = container.exec_run(cmd, user="root")
        if exit_code != 0:
            print(f"WARNING: Command failed (exit={exit_code})")
        return exit_code

    # Set the custom executor
    set_executor(container_executor)

    # Detect OS inside container
    exit_code, output = container.exec_run("cat /etc/os-release", user="root")
    if exit_code != 0:
        raise Exception("Failed to detect OS inside container")

    # Parse OS info
    os_release = output.decode()
    os_id = ""
    version_id = ""
    for line in os_release.split('\n'):
        if line.startswith("ID="):
            os_id = line.split('=')[1].strip('"').lower()
        if line.startswith("VERSION_ID="):
            version_id = line.split('=')[1].strip('"')

    major = version_id.split('.')[0] if version_id else ""
    print(f"Detected OS: {os_id}, Version: {version_id} (major={major})")

    # Call the appropriate setup function
    try:
        if os_id in ["debian", "ubuntu"]:
            setup_debian()
        elif os_id in ["rhel", "redhat", "rhelserver"]:
            if major == "9":
                setup_rhel9()
            elif major == "10":
                setup_rhel10()
            else:
                raise Exception(f"Unsupported RHEL version: {major}")
        elif os_id == "rocky":
            if major == "9":
                setup_rocky9()
            elif major == "10":
                setup_rocky10()
            else:
                raise Exception(f"Unsupported Rocky version: {major}")
        elif os_id in ["ol", "oracle", "oraclelinux"]:
            if major == "9":
                setup_oracle9()
            elif major == "10":
                setup_oracle10()
            else:
                raise Exception(f"Unsupported Oracle Linux version: {major}")
        elif os_id in ["almalinux", "alma"]:
            if major == "9":
                setup_alma9()
            elif major == "10":
                setup_alma10()
            else:
                raise Exception(f"Unsupported AlmaLinux version: {major}")
        else:
            print(f"⚠️ Unsupported OS: {os_id} {major}, skipping prerequisites...")
            return True, f"{os_id} {major}", "Unsupported OS - prerequisites skipped"
    except Exception as e:
        raise Exception(f"Failed to install prerequisites: {str(e)}")

    # Defensive idempotent: confirm wget is on PATH; if a silent transient
    # failure earlier in the setup_* function left it uninstalled, this
    # catches and retries. Surfaced by V2 alma9-arm finding (May 2026).
    ensure_wget_installed()

    message = f"Prerequisites installed successfully for {os_id} {major}"
    print(f"\n✅ {message}")
    return True, f"{os_id} {major}", message


def main():
    os_id, major = get_os_info()

    if os_id in ["debian", "ubuntu"]:
        setup_debian()

    elif os_id in ["rhel", "redhat", "rhelserver"]:
        if major == "9":
            setup_rhel9()
        elif major == "10":
            setup_rhel10()
        else:
            print(f"Unsupported RHEL version: {major}")

    elif os_id == "rocky":
        if major == "9":
            setup_rocky9()
        elif major == "10":
            setup_rocky10()

    elif os_id in ["ol", "oracle", "oraclelinux"]:
        if major == "9":
            setup_oracle9()
        elif major == "10":
            setup_oracle10()

    elif os_id in ["almalinux", "alma"]:
        if major == "9":
            setup_alma9()
        elif major == "10":
            setup_alma10()

    else:
        print(f"❌ Unsupported OS: {os_id} {major}")
        sys.exit(1)

    print("\n🎉 Machine prerequisite setup completed successfully!\n")


if __name__ == "__main__":
    main()

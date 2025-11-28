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
    run("apt-get install -y sudo")
    run("apt-get install -y gnupg2")
    run("apt-get install -y lsb-release")

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


def setup_rhel10():
    print("\n=== RHEL 10 Prerequisites ===")
    run("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm")
    run("sudo subscription-manager repos --enable codeready-builder-for-rhel-10-x86_64-rpms")


def setup_rocky9():
    print("\n=== Rocky Linux 9 Prerequisites ===")
    run("sudo dnf install -y epel-release")
    run("sudo dnf config-manager --set-enabled crb")


def setup_rocky10():
    print("\n=== Rocky Linux 10 Prerequisites ===")
    run("sudo dnf install -y epel-release")
    run("sudo dnf config-manager --set-enabled crb")


def setup_oracle9():
    print("\n=== Oracle Linux 9 Prerequisites ===")
    run("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm")
    run("sudo dnf config-manager --set-enabled ol9_codeready_builder")


def setup_oracle10():
    print("\n=== Oracle Linux 10 Prerequisites ===")
    run("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm")
    run("sudo dnf config-manager --set-enabled ol10_codeready_builder")


def setup_alma9():
    print("\n=== AlmaLinux 9 Prerequisites ===")
    run("sudo dnf install -y epel-release")
    run("sudo dnf config-manager --set-enabled crb")


def setup_alma10():
    print("\n=== AlmaLinux 10 Prerequisites ===")
    run("sudo dnf install -y epel-release")
    run("sudo dnf config-manager --set-enabled crb")


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

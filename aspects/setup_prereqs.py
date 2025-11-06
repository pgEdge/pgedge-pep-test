#!/usr/bin/env python3
"""
setup_prereqs.py
=================
Automatically sets up OS-level and Python dependencies
for RHEL and Debian/Ubuntu-based systems.
"""

import os
import subprocess
import sys
import platform

def run_command(cmd, check=True):
    """Run shell command with logging."""
    print(f"➡️  Running: {cmd}")
    try:
        subprocess.run(cmd, shell=True, check=check)
    except subprocess.CalledProcessError as e:
        print(f"❌ Command failed: {cmd}")
        sys.exit(e.returncode)


def install_rhel_prereqs(version_id):
    """Install pre-reqs for RHEL 9x or 10x."""
    print(f"🟥 Setting up RHEL {version_id}...")

    if version_id.startswith("9"):
        run_command("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm")
        run_command("sudo dnf config-manager --set-enabled codeready-builder-for-rhel-9-rhui-rpms")

    elif version_id.startswith("10"):
        run_command("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm")
        run_command("sudo subscription-manager repos --enable codeready-builder-for-rhel-10-x86_64-rpms")


def install_debian_prereqs():
    """Install pre-reqs for Debian/Ubuntu."""
    print("🐧 Setting up Debian/Ubuntu system...")
    run_command("sudo apt-get update -y")
    run_command("sudo apt-get install -y curl python3-pip")


def install_python_requirements():
    """Install Python dependencies from requirements.txt."""
    req_path = os.path.join(os.path.dirname(__file__), "..", "requirements.txt")
    req_path = os.path.abspath(req_path)

    if not os.path.exists(req_path):
        print(f"❌ requirements.txt not found at {req_path}")
        sys.exit(1)

    print(f"📦 Installing Python dependencies from: {req_path}")
    run_command(f"pip3 install -r {req_path}")


def main():
    # Detect OS
    print("🔍 Detecting operating system...")
    distro = ""
    version_id = ""

    # Read from /etc/os-release
    try:
        with open("/etc/os-release") as f:
            lines = f.readlines()
        for line in lines:
            if line.startswith("ID="):
                distro = line.strip().split("=")[1].replace('"', '')
            if line.startswith("VERSION_ID="):
                version_id = line.strip().split("=")[1].replace('"', '')
    except FileNotFoundError:
        print("❌ /etc/os-release not found. Unsupported OS.")
        sys.exit(1)

    print(f"✅ Detected OS: {distro} {version_id}")

    # Branch by OS family
    if distro in ["rhel"]:
        install_rhel_prereqs(version_id)
    elif distro in ["debian", "ubuntu"]:
        install_debian_prereqs()
    else:
        print(f"❌ Unsupported OS: {distro}")
        sys.exit(1)

    # Install Python deps
    install_python_requirements()

    print("\n✅ Environment setup completed successfully!")


if __name__ == "__main__":
    main()

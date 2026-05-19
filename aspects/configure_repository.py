#!/usr/bin/env python3
"""
Generic repository configuration module for pgedge packages.
Supports both RHEL-based and Debian-based distributions.
"""


def _wait_for_apt_lock(container, timeout=120):
    """
    Stop background apt services and wait until all apt/dpkg locks are free.
    On AWS instances unattended-upgrades and apt-daily commonly hold the lock
    between test steps, causing 'Could not get lock' failures.
    """
    # Stop and disable background apt services that hold the lock on AWS instances
    container.exec_run(
        ["/bin/sh", "-c",
         "systemctl stop unattended-upgrades apt-daily.service apt-daily-upgrade.service 2>/dev/null; "
         "systemctl disable unattended-upgrades apt-daily.service apt-daily-upgrade.service 2>/dev/null; "
         "true"],
        user="root"
    )
    # Kill any lingering apt/dpkg processes
    container.exec_run(
        ["/bin/sh", "-c", "pkill -9 -f 'apt-get|dpkg|unattended' 2>/dev/null; true"],
        user="root"
    )
    # Poll until all three lock files are free
    wait_cmd = (
        f"i=0; "
        f"while fuser /var/lib/dpkg/lock /var/lib/apt/lists/lock "
        f"/var/lib/dpkg/lock-frontend >/dev/null 2>&1; do "
        f"  i=$((i+2)); "
        f"  if [ $i -ge {timeout} ]; then echo 'Timed out waiting for apt lock'; exit 1; fi; "
        f"  echo \"Waiting for apt lock (${{i}}/{timeout}s)...\"; sleep 2; "
        f"done; echo 'apt lock is free'"
    )
    exit_code, output = container.exec_run(["/bin/sh", "-c", wait_cmd], user="root")
    print(output.decode().strip())
    if exit_code != 0:
        raise Exception("Timed out waiting for apt/dpkg lock to be released")


def configure_pgedge_repository(container, repo="release"):
    """
    Configure pgedge repository on a container.

    Args:
        container: Docker container object with exec_run method
        repo: Repository type - "release", "staging", or "daily"

    Returns:
        tuple: (success: bool, platform: str, message: str)

    Raises:
        Exception: If repository configuration fails
    """

    # Detect platform: RHEL (dnf) or Debian/Ubuntu (apt-get)
    exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v dnf"], user="root")
    if exit_code == 0:
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v apt-get"], user="root")
        if exit_code == 0:
            platform = "ubuntu"
        else:
            raise Exception("No supported package manager found (dnf or apt-get)")

    print(f"\n--- Configuring pgedge repository ({platform}) ---")

    if platform == "rhel":
        return _configure_rhel_repo(container, repo)
    else:
        return _configure_debian_repo(container, repo)


def _configure_rhel_repo(container, repo):
    """Configure repository for RHEL-based distributions."""

    repo_url = "https://dnf.pgedge.com/reporpm/pgedge-release-latest.noarch.rpm"
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", f"dnf install -y {repo_url}"], user="root"
    )
    if exit_code != 0:
        raise Exception(f"Failed to install repo: {output.decode()}")

    print(" Installed pgedge repository package")

    if repo in ["staging", "daily"]:
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c", f"sed -i 's|release|{repo}|g' /etc/yum.repos.d/pgedge.repo"],
            user="root"
        )
        if exit_code != 0:
            raise Exception(f"Failed to switch repo to {repo}: {output.decode()}")
        print(f" Repository switched to {repo}")

    return True, "rhel", f"Repository configured successfully (repo={repo})"


def _configure_debian_repo(container, repo):
    """Configure repository for Debian-based distributions."""

    # Wait for any background apt process before touching dpkg/apt
    _wait_for_apt_lock(container)

    deb_url = "https://apt.pgedge.com/repodeb/pgedge-release_latest_all.deb"
    install_cmd = (
        f"curl -sSL {deb_url} -o /tmp/pgedge-release.deb && "
        f"dpkg -i /tmp/pgedge-release.deb && "
        f"rm -f /tmp/pgedge-release.deb || true"
    )
    exit_code, output = container.exec_run(["/bin/sh", "-c", install_cmd], user="root")
    if exit_code != 0:
        raise Exception(f"Failed to install repo: {output.decode()}")

    print(" Installed pgedge repository package")

    if repo in ["staging", "daily"]:
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c",
             f"sed -i 's|release|{repo}|g' /etc/apt/sources.list.d/pgedge.sources"],
            user="root"
        )
        if exit_code != 0:
            raise Exception(f"Failed to switch repo to {repo}: {output.decode()}")
        print(f" Repository switched to {repo}")

    # Wait again before apt-get update in case dpkg triggered background work
    _wait_for_apt_lock(container)

    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", "apt-get update"], user="root"
    )
    if exit_code != 0:
        raise Exception(f"apt-get update failed: {output.decode()}")

    print(" Package lists updated (apt-get update)")

    return True, "ubuntu", f"Repository configured successfully (repo={repo})"
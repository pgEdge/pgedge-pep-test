#!/usr/bin/env python3
"""
Generic repository configuration module for pgedge packages.
Supports both RHEL-based and Debian-based distributions.
"""


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

    # Detect platform: RHEL (dnf) or Ubuntu (apt-get)
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            platform = "ubuntu"
        else:
            raise Exception("No supported package manager found (dnf or apt-get)")

    print(f"\n--- Configuring pgedge repository ({platform}) ---")

    if platform == "rhel":
        return _configure_rhel_repo(container, repo)
    elif platform == "ubuntu":
        return _configure_debian_repo(container, repo)


def _configure_rhel_repo(container, repo):
    """Configure repository for RHEL-based distributions."""

    # Step 1: Install repo
    repo_url = "https://dnf.pgedge.com/reporpm/pgedge-release-latest.noarch.rpm"
    exit_code, output = container.exec_run(
        f"dnf install -y {repo_url}", user="root"
    )
    if exit_code != 0:
        raise Exception(f"Failed to install repo: {output.decode()}")

    print(f" Installed pgedge repository package")

    # Step 2: Switch repo if needed (staging/daily)
    if repo in ["staging", "daily"]:
        exit_code, output = container.exec_run(
            f"sed -i 's|release|{repo}|g' /etc/yum.repos.d/pgedge.repo", user="root"
        )
        if exit_code != 0:
            raise Exception(f"Failed to switch repo to {repo}: {output.decode()}")
        print(f" Repository switched to {repo}")

    return True, "rhel", f"Repository configured successfully (repo={repo})"


def _configure_debian_repo(container, repo):
    """Configure repository for Debian-based distributions."""

    # Step 1: Install repo via .deb
    deb_url = "https://apt.pgedge.com/repodeb/pgedge-release_latest_all.deb"
    install_cmd = f"""
        curl -sSL {deb_url} -o /tmp/pgedge-release.deb && \\
        dpkg -i /tmp/pgedge-release.deb && \\
        rm -f /tmp/pgedge-release.deb || true
    """

    exit_code, output = container.exec_run(
        f"/bin/bash -c \"{install_cmd}\"",
        user="root",
    )
    if exit_code != 0:
        raise Exception(f"Failed to install repo: {output.decode()}")

    print(f" Installed pgedge repository package")

    # Step 2: Switch repo if needed
    if repo in ["staging", "daily"]:
        exit_code, output = container.exec_run(
            f"sed -i 's|release|{repo}|g' /etc/apt/sources.list.d/pgedge.sources",
            user="root",
        )
        if exit_code != 0:
            raise Exception(f"Failed to switch repo to {repo}: {output.decode()}")
        print(f" Repository switched to {repo}")

    # Step 3: apt-get update
    exit_code, output = container.exec_run("apt-get update", user="root")
    if exit_code != 0:
        raise Exception(f"apt-get update failed: {output.decode()}")

    print(f" Package lists updated (apt-get update)")

    return True, "ubuntu", f"Repository configured successfully (repo={repo})"
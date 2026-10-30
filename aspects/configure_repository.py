#!/usr/bin/env python3
"""
Generic repository configuration module for pgedge packages.
Supports both RHEL-based and Debian-based distributions.
"""

from aspects import pgedge_client_cert


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


def _result_message(repo, enabled):
    """Say plainly whether the repositories came out usable."""
    if enabled:
        return f"Repository configured successfully (repo={repo})"
    return (
        f"Repository defined (repo={repo}) but left disabled: no client "
        f"certificate was supplied, so no package from it will install"
    )


def _install_client_certificate(container, platform):
    """Install the client certificate and enable the gated repositories."""

    crt, key = pgedge_client_cert.read_client_certificate()
    if not crt or not key:
        print(f" {pgedge_client_cert.NO_CERTIFICATE_MESSAGE}")
        # pep-integration.yml starts a full run only with a complete pair, so
        # reaching here there is a setup problem, never a product one. A no-op
        # in a standalone run.
        pgedge_client_cert.record_setup_failure(pgedge_client_cert.MISSING_CERTIFICATE)
        return False

    pgedge_client_cert.install_on_target(container, platform, crt, key)

    print(" Installed client certificate, pgEdge repositories enabled")
    return True


def _configure_rhel_repo(container, repo):
    """Configure repository for RHEL-based distributions."""

    # downloads.pgedge.com, not dnf.pgedge.com: the gated repository cannot
    # serve the package that configures access to it.
    repo_url = "https://downloads.pgedge.com/reporpm/pgedge-release-latest.noarch.rpm"
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

    enabled = _install_client_certificate(container, "rhel")

    if enabled:
        # pgedge-subscription-manager enables the repositories but tolerates a
        # failed refresh, so a certificate the server refuses would surface only
        # at the first package install, read as a product failure. Refresh just
        # the repositories it enabled (not the disabled source one).
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c", "dnf -q makecache --repo=pgedge --repo=pgedge-noarch"],
            user="root"
        )
        if exit_code != 0:
            pgedge_client_cert.record_setup_failure(pgedge_client_cert.REPOSITORY_FAILURE)
            raise Exception(
                f"pgEdge repositories could not be refreshed with the client "
                f"certificate: {output.decode()}")
        pgedge_client_cert.clear_setup_failure()

    return True, "rhel", _result_message(repo, enabled)


def _configure_debian_repo(container, repo):
    """Configure repository for Debian-based distributions."""

    # Wait for any background apt process before touching dpkg/apt
    _wait_for_apt_lock(container)

    # downloads.pgedge.com, not apt.pgedge.com -- see _configure_rhel_repo.
    deb_url = "https://downloads.pgedge.com/repodeb/pgedge-release_latest_all.deb"
    # apt-get, not dpkg -i: the package depends on lsb-release, and dpkg
    # leaves it unpacked-but-unconfigured when a dependency is missing. That
    # wrote no sources file and no pgedge-subscription-manager, which the
    # trailing "|| true" then hid -- the tests failed much later with
    # "Unable to locate package".
    # -f so an HTTP error is a failure rather than an error page saved under a
    # .deb name; a private directory so the download cannot be pre-placed, and
    # removed either way.
    install_cmd = (
        'set -e; d=$(mktemp -d); '
        'trap \'rm -rf "$d"\' EXIT; '
        f'curl -fsSL {deb_url} -o "$d/pgedge-release.deb"; '
        'apt-get install -y "$d/pgedge-release.deb"'
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

    # Before apt-get update: pgedge-release points apt at a host it cannot
    # reach without the certificate, and an unreachable source fails the whole
    # update, not just pgEdge's part of it.
    enabled = _install_client_certificate(container, "ubuntu")

    # Wait again before apt-get update in case dpkg triggered background work
    _wait_for_apt_lock(container)

    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", "apt-get update"], user="root"
    )
    if exit_code != 0:
        if enabled:
            # The first fetch through the client certificate; see _configure_rhel_repo.
            # It refreshes every source, so the reason Summarize gives stays neutral.
            pgedge_client_cert.record_setup_failure(pgedge_client_cert.REPOSITORY_FAILURE)
        raise Exception(f"apt-get update failed: {output.decode()}")

    if enabled:
        pgedge_client_cert.clear_setup_failure()

    print(" Package lists updated (apt-get update)")

    return True, "ubuntu", _result_message(repo, enabled)
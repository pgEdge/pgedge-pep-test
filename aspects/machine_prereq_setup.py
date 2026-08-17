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


def setup_debian(os_id="", major=""):
    print(f"\n=== Debian/Ubuntu Prerequisites (os={os_id or '?'}, major={major or '?'}) ===")

    # Pre-configure timezone to Asia/Karachi to prevent tzdata from prompting
    # during package installs. Must be done before any apt-get install calls.
    run("ln -snf /usr/share/zoneinfo/Asia/Karachi /etc/localtime && echo Asia/Karachi > /etc/timezone")

    # Minimal Debian/Ubuntu Docker images (e.g. Ubuntu 24.04) ship dpkg config files
    # under /etc/dpkg/dpkg.cfg.d/ that exclude /usr/share/doc and /usr/share/licenses,
    # causing README.md and LICENSE files to be silently omitted on install.
    # Remove those path-exclude lines from every file in the directory so subsequent
    # apt-get installs write the full file tree.
    run(
        r"grep -rl 'path-exclude=/usr/share/doc' /etc/dpkg/dpkg.cfg.d/ 2>/dev/null"
        r" | xargs -r sed -i '/^path-exclude=\/usr\/share\/doc/d';"
        r" grep -rl 'path-exclude=/usr/share/licenses' /etc/dpkg/dpkg.cfg.d/ 2>/dev/null"
        r" | xargs -r sed -i '/^path-exclude=\/usr\/share\/licenses/d'; true"
    )

    run("apt-get update")
    run("DEBIAN_FRONTEND=noninteractive apt-get install -y python3")
    run("DEBIAN_FRONTEND=noninteractive apt-get install -y curl")
    run("DEBIAN_FRONTEND=noninteractive apt-get install -y sudo")
    run("DEBIAN_FRONTEND=noninteractive apt-get install -y gnupg2")
    run("DEBIAN_FRONTEND=noninteractive apt-get install -y lsb-release")
    run("DEBIAN_FRONTEND=noninteractive apt-get install -y file")
    run("DEBIAN_FRONTEND=noninteractive apt-get install -y python3-psycopg2")


    # Install sq (Sequoia PGP CLI) — method varies by distro version
    if os_id == "ubuntu":
        # universe repo required on older Ubuntu
        run("DEBIAN_FRONTEND=noninteractive apt-get install -y software-properties-common")
        run("add-apt-repository -y universe")
        run("apt-get update")
        run("DEBIAN_FRONTEND=noninteractive apt-get install -y sq")
    else:
        # All Debian versions: sq is in the standard main repos
        run("DEBIAN_FRONTEND=noninteractive apt-get install -y sq")

    # Add pgEdge repo
    cmd = (
        "curl -sSL https://apt.pgedge.com/repodeb/pgedge-release_latest_all.deb "
        "-o /tmp/pgedge-release.deb && "
        "sudo dpkg -i /tmp/pgedge-release.deb && "
        "rm -f /tmp/pgedge-release.deb || true"
    )
    run(cmd)


def disable_pgdg_repositories():
    """Disable every pgdg* repository on a RHEL-family host.

    PGDG ships its own builds of PostgreSQL and of shared dependencies such as
    python3-psycopg2. Left enabled alongside the pgEdge repository, dnf can
    resolve those packages against PGDG builds, so the run ends up testing the
    wrong artifacts. Disable them before anything is installed.

    Must tolerate both extremes: long-lived AWS instances often carry a pgdg
    repo left over from an earlier run, while fresh containers have none.
    Three strategies are attempted in order, since the tooling differs by
    release:
      1. dnf4 `config-manager --set-disabled` (RHEL 9 family)
      2. dnf5 `config-manager setopt`         (RHEL 10 family)
      3. editing /etc/yum.repos.d/pgdg*.repo  (no config-manager plugin)
    The chain ends in `true` so a host with no pgdg repo is a silent no-op.
    """
    print("--- Disabling pgdg repositories ---")
    run(
        "sudo dnf config-manager --set-disabled 'pgdg*' 2>/dev/null "
        "|| sudo dnf config-manager setopt 'pgdg*.enabled=0' 2>/dev/null "
        "|| sudo sed -i 's/^enabled[[:space:]]*=.*/enabled=0/' "
        "/etc/yum.repos.d/pgdg*.repo 2>/dev/null "
        "|| true"
    )
    # Report rather than assert: a host that never had pgdg configured is the
    # normal case and must not fail the run.
    run(
        "if sudo dnf repolist --enabled 2>/dev/null | grep -qi '^pgdg'; then "
        "echo 'WARNING: pgdg repositories are STILL ENABLED:'; "
        "sudo dnf repolist --enabled 2>/dev/null | grep -i '^pgdg'; "
        "else echo 'OK: no enabled pgdg repositories'; fi"
    )


def setup_rhel9():
    print("\n=== RHEL 9 Prerequisites ===")
    run("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm")
    run("sudo dnf config-manager --set-enabled codeready-builder-for-rhel-9-rhui-rpms")
    disable_pgdg_repositories()
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")
    #run("sudo dnf install -y python3-psycopg2")




def setup_rhel10():
    print("\n=== RHEL 10 Prerequisites ===")
    run("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm")
    run("sudo subscription-manager repos --enable codeready-builder-for-rhel-10-x86_64-rpms")
    disable_pgdg_repositories()
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")
    #run("sudo dnf install -y python3-psycopg2")





def setup_rocky9():
    print("\n=== Rocky Linux 9 Prerequisites ===")
    run("dnf install -y sudo")
    run("sudo dnf install -y epel-release")
    run("sudo dnf config-manager --set-enabled crb")
    disable_pgdg_repositories()
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")
    #run("sudo dnf install -y python3-psycopg2")





def setup_rocky10():
    print("\n=== Rocky Linux 10 Prerequisites ===")
    run("dnf install -y sudo")
    run("sudo dnf install -y epel-release")
    run("sudo dnf config-manager --set-enabled crb")
    disable_pgdg_repositories()
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")
    #run("sudo dnf install -y python3-psycopg2")





def setup_oracle9():
    print("\n=== Oracle Linux 9 Prerequisites ===")
    run("dnf install -y sudo")
    run("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm")
    run("sudo dnf config-manager --set-enabled ol9_codeready_builder")
    disable_pgdg_repositories()
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")
    #run("sudo dnf install -y python3-psycopg2")





def setup_oracle10():
    print("\n=== Oracle Linux 10 Prerequisites ===")
    run("dnf install -y sudo")
    run("sudo dnf -y install https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm")
    run("sudo dnf config-manager --set-enabled ol10_codeready_builder")
    disable_pgdg_repositories()
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")
    #run("sudo dnf install -y python3-psycopg2")





def setup_alma9():
    print("\n=== AlmaLinux 9 Prerequisites ===")
    run("dnf install -y sudo")
    run("sudo dnf install -y epel-release")
    run("sudo dnf config-manager --set-enabled crb")
    disable_pgdg_repositories()
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")
    #run("sudo dnf install -y python3-psycopg2")





def setup_alma10():
    print("\n=== AlmaLinux 10 Prerequisites ===")
    run("dnf install -y sudo")
    run("sudo dnf install -y epel-release")
    run("sudo dnf config-manager --set-enabled crb")
    disable_pgdg_repositories()
    run("sudo dnf install -y file")
    run("sudo dnf install -y sequoia-sq")
    run("sudo dnf install -y wget")
    #run("sudo dnf install -y python3-psycopg2")



def _container_run(container, cmd):
    """Run a shell command on the container as root, print a warning on failure."""
    print(f"  >>> {cmd}")
    code, out = container.exec_run(["/bin/sh", "-c", cmd], user="root")
    if code != 0:
        print(f"  WARNING: exit={code}: {out.decode()[:200]}")
    return code


def _cmd_exists(container, binary):
    """Return True if `binary` is on PATH inside the container."""
    code, _ = container.exec_run(["/bin/sh", "-c", f"command -v {binary}"], user="root")
    return code == 0


def _detect_os(container):
    """Return (os_id, major_version) detected from /etc/os-release."""
    _, out = container.exec_run("cat /etc/os-release", user="root")
    os_id, version_id = "", ""
    for line in out.decode().split("\n"):
        if line.startswith("ID="):
            os_id = line.split("=", 1)[1].strip().strip('"').lower()
        elif line.startswith("VERSION_ID="):
            version_id = line.split("=", 1)[1].strip().strip('"')
    major = version_id.split(".")[0] if version_id else ""
    return os_id, major


def ensure_wget_installed(container):
    """Install wget on the container if it is not already present."""
    if _cmd_exists(container, "wget"):
        return
    os_id, _ = _detect_os(container)
    print(f"  [ensure_wget] wget not found — installing...")
    if os_id in ("debian", "ubuntu"):
        _container_run(container, "DEBIAN_FRONTEND=noninteractive apt-get install -y wget")
    else:
        _container_run(container, "dnf install -y wget 2>/dev/null || yum install -y wget 2>/dev/null")


def _get_deb_info(container, os_id, major):
    """Return (codename, arch, mirror) for the container's Debian/Ubuntu system."""
    _, codename_out = container.exec_run(
        "grep -m1 VERSION_CODENAME /etc/os-release", user="root"
    )
    codename = codename_out.decode().strip().split("=")[-1].strip('"')
    if not codename:
        codename_map = {
            "debian": {"11": "bullseye", "12": "bookworm", "13": "trixie"},
            "ubuntu": {"22": "jammy", "24": "noble"},
        }
        codename = codename_map.get(os_id, {}).get(major, "")

    _, arch_out = container.exec_run("dpkg --print-architecture", user="root")
    arch = arch_out.decode().strip()

    if os_id == "ubuntu":
        mirror = ("http://ports.ubuntu.com/ubuntu-ports"
                  if arch in ("arm64", "armhf")
                  else "http://archive.ubuntu.com/ubuntu")
    else:
        mirror = "http://deb.debian.org/debian"

    return codename, arch, mirror


def _apt_enable_universe(container, codename, mirror):
    """
    Enable the 'universe' component for Ubuntu.
    Handles both classic sources.list and DEB822 .sources files (Ubuntu 24.04+).
    """
    # DEB822 format: patch Components line in any *.sources file
    _container_run(container,
        r"find /etc/apt/sources.list.d/ -name '*.sources' "
        r"-exec sed -i 's/\(Components:.*\)\bmain\b/\1main universe/' {} + 2>/dev/null; true")
    # Classic format: append universe line if not already present
    _container_run(container,
        f"grep -qF 'universe' /etc/apt/sources.list 2>/dev/null || "
        f"echo 'deb {mirror} {codename} main universe' >> /etc/apt/sources.list")


def _apt_ensure_full_sources(container, os_id, major):
    """
    Guarantee that the full Debian/Ubuntu main repo is in sources.list.
    Minimal Docker images sometimes ship with only security or a partial mirror,
    which causes packages like 'sq' to be missing.
    """
    codename, _, mirror = _get_deb_info(container, os_id, major)
    if not codename:
        return
    if os_id == "ubuntu":
        _apt_enable_universe(container, codename, mirror)
    else:
        _container_run(container,
            f"grep -qF '{codename} main' /etc/apt/sources.list 2>/dev/null || "
            f"echo 'deb {mirror} {codename} main contrib' >> /etc/apt/sources.list")
    _container_run(container, "apt-get update -y")


def ensure_sq_installed(container):
    """Install the sq (Sequoia PGP) CLI on the container if it is not already present."""
    if _cmd_exists(container, "sq"):
        return

    os_id, major = _detect_os(container)
    print(f"  [ensure_sq] sq not found ({os_id} {major}) — installing...")

    if os_id in ("debian", "ubuntu"):
        codename, arch, mirror = _get_deb_info(container, os_id, major)

        if os_id == "ubuntu":
            # Enable universe (handles both classic sources.list and DEB822 format)
            _apt_enable_universe(container, codename, mirror)
        else:
            # Debian (all versions): ensure the full main mirror is available
            _container_run(container,
                f"grep -qF '{codename} main' /etc/apt/sources.list 2>/dev/null || "
                f"echo 'deb {mirror} {codename} main contrib' >> /etc/apt/sources.list")

        _container_run(container, "apt-get update -y")
        _container_run(container, "DEBIAN_FRONTEND=noninteractive apt-get install -y sq")

    else:
        _container_run(container,
            "dnf install -y sequoia-sq 2>/dev/null || yum install -y sequoia-sq 2>/dev/null")

    if not _cmd_exists(container, "sq"):
        raise RuntimeError(
            f"Failed to install sq on {os_id} {major} ({arch if os_id in ('debian','ubuntu') else 'rpm'}). "
            "Ensure the full Debian/Ubuntu main repository is reachable from the container."
        )


def cleanup_disk_space(container):
    """
    Remove previously installed pgedge packages and reclaim disk space.
    Called before installing prerequisites on AWS instances, which are
    long-lived and accumulate package/cache/log bloat across runs.

    Args:
        container: Docker container / SSHExecutor object with exec_run method

    Returns:
        tuple: (success: bool, message: str)
    """
    os_id, _ = _detect_os(container)
    is_deb = os_id in ("debian", "ubuntu")

    print("\n--- Cleaning up disk space before install ---")

    if is_deb:
        # Stop background apt services before running any apt/dpkg commands to
        # avoid 'Could not get lock' errors on long-lived AWS instances.
        from aspects.configure_repository import _wait_for_apt_lock
        _wait_for_apt_lock(container)

        steps = [
            # Remove leftover pgedge packages from prior test runs
            "DEBIAN_FRONTEND=noninteractive apt-get remove -y 'pgedge-*' 2>/dev/null || true",
            "DEBIAN_FRONTEND=noninteractive apt-get autoremove --purge -y 2>/dev/null || true",
            # Wipe downloaded .deb files from the package cache
            "apt-get clean",
            "rm -rf /var/cache/apt/archives/* /var/cache/apt/*.bin",
            # Wipe stale package index files — biggest space win on long-lived instances.
            # Indexes accumulate after every apt-get update and are often several hundred MB.
            # Rebuild them fresh with apt-get update afterwards.
            "rm -rf /var/lib/apt/lists/*",
            "apt-get update",
            # Snap consumes 1-2 GB on stock Ubuntu AWS AMIs (lxd, core20, ssm-agent, etc.)
            # Stop snapd before wiping to avoid mount-unit errors
            "systemctl stop snapd snapd.socket snapd.seeded.service 2>/dev/null || true",
            "rm -rf /var/lib/snapd/cache/* /var/lib/snapd/snaps/*",
            # Old rotated logs can pile up to several hundred MB
            "find /var/log -type f -name '*.gz' -delete 2>/dev/null || true",
            "find /var/log -type f -name '*.1' -delete 2>/dev/null || true",
        ]
    else:
        steps = [
            "dnf remove -y 'pgedge-*' 2>/dev/null || yum remove -y 'pgedge-*' 2>/dev/null || true",
            "dnf clean all 2>/dev/null || yum clean all 2>/dev/null || true",
            "rm -rf /var/cache/dnf/* /var/cache/yum/*",
            # Old rotated logs
            "find /var/log -type f -name '*.gz' -delete 2>/dev/null || true",
            "find /var/log -type f -name '*.1' -delete 2>/dev/null || true",
        ]

    # Steps common to both platforms
    steps += [
        "rm -rf /tmp/* /var/tmp/* 2>/dev/null || true",
        "journalctl --vacuum-size=10M 2>/dev/null || true",
    ]

    for cmd in steps:
        _container_run(container, cmd)

    # Report remaining free space
    _, df_out = container.exec_run(["/bin/sh", "-c", "df -h /"], user="root")
    print(f"Disk usage after cleanup:\n{df_out.decode().strip()}")

    # Warn about any files larger than 50 MB still present
    _, big_files = container.exec_run(
        ["/bin/sh", "-c", "find / -xdev -type f -size +50M 2>/dev/null | head -10"],
        user="root"
    )
    big_files_str = big_files.decode().strip()
    if big_files_str:
        print(f"Large files still present (>50 MB):\n{big_files_str}")

    return True, "Disk space cleanup completed"


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

    # Create a container-aware executor. Commands are run through /bin/sh so
    # shell operators (&&, |, >, ;) work correctly.
    def container_executor(cmd):
        print(f"\n>>> Running: {cmd}")
        exit_code, output = container.exec_run(["/bin/sh", "-c", cmd], user="root")
        if exit_code != 0:
            print(f"WARNING: Command failed (exit={exit_code}): {output.decode()[:200]}")
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
            os_id = line.split('=')[1].strip().strip('"').lower()
        if line.startswith("VERSION_ID="):
            version_id = line.split('=')[1].strip().strip('"')

    major = version_id.split('.')[0] if version_id else ""
    print(f"Detected OS: {os_id}, Version: {version_id} (major={major})")

    # Call the appropriate setup function
    try:
        if os_id in ["debian", "ubuntu"]:
            # Ensure no background apt process holds the lock before setup_debian runs
            from aspects.configure_repository import _wait_for_apt_lock
            _wait_for_apt_lock(container)
            setup_debian(os_id=os_id, major=major)
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

    message = f"Prerequisites installed successfully for {os_id} {major}"
    print(f"\n✅ {message}")
    return True, f"{os_id} {major}", message


def main():
    os_id, major = get_os_info()

    if os_id in ["debian", "ubuntu"]:
        setup_debian(os_id=os_id, major=major)

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

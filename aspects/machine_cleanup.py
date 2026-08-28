#!/usr/bin/env python3
"""
Machine cleanup module for removing packages, data directories, and users.
Supports comprehensive cleanup operations for test environments.
"""

# Interpreters probed when hunting for a pip-installed Patroni. The order
# mirrors the probe in test_pep_patroni.py's regression test: on RHEL 9 the
# default python3 is 3.9 while the pgedge package targets 3.12, so a stray
# `pip3 install patroni` can land under a different interpreter than the
# packaged copy and still win on PATH.
_PYTHON_CANDIDATES = [
    "python3.13", "python3.12", "python3.11", "python3.10", "python3.9", "python3",
]

# Console scripts a pip install of Patroni drops. Package installs put these in
# /usr/bin; pip puts them in /usr/local/bin (or ~/.local/bin for --user), both
# of which precede /usr/bin on a typical root PATH.
_PATRONI_SCRIPTS = [
    "patroni", "patronictl", "patroni_raft_controller",
    "patroni_barman", "patroni_aws", "patroni_wale_restore",
]
_PIP_SCRIPT_DIRS = ["/usr/local/bin", "/root/.local/bin"]


def _detect_pkg_manager(container):
    """Return 'rpm', 'dpkg', or None depending on what the container provides."""
    for tool in ("rpm", "dpkg"):
        exit_code, _ = container.exec_run(["/bin/sh", "-c", f"command -v {tool}"], user="root")
        if exit_code == 0:
            return tool
    return None


def _path_owner(container, pkg_manager, path):
    """Return the package owning `path`, or None when no package owns it.

    Used to tell a distro-installed Patroni from a pip-installed one. Anything
    a package owns must never be handed to `pip uninstall` — pip would delete
    files out from under rpm/dpkg and leave the package half-present.
    """
    if not pkg_manager:
        return None

    cmd = f"rpm -qf {path}" if pkg_manager == "rpm" else f"dpkg -S {path}"
    exit_code, output = container.exec_run(["/bin/sh", "-c", cmd], user="root")
    if exit_code != 0:
        return None

    owner = output.decode().strip()
    lowered = owner.lower()
    if not owner or "not owned" in lowered or "no path found" in lowered:
        return None

    if pkg_manager == "dpkg":
        # dpkg -S prints "package: /path"; keep just the package name.
        owner = owner.split(":")[0].strip()
    return owner


def cleanup_pip_patroni(container):
    """Remove any pip-installed Patroni so it cannot shadow the pgedge package.

    A leftover `pip3 install patroni` breaks the Patroni tests in ways that are
    easy to misread: the pip console scripts land in /usr/local/bin, which
    precedes /usr/bin, so `patroni --version` reports the pip build's version
    rather than the packaged one, and the regression test's `import patroni`
    probe resolves to pip's site-packages copy. Both produce version mismatches
    that look like packaging bugs.

    Distro-owned installs are left strictly alone. Every candidate interpreter's
    patroni module is checked against rpm/dpkg first; anything a package owns is
    reported and skipped, never uninstalled. Only unowned (pip) copies and their
    orphaned console scripts are removed, so this is safe to call on a machine
    where pgedge-patroni is already installed.

    Args:
        container: Docker container object with exec_run method

    Returns:
        tuple: (success: bool, cleanup_summary: dict, message: str)
            cleanup_summary contains:
                - pip_installs_removed: list of "<interpreter> (<path>)" removed
                - scripts_removed: list of console script paths removed
                - package_owned_skipped: list of "<path> owned by <package>"
    """

    print("\n--- Checking for a pip-installed Patroni ---")

    cleanup_summary = {
        "pip_installs_removed": [],
        "scripts_removed": [],
        "package_owned_skipped": [],
    }

    pkg_manager = _detect_pkg_manager(container)
    if not pkg_manager:
        print("⚠️ Warning: neither rpm nor dpkg found; cannot distinguish a pip "
              "install from a packaged one, so nothing will be removed.")
        return True, cleanup_summary, "pip Patroni check skipped: no package manager detected"

    # Locate every interpreter that can import patroni, with the real on-disk
    # location of the module. Several names often resolve to one interpreter
    # (python3 -> python3.12), so results are de-duplicated by module path.
    probe = (
        "for py in " + " ".join(_PYTHON_CANDIDATES) + "; do "
        "  command -v $py >/dev/null 2>&1 || continue; "
        "  p=$($py -c 'import patroni, os; "
        "print(os.path.dirname(os.path.realpath(patroni.__file__)))' 2>/dev/null) || continue; "
        '  [ -n "$p" ] && echo "$py|$p"; '
        "done"
    )
    _, output = container.exec_run(["bash", "-c", probe], user="root")

    seen_paths = set()
    found = []
    for line in output.decode().strip().splitlines():
        if "|" not in line:
            continue
        interpreter, module_path = line.split("|", 1)
        module_path = module_path.strip()
        if module_path and module_path not in seen_paths:
            seen_paths.add(module_path)
            found.append((interpreter.strip(), module_path))

    if not found:
        print("✅ No importable patroni module found — nothing to remove.")
    else:
        print(f"Found {len(found)} patroni module location(s): "
              f"{[p for _, p in found]}")

    for interpreter, module_path in found:
        owner = _path_owner(container, pkg_manager, f"{module_path}/__init__.py")
        if owner:
            cleanup_summary["package_owned_skipped"].append(f"{module_path} owned by {owner}")
            print(f"✅ {module_path} is owned by {owner} — leaving it alone")
            continue

        print(f"Removing pip-installed patroni under {interpreter} ({module_path})...")
        exit_code, out = container.exec_run(
            ["bash", "-c", f"{interpreter} -m pip uninstall -y patroni 2>&1"], user="root"
        )
        text = out.decode().strip()

        # Debian 12 / Ubuntu 24.04 mark the system interpreter externally
        # managed (PEP 668); retry once with the documented escape hatch.
        if exit_code != 0 and "externally-managed" in text.lower():
            print("   Interpreter is externally managed; retrying with --break-system-packages")
            exit_code, out = container.exec_run(
                ["bash", "-c",
                 f"{interpreter} -m pip uninstall -y --break-system-packages patroni 2>&1"],
                user="root",
            )
            text = out.decode().strip()

        if exit_code != 0:
            print(f"⚠️ Warning: failed to uninstall patroni under {interpreter}: {text}")
            continue

        cleanup_summary["pip_installs_removed"].append(f"{interpreter} ({module_path})")
        print(f"✅ Uninstalled pip patroni under {interpreter}")

    # Sweep up console scripts pip left behind. A pip uninstall normally removes
    # its own scripts, but a partially-removed or --user install can strand them,
    # and a stranded /usr/local/bin/patroni still wins on PATH.
    for script_dir in _PIP_SCRIPT_DIRS:
        for script in _PATRONI_SCRIPTS:
            path = f"{script_dir}/{script}"
            exit_code, _ = container.exec_run(["/bin/sh", "-c", f"test -e {path}"], user="root")
            if exit_code != 0:
                continue

            owner = _path_owner(container, pkg_manager, path)
            if owner:
                cleanup_summary["package_owned_skipped"].append(f"{path} owned by {owner}")
                print(f"✅ {path} is owned by {owner} — leaving it alone")
                continue

            exit_code, out = container.exec_run(["/bin/sh", "-c", f"rm -f {path}"], user="root")
            if exit_code == 0:
                cleanup_summary["scripts_removed"].append(path)
                print(f"✅ Removed orphaned script {path}")
            else:
                print(f"⚠️ Warning: failed to remove {path}: {out.decode().strip()}")

    message_parts = []
    if cleanup_summary["pip_installs_removed"]:
        message_parts.append(f"{len(cleanup_summary['pip_installs_removed'])} pip install(s) removed")
    if cleanup_summary["scripts_removed"]:
        message_parts.append(f"{len(cleanup_summary['scripts_removed'])} orphaned script(s) removed")
    if cleanup_summary["package_owned_skipped"]:
        message_parts.append(f"{len(cleanup_summary['package_owned_skipped'])} package-owned path(s) left intact")

    if message_parts:
        message = f"pip Patroni cleanup completed: {', '.join(message_parts)}"
    else:
        message = "pip Patroni cleanup completed: no pip-installed Patroni found"

    print(f"\n✅ {message}")

    return True, cleanup_summary, message


def cleanup_pgedge_environment(container, pgdata=None, pguser=None):
    """
    Perform comprehensive cleanup of pgEdge environment including packages, data, and users.

    Package removal is platform-aware: RHEL hosts are queried with rpm and
    cleared with dnf, Debian hosts are queried with dpkg-query and cleared with
    `apt-get purge`. If neither package manager is present the step is skipped
    with a warning rather than silently reporting success.

    Args:
        container: Docker container object with exec_run method
        pgdata: Optional PostgreSQL data directory to remove (e.g., "/tmp/n1")
        pguser: Optional PostgreSQL user to remove (e.g., "postgres")

    Returns:
        tuple: (success: bool, cleanup_summary: dict, message: str)

    Raises:
        Exception: If cleanup fails
    """

    print(f"\n--- Cleaning up pgEdge environment ---")

    cleanup_summary = {
        "packages_removed": [],
        "data_directory_removed": False,
        "user_removed": False
    }

    # Step 1: Remove any installed pgEdge packages.
    #
    # Both the listing and the removal are platform-specific, and both must run
    # through an explicit shell: container.exec_run() with a plain string is
    # split with shlex and executed directly, with no shell involved, so a
    # pipe or glob in that string is passed to the program as a literal
    # argument rather than being interpreted.
    print("Checking for pgEdge packages...")

    exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v dnf"], user="root")
    if exit_code == 0:
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run(["/bin/sh", "-c", "command -v apt-get"], user="root")
        platform = "debian" if exit_code == 0 else None

    if platform is None:
        print("⚠️ Warning: no supported package manager (dnf or apt-get) found; "
              "skipping package removal.")
        packages = []
    elif platform == "rhel":
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c", "rpm -qa | grep pgedge"], user="root"
        )
        # grep exits 1 when nothing matches, which is the normal "clean machine"
        # case rather than an error.
        packages = output.decode().strip().splitlines() if exit_code == 0 else []
    else:  # debian
        # Report installed packages ("ii") and also catch "rc" leftovers —
        # removed but with config files still on disk, which purge should clear.
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c",
             "dpkg-query -W -f='${db:Status-Abbrev} ${binary:Package}\\n' 2>/dev/null "
             "| awk '($1 ~ /^i/ || $1 ~ /^r/) && $2 ~ /pgedge/ {print $2}'"],
            user="root",
        )
        packages = output.decode().strip().splitlines() if exit_code == 0 else []

    packages = [p.strip() for p in packages if p.strip()]

    if not packages:
        if platform:
            print(f" No pgEdge packages found on {platform}, skipping package uninstall step.")
    else:
        print(f"Found {len(packages)} pgEdge package(s) on {platform}: {packages}")
        print("Removing all pgEdge packages...")

        if platform == "rhel":
            remove_cmd = "dnf remove -y 'pgedge-*'"
        else:
            # Purge the discovered names explicitly. apt-get would treat a
            # 'pgedge-*' argument as an unanchored POSIX regex, which can match
            # more than intended; the explicit list has no such ambiguity.
            remove_cmd = (
                "DEBIAN_FRONTEND=noninteractive apt-get purge -y " + " ".join(packages)
            )

        exit_code, output = container.exec_run(["/bin/sh", "-c", remove_cmd], user="root")

        if exit_code != 0:
            raise Exception(f"Failed to remove pgEdge packages: {output.decode()}")

        cleanup_summary["packages_removed"] = packages
        print(f" Successfully removed {len(packages)} pgEdge package(s)")

    # Step 2: Optionally clean data directory
    if pgdata:
        print(f"\nRemoving PostgreSQL data directory: {pgdata}")
        exit_code, output = container.exec_run(f"rm -rf {pgdata}", user="root")

        if exit_code != 0:
            raise Exception(f"Failed to remove data directory {pgdata}: {output.decode()}")

        cleanup_summary["data_directory_removed"] = True
        print(f" Successfully removed data directory: {pgdata}")

    # Step 3: Delete user if specified
    if pguser:
        print(f"\nRemoving user: {pguser}")
        exit_code, output = container.exec_run(f"userdel {pguser}", user="root")

        # userdel may fail if user doesn't exist, which is okay
        if exit_code == 0:
            cleanup_summary["user_removed"] = True
            print(f" Successfully removed user: {pguser}")
        else:
            # Check if it failed because user doesn't exist
            if "does not exist" in output.decode().lower():
                print(f" User {pguser} does not exist (already removed or never created)")
            else:
                print(f" Warning: Failed to remove user {pguser}: {output.decode()}")

    # Build summary message
    message_parts = []
    if cleanup_summary["packages_removed"]:
        message_parts.append(f"{len(cleanup_summary['packages_removed'])} package(s) removed")
    if cleanup_summary["data_directory_removed"]:
        message_parts.append("data directory removed")
    if cleanup_summary["user_removed"]:
        message_parts.append("user removed")

    if message_parts:
        message = f"Cleanup completed: {', '.join(message_parts)}"
    else:
        message = "Cleanup completed: no items to clean"

    print(f"\n {message}")

    return True, cleanup_summary, message


def cleanup_pgbouncer_environment(
    container,
    pgbouncer_config_dir="/etc/pgbouncer",
    pgbouncer_user="pgbouncer",
    pgbouncer_log_dir="/var/log/pgbouncer"
):
    """
    Perform comprehensive cleanup of PgBouncer environment including process, config, logs, and user.

    Args:
        container: Docker container object with exec_run method
        pgbouncer_config_dir: PgBouncer configuration directory (default: "/etc/pgbouncer")
        pgbouncer_user: PgBouncer system user (default: "pgbouncer")
        pgbouncer_log_dir: PgBouncer log directory (default: "/var/log/pgbouncer")

    Returns:
        tuple: (success: bool, cleanup_summary: dict, message: str)

    Raises:
        Exception: If critical cleanup steps fail
    """

    print(f"\n--- Cleaning up PgBouncer environment ---")

    cleanup_summary = {
        "process_stopped": False,
        "config_directory_removed": False,
        "log_directory_removed": False,
        "user_removed": False
    }

    # Step 1: Stop pgbouncer process if running
    print("Checking for running pgbouncer process...")
    exit_code, output = container.exec_run("pgrep -x pgbouncer", user="root")

    if exit_code == 0:
        pid = output.decode().strip()
        print(f"Found pgbouncer process (PID: {pid}), stopping it...")
        kill_exit_code, kill_output = container.exec_run("pkill pgbouncer", user="root")

        # Verify process is stopped
        import time
        time.sleep(1)
        verify_exit_code, verify_output = container.exec_run("pgrep -x pgbouncer", user="root")

        if verify_exit_code != 0:
            cleanup_summary["process_stopped"] = True
            print(f"✅ Successfully stopped pgbouncer process")
        else:
            print(f"⚠️ Warning: pgbouncer process may still be running")
    else:
        print("✅ No pgbouncer process found (already stopped)")

    # Step 2: Remove pgbouncer configuration directory
    if pgbouncer_config_dir:
        print(f"\nRemoving PgBouncer config directory: {pgbouncer_config_dir}")

        # Check if directory exists
        check_exit_code, check_output = container.exec_run(
            f"test -d {pgbouncer_config_dir}",
            user="root"
        )

        if check_exit_code == 0:
            exit_code, output = container.exec_run(f"rm -rf {pgbouncer_config_dir}", user="root")

            if exit_code != 0:
                print(f"⚠️ Warning: Failed to remove config directory {pgbouncer_config_dir}: {output.decode()}")
            else:
                cleanup_summary["config_directory_removed"] = True
                print(f"✅ Successfully removed config directory: {pgbouncer_config_dir}")
        else:
            print(f"✅ Config directory does not exist (already removed or never created)")

    # Step 3: Remove pgbouncer log directory
    if pgbouncer_log_dir:
        print(f"\nRemoving PgBouncer log directory: {pgbouncer_log_dir}")

        # Check if directory exists
        check_exit_code, check_output = container.exec_run(
            f"test -d {pgbouncer_log_dir}",
            user="root"
        )

        if check_exit_code == 0:
            exit_code, output = container.exec_run(f"rm -rf {pgbouncer_log_dir}", user="root")

            if exit_code != 0:
                print(f"⚠️ Warning: Failed to remove log directory {pgbouncer_log_dir}: {output.decode()}")
            else:
                cleanup_summary["log_directory_removed"] = True
                print(f"✅ Successfully removed log directory: {pgbouncer_log_dir}")
        else:
            print(f"✅ Log directory does not exist (already removed or never created)")

    # Step 4: Delete pgbouncer user if specified
    if pgbouncer_user:
        print(f"\nRemoving user: {pgbouncer_user}")

        # Check if user exists first
        check_exit_code, check_output = container.exec_run(f"id {pgbouncer_user}", user="root")

        if check_exit_code == 0:
            exit_code, output = container.exec_run(f"userdel {pgbouncer_user}", user="root")

            if exit_code == 0:
                cleanup_summary["user_removed"] = True
                print(f"✅ Successfully removed user: {pgbouncer_user}")
            else:
                print(f"⚠️ Warning: Failed to remove user {pgbouncer_user}: {output.decode()}")
        else:
            print(f"✅ User {pgbouncer_user} does not exist (already removed or never created)")

    # Build summary message
    message_parts = []
    if cleanup_summary["process_stopped"]:
        message_parts.append("process stopped")
    if cleanup_summary["config_directory_removed"]:
        message_parts.append("config directory removed")
    if cleanup_summary["log_directory_removed"]:
        message_parts.append("log directory removed")
    if cleanup_summary["user_removed"]:
        message_parts.append("user removed")

    if message_parts:
        message = f"PgBouncer cleanup completed: {', '.join(message_parts)}"
    else:
        message = "PgBouncer cleanup completed: no items to clean"

    print(f"\n✅ {message}")

    return True, cleanup_summary, message
#!/usr/bin/env python3
"""
Machine cleanup module for removing packages, data directories, and users.
Supports comprehensive cleanup operations for test environments.
"""


def cleanup_pgedge_environment(container, pgdata=None, pguser=None):
    """
    Perform comprehensive cleanup of pgEdge environment including packages, data, and users.

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

    # Step 1: Check if any pgedge packages exist
    print("Checking for pgEdge packages...")
    exit_code, output = container.exec_run("rpm -qa | grep pgedge", user="root")
    packages = output.decode().strip().splitlines()

    if not packages or exit_code != 0:
        print(" No pgEdge packages found, skipping package uninstall step.")
    else:
        print(f"Found {len(packages)} pgEdge package(s): {packages}")
        print("Removing all pgEdge packages...")
        exit_code, output = container.exec_run("dnf remove -y 'pgedge-*'", user="root")

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
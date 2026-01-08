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
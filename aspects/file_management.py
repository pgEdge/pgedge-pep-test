#!/usr/bin/env python3
"""
Generic file management module for copying files to containers.
Handles file copying, ownership, and permissions.
"""
import os
import subprocess


def copy_config_files_to_container(
    container,
    container_name,
    local_config_dir,
    container_config_dir,
    file_mapping,
    owner="postgres",
    group="postgres",
    permissions="600"
):
    """
    Copy configuration files from host to container with proper ownership and permissions.

    Args:
        container: Docker container object with exec_run method
        container_name: Name of the container (for docker cp command)
        local_config_dir: Local directory containing config files (e.g., "./config/pgbouncer/")
        container_config_dir: Destination directory in container (e.g., "/etc/pgbouncer")
        file_mapping: Dictionary mapping source filenames to destination filenames
                     e.g., {"userlist.txt": "userlist.txt", "deb-pgbouncer.ini": "pgbouncer.ini"}
        owner: Owner for the files (default: "postgres")
        group: Group for the files (default: "postgres")
        permissions: File permissions in octal string format (default: "600")

    Returns:
        tuple: (success: bool, files_copied: list, message: str)

    Raises:
        Exception: If file copy, ownership, or permission setting fails
    """

    print(f"\n--- Copying config files to {container_name} ---")

    # Ensure destination directory exists
    exit_code, output = container.exec_run(
        f"mkdir -p {container_config_dir}",
        user="root"
    )
    if exit_code != 0:
        raise Exception(f"Failed to create config directory {container_config_dir}: {output.decode()}")

    print(f"Created directory: {container_config_dir}")

    files_copied = []

    # Copy each file
    for source_file, dest_file in file_mapping.items():
        local_file = os.path.join(local_config_dir, source_file)
        container_dest = f"{container_name}:{container_config_dir}/{dest_file}"

        # Check if local config file exists
        if not os.path.exists(local_file):
            raise Exception(f"Local config file not found: {local_file}")

        # Copy file from host to container using docker cp
        result = subprocess.run(
            ["docker", "cp", local_file, container_dest],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise Exception(f"Failed to copy {source_file} to container: {result.stderr}")

        print(f" Copied {source_file} as {dest_file} to {container_config_dir}/")

        # Set correct ownership
        chown_exit_code, chown_output = container.exec_run(
            f"chown {owner}:{group} {container_config_dir}/{dest_file}",
            user="root"
        )
        if chown_exit_code != 0:
            raise Exception(f"Failed to set ownership for {dest_file}: {chown_output.decode()}")

        # Set correct permissions
        chmod_exit_code, chmod_output = container.exec_run(
            f"chmod {permissions} {container_config_dir}/{dest_file}",
            user="root"
        )
        if chmod_exit_code != 0:
            raise Exception(f"Failed to set permissions for {dest_file}: {chmod_output.decode()}")

        print(f" Set ownership ({owner}:{group}) and permissions ({permissions}) for {dest_file}")

        files_copied.append(dest_file)

    # Verify all files were copied
    for source_file, dest_file in file_mapping.items():
        exit_code, output = container.exec_run(
            f"test -f {container_config_dir}/{dest_file}",
            user="root"
        )
        if exit_code != 0:
            raise Exception(f"Config file not found after copy: {dest_file}")

    message = f"Successfully copied {len(files_copied)} files to {container_config_dir} with ownership {owner}:{group} and permissions {permissions}"
    print(f" All config files copied successfully")

    return True, files_copied, message
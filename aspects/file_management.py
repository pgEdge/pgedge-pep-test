#!/usr/bin/env python3
"""
Generic file management module for copying files to containers.
Handles file copying, ownership, and permissions.
"""
import os
import re
import subprocess
from pathlib import Path


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


def set_file_permissions(
    container,
    file_path,
    owner,
    group,
    permissions,
    create_user=True,
    user_options="-r -s /sbin/nologin"
):
    """
    Set ownership and permissions on a file in a container.

    Args:
        container: Docker container object with exec_run method
        file_path: Full path to the file in the container (e.g., "/etc/pgbouncer/userlist.txt")
        owner: Owner username for the file
        group: Group name for the file
        permissions: File permissions in octal string format (e.g., "600", "644")
        create_user: Whether to create the user if it doesn't exist (default: True)
        user_options: Options for useradd command if creating user (default: "-r -s /sbin/nologin")

    Returns:
        tuple: (success: bool, file_info: str, message: str)

    Raises:
        Exception: If permission or ownership setting fails
    """

    print(f"\n--- Setting permissions on {file_path} ---")

    # Ensure user exists if create_user is True
    if create_user:
        exit_code, output = container.exec_run(
            f"id {owner}",
            user="root"
        )
        if exit_code != 0:
            print(f"Creating {owner} user...")
            exit_code, output = container.exec_run(
                f"useradd {user_options} {owner}",
                user="root"
            )
            # Ignore error if user already exists
            if exit_code != 0 and "already exists" not in output.decode():
                raise Exception(f"Failed to create {owner} user: {output.decode()}")
            print(f"✅ Created user: {owner}")

    # Set ownership
    exit_code, output = container.exec_run(
        f"chown {owner}:{group} {file_path}",
        user="root"
    )
    if exit_code != 0:
        raise Exception(f"Failed to change ownership: {output.decode()}")

    print(f"✅ Changed ownership to {owner}:{group}")

    # Set permissions
    exit_code, output = container.exec_run(
        f"chmod {permissions} {file_path}",
        user="root"
    )
    if exit_code != 0:
        raise Exception(f"Failed to change permissions: {output.decode()}")

    print(f"✅ Changed permissions to {permissions}")

    # Verify permissions and ownership
    exit_code, output = container.exec_run(
        f"ls -la {file_path}",
        user="root"
    )
    if exit_code != 0:
        raise Exception(f"Failed to verify permissions: {output.decode()}")

    file_info = output.decode().strip()
    print(f"File info: {file_info}")

    # Verify ownership is set correctly
    if owner not in file_info:
        raise Exception(f"Ownership not set correctly. Expected {owner} in: {file_info}")

    print(f"✅ Permissions and ownership verified")

    message = f"Successfully set permissions {permissions} and ownership {owner}:{group} on {file_path}"

    return True, file_info, message


def verify_bundled_files(
    container,
    container_name,
    container_type,
    component,
    package_name,
    project_root
):
    """
    Verify bundled files for a component match expected files.

    This compares the installed files from rpm/deb with expected files
    in expected-output/rpm/ or expected-output/deb/ directory.

    Args:
        container: Docker container object with exec_run method
        container_name: Name of the container
        container_type: Type of container ("rhel" or "deb")
        component: Component name (e.g., "pgedge-lolor_18" or "pgedge-postgresql-18-lolor")
        package_name: Actual package name to query (e.g., "pgedge-lolor_18")
        project_root: Path to project root directory

    Returns:
        tuple: (success: bool, details: dict, message: str)
            details contains:
                - expected_count: number of expected files
                - installed_count: number of installed files
                - missing_files: list of missing files
                - extra_files: list of extra files

    Raises:
        Exception: If file verification fails or expected file not found
    """

    print(f"\n--- Verifying bundled files for {component} on {container_name} ({container_type}) ---")

    # Extract base component name by removing 'pgedge-' prefix and version suffix
    # Example: pgedge-lolor_18 -> lolor (RHEL)
    # Example: pgedge-postgresql-18-lolor -> lolor (Debian)
    if container_type == "rhel":
        base_name = component.replace("pgedge-", "").rsplit('_', 1)[0]
    else:  # deb
        # For Debian packages, handle both formats:
        # - pgedge-postgresql-18-lolor -> lolor (proper Debian format)
        # - pgedge-lolor_18 -> lolor (RHEL format used with Debian container)
        if "postgresql" in component:
            # Debian format: pgedge-postgresql-18-lolor
            base_name = component.split('-')[-1]
        else:
            # RHEL format used on Debian: pgedge-lolor_18
            base_name = component.replace("pgedge-", "").rsplit('_', 1)[0]

    # Determine platform-specific path (rpm for RHEL, deb for Debian)
    platform_dir = "rpm" if container_type == "rhel" else "deb"

    # Get expected file path
    expected_file_path = Path(project_root) / "expected-output" / platform_dir / base_name

    # Check if expected file exists
    if not expected_file_path.exists():
        raise Exception(f"No expected file found for {base_name} at {expected_file_path}")

    # Read expected files
    with open(expected_file_path, 'r') as f:
        expected_files = [line.strip() for line in f if line.strip()]

    print(f"Expected {len(expected_files)} files")

    # Get installed files from container (platform-specific command)
    if container_type == "rhel":
        exit_code, output = container.exec_run(f"rpm -ql {package_name}", user="root")
    else:  # deb
        exit_code, output = container.exec_run(f"dpkg -L {package_name}", user="root")

    if exit_code != 0:
        raise Exception(f"Failed to query files for {package_name}: {output.decode()}")

    installed_files = [line.strip() for line in output.decode().strip().splitlines() if line.strip()]
    print(f"Installed {len(installed_files)} files")

    # Normalize function to remove version-specific suffixes
    def normalize_path(path):
        """Remove version-specific parts like -17, _17, -16, _16, /17/, /18/, etc."""
        # Replace -17, _17, -16, _16, -18, _18 with empty string
        normalized = re.sub(r'[-_](16|17|18)', '', path)
        # Replace /16/, /17/, /18/ with /VERSION/ for directory paths
        normalized = re.sub(r'/(16|17|18)/', '/VERSION/', normalized)
        return normalized

    # Normalize both lists
    expected_normalized = sorted([normalize_path(f) for f in expected_files])
    installed_normalized = sorted([normalize_path(f) for f in installed_files])

    # Find differences
    expected_set = set(expected_normalized)
    installed_set = set(installed_normalized)

    missing_files = sorted(expected_set - installed_set)
    extra_files = sorted(installed_set - expected_set)

    # Report results
    if missing_files:
        print(f"\n⚠️ Missing files ({len(missing_files)}):")
        for f in missing_files[:10]:  # Show first 10
            print(f"  - {f}")
        if len(missing_files) > 10:
            print(f"  ... and {len(missing_files) - 10} more")

    if extra_files:
        print(f"\n⚠️ Extra files ({len(extra_files)}):")
        for f in extra_files[:10]:  # Show first 10
            print(f"  + {f}")
        if len(extra_files) > 10:
            print(f"  ... and {len(extra_files) - 10} more")

    # Prepare details
    details = {
        "expected_count": len(expected_files),
        "installed_count": len(installed_files),
        "missing_files": missing_files,
        "extra_files": extra_files
    }

    # Check if verification passed (no missing files)
    if missing_files:
        message = f"Bundled files verification failed for {component} on {container_name}. Missing {len(missing_files)} expected files."
        return False, details, message

    if not extra_files:
        message = f"All bundled files verified: {component} ({len(expected_files)} files)"
        print(f"✅ {message}")
    else:
        message = f"All expected files present: {component} ({len(expected_files)} files, {len(extra_files)} extra)"
        print(f"✅ {message}")

    return True, details, message

#!/usr/bin/env python3
"""
Generic file management module for copying files to containers.
Handles file copying, ownership, and permissions.
"""
import os
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

    # Normalize and resolve local_config_dir: allow passing relative paths (e.g. "./config/pgbouncer")
    # Resolve relative paths against the repository root (two levels up from this file: repo_root/)
    from pathlib import Path as _Path
    if not isinstance(local_config_dir, _Path):
        local_config_dir = _Path(str(local_config_dir))

    if not local_config_dir.is_absolute():
        repo_root = _Path(__file__).resolve().parent.parent
        local_config_dir = (repo_root / local_config_dir).resolve()

    print(f"Using local config directory: {local_config_dir}")

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
        local_file = os.path.join(str(local_config_dir), source_file)
        container_dest = f"{container_name}:{container_config_dir}/{dest_file}"

        # Check if local config file exists
        if not os.path.exists(local_file):
            raise Exception(f"Local config file not found: {local_file}. (Resolved from local_config_dir: {local_config_dir})")

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
    # Example: pgedge-postgresql-18-system-stats -> system_stats (Debian multi-word)
    import re

    if container_type == "rhel":
        base_name = component.replace("pgedge-", "").rsplit('_', 1)[0]
    else:  # deb
        # For Debian packages, handle both formats:
        # - pgedge-postgresql-18-lolor -> lolor (proper Debian format)
        # - pgedge-postgresql-18-system-stats -> system_stats (multi-word component)
        # - pgedge-lolor_18 -> lolor (RHEL format used with Debian container)
        if "postgresql" in component:
            # Debian format: pgedge-postgresql-{version}-{component}
            # Remove the prefix pattern and extract the component name
            match = re.match(r'pgedge-postgresql-\d+-(.+)', component)
            if match:
                # Get component name and replace hyphens with underscores
                # This handles multi-word components like system-stats -> system_stats
                base_name = match.group(1).replace('-', '_')
            else:
                # Fallback to old logic
                base_name = component.split('-')[-1]
        else:
            # RHEL format used on Debian: pgedge-lolor_18
            base_name = component.replace("pgedge-", "").rsplit('_', 1)[0]

    # Determine platform-specific path (rpm for RHEL, deb for Debian)
    platform_dir = "rpm" if container_type == "rhel" else "deb"

    # Determine expected file path. Accept both underscore and hyphen naming
    expected_dir = Path(project_root) / "expected-output" / platform_dir

    # Candidate names: current base_name (underscores) and hyphen variant
    candidate_names = [base_name, base_name.replace('_', '-')]
    expected_file_path = None
    checked_paths = []
    for name in candidate_names:
        p = expected_dir / name
        checked_paths.append(str(p))
        if p.exists():
            expected_file_path = p
            break

    if expected_file_path is None:
        raise Exception(f"No expected file found for {base_name} at {expected_dir} (checked: {', '.join(checked_paths)})")

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


def verify_binaries_stripped(container, binary_path, container_name=None, binary_names=None):
    """
    Verify that binaries or libraries are stripped (debugging symbols removed).

    This checks executable files or libraries to ensure they have debugging symbols removed (stripped).
    Can check either all files in a directory or specific files.

    Args:
        container: Docker container object with exec_run method
        binary_path: Path to directory containing binaries (e.g., "/usr/bin") or base path for specific files
        container_name: Optional name of container for logging purposes
        binary_names: Optional comma-separated string or list of specific binary/library names to check
                     (e.g., "pgbouncer" or "postgis-3.so,address_standardizer-3.so")
                     If not provided, checks all files in binary_path directory

    Returns:
        tuple: (success: bool, details: dict, message: str)
            details contains:
                - total_binaries: total number of ELF binaries found
                - stripped_binaries: number of stripped binaries
                - unstripped_binaries: list of unstripped binary paths

    Raises:
        Exception: If command execution fails
    """

    display_name = container_name if container_name else "container"

    # Check if 'file' command is available using command -v (more reliable than which)
    check_exit_code, check_output = container.exec_run(
        ["/bin/sh", "-c", "command -v file"],
        user="root"
    )

    if check_exit_code != 0:
        raise Exception(
            f"'file' command not found in container. Please install it first.\n"
            f"For RHEL/RPM: yum install -y file\n"
            f"For Debian/DEB: apt-get install -y file"
        )

    # Parse binary_names if it's a comma-separated string
    if binary_names:
        if isinstance(binary_names, str):
            binary_list = [b.strip() for b in binary_names.split(',') if b.strip()]
        else:
            binary_list = binary_names

        print(f"\n--- Verifying {len(binary_list)} specific binaries are stripped on {display_name} ---")
        print(f"Checking: {', '.join(binary_list)}")

        # Build list of full paths
        full_paths = [f"{binary_path.rstrip('/')}/{binary}" for binary in binary_list]
    else:
        print(f"\n--- Verifying all binaries are stripped in {binary_path} on {display_name} ---")
        full_paths = None

    # Build the file command based on whether we're checking specific files or a directory
    if full_paths:
        # Check specific files
        files_to_check = ' '.join(f'"{path}"' for path in full_paths)

        # Check which files are NOT stripped
        check_cmd = f"file {files_to_check} | grep ELF | grep -v stripped || true"
        count_cmd = f"file {files_to_check} | grep ELF | wc -l"
    else:
        # Check all files in directory
        check_cmd = f"find {binary_path} -type f -exec file {{}} \\; | grep ELF | grep -v stripped || true"
        count_cmd = f"find {binary_path} -type f -exec file {{}} \\; | grep ELF | wc -l"

    # Execute commands with /bin/sh to handle pipes correctly
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", check_cmd],
        user="root"
    )

    output_text = output.decode().strip()

    # Get total count of ELF binaries
    exit_code_total, output_total = container.exec_run(
        ["/bin/sh", "-c", count_cmd],
        user="root"
    )

    if exit_code_total != 0:
        raise Exception(f"Failed to count binaries: {output_total.decode()}")

    total_binaries = int(output_total.decode().strip())

    # Parse unstripped binaries
    unstripped_binaries = []
    if output_text:
        # Each line format: "/path/to/binary: ELF 64-bit LSB executable..."
        for line in output_text.splitlines():
            if line.strip():
                # Extract just the path (before the first colon)
                binary_path = line.split(':')[0].strip()
                unstripped_binaries.append(binary_path)

    stripped_count = total_binaries - len(unstripped_binaries)

    # Prepare details
    details = {
        "total_binaries": total_binaries,
        "stripped_binaries": stripped_count,
        "unstripped_binaries": unstripped_binaries
    }

    print(f"Total ELF binaries found: {total_binaries}")
    print(f"Stripped binaries: {stripped_count}")
    print(f"Unstripped binaries: {len(unstripped_binaries)}")

    # Check if verification passed
    if unstripped_binaries:
        # Unstripped binaries found
        print(f"\n⚠️ Found {len(unstripped_binaries)} unstripped binaries:")
        for binary in unstripped_binaries[:10]:  # Show first 10
            print(f"  - {binary}")
        if len(unstripped_binaries) > 10:
            print(f"  ... and {len(unstripped_binaries) - 10} more")

        location = f"specified files" if binary_names else binary_path
        message = f"Binary stripping verification failed: {len(unstripped_binaries)} unstripped binaries found in {location}"
        return False, details, message

    # All binaries are stripped
    location = f"specified files" if binary_names else binary_path
    message = f"All {total_binaries} binaries/libraries are properly stripped ({location})"
    print(f"✅ {message}")

    return True, details, message


def copy_file_to_container(
    container,
    local_file_path,
    container_file_path,
    permissions="600",
    owner="root"
):
    """
    Copy a file from local filesystem to container with specified permissions.

    This function reads content from a local file and writes it to a container
    using heredoc to avoid issues with special characters.

    Args:
        container: Docker container object with exec_run method
        local_file_path: Path to local file (e.g., "keys/open_api_key")
        container_file_path: Destination path in container (e.g., "/tmp/api_key_file")
        permissions: File permissions in octal string format (default: "600")
        owner: Owner for the file (default: "root")

    Returns:
        tuple: (success: bool, message: str)
            success: True if file was copied successfully
            message: Success or error message

    Raises:
        Exception: If file read, write, or permission setting fails
    """

    print(f"\n--- Copying file to container ---")
    print(f"Source: {local_file_path}")
    print(f"Destination: {container_file_path}")

    # Check if local file exists
    if not os.path.exists(local_file_path):
        message = f"Local file not found: {local_file_path}"
        print(f"❌ {message}")
        return False, message

    # Read the file content from local filesystem
    try:
        with open(local_file_path, 'r') as f:
            file_content = f.read().strip()
        print(f"✓ Read {len(file_content)} bytes from local file")
    except Exception as e:
        message = f"Failed to read local file: {str(e)}"
        print(f"❌ {message}")
        return False, message

    # Create parent directory in container if needed
    parent_dir = os.path.dirname(container_file_path)
    if parent_dir:
        exit_code, output = container.exec_run(
            f"mkdir -p {parent_dir}",
            user="root"
        )
        if exit_code != 0:
            message = f"Failed to create parent directory: {output.decode()}"
            print(f"❌ {message}")
            return False, message

    # Copy file content to container using heredoc
    try:
        exit_code, output = container.exec_run(
            f"bash -c \"cat > {container_file_path} << 'EOFILE'\n{file_content}\nEOFILE\"",
            user="root"
        )
        if exit_code != 0:
            message = f"Failed to write file to container: {output.decode()}"
            print(f"❌ {message}")
            return False, message
        print(f"✓ File written to container")
    except Exception as e:
        message = f"Failed to copy file to container: {str(e)}"
        print(f"❌ {message}")
        return False, message

    # Set file permissions
    try:
        exit_code, output = container.exec_run(
            f"chmod {permissions} {container_file_path}",
            user="root"
        )
        if exit_code != 0:
            message = f"Failed to set file permissions: {output.decode()}"
            print(f"❌ {message}")
            return False, message
        print(f"✓ Permissions set to {permissions}")
    except Exception as e:
        message = f"Failed to set file permissions: {str(e)}"
        print(f"❌ {message}")
        return False, message

    # Set file ownership if not root
    if owner != "root":
        try:
            exit_code, output = container.exec_run(
                f"chown {owner}:{owner} {container_file_path}",
                user="root"
            )
            if exit_code != 0:
                message = f"Failed to set file ownership: {output.decode()}"
                print(f"❌ {message}")
                return False, message
            print(f"✓ Ownership set to {owner}")
        except Exception as e:
            message = f"Failed to set file ownership: {str(e)}"
            print(f"❌ {message}")
            return False, message

    message = f"File successfully copied to {container_file_path} with {permissions} permissions"
    print(f"✅ {message}")
    return True, message

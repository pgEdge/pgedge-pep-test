#!/usr/bin/env python3
"""
Generic package management module for installing packages on containers.
Supports both RHEL-based and Debian-based distributions.
"""


def install_package(container, package_name, pg_major_version=None, install_pg_server=False):
    """
    Install a package on a Docker container.

    Args:
        container: Docker container object with exec_run method
        package_name: Name of the package to install
        pg_major_version: PostgreSQL major version (e.g., "17")
        install_pg_server: If True, also install PostgreSQL server on Debian platforms

    Returns:
        tuple: (success: bool, platform: str, message: str)

    Raises:
        Exception: If package installation fails
    """

    print(f"\n--- Installing {package_name} on container ---")

    # Detect package manager inside the container
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        pkg_mgr = "dnf install -y"
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            # Run apt-get update for Debian/Ubuntu
            container.exec_run("apt-get update", user="root")
            pkg_mgr = "apt-get install -y"
            platform = "debian"
        else:
            raise Exception("No supported package manager found (dnf or apt-get)")

    print(f"Detected platform: {platform}")

    # Install the main package
    print(f"Installing {package_name}...")
    exit_code, output = container.exec_run(
        f"{pkg_mgr} {package_name}",
        user="root"
    )

    if exit_code != 0:
        raise Exception(f"Failed to install {package_name}: {output.decode()}")

    print(f" Successfully installed {package_name}")

    # Install PostgreSQL server package for Debian if requested
    if platform == "debian" and install_pg_server and pg_major_version:
        server_package = f"pgedge-postgresql-{pg_major_version}"
        print(f"\nInstalling PostgreSQL server package: {server_package}...")
        exit_code, output = container.exec_run(
            f"{pkg_mgr} {server_package}",
            user="root"
        )
        if exit_code != 0:
            raise Exception(f"Failed to install {server_package}: {output.decode()}")

        print(f" Successfully installed {server_package}")
        message = f"Package {package_name} and {server_package} installed successfully on {platform}"
    else:
        message = f"Package {package_name} installed successfully on {platform}"

    return True, platform, message


def uninstall_package(container, package_name):
    """
    Uninstall a package from a Docker container.

    Args:
        container: Docker container object with exec_run method
        package_name: Name of the package to uninstall

    Returns:
        tuple: (success: bool, platform: str, message: str)

    Raises:
        Exception: If package uninstallation fails
    """

    print(f"\n--- Uninstalling {package_name} from container ---")

    # Detect package manager
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        pkg_mgr = "dnf remove -y"
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            pkg_mgr = "apt-get purge -y"
            platform = "debian"
        else:
            raise Exception("No supported package manager found (dnf or apt-get)")

    print(f"Detected platform: {platform}")

    # Uninstall the package
    print(f"Uninstalling {package_name}...")
    exit_code, output = container.exec_run(
        ## Comment out the line due to not working on Debian-based systems. User not properly removed.
        #f"{pkg_mgr} {package_name}",
        f"{pkg_mgr} pgedge-*",
        user="root"
    )

    if exit_code != 0:
        raise Exception(f"Failed to uninstall {package_name}: {output.decode()}")

    print(f" Successfully uninstalled {package_name}")

    message = f"Package {package_name} uninstalled successfully from {platform}"
    return True, platform, message


def normalize_version(version_string, package_name=""):
    """
    Normalize a version string to handle beta versions with different formats.

    Beta normalization is only applied if:
    1. The version string contains "beta", OR
    2. The package name contains keywords: vectorizer, anonymizer, rag, mcp, nla

    Handles formats like:
    - 1.0-beta2, 1.0.0-beta1 (hyphen separator)
    - 1.0beta2, 1.0.0beta2 (no separator)
    - 1.0, 1.0.0 (different precision)
    - 16.11-1.bullseye (Debian packaging suffix)

    Args:
        version_string: Version string to normalize
        package_name: Optional package name to determine if beta logic should apply

    Returns:
        str: Normalized version string in format "1.0.0.beta2" (dots as separators) or "1.0.0" for non-beta
    """
    import re

    # Convert to lowercase for case-insensitive comparison
    version = version_string.lower().strip()
    package_lower = package_name.lower()

    # Strip Debian/Ubuntu packaging suffixes like -1.bullseye, -2.jammy, etc.
    # Pattern: -<digit>[.<distro>] at the end of version string
    version = re.sub(r'-\d+\.[a-z]+$', '', version)
    version = re.sub(r'-\d+$', '', version)

    # Check if this is a beta package
    beta_package_keywords = ['vectorizer', 'anonymizer', 'rag', 'mcp', 'nla']
    is_beta_package = any(keyword in package_lower for keyword in beta_package_keywords)
    has_beta_in_version = 'beta' in version

    # Only apply beta normalization if it's a beta package or version contains 'beta'
    beta_suffix = ""
    if is_beta_package or has_beta_in_version:
        # Handle beta versions with hyphen separator: 1.0-beta2 -> 1.0.beta2
        version = re.sub(r'-beta', '.beta', version)

        # Split into version parts and beta suffix
        beta_match = re.search(r'\.?beta(\d*)', version)
        if beta_match:
            beta_suffix = f".beta{beta_match.group(1)}"
            version = version[:beta_match.start()]

    # Split version by dots
    version_parts = version.split('.')

    # Pad to 3 parts (major.minor.patch)
    while len(version_parts) < 3:
        version_parts.append('0')

    # Reconstruct normalized version
    normalized = '.'.join(version_parts[:3]) + beta_suffix

    return normalized


def verify_package_version(container, package_name, expected_version):
    """
    Verify the installed version of a package.

    Supports beta versions with different formats:
    - 1.0-beta2, 1.0.0-beta1 (hyphen separator)
    - 1.0beta2, 1.0.0beta2 (no separator)
    - Different version precision (1.0 vs 1.0.0)

    Args:
        container: Docker container object with exec_run method
        package_name: Name of the package to verify
        expected_version: Expected version string to match

    Returns:
        tuple: (success: bool, platform: str, installed_version: str, message: str)

    Raises:
        Exception: If version verification fails or version doesn't match
    """

    print(f"\n--- Verifying {package_name} version on container ---")
    print(f"Expected version: {expected_version}")

    # Detect package manager inside the container
    exit_code, _ = container.exec_run("command -v dnf", user="root")
    if exit_code == 0:
        # RHEL-based: use rpm to query version
        version_cmd = f"rpm -q --queryformat '%{{VERSION}}' {package_name}"
        platform = "rhel"
    else:
        exit_code, _ = container.exec_run("/bin/bash -c 'command -v apt-get'", user="root")
        if exit_code == 0:
            # Debian-based: use dpkg-query to get version
            version_cmd = f"dpkg-query --showformat='${{Version}}' --show {package_name}"
            platform = "debian"
        else:
            raise Exception("No supported package manager found (dnf or apt-get)")

    # Get installed version
    exit_code, output = container.exec_run(version_cmd, user="root")

    if exit_code != 0:
        raise Exception(f"Failed to query {package_name} version: {output.decode()}")

    installed_version = output.decode().strip()
    print(f"Installed version: {installed_version}")

    # Normalize both versions for comparison to handle beta formats
    # Pass package_name to determine if beta logic should apply
    normalized_expected = normalize_version(expected_version, package_name)
    normalized_installed = normalize_version(installed_version, package_name)

    print(f"Normalized expected: {normalized_expected}")
    print(f"Normalized installed: {normalized_installed}")

    # Version comparison - check if normalized expected version is contained in normalized installed version
    if normalized_expected not in normalized_installed:
        raise Exception(
            f"Version mismatch for {package_name} on {platform}\n"
            f"Expected: {expected_version} (normalized: {normalized_expected})\n"
            f"Installed: {installed_version} (normalized: {normalized_installed})"
        )

    message = f"Version verified: {package_name} {installed_version} on {platform}"
    print(f" {message}")

    return True, platform, installed_version, message


def validate_bundled_file(container, file_path):
    """
    Validate that a bundled file exists in the container.

    Args:
        container: Docker container object with exec_run method
        file_path: Path to the file/directory to validate

    Returns:
        tuple: (success: bool, file_info: str, message: str)

    Raises:
        Exception: If file validation fails
    """

    print(f"\n--- Validating bundled file: {file_path} ---")

    # Check if file/directory exists
    exit_code, output = container.exec_run(
        f"test -e {file_path}",
        user="root"
    )

    if exit_code != 0:
        raise Exception(f"Bundled file/directory not found: {file_path}")

    # Get file type info
    exit_code, output = container.exec_run(
        f"ls -la {file_path}",
        user="root"
    )

    if exit_code != 0:
        raise Exception(f"Failed to get file info for {file_path}: {output.decode()}")

    file_info = output.decode().strip()
    print(f"File info: {file_info}")

    message = f"Bundled file validated: {file_path}"
    print(f" {message}")

    return True, file_info, message
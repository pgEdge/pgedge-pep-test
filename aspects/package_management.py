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
            pkg_mgr = "apt purge -y"
            platform = "debian"
        else:
            raise Exception("No supported package manager found (dnf or apt-get)")

    print(f"Detected platform: {platform}")

    # Uninstall the package
    print(f"Uninstalling {package_name}...")
    exit_code, output = container.exec_run(
        f"{pkg_mgr} {package_name}",
        user="root"
    )

    if exit_code != 0:
        raise Exception(f"Failed to uninstall {package_name}: {output.decode()}")

    print(f" Successfully uninstalled {package_name}")

    message = f"Package {package_name} uninstalled successfully from {platform}"
    return True, platform, message


def verify_package_version(container, package_name, expected_version):
    """
    Verify the installed version of a package.

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

    # Version comparison - check if expected version is contained in installed version
    if expected_version not in installed_version:
        raise Exception(
            f"Version mismatch for {package_name} on {platform}\n"
            f"Expected: {expected_version}\n"
            f"Installed: {installed_version}"
        )

    message = f"Version verified: {package_name} {installed_version} on {platform}"
    print(f" {message}")

    return True, platform, installed_version, message
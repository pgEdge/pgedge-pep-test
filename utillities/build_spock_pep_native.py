#!/usr/bin/env python3
"""
Spock Build Script for pgEdge PostgreSQL RPMs
Automates the installation of dependencies and building of Spock extension
"""

import subprocess
import sys
import os


def run_cmd(cmd, check=True, shell=True):
    """Execute a shell command and return the result."""
    print(f"\n>>> Running: {cmd}")
    result = subprocess.run(cmd, shell=shell, check=check)
    return result.returncode == 0


def get_user_input():
    """Get PostgreSQL version and Spock branch from user."""
    print("=" * 60)
    print("Spock Build Script for pgEdge PostgreSQL RPMs")
    print("=" * 60)

    pg_version = input("\nEnter PostgreSQL version (e.g., 15, 16, 17, 18): ").strip()
    if not pg_version.isdigit() or int(pg_version) < 12:
        print("Error: Invalid PostgreSQL version")
        sys.exit(1)

    spock_branch = input("Enter Spock branch (press Enter for 'main'): ").strip()
    if not spock_branch:
        spock_branch = "main"

    print(f"\nConfiguration:")
    print(f"  PostgreSQL Version: {pg_version}")
    print(f"  Spock Branch: {spock_branch}")

    confirm = input("\nProceed with installation? (y/n): ").strip().lower()
    if confirm != 'y':
        print("Aborted by user.")
        sys.exit(0)

    return pg_version, spock_branch


def install_dependencies():
    """Install required system dependencies."""
    print("\n" + "=" * 60)
    print("Step 1: Installing system dependencies...")
    print("=" * 60)

    deps = [
        "epel-release",
        "git",
        "gcc",
        "make",
        "krb5-devel",
        "perl-IPC-Run",
        "openssl-devel",
        "libxml2-devel",
        "libxslt-devel",
        "readline-devel",
        "zlib-devel",
        "redhat-rpm-config",
        "annobin",
        "jansson-devel",
        "clang"
    ]

    # Enable CRB repository for perl-IPC-Run
    run_cmd("dnf config-manager --set-enabled crb", check=False)
    run_cmd(f"dnf install -y {' '.join(deps)}")


def install_pgedge_repo():
    """Install pgEdge repository."""
    print("\n" + "=" * 60)
    print("Step 2: Installing pgEdge repository...")
    print("=" * 60)

    run_cmd("dnf install -y https://dnf.pgedge.com/reporpm/pgedge-release-latest.noarch.rpm", check=False)


def install_postgresql(pg_version):
    """Install PostgreSQL server and development packages."""
    print("\n" + "=" * 60)
    print(f"Step 3: Installing PostgreSQL {pg_version}...")
    print("=" * 60)

    packages = [
        f"pgedge-postgresql{pg_version}-server",
        f"pgedge-postgresql{pg_version}-devel",
        f"pgedge-postgresql{pg_version}-llvmjit"
    ]
    run_cmd(f"dnf install -y {' '.join(packages)}")


def setup_path(pg_version):
    """Add pg_config to PATH."""
    print("\n" + "=" * 60)
    print("Step 4: Setting up PATH...")
    print("=" * 60)

    pg_bin = f"/usr/pgsql-{pg_version}/bin"
    os.environ["PATH"] = f"{pg_bin}:{os.environ['PATH']}"
    print(f"Added {pg_bin} to PATH")

    # Verify pg_config is accessible
    run_cmd("pg_config --version")


def clone_spock(pg_version):
    """Clone Spock repository."""
    print("\n" + "=" * 60)
    print("Step 5: Cloning Spock repository...")
    print("=" * 60)

    spock_dir = f"/usr/pgsql-{pg_version}/spock"

    if os.path.exists(spock_dir):
        print(f"Directory {spock_dir} already exists.")
        remove = input("Remove and re-clone? (y/n): ").strip().lower()
        if remove == 'y':
            run_cmd(f"rm -rf {spock_dir}")
        else:
            return spock_dir

    os.chdir(f"/usr/pgsql-{pg_version}")
    run_cmd("git clone https://github.com/pgEdge/spock.git")

    return spock_dir


def checkout_branch(spock_dir, branch):
    """Checkout specified branch if not main."""
    print("\n" + "=" * 60)
    print(f"Step 6: Checking out branch '{branch}'...")
    print("=" * 60)

    os.chdir(spock_dir)

    if branch != "main":
        run_cmd(f"git checkout {branch}")
    else:
        print("Using main branch (no checkout needed)")


def build_spock(spock_dir):
    """Build and install Spock."""
    print("\n" + "=" * 60)
    print("Step 7: Building Spock...")
    print("=" * 60)

    os.chdir(spock_dir)
    run_cmd("USE_PGXS=1 make clean", check=False)
    run_cmd("USE_PGXS=1 make")

    print("\n" + "=" * 60)
    print("Step 8: Installing Spock...")
    print("=" * 60)

    run_cmd("USE_PGXS=1 make install")


def main():
    # Check if running as root
    if os.geteuid() != 0:
        print("Error: This script must be run as root")
        sys.exit(1)

    try:
        pg_version, spock_branch = get_user_input()
        install_dependencies()
        install_pgedge_repo()
        install_postgresql(pg_version)
        setup_path(pg_version)
        spock_dir = clone_spock(pg_version)
        checkout_branch(spock_dir, spock_branch)
        build_spock(spock_dir)

        print("\n" + "=" * 60)
        print("SUCCESS! Spock has been built and installed.")
        print("=" * 60)
        print(f"\nSpock installed for PostgreSQL {pg_version}")
        print(f"Branch: {spock_branch}")
        print(f"\nTo use Spock, add to postgresql.conf:")
        print("  shared_preload_libraries = 'spock'")
        print("\nThen restart PostgreSQL and create the extension:")
        print("  CREATE EXTENSION spock;")

    except subprocess.CalledProcessError as e:
        print(f"\nError: Command failed with exit code {e.returncode}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nAborted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
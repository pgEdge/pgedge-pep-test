#!/usr/bin/env python3
"""
Generic PostgreSQL server management module for container-based testing.
Supports initialization, configuration, starting, stopping, and connection testing.
"""

import os
import re

# ---------------------------------------------------------------------------
# output_plugin_libraries
# ---------------------------------------------------------------------------
# From PostgreSQL 16.15 / 17.11 / 18.5 / 19.0beta3 onward, logical decoding
# output plugins must be allow-listed in postgresql.conf via
# output_plugin_libraries before the server will load them. Spock's
# spock_output plugin is therefore not loadable by default, which breaks
# cross-wired (2+ node) Spock and zodan clusters.
#
# Earlier point releases do not recognise the GUC at all — setting it there
# makes the server fail to start — so every caller must gate on the running
# PG_VERSION rather than applying it unconditionally.

# Minimum point release per major version that introduced the GUC.
OUTPUT_PLUGIN_LIBRARIES_MIN_VERSION = {
    16: "16.15",
    17: "17.11",
    18: "18.5",
    19: "19.0beta3",
}

# Default allow-list: pgoutput and test_decoding are the in-tree plugins,
# spock_output is Spock's.
DEFAULT_OUTPUT_PLUGIN_LIBRARIES = "pgoutput, test_decoding, spock_output"


def parse_pg_version(version):
    """Parse a PostgreSQL version string into a sortable tuple.

    Handles release versions ("18.5") and pre-releases ("19.0beta3",
    "19.0rc1"). Pre-release ordering follows PostgreSQL's own:
        19.0beta1 < 19.0beta3 < 19.0rc1 < 19.0

    Returns:
        tuple: (major, minor, stage_rank, stage_number) where stage_rank is
        0 for beta, 1 for rc and 2 for a final release.
    """
    if not version:
        return None

    match = re.match(r"^(\d+)(?:\.(\d+))?(?:(beta|rc)(\d+))?", str(version).strip())
    if not match:
        return None

    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    stage = match.group(3)
    stage_num = int(match.group(4) or 0)

    stage_rank = {"beta": 0, "rc": 1}.get(stage, 2)
    return (major, minor, stage_rank, stage_num)


def requires_output_plugin_libraries(pg_version=None):
    """Return True when the given PostgreSQL version needs output_plugin_libraries.

    Args:
        pg_version: Version string such as "18.5". Defaults to the PG_VERSION
            environment variable.

    Returns:
        bool: True when the GUC exists and must be set for spock_output to load.
    """
    if pg_version is None:
        pg_version = os.getenv("PG_VERSION", "")

    parsed = parse_pg_version(pg_version)
    if parsed is None:
        # Unparseable version — don't risk emitting a GUC the server may reject.
        return False

    major = parsed[0]
    known = sorted(OUTPUT_PLUGIN_LIBRARIES_MIN_VERSION)

    if major > known[-1]:
        # Majors newer than any we know about always include the change.
        return True
    if major < known[0]:
        return False

    minimum = OUTPUT_PLUGIN_LIBRARIES_MIN_VERSION.get(major)
    if minimum is None:
        return False

    return parsed >= parse_pg_version(minimum)


def output_plugin_libraries_guc(pg_version=None, libraries=None, quoted=True):
    """Build the output_plugin_libraries GUC entry for a cluster's postgresql.conf.

    Returns an empty dict on PostgreSQL versions that predate the GUC, so it
    can be merged into a guc_parameters dict unconditionally:

        guc_parameters = {"shared_preload_libraries": "'spock'", ...}
        guc_parameters.update(pg_server_management.output_plugin_libraries_guc())

    Args:
        pg_version: Version string; defaults to the PG_VERSION env var.
        libraries: Comma-separated plugin list; defaults to
            OUTPUT_PLUGIN_LIBRARIES env var, then the pgoutput/test_decoding/
            spock_output default.
        quoted: Wrap the value in single quotes (needed for postgresql.conf,
            not for Patroni YAML which quotes values itself).

    Returns:
        dict: {"output_plugin_libraries": "<value>"} or {} when not required.
    """
    if not requires_output_plugin_libraries(pg_version):
        return {}

    if libraries is None:
        libraries = os.getenv("OUTPUT_PLUGIN_LIBRARIES", DEFAULT_OUTPUT_PLUGIN_LIBRARIES)

    return {"output_plugin_libraries": f"'{libraries}'" if quoted else libraries}


def execute_psql_query(container, pgbin, pgport, pguser, query, dbname="postgres"):
    """
    Execute a PostgreSQL query using psql.

    Args:
        container: Docker container object
        pgbin: Path to PostgreSQL binaries
        pgport: PostgreSQL port
        pguser: PostgreSQL user
        query: SQL query to execute
        dbname: Database name (default: postgres)

    Returns:
        tuple: (exit_code, output_string)
    """
    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -d {dbname} -c \"{query}\"",
        user=pguser,
    )
    return exit_code, output.decode()


def init_cluster(container, pgbin, pgdata, pguser, guc_parameters=None):
    """
    Initialize a PostgreSQL cluster and configure GUC parameters.

    Args:
        container: Docker container object with exec_run method
        pgbin: Path to PostgreSQL binaries (e.g., "/usr/pgsql-17/bin")
        pgdata: Path to PostgreSQL data directory (e.g., "/tmp/n1")
        pguser: PostgreSQL user to run commands as (e.g., "postgres")
        guc_parameters: Optional dict of GUC parameters to add to postgresql.conf
                       Example: {"shared_preload_libraries": "'snowflake'",
                                "track_commit_timestamp": "on"}

    Returns:
        tuple: (success: bool, config_content: str, message: str)

    Raises:
        Exception: If cluster initialization fails
    """

    print(f"\n--- Initializing PostgreSQL cluster ---")
    print(f"PGDATA: {pgdata}")
    print(f"PGBIN: {pgbin}")

    # Remove existing data directory
    print("Removing existing data directory...")
    container.exec_run(f"rm -rf {pgdata}", user=pguser)

    # Run initdb
    print("Running initdb...")
    exit_code, output = container.exec_run(
        f"{pgbin}/initdb -D {pgdata}", user=pguser
    )

    if exit_code != 0:
        raise Exception(f"Initdb failed: {output.decode()}")

    print(" PostgreSQL cluster initialized successfully")

    # Configure GUC parameters if provided
    config_content = ""
    if guc_parameters:
        print("\nConfiguring GUC parameters in postgresql.conf...")

        # Add blank line
        container.exec_run(
            f'sh -c "echo >> {pgdata}/postgresql.conf"',
            user=pguser
        )

        # Add comment
        exit_code, output = container.exec_run(
            f'sh -c "echo \\"# Custom GUC Parameters\\" >> {pgdata}/postgresql.conf"',
            user=pguser
        )
        if exit_code != 0:
            raise Exception(f"Failed to add comment: {output.decode()}")

        # Add each GUC parameter
        for param_name, param_value in guc_parameters.items():
            print(f"Adding parameter: {param_name} = {param_value}")
            exit_code, output = container.exec_run(
                f'sh -c "echo \\"{param_name} = {param_value}\\" >> {pgdata}/postgresql.conf"',
                user=pguser
            )
            if exit_code != 0:
                raise Exception(f"Failed to add {param_name}: {output.decode()}")

        # Verify the parameters were added
        exit_code, output = container.exec_run(
            f"tail -n {len(guc_parameters) + 5} {pgdata}/postgresql.conf",
            user=pguser
        )

        config_content = output.decode()
        print(f"\nLast lines of postgresql.conf:\n{config_content}")

        # Verify each parameter is in the config
        for param_name, param_value in guc_parameters.items():
            expected_line = f"{param_name} = {param_value}"
            if expected_line not in config_content:
                raise Exception(f"Parameter '{param_name}' not found in postgresql.conf")
            print(f" Verified: {param_name} = {param_value}")

        print(" All GUC parameters configured and verified")

    message = "PostgreSQL cluster initialized successfully"
    if guc_parameters:
        message += f" with {len(guc_parameters)} custom GUC parameters"

    return True, config_content, message


def start_server(container, pgbin, pgdata, pgport, pguser):
    """
    Start a PostgreSQL server.

    Args:
        container: Docker container object with exec_run method
        pgbin: Path to PostgreSQL binaries (e.g., "/usr/pgsql-17/bin")
        pgdata: Path to PostgreSQL data directory (e.g., "/tmp/n1")
        pgport: PostgreSQL port (e.g., "5432")
        pguser: PostgreSQL user to run commands as (e.g., "postgres")

    Returns:
        tuple: (success: bool, output: str, message: str)

    Raises:
        Exception: If server start fails
    """

    print(f"\n--- Starting PostgreSQL server ---")
    print(f"PGDATA: {pgdata}")
    print(f"Port: {pgport}")

    # Ensure data directory exists and has proper permissions
    container.exec_run(f"mkdir -p {pgdata}", user="root")

    # Optionally check the directory
    exit_code, output = container.exec_run(f"ls -l {pgdata}", user=pguser)
    dir_info = output.decode()
    print(f"Data directory info:\n{dir_info}")

    # Start PostgreSQL server
    print(f"Starting PostgreSQL with pg_ctl...")
    exit_code, output = container.exec_run(
        f"{pgbin}/pg_ctl -D {pgdata} -o '-p {pgport}' -l {pgdata}/logfile start",
        user=pguser,
    )

    if exit_code != 0:
        raise Exception(f"pg_ctl start failed: {output.decode()}")

    server_output = output.decode()
    print(f" PostgreSQL server started successfully")

    message = f"PostgreSQL server started on port {pgport}"
    return True, server_output, message


def stop_server(container, pgbin, pgdata, pgport, pguser):
    """
    Stop a PostgreSQL server.

    Args:
        container: Docker container object with exec_run method
        pgbin: Path to PostgreSQL binaries (e.g., "/usr/pgsql-17/bin")
        pgdata: Path to PostgreSQL data directory (e.g., "/tmp/n1")
        pgport: PostgreSQL port (e.g., "5432")
        pguser: PostgreSQL user to run commands as (e.g., "postgres")

    Returns:
        tuple: (success: bool, output: str, message: str)

    Raises:
        Exception: If server stop fails
    """

    print(f"\n--- Stopping PostgreSQL server ---")
    print(f"PGDATA: {pgdata}")

    exit_code, output = container.exec_run(
        f"{pgbin}/pg_ctl -D {pgdata} -o '-p {pgport}' -l {pgdata}/logfile stop",
        user=pguser,
    )

    if exit_code != 0:
        raise Exception(f"pg_ctl stop failed: {output.decode()}")

    server_output = output.decode()
    print(f" PostgreSQL server stopped successfully")

    message = "PostgreSQL server stopped successfully"
    return True, server_output, message


def check_connection(container, pgbin, pgport, pguser):
    """
    Check PostgreSQL connection by running a simple query.

    Args:
        container: Docker container object with exec_run method
        pgbin: Path to PostgreSQL binaries (e.g., "/usr/pgsql-17/bin")
        pgport: PostgreSQL port (e.g., "5432")
        pguser: PostgreSQL user to run commands as (e.g., "postgres")

    Returns:
        tuple: (success: bool, version_output: str, message: str)

    Raises:
        Exception: If connection check fails
    """

    print(f"\n--- Checking PostgreSQL connection ---")
    print(f"Port: {pgport}")
    print(f"User: {pguser}")

    exit_code, output = container.exec_run(
        f"{pgbin}/psql -p {pgport} -U {pguser} -c 'SELECT version();'",
        user=pguser,
    )

    if exit_code != 0:
        raise Exception(f"PostgreSQL connection failed: {output.decode()}")

    version_output = output.decode()
    print(f"PostgreSQL is running:\n{version_output}")

    message = "PostgreSQL connection successful"
    return True, version_output, message
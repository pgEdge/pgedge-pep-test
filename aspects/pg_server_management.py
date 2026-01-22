#!/usr/bin/env python3
"""
Generic PostgreSQL server management module for container-based testing.
Supports initialization, configuration, starting, stopping, and connection testing.
"""


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
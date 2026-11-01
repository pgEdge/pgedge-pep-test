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
# makes the server fail to start — so every caller must gate on the server it
# is configuring rather than applying it unconditionally.
#
# That means the version reported by that server's own binary
# (output_plugin_libraries_guc_for_server), read after the server is
# installed. Never the configured PG_VERSION: a channel can serve older point
# releases than configuration/configN.env names (daily served 16.13 / 17.9 /
# 18.3 while the configs said 16.15 / 17.11 / 18.6, so no node started). Nor
# a pg_config on PATH: with two majors installed it reports the newer one.

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


def requires_output_plugin_libraries(pg_version):
    """Return True when the given PostgreSQL version needs output_plugin_libraries.

    Args:
        pg_version: Version string such as "18.5" or "19rc1", as reported by the
            server being configured (see server_version).

    Returns:
        bool: True when the GUC exists and must be set for spock_output to load.

    Raises:
        ValueError: If the version cannot be parsed.
    """
    parsed = parse_pg_version(pg_version)
    if parsed is None:
        raise ValueError(f"cannot parse PostgreSQL version {pg_version!r}")

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


def output_plugin_libraries_guc(pg_version, libraries=None, quoted=True):
    """Build the output_plugin_libraries GUC entry for a cluster's postgresql.conf.

    Returns an empty dict on PostgreSQL versions that predate the GUC. Callers
    configuring a cluster use output_plugin_libraries_guc_for_server, which
    reads the version from the server itself.

    Args:
        pg_version: Version string reported by the server being configured.
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


class ServerVersionError(Exception):
    """The PostgreSQL server's own binary did not report a usable version."""


def server_version(container, pgbin, pguser):
    """Return the version of the PostgreSQL server in pgbin, as its binary reports it.

    Runs `<pgbin>/postgres --version` in the container, so the answer is about
    the exact server a cluster is created and started with. Call it after the
    server is installed (or upgraded).

    Args:
        container: Docker container object with exec_run method
        pgbin: Path to that server's binaries (e.g. "/usr/pgsql-18/bin")
        pguser: User to run the binary as

    Returns:
        str: The version, e.g. "18.3", "16.15", "19rc1".

    Raises:
        ServerVersionError: If the binary fails or reports no parseable version.
            There is deliberately no fallback to the configured PG_VERSION.
    """
    command = f"{pgbin.rstrip('/')}/postgres --version"
    exit_code, output = container.exec_run(command, user=pguser)
    text = output.decode(errors="replace").strip() if output else ""
    match = re.search(r"\(PostgreSQL\)\s+(\d+(?:\.\d+)?(?:(?:beta|rc)\d+)?)\b", text)
    if exit_code != 0 or not match or parse_pg_version(match.group(1)) is None:
        reason = f"exit {exit_code}" if exit_code != 0 else "no PostgreSQL version in its output"
        raise ServerVersionError(
            f"Could not determine the PostgreSQL server version from `{command}` ({reason}): "
            f"{text[:300]!r}. The configured PG_VERSION is not used as a fallback; "
            f"check that the server is installed at {pgbin}."
        )
    return match.group(1)


def output_plugin_libraries_guc_for_server(container, pgbin, pguser, libraries=None, quoted=True):
    """output_plugin_libraries for the server in pgbin, decided by its own version.

    Returns an empty dict when that server predates the GUC, so it can be merged
    into a guc_parameters dict unconditionally:

        guc_parameters = {"shared_preload_libraries": "'spock'", ...}
        guc_parameters.update(
            pg_server_management.output_plugin_libraries_guc_for_server(container, pgbin, pguser))

    Raises:
        ServerVersionError: See server_version.
    """
    version = server_version(container, pgbin, pguser)
    guc = output_plugin_libraries_guc(version, libraries=libraries, quoted=quoted)
    print(f"PostgreSQL server at {pgbin.rstrip('/')}: {version}; output_plugin_libraries "
          f"{'set' if guc else 'not set (this release predates it)'}")
    return guc


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


# Default database encoding for every test cluster.
#
# initdb with no --encoding derives the encoding from the environment's locale.
# These containers and AMIs run with LANG unset (C/POSIX locale), so initdb
# picks SQL_ASCII — and some extensions refuse to install on a non-UTF8
# database. pg_duckdb, for example, aborts with:
#   "pg_duckdb can only be installed in a Postgres database with UTF8 encoding,
#    this one is encoded using SQL_ASCII."
# which also takes down coldfront, since CREATE EXTENSION coldfront CASCADEs to
# pg_duckdb. No pgEdge deployment runs SQL_ASCII, so UTF8 is both the realistic
# and the compatible choice.
DEFAULT_ENCODING = "UTF8"


def init_cluster(container, pgbin, pgdata, pguser, guc_parameters=None,
                 encoding=DEFAULT_ENCODING, locale=None):
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
        encoding: Database encoding passed to initdb (default UTF8 — see
                 DEFAULT_ENCODING above). Pass None to let initdb derive the
                 encoding from the environment locale, restoring the old
                 behaviour.
        locale: Optional locale for initdb. When omitted, --no-locale is used so
               the cluster keeps the C locale it has always had; initdb allows
               the C locale to pair with any explicit encoding, whereas a
               non-C locale whose codeset differs from `encoding` is rejected.

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

    # Build the initdb command
    initdb_cmd = f"{pgbin}/initdb -D {pgdata}"
    if encoding:
        initdb_cmd += f" --encoding={encoding}"
        # Keep the C locale (what these hosts already produce) so only the
        # encoding changes; collation behaviour is unaffected.
        initdb_cmd += f" --locale={locale}" if locale else " --no-locale"
    elif locale:
        initdb_cmd += f" --locale={locale}"

    # Run initdb
    print(f"Running initdb: {initdb_cmd}")
    exit_code, output = container.exec_run(initdb_cmd, user=pguser)

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


_LOG_TAIL_LINES = 20
_LOG_LINE_MAX = 300
_CONTROL_CHARS = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]")
_PASSWORD_VALUE = re.compile(r"(?i)\b(\w*password)\s*=\s*\S+")


def _server_log_tail(container, pgdata, pguser):
    """The last lines of a server's logfile, sanitized for a failure message.

    pg_ctl only says "could not start server"; the reason is in the log. The
    tail is cut to a few short lines, control characters are removed and
    password values redacted. Returns "" when the log cannot be read.
    """
    logfile = f"{pgdata}/logfile"
    try:
        exit_code, output = container.exec_run(f"tail -n {_LOG_TAIL_LINES} {logfile}", user=pguser)
    except Exception:
        return ""
    text = output.decode(errors="replace") if output else ""
    if exit_code != 0 or not text.strip():
        return ""
    lines = []
    for line in text.splitlines()[-_LOG_TAIL_LINES:]:
        line = _PASSWORD_VALUE.sub(r"\1=<redacted>", _CONTROL_CHARS.sub("", line))
        lines.append(line if len(line) <= _LOG_LINE_MAX else line[:_LOG_LINE_MAX] + " …")
    return f"\n--- last {len(lines)} lines of {logfile} ---\n" + "\n".join(lines)


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
        raise Exception(f"pg_ctl start failed: {output.decode()}"
                        f"{_server_log_tail(container, pgdata, pguser)}")

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
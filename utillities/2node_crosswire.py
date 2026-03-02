#!/usr/bin/env python3
"""
Spock Two-Node Cluster Setup Script
Sets up bidirectional replication between two PostgreSQL nodes using Spock.
"""

import psycopg2
from psycopg2 import sql
import sys

# Node configuration
NODES = {
    'n1': {
        'host': 'localhost',
        'port': 5432,
        'dbname': 'postgres',
        'user': 'postgres',
        'password': 'postgres'
    },
    'n2': {
        'host': 'localhost',
        'port': 5433,
        'dbname': 'postgres',
        'user': 'postgres',
        'password': 'postgres'
    }
}

def get_connection(node_name):
    """Create a connection to the specified node."""
    config = NODES[node_name]
    return psycopg2.connect(
        host=config['host'],
        port=config['port'],
        dbname=config['dbname'],
        user=config['user'],
        password=config['password']
    )

def execute_sql(node_name, sql_statement, fetch=False):
    """Execute SQL on a specific node."""
    conn = get_connection(node_name)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            print(f"[{node_name}] Executing: {sql_statement[:80]}...")
            cur.execute(sql_statement)
            if fetch:
                return cur.fetchall()
            print(f"[{node_name}] Success")
    except psycopg2.Error as e:
        print(f"[{node_name}] Error: {e}")
        raise
    finally:
        conn.close()

def execute_on_both(sql_statement):
    """Execute SQL on both nodes."""
    for node in ['n1', 'n2']:
        execute_sql(node, sql_statement)

def get_dsn(node_name):
    """Get DSN string for a node."""
    c = NODES[node_name]
    return f"host={c['host']} port={c['port']} dbname={c['dbname']} user={c['user']} password={c['password']}"

def main():
    print("=" * 60)
    print("Spock Two-Node Cluster Setup")
    print("=" * 60)

    # Step 1: Change DB user password on both nodes
    print("\n[Step 1] Setting postgres user password on both nodes...")
    execute_on_both("ALTER USER postgres WITH PASSWORD 'postgres';")

    # Step 2: Create spock extension on both nodes
    print("\n[Step 2] Creating spock extension on both nodes...")
    execute_on_both("CREATE EXTENSION IF NOT EXISTS spock;")

    # Step 3: Create node on n1
    print("\n[Step 3] Creating spock node on n1...")
    execute_sql('n1', f"""
        SELECT spock.node_create(
            node_name := 'n1',
            dsn := '{get_dsn('n1')}'
        );
    """)

    # Step 4: Create node on n2
    print("\n[Step 4] Creating spock node on n2...")
    execute_sql('n2', f"""
        SELECT spock.node_create(
            node_name := 'n2',
            dsn := '{get_dsn('n2')}'
        );
    """)

    # Step 5: Add all tables to default replication set on n1
    print("\n[Step 5] Adding all public tables to default repset on n1...")
    execute_sql('n1', """
        SELECT spock.repset_add_all_tables('default', ARRAY['public']);
    """)

    # Step 6: Create subscription on n1 to n2
    print("\n[Step 6] Creating subscription on n1 to n2...")
    execute_sql('n1', f"""
        SELECT spock.sub_create(
            subscription_name := 'sub_n2_n1',
            provider_dsn := '{get_dsn('n2')}'
        );
    """)
    print("[n1] Waiting for sync...")
    execute_sql('n1', "SELECT spock.sub_wait_for_sync('sub_n2_n1');")

    # Step 7: Create subscription on n2 to n1
    print("\n[Step 7] Creating subscription on n2 to n1...")
    execute_sql('n2', f"""
        SELECT spock.sub_create(
            subscription_name := 'sub_n1_n2',
            provider_dsn := '{get_dsn('n1')}'
        );
    """)

    # Step 8: Create replication set n1r1 on n1
    print("\n[Step 8] Creating replication set 'n1r1' on n1...")
    execute_sql('n1', """
        SELECT spock.repset_create(
            set_name           := 'n1r1',
            replicate_insert   := true,
            replicate_update   := true,
            replicate_delete   := true,
            replicate_truncate := true
        );
    """)

    # Step 9: Create replication set n2r2 on n2
    print("\n[Step 9] Creating replication set 'n2r2' on n2...")
    execute_sql('n2', """
        SELECT spock.repset_create(
            set_name           := 'n2r2',
            replicate_insert   := true,
            replicate_update   := true,
            replicate_delete   := true,
            replicate_truncate := true
        );
    """)

    # Step 10: Enable DDL replication on both nodes
    print("\n[Step 10] Enabling DDL replication on both nodes...")
    ddl_statements = [
        "ALTER SYSTEM SET spock.enable_ddl_replication = on;",
        "ALTER SYSTEM SET spock.include_ddl_repset = on;",
        "ALTER SYSTEM SET spock.allow_ddl_from_functions = on;",
        "SELECT pg_reload_conf();"
    ]
    for stmt in ddl_statements:
        execute_on_both(stmt)

    print("\n" + "=" * 60)
    print("Spock cluster setup completed successfully!")
    print("=" * 60)
    print("\nVerification commands:")
    print("  Node status:       SELECT * FROM spock.node;")
    print("  Subscription:      SELECT * FROM spock.subscription;")
    print("  Replication sets:  SELECT * FROM spock.replication_set;")
    print("  Sync status:       SELECT * FROM spock.local_sync_status;")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nSetup failed: {e}")
        sys.exit(1)
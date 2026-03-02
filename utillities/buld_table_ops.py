#!/usr/bin/env python3
"""
Benchmark script that performs INSERT, UPDATE, and DELETE operations
on mybench table for 1 minute.
"""

import psycopg2
import random
import string
import time
from datetime import datetime, timedelta


def get_db_config():
    """Prompt user for database connection details."""
    print("=" * 50)
    print("Database Connection Configuration")
    print("=" * 50)

    host = input("Enter host [localhost]: ").strip() or 'localhost'
    port = input("Enter port [5432]: ").strip() or '5432'
    dbname = input("Enter database name [postgres]: ").strip() or 'postgres'
    user = input("Enter username [postgres]: ").strip() or 'postgres'
    password = input("Enter password [postgres]: ").strip() or 'postgres'
    table = input("Enter table name [mybench]: ").strip() or 'mybench'

    return {
        'host': host,
        'port': int(port),
        'dbname': dbname,
        'user': user,
        'password': password,
        'table': table
    }


# Database configuration (will be set by user input)
DB_CONFIG = None

# Workload configuration
DURATION_SECONDS = 60
INSERT_WEIGHT = 50  # 50% inserts
UPDATE_WEIGHT = 30  # 30% updates
DELETE_WEIGHT = 20  # 20% deletes


def get_connection():
    """Create database connection."""
    conn_params = {
        'host': DB_CONFIG['host'],
        'port': DB_CONFIG['port'],
        'dbname': DB_CONFIG['dbname'],
        'user': DB_CONFIG['user'],
        'password': DB_CONFIG['password']
    }
    return psycopg2.connect(**conn_params)


def generate_name(id_value, updated=False):
    """Generate name based on ID."""
    if updated:
        return f"{id_value}_pgedge_updated"
    return f"{id_value}_pgedge"


def setup_table(conn):
    """Create the table if it doesn't exist."""
    table = DB_CONFIG['table']
    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id INT PRIMARY KEY,
                name VARCHAR(100)
            );
        """)
        conn.commit()
        print(f"Table '{table}' is ready.")


def get_max_id(cur):
    """Get the current maximum ID in the table."""
    table = DB_CONFIG['table']
    cur.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table};")
    return cur.fetchone()[0]


def get_random_existing_id(cur):
    """Get a random existing ID from the table."""
    table = DB_CONFIG['table']
    cur.execute(f"SELECT id FROM {table} ORDER BY RANDOM() LIMIT 1;")
    result = cur.fetchone()
    return result[0] if result else None


def do_insert(cur, next_id):
    """Perform an INSERT operation."""
    table = DB_CONFIG['table']
    name = generate_name(next_id)
    cur.execute(
        f"INSERT INTO {table} (id, name) VALUES (%s, %s);",
        (next_id, name)
    )
    return next_id + 1


def do_update(cur):
    """Perform an UPDATE operation."""
    table = DB_CONFIG['table']
    existing_id = get_random_existing_id(cur)
    if existing_id:
        new_name = generate_name(existing_id, updated=True)
        cur.execute(
            f"UPDATE {table} SET name = %s WHERE id = %s;",
            (new_name, existing_id)
        )
        return True
    return False


def do_delete(cur):
    """Perform a DELETE operation."""
    table = DB_CONFIG['table']
    existing_id = get_random_existing_id(cur)
    if existing_id:
        cur.execute(f"DELETE FROM {table} WHERE id = %s;", (existing_id,))
        return True
    return False


def choose_operation():
    """Randomly choose an operation based on weights."""
    r = random.randint(1, 100)
    if r <= INSERT_WEIGHT:
        return 'insert'
    elif r <= INSERT_WEIGHT + UPDATE_WEIGHT:
        return 'update'
    else:
        return 'delete'


def run_workload():
    """Run the benchmark workload for specified duration."""
    conn = get_connection()
    conn.autocommit = True

    # Setup table
    setup_table(conn)

    # Statistics
    stats = {'insert': 0, 'update': 0, 'delete': 0, 'errors': 0}

    # Get starting max ID
    with conn.cursor() as cur:
        next_id = get_max_id(cur) + 1

    print(f"\nStarting workload for {DURATION_SECONDS} seconds...")
    print(f"Operation weights: INSERT={INSERT_WEIGHT}%, UPDATE={UPDATE_WEIGHT}%, DELETE={DELETE_WEIGHT}%")
    print("-" * 50)

    start_time = datetime.now()
    end_time = start_time + timedelta(seconds=DURATION_SECONDS)
    last_report = start_time

    try:
        with conn.cursor() as cur:
            while datetime.now() < end_time:
                operation = choose_operation()

                try:
                    if operation == 'insert':
                        next_id = do_insert(cur, next_id)
                        stats['insert'] += 1
                    elif operation == 'update':
                        if do_update(cur):
                            stats['update'] += 1
                    elif operation == 'delete':
                        if do_delete(cur):
                            stats['delete'] += 1
                except psycopg2.Error as e:
                    stats['errors'] += 1

                # Progress report every 10 seconds
                now = datetime.now()
                if (now - last_report).seconds >= 10:
                    elapsed = (now - start_time).seconds
                    total_ops = stats['insert'] + stats['update'] + stats['delete']
                    print(f"[{elapsed}s] Total ops: {total_ops} | "
                          f"I:{stats['insert']} U:{stats['update']} D:{stats['delete']} E:{stats['errors']}")
                    last_report = now

    except KeyboardInterrupt:
        print("\nWorkload interrupted by user.")
    finally:
        conn.close()

    # Final report
    elapsed = (datetime.now() - start_time).total_seconds()
    total_ops = stats['insert'] + stats['update'] + stats['delete']

    print("\n" + "=" * 50)
    print("WORKLOAD COMPLETE")
    print("=" * 50)
    print(f"Duration:    {elapsed:.2f} seconds")
    print(f"Total ops:   {total_ops}")
    print(f"Ops/second:  {total_ops / elapsed:.2f}")
    print("-" * 50)
    print(f"Inserts:     {stats['insert']}")
    print(f"Updates:     {stats['update']}")
    print(f"Deletes:     {stats['delete']}")
    print(f"Errors:      {stats['errors']}")
    print("=" * 50)


if __name__ == "__main__":
    DB_CONFIG = get_db_config()
    print(f"\nConnecting to {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']} as {DB_CONFIG['user']}")
    print(f"Target table: {DB_CONFIG['table']}")
    run_workload()
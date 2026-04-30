#!/usr/bin/env python3
"""
Spock DDL Load Test Script (Interactive)
- Asks user for inputs interactively
- Executes all 16 DDL operations on multiple nodes in parallel
- Repeats N times
- Validates replication and MD5 checksums across all nodes
"""

import psycopg2
import time
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# ============================================================
# DDL Operations - All 16 categories
# ============================================================
DDL_OPERATIONS = {
    "1_schema": """
        DROP SCHEMA IF EXISTS load_test_{node_id}_{iter} CASCADE;
        CREATE SCHEMA load_test_{node_id}_{iter};
        COMMENT ON SCHEMA load_test_{node_id}_{iter} IS 'Load test schema';
    """,

    "2_table": """
        CREATE TABLE load_test_{node_id}_{iter}.employees (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            email VARCHAR(100) UNIQUE,
            salary DECIMAL(10,2),
            hire_date DATE DEFAULT CURRENT_DATE
        );
        ALTER TABLE load_test_{node_id}_{iter}.employees ADD COLUMN phone VARCHAR(30);
        ALTER TABLE load_test_{node_id}_{iter}.employees ADD CONSTRAINT chk_salary_{node_id}_{iter} CHECK (salary >= 0);
    """,

    "3_index": """
        CREATE INDEX idx_emp_name_{node_id}_{iter} ON load_test_{node_id}_{iter}.employees(name);
        CREATE UNIQUE INDEX idx_emp_email_{node_id}_{iter} ON load_test_{node_id}_{iter}.employees(email);
    """,

    "4_sequence": """
        CREATE SEQUENCE load_test_{node_id}_{iter}.emp_seq START WITH 1000;
        ALTER SEQUENCE load_test_{node_id}_{iter}.emp_seq INCREMENT BY 5;
    """,

    "5_view": """
        CREATE VIEW load_test_{node_id}_{iter}.high_paid AS
        SELECT id, name, salary FROM load_test_{node_id}_{iter}.employees WHERE salary > 50000;
    """,

    "6_function": """
        CREATE OR REPLACE FUNCTION load_test_{node_id}_{iter}.get_count() 
        RETURNS INTEGER AS $func$
        BEGIN
            RETURN (SELECT COUNT(*) FROM load_test_{node_id}_{iter}.employees);
        END;
        $func$ LANGUAGE plpgsql;
    """,

    "7_procedure": """
        CREATE OR REPLACE PROCEDURE load_test_{node_id}_{iter}.update_emp(
            emp_id INTEGER, new_salary DECIMAL
        ) AS $proc$
        BEGIN
            UPDATE load_test_{node_id}_{iter}.employees 
            SET salary = new_salary WHERE id = emp_id;
        END;
        $proc$ LANGUAGE plpgsql;
    """,

    "8_trigger": """
        CREATE OR REPLACE FUNCTION load_test_{node_id}_{iter}.log_changes() 
        RETURNS TRIGGER AS $func$
        BEGIN
            RETURN NEW;
        END;
        $func$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_emp_{node_id}_{iter}
        BEFORE UPDATE ON load_test_{node_id}_{iter}.employees
        FOR EACH ROW EXECUTE FUNCTION load_test_{node_id}_{iter}.log_changes();
    """,

    "9_type": """
        CREATE TYPE load_test_{node_id}_{iter}.address_type AS (
            street VARCHAR(100), city VARCHAR(50), zip VARCHAR(10)
        );
        CREATE TYPE load_test_{node_id}_{iter}.status_enum AS ENUM ('active', 'inactive');
    """,

    "10_domain": """
        CREATE DOMAIN load_test_{node_id}_{iter}.email_domain AS VARCHAR(100)
        CHECK (VALUE ~* '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{{2,}}$');
    """,

    "11_rule": """
        CREATE RULE log_delete_{node_id}_{iter} AS
        ON DELETE TO load_test_{node_id}_{iter}.employees
        DO INSTEAD NOTHING;
    """,

    "12_alter_table": """
        ALTER TABLE load_test_{node_id}_{iter}.employees ADD COLUMN dept_id INTEGER;
        ALTER TABLE load_test_{node_id}_{iter}.employees ALTER COLUMN salary SET DEFAULT 0;
    """,

    "13_grant": """
        GRANT USAGE ON SCHEMA load_test_{node_id}_{iter} TO PUBLIC;
        GRANT SELECT ON ALL TABLES IN SCHEMA load_test_{node_id}_{iter} TO PUBLIC;
    """,

    "14_insert_data": """
        INSERT INTO load_test_{node_id}_{iter}.employees (name, email, salary) VALUES
            ('User1_n{node_id}_i{iter}', 'user1_n{node_id}_i{iter}@test.com', 75000),
            ('User2_n{node_id}_i{iter}', 'user2_n{node_id}_i{iter}@test.com', 85000),
            ('User3_n{node_id}_i{iter}', 'user3_n{node_id}_i{iter}@test.com', 95000);
    """,

    "15_truncate": """
        CREATE TABLE load_test_{node_id}_{iter}.temp_data (id INT, value TEXT);
        INSERT INTO load_test_{node_id}_{iter}.temp_data VALUES (1, 'test');
        TRUNCATE TABLE load_test_{node_id}_{iter}.temp_data;
    """,

    "16_materialized_view": """
        CREATE MATERIALIZED VIEW load_test_{node_id}_{iter}.emp_summary AS
        SELECT COUNT(*) AS total, AVG(salary) AS avg_salary
        FROM load_test_{node_id}_{iter}.employees;
    """,
}


# ============================================================
# Helper Functions
# ============================================================
def log(message, level="INFO"):
    """Print log message with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")


def get_user_input(prompt, default=None, input_type=str):
    """Get user input with optional default value"""
    if default is not None:
        prompt = f"{prompt} [{default}]: "
    else:
        prompt = f"{prompt}: "

    while True:
        value = input(prompt).strip()

        if not value and default is not None:
            return default

        if not value:
            print("This field is required. Please enter a value.")
            continue

        try:
            return input_type(value)
        except ValueError:
            print(f"Invalid input. Expected {input_type.__name__}.")


def get_yes_no(prompt, default="n"):
    """Get yes/no input from user"""
    default_text = "Y/n" if default.lower() == "y" else "y/N"
    while True:
        value = input(f"{prompt} ({default_text}): ").strip().lower()
        if not value:
            value = default.lower()
        if value in ['y', 'yes']:
            return True
        elif value in ['n', 'no']:
            return False
        else:
            print("Please enter 'y' or 'n'")


def collect_node_info(node_num):
    """Collect connection info for a single node"""
    print(f"\n--- Node {node_num} Configuration ---")

    host = get_user_input("Host", default="localhost")
    port = get_user_input("Port", default=5432, input_type=int)
    database = get_user_input("Database", default="postgres")
    user = get_user_input("User", default="postgres")
    password = get_user_input("Password", default="postgres")

    return {
        'host': host,
        'port': port,
        'database': database,
        'user': user,
        'password': password
    }


def get_all_nodes():
    """Get connection info for all nodes interactively"""
    print("\n" + "=" * 60)
    print("NODE CONFIGURATION")
    print("=" * 60)

    num_nodes = get_user_input(
        "How many nodes do you want to test",
        default=2,
        input_type=int
    )

    if num_nodes < 1:
        print("Number of nodes must be at least 1")
        sys.exit(1)

    nodes = []
    for i in range(1, num_nodes + 1):
        nodes.append(collect_node_info(i))

    return nodes


def get_test_parameters():
    """Get test parameters interactively"""
    print("\n" + "=" * 60)
    print("TEST PARAMETERS")
    print("=" * 60)

    iterations = get_user_input(
        "Number of iterations to run",
        default=1,
        input_type=int
    )

    wait_time = get_user_input(
        "Wait time for replication (seconds)",
        default=30,
        input_type=int
    )

    skip_validation = not get_yes_no(
        "Validate replication after test",
        default="y"
    )

    cleanup = get_yes_no(
        "Cleanup test objects after validation",
        default="n"
    )

    return {
        'iterations': iterations,
        'wait_time': wait_time,
        'skip_validation': skip_validation,
        'cleanup': cleanup
    }


def display_summary(nodes, params):
    """Display test summary before running"""
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    print(f"Number of nodes: {len(nodes)}")
    for i, node in enumerate(nodes, 1):
        print(f"  Node {i}: {node['host']}:{node['port']}/{node['database']}")
    print(f"Iterations: {params['iterations']}")
    print(f"Wait time: {params['wait_time']}s")
    print(f"Validate replication: {'No' if params['skip_validation'] else 'Yes'}")
    print(f"Cleanup after test: {'Yes' if params['cleanup'] else 'No'}")
    print("=" * 60)


def get_connection(node):
    """Create a database connection"""
    return psycopg2.connect(
        host=node['host'],
        port=node['port'],
        database=node['database'],
        user=node['user'],
        password=node['password']
    )


def execute_ddl(node, node_id, iteration):
    """Execute all 16 DDL operations on a single node"""
    try:
        conn = get_connection(node)
        conn.autocommit = True
        cur = conn.cursor()

        log(f"Node {node['host']}:{node['port']} - Iteration {iteration}: Starting DDL operations")

        for op_name, op_sql in DDL_OPERATIONS.items():
            sql = op_sql.format(node_id=node_id, iter=iteration)
            try:
                cur.execute(sql)
            except Exception as e:
                log(f"  Node {node['port']} - {op_name}: ✗ ({str(e)[:100]})", level="ERROR")

        cur.close()
        conn.close()

        log(f"Node {node['host']}:{node['port']} - Iteration {iteration}: Completed")
        return True

    except Exception as e:
        log(f"Node {node['host']}:{node['port']} failed: {e}", level="ERROR")
        return False


def run_parallel_load(nodes, iterations):
    """Run DDL operations in parallel on all nodes"""
    log(f"Starting parallel load: {len(nodes)} nodes × {iterations} iterations")

    with ThreadPoolExecutor(max_workers=len(nodes) * 2) as executor:
        futures = []

        for iteration in range(1, iterations + 1):
            for node_id, node in enumerate(nodes, start=1):
                future = executor.submit(execute_ddl, node, node_id, iteration)
                futures.append((future, node, iteration))

        results = {'success': 0, 'failed': 0}
        for future, node, iteration in futures:
            try:
                if future.result():
                    results['success'] += 1
                else:
                    results['failed'] += 1
            except Exception as e:
                log(f"Future failed: {e}", level="ERROR")
                results['failed'] += 1

    log(f"Load test completed: {results['success']} success, {results['failed']} failed")
    return results


def wait_for_replication(seconds=30):
    """Wait for replication to catch up"""
    log(f"Waiting {seconds} seconds for replication to sync...")
    time.sleep(seconds)


def get_object_counts(node):
    """Get counts of all created objects"""
    conn = get_connection(node)
    cur = conn.cursor()

    counts = {}

    queries = {
        'schemas': "SELECT COUNT(*) FROM pg_namespace WHERE nspname LIKE 'load_test_%'",
        'tables': "SELECT COUNT(*) FROM pg_tables WHERE schemaname LIKE 'load_test_%'",
        'views': "SELECT COUNT(*) FROM pg_views WHERE schemaname LIKE 'load_test_%'",
        'mat_views': "SELECT COUNT(*) FROM pg_matviews WHERE schemaname LIKE 'load_test_%'",
        'indexes': "SELECT COUNT(*) FROM pg_indexes WHERE schemaname LIKE 'load_test_%'",
        'sequences': "SELECT COUNT(*) FROM information_schema.sequences WHERE sequence_schema LIKE 'load_test_%'",
        'functions': """
            SELECT COUNT(*) FROM pg_proc p
            JOIN pg_namespace n ON p.pronamespace = n.oid
            WHERE n.nspname LIKE 'load_test_%'
        """,
        'types': """
            SELECT COUNT(*) FROM pg_type t
            JOIN pg_namespace n ON t.typnamespace = n.oid
            WHERE n.nspname LIKE 'load_test_%' AND t.typtype IN ('c', 'e', 'd')
        """,
    }

    for obj_type, query in queries.items():
        cur.execute(query)
        counts[obj_type] = cur.fetchone()[0]

    cur.close()
    conn.close()
    return counts


def get_table_checksums(node):
    """Get MD5 checksum of all load_test tables"""
    conn = get_connection(node)
    cur = conn.cursor()

    cur.execute("""
        SELECT schemaname, tablename 
        FROM pg_tables 
        WHERE schemaname LIKE 'load_test_%'
        ORDER BY schemaname, tablename
    """)

    checksums = {}
    for schema, table in cur.fetchall():
        try:
            cur.execute(f"""
                SELECT MD5(STRING_AGG(t::text, ',' ORDER BY t::text))
                FROM {schema}.{table} t
            """)
            result = cur.fetchone()
            checksums[f"{schema}.{table}"] = result[0] if result and result[0] else "EMPTY"
        except Exception as e:
            checksums[f"{schema}.{table}"] = f"ERROR: {str(e)[:50]}"

    cur.close()
    conn.close()
    return checksums


def validate_replication(nodes):
    """Validate that all nodes have same objects and data"""
    print("\n" + "=" * 60)
    log("VALIDATING REPLICATION ACROSS ALL NODES")
    print("=" * 60)

    # Step 1: Compare object counts
    log("\n--- Step 1: Comparing object counts ---")
    all_counts = {}
    for i, node in enumerate(nodes, 1):
        counts = get_object_counts(node)
        all_counts[f"Node{i} ({node['host']}:{node['port']})"] = counts
        log(f"Node{i} ({node['host']}:{node['port']}): {counts}")

    counts_match = True
    first_node_counts = list(all_counts.values())[0]
    for node_name, counts in all_counts.items():
        if counts != first_node_counts:
            counts_match = False
            log(f"MISMATCH on {node_name}", level="ERROR")

    if counts_match:
        log("✓ Object counts match on all nodes")
    else:
        log("✗ Object counts do NOT match on all nodes", level="ERROR")

    # Step 2: Compare table checksums
    log("\n--- Step 2: Comparing table checksums ---")
    all_checksums = {}
    for i, node in enumerate(nodes, 1):
        checksums = get_table_checksums(node)
        all_checksums[f"Node{i}"] = checksums
        log(f"Node{i}: Got checksums for {len(checksums)} tables")

    checksums_match = True
    first_node_checksums = list(all_checksums.values())[0]

    for table_name in first_node_checksums.keys():
        first_checksum = first_node_checksums[table_name]
        for node_name, node_checksums in all_checksums.items():
            if table_name not in node_checksums:
                log(f"  Table {table_name} missing on {node_name}", level="ERROR")
                checksums_match = False
            elif node_checksums[table_name] != first_checksum:
                log(f"  Table {table_name}: MISMATCH on {node_name}", level="ERROR")
                log(f"    Expected: {first_checksum}", level="ERROR")
                log(f"    Got:      {node_checksums[table_name]}", level="ERROR")
                checksums_match = False

    if checksums_match:
        log("✓ All table checksums match across all nodes")
    else:
        log("✗ Table checksums do NOT match", level="ERROR")

    # Final summary
    print("\n" + "=" * 60)
    log("VALIDATION SUMMARY")
    print("=" * 60)
    log(f"Object counts match: {'YES ✓' if counts_match else 'NO ✗'}")
    log(f"Table checksums match: {'YES ✓' if checksums_match else 'NO ✗'}")
    log(f"Overall result: {'PASS ✓' if counts_match and checksums_match else 'FAIL ✗'}")

    return counts_match and checksums_match


def cleanup_test_objects(nodes):
    """Cleanup all test objects from all nodes"""
    print("\n" + "=" * 60)
    log("CLEANUP")
    print("=" * 60)

    for i, node in enumerate(nodes, 1):
        try:
            conn = get_connection(node)
            conn.autocommit = True
            cur = conn.cursor()

            cur.execute("""
                SELECT nspname FROM pg_namespace 
                WHERE nspname LIKE 'load_test_%'
            """)
            schemas = [row[0] for row in cur.fetchall()]

            for schema in schemas:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                log(f"Node{i}: Dropped schema {schema}")

            cur.close()
            conn.close()
        except Exception as e:
            log(f"Node{i} cleanup failed: {e}", level="ERROR")


def show_menu():
    """Show main menu"""
    print("\n" + "=" * 60)
    print("SPOCK DDL LOAD TEST - MAIN MENU")
    print("=" * 60)
    print("1. Run Load Test")
    print("2. Cleanup Test Objects Only")
    print("3. Validate Replication Only")
    print("4. Exit")
    print("=" * 60)

    while True:
        choice = input("Enter your choice (1-4): ").strip()
        if choice in ['1', '2', '3', '4']:
            return choice
        print("Invalid choice. Please enter 1, 2, 3, or 4.")


# ============================================================
# Main Function
# ============================================================
def main():
    print("\n" + "=" * 60)
    print(" SPOCK DDL LOAD TEST - INTERACTIVE MODE")
    print("=" * 60)

    while True:
        choice = show_menu()

        if choice == '4':
            print("Exiting...")
            sys.exit(0)

        # Get nodes for all options
        nodes = get_all_nodes()

        if choice == '1':
            # Run load test
            params = get_test_parameters()
            display_summary(nodes, params)

            if not get_yes_no("\nProceed with the test", default="y"):
                print("Test cancelled.")
                continue

            start_time = time.time()

            results = run_parallel_load(nodes, params['iterations'])

            if results['failed'] > 0:
                log(f"WARNING: {results['failed']} operations failed", level="WARNING")

            if not params['skip_validation']:
                wait_for_replication(params['wait_time'])
                success = validate_replication(nodes)
            else:
                success = True

            elapsed = time.time() - start_time
            log(f"\nTotal time: {elapsed:.2f} seconds")

            if params['cleanup']:
                if get_yes_no("\nProceed with cleanup", default="y"):
                    cleanup_test_objects(nodes)

        elif choice == '2':
            # Cleanup only
            if get_yes_no("Are you sure you want to cleanup all test objects", default="n"):
                cleanup_test_objects(nodes)

        elif choice == '3':
            # Validate only
            validate_replication(nodes)

        # Ask if user wants to continue
        if not get_yes_no("\nDo you want to perform another operation", default="n"):
            print("Exiting...")
            break


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user. Exiting...")
        sys.exit(1)
    except Exception as e:
        log(f"Unexpected error: {e}", level="ERROR")
        sys.exit(1)
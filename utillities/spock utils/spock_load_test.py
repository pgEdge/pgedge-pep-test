"""
Spock Cluster Load Test — Northwind Database + Resource Manager
================================================================
Interactive load tester for Spock replication clusters using the
standard Northwind PostgreSQL database.

Features:
  • INSERT / UPDATE / DELETE against real Northwind tables
  • Configurable per-node: operation, duration, transaction interval
  • Cluster health report: row counts + checksums across all nodes
  • Resource Manager overview:
    - Runs `SELECT * FROM spock.progress` on every node
    - Sends combined output to Claude for AI-powered analysis
    - Generates a detailed Resource Manager health report

Usage:
  pip install psycopg2-binary requests
  python3 spock_northwind_load_test.py

  Set environment variable for Claude analysis:
    export ANTHROPIC_API_KEY=sk-ant-...
"""

import threading
import time
import random
import string
import json
import sys
import signal
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from collections import defaultdict, OrderedDict

# ─────────────────────────────────────────────
#  DB DRIVER
# ─────────────────────────────────────────────

try:
    import psycopg2
    import psycopg2.extras
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False

try:
    import psycopg
    HAS_PSYCOPG3 = True
except ImportError:
    HAS_PSYCOPG3 = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


# ─────────────────────────────────────────────
#  GLOBALS
# ─────────────────────────────────────────────

STOP_FLAG = threading.Event()

def signal_handler(sig, frame):
    print("\n\n  ⚠ Interrupt received — stopping gracefully...")
    STOP_FLAG.set()

signal.signal(signal.SIGINT, signal_handler)

NORTHWIND_TABLES = [
    "categories", "customers", "employees", "shippers",
    "suppliers", "products", "orders", "order_details",
    "region", "territories", "employee_territories",
    "customer_customer_demo", "customer_demographics", "us_states",
]

VALID_OPERATIONS = ["insert", "update", "delete", "idle"]


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def random_string(length=10):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def random_phone():
    return f"({random.randint(100,999)}) {random.randint(100,999)}-{random.randint(1000,9999)}"

def random_company():
    prefixes = ["Acme", "Global", "Prime", "Nexus", "Vertex", "Apex", "Summit", "Nova", "Atlas", "Echo"]
    suffixes = ["Trading", "Foods", "Imports", "Exports", "Supply", "Corp", "Ltd", "Industries", "Group", "Co"]
    return f"{random.choice(prefixes)} {random.choice(suffixes)} {random_string(3)}"

def random_contact():
    first = random.choice(["James", "Maria", "Robert", "Linda", "Carlos", "Yuki", "Hans", "Fatima", "Chen", "Anna"])
    last = random.choice(["Smith", "Garcia", "Mueller", "Tanaka", "Ali", "Chen", "Brown", "Silva", "Kim", "Ivanov"])
    return f"{first} {last}"

def random_city():
    return random.choice(["London", "Berlin", "Paris", "Tokyo", "Mumbai", "Cairo", "Toronto", "Sydney", "Lagos", "Lima"])

def random_country():
    return random.choice(["UK", "Germany", "France", "Japan", "India", "Egypt", "Canada", "Australia", "Nigeria", "Peru"])

TITLES = ["Sales Representative", "Owner", "Marketing Manager", "Accounting Manager", "Sales Agent", "Export Administrator"]


def get_connection(node):
    params = {
        "host": node["host"],
        "port": node["port"],
        "dbname": node["database"],
        "user": node["user"],
        "password": node["password"],
    }
    if HAS_PSYCOPG2:
        return psycopg2.connect(**params)
    elif HAS_PSYCOPG3:
        conninfo = (
            f"host={params['host']} port={params['port']} "
            f"dbname={params['dbname']} user={params['user']} password={params['password']}"
        )
        return psycopg.connect(conninfo)
    else:
        raise ImportError("No PostgreSQL driver found.")


def prompt(msg, default=None, cast=str, validate=None):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"  {msg}{suffix}: ").strip()
        if not raw and default is not None:
            raw = str(default)
        if not raw:
            print("    → Required.")
            continue
        try:
            val = cast(raw)
        except (ValueError, TypeError):
            print(f"    → Invalid. Expected {cast.__name__}.")
            continue
        if validate and not validate(val):
            continue
        return val


def prompt_choice(msg, choices, default=None):
    choices_str = " / ".join(choices)
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"  {msg} ({choices_str}){suffix}: ").strip().lower()
        if not raw and default:
            return default
        if raw in [c.lower() for c in choices]:
            return raw
        print(f"    → Choose one of: {choices_str}")


# ─────────────────────────────────────────────
#  STATS
# ─────────────────────────────────────────────

class Stats:
    def __init__(self, node_name, operation):
        self.node_name = node_name
        self.operation = operation
        self.lock = threading.Lock()
        self.success = 0
        self.failure = 0
        self.latencies = []
        self.errors = defaultdict(int)
        self.start_time = None
        self.end_time = None
        self.table_ops = defaultdict(int)

    def record(self, latency_ms, table=None, error=None):
        with self.lock:
            if error:
                self.failure += 1
                self.errors[str(error)[:120]] += 1
            else:
                self.success += 1
                self.latencies.append(latency_ms)
            if table:
                self.table_ops[table] += 1

    def pct(self, sorted_list, p):
        if not sorted_list:
            return 0
        return sorted_list[min(int(len(sorted_list) * p), len(sorted_list) - 1)]

    def summary(self):
        total = self.success + self.failure
        sl = sorted(self.latencies) if self.latencies else []
        elapsed = (self.end_time - self.start_time) if (self.end_time and self.start_time) else 1
        return {
            "node": self.node_name,
            "operation": self.operation,
            "total": total,
            "success": self.success,
            "failure": self.failure,
            "avg_ms": round(sum(sl) / len(sl), 2) if sl else 0,
            "min_ms": round(sl[0], 2) if sl else 0,
            "max_ms": round(sl[-1], 2) if sl else 0,
            "p50_ms": round(self.pct(sl, 0.50), 2),
            "p95_ms": round(self.pct(sl, 0.95), 2),
            "p99_ms": round(self.pct(sl, 0.99), 2),
            "throughput_ops": round(self.success / elapsed, 1) if elapsed > 0 else 0,
            "elapsed_s": round(elapsed, 2),
            "table_ops": dict(self.table_ops),
            "top_errors": dict(list(self.errors.items())[:5]),
        }


# ─────────────────────────────────────────────
#  NORTHWIND OPERATIONS
# ─────────────────────────────────────────────

# ── INSERTS ──

def insert_customer(conn):
    cur = conn.cursor()
    cid = random_string(5).upper()
    cur.execute("""
        INSERT INTO customers (customer_id, company_name, contact_name, contact_title, address, city, region, postal_code, country, phone)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        cid, random_company(), random_contact(), random.choice(TITLES),
        f"{random.randint(1,999)} {random_string(8)} St",
        random_city(), None, f"{random.randint(10000,99999)}", random_country(), random_phone()
    ))
    conn.commit()
    cur.close()
    return "customers"

def insert_order(conn):
    cur = conn.cursor()
    cur.execute("SELECT customer_id FROM customers ORDER BY RANDOM() LIMIT 1;")
    cust = cur.fetchone()
    if not cust:
        cur.close()
        return "orders"
    cur.execute("SELECT employee_id FROM employees ORDER BY RANDOM() LIMIT 1;")
    emp = cur.fetchone()
    emp_id = emp[0] if emp else 1
    cur.execute("SELECT shipper_id FROM shippers ORDER BY RANDOM() LIMIT 1;")
    ship = cur.fetchone()
    ship_id = ship[0] if ship else 1
    cur.execute("""
        INSERT INTO orders (customer_id, employee_id, order_date, required_date, shipped_date,
                            ship_via, freight, ship_name, ship_address, ship_city, ship_country)
        VALUES (%s, %s, NOW(), NOW() + INTERVAL '14 days', NULL,
                %s, %s, %s, %s, %s, %s)
        RETURNING order_id
    """, (
        cust[0], emp_id, ship_id, round(random.uniform(5, 500), 2),
        random_company(), f"{random.randint(1,999)} {random_string(6)} Ave",
        random_city(), random_country()
    ))
    order_id = cur.fetchone()[0]
    cur.execute("SELECT product_id, unit_price FROM products ORDER BY RANDOM() LIMIT %s;", (random.randint(1, 3),))
    products = cur.fetchall()
    for prod_id, unit_price in products:
        cur.execute("""
            INSERT INTO order_details (order_id, product_id, unit_price, quantity, discount)
            VALUES (%s, %s, %s, %s, %s)
        """, (order_id, prod_id, float(unit_price), random.randint(1, 50), random.choice([0, 0, 0.05, 0.1, 0.15, 0.2])))
    conn.commit()
    cur.close()
    return "orders"

def insert_product(conn):
    cur = conn.cursor()
    cur.execute("SELECT category_id FROM categories ORDER BY RANDOM() LIMIT 1;")
    cat = cur.fetchone()
    cat_id = cat[0] if cat else 1
    cur.execute("SELECT supplier_id FROM suppliers ORDER BY RANDOM() LIMIT 1;")
    sup = cur.fetchone()
    sup_id = sup[0] if sup else 1
    cur.execute("""
        INSERT INTO products (product_name, supplier_id, category_id, quantity_per_unit,
                              unit_price, units_in_stock, units_on_order, reorder_level, discontinued)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        f"{random.choice(['Organic','Fresh','Premium','Classic','Artisan'])} {random_string(6)}",
        sup_id, cat_id, f"{random.randint(1,48)} boxes",
        round(random.uniform(5, 200), 2), random.randint(0, 200),
        random.randint(0, 50), random.randint(0, 30), random.choice([0, 0, 0, 1])
    ))
    conn.commit()
    cur.close()
    return "products"

INSERT_FUNCS = [insert_customer, insert_order, insert_product]

# ── UPDATES ──

def update_customer(conn):
    cur = conn.cursor()
    cur.execute("SELECT customer_id FROM customers ORDER BY RANDOM() LIMIT 1;")
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE customers SET contact_name=%s, contact_title=%s, phone=%s WHERE customer_id=%s",
                     (random_contact(), random.choice(TITLES), random_phone(), row[0]))
        conn.commit()
    cur.close()
    return "customers"

def update_order(conn):
    cur = conn.cursor()
    cur.execute("SELECT order_id FROM orders WHERE shipped_date IS NULL ORDER BY RANDOM() LIMIT 1;")
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE orders SET shipped_date=NOW(), freight=%s WHERE order_id=%s",
                     (round(random.uniform(5, 300), 2), row[0]))
        conn.commit()
    cur.close()
    return "orders"

def update_product(conn):
    cur = conn.cursor()
    cur.execute("SELECT product_id FROM products WHERE discontinued=0 ORDER BY RANDOM() LIMIT 1;")
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE products SET unit_price=%s, units_in_stock=%s, units_on_order=%s WHERE product_id=%s",
                     (round(random.uniform(5, 200), 2), random.randint(0, 200), random.randint(0, 50), row[0]))
        conn.commit()
    cur.close()
    return "products"

UPDATE_FUNCS = [update_customer, update_order, update_product]

# ── DELETES ──

def delete_order_detail(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT od.order_id, od.product_id FROM order_details od
        JOIN orders o ON o.order_id = od.order_id
        WHERE o.shipped_date IS NOT NULL ORDER BY RANDOM() LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        cur.execute("DELETE FROM order_details WHERE order_id=%s AND product_id=%s", (row[0], row[1]))
        conn.commit()
    cur.close()
    return "order_details"

def delete_order(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT o.order_id FROM orders o
        LEFT JOIN order_details od ON o.order_id = od.order_id
        WHERE od.order_id IS NULL ORDER BY RANDOM() LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        cur.execute("DELETE FROM orders WHERE order_id=%s", (row[0],))
        conn.commit()
    cur.close()
    return "orders"

def delete_customer(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT c.customer_id FROM customers c
        LEFT JOIN orders o ON c.customer_id = o.customer_id
        WHERE o.order_id IS NULL ORDER BY RANDOM() LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        cur.execute("DELETE FROM customers WHERE customer_id=%s", (row[0],))
        conn.commit()
    cur.close()
    return "customers"

DELETE_FUNCS = [delete_order_detail, delete_order, delete_customer]

OP_MAP = {
    "insert": INSERT_FUNCS,
    "update": UPDATE_FUNCS,
    "delete": DELETE_FUNCS,
}


# ─────────────────────────────────────────────
#  NODE RUNNER
# ─────────────────────────────────────────────

def run_node(node_name, node, op_name, duration_sec, interval_us):
    if op_name == "idle":
        return None
    funcs = OP_MAP[op_name]
    stats = Stats(node_name, op_name)
    interval_sec = interval_us / 1_000_000.0
    print(f"  [{node_name}] {op_name.upper()} → {node['host']}:{node['port']} for {duration_sec}s  (interval: {interval_us} µs)")
    stats.start_time = time.time()
    deadline = stats.start_time + duration_sec
    ops_done = 0
    while time.time() < deadline and not STOP_FLAG.is_set():
        conn = None
        table = None
        try:
            conn = get_connection(node)
            func = random.choice(funcs)
            t0 = time.perf_counter()
            table = func(conn)
            latency = (time.perf_counter() - t0) * 1000
            stats.record(latency, table=table)
        except Exception as e:
            stats.record(0, table=table, error=e)
        finally:
            if conn:
                try: conn.close()
                except: pass
        ops_done += 1
        if ops_done % 25 == 0:
            remaining = max(0, deadline - time.time())
            print(f"  [{node_name}] {ops_done} ops  ✓{stats.success} ✗{stats.failure}  {remaining:.0f}s left      ", end="\r")
        if interval_sec > 0 and not STOP_FLAG.is_set():
            time.sleep(interval_sec)
    stats.end_time = time.time()
    total = stats.success + stats.failure
    elapsed = stats.end_time - stats.start_time
    print(f"  [{node_name}] Done — {total} ops in {elapsed:.1f}s  ✓{stats.success} ✗{stats.failure}                ")
    return stats


# ─────────────────────────────────────────────
#  LOAD TEST REPORT
# ─────────────────────────────────────────────

def print_load_report(all_stats):
    w = 104
    print(f"\n{'═' * w}")
    print(f"  LOAD TEST RESULTS")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═' * w}")
    header = (
        f"  {'Node':<8} {'Op':<10} {'Total':>7} {'OK':>7} {'Fail':>7} "
        f"{'Avg':>9} {'P50':>9} {'P95':>9} {'P99':>9} {'Ops/s':>8} {'Time':>7}"
    )
    print(f"\n{header}")
    print(f"  {'─' * (w - 4)}")
    total_ops = total_ok = total_fail = 0
    for s in all_stats:
        sm = s.summary()
        total_ops += sm["total"]; total_ok += sm["success"]; total_fail += sm["failure"]
        print(
            f"  {sm['node']:<8} {sm['operation']:<10} {sm['total']:>7} {sm['success']:>7} {sm['failure']:>7} "
            f"{sm['avg_ms']:>7.1f}ms {sm['p50_ms']:>7.1f}ms {sm['p95_ms']:>7.1f}ms {sm['p99_ms']:>7.1f}ms "
            f"{sm['throughput_ops']:>7.1f} {sm['elapsed_s']:>6.1f}s"
        )
        if sm["table_ops"]:
            tables_str = ", ".join(f"{t}:{c}" for t, c in sorted(sm["table_ops"].items(), key=lambda x: -x[1]))
            print(f"           tables → {tables_str}")
        if sm["top_errors"]:
            for err, cnt in sm["top_errors"].items():
                print(f"           ⚠ {cnt}x — {err}")
    print(f"  {'─' * (w - 4)}")
    print(f"  {'TOTAL':<8} {'':10} {total_ops:>7} {total_ok:>7} {total_fail:>7}")
    fail_pct = (total_fail / total_ops * 100) if total_ops > 0 else 0
    print(f"\n  ✓ Success : {total_ok}/{total_ops} ({100 - fail_pct:.2f}%)")
    print(f"  ✗ Failure : {total_fail}/{total_ops} ({fail_pct:.2f}%)")
    print(f"{'═' * w}")
    return {"totals": {"ops": total_ops, "success": total_ok, "failure": total_fail}, "per_node": [s.summary() for s in all_stats]}


# ─────────────────────────────────────────────
#  CLUSTER HEALTH & CONSISTENCY CHECK
# ─────────────────────────────────────────────

def get_pk_column(table):
    pk_map = {
        "categories": "category_id", "customers": "customer_id", "employees": "employee_id",
        "suppliers": "supplier_id", "shippers": "shipper_id", "products": "product_id",
        "orders": "order_id", "order_details": "order_id", "region": "region_id",
        "territories": "territory_id", "us_states": "state_id",
        "employee_territories": "employee_id", "customer_customer_demo": "customer_id",
        "customer_demographics": "customer_type_id",
    }
    return pk_map.get(table, "1")

def check_cluster_health(nodes):
    w = 90
    print(f"\n{'═' * w}")
    print(f"  CLUSTER HEALTH — DATA CONSISTENCY REPORT")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═' * w}\n")
    node_names = list(nodes.keys())
    tables_to_check = ["categories", "customers", "employees", "suppliers", "shippers", "products", "orders", "order_details"]
    node_data = {}
    print("  Collecting data from each node...\n")
    for nname in node_names:
        node = nodes[nname]
        node_data[nname] = {}
        try:
            conn = get_connection(node)
            conn.autocommit = True
            cur = conn.cursor()
            for tbl in tables_to_check:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {tbl};")
                    count = cur.fetchone()[0]
                    pk_col = get_pk_column(tbl)
                    cur.execute(f"""
                        SELECT MD5(STRING_AGG(sub.row_hash, ',' ORDER BY sub.row_hash))
                        FROM (SELECT MD5(CAST({tbl}.* AS TEXT)) AS row_hash FROM {tbl} ORDER BY {pk_col}) sub;
                    """)
                    cksum_row = cur.fetchone()
                    checksum = cksum_row[0] if cksum_row and cksum_row[0] else "EMPTY"
                    node_data[nname][tbl] = {"count": count, "checksum": checksum}
                except Exception as e:
                    node_data[nname][tbl] = {"count": -1, "checksum": "ERROR", "error": str(e)[:80]}
            cur.close(); conn.close()
            print(f"  ✓ {nname} ({node['host']}:{node['port']}) — collected")
        except Exception as e:
            print(f"  ✗ {nname} ({node['host']}:{node['port']}) — failed: {e}")
            for tbl in tables_to_check:
                node_data[nname][tbl] = {"count": -1, "checksum": "UNREACHABLE"}

    col_w = 10
    count_issues = checksum_issues = 0

    print(f"\n  {'─' * (w - 4)}")
    print(f"  ROW COUNTS")
    print(f"  {'─' * (w - 4)}")
    header = f"  {'Table':<20}"
    for nn in node_names: header += f" {nn:>{col_w}}"
    header += f"  {'Status':>10}"
    print(header)
    print(f"  {'─' * (w - 4)}")
    for tbl in tables_to_check:
        row = f"  {tbl:<20}"
        counts = []
        for nn in node_names:
            c = node_data[nn][tbl]["count"]; counts.append(c)
            row += f" {(str(c) if c >= 0 else 'ERR'):>{col_w}}"
        valid = [c for c in counts if c >= 0]
        if len(set(valid)) <= 1: row += f"  {'✓ OK':>10}"
        else: row += f"  {'⚠ MISMATCH':>10}"; count_issues += 1
        print(row)

    print(f"\n  {'─' * (w - 4)}")
    print(f"  CHECKSUMS (MD5)")
    print(f"  {'─' * (w - 4)}")
    header2 = f"  {'Table':<20}"
    for nn in node_names: header2 += f" {nn:>{col_w}}"
    header2 += f"  {'Status':>10}"
    print(header2)
    print(f"  {'─' * (w - 4)}")
    for tbl in tables_to_check:
        row = f"  {tbl:<20}"
        checksums = []
        for nn in node_names:
            ck = node_data[nn][tbl]["checksum"]; checksums.append(ck)
            row += f" {ck[:8]:>{col_w}}"
        valid_ck = [c for c in checksums if c not in ("ERROR", "UNREACHABLE")]
        if len(set(valid_ck)) <= 1: row += f"  {'✓ OK':>10}"
        else: row += f"  {'⚠ DIVERGE':>10}"; checksum_issues += 1
        print(row)

    print(f"\n  {'─' * (w - 4)}")
    total_issues = count_issues + checksum_issues
    if total_issues == 0:
        print(f"\n  ✅ ALL CONSISTENT — {len(tables_to_check)} tables × {len(node_names)} nodes")
    else:
        print(f"\n  ⚠  ISSUES: {count_issues} count mismatches, {checksum_issues} checksum divergences")
        print(f"     Wait for replication to settle and re-check.")
    print(f"{'═' * w}\n")
    return {
        "tables_checked": len(tables_to_check), "nodes_checked": len(node_names),
        "count_mismatches": count_issues, "checksum_divergences": checksum_issues,
        "all_consistent": total_issues == 0, "details": {
            tbl: {nn: node_data[nn][tbl] for nn in node_names} for tbl in tables_to_check
        },
    }


# ─────────────────────────────────────────────
#  RESOURCE MANAGER — spock.progress
# ─────────────────────────────────────────────

def fetch_spock_progress(nodes):
    """
    Run `SELECT * FROM spock.progress` on every node.
    Returns dict: { node_name: { columns: [...], rows: [...], error: str|None } }
    """
    results = OrderedDict()

    print(f"\n  Querying spock.progress on all nodes...\n")

    for nname, node in nodes.items():
        try:
            conn = get_connection(node)
            conn.autocommit = True
            cur = conn.cursor()

            # Get column names
            cur.execute("SELECT * FROM spock.progress LIMIT 0;")
            col_names = [desc[0] for desc in cur.description]

            # Get all rows
            cur.execute("SELECT * FROM spock.progress;")
            rows = cur.fetchall()

            # Convert to serializable format
            serialized_rows = []
            for row in rows:
                row_dict = {}
                for i, col in enumerate(col_names):
                    val = row[i]
                    # Handle non-serializable types
                    if isinstance(val, datetime):
                        val = val.isoformat()
                    elif isinstance(val, bytes):
                        val = val.hex()
                    elif hasattr(val, '__str__') and not isinstance(val, (str, int, float, bool, type(None))):
                        val = str(val)
                    row_dict[col] = val
                serialized_rows.append(row_dict)

            results[nname] = {
                "columns": col_names,
                "rows": serialized_rows,
                "row_count": len(rows),
                "error": None,
                "host": f"{node['host']}:{node['port']}",
            }

            cur.close()
            conn.close()
            print(f"  ✓ {nname} ({node['host']}:{node['port']}) — {len(rows)} rows")

        except Exception as e:
            results[nname] = {
                "columns": [],
                "rows": [],
                "row_count": 0,
                "error": str(e),
                "host": f"{node['host']}:{node['port']}",
            }
            print(f"  ✗ {nname} ({node['host']}:{node['port']}) — {e}")

    return results


def print_spock_progress_raw(progress_data):
    """Print raw spock.progress output per node."""
    w = 100
    print(f"\n{'═' * w}")
    print(f"  RESOURCE MANAGER — spock.progress RAW OUTPUT")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═' * w}")

    for nname, data in progress_data.items():
        print(f"\n  ┌── {nname} ({data['host']}) ─────────────────────────")

        if data["error"]:
            print(f"  │  ERROR: {data['error']}")
            print(f"  └──────────────────────────────────────────────")
            continue

        if not data["rows"]:
            print(f"  │  (no rows returned)")
            print(f"  └──────────────────────────────────────────────")
            continue

        cols = data["columns"]
        # Print each row as key-value pairs for readability
        for idx, row in enumerate(data["rows"]):
            print(f"  │")
            print(f"  │  Row {idx + 1}:")
            for col in cols:
                val = row.get(col, "")
                print(f"  │    {col:<30} : {val}")

        print(f"  │")
        print(f"  │  Total rows: {data['row_count']}")
        print(f"  └──────────────────────────────────────────────")

    print(f"\n{'═' * w}")


def analyze_with_claude(progress_data, api_key):
    """
    Send spock.progress output from all nodes to Claude API
    for AI-powered Resource Manager analysis.
    """
    if not HAS_REQUESTS:
        print("  ⚠ 'requests' library not installed. Run: pip install requests")
        return None

    print(f"\n  Sending spock.progress data to Claude for analysis...")

    # Build the prompt with all node data
    node_outputs = []
    for nname, data in progress_data.items():
        if data["error"]:
            node_outputs.append(f"NODE {nname} ({data['host']}): ERROR — {data['error']}")
        elif not data["rows"]:
            node_outputs.append(f"NODE {nname} ({data['host']}): No rows returned (empty spock.progress)")
        else:
            rows_text = json.dumps(data["rows"], indent=2, default=str)
            node_outputs.append(f"NODE {nname} ({data['host']}): {data['row_count']} rows\nColumns: {', '.join(data['columns'])}\nData:\n{rows_text}")

    combined_output = "\n\n---\n\n".join(node_outputs)

    user_prompt = f"""I ran `SELECT * FROM spock.progress;` on all nodes of my Spock PostgreSQL replication cluster.
Here is the output from each node:

{combined_output}

Please analyze this as a "Resource Manager Overview" and provide:

1. **Node Status Summary**: For each node, is the replication progressing normally? What is each node's role (provider/subscriber)?

2. **Replication Health**: 
   - Are all subscriptions active and progressing?
   - Are there any stalled or lagging subscriptions?
   - Check if commit_lsn, flush_lsn, and replay_lsn values are advancing or stuck.

3. **Cross-Node Consistency**:
   - Compare the progress entries across all nodes. Are they consistent?
   - Do all nodes show matching subscription states?
   - Flag any node that is missing subscriptions or shows unexpected entries.

4. **Issues & Warnings**:
   - Flag any anomalies, errors, or signs of replication problems.
   - Warn about any nodes that returned errors or no data.

5. **Overall Verdict**: 
   - Is the Spock Resource Manager healthy across all nodes?
   - Give a clear PASS / WARNING / FAIL status.

Format the response as a structured report with clear sections and a final verdict."""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=120,
        )

        if response.status_code != 200:
            print(f"  ✗ Claude API error: {response.status_code} — {response.text[:200]}")
            return None

        result = response.json()
        analysis = ""
        for block in result.get("content", []):
            if block.get("type") == "text":
                analysis += block["text"]

        return analysis

    except requests.exceptions.Timeout:
        print("  ✗ Claude API request timed out.")
        return None
    except Exception as e:
        print(f"  ✗ Claude API call failed: {e}")
        return None


def run_resource_manager_check(nodes):
    """Full Resource Manager flow: fetch → print raw → send to Claude → print analysis."""
    w = 100

    # Fetch spock.progress from all nodes
    progress_data = fetch_spock_progress(nodes)

    # Print raw output
    print_spock_progress_raw(progress_data)

    # Check for API key
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    if not api_key:
        print(f"\n  ────────────────────────────────────────────────")
        print(f"  To get AI-powered analysis of Resource Manager output,")
        print(f"  set your Anthropic API key:\n")
        print(f"    export ANTHROPIC_API_KEY=sk-ant-...\n")
        api_key = prompt("  Enter your Anthropic API key (or 'skip' to skip)", default="skip", cast=str)
        if api_key.lower() == "skip" or not api_key.startswith("sk-"):
            print(f"\n  Skipping Claude analysis. Raw output shown above.")
            return do_local_analysis(progress_data)

    if not HAS_REQUESTS:
        print("  ⚠ Cannot call Claude API — 'requests' library not installed.")
        print("    Run: pip install requests\n")
        return do_local_analysis(progress_data)

    # Send to Claude for analysis
    analysis = analyze_with_claude(progress_data, api_key)

    if analysis:
        print(f"\n{'═' * w}")
        print(f"  RESOURCE MANAGER OVERVIEW — AI ANALYSIS")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'═' * w}\n")
        print(analysis)
        print(f"\n{'═' * w}\n")
        return {"raw_data": {k: v for k, v in progress_data.items()}, "ai_analysis": analysis}
    else:
        print(f"\n  Claude analysis unavailable. Falling back to local checks.\n")
        return do_local_analysis(progress_data)


def do_local_analysis(progress_data):
    """Basic local analysis when Claude API is not available."""
    w = 90
    print(f"\n{'═' * w}")
    print(f"  RESOURCE MANAGER OVERVIEW — LOCAL ANALYSIS")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═' * w}\n")

    node_names = list(progress_data.keys())
    all_ok = True

    # Check 1: Connection status
    print(f"  1. NODE CONNECTIVITY")
    print(f"  {'─' * 50}")
    for nname, data in progress_data.items():
        if data["error"]:
            print(f"     ✗ {nname} ({data['host']}) — ERROR: {data['error']}")
            all_ok = False
        else:
            print(f"     ✓ {nname} ({data['host']}) — reachable, {data['row_count']} progress entries")

    # Check 2: Row count consistency
    print(f"\n  2. PROGRESS ENTRY COUNTS")
    print(f"  {'─' * 50}")
    counts = {nn: d["row_count"] for nn, d in progress_data.items() if not d["error"]}
    unique_counts = set(counts.values())
    for nn, cnt in counts.items():
        print(f"     {nn}: {cnt} entries")
    if len(unique_counts) > 1:
        print(f"\n     ⚠ Different entry counts across nodes — possible misconfiguration")
        all_ok = False
    elif len(unique_counts) == 1:
        print(f"\n     ✓ All reachable nodes have {list(unique_counts)[0]} progress entries")

    # Check 3: Compare column structures
    print(f"\n  3. SCHEMA CONSISTENCY")
    print(f"  {'─' * 50}")
    col_sets = {nn: tuple(d["columns"]) for nn, d in progress_data.items() if not d["error"] and d["columns"]}
    unique_schemas = set(col_sets.values())
    if len(unique_schemas) <= 1 and len(unique_schemas) > 0:
        print(f"     ✓ All nodes report same column structure")
        if col_sets:
            cols = list(col_sets.values())[0]
            print(f"       Columns: {', '.join(cols)}")
    elif len(unique_schemas) > 1:
        print(f"     ⚠ Column structure differs across nodes!")
        all_ok = False

    # Check 4: Look for common issues in data
    print(f"\n  4. REPLICATION PROGRESS CHECK")
    print(f"  {'─' * 50}")
    for nname, data in progress_data.items():
        if data["error"] or not data["rows"]:
            continue
        for row in data["rows"]:
            # Look for common spock.progress columns
            sub_name = row.get("subscription_name", row.get("sub_name", row.get("name", "unknown")))
            status = row.get("status", row.get("state", "unknown"))
            # Check for LSN columns
            lsn_cols = [k for k in row.keys() if "lsn" in k.lower()]
            lsn_info = ", ".join(f"{k}={row[k]}" for k in lsn_cols) if lsn_cols else "no LSN data"
            print(f"     {nname} → {sub_name}: status={status}, {lsn_info}")

    # Verdict
    print(f"\n  {'─' * 50}")
    print(f"  VERDICT")
    print(f"  {'─' * 50}")
    errors = [nn for nn, d in progress_data.items() if d["error"]]
    empty = [nn for nn, d in progress_data.items() if not d["error"] and d["row_count"] == 0]

    if errors:
        print(f"\n     ⛔ FAIL — {len(errors)} node(s) unreachable: {', '.join(errors)}")
    elif empty:
        print(f"\n     ⚠ WARNING — {len(empty)} node(s) returned empty spock.progress: {', '.join(empty)}")
        print(f"       This may indicate Spock is not configured on these nodes.")
    elif not all_ok:
        print(f"\n     ⚠ WARNING — Inconsistencies detected. Review details above.")
    else:
        print(f"\n     ✅ PASS — All {len(node_names)} nodes show consistent spock.progress data.")

    print(f"\n  TIP: For deeper AI-powered analysis, set ANTHROPIC_API_KEY and re-run.")
    print(f"{'═' * w}\n")

    return {"raw_data": {k: v for k, v in progress_data.items()}, "local_analysis": True}


# ─────────────────────────────────────────────
#  INTERACTIVE INPUT
# ─────────────────────────────────────────────

def collect_input():
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   SPOCK CLUSTER LOAD TEST — NORTHWIND + RESOURCE MGR    ║")
    print("║   Interactive Configuration                             ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    if not HAS_PSYCOPG2 and not HAS_PSYCOPG3:
        print("  ERROR: No PostgreSQL driver. Run: pip install psycopg2-binary\n")
        sys.exit(1)

    driver = "psycopg2" if HAS_PSYCOPG2 else "psycopg3"
    print(f"  DB driver : {driver}")
    print(f"  requests  : {'✓' if HAS_REQUESTS else '✗ (pip install requests for Claude analysis)'}\n")

    # Step 1
    print("─── STEP 1: Cluster Size ────────────────────────────\n")
    num_nodes = prompt("How many nodes?", default=6, cast=int,
                       validate=lambda v: v > 0 or print("    → Must be ≥ 1."))

    # Step 2
    print(f"\n─── STEP 2: Node Credentials ({num_nodes} nodes) ──────────")
    print(f"  Defaults carry forward from previous node.\n")
    nodes = OrderedDict()
    last = {"port": "5432", "user": "postgres", "password": "", "database": "northwind"}
    for i in range(1, num_nodes + 1):
        name = f"n{i}"
        print(f"  ┌── Node {name} ──────────────────────────────────")
        host = prompt(f"  │  Host / IP", cast=str)
        port = prompt(f"  │  Port", default=last["port"], cast=str)
        user = prompt(f"  │  DB User", default=last["user"], cast=str)
        password = prompt(f"  │  DB Password", default=last["password"] if last["password"] else None, cast=str)
        database = prompt(f"  │  Database", default=last["database"], cast=str)
        nodes[name] = {"host": host, "port": int(port), "user": user, "password": password, "database": database}
        last.update({"port": port, "user": user, "password": password, "database": database})
        print(f"  └── ✓ {name} → {host}:{port}/{database}\n")

    # Verify
    print("─── Verifying Connections ──────────────────────────\n")
    all_ok = True
    for name, node in nodes.items():
        try:
            conn = get_connection(node)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='customers';")
            has_nw = cur.fetchone()[0] > 0
            cur.close(); conn.close()
            if has_nw:
                print(f"  ✓ {name}  {node['host']}:{node['port']} — Northwind detected")
            else:
                print(f"  ⚠ {name}  {node['host']}:{node['port']} — connected but 'customers' table missing")
                all_ok = False
        except Exception as e:
            all_ok = False
            print(f"  ✗ {name}  {node['host']}:{node['port']} — FAILED: {e}")
    if not all_ok:
        fix = prompt_choice("\n  Issues detected. Continue?", ["yes", "no"], default="no")
        if fix == "no":
            sys.exit(1)

    # Step 3
    print(f"\n─── STEP 3: Assign Operations ──────────────────────\n")
    print(f"    insert — customers, orders + details, products")
    print(f"    update — customers, orders, products")
    print(f"    delete — order_details, orders, customers (FK-safe)")
    print(f"    idle   — skip this node\n")
    assignments = OrderedDict()
    for name in nodes:
        n = nodes[name]
        op = prompt_choice(f"  Operation for {name} ({n['host']})", VALID_OPERATIONS, default="idle")
        assignments[name] = op
        print(f"    {name} {'⏸  skip' if op == 'idle' else f'→  {op.upper()}'}\n")
    active = {k: v for k, v in assignments.items() if v != "idle"}
    if not active:
        print("  No active operations.\n"); sys.exit(0)

    # Step 4
    print("─── STEP 4: Duration (seconds) ─────────────────────\n")
    durations = {}
    for name, op in active.items():
        dur = prompt(f"  Duration for {name} ({op})", default=60, cast=int,
                     validate=lambda v: v > 0 or print("    → Must be > 0."))
        durations[name] = dur

    # Step 5
    print(f"\n─── STEP 5: Transaction Interval ───────────────────\n")
    interval_us = prompt("  Interval (microseconds)", default=1000, cast=int,
                         validate=lambda v: v >= 0 or print("    → Must be ≥ 0."))
    print(f"    → {interval_us} µs = {interval_us / 1000:.2f} ms\n")

    return nodes, assignments, durations, interval_us


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    nodes, assignments, durations, interval_us = collect_input()
    active = {k: v for k, v in assignments.items() if v != "idle"}

    # Summary
    print("═" * 62)
    print("  CONFIGURATION SUMMARY")
    print("═" * 62)
    for name in nodes:
        op = assignments[name]; n = nodes[name]
        if op == "idle":
            print(f"  {name}  {n['host']:>15}:{n['port']}  ⏸  idle")
        else:
            print(f"  {name}  {n['host']:>15}:{n['port']}  →  {op:<10} for {durations[name]}s")
    print(f"\n  Interval : {interval_us} µs  |  Database : Northwind  |  Active : {len(active)}/{len(nodes)}")
    print("═" * 62)

    go = prompt_choice("\n  Start load test?", ["yes", "no"], default="yes")
    if go != "yes":
        print("  Aborted.\n"); sys.exit(0)

    # ── Run load test ──
    print(f"\n[LOAD TEST]  Press Ctrl+C to stop early.\n")
    all_stats = []
    wall_start = time.time()
    with ThreadPoolExecutor(max_workers=len(active)) as executor:
        futures = {executor.submit(run_node, name, nodes[name], op, durations[name], interval_us): name
                   for name, op in active.items()}
        for f in as_completed(futures):
            result = f.result()
            if result:
                all_stats.append(result)
    wall_elapsed = time.time() - wall_start
    print(f"\n  Total wall time: {wall_elapsed:.1f}s")

    # ── Load report ──
    load_report = {}
    if all_stats:
        load_report = print_load_report(all_stats)

    # ── Consistency check ──
    print(f"\n  Waiting 5 seconds for replication to settle...")
    time.sleep(5)
    health_report = check_cluster_health(nodes)

    # ── Resource Manager ──
    run_rm = prompt_choice("\n  Run Resource Manager check (spock.progress)?", ["yes", "no"], default="yes")

    rm_report = {}
    if run_rm == "yes":
        rm_report = run_resource_manager_check(nodes)

    # ── Save combined report ──
    report_file = f"spock_northwind_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    combined = {
        "timestamp": datetime.now().isoformat(),
        "database": "northwind",
        "interval_us": interval_us,
        "load_test": load_report,
        "cluster_health": health_report,
        "resource_manager": rm_report,
    }
    with open(report_file, "w") as f:
        json.dump(combined, f, indent=2, default=str)
    print(f"\n  📄 Full report saved → {report_file}")

    # ── Re-check loops ──
    while True:
        print(f"\n  What next?")
        choice = prompt_choice("  ", ["consistency_check", "resource_manager", "both", "done"], default="done")
        if choice == "done":
            break
        if choice in ("consistency_check", "both"):
            print(f"\n  Waiting 5 seconds...")
            time.sleep(5)
            check_cluster_health(nodes)
        if choice in ("resource_manager", "both"):
            run_resource_manager_check(nodes)

    print("\n  Done. Goodbye!\n")


if __name__ == "__main__":
    main()

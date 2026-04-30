#!/usr/bin/env python3
"""
Spock TRANSDISCARD/DISCARD Load Test Script
============================================
Tests Spock's discard behaviour across N nodes.

Test Coverage:
  Doc-aligned exception-behaviour tests (NEW):
    - discard_mode      : Validates spock.exception_behaviour='DISCARD' against
                          a planted unique-key conflict (single transaction).
    - transdiscard_mode : Validates spock.exception_behaviour='TRANSDISCARD'
                          (single transaction).

  Doc-aligned LOAD tests (concurrent, duration-based):
    - discard_load      : Many concurrent provider txns under DISCARD mode,
                          mix of clean + conflicting. Reports throughput,
                          latency, subscriber row counts, and verifies
                          row-count expectation
                          (~clean*3 + conflict*2 rows on subscriber).
    - transdiscard_load : Same as above under TRANSDISCARD mode. Conflicting
                          txns lose ALL 3 rows, so subscriber expectation is
                          ~clean*3 only.
    - doc_load          : Runs both load tests back-to-back.

  Legacy exception-rule tests (kept from original script):
    - validate          : Insert mix of normal/discard/transdiscard-marked rows
                          and verify per-node row counts match exception rules.
    - stress            : High-concurrency throughput/latency test.
    - conflict          : Concurrent updates from multiple nodes against the
                          same PKs.

Spock Discard Concepts:
  spock.exception_behaviour (GUC) controls how the apply worker handles ANY
  conflict/exception during replication:
    - 'DISCARD'      : skip just the failing operation (savepoint rollback);
                       the rest of the remote transaction continues.
    - 'TRANSDISCARD' : roll back the entire remote transaction on this node.
  Exception rules (spock.add_exception_rule) are a separate, finer-grained
  feature that targets specific rows by predicate.

Usage:
    # Doc-style DISCARD test (creates its own test_discard table)
    python test_spock_discard.py --config nodes.json --test discard_mode

    # Doc-style TRANSDISCARD test (creates its own test_transdiscard table)
    python test_spock_discard.py --config nodes.json --test transdiscard_mode

    # Run BOTH doc tests in sequence
    python test_spock_discard.py --config nodes.json --test doc

    # Legacy modes still need --table
    python test_spock_discard.py --config nodes.json --table public.customers --test validate
    python test_spock_discard.py --config nodes.json --table public.customers --test stress
    python test_spock_discard.py --config nodes.json --table public.customers --test conflict
    python test_spock_discard.py --config nodes.json --table public.customers --test all

    # Cleanup of legacy test data on existing table
    python test_spock_discard.py --config nodes.json --table public.customers --cleanup

    # Cleanup of doc-style test tables
    python test_spock_discard.py --config nodes.json --test discard_mode --cleanup
    python test_spock_discard.py --config nodes.json --test transdiscard_mode --cleanup

Example nodes.json:
{
    "nodes": [
        {"name": "n1", "host": "localhost", "port": 5432, "dbname": "z1", "user": "postgres", "password": "postgres"},
        {"name": "n2", "host": "localhost", "port": 5433, "dbname": "z1", "user": "postgres", "password": "postgres"},
        {"name": "n3", "host": "localhost", "port": 5434, "dbname": "z1", "user": "postgres", "password": "postgres"}
    ],
    "discard_rules": [
        {"node": "n2", "rule_name": "discard_test_marker",
         "condition": "company_name LIKE '%DISCARD_TEST%'", "action": "DISCARD"},
        {"node": "n3", "rule_name": "transdiscard_test_marker",
         "condition": "company_name LIKE '%TRANSDISCARD_TEST%'", "action": "TRANSDISCARD"}
    ]
}

For doc-style tests, the FIRST node in `nodes` is treated as the PROVIDER and
the SECOND node as the SUBSCRIBER. Any further nodes are also checked for
replication outcome but are not driven directly.
"""

import psycopg2
import json
import argparse
import time
import threading
import random
import string
import sys
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional
import statistics


class Colors:
    """ANSI color codes for terminal output"""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    END = '\033[0m'


class SpockDiscardTester:
    """Test framework for Spock discard behaviour and exception rules."""

    def __init__(self, config_file: str, table_name: Optional[str] = None,
                 verbose: bool = False):
        self.verbose = verbose
        self.config = self._load_config(config_file)
        self.nodes = self.config['nodes']
        self.discard_rules = self.config.get('discard_rules', [])

        self.table_name = table_name
        if table_name:
            self.schema_name, self.table_only = self._parse_table_name(table_name)
        else:
            self.schema_name, self.table_only = (None, None)

        self.table_metadata = None
        self.test_marker_column = None
        self.test_marker_prefix = f"SPOCK_TEST_{int(time.time())}"

        self.stats = {
            'inserts_attempted': 0, 'inserts_successful': 0,
            'updates_attempted': 0, 'updates_successful': 0,
            'transactions_attempted': 0, 'transactions_successful': 0,
            'errors': [], 'latencies': [],
            'per_node_ops': {n['name']: 0 for n in self.nodes},
        }
        self.stats_lock = threading.Lock()

    # ------------------------------------------------------------------ utils
    def _parse_table_name(self, full_name: str) -> Tuple[str, str]:
        if '.' in full_name:
            return tuple(full_name.split('.', 1))
        return ('public', full_name)

    def _load_config(self, config_file: str) -> Dict:
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"{Colors.RED}Config file not found: {config_file}{Colors.END}")
            print("Creating sample config file...")
            sample = {
                "nodes": [
                    {"name": "n1", "host": "localhost", "port": 5432, "dbname": "z1",
                     "user": "postgres", "password": "postgres"},
                    {"name": "n2", "host": "localhost", "port": 5433, "dbname": "z1",
                     "user": "postgres", "password": "postgres"},
                    {"name": "n3", "host": "localhost", "port": 5434, "dbname": "z1",
                     "user": "postgres", "password": "postgres"},
                ],
                "discard_rules": [
                    {"node": "n2", "rule_name": "discard_test_rule",
                     "condition": "company_name LIKE '%SPOCK_TEST_DISCARD%'",
                     "action": "DISCARD"},
                    {"node": "n3", "rule_name": "transdiscard_test_rule",
                     "condition": "company_name LIKE '%SPOCK_TEST_TRANSDISCARD%'",
                     "action": "TRANSDISCARD"},
                ],
            }
            with open(config_file, 'w') as f:
                json.dump(sample, f, indent=2)
            print(f"Sample config created at {config_file}")
            sys.exit(1)

    def log(self, msg: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        color_map = {
            "INFO": Colors.BLUE, "SUCCESS": Colors.GREEN, "WARN": Colors.YELLOW,
            "ERROR": Colors.RED, "HEADER": Colors.HEADER + Colors.BOLD,
        }
        color = color_map.get(level, Colors.END)
        print(f"{color}[{timestamp}] [{level:7}] {msg}{Colors.END}")

    def get_connection(self, node_name: str) -> psycopg2.extensions.connection:
        node = next((n for n in self.nodes if n['name'] == node_name), None)
        if not node:
            raise ValueError(f"Node {node_name} not found")
        return psycopg2.connect(
            host=node['host'], port=node['port'], dbname=node['dbname'],
            user=node['user'], password=node['password'], connect_timeout=10,
        )

    def execute_on_node(self, node_name: str, sql_stmt: str,
                        params: tuple = None, fetch: bool = False):
        conn = self.get_connection(node_name)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                if self.verbose:
                    self.log(f"[{node_name}] {sql_stmt[:200]}", "INFO")
                cur.execute(sql_stmt, params)
                if fetch:
                    return cur.fetchall()
        finally:
            conn.close()

    # ====================================================================
    # DOC-ALIGNED EXCEPTION BEHAVIOUR TESTS
    # ====================================================================
    #
    # Provider = nodes[0], Subscriber = nodes[1]. The provider runs a
    # multi-statement transaction; the subscriber has been pre-poisoned with
    # a row that will collide on the unique key. We then assert:
    #   DISCARD      -> non-conflicting rows arrive, conflicting one is skipped
    #   TRANSDISCARD -> nothing arrives (apply txn rolled back on subscriber)
    # Either way the exception_log on the subscriber must contain entries.

    DOC_TABLE_DISCARD = "test_discard"
    DOC_TABLE_TRANSDISCARD = "test_transdiscard"

    def _set_exception_behaviour(self, node_name: str, mode: str,
                                  set_logging: bool = True):
        """ALTER SYSTEM SET spock.exception_behaviour=... + reload."""
        mode = mode.upper()
        if mode not in ('DISCARD', 'TRANSDISCARD'):
            raise ValueError(f"Bad exception_behaviour: {mode}")
        try:
            self.execute_on_node(
                node_name,
                f"ALTER SYSTEM SET spock.exception_behaviour = '{mode}';"
            )
            if set_logging:
                self.execute_on_node(
                    node_name,
                    "ALTER SYSTEM SET spock.exception_logging = 'ALL';"
                )
            self.execute_on_node(node_name, "SELECT pg_reload_conf();")
            self.log(
                f"[{node_name}] spock.exception_behaviour = '{mode}' (reloaded)",
                "SUCCESS",
            )
        except Exception as e:
            self.log(f"[{node_name}] failed to set exception_behaviour: {e}",
                     "ERROR")
            raise

    def _truncate_exception_log(self, node_name: str):
        try:
            self.execute_on_node(node_name, "TRUNCATE spock.exception_log;")
            self.log(f"[{node_name}] truncated spock.exception_log", "INFO")
        except Exception as e:
            self.log(f"[{node_name}] could not truncate exception_log: {e}",
                     "WARN")

    def _ensure_doc_table(self, node_name: str, table: str):
        """Create the doc test table if missing and add to default repset."""
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id    INTEGER PRIMARY KEY,
            name  VARCHAR(50) UNIQUE,
            value INTEGER
        );
        """
        self.execute_on_node(node_name, ddl)
        # Add to default replication set on the provider only; subscriber
        # picks the table up via subscription/sync.
        try:
            self.execute_on_node(
                node_name,
                f"SELECT spock.repset_add_table('default', '{table}');"
            )
            self.log(f"[{node_name}] {table} added to default repset", "SUCCESS")
        except Exception as e:
            # Already in repset is fine.
            msg = str(e).lower()
            if "already" in msg or "duplicate" in msg:
                self.log(f"[{node_name}] {table} already in repset", "INFO")
            else:
                self.log(f"[{node_name}] repset_add_table warning: {e}", "WARN")

    def _wait_for_table_on_subscriber(self, sub_node: str, table: str,
                                       timeout: int = 30):
        """Block until the subscriber sees the table (synced)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                res = self.execute_on_node(
                    sub_node,
                    "SELECT to_regclass(%s);", (table,), fetch=True
                )
                if res and res[0][0] is not None:
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def _doc_drop_tables(self, table: str):
        """Best-effort cleanup of doc tables on all nodes."""
        for node in self.nodes:
            try:
                self.execute_on_node(
                    node['name'], f"DROP TABLE IF EXISTS {table} CASCADE;"
                )
                self.log(f"[{node['name']}] dropped {table}", "INFO")
            except Exception as e:
                self.log(f"[{node['name']}] drop {table} failed: {e}", "WARN")

    def _plant_poison_row(self, sub_node: str, table: str,
                          poison_id: int, poison_name: str, poison_value: int):
        """
        On the subscriber, insert the row that will conflict when the
        provider's transaction tries to apply. We use spock.repair_mode(true)
        so the row is local-only and will not be replicated back to the
        provider.
        """
        conn = self.get_connection(sub_node)
        conn.autocommit = False  # we want one explicit txn
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT spock.repair_mode(true);")
                cur.execute(
                    f"INSERT INTO {table} (id, name, value) VALUES (%s, %s, %s);",
                    (poison_id, poison_name, poison_value),
                )
            conn.commit()
            self.log(
                f"[{sub_node}] planted poison row id={poison_id} "
                f"name='{poison_name}' (repair_mode)",
                "SUCCESS",
            )
        except Exception as e:
            conn.rollback()
            self.log(f"[{sub_node}] poison-row insert failed: {e}", "ERROR")
            raise
        finally:
            conn.close()

    def _provider_run_conflict_txn(self, prov_node: str, table: str,
                                   conflict_name: str):
        """
        On the provider, run the multi-statement transaction:
            INSERT (1, 'ok_..._1', 100)
            INSERT (2, conflict_name, 200)   <-- will conflict on subscriber
            INSERT (3, 'ok_..._3', 300)
        and COMMIT.
        On the provider itself this all succeeds (no conflict locally).
        """
        conn = self.get_connection(prov_node)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {table} (id, name, value) VALUES (%s, %s, %s);",
                    (1, f"ok_name_1_{int(time.time())}", 100),
                )
                cur.execute(
                    f"INSERT INTO {table} (id, name, value) VALUES (%s, %s, %s);",
                    (2, conflict_name, 200),
                )
                cur.execute(
                    f"INSERT INTO {table} (id, name, value) VALUES (%s, %s, %s);",
                    (3, f"ok_name_3_{int(time.time())}", 300),
                )
            conn.commit()
            self.log(f"[{prov_node}] provider transaction COMMITTED "
                     f"(rows 1,2,3 into {table})", "SUCCESS")
        except Exception as e:
            conn.rollback()
            self.log(f"[{prov_node}] provider txn failed: {e}", "ERROR")
            raise
        finally:
            conn.close()

    def _check_subscriber_rows(self, sub_node: str, table: str,
                                expected: Dict[int, bool]) -> bool:
        """expected is {id: should_exist}. Returns True if all match."""
        all_ok = True
        for row_id, should_exist in expected.items():
            try:
                res = self.execute_on_node(
                    sub_node,
                    f"SELECT EXISTS (SELECT 1 FROM {table} WHERE id = %s);",
                    (row_id,), fetch=True,
                )
                exists = bool(res[0][0]) if res else False
                ok = (exists == should_exist)
                icon = "✓" if ok else "✗"
                lvl = "SUCCESS" if ok else "ERROR"
                self.log(
                    f"  {icon} id={row_id}: exists={exists} expected={should_exist}",
                    lvl,
                )
                if not ok:
                    all_ok = False
            except Exception as e:
                self.log(f"  ✗ id={row_id}: query error {e}", "ERROR")
                all_ok = False
        return all_ok

    def _exception_log_summary(self, sub_node: str) -> Dict[str, Any]:
        """Pull a small summary of spock.exception_log from subscriber."""
        out = {'total': 0, 'inserts': 0, 'null_error_msgs': 0, 'recent': []}
        try:
            res = self.execute_on_node(
                sub_node,
                """
                SELECT
                    count(*),
                    count(*) FILTER (WHERE operation = 'INSERT'),
                    count(*) FILTER (WHERE error_message IS NULL)
                FROM spock.exception_log;
                """,
                fetch=True,
            )
            if res:
                out['total'], out['inserts'], out['null_error_msgs'] = res[0]
        except Exception as e:
            self.log(f"[{sub_node}] could not aggregate exception_log: {e}",
                     "WARN")
        try:
            res = self.execute_on_node(
                sub_node,
                """
                SELECT operation, error_message
                FROM spock.exception_log
                ORDER BY retry_errored_at DESC NULLS LAST
                LIMIT 5;
                """,
                fetch=True,
            )
            out['recent'] = res or []
        except Exception as e:
            self.log(f"[{sub_node}] could not list recent exception_log "
                     f"rows: {e}", "WARN")
        return out

    # --------------------------------------------------------------- DISCARD
    def test_discard_mode(self, wait_seconds: int = 10):
        """
        Doc Part 1: spock.exception_behaviour = 'DISCARD'.
        Expectation on subscriber after replication:
            - id=1 present
            - id=2 NOT present (conflict skipped)
            - id=3 present
            - exception_log has at least one INSERT entry
            - no log row has NULL error_message
        """
        self.log("=" * 75, "HEADER")
        self.log("DOC TEST 1: DISCARD MODE", "HEADER")
        self.log("=" * 75, "HEADER")

        if len(self.nodes) < 2:
            self.log("Need at least 2 nodes (provider + subscriber)", "ERROR")
            return False

        provider = self.nodes[0]['name']
        subscriber = self.nodes[1]['name']
        table = self.DOC_TABLE_DISCARD
        conflict_name = "conflict_name"

        try:
            # 1. Set GUC on both sides.
            self.log("--- Configuring exception_behaviour=DISCARD ---", "INFO")
            self._set_exception_behaviour(provider, 'DISCARD')
            self._set_exception_behaviour(subscriber, 'DISCARD')

            # 2. Truncate exception log on both.
            self._truncate_exception_log(provider)
            self._truncate_exception_log(subscriber)

            # 3. Create table on provider; ensure subscriber has it.
            self.log(f"--- Creating {table} on provider ---", "INFO")
            self._ensure_doc_table(provider, table)
            if not self._wait_for_table_on_subscriber(subscriber, table):
                # Fallback: create directly on subscriber as well.
                self.log(f"[{subscriber}] table not auto-synced; creating "
                         f"locally", "WARN")
                self._ensure_doc_table(subscriber, table)

            # Clean any stale rows from previous runs.
            for n in (provider, subscriber):
                try:
                    self.execute_on_node(n, f"DELETE FROM {table};")
                except Exception:
                    pass
            time.sleep(2)

            # 4. Plant poison row on subscriber.
            self.log("--- Planting poison row on subscriber ---", "INFO")
            self._plant_poison_row(subscriber, table, 100, conflict_name, 999)

            # 5. Provider runs the multi-row transaction.
            self.log("--- Running provider transaction ---", "INFO")
            self._provider_run_conflict_txn(provider, table, conflict_name)

            # 6. Wait for replication.
            self.log(f"Waiting {wait_seconds}s for replication...", "INFO")
            time.sleep(wait_seconds)

            # 7. Verify subscriber state.
            self.log("--- Verifying subscriber row presence ---", "HEADER")
            row_ok = self._check_subscriber_rows(
                subscriber, table,
                expected={1: True, 2: False, 3: True},
            )

            # 8. Verify exception_log on subscriber.
            self.log("--- Verifying spock.exception_log on subscriber ---",
                     "HEADER")
            summary = self._exception_log_summary(subscriber)
            self.log(f"  total entries:   {summary['total']}", "INFO")
            self.log(f"  INSERT entries:  {summary['inserts']}", "INFO")
            self.log(f"  NULL error_msgs: {summary['null_error_msgs']}", "INFO")
            for op, err in summary['recent']:
                err_short = (err[:120] + '...') if err and len(err) > 120 else err
                self.log(f"    [{op}] {err_short}", "INFO")

            log_ok = (summary['total'] >= 1
                      and summary['inserts'] >= 1
                      and summary['null_error_msgs'] == 0)
            if log_ok:
                self.log("  ✓ exception_log looks correct", "SUCCESS")
            else:
                self.log("  ✗ exception_log not as expected", "ERROR")

            passed = row_ok and log_ok
            if passed:
                self.log("\n🎉 DISCARD MODE TEST PASSED", "SUCCESS")
            else:
                self.log("\n⚠️  DISCARD MODE TEST FAILED", "ERROR")
            return passed

        except Exception as e:
            self.log(f"DISCARD test errored: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return False

    # ---------------------------------------------------------- TRANSDISCARD
    def test_transdiscard_mode(self, wait_seconds: int = 10):
        """
        Doc Part 2: spock.exception_behaviour = 'TRANSDISCARD'.
        Expectation on subscriber after replication:
            - id=1, 2, 3 ALL absent (entire apply txn rolled back)
            - exception_log still has entries (logged in separate txn)
            - no log row has NULL error_message
        """
        self.log("=" * 75, "HEADER")
        self.log("DOC TEST 2: TRANSDISCARD MODE", "HEADER")
        self.log("=" * 75, "HEADER")

        if len(self.nodes) < 2:
            self.log("Need at least 2 nodes (provider + subscriber)", "ERROR")
            return False

        provider = self.nodes[0]['name']
        subscriber = self.nodes[1]['name']
        table = self.DOC_TABLE_TRANSDISCARD
        conflict_name = "td_conflict"

        try:
            self.log("--- Configuring exception_behaviour=TRANSDISCARD ---",
                     "INFO")
            self._set_exception_behaviour(provider, 'TRANSDISCARD')
            self._set_exception_behaviour(subscriber, 'TRANSDISCARD')

            self._truncate_exception_log(provider)
            self._truncate_exception_log(subscriber)

            self.log(f"--- Creating {table} on provider ---", "INFO")
            self._ensure_doc_table(provider, table)
            if not self._wait_for_table_on_subscriber(subscriber, table):
                self.log(f"[{subscriber}] table not auto-synced; creating "
                         f"locally", "WARN")
                self._ensure_doc_table(subscriber, table)

            for n in (provider, subscriber):
                try:
                    self.execute_on_node(n, f"DELETE FROM {table};")
                except Exception:
                    pass
            time.sleep(2)

            self.log("--- Planting poison row on subscriber ---", "INFO")
            self._plant_poison_row(subscriber, table, 100, conflict_name, 999)

            self.log("--- Running provider transaction ---", "INFO")
            self._provider_run_conflict_txn(provider, table, conflict_name)

            self.log(f"Waiting {wait_seconds}s for replication...", "INFO")
            time.sleep(wait_seconds)

            self.log("--- Verifying subscriber row presence "
                     "(expect ALL absent) ---", "HEADER")
            row_ok = self._check_subscriber_rows(
                subscriber, table,
                expected={1: False, 2: False, 3: False},
            )

            self.log("--- Verifying spock.exception_log on subscriber ---",
                     "HEADER")
            summary = self._exception_log_summary(subscriber)
            self.log(f"  total entries:   {summary['total']}", "INFO")
            self.log(f"  INSERT entries:  {summary['inserts']}", "INFO")
            self.log(f"  NULL error_msgs: {summary['null_error_msgs']}", "INFO")
            for op, err in summary['recent']:
                err_short = (err[:120] + '...') if err and len(err) > 120 else err
                self.log(f"    [{op}] {err_short}", "INFO")

            log_ok = (summary['total'] >= 1
                      and summary['null_error_msgs'] == 0)
            if log_ok:
                self.log("  ✓ exception_log persisted across rollback", "SUCCESS")
            else:
                self.log("  ✗ exception_log not as expected", "ERROR")

            passed = row_ok and log_ok
            if passed:
                self.log("\n🎉 TRANSDISCARD MODE TEST PASSED", "SUCCESS")
            else:
                self.log("\n⚠️  TRANSDISCARD MODE TEST FAILED", "ERROR")
            return passed

        except Exception as e:
            self.log(f"TRANSDISCARD test errored: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return False

    # ====================================================================
    # DOC-ALIGNED LOAD TESTS (concurrent, duration-based)
    # ====================================================================
    #
    # Same conceptual flow as the single-shot doc tests, but driven by N
    # concurrent worker threads on the provider for `duration` seconds.
    # Each worker runs many independent transactions; a `conflict_ratio`
    # fraction of those transactions include a row that collides with one
    # of the pre-planted poison rows on the subscriber.
    #
    # Layout per provider transaction:
    #   - 2 rows with provider-unique ids and unique names  (always succeed
    #     locally on the provider)
    #   - 1 row marked "conflict" iff this txn is in the conflicting bucket
    #
    # We avoid PK collisions on the *provider* by giving every row a
    # globally-unique id (worker_id * BIG + counter). Conflicts are only
    # induced on the *subscriber* via the unique 'name' column matching one
    # of the planted poison names.

    POISON_POOL_SIZE = 64  # number of distinct poison names planted

    def _generate_poison_names(self, prefix: str) -> List[str]:
        return [f"{prefix}_poison_{i:04d}" for i in range(self.POISON_POOL_SIZE)]

    def _plant_poison_pool(self, sub_node: str, table: str,
                           poison_names: List[str], id_base: int = 1_000_000):
        """Plant a pool of poison rows on subscriber under repair_mode."""
        conn = self.get_connection(sub_node)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT spock.repair_mode(true);")
                for i, name in enumerate(poison_names):
                    cur.execute(
                        f"INSERT INTO {table} (id, name, value) "
                        f"VALUES (%s, %s, %s);",
                        (id_base + i, name, 999),
                    )
            conn.commit()
            self.log(
                f"[{sub_node}] planted {len(poison_names)} poison rows "
                f"(ids {id_base}..{id_base + len(poison_names) - 1})",
                "SUCCESS",
            )
        except Exception as e:
            conn.rollback()
            self.log(f"[{sub_node}] poison pool insert failed: {e}", "ERROR")
            raise
        finally:
            conn.close()

    def _run_doc_load(self, mode: str, table: str, duration: int,
                      threads: int, conflict_ratio: float,
                      wait_seconds: int) -> bool:
        """
        Generic load runner for DISCARD/TRANSDISCARD modes.

        Returns True if the observed subscriber state is consistent with the
        configured mode (within tolerance). Exact equality isn't asserted
        because async replication and timing make some slop unavoidable.
        """
        mode = mode.upper()
        assert mode in ('DISCARD', 'TRANSDISCARD')

        if len(self.nodes) < 2:
            self.log("Need at least 2 nodes (provider + subscriber)", "ERROR")
            return False
        provider = self.nodes[0]['name']
        subscriber = self.nodes[1]['name']

        self.log("=" * 75, "HEADER")
        self.log(f"LOAD TEST: {mode} MODE  "
                 f"(duration={duration}s, threads={threads}, "
                 f"conflict_ratio={conflict_ratio})", "HEADER")
        self.log("=" * 75, "HEADER")

        # ---- 1. configure GUCs ------------------------------------------
        self.log(f"--- Configuring exception_behaviour={mode} ---", "INFO")
        try:
            self._set_exception_behaviour(provider, mode)
            self._set_exception_behaviour(subscriber, mode)
        except Exception:
            return False
        self._truncate_exception_log(provider)
        self._truncate_exception_log(subscriber)

        # ---- 2. table setup ---------------------------------------------
        self.log(f"--- Preparing {table} ---", "INFO")
        self._ensure_doc_table(provider, table)
        if not self._wait_for_table_on_subscriber(subscriber, table):
            self.log(f"[{subscriber}] table not auto-synced; creating locally",
                     "WARN")
            self._ensure_doc_table(subscriber, table)
        for n in (provider, subscriber):
            try:
                self.execute_on_node(n, f"DELETE FROM {table};")
            except Exception:
                pass
        time.sleep(2)

        # ---- 3. plant poison pool on subscriber -------------------------
        run_tag = f"L{int(time.time())}"
        poison_prefix = f"{run_tag}_{mode.lower()}"
        poison_names = self._generate_poison_names(poison_prefix)
        self._plant_poison_pool(subscriber, table, poison_names,
                                id_base=10_000_000)

        # ---- 4. launch workers ------------------------------------------
        stop_event = threading.Event()
        worker_stats = {
            i: {'clean_txns': 0, 'conflict_txns': 0,
                'errors': 0, 'latencies': []}
            for i in range(threads)
        }
        stats_lock = threading.Lock()

        # Reserve disjoint id ranges per worker to avoid provider-side PK
        # collisions. Each worker owns BLOCK ids starting at base.
        ID_BLOCK = 10_000_000  # ample headroom

        def worker(worker_id: int):
            base = worker_id * ID_BLOCK
            counter = 0
            local = {'clean_txns': 0, 'conflict_txns': 0,
                     'errors': 0, 'latencies': []}
            try:
                conn = self.get_connection(provider)
                conn.autocommit = False
                cur = conn.cursor()

                while not stop_event.is_set():
                    # Decide if this txn carries a conflict.
                    is_conflict = (random.random() < conflict_ratio)
                    rows_to_insert = []

                    # row 1 — always clean
                    counter += 1
                    rows_to_insert.append((
                        base + counter,
                        f"{run_tag}_w{worker_id}_n{counter}_a",
                        random.randint(1, 10000),
                    ))
                    # row 2 — clean OR conflict (chosen poison name)
                    counter += 1
                    if is_conflict:
                        poison_name = random.choice(poison_names)
                        rows_to_insert.append((
                            base + counter, poison_name,
                            random.randint(1, 10000),
                        ))
                    else:
                        rows_to_insert.append((
                            base + counter,
                            f"{run_tag}_w{worker_id}_n{counter}_b",
                            random.randint(1, 10000),
                        ))
                    # row 3 — always clean
                    counter += 1
                    rows_to_insert.append((
                        base + counter,
                        f"{run_tag}_w{worker_id}_n{counter}_c",
                        random.randint(1, 10000),
                    ))

                    op_start = time.time()
                    try:
                        for row in rows_to_insert:
                            cur.execute(
                                f"INSERT INTO {table} (id, name, value) "
                                f"VALUES (%s, %s, %s);", row,
                            )
                        conn.commit()
                        local['latencies'].append(
                            (time.time() - op_start) * 1000)
                        if is_conflict:
                            local['conflict_txns'] += 1
                        else:
                            local['clean_txns'] += 1
                    except Exception as e:
                        conn.rollback()
                        local['errors'] += 1
                        if self.verbose and local['errors'] <= 3:
                            self.log(f"[w{worker_id}] txn error: {e}", "WARN")
                cur.close()
                conn.close()
            except Exception as e:
                self.log(f"[w{worker_id}] fatal: {e}", "ERROR")

            with stats_lock:
                for k in ('clean_txns', 'conflict_txns', 'errors'):
                    worker_stats[worker_id][k] = local[k]
                worker_stats[worker_id]['latencies'] = local['latencies']

        self.log(f"--- Launching {threads} workers on {provider} for "
                 f"{duration}s ---", "INFO")
        threads_list = []
        start_time = time.time()
        for i in range(threads):
            t = threading.Thread(target=worker, args=(i,), daemon=True)
            t.start()
            threads_list.append(t)

        # progress
        for remaining in range(duration, 0, -10):
            time.sleep(min(10, remaining))
            with stats_lock:
                done_clean = sum(s['clean_txns'] for s in worker_stats.values())
                done_conf = sum(s['conflict_txns']
                                for s in worker_stats.values())
            elapsed = time.time() - start_time
            total_done = done_clean + done_conf
            rate = total_done / elapsed if elapsed > 0 else 0
            self.log(f"  Progress: {total_done:,} txns "
                     f"(clean={done_clean:,}, conflict={done_conf:,}), "
                     f"{rate:.0f} txn/s, {remaining}s remaining", "INFO")

        stop_event.set()
        for t in threads_list:
            t.join(timeout=15)
        actual_duration = time.time() - start_time

        # ---- 5. aggregate provider-side stats ---------------------------
        total_clean = sum(s['clean_txns'] for s in worker_stats.values())
        total_conflict = sum(s['conflict_txns']
                             for s in worker_stats.values())
        total_errors = sum(s['errors'] for s in worker_stats.values())
        total_txns = total_clean + total_conflict
        all_lat = []
        for s in worker_stats.values():
            all_lat.extend(s['latencies'])

        self.log("\n--- Provider Workload Results ---", "HEADER")
        self.log(f"  duration:    {actual_duration:.1f}s", "INFO")
        self.log(f"  total txns:  {total_txns:,}", "INFO")
        self.log(f"  clean:       {total_clean:,}", "INFO")
        self.log(f"  conflicting: {total_conflict:,}", "INFO")
        self.log(f"  errors:      {total_errors}",
                 "WARN" if total_errors else "INFO")
        if total_txns > 0:
            self.log(f"  throughput:  {total_txns / actual_duration:.0f} txn/s",
                     "SUCCESS")
        if all_lat:
            sl = sorted(all_lat)
            self.log(
                f"  latency (ms) avg={statistics.mean(all_lat):.2f} "
                f"p50={sl[len(sl) // 2]:.2f} "
                f"p95={sl[int(len(sl) * 0.95)]:.2f} "
                f"p99={sl[int(len(sl) * 0.99)]:.2f}", "INFO")

        # Provider-side row counts (every txn that committed = 3 rows)
        provider_rows_expected = total_txns * 3

        # ---- 6. wait for replication ------------------------------------
        self.log(f"\nWaiting {wait_seconds}s for replication catchup...",
                 "INFO")
        time.sleep(wait_seconds)

        # ---- 7. measure subscriber-side outcome -------------------------
        self.log("--- Subscriber State ---", "HEADER")

        # rows tagged with this run (excludes poison rows whose names start
        # with poison_prefix; we filter by run_tag).
        try:
            sub_count = self.execute_on_node(
                subscriber,
                f"SELECT count(*) FROM {table} WHERE name LIKE %s;",
                (f"{run_tag}_w%",), fetch=True,
            )[0][0]
        except Exception as e:
            self.log(f"  subscriber count error: {e}", "ERROR")
            sub_count = -1

        try:
            prov_count = self.execute_on_node(
                provider,
                f"SELECT count(*) FROM {table} WHERE name LIKE %s;",
                (f"{run_tag}_w%",), fetch=True,
            )[0][0]
        except Exception as e:
            self.log(f"  provider count error: {e}", "ERROR")
            prov_count = -1

        self.log(f"  provider row count (this run):   {prov_count}", "INFO")
        self.log(f"  subscriber row count (this run): {sub_count}", "INFO")

        # Expectations:
        #   DISCARD:      conflicting txn loses 1 row; the other 2 still apply.
        #                 expected ≈ clean*3 + conflict*2
        #   TRANSDISCARD: conflicting txn loses ALL 3 rows (txn rolled back).
        #                 expected ≈ clean*3
        if mode == 'DISCARD':
            expected_sub = total_clean * 3 + total_conflict * 2
        else:
            expected_sub = total_clean * 3

        self.log(f"  expected on subscriber:          {expected_sub} "
                 f"(provider has {provider_rows_expected})", "INFO")

        # tolerance: allow small slop from in-flight replication.
        if sub_count >= 0 and expected_sub > 0:
            ratio = sub_count / expected_sub
            within = 0.95 <= ratio <= 1.05
            self.log(f"  ratio observed/expected:         {ratio:.3f} "
                     f"({'OK' if within else 'OUT OF TOLERANCE'})",
                     "SUCCESS" if within else "ERROR")
        else:
            within = (sub_count == expected_sub)

        # exception_log on subscriber
        self.log("--- Subscriber spock.exception_log ---", "HEADER")
        summary = self._exception_log_summary(subscriber)
        self.log(f"  total entries:   {summary['total']}", "INFO")
        self.log(f"  INSERT entries:  {summary['inserts']}", "INFO")
        self.log(f"  NULL error_msgs: {summary['null_error_msgs']}", "INFO")

        log_ok = (summary['total'] >= 1
                  and summary['null_error_msgs'] == 0)
        if total_conflict == 0:
            # If no conflicts were generated, no log entries are required.
            log_ok = (summary['null_error_msgs'] == 0)

        if log_ok:
            self.log("  ✓ exception_log looks correct", "SUCCESS")
        else:
            self.log("  ✗ exception_log not as expected", "ERROR")

        passed = within and log_ok
        if passed:
            self.log(f"\n🎉 {mode} LOAD TEST PASSED", "SUCCESS")
        else:
            self.log(f"\n⚠️  {mode} LOAD TEST FAILED", "ERROR")
        return passed

    def test_discard_mode_load(self, duration: int = 60, threads: int = 8,
                                conflict_ratio: float = 0.3,
                                wait_seconds: int = 15) -> bool:
        return self._run_doc_load(
            mode='DISCARD',
            table=self.DOC_TABLE_DISCARD,
            duration=duration, threads=threads,
            conflict_ratio=conflict_ratio,
            wait_seconds=wait_seconds,
        )

    def test_transdiscard_mode_load(self, duration: int = 60, threads: int = 8,
                                     conflict_ratio: float = 0.3,
                                     wait_seconds: int = 15) -> bool:
        return self._run_doc_load(
            mode='TRANSDISCARD',
            table=self.DOC_TABLE_TRANSDISCARD,
            duration=duration, threads=threads,
            conflict_ratio=conflict_ratio,
            wait_seconds=wait_seconds,
        )

    def run_doc_load_tests(self, duration: int = 60, threads: int = 8,
                           conflict_ratio: float = 0.3,
                           wait_seconds: int = 15) -> bool:
        """Run BOTH load tests back-to-back and summarise."""
        self.log("=" * 75, "HEADER")
        self.log("DOC-ALIGNED LOAD TESTS (DISCARD + TRANSDISCARD)", "HEADER")
        self.log("=" * 75, "HEADER")
        self.log(f"Provider: {self.nodes[0]['name']}", "INFO")
        if len(self.nodes) >= 2:
            self.log(f"Subscriber: {self.nodes[1]['name']}", "INFO")

        d_ok = self.test_discard_mode_load(
            duration=duration, threads=threads,
            conflict_ratio=conflict_ratio, wait_seconds=wait_seconds,
        )
        time.sleep(3)
        td_ok = self.test_transdiscard_mode_load(
            duration=duration, threads=threads,
            conflict_ratio=conflict_ratio, wait_seconds=wait_seconds,
        )

        self.log("\n" + "=" * 75, "HEADER")
        self.log("DOC LOAD TEST SUMMARY", "HEADER")
        self.log("=" * 75, "HEADER")
        self.log(f"  DISCARD load      : {'PASS' if d_ok else 'FAIL'}",
                 "SUCCESS" if d_ok else "ERROR")
        self.log(f"  TRANSDISCARD load : {'PASS' if td_ok else 'FAIL'}",
                 "SUCCESS" if td_ok else "ERROR")
        return d_ok and td_ok

    # ====================================================================
    # LEGACY: discovery, exception-rule tests (kept from original script)
    # ====================================================================

    def discover_table_structure(self):
        """Discover the structure of the existing user table."""
        if not self.table_name:
            self.log("--table is required for legacy modes", "ERROR")
            sys.exit(1)

        self.log("=" * 75, "HEADER")
        self.log(f"DISCOVERING TABLE STRUCTURE: {self.table_name}", "HEADER")
        self.log("=" * 75, "HEADER")

        source_node = self.nodes[0]['name']

        try:
            columns_result = self.execute_on_node(source_node, f"""
                SELECT column_name, data_type, character_maximum_length,
                       is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = '{self.schema_name}'
                  AND table_name = '{self.table_only}'
                ORDER BY ordinal_position;
            """, fetch=True)

            if not columns_result:
                self.log(f"Table {self.table_name} not found!", "ERROR")
                sys.exit(1)

            self.log(f"Table has {len(columns_result)} columns:", "INFO")

            text_columns = []
            for col in columns_result:
                col_name, data_type, max_len, nullable, default = col
                self.log(f"  - {col_name}: {data_type}" +
                         (f"({max_len})" if max_len else ""), "INFO")
                if data_type in ('text', 'character varying', 'varchar',
                                 'character', 'char'):
                    text_columns.append((col_name, max_len))

            pk_result = self.execute_on_node(source_node, f"""
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid
                                   AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = '{self.schema_name}.{self.table_only}'::regclass
                  AND i.indisprimary;
            """, fetch=True)
            primary_keys = [row[0] for row in pk_result] if pk_result else []

            count_result = self.execute_on_node(
                source_node, f"SELECT count(*) FROM {self.table_name}",
                fetch=True,
            )
            row_count = count_result[0][0] if count_result else 0

            self.table_metadata = {
                'columns': columns_result,
                'text_columns': text_columns,
                'primary_keys': primary_keys,
                'row_count': row_count,
                'source_node': source_node,
            }

            self.log(f"Primary keys: {primary_keys}", "INFO")
            self.log(f"Current row count: {row_count}", "INFO")
            self.log(f"Text columns available for markers: "
                     f"{[c[0] for c in text_columns]}", "INFO")

            if text_columns:
                preferred = [c for c in text_columns if 'name' in c[0].lower()]
                self.test_marker_column = (preferred[0][0] if preferred
                                           else text_columns[0][0])
                self.log(f"Auto-selected marker column: "
                         f"{self.test_marker_column}", "SUCCESS")
            else:
                self.log("No text column found for test markers!", "ERROR")
                sys.exit(1)

            self._verify_replication_setup()

        except Exception as e:
            self.log(f"Failed to discover table: {e}", "ERROR")
            sys.exit(1)

    def _verify_replication_setup(self):
        source_node = self.nodes[0]['name']
        try:
            result = self.execute_on_node(source_node, f"""
                SELECT rs.set_name
                FROM spock.replication_set_table rt
                JOIN spock.replication_set rs ON rt.set_id = rs.set_id
                WHERE rt.set_reloid = '{self.schema_name}.{self.table_only}'::regclass;
            """, fetch=True)
            if result:
                repsets = [row[0] for row in result]
                self.log(f"Table is in replication set(s): {repsets}", "SUCCESS")
            else:
                self.log(f"WARNING: Table {self.table_name} is NOT in any "
                         f"replication set!", "WARN")
                self.log("Run: SELECT spock.repset_add_table('default', "
                         f"'{self.table_name}'); on source node first", "WARN")
                response = input("Continue anyway? (y/N): ")
                if response.lower() != 'y':
                    sys.exit(1)
        except Exception as e:
            self.log(f"Could not verify replication setup: {e}", "WARN")

    def setup_discard_rules(self):
        """Configure DISCARD/TRANSDISCARD exception rules from config."""
        self.log("=" * 75, "HEADER")
        self.log("SETTING UP EXCEPTION RULES", "HEADER")
        self.log("=" * 75, "HEADER")

        if not self.discard_rules:
            self.log("No discard rules in config - using defaults based on table",
                     "WARN")
            self._setup_default_rules()
            return

        for rule in self.discard_rules:
            node_name = rule['node']
            rule_name = rule['rule_name']
            condition = rule['condition']
            action = rule['action'].upper()

            try:
                rule_sql = """
                    SELECT spock.add_exception_rule(
                        rule_name := %s,
                        relation := %s,
                        condition := %s,
                        rule_action := %s
                    );
                """
                conn = self.get_connection(node_name)
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(rule_sql,
                                (rule_name, self.table_name, condition, action))
                conn.close()

                self.log(f"[{node_name}] Added {action} rule: {rule_name}",
                         "SUCCESS")
                self.log(f"  Condition: {condition}", "INFO")
            except Exception as e:
                self.log(f"[{node_name}] Failed to add rule {rule_name}: {e}",
                         "ERROR")
                self.log("Note: check your Spock version - function name may "
                         "differ", "WARN")

        time.sleep(2)

    def _setup_default_rules(self):
        if len(self.nodes) >= 2:
            n2 = self.nodes[1]['name']
            condition = (f"{self.test_marker_column} LIKE "
                         f"'%{self.test_marker_prefix}_DISCARD%'")
            try:
                self.execute_on_node(n2, f"""
                    SELECT spock.add_exception_rule(
                        rule_name := 'auto_discard_rule',
                        relation := '{self.table_name}',
                        condition := '{condition}',
                        rule_action := 'DISCARD'
                    );
                """)
                self.log(f"[{n2}] Default DISCARD rule added", "SUCCESS")
            except Exception as e:
                self.log(f"[{n2}] Default DISCARD rule failed: {e}", "WARN")

        if len(self.nodes) >= 3:
            n3 = self.nodes[2]['name']
            condition = (f"{self.test_marker_column} LIKE "
                         f"'%{self.test_marker_prefix}_TRANSDISCARD%'")
            try:
                self.execute_on_node(n3, f"""
                    SELECT spock.add_exception_rule(
                        rule_name := 'auto_transdiscard_rule',
                        relation := '{self.table_name}',
                        condition := '{condition}',
                        rule_action := 'TRANSDISCARD'
                    );
                """)
                self.log(f"[{n3}] Default TRANSDISCARD rule added", "SUCCESS")
            except Exception as e:
                self.log(f"[{n3}] Default TRANSDISCARD rule failed: {e}", "WARN")

    def remove_discard_rules(self):
        self.log("Removing exception rules from all nodes...", "INFO")
        rule_names = ['auto_discard_rule', 'auto_transdiscard_rule']
        for rule in self.discard_rules:
            rule_names.append(rule['rule_name'])

        for node in self.nodes:
            for rule_name in rule_names:
                try:
                    self.execute_on_node(
                        node['name'],
                        f"SELECT spock.remove_exception_rule('{rule_name}');",
                    )
                except Exception:
                    pass

    # ------------------------------------------------- legacy data generation
    def _generate_value_for_column(self, column_info: tuple,
                                    is_marker: bool = False,
                                    marker_suffix: str = "") -> Any:
        col_name, data_type, max_len, nullable, default = column_info

        if col_name == self.test_marker_column and is_marker:
            value = (f"{self.test_marker_prefix}_{marker_suffix}_"
                     f"{random.randint(1000, 9999)}")
            if max_len and len(value) > max_len:
                value = value[:max_len]
            return value

        if default and ('nextval' in str(default)
                        or 'gen_random_uuid' in str(default)):
            return None

        if data_type in ('integer', 'bigint', 'smallint'):
            return random.randint(1, 1000000)
        elif data_type in ('numeric', 'decimal', 'real', 'double precision'):
            return round(random.uniform(1.0, 10000.0), 2)
        elif data_type in ('text', 'character varying', 'varchar',
                           'character', 'char'):
            length = min(max_len or 50, 50)
            return ''.join(random.choices(
                string.ascii_letters + string.digits, k=length))
        elif data_type == 'boolean':
            return random.choice([True, False])
        elif data_type in ('timestamp', 'timestamp without time zone',
                           'timestamp with time zone', 'timestamptz'):
            return datetime.now()
        elif data_type == 'date':
            return datetime.now().date()
        elif data_type in ('jsonb', 'json'):
            return json.dumps({'test': random.randint(1, 100)})
        elif data_type == 'uuid':
            return None
        else:
            return None

    def _build_insert_query(self, marker_type: str = "NORMAL") -> Tuple[str, tuple]:
        columns, values, placeholders = [], [], []
        is_marker_row = marker_type in ('DISCARD', 'TRANSDISCARD',
                                        'CONFLICT_SEED')

        for col_info in self.table_metadata['columns']:
            col_name = col_info[0]
            value = self._generate_value_for_column(
                col_info, is_marker=is_marker_row, marker_suffix=marker_type
            )
            if value is not None:
                columns.append(col_name)
                values.append(value)
                placeholders.append('%s')

        query = (f"INSERT INTO {self.table_name} ({', '.join(columns)}) "
                 f"VALUES ({', '.join(placeholders)})")
        return query, tuple(values)

    # ------------------------------------------------ legacy: validate test
    def test_validate_rules(self, num_rows: int = 100):
        self.log("=" * 75, "HEADER")
        self.log(f"LEGACY TEST: VALIDATION ({num_rows} rows)", "HEADER")
        self.log("=" * 75, "HEADER")

        source_node = self.nodes[0]['name']
        inserted = {'NORMAL': 0, 'DISCARD': 0, 'TRANSDISCARD': 0}

        baseline = {}
        for node in self.nodes:
            try:
                result = self.execute_on_node(
                    node['name'],
                    f"SELECT count(*) FROM {self.table_name} "
                    f"WHERE {self.test_marker_column} LIKE "
                    f"'{self.test_marker_prefix}%'",
                    fetch=True,
                )
                baseline[node['name']] = result[0][0] if result else 0
            except Exception as e:
                self.log(f"Baseline error on {node['name']}: {e}", "ERROR")
                baseline[node['name']] = 0

        self.log("Inserting test data with controlled distribution...", "INFO")

        try:
            conn = self.get_connection(source_node)
            conn.autocommit = True
            with conn.cursor() as cur:
                for i in range(num_rows):
                    rand = random.random()
                    if rand < 0.60:
                        marker_type = 'NORMAL'
                    elif rand < 0.85:
                        marker_type = 'DISCARD'
                    else:
                        marker_type = 'TRANSDISCARD'
                    query, params = self._build_insert_query(marker_type)
                    try:
                        cur.execute(query, params)
                        inserted[marker_type] += 1
                    except Exception as e:
                        if self.verbose:
                            self.log(f"Insert {i} failed: {e}", "WARN")
            conn.close()
        except Exception as e:
            self.log(f"Insert error: {e}", "ERROR")
            return

        total = sum(inserted.values())
        self.log(f"Inserted: {inserted['NORMAL']} normal, "
                 f"{inserted['DISCARD']} discard, "
                 f"{inserted['TRANSDISCARD']} transdiscard "
                 f"(total: {total})", "SUCCESS")

        self.log("Waiting 15s for replication...", "INFO")
        time.sleep(15)

        self.log("\n--- Validation Results ---", "HEADER")
        all_passed = True

        for node in self.nodes:
            node_name = node['name']
            try:
                total_result = self.execute_on_node(node_name, f"""
                    SELECT
                        count(*) AS total,
                        count(*) FILTER (WHERE {self.test_marker_column}
                            LIKE '%_NORMAL_%') AS normal,
                        count(*) FILTER (WHERE {self.test_marker_column}
                            LIKE '%_DISCARD_%') AS discarded,
                        count(*) FILTER (WHERE {self.test_marker_column}
                            LIKE '%_TRANSDISCARD_%') AS trans_discarded
                    FROM {self.table_name}
                    WHERE {self.test_marker_column} LIKE
                          '{self.test_marker_prefix}%'
                """, fetch=True)

                if total_result:
                    total_n, normal_n, disc_n, trans_n = total_result[0]

                    has_discard_rule = any(
                        r['node'] == node_name and r['action'] == 'DISCARD'
                        for r in self.discard_rules
                    )
                    has_transdiscard_rule = any(
                        r['node'] == node_name and r['action'] == 'TRANSDISCARD'
                        for r in self.discard_rules
                    )

                    if not self.discard_rules:
                        if node_name == self.nodes[1]['name']:
                            has_discard_rule = True
                        if (len(self.nodes) >= 3
                                and node_name == self.nodes[2]['name']):
                            has_transdiscard_rule = True

                    expected_normal = inserted['NORMAL']
                    expected_discard = (0 if has_discard_rule
                                        else inserted['DISCARD'])
                    expected_trans = (0 if has_transdiscard_rule
                                      else inserted['TRANSDISCARD'])
                    expected_total = (expected_normal + expected_discard
                                      + expected_trans)

                    rules_str = []
                    if has_discard_rule:
                        rules_str.append("DISCARD")
                    if has_transdiscard_rule:
                        rules_str.append("TRANSDISCARD")
                    rules_label = (f" [{','.join(rules_str)}]" if rules_str
                                   else " [no rules]")

                    is_correct = (total_n == expected_total)
                    status_icon = "✓" if is_correct else "✗"
                    color = "SUCCESS" if is_correct else "ERROR"
                    if not is_correct:
                        all_passed = False

                    self.log(f"{status_icon} [{node_name}]{rules_label}", color)
                    self.log(
                        f"    Total: {total_n}/{expected_total} | "
                        f"Normal: {normal_n}/{expected_normal} | "
                        f"Discard: {disc_n}/{expected_discard} | "
                        f"TransDisc: {trans_n}/{expected_trans}",
                        color,
                    )
            except Exception as e:
                self.log(f"✗ [{node_name}] Verification error: {e}", "ERROR")
                all_passed = False

        if all_passed:
            self.log("\n🎉 VALIDATION PASSED: All exception rules working "
                     "correctly!", "SUCCESS")
        else:
            self.log("\n⚠️  VALIDATION FAILED: Some exception rules not "
                     "working as expected", "ERROR")
        return all_passed

    # -------------------------------------------------- legacy: stress test
    def test_stress(self, duration_seconds: int = 60,
                    threads_per_node: int = 5):
        self.log("=" * 75, "HEADER")
        self.log(f"LEGACY TEST: STRESS ({duration_seconds}s, "
                 f"{threads_per_node} threads/node)", "HEADER")
        self.log("=" * 75, "HEADER")

        stop_event = threading.Event()
        worker_stats = {n['name']: {
            'inserts': 0, 'errors': 0, 'latencies': []
        } for n in self.nodes}
        stats_lock = threading.Lock()

        def stress_worker(node_name: str, worker_id: int):
            local_inserts, local_errors = 0, 0
            local_latencies = []
            try:
                conn = self.get_connection(node_name)
                conn.autocommit = True
                cur = conn.cursor()

                while not stop_event.is_set():
                    try:
                        rand = random.random()
                        if rand < 0.70:
                            marker_type = 'NORMAL'
                        elif rand < 0.90:
                            marker_type = 'DISCARD'
                        else:
                            marker_type = 'TRANSDISCARD'
                        query, params = self._build_insert_query(marker_type)
                        op_start = time.time()
                        cur.execute(query, params)
                        latency = (time.time() - op_start) * 1000
                        local_inserts += 1
                        local_latencies.append(latency)
                    except Exception as e:
                        local_errors += 1
                        if self.verbose and local_errors < 5:
                            self.log(f"Worker {node_name}-{worker_id} "
                                     f"error: {e}", "WARN")

                cur.close()
                conn.close()
            except Exception as e:
                self.log(f"Worker {node_name}-{worker_id} fatal: {e}", "ERROR")

            with stats_lock:
                worker_stats[node_name]['inserts'] += local_inserts
                worker_stats[node_name]['errors'] += local_errors
                worker_stats[node_name]['latencies'].extend(local_latencies)

        self.log("Capturing baseline...", "INFO")
        baseline = {}
        for node in self.nodes:
            try:
                result = self.execute_on_node(
                    node['name'],
                    f"SELECT count(*) FROM {self.table_name} "
                    f"WHERE {self.test_marker_column} LIKE "
                    f"'{self.test_marker_prefix}%'",
                    fetch=True,
                )
                baseline[node['name']] = result[0][0] if result else 0
            except Exception:
                baseline[node['name']] = 0

        total_threads = len(self.nodes) * threads_per_node
        self.log(f"Launching {total_threads} workers across "
                 f"{len(self.nodes)} nodes...", "INFO")

        threads = []
        start_time = time.time()
        for node in self.nodes:
            for i in range(threads_per_node):
                t = threading.Thread(target=stress_worker,
                                     args=(node['name'], i), daemon=True)
                t.start()
                threads.append(t)

        for remaining in range(duration_seconds, 0, -10):
            time.sleep(min(10, remaining))
            with stats_lock:
                total_ops = sum(s['inserts'] for s in worker_stats.values())
            elapsed = time.time() - start_time
            rate = total_ops / elapsed if elapsed > 0 else 0
            self.log(f"  Progress: {total_ops:,} ops, {rate:.0f} ops/s, "
                     f"{remaining}s remaining", "INFO")

        stop_event.set()
        for t in threads:
            t.join(timeout=15)

        actual_duration = time.time() - start_time

        self.log("\n--- Stress Test Results ---", "HEADER")
        total_inserts, total_errors = 0, 0
        all_latencies = []

        for node_name, stats in worker_stats.items():
            inserts = stats['inserts']
            errors = stats['errors']
            latencies = stats['latencies']
            total_inserts += inserts
            total_errors += errors
            all_latencies.extend(latencies)

            avg_lat = statistics.mean(latencies) if latencies else 0
            p99_lat = (sorted(latencies)[int(len(latencies) * 0.99)]
                       if len(latencies) > 100
                       else (max(latencies) if latencies else 0))

            self.log(
                f"  [{node_name}] {inserts:,} inserts, {errors} errors, "
                f"{inserts / actual_duration:.0f} ops/s, "
                f"avg: {avg_lat:.2f}ms, p99: {p99_lat:.2f}ms",
                "INFO",
            )

        self.log(f"\n  TOTAL: {total_inserts:,} ops in {actual_duration:.1f}s "
                 f"= {total_inserts / actual_duration:.0f} ops/s", "SUCCESS")
        self.log(f"  Total errors: {total_errors}",
                 "WARN" if total_errors else "SUCCESS")

        if all_latencies:
            sorted_lats = sorted(all_latencies)
            self.log(
                f"  Latency - avg: {statistics.mean(all_latencies):.2f}ms, "
                f"p50: {sorted_lats[len(sorted_lats) // 2]:.2f}ms, "
                f"p95: {sorted_lats[int(len(sorted_lats) * 0.95)]:.2f}ms, "
                f"p99: {sorted_lats[int(len(sorted_lats) * 0.99)]:.2f}ms",
                "INFO",
            )

        self.log("\nWaiting 30s for replication catchup...", "INFO")
        time.sleep(30)

        self.log("\n--- Replication Catchup Verification ---", "HEADER")
        for node in self.nodes:
            try:
                result = self.execute_on_node(node['name'], f"""
                    SELECT
                        count(*) - {baseline[node['name']]} AS new_total,
                        count(*) FILTER (WHERE {self.test_marker_column}
                            LIKE '%_NORMAL_%') AS normal_count,
                        count(*) FILTER (WHERE {self.test_marker_column}
                            LIKE '%_DISCARD_%') AS discard_count
                    FROM {self.table_name}
                    WHERE {self.test_marker_column} LIKE
                          '{self.test_marker_prefix}%'
                """, fetch=True)
                if result:
                    new_total, normal, discard = result[0]
                    self.log(f"  [{node['name']}] +{new_total} new rows, "
                             f"normal: {normal}, discard-marked: {discard}",
                             "INFO")
            except Exception as e:
                self.log(f"  [{node['name']}] Error: {e}", "ERROR")

    # ------------------------------------------------- legacy: conflict test
    def test_conflict_scenarios(self, num_conflicts: int = 50):
        self.log("=" * 75, "HEADER")
        self.log(f"LEGACY TEST: CONFLICTS ({num_conflicts} rows)", "HEADER")
        self.log("=" * 75, "HEADER")

        if not self.table_metadata['primary_keys']:
            self.log("Cannot run conflict test - table has no primary key",
                     "WARN")
            return

        pk_col = self.table_metadata['primary_keys'][0]
        source_node = self.nodes[0]['name']

        self.log("Inserting seed data for conflict test...", "INFO")
        seed_pks = []
        try:
            conn = self.get_connection(source_node)
            conn.autocommit = True
            with conn.cursor() as cur:
                for i in range(num_conflicts):
                    query, params = self._build_insert_query("CONFLICT_SEED")
                    query = query.rstrip() + f" RETURNING {pk_col}"
                    try:
                        cur.execute(query, params)
                        result = cur.fetchone()
                        if result:
                            seed_pks.append(result[0])
                    except Exception as e:
                        if self.verbose:
                            self.log(f"Seed insert {i} failed: {e}", "WARN")
            conn.close()
        except Exception as e:
            self.log(f"Seed setup error: {e}", "ERROR")
            return

        self.log(f"Inserted {len(seed_pks)} seed rows", "SUCCESS")
        time.sleep(10)

        self.log("Running concurrent updates from all nodes...", "INFO")

        update_stats = {n['name']: {'success': 0, 'failed': 0}
                        for n in self.nodes}
        stats_lock = threading.Lock()

        def conflict_worker(node_name: str, pks: List):
            local_success, local_failed = 0, 0
            try:
                conn = self.get_connection(node_name)
                conn.autocommit = True
                with conn.cursor() as cur:
                    for pk in pks:
                        try:
                            rand = random.random()
                            if rand < 0.7:
                                marker_value = (
                                    f"{self.test_marker_prefix}_CONFLICT_NORMAL_"
                                    f"{node_name}_{random.randint(1, 9999)}"
                                )
                            elif rand < 0.85:
                                marker_value = (
                                    f"{self.test_marker_prefix}_CONFLICT_DISCARD_"
                                    f"{node_name}_{random.randint(1, 9999)}"
                                )
                            else:
                                marker_value = (
                                    f"{self.test_marker_prefix}_CONFLICT_"
                                    f"TRANSDISCARD_{node_name}_"
                                    f"{random.randint(1, 9999)}"
                                )
                            cur.execute(
                                f"UPDATE {self.table_name} SET "
                                f"{self.test_marker_column} = %s "
                                f"WHERE {pk_col} = %s",
                                (marker_value, pk),
                            )
                            local_success += 1
                        except Exception:
                            local_failed += 1
                conn.close()
            except Exception as e:
                self.log(f"[{node_name}] conflict worker error: {e}", "ERROR")

            with stats_lock:
                update_stats[node_name]['success'] += local_success
                update_stats[node_name]['failed'] += local_failed

        threads = []
        for node in self.nodes:
            t = threading.Thread(target=conflict_worker,
                                 args=(node['name'], seed_pks), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=60)

        for node_name, stats in update_stats.items():
            self.log(f"  [{node_name}] {stats['success']} updates, "
                     f"{stats['failed']} failed", "INFO")

        self.log("Waiting 20s for conflict resolution...", "INFO")
        time.sleep(20)

        self.log("\n--- Conflict Resolution Results ---", "HEADER")
        for node in self.nodes:
            try:
                result = self.execute_on_node(node['name'], f"""
                    SELECT
                        count(*) FILTER (WHERE {self.test_marker_column}
                            LIKE '%_CONFLICT_NORMAL_%') AS normal,
                        count(*) FILTER (WHERE {self.test_marker_column}
                            LIKE '%_CONFLICT_DISCARD_%') AS discard,
                        count(*) FILTER (WHERE {self.test_marker_column}
                            LIKE '%_CONFLICT_TRANSDISCARD_%') AS trans,
                        count(*) FILTER (WHERE {self.test_marker_column}
                            LIKE '%_CONFLICT_SEED_%') AS unchanged
                    FROM {self.table_name}
                    WHERE {self.test_marker_column} LIKE
                          '{self.test_marker_prefix}_CONFLICT%'
                """, fetch=True)
                if result:
                    normal, discard, trans, unchanged = result[0]
                    self.log(f"  [{node['name']}] Normal: {normal}, "
                             f"Discard: {discard}, TransDisc: {trans}, "
                             f"Unchanged: {unchanged}", "INFO")
            except Exception as e:
                self.log(f"  [{node['name']}] Error: {e}", "ERROR")

        self.log("\n--- Exception/Conflict Log ---", "HEADER")
        for node in self.nodes:
            try:
                result = self.execute_on_node(node['name'], """
                    SELECT count(*) FROM spock.exception_log
                    WHERE remote_commit_ts > now() - interval '5 minutes'
                """, fetch=True)
                count = result[0][0] if result else 0
                self.log(f"  [{node['name']}] Exception entries (last 5min): "
                         f"{count}",
                         "WARN" if count > 0 else "INFO")
            except Exception as e:
                self.log(f"  [{node['name']}] Could not check log: {e}", "WARN")

    # ------------------------------------------------------- legacy: cleanup
    def cleanup_test_data(self):
        self.log("=" * 75, "HEADER")
        self.log("CLEANING UP LEGACY TEST DATA", "HEADER")
        self.log("=" * 75, "HEADER")

        source_node = self.nodes[0]['name']

        if not self.test_marker_column:
            self.log("No marker column known - cannot clean up safely", "ERROR")
            return

        try:
            result = self.execute_on_node(source_node, f"""
                DELETE FROM {self.table_name}
                WHERE {self.test_marker_column} LIKE
                      '{self.test_marker_prefix}%'
                RETURNING 1;
            """, fetch=True)

            deleted = len(result) if result else 0
            self.log(f"Deleted {deleted} test rows from {self.table_name}",
                     "SUCCESS")

            time.sleep(5)
            for node in self.nodes:
                try:
                    result = self.execute_on_node(
                        node['name'],
                        f"SELECT count(*) FROM {self.table_name} "
                        f"WHERE {self.test_marker_column} LIKE "
                        f"'{self.test_marker_prefix}%'",
                        fetch=True,
                    )
                    remaining = result[0][0] if result else 0
                    self.log(f"  [{node['name']}] Remaining test rows: "
                             f"{remaining}",
                             "SUCCESS" if remaining == 0 else "WARN")
                except Exception as e:
                    self.log(f"  [{node['name']}] Verify error: {e}", "ERROR")

        except Exception as e:
            self.log(f"Cleanup error: {e}", "ERROR")

    # ====================================================================
    # COMBINED RUNNERS
    # ====================================================================
    def run_all_tests(self, num_rows: int, num_threads: int,
                      duration_seconds: int, num_conflicts: int):
        self.log("=" * 75, "HEADER")
        self.log("SPOCK DISCARD/TRANSDISCARD COMPREHENSIVE TEST SUITE",
                 "HEADER")
        self.log("=" * 75, "HEADER")
        self.log(f"Target table: {self.table_name}", "INFO")
        self.log(f"Test marker prefix: {self.test_marker_prefix}", "INFO")
        self.log(f"Nodes: {[n['name'] for n in self.nodes]}", "INFO")

        try:
            self.discover_table_structure()
            self.setup_discard_rules()

            self.test_validate_rules(num_rows=num_rows // 10)
            time.sleep(5)
            self.test_conflict_scenarios(num_conflicts=num_conflicts)
            time.sleep(5)
            self.test_stress(duration_seconds=duration_seconds,
                             threads_per_node=num_threads)

            self.log("\n" + "=" * 75, "HEADER")
            self.log("ALL TESTS COMPLETED", "HEADER")
            self.log("=" * 75, "HEADER")
            self.log("To remove test data: rerun with --cleanup", "INFO")

        except KeyboardInterrupt:
            self.log("\nInterrupted by user", "WARN")
        except Exception as e:
            self.log(f"Test suite failed: {e}", "ERROR")
            import traceback
            traceback.print_exc()

    def run_doc_tests(self, wait_seconds: int = 10):
        """Run both doc-aligned tests in sequence and summarise."""
        self.log("=" * 75, "HEADER")
        self.log("DOC-ALIGNED EXCEPTION BEHAVIOUR TESTS", "HEADER")
        self.log("=" * 75, "HEADER")
        self.log(f"Provider: {self.nodes[0]['name']}", "INFO")
        self.log(f"Subscriber: {self.nodes[1]['name']}"
                 if len(self.nodes) >= 2 else "Subscriber: <missing>",
                 "INFO")

        d_ok = self.test_discard_mode(wait_seconds=wait_seconds)
        time.sleep(2)
        td_ok = self.test_transdiscard_mode(wait_seconds=wait_seconds)

        self.log("\n" + "=" * 75, "HEADER")
        self.log("DOC TEST SUMMARY", "HEADER")
        self.log("=" * 75, "HEADER")
        self.log(f"  DISCARD mode      : {'PASS' if d_ok else 'FAIL'}",
                 "SUCCESS" if d_ok else "ERROR")
        self.log(f"  TRANSDISCARD mode : {'PASS' if td_ok else 'FAIL'}",
                 "SUCCESS" if td_ok else "ERROR")
        return d_ok and td_ok


# ===========================================================================
# CLI
# ===========================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Spock TRANSDISCARD/DISCARD test suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Doc-aligned DISCARD test (auto-creates test_discard table)
  python test_spock_discard.py --config nodes.json --test discard_mode

  # Doc-aligned TRANSDISCARD test (auto-creates test_transdiscard table)
  python test_spock_discard.py --config nodes.json --test transdiscard_mode

  # Both doc tests in sequence (single transaction each)
  python test_spock_discard.py --config nodes.json --test doc

  # LOAD versions: many concurrent provider txns, mix of clean + conflicting
  python test_spock_discard.py --config nodes.json --test discard_load --duration 60 --threads 8 --conflict-ratio 0.3
  python test_spock_discard.py --config nodes.json --test transdiscard_load --duration 60 --threads 8 --conflict-ratio 0.3
  python test_spock_discard.py --config nodes.json --test doc_load --duration 60 --threads 8 --conflict-ratio 0.3

  # Legacy modes (require --table)
  python test_spock_discard.py --config nodes.json --table public.customers --test all
  python test_spock_discard.py --config nodes.json --table public.customers --test validate --rows 200
  python test_spock_discard.py --config nodes.json --table public.customers --test stress --duration 300 --threads 10
  python test_spock_discard.py --config nodes.json --table public.customers --test conflict --conflicts 100

  # Cleanup
  python test_spock_discard.py --config nodes.json --table public.customers --cleanup
  python test_spock_discard.py --config nodes.json --test discard_mode --cleanup
  python test_spock_discard.py --config nodes.json --test transdiscard_mode --cleanup
  python test_spock_discard.py --config nodes.json --test doc_load --cleanup
""",
    )

    parser.add_argument('--config', required=True,
                        help='JSON config file with nodes')
    parser.add_argument('--table',
                        help='Existing table for legacy tests '
                             '(schema.table format). Not needed for '
                             'discard_mode/transdiscard_mode/doc.')
    parser.add_argument(
        '--test',
        choices=['all', 'validate', 'stress', 'conflict',
                 'discard_mode', 'transdiscard_mode', 'doc',
                 'discard_load', 'transdiscard_load', 'doc_load'],
        default='doc',
        help='Which test to run (default: doc). '
             '*_load variants are concurrent, duration-based.',
    )
    parser.add_argument('--rows', type=int, default=1000,
                        help='Number of rows for validation/general tests')
    parser.add_argument('--threads', type=int, default=5,
                        help='Threads per node (legacy stress) / '
                             'concurrent provider workers (load tests). '
                             'Default for load tests effectively becomes 8 '
                             'if you stick with the default 5; pass --threads '
                             'explicitly to override.')
    parser.add_argument('--duration', type=int, default=60,
                        help='Duration in seconds for stress/load tests')
    parser.add_argument('--conflicts', type=int, default=50,
                        help='Number of conflict scenarios (legacy)')
    parser.add_argument('--conflict-ratio', type=float, default=0.3,
                        help='Fraction of load-test transactions that should '
                             'include a conflicting row (0.0-1.0, '
                             'default 0.3)')
    parser.add_argument('--wait', type=int, default=10,
                        help='Seconds to wait for replication in doc tests '
                             '(default 10; load tests use max(15, --wait))')
    parser.add_argument('--cleanup', action='store_true',
                        help='Remove test data and rules')
    parser.add_argument('--marker-column',
                        help='Override auto-detected marker column '
                             '(legacy modes)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Verbose output')

    args = parser.parse_args()

    legacy_modes = {'all', 'validate', 'stress', 'conflict'}
    doc_modes = {'discard_mode', 'transdiscard_mode', 'doc',
                 'discard_load', 'transdiscard_load', 'doc_load'}
    load_modes = {'discard_load', 'transdiscard_load', 'doc_load'}

    if args.test in legacy_modes and not args.table and not args.cleanup:
        parser.error(f"--table is required for --test {args.test}")

    if not (0.0 <= args.conflict_ratio <= 1.0):
        parser.error("--conflict-ratio must be between 0.0 and 1.0")

    tester = SpockDiscardTester(args.config, args.table, verbose=args.verbose)

    if args.marker_column:
        tester.test_marker_column = args.marker_column

    try:
        # ---- cleanup paths ----
        if args.cleanup:
            if args.test in doc_modes:
                if args.test in ('discard_mode', 'doc',
                                 'discard_load', 'doc_load'):
                    tester._doc_drop_tables(SpockDiscardTester.DOC_TABLE_DISCARD)
                if args.test in ('transdiscard_mode', 'doc',
                                 'transdiscard_load', 'doc_load'):
                    tester._doc_drop_tables(
                        SpockDiscardTester.DOC_TABLE_TRANSDISCARD)
                tester.log("Doc-test cleanup complete.", "SUCCESS")
                return
            else:
                if not args.table:
                    parser.error("--table is required for legacy --cleanup")
                tester.discover_table_structure()
                tester.remove_discard_rules()
                tester.cleanup_test_data()
                return

        # ---- doc-aligned single-shot tests ----
        if args.test == 'discard_mode':
            tester.test_discard_mode(wait_seconds=args.wait)
            return
        if args.test == 'transdiscard_mode':
            tester.test_transdiscard_mode(wait_seconds=args.wait)
            return
        if args.test == 'doc':
            tester.run_doc_tests(wait_seconds=args.wait)
            return

        # ---- doc-aligned LOAD tests ----
        if args.test in load_modes:
            wait_s = max(15, args.wait)
            if args.test == 'discard_load':
                tester.test_discard_mode_load(
                    duration=args.duration, threads=args.threads,
                    conflict_ratio=args.conflict_ratio,
                    wait_seconds=wait_s,
                )
            elif args.test == 'transdiscard_load':
                tester.test_transdiscard_mode_load(
                    duration=args.duration, threads=args.threads,
                    conflict_ratio=args.conflict_ratio,
                    wait_seconds=wait_s,
                )
            elif args.test == 'doc_load':
                tester.run_doc_load_tests(
                    duration=args.duration, threads=args.threads,
                    conflict_ratio=args.conflict_ratio,
                    wait_seconds=wait_s,
                )
            return

        # ---- legacy tests (need table discovery) ----
        tester.discover_table_structure()

        if args.test == 'all':
            tester.run_all_tests(
                num_rows=args.rows,
                num_threads=args.threads,
                duration_seconds=args.duration,
                num_conflicts=args.conflicts,
            )
        elif args.test == 'validate':
            tester.setup_discard_rules()
            tester.test_validate_rules(num_rows=args.rows)
        elif args.test == 'stress':
            tester.setup_discard_rules()
            tester.test_stress(duration_seconds=args.duration,
                               threads_per_node=args.threads)
        elif args.test == 'conflict':
            tester.setup_discard_rules()
            tester.test_conflict_scenarios(num_conflicts=args.conflicts)

    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Interrupted by user{Colors.END}")
    except Exception as e:
        print(f"{Colors.RED}Error: {e}{Colors.END}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
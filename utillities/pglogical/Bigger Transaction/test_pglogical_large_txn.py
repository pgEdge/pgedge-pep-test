#!/usr/bin/env python3
"""
pglogical Large-Transaction Stress Test (interactive)
======================================================

Drives a single large transaction on n1 and verifies n2 converges, with
full instrumentation and optional chaos injection.

Uses pglogical extension for logical replication between two nodes.
Table setup follows the pglogical workflow:
  1. CREATE TABLE manually on BOTH nodes (pglogical does NOT replicate DDL)
  2. pglogical.replication_set_add_table('default', 'public.table')
     on BOTH nodes to register it in the default replication set
  3. pglogical.alter_subscription_resynchronize_table(sub, 'public.table')
     on each subscriber to kick off table synchronization

A "large transaction" here means MANY rows committed in ONE BEGIN/COMMIT
block. This stresses pglogical's reorder buffer, the change-record
stream, and the apply worker's throughput.

Example shapes you can test:
    2.5 GiB txn:  50,000,000 rows × 50 B
    5 GiB txn :  10,000,000 rows × 500 B
    10 GiB txn:    100,000 rows × 100 KiB

Interactive flow at startup:
  1. "How many rows in the transaction?"  (with suggestions)
  2. "How big should each row be?"
  3. Plan summary + confirmation
  4. Run, monitor, verify convergence
  5. Ask whether to drop the test table

Validation (--expect):
  success : commit on n1 + convergence on n2 + clean apply worker
  failure : test PASSES if producer errored OR n2 didn't converge

Chaos hooks (--chaos, comma-separated):
  fill-slot          : (pre-commit) disable n2 sub before producer
                       commits so the slot fills; then re-enable
  restart-subscriber : alter_subscription_disable + enable mid-replay
  kill-apply-worker  : pg_terminate_backend on pglogical workers
  pause              : sleep N seconds mid-replay
                       (specified via --chaos-pause-sec)

Metrics every --monitor-interval seconds; JSONL via --metrics-out.

Non-interactive override (for CI / scripting):
  --non-interactive   requires --rows and --row-bytes
    Example:  --rows 50m --row-bytes 50

Cleanup behaviour:
  After the run, the script asks whether to drop the test table.
  Pressing enter accepts; answer 'n' to keep the table for inspection.
    --auto-cleanup    drop without asking (implied by --non-interactive)
    --no-cleanup      keep the table without asking

Config (nodes2.json):
{
    "nodes": [
        {"name": "n1", "host": "...", "port": 5432, "dbname": "z1",
         "user": "postgres", "password": "..."},
        {"name": "n2", "host": "...", "port": 5433, "dbname": "z1",
         "user": "postgres", "password": "..."}
    ],
    "subscriptions": {
        "n1": "sub_from_n2",
        "n2": "sub_from_n1"
    }
}

  subscriptions: maps each node name to the pglogical subscription
  that node holds (i.e. the subscription that receives data FROM the
  other node). Used for alter_subscription_resynchronize_table.

Usage:
  python test_pglogical_large_txn.py --config nodes2.json
  python test_pglogical_large_txn.py --config nodes2.json --non-interactive \\
      --rows 50m --row-bytes 50
  python test_pglogical_large_txn.py --config nodes2.json --cleanup
"""

import psycopg2
import psycopg2.extras
import json
import argparse
import time
import sys
import io
import threading
import hashlib
import os
import re
import random
import struct
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple


# ============================================================ ANSI colours
class C:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    GRAY = '\033[90m'
    BOLD = '\033[1m'
    END = '\033[0m'


TABLE = "pglogical_largetxn_test"
DEFAULT_REPSET = "default"
VALID_CHAOS = {'restart-subscriber', 'kill-apply-worker',
               'fill-slot', 'pause'}


# ============================================================================
# Size parsing
# ============================================================================
SIZE_SUFFIXES = {
    '': 1, 'B': 1,
    'K': 1024, 'KB': 1024, 'KIB': 1024,
    'M': 1024**2, 'MB': 1024**2, 'MIB': 1024**2,
    'G': 1024**3, 'GB': 1024**3, 'GIB': 1024**3,
    'T': 1024**4, 'TB': 1024**4, 'TIB': 1024**4,
}


def parse_size(s: str) -> int:
    """Parse '2.5GB', '500MB', '10G', '1024' -> bytes."""
    if s is None or not str(s).strip():
        raise ValueError("empty size")
    s = str(s).strip().upper()
    m = re.match(r'^([0-9]+(?:\.[0-9]+)?)\s*([A-Z]*)$', s)
    if not m:
        raise ValueError(f"cannot parse size: {s!r}")
    num = float(m.group(1))
    suf = m.group(2)
    if suf not in SIZE_SUFFIXES:
        raise ValueError(
            f"unknown suffix {suf!r} in {s!r}; "
            f"valid: K, M, G, T (plus B/KB/MB/GB/TB/KiB/...)")
    return int(num * SIZE_SUFFIXES[suf])


def fmt_bytes(n: int) -> str:
    if n is None:
        return "n/a"
    for unit, sz in (('TiB', 1024**4), ('GiB', 1024**3),
                      ('MiB', 1024**2), ('KiB', 1024)):
        if n >= sz:
            return f"{n/sz:.2f} {unit}"
    return f"{n} B"


# ============================================================================
# Interactive prompts
# ============================================================================
SIZE_PRESETS = [
    ("2.5GB",  parse_size("2.5GB")),
    ("5GB",    parse_size("5GB")),
    ("10GB",   parse_size("10GB")),
    ("25GB",   parse_size("25GB")),
]


def prompt_for_row_count(target_bytes: int, max_bytes_per_row: int,
                          default_count: int = 1_000_000) -> int:
    """For multi-row shape: ask the user how many rows the single
    transaction should contain. The row payload size is then derived
    as `target_bytes / count`. Default offered = whichever round number
    near 1M rows fits.

    `max_bytes_per_row` is the largest sensible per-row payload —
    typically the target size divided by some sane minimum row count.
    """
    print(f"\n{C.HEADER}{C.BOLD}=== Row count ==={C.END}")
    print(f"  How many rows should the transaction contain?")
    print(f"  Target total : {fmt_bytes(target_bytes)}")
    print(f"  Examples     : 1000, 100000, 1m, 10m, 50m")
    print(f"  (suffixes: k=thousand, m=million)")
    # offer some computed presets
    presets = []
    for n in (1_000, 100_000, 1_000_000, 10_000_000, 50_000_000):
        per_row = max(1, target_bytes // n)
        if per_row > 10 * 1024 * 1024:
            continue  # >10 MiB per row is silly for multi-row mode
        if per_row < 8:
            continue  # too small to be meaningful
        presets.append((n, per_row))
    if presets:
        print(f"  Sensible options for {fmt_bytes(target_bytes)}:")
        for n, pr in presets:
            print(f"    {n:>12,} rows → {fmt_bytes(pr)} per row")
    while True:
        try:
            raw = input(f"{C.BLUE}Row count "
                        f"[{default_count:,}]: {C.END}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C.YELLOW}Cancelled{C.END}")
            sys.exit(130)
        if not raw:
            return default_count
        # parse with k/m suffixes
        try:
            mult = 1
            if raw.endswith('k'):
                mult = 1_000
                raw = raw[:-1]
            elif raw.endswith('m'):
                mult = 1_000_000
                raw = raw[:-1]
            n = int(float(raw) * mult)
            if n <= 0:
                print(f"{C.RED}row count must be > 0{C.END}")
                continue
            return n
        except ValueError:
            print(f"{C.RED}Not a number; use plain digits or "
                  f"suffixes k/m{C.END}")


def prompt_for_size(default_bytes: Optional[int] = None) -> int:
    """Always prompt. If a default is supplied (e.g. from --size CLI),
    it pre-selects the matching preset or 'custom' with that value."""
    print(f"\n{C.HEADER}{C.BOLD}=== Target transaction size ==={C.END}")
    for i, (label, n) in enumerate(SIZE_PRESETS, 1):
        marker = (" (default)"
                  if default_bytes is not None and n == default_bytes
                  else "")
        print(f"  {i}. {label:6s}  ({fmt_bytes(n)}){marker}")
    custom_idx = len(SIZE_PRESETS) + 1
    if (default_bytes is not None
            and default_bytes not in [n for _, n in SIZE_PRESETS]):
        print(f"  {custom_idx}. custom  (default: "
              f"{fmt_bytes(default_bytes)})")
    else:
        print(f"  {custom_idx}. custom  (e.g. 7.5GB, 500MB, 100G)")
    while True:
        try:
            prompt = f"{C.BLUE}Selection [1-{custom_idx}]"
            if default_bytes is not None:
                # default to whichever preset matches the CLI value,
                # otherwise default to custom
                default_choice = custom_idx
                for i, (_, n) in enumerate(SIZE_PRESETS, 1):
                    if n == default_bytes:
                        default_choice = i
                        break
                prompt += f" (default {default_choice})"
            prompt += f": {C.END}"
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C.YELLOW}Cancelled{C.END}")
            sys.exit(130)
        if not raw and default_bytes is not None:
            return default_bytes
        if not raw:
            continue
        try:
            choice = int(raw)
        except ValueError:
            print(f"{C.RED}Not a number{C.END}")
            continue
        if 1 <= choice <= len(SIZE_PRESETS):
            return SIZE_PRESETS[choice - 1][1]
        if choice == custom_idx:
            default_label = (fmt_bytes(default_bytes)
                              if default_bytes is not None else "")
            while True:
                try:
                    suffix = (f" [{default_label}]"
                              if default_label else "")
                    custom = input(f"{C.BLUE}  custom size"
                                    f"{suffix}: {C.END}").strip()
                except (EOFError, KeyboardInterrupt):
                    print(f"\n{C.YELLOW}Cancelled{C.END}")
                    sys.exit(130)
                if not custom and default_bytes is not None:
                    return default_bytes
                try:
                    return parse_size(custom)
                except ValueError as e:
                    print(f"{C.RED}{e}{C.END}")
        else:
            print(f"{C.RED}Out of range{C.END}")


def prompt_for_row_size(default_bytes: int) -> int:
    """Ask the user for the per-row payload size."""
    print(f"\n{C.HEADER}{C.BOLD}=== Row size ==={C.END}")
    print(f"  Bytes per row in the transaction.")
    print(f"  Examples:  '50', '500B', '1KB', '4KiB', '1MB', '1MiB'")
    print(f"  Default :  {fmt_bytes(default_bytes)}")
    while True:
        try:
            raw = input(f"{C.BLUE}Row size "
                        f"[{fmt_bytes(default_bytes)}]: {C.END}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C.YELLOW}Cancelled{C.END}")
            sys.exit(130)
        if not raw:
            return default_bytes
        try:
            val = parse_size(raw)
            if val <= 0:
                print(f"{C.RED}row size must be > 0{C.END}")
                continue
            return val
        except ValueError as e:
            print(f"{C.RED}{e}{C.END}")


def confirm_plan(plan: 'PhasePlan') -> bool:
    print(f"\n{C.HEADER}{C.BOLD}=== Planned workload ==={C.END}")
    for line in plan.describe():
        print(f"  {line}")
    while True:
        try:
            raw = input(f"\n{C.BLUE}Proceed? [Y/n]: {C.END}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{C.YELLOW}Cancelled{C.END}")
            return False
        if raw in ('', 'y', 'yes'):
            return True
        if raw in ('n', 'no'):
            return False
        print(f"{C.RED}Please answer y or n{C.END}")


def prompt_cleanup(context: str = "") -> bool:
    """Ask whether to drop the test table from both nodes. Default is
    yes — pressing enter accepts cleanup. Ctrl-C or 'n' keeps the data
    on both nodes (useful for post-mortem inspection of a failed run).

    `context` is an optional one-line description shown above the prompt
    (e.g. "Test passed" or "Test failed — keep table for investigation?").
    """
    print()
    print(f"{C.HEADER}{C.BOLD}=== Cleanup ==={C.END}")
    if context:
        print(f"  {context}")
    print(f"  This will DROP TABLE {TABLE} on BOTH nodes (n1 and n2).")
    print(f"  Answer 'n' to keep the test table for inspection.")
    while True:
        try:
            raw = input(f"{C.BLUE}Drop test table now? [Y/n]: "
                        f"{C.END}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # treat ctrl-c at this prompt as "don't clean up"
            print(f"\n{C.YELLOW}Skipping cleanup (interrupted){C.END}")
            return False
        if raw in ('', 'y', 'yes'):
            return True
        if raw in ('n', 'no'):
            return False
        print(f"{C.RED}Please answer y or n{C.END}")


# ============================================================================
# Phase planner — multi-row transactions only
# ============================================================================
class PhasePlan:
    """Plans a single multi-row transaction.

    Internally uses the bulk (COPY) producer for speed.
    """

    def __init__(self, row_count: int, row_bytes: int):
        if row_count < 1:
            raise ValueError("row_count must be >= 1")
        if row_bytes < 1:
            raise ValueError("row_bytes must be >= 1")
        self.row_count = row_count
        self.row_bytes = row_bytes
        # Keep a single 'phase' for the runner to dispatch on
        self.phases: List[Dict] = [{
            'name': 'multi',
            'producer': 'bulk',
            'rows': row_count,
            'row_size': row_bytes,
            'actual_bytes': row_count * row_bytes,
        }]

    def total_rows(self) -> int:
        return self.row_count

    def total_actual_bytes(self) -> int:
        return self.row_count * self.row_bytes

    def describe(self) -> List[str]:
        lines = [
            f"transaction size : {fmt_bytes(self.total_actual_bytes())}",
            f"row count        : {self.row_count:,}",
            f"row size         : {fmt_bytes(self.row_bytes)}",
        ]
        warnings = []
        if self.row_count < 100:
            warnings.append(
                f"only {self.row_count} rows — increase row count to "
                f"actually stress the reorder buffer")
        if self.row_bytes > 64 * 1024:
            warnings.append(
                f"row size is {fmt_bytes(self.row_bytes)} — that's wide "
                f"per-row; multi-row mode wants smaller rows")
        if warnings:
            lines.append("")
            lines.append("WARNINGS:")
            for w in warnings:
                lines.append(f"  ⚠ {w}")
        return lines


# ============================================================================
# Replication monitor
# ============================================================================
class ReplicationMonitor(threading.Thread):

    def __init__(self, parent: 'LargeTxnTester',
                 interval_sec: float, out_path: Optional[str]):
        super().__init__(daemon=True)
        self.parent = parent
        self.interval = interval_sec
        self.out_path = out_path
        self._stopflag = threading.Event()
        self.samples: List[Dict] = []
        self._fh = None
        self._lock = threading.Lock()

    def run(self):
        if self.out_path:
            try:
                self._fh = open(self.out_path, 'a', buffering=1)
            except Exception as e:
                self.parent._log(
                    f"metrics file open failed: {e}", "WARN")
        try:
            while not self._stopflag.is_set():
                s = self._capture()
                with self._lock:
                    self.samples.append(s)
                if self._fh:
                    try:
                        self._fh.write(
                            json.dumps(s, default=str) + '\n')
                    except Exception:
                        pass
                self._stopflag.wait(self.interval)
        finally:
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass

    def stop(self):
        self._stopflag.set()
        self.join(timeout=10)

    def _capture(self) -> Dict:
        s: Dict[str, Any] = {'ts': time.time(),
                              'iso': datetime.now(timezone.utc)
                                              .isoformat()}
        try:
            with self.parent._conn(self.parent.n1) as c, c.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute("""
                    SELECT slot_name, active, restart_lsn,
                        confirmed_flush_lsn,
                        pg_wal_lsn_diff(pg_current_wal_lsn(),
                                        confirmed_flush_lsn)
                            AS lag_bytes,
                        pg_wal_lsn_diff(pg_current_wal_lsn(),
                                        restart_lsn)
                            AS retained_bytes
                    FROM pg_replication_slots
                    WHERE slot_type='logical';
                """)
                s['n1_slots'] = [dict(r) for r in cur.fetchall()]
                cur.execute("SELECT pg_current_wal_lsn() AS lsn;")
                s['n1_wal_lsn'] = cur.fetchone()['lsn']
                cur.execute("""
                    SELECT pid, application_name, state, sync_state,
                           pg_wal_lsn_diff(sent_lsn, write_lsn)
                              AS sent_minus_write,
                           pg_wal_lsn_diff(sent_lsn, flush_lsn)
                              AS sent_minus_flush,
                           pg_wal_lsn_diff(sent_lsn, replay_lsn)
                              AS sent_minus_replay
                    FROM pg_stat_replication;
                """)
                s['n1_repl'] = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            s['n1_error'] = str(e).split('\n')[0]
        try:
            with self.parent._conn(self.parent.n2) as c, c.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                try:
                    cur.execute(
                        "SELECT * FROM pglogical.show_subscription_status();")
                    s['n2_subs'] = [dict(r) for r in cur.fetchall()]
                except Exception:
                    s['n2_subs'] = []
                cur.execute("""
                    SELECT pid, application_name, backend_type, state,
                           wait_event_type, wait_event,
                           backend_start, query_start
                    FROM pg_stat_activity
                    WHERE (backend_type='background worker'
                           OR backend_type='logical replication worker')
                      AND application_name ~* 'pglogical';
                """)
                s['n2_workers'] = [dict(r) for r in cur.fetchall()]
                try:
                    cur.execute(
                        f"SELECT count(*) AS n FROM {TABLE};")
                    s['n2_rowcount'] = cur.fetchone()['n']
                    cur.execute(
                        f"SELECT pg_total_relation_size(%s) AS bytes;",
                        (TABLE,))
                    s['n2_table_bytes'] = cur.fetchone()['bytes']
                except Exception:
                    s['n2_rowcount'] = None
                    s['n2_table_bytes'] = None
                    # discover pg_stat_wal_receiver columns and query them
                    try:
                        # PostgreSQL versions expose different
                        # columns in pg_stat_wal_receiver.
                        #
                        # Older environments may NOT have:
                        #   received_lsn
                        #
                        # So we dynamically discover available columns
                        # and only query the ones that exist.

                        cur.execute("""
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema = 'pg_catalog'
                              AND table_name = 'pg_stat_wal_receiver';
                        """)

                        available_cols = {
                            r['column_name']
                            for r in cur.fetchall()
                        }

                        preferred_cols = [
                            'pid',
                            'status',
                            'received_lsn',
                            'written_lsn',
                            'flushed_lsn',
                            'latest_end_lsn',
                            'latest_end_time'
                        ]

                        selected_cols = [
                            c for c in preferred_cols
                            if c in available_cols
                        ]

                        if selected_cols:
                            query = f"""
                                SELECT {', '.join(selected_cols)}
                                FROM pg_stat_wal_receiver;
                            """

                            cur.execute(query)

                            s['n2_wal_recv'] = [
                                dict(r) for r in cur.fetchall()
                            ]
                        else:
                            s['n2_wal_recv'] = []

                    except Exception as e:
                        s['n2_wal_recv_error'] = str(e).split('\n')[0]
                        s['n2_wal_recv'] = []
        except Exception as e:
            s['n2_error'] = str(e).split('\n')[0]
        return s


# ============================================================================
# Chaos engine
# ============================================================================
class ChaosEngine:
    def __init__(self, parent: 'LargeTxnTester', actions: List[str],
                 pause_seconds: int):
        self.parent = parent
        self.actions = actions
        self.pause_seconds = pause_seconds
        self.events: List[Dict] = []
        self._thread: Optional[threading.Thread] = None
        self._stopflag = threading.Event()
        self.completed = threading.Event()

    def start_after_commit(self):
        if not self.actions:
            self.completed.set()
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stopflag.set()
        if self._thread:
            self._thread.join(timeout=30)

    def _record(self, action: str, ok: bool, detail: str):
        ev = {'ts': time.time(), 'action': action,
              'ok': ok, 'detail': detail}
        self.events.append(ev)
        self.parent._log(
            f"CHAOS {action}: {'OK' if ok else 'FAIL'} — {detail}",
            "OK" if ok else "ERROR")

    def _run(self):
        time.sleep(2)
        for action in self.actions:
            if self._stopflag.is_set():
                break
            try:
                if action == 'restart-subscriber':
                    self._restart_subscriber()
                elif action == 'kill-apply-worker':
                    self._kill_apply_worker()
                elif action == 'pause':
                    self._pause(self.pause_seconds)
            except Exception as e:
                self._record(action, False, str(e).split('\n')[0])
            time.sleep(2)
        self.completed.set()

    def _restart_subscriber(self):
        try:
            subs = [s['name'] for s in self.parent._get_subs(self.parent.n2)
                    if s.get('name')]
        except Exception:
            subs = []
        if not subs:
            self._record('restart-subscriber', False,
                         'no subscriptions on n2')
            return
        with self.parent._conn(self.parent.n2) as c, c.cursor() as cur:
            for sn in subs:
                cur.execute(
                    "SELECT pglogical.alter_subscription_disable(%s, "
                    "immediate := true);",
                    (sn,))
        time.sleep(3)
        with self.parent._conn(self.parent.n2) as c, c.cursor() as cur:
            for sn in subs:
                cur.execute(
                    "SELECT pglogical.alter_subscription_enable(%s);",
                    (sn,))
        self._record('restart-subscriber', True,
                     f"disabled+enabled: {subs}")

    def _kill_apply_worker(self):
        with self.parent._conn(self.parent.n2) as c, c.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT pid, application_name FROM pg_stat_activity
                WHERE (backend_type='background worker'
                       OR backend_type='logical replication worker')
                  AND application_name ~* 'pglogical'
                  AND application_name ~* 'apply';
            """)
            workers = cur.fetchall()
            if not workers:
                cur.execute("""
                    SELECT pid, application_name FROM pg_stat_activity
                    WHERE (backend_type='background worker'
                           OR backend_type='logical replication worker')
                      AND application_name ~* 'pglogical';
                """)
                workers = cur.fetchall()
            if not workers:
                self._record('kill-apply-worker', False,
                             'no pglogical workers visible')
                return
            killed = []
            for w in workers:
                try:
                    cur.execute(
                        "SELECT pg_terminate_backend(%s);", (w['pid'],))
                    killed.append((w['pid'], w['application_name']))
                except Exception:
                    pass
            self._record('kill-apply-worker', True,
                         f"terminated: {killed}")
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(2)
            with self.parent._conn(self.parent.n2) as c, c.cursor() as cur:
                cur.execute("""
                    SELECT count(*) FROM pg_stat_activity
                    WHERE (backend_type='background worker'
                           OR backend_type='logical replication worker')
                      AND application_name ~* 'pglogical';
                """)
                if cur.fetchone()[0] > 0:
                    self.parent._log("apply worker reappeared", "OK")
                    return
        self.parent._log(
            "apply worker did NOT reappear within 30s", "WARN")

    def _pause(self, seconds: int):
        self.parent._log(f"CHAOS pause: sleeping {seconds}s", "INFO")
        for _ in range(seconds):
            if self._stopflag.is_set():
                break
            time.sleep(1)
        self._record('pause', True, f"slept {seconds}s")


# ============================================================================
# Tester
# ============================================================================
class LargeTxnTester:

    def __init__(self, config_file: str, repset: str, verbose: bool):
        self.verbose = verbose
        self.repset = repset
        with open(config_file, 'r') as f:
            self.config = json.load(f)
        self.nodes = self.config['nodes']
        if len(self.nodes) < 2:
            self._log("Need 2 nodes (writer + subscriber)", "ERROR")
            sys.exit(1)
        self.n1 = self.nodes[0]['name']
        self.n2 = self.nodes[1]['name']
        # subscription names: maps node → the sub that node holds
        # (i.e. "sub_from_n2" on n1 receives data FROM n2,
        #        "sub_from_n1" on n2 receives data FROM n1)
        self.subscriptions = self.config.get('subscriptions', {})
        self.monitor: Optional[ReplicationMonitor] = None
        self.chaos: Optional[ChaosEngine] = None
        self._n1_global_md5 = hashlib.md5()

    def _log(self, msg: str, level: str = "INFO"):
        ts = datetime.now().strftime("%H:%M:%S")
        cmap = {"INFO": C.BLUE, "OK": C.GREEN, "WARN": C.YELLOW,
                "ERROR": C.RED, "HEADER": C.HEADER + C.BOLD,
                "METRIC": C.GRAY, "CHAOS": C.YELLOW + C.BOLD,
                "PLAN": C.HEADER}
        col = cmap.get(level, C.END)
        print(f"{col}[{ts}] [{level:6}] {msg}{C.END}", flush=True)

    def _node_def(self, name: str) -> Dict:
        return next(x for x in self.nodes if x['name'] == name)

    def _conn(self, node_name: str, autocommit: bool = True):
        n = self._node_def(node_name)
        c = psycopg2.connect(
            host=n['host'], port=n['port'], dbname=n['dbname'],
            user=n['user'], password=n['password'],
            connect_timeout=10,
            keepalives=1, keepalives_idle=30,
            keepalives_interval=10, keepalives_count=5,
        )
        c.autocommit = autocommit
        return c

    def _get_subs(self, node_name: str) -> List[Dict[str, Any]]:
        """Return subscriptions on a node via pglogical.show_subscription_status().
        Each dict has keys 'name' (str|None) and 'enabled' (bool|None).
        Also includes '_raw' with all original columns.

        pglogical.show_subscription_status() returns columns like:
          subscription_name, status, provider_node, ...
        status values: 'replicating', 'down', 'initializing', etc.
        """
        try:
            with self._conn(node_name) as c, c.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM pglogical.show_subscription_status();")
                raw = cur.fetchall()
        except Exception:
            raise
        out: List[Dict[str, Any]] = []
        for row in raw:
            d = dict(row)
            name = (d.get('subscription_name')
                    or d.get('sub_name')
                    or d.get('name'))
            # enabled resolution from status field
            enabled: Optional[bool] = None
            if 'status' in d:
                st = (str(d['status']).lower()
                       if d['status'] is not None else '')
                if 'disable' in st or st == 'down':
                    enabled = False
                elif st in ('replicating', 'initializing',
                            'catchup', 'syncwait', 'synced',
                            'ready', 'enabled', 'active'):
                    enabled = True
                else:
                    enabled = None  # unknown
            out.append({'name': name, 'enabled': enabled, '_raw': d})
        return out

    def preflight(self, plan: PhasePlan) -> bool:
        self._log("=" * 78, "HEADER")
        self._log("PRE-FLIGHT CHECKS", "HEADER")
        self._log("=" * 78, "HEADER")
        ok = True
        for n in (self.n1, self.n2):
            try:
                with self._conn(n) as c, c.cursor() as cur:
                    cur.execute("SELECT 1;")
                self._log(f"[{n}] reachable", "OK")
            except Exception as e:
                self._log(f"[{n}] connection failed: {e}", "ERROR")
                ok = False
        for n in (self.n1, self.n2):
            try:
                with self._conn(n) as c, c.cursor() as cur:
                    cur.execute(
                        "SELECT extversion FROM pg_extension "
                        "WHERE extname='pglogical';")
                    r = cur.fetchone()
                    if not r:
                        self._log(f"[{n}] pglogical NOT installed",
                                  "ERROR")
                        ok = False
                    else:
                        self._log(f"[{n}] pglogical {r[0]} installed",
                                  "OK")
            except Exception as e:
                self._log(f"[{n}] extension check failed: {e}", "WARN")
        try:
            subs = self._get_subs(self.n2)
            if not subs:
                self._log(f"[{self.n2}] no subscriptions",
                          "ERROR")
                ok = False
            else:
                # show the column shape once so users can debug
                # version-specific column names
                if subs[0].get('_raw'):
                    self._log(
                        f"[{self.n2}] show_subscription_status columns: "
                        f"{sorted(subs[0]['_raw'].keys())}",
                        "INFO")
                for r in subs:
                    if r['enabled'] is True:
                        st = 'enabled'
                        lvl = 'OK'
                    elif r['enabled'] is False:
                        st = 'DISABLED'
                        lvl = 'WARN'
                    else:
                        st = 'unknown'
                        lvl = 'WARN'
                    self._log(f"[{self.n2}] sub {r['name']}: {st}",
                              lvl)
        except Exception as e:
            self._log(f"[{self.n2}] subscription check failed: {e}",
                      "WARN")
        needed = int(plan.total_actual_bytes() * 2.5)
        for n in (self.n1, self.n2):
            try:
                with self._conn(n) as c, c.cursor() as cur:
                    cur.execute(
                        "SELECT setting FROM pg_settings "
                        "WHERE name='data_directory';")
                    r = cur.fetchone()
                    if r and r[0]:
                        try:
                            st = os.statvfs(r[0])
                            free = st.f_bavail * st.f_frsize
                            level = ("OK" if free >= needed
                                     else "WARN")
                            margin = "ok" if free >= needed else "LOW"
                            self._log(
                                f"[{n}] data_dir {r[0]}: "
                                f"{fmt_bytes(free)} free "
                                f"(need ~{fmt_bytes(needed)}, "
                                f"{margin})", level)
                        except Exception:
                            self._log(
                                f"[{n}] data_dir {r[0]} "
                                f"(remote — can't statvfs)", "INFO")
            except Exception as e:
                self._log(f"[{n}] disk check failed: {e}", "WARN")
        try:
            with self._conn(self.n1) as c, c.cursor() as cur:
                cur.execute(
                    "SELECT setting FROM pg_settings "
                    "WHERE name='max_slot_wal_keep_size';")
                r = cur.fetchone()
                if r:
                    self._log(
                        f"[{self.n1}] max_slot_wal_keep_size = {r[0]}",
                        "INFO")
        except Exception:
            pass
        return ok

    def _setup_schema(self):
        """pglogical schema setup workflow:
        1. Remove table from repset + DROP on both nodes (cleanup)
        2. CREATE TABLE manually on BOTH nodes (pglogical does NOT replicate DDL)
        3. pglogical.replication_set_add_table('default', 'public.table')
           on BOTH nodes — registers the table in the default replication set
        4. pglogical.alter_subscription_resynchronize_table(sub, 'public.table')
           on each node — kicks off table synchronization for the subscription
           worker so it starts tracking the new table
        """
        self._log("=" * 78, "HEADER")
        self._log("SCHEMA SETUP (pglogical workflow)", "HEADER")
        self._log("=" * 78, "HEADER")
        ddl_drop = f"DROP TABLE IF EXISTS {TABLE} CASCADE;"
        ddl_create = f"""
            CREATE TABLE {TABLE} (
                id          bigint PRIMARY KEY,
                payload     bytea NOT NULL,
                checksum    text  NOT NULL
            );
        """
        qualified_table = f"public.{TABLE}"

        # Step 0: Remove from repset + drop on both nodes (idempotent cleanup)
        for n in (self.n1, self.n2):
            try:
                with self._conn(n, autocommit=True) as c, \
                        c.cursor() as cur:
                    try:
                        cur.execute(
                            "SELECT pglogical.replication_set_remove_table("
                            "%s, %s);",
                            (self.repset, qualified_table))
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                with self._conn(n, autocommit=True) as c, \
                        c.cursor() as cur:
                    cur.execute(ddl_drop)
                self._log(f"[{n}] dropped any prior {TABLE}", "OK")
            except Exception as e:
                self._log(f"[{n}] drop failed: {e}", "WARN")

        # Step 1: CREATE TABLE on BOTH nodes (pglogical does NOT replicate DDL)
        for n in (self.n1, self.n2):
            with self._conn(n) as c, c.cursor() as cur:
                cur.execute(ddl_create)
            self._log(f"[{n}] created {TABLE}", "OK")

        # Step 2: Add the table to the replication set on BOTH nodes
        # This registers the table in pglogical's tracking engine so
        # changes on either node get published to subscribers.
        for n in (self.n1, self.n2):
            with self._conn(n) as c, c.cursor() as cur:
                try:
                    cur.execute(
                        "SELECT pglogical.replication_set_add_table("
                        "%s, %s);",
                        (self.repset, qualified_table))
                    self._log(f"[{n}] added {qualified_table} to repset "
                              f"'{self.repset}'", "OK")
                except Exception as e:
                    if ('already' in str(e).lower()
                            or 'duplicate' in str(e).lower()):
                        self._log(f"[{n}] already in repset", "INFO")
                    else:
                        raise

        # Step 3: Resynchronize the table on each subscription
        # This tells the subscription worker to look for and map the new table.
        time.sleep(1)
        for node_name, sub_name in self.subscriptions.items():
            try:
                with self._conn(node_name) as c, c.cursor() as cur:
                    cur.execute(
                        "SELECT pglogical."
                        "alter_subscription_resynchronize_table("
                        "%s, %s);",
                        (sub_name, qualified_table))
                    self._log(
                        f"[{node_name}] resynchronized {qualified_table} "
                        f"on subscription '{sub_name}'", "OK")
            except Exception as e:
                # If the table was just created and is empty, resync
                # might fail or be a no-op in some pglogical versions
                self._log(
                    f"[{node_name}] resync on '{sub_name}' note: "
                    f"{str(e).split(chr(10))[0]}", "WARN")

        time.sleep(2)
        self._log("Schema setup complete — table tracked on both nodes",
                  "OK")

    def cleanup_or_skip(self, args, context: str = ""):
        """Decide whether to run cleanup based on args, prompt the user
        if not explicitly told.

        - args.no_cleanup       => skip silently
        - args.auto_cleanup     => clean silently (CI / scripted)
        - args.non_interactive  => clean silently
        - otherwise             => prompt the user
        """
        if getattr(args, 'no_cleanup', False):
            self._log("Skipping cleanup (--no-cleanup); test table left "
                      f"in place on both nodes for inspection", "INFO")
            return
        if (getattr(args, 'auto_cleanup', False)
                or getattr(args, 'non_interactive', False)):
            self.cleanup()
            return
        if prompt_cleanup(context):
            self.cleanup()
        else:
            self._log(f"Skipping cleanup; '{TABLE}' kept on both nodes "
                      f"for inspection. Run with --cleanup to drop later.",
                      "INFO")

    def cleanup(self):
        self._log("=" * 78, "HEADER")
        self._log("CLEANUP", "HEADER")
        self._log("=" * 78, "HEADER")
        qualified_table = f"public.{TABLE}"
        # Re-enable any disabled subscriptions
        try:
            subs = self._get_subs(self.n2)
            with self._conn(self.n2) as c, c.cursor() as cur:
                for r in subs:
                    if r.get('enabled') is False and r.get('name'):
                        try:
                            cur.execute(
                                "SELECT pglogical."
                                "alter_subscription_enable(%s);",
                                (r['name'],))
                            self._log(
                                f"[{self.n2}] re-enabled sub "
                                f"{r['name']}", "OK")
                        except Exception:
                            pass
        except Exception:
            pass
        # Remove table from repset + drop on both nodes
        for n in (self.n1, self.n2):
            try:
                with self._conn(n, autocommit=True) as c, \
                        c.cursor() as cur:
                    try:
                        cur.execute(
                            "SELECT pglogical."
                            "replication_set_remove_table(%s, %s);",
                            (self.repset, qualified_table))
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                with self._conn(n, autocommit=True) as c, \
                        c.cursor() as cur:
                    cur.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE;")
                self._log(f"[{n}] dropped {TABLE}", "OK")
            except Exception as e:
                self._log(f"[{n}] cleanup error: {e}", "WARN")

    # ===================================================================
    # Producers
    # ===================================================================
    def _patterns(self, size_bytes: int) -> List[bytes]:
        random.seed(42)
        return [bytes(random.choices(range(32, 127), k=size_bytes))
                for _ in range(8)]

    def _eta(self, done: int, total: int, t0: float) -> str:
        if done == 0:
            return "ETA --"
        elapsed = time.time() - t0
        rate = done / max(elapsed, 1e-6)
        remaining = (total - done) / max(rate, 1e-6)
        return f"ETA {int(remaining)}s"

    def _bulk_copy(self, rows: int, row_size: int,
                    id_offset: int) -> Tuple[int, int]:
        self._log(f"[{self.n1}] BULK COPY: {rows:,} rows × "
                  f"{fmt_bytes(row_size)} ≈ {fmt_bytes(rows*row_size)} "
                  f"(id offset={id_offset:,})", "INFO")
        patterns = self._patterns(row_size)

        def hex_payload(b: bytes) -> str:
            # COPY text format treats backslash as an escape character.
            # A literal `\x` in the stream is consumed as a hex-byte
            # escape (one byte from `\xNN`), corrupting the bytea.
            # We must emit `\\x` so COPY decodes it back to `\x` for the
            # bytea hex-format parser.
            return '\\\\x' + b.hex()

        copy_sql = (f"COPY {TABLE}(id, payload, checksum) "
                    f"FROM STDIN WITH (FORMAT text);")

        # Memory bound: flush when buffer text exceeds this many bytes.
        # 32 MiB keeps RAM usage reasonable even with 100 MiB rows.
        # Hex-encoded payload is ~2.5x the binary size (2 hex chars per
        # byte plus the `\\x` prefix), so a single 100 MiB row is ~250 MiB
        # of text — we let it exceed the cap rather than splitting a row.
        FLUSH_BYTES = 32 * 1024 * 1024
        # Progress logs: every N MiB of payload sent (raw, not hex)
        PROGRESS_BYTES = 256 * 1024 * 1024

        total_bytes = 0
        last_progress_at = 0
        flush_count = 0
        t0 = time.time()
        conn = self._conn(self.n1, autocommit=False)
        try:
            with conn.cursor() as cur:
                buf = io.StringIO()
                rows_in_buf = 0
                for i in range(rows):
                    payload = patterns[i & 7]
                    chk = hashlib.md5(payload).hexdigest()
                    self._n1_global_md5.update(payload)
                    buf.write(
                        f"{i + id_offset}\t"
                        f"{hex_payload(payload)}\t{chk}\n")
                    rows_in_buf += 1
                    total_bytes += row_size
                    # Flush by text-buffer size (not by row count)
                    if buf.tell() >= FLUSH_BYTES:
                        buf.seek(0)
                        cur.copy_expert(copy_sql, buf)
                        buf.seek(0); buf.truncate(); rows_in_buf = 0
                        flush_count += 1
                    # Progress log every ~PROGRESS_BYTES of payload
                    if total_bytes - last_progress_at >= PROGRESS_BYTES:
                        elapsed = time.time() - t0
                        mbs = (total_bytes / 1024 / 1024
                                / max(elapsed, 1e-6))
                        self._log(
                            f"[{self.n1}] {i+1:>11,}/{rows:,} rows "
                            f"({fmt_bytes(total_bytes)}) @ "
                            f"{mbs:.1f} MB/s  "
                            f"{self._eta(i+1, rows, t0)}",
                            "METRIC")
                        last_progress_at = total_bytes
                # Final partial flush
                if rows_in_buf:
                    buf.seek(0)
                    cur.copy_expert(copy_sql, buf)
                    buf.seek(0); buf.truncate()
                    flush_count += 1
                conn.commit()
            elapsed = time.time() - t0
            mbs = total_bytes / 1024 / 1024 / max(elapsed, 1e-6)
            self._log(f"[{self.n1}] BULK COPY committed: "
                      f"{fmt_bytes(total_bytes)} in {elapsed:.1f}s "
                      f"({mbs:.1f} MB/s, {flush_count} flushes)", "OK")
            return rows, total_bytes
        finally:
            try:
                conn.close()
            except Exception:
                pass


    def _maybe_pre_commit_chaos(self, chaos_actions: List[str]):
        if 'fill-slot' not in chaos_actions:
            return
        self._log("CHAOS fill-slot: disabling subscription on n2 BEFORE "
                  "producer commit", "CHAOS")
        subs = [s['name'] for s in self._get_subs(self.n2)
                if s.get('name')]
        with self._conn(self.n2) as c, c.cursor() as cur:
            for sn in subs:
                cur.execute(
                    "SELECT pglogical.alter_subscription_disable(%s, "
                    "immediate := true);",
                    (sn,))
        self._log(f"[{self.n2}] disabled subscriptions: {subs}", "OK")

    def _maybe_resume_pre_commit_chaos(self, chaos_actions: List[str]):
        if 'fill-slot' not in chaos_actions:
            return
        self._log("CHAOS fill-slot: re-enabling subscription on n2",
                  "CHAOS")
        subs = [s['name'] for s in self._get_subs(self.n2)
                if s.get('name')]
        with self._conn(self.n2) as c, c.cursor() as cur:
            for sn in subs:
                cur.execute(
                    "SELECT pglogical.alter_subscription_enable(%s);",
                    (sn,))
        self._log(f"[{self.n2}] re-enabled subscriptions: {subs}", "OK")

    def _wait_for_convergence(self, expected_rows: int,
                                timeout_sec: int
                                ) -> Tuple[bool, Dict]:
        self._log("=" * 78, "HEADER")
        self._log(f"WAITING FOR CONVERGENCE on n2 "
                  f"(expected {expected_rows:,} rows, "
                  f"timeout {timeout_sec}s)", "HEADER")
        self._log("=" * 78, "HEADER")
        t_start = time.time()
        last_count = -1
        last_change = time.time()
        stall_warned = False
        first_obs_count = None
        first_obs_time = None
        while time.time() - t_start < timeout_sec:
            try:
                with self._conn(self.n2) as c, c.cursor() as cur:
                    cur.execute(f"SELECT count(*) FROM {TABLE};")
                    cur_count = cur.fetchone()[0]
            except Exception as e:
                self._log(f"[{self.n2}] count failed: {e}", "WARN")
                time.sleep(2)
                continue
            if cur_count != last_count:
                pct = (100.0 * cur_count / expected_rows
                        if expected_rows else 0.0)
                if first_obs_count is None and cur_count > 0:
                    first_obs_count = cur_count
                    first_obs_time = time.time()
                eta = ""
                if (first_obs_count is not None
                        and first_obs_time is not None
                        and cur_count > first_obs_count):
                    rate = ((cur_count - first_obs_count)
                            / max(time.time() - first_obs_time, 1e-6))
                    remaining_rows = expected_rows - cur_count
                    if rate > 0:
                        eta = (f" ETA {int(remaining_rows / rate)}s "
                                f"@ {rate:,.0f} rows/s on n2")
                self._log(f"[{self.n2}] {cur_count:,}/"
                            f"{expected_rows:,} ({pct:.1f}%){eta}",
                            "METRIC")
                last_count = cur_count
                last_change = time.time()
                stall_warned = False
            elif time.time() - last_change > 30 and not stall_warned:
                self._log(f"[{self.n2}] stalled at {cur_count:,} "
                            f"for >30s", "WARN")
                stall_warned = True
            if cur_count >= expected_rows:
                break
            time.sleep(2)
        elapsed = time.time() - t_start
        n2_throughput_rps = None
        if (first_obs_count is not None and first_obs_time is not None
                and last_count > first_obs_count):
            n2_throughput_rps = ((last_count - first_obs_count)
                                  / max(time.time() - first_obs_time,
                                        1e-6))
        try:
            with self._conn(self.n2) as c, c.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {TABLE};")
                final_count = cur.fetchone()[0]
                if final_count == expected_rows:
                    cur.execute(
                        f"SELECT md5(string_agg("
                        f"  encode(payload,'hex'), '' ORDER BY id)) "
                        f"FROM {TABLE};")
                    n2_md5 = cur.fetchone()[0]
                else:
                    n2_md5 = None
        except Exception as e:
            return False, {'error': str(e), 'final_count': last_count,
                            'elapsed': elapsed,
                            'n2_throughput_rps': n2_throughput_rps}
        n1_md5 = None
        if n2_md5:
            try:
                with self._conn(self.n1) as c, c.cursor() as cur:
                    cur.execute(
                        f"SELECT md5(string_agg("
                        f"  encode(payload,'hex'), '' ORDER BY id)) "
                        f"FROM {TABLE};")
                    n1_md5 = cur.fetchone()[0]
            except Exception as e:
                self._log(f"[{self.n1}] md5 failed: {e}", "WARN")
        ok = (final_count == expected_rows
              and n2_md5 is not None and n2_md5 == n1_md5)
        return ok, {
            'final_count': final_count,
            'expected_count': expected_rows,
            'n1_md5': n1_md5,
            'n2_md5': n2_md5,
            'elapsed': elapsed,
            'n2_throughput_rps': n2_throughput_rps,
        }

    def _summarize(self):
        if not self.monitor or not self.monitor.samples:
            return
        samples = self.monitor.samples
        peak_lag = peak_retained = peak_n2_bytes = 0
        worker_pids_seen = set()
        worker_restart_events = 0
        prev_worker_pids: set = set()
        slot_active_flips = 0
        prev_slot_active = None
        for s in samples:
            for sl in s.get('n1_slots', []):
                if sl.get('lag_bytes') is not None:
                    peak_lag = max(peak_lag, sl['lag_bytes'])
                if sl.get('retained_bytes') is not None:
                    peak_retained = max(peak_retained,
                                         sl['retained_bytes'])
                if prev_slot_active is None:
                    prev_slot_active = sl.get('active')
                elif sl.get('active') != prev_slot_active:
                    slot_active_flips += 1
                    prev_slot_active = sl.get('active')
            this_pids = {w['pid'] for w in s.get('n2_workers', [])}
            if (prev_worker_pids and this_pids != prev_worker_pids
                    and (this_pids - prev_worker_pids)):
                worker_restart_events += 1
            worker_pids_seen |= this_pids
            prev_worker_pids = this_pids
            if s.get('n2_table_bytes') is not None:
                peak_n2_bytes = max(peak_n2_bytes, s['n2_table_bytes'])
        self._log("=" * 78, "HEADER")
        self._log("REPLICATION MONITOR SUMMARY", "HEADER")
        self._log("=" * 78, "HEADER")
        self._log(f"  samples taken          : {len(samples)}", "INFO")
        self._log(f"  peak slot lag          : {fmt_bytes(peak_lag)}",
                  "INFO")
        self._log(f"  peak slot retained WAL : "
                  f"{fmt_bytes(peak_retained)}", "INFO")
        self._log(f"  peak n2 table size     : "
                  f"{fmt_bytes(peak_n2_bytes)}", "INFO")
        self._log(f"  pglogical pids on n2  : "
                  f"{sorted(worker_pids_seen)}", "INFO")
        self._log(f"  apply-worker restarts  : "
                  f"{worker_restart_events}", "INFO")
        self._log(f"  slot active flips      : {slot_active_flips}",
                  "INFO")
        if self.chaos and self.chaos.events:
            self._log("\nChaos events:", "INFO")
            for ev in self.chaos.events:
                self._log(f"  {ev['action']:24s} "
                          f"{'OK' if ev['ok'] else 'FAIL'} "
                          f"— {ev['detail'][:80]}",
                          "OK" if ev['ok'] else "ERROR")

    def run(self, args, plan: PhasePlan):
        chaos_actions = ([a.strip() for a in args.chaos.split(',')
                            if a.strip()] if args.chaos else [])
        bad = [a for a in chaos_actions if a not in VALID_CHAOS]
        if bad:
            self._log(f"Invalid chaos: {bad}. Valid: "
                      f"{sorted(VALID_CHAOS)}", "ERROR")
            sys.exit(2)

        self._log("=" * 78, "HEADER")
        self._log(f"PGLOGICAL LARGE TXN TEST  "
                  f"{plan.row_count:,} rows × "
                  f"{fmt_bytes(plan.row_bytes)} = "
                  f"{fmt_bytes(plan.total_actual_bytes())}", "HEADER")
        for line in plan.describe():
            self._log(f"  {line}", "PLAN")
        self._log(f"  writer={self.n1}  subscriber={self.n2}  "
                  f"repset={self.repset}", "INFO")
        self._log(f"  expect: {args.expect}", "INFO")
        if chaos_actions:
            self._log(f"  chaos : {chaos_actions}", "CHAOS")
            if 'pause' in chaos_actions:
                self._log(f"  chaos-pause-sec : "
                          f"{args.chaos_pause_sec}", "CHAOS")
        self._log("=" * 78, "HEADER")

        if not self.preflight(plan):
            self._log("Pre-flight failed", "ERROR")
            sys.exit(2)

        self._setup_schema()
        self.monitor = ReplicationMonitor(
            self, args.monitor_interval, args.metrics_out)
        self.monitor.start()

        try:
            self._maybe_pre_commit_chaos(chaos_actions)
        except Exception as e:
            self._log(f"pre-commit chaos failed: {e}", "ERROR")

        produce_ok = True
        produce_err = ''
        total_rows = 0
        total_bytes = 0
        t_producer_start = time.time()
        try:
            r, b = self._bulk_copy(
                plan.row_count, plan.row_bytes, 0)
            total_rows += r
            total_bytes += b
        except Exception as e:
            produce_ok = False
            produce_err = str(e).split('\n')[0]
            self._log(f"[{self.n1}] PRODUCER FAILED: {produce_err}",
                      "ERROR")
        producer_elapsed = time.time() - t_producer_start
        try:
            self._maybe_resume_pre_commit_chaos(chaos_actions)
        except Exception as e:
            self._log(f"post-commit chaos resume failed: {e}", "ERROR")

        if not produce_ok:
            self.monitor.stop()
            self._summarize()
            if args.expect == 'failure':
                self._log("RESULT: PASS — producer failed AS EXPECTED",
                          "OK")
                self.cleanup_or_skip(
                    args, context="Producer failed as expected.")
                sys.exit(0)
            else:
                self._log("RESULT: FAIL — producer failed, success "
                          "expected", "ERROR")
                self.cleanup_or_skip(
                    args, context="FAIL: producer error — keep table for "
                                  "investigation?")
                sys.exit(1)

        in_flight_chaos = [a for a in chaos_actions
                            if a in ('restart-subscriber',
                                     'kill-apply-worker', 'pause')]
        if in_flight_chaos:
            self.chaos = ChaosEngine(
                self, in_flight_chaos, args.chaos_pause_sec)
            self.chaos.start_after_commit()
        converged, detail = self._wait_for_convergence(
            total_rows, args.converge_timeout)
        if self.chaos:
            self.chaos.stop()
        self.monitor.stop()
        self._summarize()
        n1_rps = total_rows / max(producer_elapsed, 1e-6)
        n1_mbs = total_bytes / 1024 / 1024 / max(producer_elapsed, 1e-6)
        n2_rps = detail.get('n2_throughput_rps')
        self._log("=" * 78, "HEADER")
        self._log("VERDICT", "HEADER")
        self._log("=" * 78, "HEADER")
        self._log(f"  rows × size          : {plan.row_count:,} × "
                  f"{fmt_bytes(plan.row_bytes)}", "INFO")
        self._log(f"  size                 : {fmt_bytes(total_bytes)} "
                  f"(planned {fmt_bytes(plan.total_actual_bytes())})",
                  "INFO")
        self._log(f"  producer elapsed     : {producer_elapsed:.1f}s",
                  "INFO")
        self._log(f"  n1 throughput        : {n1_rps:,.0f} rows/s "
                  f"({n1_mbs:.1f} MB/s)", "INFO")
        if n2_rps:
            ratio = n2_rps / n1_rps if n1_rps else 0
            self._log(f"  n2 apply throughput  : {n2_rps:,.0f} rows/s "
                      f"({ratio*100:.0f}% of n1)", "INFO")
        else:
            self._log(f"  n2 apply throughput  : (not measurable)",
                      "INFO")
        self._log(f"  rows written         : {total_rows:,}", "INFO")
        self._log(f"  n2 final row count   : "
                  f"{detail.get('final_count')}", "INFO")
        self._log(f"  n2 expected rows     : "
                  f"{detail.get('expected_count')}", "INFO")
        self._log(f"  n1 md5               : {detail.get('n1_md5')}",
                  "INFO")
        self._log(f"  n2 md5               : {detail.get('n2_md5')}",
                  "INFO")
        self._log(f"  convergence elapsed  : "
                  f"{detail.get('elapsed', 0):.1f}s", "INFO")
        if args.expect == 'success':
            if converged:
                self._log("RESULT: PASS — replicated, converged, "
                          "md5 matches", "OK")
                rc = 0
                ctx = "Test passed."
            else:
                self._log("RESULT: FAIL — did not converge", "ERROR")
                rc = 1
                ctx = ("FAIL: did not converge — keep table for "
                       "investigation?")
        else:
            if converged:
                self._log("RESULT: FAIL — convergence happened but "
                          "failure was expected", "ERROR")
                rc = 1
                ctx = ("FAIL: convergence was not expected — keep "
                       "table for investigation?")
            else:
                self._log("RESULT: PASS — failure observed as expected",
                          "OK")
                rc = 0
                ctx = "Failure observed as expected."
        self.cleanup_or_skip(args, context=ctx)
        sys.exit(rc)


# ============================================================================
# CLI
# ============================================================================
def main():
    p = argparse.ArgumentParser(
        description="pglogical 2-node large-transaction stress test "
                    "(interactive)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:

  # Fully interactive — asks for row count, then row size
  python test_pglogical_large_txn.py --config nodes2.json

  # Hint a target size; the prompts suggest defaults derived from it
  python test_pglogical_large_txn.py --config nodes2.json --size 2.5GB

  # Non-interactive: 50M rows of 50 B each (~2.5 GiB)
  python test_pglogical_large_txn.py --config nodes2.json --non-interactive \\
      --rows 50m --row-bytes 50

  # Non-interactive: 10M rows of 1 KiB each (~10 GiB)
  python test_pglogical_large_txn.py --config nodes2.json --non-interactive \\
      --rows 10m --row-bytes 1KiB

  # Chaos
  python test_pglogical_large_txn.py --config nodes2.json \\
      --chaos kill-apply-worker --metrics-out run.jsonl

  # Failure-boundary: huge txn that should NOT replicate cleanly
  python test_pglogical_large_txn.py --config nodes2.json --non-interactive \\
      --rows 200m --row-bytes 50 --expect failure

  # Cleanup leftover from a previous run
  python test_pglogical_large_txn.py --config nodes2.json --cleanup
""")
    p.add_argument('--config', required=True,
                   help='2-node JSON config')
    p.add_argument('--expect', choices=['success', 'failure'],
                   default='success')

    p.add_argument('--rows',
                   help='Row count for the transaction. Accepts k/m '
                        'suffixes (e.g. 50m = 50 million). Pre-fills the '
                        'interactive prompt; required with '
                        '--non-interactive.')
    p.add_argument('--row-bytes',
                   help='Bytes per row (e.g. 50, 500B, 1KB, 4KiB). '
                        'Pre-fills the interactive prompt; required '
                        'with --non-interactive.')

    # Optional hint used to suggest a default row count when no
    # --rows / --row-bytes given
    p.add_argument('--size',
                   help='[hint] Total transaction size used to suggest '
                        'defaults in the interactive prompts.')

    p.add_argument('--chaos', default=None,
                   help='restart-subscriber, kill-apply-worker, '
                        'fill-slot, pause (comma-separated)')
    p.add_argument('--chaos-pause-sec', type=int, default=15)

    p.add_argument('--monitor-interval', type=float, default=2.0)
    p.add_argument('--metrics-out',
                   help='JSONL per-sample metrics file')
    p.add_argument('--converge-timeout', type=int, default=3600)
    p.add_argument('--repset', default=DEFAULT_REPSET)

    p.add_argument('--non-interactive', action='store_true',
                   help='Skip ALL prompts; require --rows and '
                        '--row-bytes on the CLI. Implies '
                        '--auto-cleanup unless --no-cleanup is also set.')
    p.add_argument('--cleanup', action='store_true',
                   help='Drop the test table and exit (no test run)')
    p.add_argument('--auto-cleanup', action='store_true',
                   help='At end of run, drop the test table without '
                        'asking (default behaviour is to prompt)')
    p.add_argument('--no-cleanup', action='store_true',
                   help='At end of run, KEEP the test table on both '
                        'nodes (skip cleanup entirely; useful for '
                        'post-mortem inspection)')
    p.add_argument('--verbose', '-v', action='store_true')
    args = p.parse_args()
    if args.auto_cleanup and args.no_cleanup:
        print(f"{C.RED}--auto-cleanup and --no-cleanup are mutually "
              f"exclusive{C.END}")
        sys.exit(2)

    t = LargeTxnTester(args.config, repset=args.repset,
                        verbose=args.verbose)

    if args.cleanup:
        try:
            t.cleanup()
        except Exception as e:
            print(f"{C.RED}cleanup error: {e}{C.END}")
        return

    # ---------------- defaults ----------------
    DEFAULT_ROW_COUNT = 1_000_000
    DEFAULT_ROW_BYTES = 500
    DEFAULT_SIZE_HINT = parse_size("2.5GB")

    # Parse CLI overrides
    cli_rows: Optional[int] = None
    cli_row_bytes: Optional[int] = None
    if args.rows:
        try:
            raw = args.rows.strip().lower()
            mult = 1
            if raw.endswith('k'):
                mult = 1_000; raw = raw[:-1]
            elif raw.endswith('m'):
                mult = 1_000_000; raw = raw[:-1]
            cli_rows = int(float(raw) * mult)
        except ValueError:
            print(f"{C.RED}--rows: not a number{C.END}")
            sys.exit(2)
    if args.row_bytes:
        try:
            cli_row_bytes = parse_size(args.row_bytes)
        except ValueError as e:
            print(f"{C.RED}--row-bytes: {e}{C.END}")
            sys.exit(2)

    # Legacy --size as a hint when other flags omitted
    size_hint: Optional[int] = None
    if args.size:
        try:
            size_hint = parse_size(args.size)
        except ValueError as e:
            print(f"{C.RED}--size: {e}{C.END}")
            sys.exit(2)

    if args.non_interactive:
        if cli_rows is None:
            print(f"{C.RED}--non-interactive requires --rows "
                  f"(e.g. --rows 50m){C.END}")
            sys.exit(2)
        if cli_row_bytes is None:
            print(f"{C.RED}--non-interactive requires --row-bytes "
                  f"(e.g. --row-bytes 50){C.END}")
            sys.exit(2)
        row_count = cli_rows
        row_bytes = cli_row_bytes
    else:
        # Interactive: ask for row count first, then row size
        target_hint = size_hint or DEFAULT_SIZE_HINT
        row_count = prompt_for_row_count(
            target_bytes=target_hint,
            max_bytes_per_row=10 * 1024 * 1024,
            default_count=cli_rows or DEFAULT_ROW_COUNT,
        )
        if cli_row_bytes:
            default_row_size = cli_row_bytes
        elif size_hint:
            default_row_size = max(1, size_hint // row_count)
        else:
            default_row_size = DEFAULT_ROW_BYTES
        row_bytes = prompt_for_row_size(default_row_size)

    # ---------------- build plan ----------------
    try:
        plan = PhasePlan(row_count=row_count, row_bytes=row_bytes)
    except ValueError as e:
        print(f"{C.RED}Plan error: {e}{C.END}")
        sys.exit(2)

    # ---------------- confirm ----------------
    if not args.non_interactive:
        if not confirm_plan(plan):
            print(f"{C.YELLOW}Aborted at confirmation{C.END}")
            sys.exit(0)

    try:
        t.run(args, plan)
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}Interrupted{C.END}")
        try:
            if t.chaos: t.chaos.stop()
            if t.monitor: t.monitor.stop()
        except Exception:
            pass
        try:
            t.cleanup_or_skip(
                args, context="Run was interrupted (Ctrl-C).")
        except Exception:
            pass
        sys.exit(130)
    except Exception as e:
        print(f"{C.RED}Fatal: {e}{C.END}")
        import traceback
        traceback.print_exc()
        try:
            if t.monitor: t.monitor.stop()
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
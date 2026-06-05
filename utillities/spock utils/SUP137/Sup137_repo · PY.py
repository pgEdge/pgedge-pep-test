#!/usr/bin/env python3
"""
SUP-137 reproducer.

Replicates the spock v5.0.8 manager-worker race by churning tenant databases
from multiple parallel workers while (optionally) restarting one or more
nodes mid-churn.

Assumptions:
  - A cluster of N nodes (N >= 1) is already up and reachable.
  - spock 5.0.8 is installed and the spock-enabled DB (default 'pgedge')
    exists on each node with the spock extension created.
  - A Postgres role with CREATEDB privilege is available (default: postgres).

The script asks how many nodes you have, then per-node connection details,
install paths, churn targets, and restart targets at runtime. Press Enter
to accept the [default] for any prompt.
"""

import multiprocessing as mp
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

try:
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
except ImportError:
    sys.exit("psycopg2 not installed. Run: pip install psycopg2-binary")


# ---------- config ----------

@dataclass
class Node:
    name: str
    host: str
    port: int
    user: str
    dbname: str
    pgdata: Optional[str] = None
    pg_bin: Optional[str] = None
    pg_os_user: Optional[str] = None   # OS user to run pg_ctl as (None = current user)

    def conn_kwargs(self, dbname: Optional[str] = None) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "dbname": dbname or self.dbname,
            "connect_timeout": 10,
        }

    @property
    def server_log(self) -> Optional[str]:
        return os.path.join(self.pgdata, "server.log") if self.pgdata else None


def prompt(label: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        val = input(f"{label}{suffix}: ").strip()
        if val:
            return val
        if default is not None:
            return default


def prompt_int(label: str, default: int, min_val: int = 1) -> int:
    while True:
        raw = prompt(label, str(default))
        try:
            v = int(raw)
            if v < min_val:
                print(f"  must be >= {min_val}")
                continue
            return v
        except ValueError:
            print("  not a number, try again")


def prompt_yes_no(label: str, default: bool = False) -> bool:
    default_str = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{default_str}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("  please answer y or n")


def prompt_indices(label: str, max_idx: int, allow_empty: bool = True) -> list[int]:
    """
    Prompt for a comma-separated list of 1-based indices, or 'all', or empty.
    Returns 0-based indices.
    """
    hint = f"comma-separated 1..{max_idx}, or 'all'"
    if allow_empty:
        hint += ", or blank for none"
    while True:
        raw = input(f"{label} ({hint}): ").strip().lower()
        if not raw:
            if allow_empty:
                return []
            print("  selection required")
            continue
        if raw == "all":
            return list(range(max_idx))
        try:
            picks = [int(x.strip()) for x in raw.split(",") if x.strip()]
            if not picks or any(p < 1 or p > max_idx for p in picks):
                raise ValueError
            return sorted({p - 1 for p in picks})
        except ValueError:
            print(f"  invalid — expected numbers in 1..{max_idx}")


def detect_pgdata_owner(pgdata: str) -> Optional[str]:
    """Return the username that owns the PGDATA directory, or None."""
    try:
        import pwd
        st = os.stat(pgdata)
        return pwd.getpwuid(st.st_uid).pw_name
    except Exception:
        return None


def gather_nodes() -> list[Node]:
    print("=" * 60)
    print("SUP-137 repro — connection setup")
    print("=" * 60)

    n = prompt_int("\nHow many nodes in the cluster?", 2)

    nodes: list[Node] = []
    prev_user = "postgres"
    prev_db = "pgedge"

    for i in range(1, n + 1):
        print(f"\n--- Node {i} ---")
        host = prompt("Host", "127.0.0.1")
        # Suggest sequential ports as defaults: 5432, 5433, 5434, ...
        port = prompt_int("Port", 5431 + i)
        user = prompt("Postgres user", prev_user)
        db   = prompt("Spock-enabled database", prev_db)

        pgdata = prompt("PGDATA path (blank if you don't need to restart this node)", "")
        pgbin  = prompt("PG bin dir (e.g. /usr/local/pgsql.16/bin)", "") if pgdata else ""

        # Postgres refuses to let pg_ctl run as root. If we're root and the
        # user gave us a PGDATA, ask which OS user should own the pg_ctl
        # invocation. Default to whoever owns PGDATA on disk.
        pg_os_user: Optional[str] = None
        if pgdata:
            running_as_root = (os.geteuid() == 0) if hasattr(os, "geteuid") else False
            owner = detect_pgdata_owner(pgdata) if os.path.exists(pgdata) else None
            default_user = owner or ("postgres" if running_as_root else "")
            label = "OS user to run pg_ctl as (blank = current user)"
            pg_os_user = prompt(label, default_user) or None
            if running_as_root and not pg_os_user:
                print("  !! WARNING: running as root and no OS user set.")
                print("     pg_ctl will refuse to run. Set an OS user or rerun as non-root.")

        nodes.append(Node(
            name=f"n{i}",
            host=host, port=port, user=user, dbname=db,
            pgdata=pgdata or None,
            pg_bin=pgbin or None,
            pg_os_user=pg_os_user,
        ))
        prev_user, prev_db = user, db

    return nodes


def gather_run_params(nodes: list[Node]) -> tuple[list[int], list[int], int, int, int, bool]:
    """
    Returns (churn_node_indices, restart_node_indices, workers_per_node,
             iterations, pre_restart_sleep, do_drop).
    """
    print("\n--- Churn targets ---")
    print("Which node(s) should receive the CREATE/DROP DATABASE churn?")
    for i, node in enumerate(nodes, 1):
        print(f"  {i}. {node.name} ({node.host}:{node.port})")
    churn_idx = prompt_indices("Pick churn nodes", len(nodes), allow_empty=False)

    workers = prompt_int("\nWorkers per churn node", 6)
    iters   = prompt_int("Iterations per worker", 200)

    # Whether to drop databases after creating them. The DROP is what triggers
    # SUP-137 — skipping it gives a CREATE-only baseline run that will leave
    # all tenant DBs behind and almost certainly NOT reproduce the bug.
    print("\n--- DROP behaviour ---")
    print("By default each worker runs CREATE DATABASE then DROP DATABASE ... WITH (FORCE).")
    print("The DROP is what races with spock's manager worker and triggers SUP-137.")
    print("Answer 'n' only if you want a CREATE-only baseline (will not reproduce the bug).")
    do_drop = prompt_yes_no("Run DROP DATABASE after each CREATE?", default=True)

    print("\n--- Mid-churn restart ---")
    print("Which node(s) should be restarted partway through the churn?")
    print("(Only nodes with PGDATA + PG bin dir set can be restarted.)")
    for i, node in enumerate(nodes, 1):
        restartable = "ok" if (node.pgdata and node.pg_bin) else "no pgdata/bin"
        print(f"  {i}. {node.name} [{restartable}]")
    restart_idx = prompt_indices("Pick restart nodes (blank = no restart)",
                                 len(nodes), allow_empty=True)

    # Validate that restart picks have pgdata + pg_bin
    bad = [nodes[i].name for i in restart_idx
           if not (nodes[i].pgdata and nodes[i].pg_bin)]
    if bad:
        print(f"\n!! Cannot restart {', '.join(bad)} — missing PGDATA or PG bin dir.")
        print("   They will be skipped.")
        restart_idx = [i for i in restart_idx
                       if nodes[i].pgdata and nodes[i].pg_bin]

    pre_sleep = prompt_int("\nSeconds of churn before restart", 15) if restart_idx else 0

    return churn_idx, restart_idx, workers, iters, pre_sleep, do_drop


# ---------- DB churn worker ----------

def churn_worker(node_kwargs: dict, worker_id: int, iterations: int,
                 tag: str, do_drop: bool = True):
    """
    Subprocess body. Loops CREATE (and optionally DROP) against the
    spock-enabled DB on a single node. Fresh connection per statement
    to avoid holding a session that could interfere with the manager
    worker or with DROP ... FORCE.
    """
    # In bash, $$ expands to the parent shell PID — identical across all
    # sibling workers; uniqueness comes from $w and $i. Here each child
    # process has its own PID — stronger uniqueness, same churn pattern.
    pid = os.getpid()
    created = dropped = errors = 0

    for i in range(1, iterations + 1):
        # Matches bash naming: kylo_w${w}_${i}_$$
        name = f"kylo_w{worker_id}_{i}_{pid}"

        # CREATE — errors silenced, matching `>/dev/null 2>&1` in bash
        try:
            conn = psycopg2.connect(**node_kwargs)
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            with conn.cursor() as cur:
                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            conn.close()
            created += 1
        except Exception:
            errors += 1
            # Bash version always proceeds to DROP regardless — fall through.

        if not do_drop:
            continue

        # DROP WITH FORCE — also silenced, also unconditional
        try:
            conn = psycopg2.connect(**node_kwargs)
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
                )
            conn.close()
            dropped += 1
        except Exception:
            errors += 1

    print(f"  [worker {tag}-{worker_id}] done: "
          f"created={created} dropped={dropped} errors={errors}")


def spawn_workers(node: Node, num_workers: int, iterations: int,
                  do_drop: bool) -> list[mp.Process]:
    procs = []
    for w in range(1, num_workers + 1):
        p = mp.Process(
            target=churn_worker,
            args=(node.conn_kwargs(), w, iterations, node.name, do_drop),
            name=f"churn-{node.name}-{w}",
        )
        p.start()
        procs.append(p)
    return procs


# ---------- restart helpers ----------

def pg_ctl(node: Node, *args: str) -> subprocess.CompletedProcess:
    if not (node.pg_bin and node.pgdata):
        raise RuntimeError(f"node {node.name}: pg_bin and pgdata required for pg_ctl")

    pg_ctl_path = os.path.join(node.pg_bin, "pg_ctl")
    base_cmd = [pg_ctl_path, "-D", node.pgdata, *args]

    if node.pg_os_user:
        # `su - USER -c 'CMD'` runs CMD as USER with a login shell. We shlex.quote
        # each argument so paths with spaces still work.
        inner = " ".join(shlex.quote(c) for c in base_cmd)
        cmd = ["su", "-", node.pg_os_user, "-c", inner]
    else:
        cmd = base_cmd

    print(f"  $ {' '.join(shlex.quote(c) for c in cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True)


def restart_node(node: Node) -> bool:
    log_arg = ["-l", node.server_log] if node.server_log else []
    print(f"\n>>> restarting {node.name}")

    stop = pg_ctl(node, "-m", "fast", "-w", "stop")
    if stop.returncode != 0:
        print(f"  stop failed: {stop.stderr.strip()}")
        return False
    print(f"  [{node.name}] stopped")

    time.sleep(2)

    start = pg_ctl(node, *log_arg, "-w", "start")
    if start.returncode != 0:
        print(f"  start failed: {start.stderr.strip()}")
        return False
    print(f"  [{node.name}] back up")
    return True


# ---------- log scanning ----------

PATTERNS = [
    r"FATAL.*cannot connect to invalid database",
    r"FATAL.*database [0-9]+ does not exist",
    r"manager worker.*exiting with error",
]
COMBINED_RE = re.compile("|".join(PATTERNS))


def capture_log_offset(node: Node) -> Optional[int]:
    if not node.server_log or not os.path.exists(node.server_log):
        return None
    return os.path.getsize(node.server_log)


def scan_log(node: Node, offset: Optional[int]) -> tuple[int, list[str]]:
    if offset is None or not node.server_log or not os.path.exists(node.server_log):
        return (0, [])
    with open(node.server_log, "rb") as f:
        f.seek(offset)
        tail = f.read().decode("utf-8", errors="replace")
    hits = [line for line in tail.splitlines() if COMBINED_RE.search(line)]
    return (len(hits), hits)


# ---------- main ----------

def main():
    nodes = gather_nodes()
    churn_idx, restart_idx, workers, iters, pre_sleep, do_drop = gather_run_params(nodes)

    churn_nodes = [nodes[i] for i in churn_idx]
    restart_nodes = [nodes[i] for i in restart_idx]

    print("\n" + "=" * 60)
    print("Plan:")
    print(f"  Cluster:        {len(nodes)} node(s) — {', '.join(n.name for n in nodes)}")
    print(f"  Churn on:       {', '.join(n.name for n in churn_nodes)}")
    print(f"  Workers/node:   {workers}")
    print(f"  Iterations:     {iters}")
    print(f"  Operations:     {'CREATE + DROP (WITH FORCE)' if do_drop else 'CREATE only (no DROP)'}")
    if not do_drop:
        print("                  !! CREATE-only run will leave tenant DBs behind")
        print("                     and is unlikely to reproduce SUP-137.")
    if restart_nodes:
        print(f"  Restart after:  {pre_sleep}s — {', '.join(n.name for n in restart_nodes)}")
    else:
        print("  Restart:        none")
    print("=" * 60)

    if not prompt_yes_no("\nProceed?", default=True):
        sys.exit(0)

    # Capture log offsets BEFORE starting churn (matches `OFF=$(wc -c < ...)`).
    # We capture for *every* node with a known log path, not just churn nodes,
    # since manager workers on other nodes can also log FATALs.
    print("\n--- capturing log offsets ---")
    offsets: dict[str, Optional[int]] = {}
    for node in nodes:
        off = capture_log_offset(node)
        offsets[node.name] = off
        if off is None:
            print(f"  {node.name}: (no log access — skipped at scan time)")
        else:
            print(f"  {node.name}: offset={off}")

    # Launch churn workers (non-blocking — equivalent to bash `&`).
    print(f"\n--- launching {workers} workers on each of "
          f"{len(churn_nodes)} node(s), {iters} iterations each ---")
    all_procs: list[mp.Process] = []
    for node in churn_nodes:
        all_procs.extend(spawn_workers(node, workers, iters, do_drop))
    print(f"  total workers running: {len(all_procs)}")

    # Mid-churn restart (matches bash: sleep 15; pg_ctl stop; sleep 2; pg_ctl start).
    if restart_nodes:
        print(f"\n--- sleeping {pre_sleep}s before restart ---")
        time.sleep(pre_sleep)
        for node in restart_nodes:
            restart_node(node)

    # `wait` equivalent.
    print("\n--- waiting for workers to finish ---")
    for p in all_procs:
        p.join()

    # Scan logs from saved offsets (matches step 4).
    print("\n--- scanning logs for SUP-137 signatures ---")
    total_hits = 0
    for node in nodes:
        count, lines = scan_log(node, offsets[node.name])
        total_hits += count
        if offsets[node.name] is None:
            print(f"\n  [{node.name}] skipped (no log path)")
            continue
        print(f"\n  [{node.name}] matches: {count}")
        for line in lines[:20]:
            print(f"    {line}")
        if len(lines) > 20:
            print(f"    ... and {len(lines) - 20} more")

    print("\n" + "=" * 60)
    if total_hits > 0:
        print(f"BUG REPRODUCED: {total_hits} matching log line(s) across cluster")
        sys.exit(0)
    else:
        print("No matching log lines found.")
        print("Try: longer pre-restart sleep, more workers, or verify log paths.")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
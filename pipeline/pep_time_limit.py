"""Run one command under a wall-clock limit and record whether the LIMIT ended it.

pep-integration.yml runs ``run_pep_tf.sh`` through this wrapper so one hung test run cannot
hold a release for GitHub's six-hour default:

    python3 pipeline/pep_time_limit.py --limit-seconds 2700 --grace-seconds 120 \
        --outcome-file "$GITHUB_OUTPUT" -- ./run_pep_tf.sh ...

It appends ``timed_out=true|false`` to the outcome file, so the Summarize step reads an explicit
flag and never infers a timeout from an exit code (137, for example, also follows an
out-of-memory kill). The exit status is the command's own (128+N when signal N ended it), or
124 when the limit ended it.

The command runs in its own process group. At the limit the whole group gets SIGTERM, and
SIGKILL if anything is still running after the grace period, so pytest and ``docker exec``
children stop too. A SIGTERM or SIGINT sent to the wrapper (a cancelled job) is passed on to
the group. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

TIMEOUT_EXIT = 124
_NOT_STARTED_EXIT = 127
_POLL_SECONDS = 0.2


def _signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _status(returncode):
    """A Popen return code as a shell exit status (signal N -> 128+N)."""
    return 128 - returncode if returncode < 0 else returncode


def _stop_group(proc, grace_seconds):
    """SIGTERM the command's process group, then SIGKILL whatever is left after the grace."""
    pgid = proc.pid
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
    if proc.poll() is None or _group_alive(pgid):
        _signal_group(pgid, signal.SIGKILL)
    proc.wait()


def run_with_limit(command, limit_seconds, grace_seconds):
    """Run ``command`` and return ``(exit_status, timed_out)``. ``timed_out`` is True only
    when this wrapper's limit ended the command."""
    try:
        proc = subprocess.Popen(command, start_new_session=True)
    except OSError as e:
        print("pep_time_limit: cannot start %r: %s" % (command[0], e.strerror or e), file=sys.stderr)
        return _NOT_STARTED_EXIT, False

    def _forward(signum, _frame):
        _signal_group(proc.pid, signum)

    previous = {s: signal.signal(s, _forward) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            return _status(proc.wait(timeout=limit_seconds)), False
        except subprocess.TimeoutExpired:
            print("pep_time_limit: the %d-second limit was reached; stopping the test run"
                  % limit_seconds, file=sys.stderr)
            _stop_group(proc, grace_seconds)
            return TIMEOUT_EXIT, True
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run a command under a wall-clock limit")
    ap.add_argument("--limit-seconds", type=int, required=True)
    ap.add_argument("--grace-seconds", type=int, default=120)
    ap.add_argument("--outcome-file", required=True,
                    help="file to append 'timed_out=true|false' to (e.g. $GITHUB_OUTPUT)")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="-- command [args...]")
    args = ap.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ap.error("a command is required after --")
    if args.limit_seconds <= 0 or args.grace_seconds < 0:
        ap.error("--limit-seconds must be positive and --grace-seconds not negative")

    status, timed_out = run_with_limit(command, args.limit_seconds, args.grace_seconds)
    with open(args.outcome_file, "a") as f:
        f.write("timed_out=%s\n" % ("true" if timed_out else "false"))
    return status


if __name__ == "__main__":
    sys.exit(main())

"""Tests for pipeline/pep_time_limit.py and the test-run time limit in pep-integration.yml.

The behavioural tests run real child processes with second-scale limits; the contract tests
read the workflow. Together they pin three facts: the limit, not an exit code, decides whether
a run timed out; a run that ends any other way keeps its own status; and the job keeps enough
time after the limit for the summary and upload steps (best effort after a hard job timeout).
"""
import importlib.util
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "pipeline" / "pep_time_limit.py"
_spec = importlib.util.spec_from_file_location("pep_time_limit", str(_SCRIPT))
ptl = importlib.util.module_from_spec(_spec)
sys.modules["pep_time_limit"] = ptl
_spec.loader.exec_module(ptl)

_PY = sys.executable


def _py(code):
    return [_PY, "-c", code]


def _gone(pid, within=5.0):
    """True once ``pid`` no longer exists (reaped or never reachable), polling briefly."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
# behaviour
# --------------------------------------------------------------------------- #
def test_a_command_that_finishes_in_time_keeps_its_status():
    assert ptl.run_with_limit(_py("pass"), 30, 5) == (0, False)
    assert ptl.run_with_limit(_py("import sys; sys.exit(3)"), 30, 5) == (3, False)


def test_reaching_the_limit_stops_the_command_and_is_reported_as_a_timeout():
    start = time.monotonic()
    status, timed_out = ptl.run_with_limit(_py("import time; time.sleep(60)"), 1, 5)
    assert (status, timed_out) == (ptl.TIMEOUT_EXIT, True)
    assert time.monotonic() - start < 15


def test_a_group_that_ignores_sigterm_is_killed_after_the_grace_period(tmp_path):
    # The command ignores SIGTERM and starts a child of its own: both must be gone.
    pidfile = tmp_path / "child.pid"
    code = (
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(%r, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n" % str(pidfile)
    )
    start = time.monotonic()
    status, timed_out = ptl.run_with_limit(_py(code), 2, 1)
    assert (status, timed_out) == (ptl.TIMEOUT_EXIT, True)
    assert time.monotonic() - start < 20
    assert _gone(int(pidfile.read_text()))


def test_a_kill_that_is_not_the_limit_is_not_a_timeout():
    # Exit 137 also follows an out-of-memory kill: only the limit itself means "timed out".
    status, timed_out = ptl.run_with_limit(_py("import os, signal; os.kill(os.getpid(), signal.SIGKILL)"), 30, 5)
    assert (status, timed_out) == (137, False)


def test_a_command_that_cannot_start_is_not_a_timeout(tmp_path):
    assert ptl.run_with_limit([str(tmp_path / "no-such-command")], 30, 5) == (127, False)


def test_main_writes_the_outcome_line_and_returns_the_status(tmp_path):
    out = tmp_path / "github_output"
    out.write_text("exit_code=0\n")                      # appended to, never truncated
    rc = ptl.main(["--limit-seconds", "30", "--grace-seconds", "5", "--outcome-file", str(out),
                   "--", *_py("import sys; sys.exit(4)")])
    assert rc == 4
    assert out.read_text() == "exit_code=0\ntimed_out=false\n"


def test_main_records_a_timeout(tmp_path):
    out = tmp_path / "github_output"
    rc = ptl.main(["--limit-seconds", "1", "--grace-seconds", "2", "--outcome-file", str(out),
                   "--", *_py("import time; time.sleep(60)")])
    assert rc == ptl.TIMEOUT_EXIT
    assert out.read_text() == "timed_out=true\n"


@pytest.mark.parametrize("argv", [
    ["--limit-seconds", "30", "--outcome-file", "x"],                               # no command
    ["--limit-seconds", "0", "--outcome-file", "x", "--", "true"],                  # no limit
    ["--limit-seconds", "30", "--grace-seconds", "-1", "--outcome-file", "x", "--", "true"],
])
def test_main_rejects_an_unusable_request(argv):
    with pytest.raises(SystemExit) as e:
        ptl.main(argv)
    assert e.value.code == 2


def test_termination_of_the_wrapper_is_passed_to_the_command(tmp_path):
    # A cancelled job signals the wrapper; the command (in its own process group) must stop too.
    ready = tmp_path / "ready"
    code = "import time; open(%r, 'w').write('1'); time.sleep(60)" % str(ready)
    out = tmp_path / "github_output"
    proc = subprocess.Popen([_PY, str(_SCRIPT), "--limit-seconds", "60", "--grace-seconds", "5",
                             "--outcome-file", str(out), "--", *_py(code)])
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready.exists()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=15) == 128 + signal.SIGTERM
    assert out.read_text() == "timed_out=false\n"


# --------------------------------------------------------------------------- #
# workflow contract (pep-integration.yml), read as text: stdlib only, like the
# other workflow contract tests (PyYAML is not a declared test dependency)
# --------------------------------------------------------------------------- #
_WF_TEXT = (_REPO / ".github" / "workflows" / "pep-integration.yml").read_text()


def _step(name_prefix):
    """The text of one step of the ``run`` job, from ``- name: <prefix>`` to the next step."""
    out, capturing = [], False
    for ln in _WF_TEXT.splitlines(keepends=True):
        if re.match(r"^      - name: %s" % re.escape(name_prefix), ln):
            capturing = True
        elif capturing and re.match(r"^      - (name|uses):", ln):
            break
        if capturing:
            out.append(ln)
    return "".join(out)


def _job_timeout_minutes():
    header = _WF_TEXT.split("\n    steps:\n", 1)[0]          # the run job's settings, before its steps
    found = re.findall(r"^    timeout-minutes: (\d+)\s*$", header, flags=re.M)
    assert len(found) == 1
    return int(found[0])


def test_job_has_a_60_minute_backstop():
    assert _job_timeout_minutes() == 60


def test_framework_runs_under_the_45_minute_limit_with_room_left_for_reporting():
    fw = _step("Run framework")
    assert re.search(r"^          FRAMEWORK_LIMIT_SECONDS: 2700\b", fw, flags=re.M)
    assert ('python3 pipeline/pep_time_limit.py --limit-seconds "$FRAMEWORK_LIMIT_SECONDS" '
            '--grace-seconds 120 --outcome-file "$GITHUB_OUTPUT" -- ./run_pep_tf.sh "${args[@]}"') in fw
    assert fw.count("./run_pep_tf.sh") == 1                   # never run outside the limit
    # Limit + grace + 10 minutes for Summarize/Emit/Upload/Report stays inside the backstop.
    assert 2700 + 120 + 600 <= _job_timeout_minutes() * 60


def test_summarize_reports_a_timeout_only_from_the_explicit_flag():
    run = _step("Summarize (ALWAYS)")
    assert "FW_TIMED_OUT: ${{ steps.fw.outputs.timed_out }}" in run
    branch = run.index('elif [ "$FW_TIMED_OUT" = "true" ]; then')
    assert run.index('elif [ "$DOCKER_OUTCOME" = "failure" ]') < branch < run.index('elif [ "$FW_CODE" = "3" ]')
    assert '--infra-error "test run exceeded its 45-minute limit (timeout)"' in run[branch:]
    # No exit code is taken as proof of a timeout (137 also follows an out-of-memory kill).
    assert re.search(r'FW_CODE"?\s*(=|!=|-eq|-ne)\s*"?(124|137)\b', run) is None

"""Layout guards for the certification code in pipeline/ (stdlib only).

The certification modules, their catalog, tests and fixtures moved from utillities/ to pipeline/.
These checks keep that move honest: no file still points at an old location, every script and
data path a workflow runs exists, each smoke workflow's push filter covers all the code it
exercises (a stale filter silently stops the smoke from triggering), and the dependency direction
stays pipeline/ -> utillities/.
"""
import ast
import fnmatch
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_PIPELINE = _REPO / "pipeline"
_UTIL = _REPO / "utillities"
_WF = _REPO / ".github" / "workflows"
_RECEIPT_BUILD = _REPO / ".github" / "actions" / "pep-package-receipt" / "receipt_build.py"

# Text that can name a repository path: workflows, the action, docs, code and fixtures.
_SCANNED = [_REPO / ".github", _REPO / "docs", _REPO / "README.md", _UTIL, _PIPELINE,
            _REPO / "aspects", _REPO / "component-test", _REPO / "run_pep_tf.sh"]
_TEXT_SUFFIXES = {".py", ".yml", ".yaml", ".md", ".json", ".sh", ".txt", ""}


def _moved_names():
    """The names that used to live directly in utillities/: modules and the catalog, the test
    files, and the fixture directories."""
    names = {p.name for p in _PIPELINE.iterdir() if p.is_file()}
    tests = _PIPELINE / "tests"
    names |= {p.name for p in tests.iterdir() if p.name.startswith("test_") or p.is_dir()}
    return sorted(n for n in names if not n.startswith((".", "__")))


def _text_files():
    for root in _SCANNED:
        paths = [root] if root.is_file() else root.rglob("*")
        for p in paths:
            if p.is_file() and p.suffix in _TEXT_SUFFIXES and "__pycache__" not in p.parts:
                yield p


def test_moved_names_are_the_certification_set():
    names = _moved_names()
    assert "pep_capture.py" in names and "pep_exec_catalog.json" in names
    assert "test_pep_capture.py" in names and "cert_plan_fixtures" in names
    assert "pep_capture_policy.json" not in names        # the shared component policy stays put


def test_no_moved_file_is_left_or_duplicated_in_utillities():
    left = [n for n in _moved_names() if (_UTIL / n).exists()]
    assert left == []


def test_no_reference_to_a_moved_file_under_its_old_path():
    stale = re.compile(r"utillities/(%s)\b" % "|".join(re.escape(n) for n in _moved_names()))
    hits = []
    for p in _text_files():
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if stale.search(line):
                hits.append("%s:%d" % (p.relative_to(_REPO), i))
    assert hits == []


def test_every_workflow_script_and_data_path_exists():
    refs = set()
    for wf in sorted(_WF.glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        refs |= {(wf.name, m) for m in re.findall(r"python3\s+((?:pipeline|utillities)/[\w./-]+)", text)}
        refs |= {(wf.name, m) for m in re.findall(r"--(?:policy|exec-catalog)\s+([\w./-]+)", text)}
    assert ("pep-certify.yml", "pipeline/pep_invocation_plan.py") in refs
    assert ("pep-capture.yml", "utillities/pep_capture_policy.json") in refs
    missing = sorted("%s: %s" % r for r in refs if not (_REPO / r[1]).is_file())
    assert missing == []


def _push_paths(workflow):
    """The push trigger's `paths:` entries (a flat list of quoted strings)."""
    text = (_WF / workflow).read_text(encoding="utf-8")
    block = re.search(r"^  push:\n    paths:\n((?:      - .*\n)+)", text, re.M)
    assert block, workflow
    return [m.strip("'\"") for m in re.findall(r"- (\S+)", block.group(1))]


def _covered(path, patterns):
    return any(fnmatch.fnmatch(path, pat.replace("**", "*")) for pat in patterns)


def _local_modules(path):
    """Repository modules that `path` imports or loads by file name (pipeline/ or utillities/)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.endswith(".py"):
            names.add(node.value[:-3])
    found = set()
    for n in names:
        for d in (_PIPELINE, _UTIL):
            if (d / (n + ".py")).is_file():
                found.add(d / (n + ".py"))
    return found


def _closure(entries):
    seen, todo = set(), list(entries)
    while todo:
        p = todo.pop()
        if p not in seen:
            seen.add(p)
            todo.extend(_local_modules(p) - seen)
    return seen


def test_smoke_filters_name_existing_paths():
    for wf in ("pep-capture-smoke.yml", "pep-receipt-smoke.yml"):
        for pat in _push_paths(wf):
            if "*" in pat:
                assert list(_REPO.glob(pat)), (wf, pat)
            else:
                assert (_REPO / pat).is_file(), (wf, pat)


def test_capture_smoke_filter_covers_the_capture_code_and_policy():
    text = (_WF / "pep-capture.yml").read_text(encoding="utf-8")
    entries = [_REPO / s for s in re.findall(r"python3\s+((?:pipeline|utillities)/[\w./-]+\.py)", text)]
    assert entries == [_PIPELINE / "pep_capture_io.py"]
    needed = {str(p.relative_to(_REPO)) for p in _closure(entries)}
    needed |= set(re.findall(r"--policy\s+([\w./-]+)", text))
    needed |= {str(p.relative_to(_REPO)) for p in _closure([_RECEIPT_BUILD])}
    assert "pipeline/pep_evidence_class.py" in needed and "pipeline/pep_pkg_inspect.py" in needed
    patterns = _push_paths("pep-capture-smoke.yml")
    assert sorted(p for p in needed if not _covered(p, patterns)) == []


def test_receipt_smoke_filter_covers_the_receipt_action_code():
    needed = {str(p.relative_to(_REPO)) for p in _closure([_RECEIPT_BUILD])}
    assert "pipeline/pep_pkg_inspect.py" in needed
    patterns = _push_paths("pep-receipt-smoke.yml")
    assert sorted(p for p in needed if not _covered(p, patterns)) == []


def test_utillities_never_imports_pipeline():
    offenders = []
    for p in sorted(_UTIL.glob("*.py")):
        mods = sorted(m.name for m in _local_modules(p) if m.parent == _PIPELINE)
        paths = [n.value for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and (n.value == "pipeline" or n.value.startswith("pipeline/"))]
        if mods or paths:
            offenders.append((p.name, mods, paths))
    assert offenders == []


def test_selftest_runs_the_pipeline_suites():
    text = (_WF / "pep-selftest.yml").read_text(encoding="utf-8")
    unit = [l for l in text.splitlines() if l.strip().startswith("run: pytest ")]
    assert len(unit) == 1 and " pipeline/tests/" in unit[0]

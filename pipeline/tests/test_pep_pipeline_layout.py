"""Layout guards for the integration code in pipeline/ and aspects/pipeline/ (stdlib only).

The integration and certification steps (modules, the exec catalog, tests and fixtures) moved from
utillities/ to pipeline/, and the shared integration helpers with the component policy moved to
aspects/pipeline/. These checks keep the moves honest: no file still points at an old location, no
module or policy exists twice, every script and data path a workflow or the shell runner uses
exists, each smoke workflow's push filter covers all the code it exercises (a stale filter silently
stops the smoke from triggering), the Self-Test selection still collects every unit-test file, and
the dependency direction stays pipeline/ -> aspects/pipeline/ -> utillities/.
"""
import ast
import fnmatch
import re
import shlex
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_PIPELINE = _REPO / "pipeline"
_HELPERS = _REPO / "aspects" / "pipeline"
_UTIL = _REPO / "utillities"
_WF = _REPO / ".github" / "workflows"
_RUNNER = _REPO / "run_pep_tf.sh"
_RECEIPT_BUILD = _REPO / ".github" / "actions" / "pep-package-receipt" / "receipt_build.py"
_CODE_DIRS = (_PIPELINE, _HELPERS, _UTIL)
_PATH_RE = r"(?:aspects/pipeline|pipeline|utillities)/[\w./-]+"

# Text that can name a repository path: workflows, the action, docs, code and fixtures.
_SCANNED = [_REPO / ".github", _REPO / "docs", _REPO / "README.md", _UTIL, _PIPELINE,
            _REPO / "aspects", _REPO / "component-test", _RUNNER]
_TEXT_SUFFIXES = {".py", ".yml", ".yaml", ".md", ".json", ".sh", ".txt", ""}


def _moved_names():
    """The names that used to live directly in utillities/: modules, the catalog and the policy,
    the test files, and the fixture directories."""
    names = set()
    for d in (_PIPELINE, _HELPERS):
        names |= {p.name for p in d.iterdir() if p.is_file()}
        names |= {p.name for p in (d / "tests").iterdir() if p.name.startswith("test_") or p.is_dir()}
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
    assert not (_PIPELINE / "pep_capture_policy.json").exists()   # the one component policy is a shared helper


def test_moved_names_include_the_cli_steps_and_shared_helpers():
    names = _moved_names()
    for n in ("pep_resolve_cli.py", "pep_result_summary.py", "test_pep_bridge.py",
              "pep_request.py", "pep_verify.py", "pep_capture_policy.json", "test_pep_rag_wiring.py"):
        assert n in names, n
    for n in ("container_resolver.py", "ci_consolidated_report.py"):      # shared with the regression; stay
        assert n not in names and (_UTIL / n).is_file(), n


def test_no_moved_file_is_left_or_duplicated_in_utillities():
    left = [n for n in _moved_names() if (_UTIL / n).exists()]
    assert left == []


def test_no_module_or_policy_exists_twice():
    seen = {}
    for d in _CODE_DIRS:
        for p in list(d.glob("*.py")) + list(d.glob("*.json")):
            seen.setdefault(p.name, []).append(str(d.relative_to(_REPO)))
    assert {n: ds for n, ds in seen.items() if len(ds) > 1} == {}
    policies = [p for p in _REPO.rglob("pep_capture_policy.json") if "venv" not in p.parts]
    assert policies == [_HELPERS / "pep_capture_policy.json"]


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
    for f in sorted(_WF.glob("*.yml")) + [_RUNNER]:
        text = f.read_text(encoding="utf-8")
        refs |= {(f.name, m) for m in re.findall(r"python3\s+(%s)" % _PATH_RE, text)}
        refs |= {(f.name, m) for m in re.findall(r"--(?:policy|exec-catalog)\s+([\w./-]+)", text)}
    for expected in (("pep-certify.yml", "pipeline/pep_invocation_plan.py"),
                     ("pep-capture.yml", "aspects/pipeline/pep_capture_policy.json"),
                     ("pep-integration.yml", "pipeline/pep_result_summary.py"),
                     ("run_pep_tf.sh", "pipeline/pep_resolve_cli.py"),
                     ("run_pep_tf.sh", "aspects/pipeline/pep_request_env.py"),
                     ("run_pep_tf.sh", "utillities/container_resolver.py")):
        assert expected in refs, expected
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
    """Repository modules that `path` imports or loads by file name (pipeline/, aspects/pipeline/
    or utillities/; module names are unique across them, see test_no_module_or_policy_exists_twice)."""
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
        for d in _CODE_DIRS:
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
    entries = [_REPO / s for s in re.findall(r"python3\s+(%s)" % _PATH_RE, text)]
    assert entries == [_PIPELINE / "pep_capture_io.py"]
    needed = {str(p.relative_to(_REPO)) for p in _closure(entries)}
    needed |= set(re.findall(r"--policy\s+([\w./-]+)", text))
    needed |= {str(p.relative_to(_REPO)) for p in _closure([_RECEIPT_BUILD])}
    assert "pipeline/pep_evidence_class.py" in needed and "pipeline/pep_pkg_inspect.py" in needed
    assert "aspects/pipeline/pep_capture_policy.json" in needed
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


def test_shared_helpers_never_depend_on_pipeline_orchestration():
    # Runtime code under aspects/ (the aspects themselves and the shared helpers) may use
    # utillities/ but never the orchestration in top-level pipeline/. Test-only cross-layer
    # checks under aspects/pipeline/tests/ are allowed.
    offenders = []
    for p in sorted((_REPO / "aspects").rglob("*.py")):
        if "tests" in p.relative_to(_REPO).parts or "__pycache__" in p.parts:
            continue
        mods = sorted(m.name for m in _closure([p]) if m.parent == _PIPELINE)
        if mods:
            offenders.append((str(p.relative_to(_REPO)), mods))
    assert offenders == []


def _selftest_unit_args():
    text = (_WF / "pep-selftest.yml").read_text(encoding="utf-8")
    unit = [l for l in text.splitlines() if l.strip().startswith("run: pytest ")]
    assert len(unit) == 1
    return [a for a in shlex.split(unit[0].strip()[len("run: pytest "):]) if not a.startswith("-")]


def test_selftest_runs_the_pipeline_suites():
    args = _selftest_unit_args()
    assert "aspects/pipeline/tests/" in args and "pipeline/tests/" in args


def test_selftest_selection_collects_every_unit_test_file():
    args = _selftest_unit_args()
    for a in args:                                   # no wildcard that matches nothing (pytest exit 4)
        assert list(_REPO.glob(a.rstrip("/"))), a
    selected = set()
    for a in args:
        for p in _REPO.glob(a.rstrip("/")):
            selected |= set(p.rglob("test_*.py")) if p.is_dir() else {p}
    unit_files = set(_UTIL.glob("test_*.py"))
    for d in (_PIPELINE, _REPO / "aspects"):
        unit_files |= {p for p in d.rglob("test_*.py") if "tests" in p.relative_to(_REPO).parts}
    assert sorted(str(p.relative_to(_REPO)) for p in unit_files - selected) == []

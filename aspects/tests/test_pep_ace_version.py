"""ACE version checks (component-test/test_pep_ace.py).

Docker-free: the component test is loaded by path with docker.from_env neutralized (the approach
of aspects/pipeline/tests/test_pep_rag_wiring.py) and its REAL test functions are run against fake
containers. For `ace --version` the fake answers the way docker exec does: without demux, stderr
and stdout come back as one stream; with demux, as a (stdout, stderr) pair, each None when empty.

ACE prints "ace version <tag without v>" on stdout (3.0.0, 3.0.0-rc.3). Covers the binary check:
an exact match on every channel, so a GA expectation rejects RC, dirty and development builds; an
explicitly requested pre-release; the version read from stdout only; and missing, malformed or
ambiguous output rejected. Also covers the existing package check against the 3.0.0-rc.3 RPM and
DEB packages published in staging and the released 3.0.0 packages (fixed examples of the package
formats), and that the standalone configs name one version the checks can match, whichever release
that is.
"""
import importlib.util
import itertools
import os
import re
import sys

import docker
import pytest

_ACE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "component-test", "test_pep_ace.py")
_counter = itertools.count()


def load(monkeypatch, expected, repo="release"):
    """A fresh copy of the ACE component test, reading PGEDGE_ACE_VERSION/REPO at import."""
    monkeypatch.setenv("PGEDGE_ACE_VERSION", expected)
    monkeypatch.setenv("REPO", repo)
    monkeypatch.setattr(docker, "from_env", lambda *a, **k: None)
    name = "ace_under_test_%d" % next(_counter)
    spec = importlib.util.spec_from_file_location(name, _ACE_PATH)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


class _Container:
    status = "running"

    def __init__(self, stdout, stderr="", exit_code=0):
        self.out = stdout.encode() or None
        self.err = stderr.encode() or None
        self.exit_code = exit_code

    def exec_run(self, cmd, user=None, demux=False, **kwargs):
        if demux:
            return self.exit_code, (self.out, self.err)
        return self.exit_code, (self.err or b"") + (self.out or b"")


class _PackageContainer:
    """Answers verify_package_version's package-manager probe and pgedge-ace version query."""
    status = "running"

    def __init__(self, family, installed):
        self.family, self.installed = family, installed

    def exec_run(self, cmd, user=None, **kwargs):
        script = cmd[-1]
        if script == "command -v dnf":
            return (0 if self.family == "rpm" else 1), b""
        if script == "command -v apt-get":
            return (0 if self.family == "deb" else 1), b""
        assert "pgedge-ace" in script
        return 0, self.installed.encode()


class _Client:
    def __init__(self, container):
        self.containers = type("Containers", (), {"get": lambda _self, name: container})()


def run(mod, container, test="test_ace_binary_version", container_type="rhel"):
    """(outcome, message) of the real component test ``test`` on ``container``."""
    mod.client = _Client(container)
    try:
        getattr(mod, test)("el9-amd64", container_type)
    except (AssertionError, pytest.fail.Exception) as e:
        return "failed", str(e)
    except pytest.skip.Exception as e:
        return "skipped", str(e)
    return "passed", ""


@pytest.mark.parametrize("repo", ["release", "staging", "daily"])
@pytest.mark.parametrize("reported", ["3.0.0-rc.2", "3.0.0-dirty", "3.0.0-5-gabc1234", "3.0.0-dev"])
def test_a_ga_expectation_rejects_a_suffixed_binary_on_every_channel(monkeypatch, repo, reported):
    outcome, message = run(load(monkeypatch, "3.0.0", repo), _Container("ace version %s\n" % reported))
    assert outcome == "failed"
    assert reported in message


@pytest.mark.parametrize("repo", ["release", "staging"])
def test_the_exact_ga_version_passes(monkeypatch, repo):
    assert run(load(monkeypatch, "3.0.0", repo), _Container("ace version 3.0.0\n"))[0] == "passed"


@pytest.mark.parametrize("repo", ["staging", "daily"])
def test_an_explicitly_requested_prerelease_must_match_exactly(monkeypatch, repo):
    mod = load(monkeypatch, "3.0.0-rc.3", repo)
    assert run(mod, _Container("ace version 3.0.0-rc.3\n"))[0] == "passed"
    assert run(mod, _Container("ace version 3.0.0-rc.2\n"))[0] == "failed"
    assert run(mod, _Container("ace version 3.0.0\n"))[0] == "failed"


@pytest.mark.parametrize("stdout", [
    "ace version v3.0.0\n",
    "ace version 3.0.0 (commit abc1234)\n",
    "using built-in defaults\nace version 3.0.0\n",
])
def test_the_ace_version_line_is_found_and_parsed(monkeypatch, stdout):
    assert run(load(monkeypatch, "3.0.0"), _Container(stdout))[0] == "passed"


def test_a_warning_on_stderr_does_not_hide_the_version(monkeypatch):
    container = _Container("ace version 3.0.0\n", stderr="WARN config file 'ace.yaml' not found\n")
    assert run(load(monkeypatch, "3.0.0"), container)[0] == "passed"


@pytest.mark.parametrize("stdout,stderr", [
    ("", ""),
    ("", "ace version 3.0.0\n"),
    ("ace version\n", ""),
    ("ace version 3.0\n", ""),
    ("ace version unknown\n", ""),
    ("ace 3.0.0\n", ""),
    ("ace version 3.0.0\nace version 3.0.0-rc.3\n", ""),
    ("ace version 3.0.0\nace version 3.0.0\n", ""),
], ids=["no-output", "version-only-on-stderr", "no-version", "two-part", "not-a-version",
        "no-version-label", "two-versions", "repeated-line"])
def test_missing_malformed_or_ambiguous_output_fails(monkeypatch, stdout, stderr):
    assert run(load(monkeypatch, "3.0.0"), _Container(stdout, stderr))[0] == "failed"


def test_a_failing_version_command_fails(monkeypatch):
    assert run(load(monkeypatch, "3.0.0"), _Container("ace version 3.0.0\n", exit_code=1))[0] == "failed"


def test_no_configured_version_skips(monkeypatch):
    assert run(load(monkeypatch, ""), _Container("ace version 3.0.0\n"))[0] == "skipped"


# pgedge-ace 3.0.0-rc.3 as published in staging: RPM release rc3_1, DEB upstream 3.0.0~rc3.
@pytest.mark.parametrize("family,container_type,installed", [
    ("rpm", "rhel", "3.0.0-rc3_1.el9"), ("rpm", "rhel", "3.0.0-rc3_1.el10"),
    ("deb", "deb", "3.0.0~rc3-1.jammy"), ("deb", "deb", "3.0.0~rc3-1.noble"),
    ("deb", "deb", "3.0.0~rc3-1.bookworm"), ("deb", "deb", "3.0.0~rc3-1.trixie"),
])
def test_the_package_check_accepts_the_published_rc3_packages(monkeypatch, family, container_type,
                                                               installed):
    mod = load(monkeypatch, "3.0.0-rc.3", "staging")
    outcome = run(mod, _PackageContainer(family, installed), "test_component_package_version",
                  container_type)[0]
    assert outcome == "passed"


@pytest.mark.parametrize("family,container_type,installed", [
    ("rpm", "rhel", "3.0.0-1.el9"), ("deb", "deb", "3.0.0-1.bookworm"),
    ("rpm", "rhel", "2.1.1-1.el9"), ("deb", "deb", "2.1.1-1.jammy"),
])
def test_the_package_check_rejects_ga_and_release_packages_for_an_rc3_expectation(
        monkeypatch, family, container_type, installed):
    mod = load(monkeypatch, "3.0.0-rc.3", "staging")
    outcome = run(mod, _PackageContainer(family, installed), "test_component_package_version",
                  container_type)[0]
    assert outcome == "failed"


# A standalone run (run_pep_tf.sh) sources configuration/config<PG>.env, and the ACE package and
# binary checks then expect its PGEDGE_ACE_VERSION exactly. A release run ignores that value and
# expects the request's tag (test_pep_ace_wiring.py). The current ACE release is therefore named
# only in those config files: these tests check that the files name one version the checks can
# match, not which version, so a consistent bump of the configs needs no edit here.
_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "configuration")
_ACE_SETTING = re.compile(r"^export PGEDGE_ACE_VERSION=(.*)$", re.M)


def _config_names(config_dir):
    return sorted(n for n in os.listdir(config_dir) if re.fullmatch(r"config\d+\.env", n))


def ace_config_problems(config_dir, accepts):
    """Problems with the standalone ACE expectation in config_dir's config<N>.env files. Each file
    must set PGEDGE_ACE_VERSION exactly once (unset, the version checks skip) to a value ``accepts``
    (the binary check can be satisfied with it), and the files must agree, because one
    PostgreSQL-independent pgedge-ace package serves every PostgreSQL major."""
    names = _config_names(config_dir)
    if not names:
        return ["no config<N>.env file in %s" % config_dir]
    problems, values = [], {}
    for name in names:
        with open(os.path.join(config_dir, name)) as f:
            found = _ACE_SETTING.findall(f.read())
        if len(found) != 1:
            problems.append("%s sets PGEDGE_ACE_VERSION %d times, not once" % (name, len(found)))
        elif not accepts(found[0]):
            problems.append("%s: PGEDGE_ACE_VERSION=%r is not a version the binary check can match"
                            % (name, found[0]))
        else:
            values[name] = found[0]
    if len(set(values.values())) > 1:
        problems.append("the configs expect different ACE versions: %s" % values)
    return problems


def _accepts(monkeypatch):
    """Whether the real binary check, expecting a value, passes a binary reporting exactly it."""
    return lambda value: run(load(monkeypatch, value),
                             _Container("ace version %s\n" % value))[0] == "passed"


def _set(value):
    """A rewrite of a config's text that sets PGEDGE_ACE_VERSION to ``value``."""
    return lambda text: _ACE_SETTING.sub(lambda _: "export PGEDGE_ACE_VERSION=" + value, text)


def _configs(tmp_path, value):
    """Copies of the real config<N>.env files with PGEDGE_ACE_VERSION set to ``value``."""
    for name in _config_names(_CONFIG_DIR):
        with open(os.path.join(_CONFIG_DIR, name)) as f:
            (tmp_path / name).write_text(_set(value)(f.read()))
    return str(tmp_path)


def _rewrite_last(config_dir, rewrite):
    """Rewrite the last config file (by name), returning its name."""
    name = _config_names(config_dir)[-1]
    path = os.path.join(config_dir, name)
    with open(path) as f:
        text = rewrite(f.read())
    with open(path, "w") as f:
        f.write(text)
    return name


def test_the_standalone_configs_name_one_usable_ace_version(monkeypatch):
    assert ace_config_problems(_CONFIG_DIR, _accepts(monkeypatch)) == []


@pytest.mark.parametrize("value", ["3.0.0", "3.0.1", "3.1.0", "3.0.1-rc.1", "4.0.0-beta.2"])
def test_a_consistent_version_bump_needs_no_edit_here(monkeypatch, tmp_path, value):
    assert ace_config_problems(_configs(tmp_path, value), _accepts(monkeypatch)) == []


@pytest.mark.parametrize("rewrite", [
    lambda text: _ACE_SETTING.sub("", text),
    lambda text: _ACE_SETTING.sub(r"# \g<0>", text),
    lambda text: text + "export PGEDGE_ACE_VERSION=3.0.1\n",
], ids=["missing", "commented-out", "set-twice"])
def test_a_config_that_does_not_set_the_version_once_is_caught(monkeypatch, tmp_path, rewrite):
    config_dir = _configs(tmp_path, "3.0.1")
    name = _rewrite_last(config_dir, rewrite)
    problems = ace_config_problems(config_dir, _accepts(monkeypatch))
    assert len(problems) == 1 and problems[0].startswith(name), problems


@pytest.mark.parametrize("value", ["", "3.0", "v3.0.1", "latest", "3.0.1-"],
                         ids=["empty", "two-part", "leading-v", "not-a-version", "empty-suffix"])
def test_a_malformed_version_is_caught(monkeypatch, tmp_path, value):
    config_dir = _configs(tmp_path, "3.0.1")
    name = _rewrite_last(config_dir, _set(value))
    problems = ace_config_problems(config_dir, _accepts(monkeypatch))
    assert len(problems) == 1 and problems[0].startswith(name), problems
    assert repr(value) in problems[0], problems


@pytest.mark.parametrize("other", ["3.0.0", "3.0.1-rc.1"])
def test_configs_that_disagree_are_caught(monkeypatch, tmp_path, other):
    config_dir = _configs(tmp_path, "3.0.1")
    name = _rewrite_last(config_dir, _set(other))
    problems = ace_config_problems(config_dir, _accepts(monkeypatch))
    assert len(problems) == 1 and "different ACE versions" in problems[0], problems
    assert "%r: %r" % (name, other) in problems[0], problems


def test_no_config_files_is_caught(monkeypatch, tmp_path):
    assert ace_config_problems(str(tmp_path), _accepts(monkeypatch)) == [
        "no config<N>.env file in %s" % tmp_path]


# pgedge-ace 3.0.0 as published on every native ACE platform: RPM release 1.el9/1.el10, DEB 3.0.0-1.
@pytest.mark.parametrize("family,container_type,installed", [
    ("rpm", "rhel", "3.0.0-1.el9"), ("rpm", "rhel", "3.0.0-1.el10"),
    ("deb", "deb", "3.0.0-1.jammy"), ("deb", "deb", "3.0.0-1.noble"), ("deb", "deb", "3.0.0-1.resolute"),
    ("deb", "deb", "3.0.0-1.bookworm"), ("deb", "deb", "3.0.0-1.trixie"),
])
def test_the_package_check_accepts_the_released_3_0_0_packages(monkeypatch, family, container_type,
                                                                 installed):
    mod = load(monkeypatch, "3.0.0", "release")
    outcome = run(mod, _PackageContainer(family, installed), "test_component_package_version",
                  container_type)[0]
    assert outcome == "passed"

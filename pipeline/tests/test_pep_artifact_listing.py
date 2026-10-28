"""Tests for pipeline/pep_artifact_listing.py: the aggregate's artifact listing, read
through the capture transport so it shares capture's retry classification."""
import email.message
import io
import json
import urllib.error

import pytest

import pep_artifact_listing as L
import pep_capture_io as IO

_PAGE1 = "https://api.github.com/repos/o/r/actions/runs/7/artifacts?per_page=100&page=1"
_PAGE2 = "https://api.github.com/repos/o/r/actions/runs/7/artifacts?per_page=100&page=2"


class _Resp:
    def __init__(self, body):
        self._b = io.BytesIO(body)

    def read(self, n=-1):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _status(code):
    return urllib.error.HTTPError(_PAGE1, code, "msg", email.message.Message(), None)


def _art(i, name, expired=False):
    return {"id": i, "name": name, "expired": expired, "size_in_bytes": 10 * i,
            "archive_download_url": "https://api.github.com/x/%d/zip" % i, "digest": "sha256:" + "a" * 64}


@pytest.fixture
def network(monkeypatch):
    """Serve scripted outcomes per URL through the REAL transport; record calls and waits."""
    calls, waits, plan = [], [], {}

    def fake(req, timeout):
        calls.append(req.full_url)
        seq = plan[req.full_url]
        outcome = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(json.dumps(outcome).encode())

    monkeypatch.setattr(IO, "_urlopen", fake)
    monkeypatch.setattr(IO, "_sleep", waits.append)
    return plan, calls, waits


def _run(tmp_path, argv_extra=(), env=None):
    out = tmp_path / "github_output"
    listing = tmp_path / "listing.json"
    rc = L.main(["--repo", "o/r", "--run-id", "7", "--name-prefix", "pep-summary-",
                 "--listing", str(listing), "--github-output", str(out), *argv_extra],
                env={"GH_TOKEN": "TKN"} if env is None else env)
    return rc, listing, out


def test_listing_is_normalized_and_download_ids_skip_expired_and_other_names(tmp_path, network):
    plan, calls, _ = network
    first = [_art(i, "pep-summary-x-%d" % i) for i in range(1, 100)] + [_art(100, "pep-summary-old", expired=True)]
    plan[_PAGE1] = [{"total_count": 102, "artifacts": first}]
    plan[_PAGE2] = [{"total_count": 102, "artifacts": [_art(101, "rpm__el-9__amd64"), _art(102, "pep-summary-y")]}]
    rc, listing, out = _run(tmp_path)
    assert rc == 0 and calls == [_PAGE1, _PAGE2]
    rows = json.loads(listing.read_text())
    assert len(rows) == 102
    assert rows[0] == {"id": 1, "name": "pep-summary-x-1", "expired": False, "size_in_bytes": 10}
    assert all(set(r) == {"id", "name", "expired", "size_in_bytes"} for r in rows)   # no URLs or digests
    ids = out.read_text().strip().split("=", 1)[1].split(",")
    assert ids == [str(i) for i in range(1, 100)] + ["102"]          # expired 100 and rpm 101 excluded


def test_no_matching_artifacts_gives_empty_download_ids(tmp_path, network):
    plan, _, _ = network
    plan[_PAGE1] = [{"total_count": 1, "artifacts": [_art(1, "pep-summary-a", expired=True)]}]
    rc, listing, out = _run(tmp_path)
    assert rc == 0 and out.read_text() == "download_ids=\n"
    assert json.loads(listing.read_text()) == [{"id": 1, "name": "pep-summary-a", "expired": True,
                                                "size_in_bytes": 10}]


def test_a_transient_failure_is_retried_like_capture(tmp_path, network):
    plan, calls, waits = network
    plan[_PAGE1] = [_status(502), {"total_count": 1, "artifacts": [_art(1, "pep-summary-a")]}]
    rc, _, out = _run(tmp_path)
    assert rc == 0 and calls == [_PAGE1, _PAGE1] and waits == [2]
    assert out.read_text() == "download_ids=1\n"


@pytest.mark.parametrize("code,attempts", [
    (503, 3),          # transient: three attempts in total, then fail
    (403, 1),          # authorization: no retry
    (404, 1),          # the run's own listing is gone: no retry
])
def test_a_failed_listing_writes_nothing_and_exits_1(tmp_path, network, capsys, code, attempts):
    plan, calls, _ = network
    plan[_PAGE1] = [_status(code)]
    rc, listing, out = _run(tmp_path)
    assert rc == 1 and len(calls) == attempts
    assert not listing.exists() and not out.exists()                 # the collector then fails closed
    assert "artifact listing failed" in capsys.readouterr().err


def test_missing_token_fails_without_any_request(tmp_path, network):
    _, calls, _ = network
    rc, listing, _ = _run(tmp_path, env={})
    assert rc == 1 and calls == [] and not listing.exists()

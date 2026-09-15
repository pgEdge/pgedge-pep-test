"""Offline tests for the GitHub I/O capture shell (utillities/pep_capture_io.py).

No network, no docker, no real rpm/dpkg: a fake urlopen exercises redirect/auth
mechanics; a fake transport exercises pagination, exact-id downloads, disappearing
artifacts, tool preflight and the end-to-end orchestration; fake rpm/dpkg-deb and
generated ZIPs let the committed pure verifier run. Verification RULES live in
pep_capture and are not re-tested here.
"""
import email.message
import hashlib
import io
import json
import os
import urllib.error
import zipfile
from pathlib import Path

import pytest

import pep_capture_io as IO
import pep_capture as C
import pep_pkg_inspect as I

HERE = Path(__file__).parent
GOLDEN = json.loads((HERE / "pkg_inspect_fixtures" / "golden.json").read_text())
CASES = GOLDEN["cases"]


# --------------------------------------------------------------------------- #
# fake network primitives
# --------------------------------------------------------------------------- #
def _http_error(code, location=None):
    hdrs = email.message.Message()
    if location:
        hdrs["Location"] = location
    return urllib.error.HTTPError("https://api.github.com/x", code, "msg", hdrs, None)


class _FakeResp:
    def __init__(self, body):
        self._b = io.BytesIO(body)

    def read(self, n=-1):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_fake_urlopen(plan, calls):
    """plan: url -> ('redirect', location) | ('notfound',) | ('body', bytes)."""
    def fake(req, timeout):
        calls.append({"url": req.full_url, "headers": dict(req.headers)})
        entry = plan.get(req.full_url)
        if entry is None:
            raise AssertionError("no plan for %r" % req.full_url)
        kind = entry[0]
        if kind == "redirect":
            raise _http_error(302, entry[1])
        if kind == "notfound":
            raise _http_error(404)
        if kind == "body":
            return _FakeResp(entry[1])
        raise AssertionError("bad plan entry %r" % (entry,))
    return fake


# --------------------------------------------------------------------------- #
# fake tooling + package/receipt/zip builders (GitHub-inventory shaped)
# --------------------------------------------------------------------------- #
def use_fake_tools(tmp_path, monkeypatch):
    present = {}
    for name, bin_attr, case in (("rpm", "RPM_BIN", "rpm_runtime"), ("dpkg-deb", "DPKG_DEB_BIN", "deb_runtime")):
        out = tmp_path / (name + ".out")
        out.write_bytes(CASES[case]["tool_output"].encode("utf-8"))
        scr = tmp_path / name
        scr.write_text('#!/bin/sh\ncat %s\n' % json.dumps(str(out)))
        scr.chmod(0o755)
        monkeypatch.setattr(I, bin_attr, str(scr))
        present[name] = str(scr)
    # The preflight is a gate over the HOST PATH; the actual inspection uses the fakes
    # above. Make the gate see rpm/dpkg-deb as present (tests that need a MISSING tool
    # re-patch which() afterwards, and the later patch wins).
    monkeypatch.setattr(IO.shutil, "which", lambda t: present.get(t))


def _sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _zip(zip_path, arc_to_file):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, fp in arc_to_file.items():
            zf.write(fp, arcname=arc)
    return str(zip_path)


class Run:
    """A fake GitHub run: jobs + artifact inventory + a map of artifact_id -> zip path."""

    def __init__(self):
        self.jobs = []
        self.artifacts = []
        self.zips = {}
        self._next = 1000

    def _id(self):
        self._next += 1
        return self._next

    def add_job(self, cell_id, conclusion="success"):
        self.jobs.append({"id": self._id(), "name": "build [pep-cell:%s]" % cell_id,
                          "run_attempt": 1, "status": "completed", "conclusion": conclusion})

    def add_cell(self, tmp_path, family, cell_id, os_token, arch, member_case, body=b"BODY",
                 with_job="success"):
        magic = I._RPM_MAGIC if family == "rpm" else I._DEB_MAGIC
        exp_fam = I.RPM if family == "rpm" else I.DEB
        mp = CASES[member_case]["artifact_member_path"]
        d = tmp_path / ("src-" + cell_id.replace("/", "_"))
        d.mkdir(parents=True, exist_ok=True)
        fpath = d / mp
        fpath.write_bytes(magic + body + cell_id.encode())
        members = I.inspect_members([(str(fpath), mp)], expected_family=exp_fam)
        pkg_id, receipt_id = self._id(), self._id()
        pkg_zip = _zip(tmp_path / ("pkg-%d.zip" % pkg_id), {mp: str(fpath)})
        pkg_digest = _sha_file(pkg_zip)
        aname = "pkg-%s" % cell_id
        receipt = {"schema": "pep-receipt/2", "cell_id": cell_id, "artifact_id": pkg_id,
                   "artifact_name": aname, "archive_digest": pkg_digest, "members": members}
        rdir = tmp_path / ("rc-%d" % receipt_id)
        rdir.mkdir(parents=True, exist_ok=True)
        rjson = rdir / "receipt.json"
        rjson.write_text(json.dumps(receipt))
        receipt_zip = _zip(tmp_path / ("receipt-%d.zip" % receipt_id), {"receipt.json": str(rjson)})
        self.artifacts.append(self._inv(pkg_id, aname, pkg_digest))
        self.artifacts.append(self._inv(receipt_id, "pep-receipt-" + cell_id, _sha_file(receipt_zip)))
        self.zips[pkg_id] = pkg_zip
        self.zips[receipt_id] = receipt_zip
        if with_job:
            self.add_job(cell_id, with_job)
        return {"cell_id": cell_id, "family": family, "os": os_token, "normalized_arch": arch,
                "pkg_id": pkg_id, "receipt_id": receipt_id, "aname": aname}

    def _inv(self, aid, name, digest):
        return {"id": aid, "name": name, "digest": "sha256:" + digest, "expired": False,
                "size_in_bytes": 100, "created_at": "2026-01-01T00:00:00Z",
                "archive_download_url": "https://api.github.com/repos/o/r/actions/artifacts/%d/zip" % aid}


class FakeTransport:
    """Serves the fake run over the same url shapes the shell builds."""

    def __init__(self, run, gone_zip=frozenset(), gone_meta=frozenset()):
        self.run = run
        self.gone_zip = set(gone_zip)
        self.gone_meta = set(gone_meta)
        self.downloads = []

    def get_json(self, url):
        import urllib.parse as up
        p = up.urlsplit(url)
        q = up.parse_qs(p.query)
        if p.path.endswith("/jobs"):
            return _page(self.run.jobs, q, "jobs")
        if p.path.endswith("/artifacts"):
            return _page(self.run.artifacts, q, "artifacts")
        import re
        m = re.search(r"/artifacts/(\d+)$", p.path)
        if m:
            aid = int(m.group(1))
            if aid in self.gone_meta:
                raise IO._NotFound()
            for a in self.run.artifacts:
                if a["id"] == aid:
                    return a
            raise IO._NotFound()
        raise AssertionError("unexpected get_json url %r" % url)

    def download(self, url, dest):
        import re
        aid = int(re.search(r"/artifacts/(\d+)/zip$", url).group(1))
        self.downloads.append(aid)
        if aid in self.gone_zip:
            raise IO._NotFound()
        src = self.run.zips.get(aid)
        if src is None:
            raise IO._NotFound()
        import shutil
        shutil.copyfile(src, dest)


def _page(items, q, key):
    per = int(q.get("per_page", ["100"])[0])
    page = int(q.get("page", ["1"])[0])
    start = (page - 1) * per
    return {"total_count": len(items), key: items[start:start + per]}


_RI = {"logical_component": "pgedge-rag-server2", "intended_version": "2.0.0",
       "intended_buildnum": "1", "effective_tag": "t", "channel": "staging", "simulated": False}
_POL = {"allowed_runtime_package_names": ["pgedge-rag-server2"], "expected_binary_version": ""}
_PROV = {"repository": "o/r", "run_id": "1", "run_attempt": "1", "captured_at": "2026-09-15T00:00:00Z"}


def _matrices(cells):
    rpm = {"include": [c for c in cells if c["family"] == "rpm"]}
    deb = {"include": [c for c in cells if c["family"] == "deb"]}
    return [rpm, deb]


def _run_capture(run, cells, tmp_path, **over):
    kw = dict(transport=FakeTransport(run), repo="o/r", run_id="1", matrices=_matrices(cells),
              release_intent=_RI, publication_results={"rpm": "success", "deb": "success"},
              component_policy=_POL, provenance=_PROV, tmp_root=str(tmp_path / "dl"))
    (tmp_path / "dl").mkdir(exist_ok=True)
    kw.update(over)
    return IO.run_capture(**kw)


# --------------------------------------------------------------------------- #
# A. redaction
# --------------------------------------------------------------------------- #
def test_redact_strips_urls_query_and_tokens():
    s = IO.redact("boom https://blob.storage.net/pkg.zip?sig=SECRET&token=abc failed; Bearer ghs_AAA111")
    assert "sig=" not in s and "token=abc" not in s and "ghs_AAA111" not in s and "Bearer ghs" not in s
    assert "blob.storage.net/pkg.zip" in s          # host+path kept, query dropped
    assert "<redacted>" in s


def test_redact_masks_bare_token_shapes():
    assert "ghp_" not in IO.redact("leak ghp_0123456789abcdef")
    assert "<redacted>" in IO.redact("leak ghp_0123456789abcdef")


# --------------------------------------------------------------------------- #
# B. URL origin + redirect header helpers
# --------------------------------------------------------------------------- #
def test_is_github_api_url():
    assert IO._is_github_api_url("https://api.github.com/x")
    assert not IO._is_github_api_url("http://api.github.com/x")       # not https
    assert not IO._is_github_api_url("https://evil.example/x")        # wrong host
    assert not IO._is_github_api_url("https://api.github.com.evil/x")


def test_next_hop_headers_same_origin_keeps_auth():
    h = IO._initial_headers("TKN")
    kept = IO._next_hop_headers("https://api.github.com/a", "https://api.github.com/b", h)
    assert kept.get("Authorization") == "Bearer TKN"


def test_next_hop_headers_cross_origin_strips_auth():
    h = IO._initial_headers("TKN")
    stripped = IO._next_hop_headers("https://api.github.com/a", "https://blob.net/x?sig=1", h)
    assert not any(k.lower() in IO._GITHUB_ONLY_HEADERS for k in stripped)


def test_next_hop_headers_rejects_downgrade():
    h = IO._initial_headers("TKN")
    with pytest.raises(IO.CaptureIOError):
        IO._next_hop_headers("https://api.github.com/a", "http://blob.net/x", h)


# --------------------------------------------------------------------------- #
# C/D. _safe_fetch + UrllibTransport.download redirect + auth mechanics
# --------------------------------------------------------------------------- #
def test_download_follows_redirect_and_strips_auth(tmp_path, monkeypatch):
    api = "https://api.github.com/repos/o/r/actions/artifacts/5/zip"
    blob = "https://blob.storage.net/5.zip?sig=xyz"
    body = b"ZIPBYTES-CONTENT"
    calls = []
    monkeypatch.setattr(IO, "_urlopen", make_fake_urlopen({api: ("redirect", blob), blob: ("body", body)}, calls))
    dest = tmp_path / "out.zip"
    IO.UrllibTransport("TKN").download(api, str(dest))
    assert dest.read_bytes() == body
    assert "Authorization" in calls[0]["headers"]                    # api origin: authed
    assert not any(k.lower() == "authorization" for k in calls[1]["headers"])   # blob: stripped


def test_download_rejects_non_github_initial_url(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(IO, "_urlopen", make_fake_urlopen({}, calls))
    with pytest.raises(IO.CaptureIOError):
        IO.UrllibTransport("TKN").download("https://evil.example/x.zip", str(tmp_path / "o.zip"))
    assert calls == []                                               # refused BEFORE any request


def test_download_rejects_https_to_http_downgrade(tmp_path, monkeypatch):
    api = "https://api.github.com/repos/o/r/actions/artifacts/5/zip"
    calls = []
    monkeypatch.setattr(IO, "_urlopen", make_fake_urlopen({api: ("redirect", "http://blob.net/5.zip")}, calls))
    with pytest.raises(IO.CaptureIOError):
        IO.UrllibTransport("TKN").download(api, str(tmp_path / "o.zip"))


def test_safe_fetch_404_is_notfound(tmp_path, monkeypatch):
    api = "https://api.github.com/repos/o/r/actions/artifacts/5/zip"
    monkeypatch.setattr(IO, "_urlopen", make_fake_urlopen({api: ("notfound",)}, []))
    with pytest.raises(IO._NotFound):
        IO.UrllibTransport("TKN").download(api, str(tmp_path / "o.zip"))


def test_download_streams_without_whole_response_read(tmp_path, monkeypatch):
    api = "https://api.github.com/repos/o/r/actions/artifacts/5/zip"
    body = os.urandom(3 * IO._CHUNK + 7)

    class _Guarded(_FakeResp):
        def read(self, n=-1):
            assert isinstance(n, int) and 0 < n <= IO._CHUNK, "must read in bounded chunks: %r" % n
            return super().read(n)
    monkeypatch.setattr(IO, "_urlopen", lambda req, timeout: _Guarded(body))
    dest = tmp_path / "big.zip"
    IO.UrllibTransport("TKN").download(api, str(dest))
    assert dest.read_bytes() == body


def test_get_json_parses_and_caps(tmp_path, monkeypatch):
    url = "https://api.github.com/x"
    monkeypatch.setattr(IO, "_urlopen", lambda req, timeout: _FakeResp(b'{"a":1}'))
    assert IO.UrllibTransport("TKN").get_json(url) == {"a": 1}
    monkeypatch.setattr(IO, "_urlopen", lambda req, timeout: _FakeResp(b"x" * (IO._JSON_CAP + 5)))
    with pytest.raises(IO.CaptureIOError):
        IO.UrllibTransport("TKN").get_json(url)


# --------------------------------------------------------------------------- #
# E. pagination through the strict adapter (total_count agreement + dup-id reject)
# --------------------------------------------------------------------------- #
def test_pagination_multi_page(tmp_path, monkeypatch):
    monkeypatch.setattr(IO, "PER_PAGE", 1)                          # force multi-page
    run = Run()
    for i in range(3):
        run.artifacts.append(run._inv(run._id(), "art-%d" % i, "ab" * 32))
    pages = IO._list_pages(FakeTransport(run), lambda p: IO._artifacts_url("o/r", "1", p), "artifacts")
    combined = C.A.combine_pages(pages, "artifacts")
    assert len(combined) == 3                                       # total_count agreement holds


def test_pagination_duplicate_id_is_systemic(tmp_path, monkeypatch):
    run = Run()
    a = run._inv(777, "dup", "ab" * 32)
    run.artifacts += [a, dict(a)]                                   # two artifacts, same id
    cell = {"cell_id": "x", "family": "rpm", "os": "el-9", "normalized_arch": "amd64"}
    with pytest.raises(IO.CaptureIOError):
        IO.run_capture(transport=FakeTransport(run), repo="o/r", run_id="1", matrices=_matrices([cell]),
                       release_intent=_RI, publication_results={}, component_policy=_POL,
                       provenance=_PROV, tmp_root=str(tmp_path))


# --------------------------------------------------------------------------- #
# F. PEP-owned component policy resolution
# --------------------------------------------------------------------------- #
def _policy_doc(entries):
    return {"schema": IO.POLICY_SCHEMA, "components": entries}


def test_resolve_component_policy_ok(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(_policy_doc({"comp-a": {"allowed_runtime_package_names": ["p"]}})))
    assert IO.resolve_component_policy(str(p), "comp-a") == {"allowed_runtime_package_names": ["p"]}


def test_resolve_component_policy_requires_exact_schema(tmp_path):
    noschema = tmp_path / "no.json"
    noschema.write_text(json.dumps({"components": {"comp-a": {"allowed_runtime_package_names": ["p"]}}}))
    with pytest.raises(IO.CaptureIOError):
        IO.resolve_component_policy(str(noschema), "comp-a")
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"schema": "pep-capture-policy/999",
                                 "components": {"comp-a": {"allowed_runtime_package_names": ["p"]}}}))
    with pytest.raises(IO.CaptureIOError):
        IO.resolve_component_policy(str(wrong), "comp-a")


def test_resolve_component_policy_missing_component(tmp_path):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(_policy_doc({"comp-a": {"allowed_runtime_package_names": ["p"]}})))
    with pytest.raises(IO.CaptureIOError):
        IO.resolve_component_policy(str(p), "comp-z")


def test_resolve_component_policy_rejects_bad_entry(tmp_path):
    # selected entry validated: allowed_runtime_package_names must be a nonempty string list.
    for bad in ({}, {"allowed_runtime_package_names": []},
                {"allowed_runtime_package_names": "p"},
                {"allowed_runtime_package_names": ["p", 3]},
                {"allowed_runtime_package_names": ["p"], "expected_binary_version": 1}):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(_policy_doc({"comp-a": bad})))
        with pytest.raises(IO.CaptureIOError):
            IO.resolve_component_policy(str(p), "comp-a")


def test_resolve_component_policy_malformed_or_no_components(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(IO.CaptureIOError):
        IO.resolve_component_policy(str(bad), "x")
    nocomp = tmp_path / "nc.json"
    nocomp.write_text(json.dumps({"schema": IO.POLICY_SCHEMA, "nope": {}}))
    with pytest.raises(IO.CaptureIOError):
        IO.resolve_component_policy(str(nocomp), "x")


def test_committed_policy_is_valid_and_seeds_smoke():
    # The PEP-owned seed policy must exist, carry the exact schema, and resolve the smoke
    # component through the same validated path the workflow uses.
    p = HERE / "pep_capture_policy.json"
    data = json.loads(p.read_text())
    assert data.get("schema") == IO.POLICY_SCHEMA
    assert isinstance(data.get("components"), dict) and data["components"], "policy must seed >=1 component"
    pol = IO.resolve_component_policy(str(p), "pgedge-capture-smoke")
    assert pol["allowed_runtime_package_names"] == ["pgedge-capture-smoke"]


# --------------------------------------------------------------------------- #
# G. family-specific tool preflight
# --------------------------------------------------------------------------- #
def test_preflight_only_present_families(monkeypatch):
    seen = {}
    monkeypatch.setattr(IO.shutil, "which", lambda t: seen.setdefault(t, "/usr/bin/" + t))
    IO.preflight_tools({"rpm"})
    assert seen == {"rpm": "/usr/bin/rpm"}                          # dpkg-deb NOT probed


def test_preflight_missing_tool_is_systemic(monkeypatch):
    monkeypatch.setattr(IO.shutil, "which", lambda t: None if t == "dpkg-deb" else "/usr/bin/" + t)
    with pytest.raises(IO.CaptureIOError):
        IO.preflight_tools({"rpm", "deb"})
    IO.preflight_tools({"rpm"})                                     # rpm present -> ok


# --------------------------------------------------------------------------- #
# H. gather_blobs: exact-id downloads + disappearing artifacts
# --------------------------------------------------------------------------- #
def test_gather_downloads_only_receipts_and_referenced_packages(tmp_path, monkeypatch):
    use_fake_tools(tmp_path, monkeypatch)
    run = Run()
    c = run.add_cell(tmp_path, "rpm", "cell-rpm", "el-9", "amd64", "rpm_runtime")
    # an unrelated artifact must NOT be downloaded
    run.artifacts.append(run._inv(run._id(), "unrelated-thing", "ab" * 32))
    ft = FakeTransport(run)
    os.makedirs(str(tmp_path / "dl0"), exist_ok=True)
    blobs, gone, fams = IO.gather_blobs(ft, "o/r", run.artifacts, ["cell-rpm"], {"cell-rpm": "rpm"}, str(tmp_path / "dl0"))
    assert set(blobs) == {c["receipt_id"], c["pkg_id"]}            # exactly receipt + its package
    assert fams == {"rpm"} and gone == set()
    assert c["receipt_id"] in ft.downloads and c["pkg_id"] in ft.downloads


def test_gather_disappeared_receipt_is_absent(tmp_path, monkeypatch):
    use_fake_tools(tmp_path, monkeypatch)
    run = Run()
    c = run.add_cell(tmp_path, "rpm", "cell-rpm", "el-9", "amd64", "rpm_runtime")
    os.makedirs(str(tmp_path / "dl1"), exist_ok=True)
    ft = FakeTransport(run, gone_zip={c["receipt_id"]}, gone_meta={c["receipt_id"]})   # gone + confirmed gone
    blobs, gone, fams = IO.gather_blobs(ft, "o/r", run.artifacts, ["cell-rpm"], {"cell-rpm": "rpm"}, str(tmp_path / "dl1"))
    assert c["receipt_id"] in gone and blobs == {} and fams == set()


def test_gather_present_but_undownloadable_is_systemic(tmp_path, monkeypatch):
    use_fake_tools(tmp_path, monkeypatch)
    run = Run()
    c = run.add_cell(tmp_path, "rpm", "cell-rpm", "el-9", "amd64", "rpm_runtime")
    os.makedirs(str(tmp_path / "dl2"), exist_ok=True)
    ft = FakeTransport(run, gone_zip={c["receipt_id"]})            # zip 404s but metadata still present
    with pytest.raises(IO.CaptureIOError):
        IO.gather_blobs(ft, "o/r", run.artifacts, ["cell-rpm"], {"cell-rpm": "rpm"}, str(tmp_path / "dl2"))


def test_gather_disappeared_package_recorded(tmp_path, monkeypatch):
    use_fake_tools(tmp_path, monkeypatch)
    run = Run()
    c = run.add_cell(tmp_path, "rpm", "cell-rpm", "el-9", "amd64", "rpm_runtime")
    os.makedirs(str(tmp_path / "dl3"), exist_ok=True)
    ft = FakeTransport(run, gone_zip={c["pkg_id"]}, gone_meta={c["pkg_id"]})   # package gone
    blobs, gone, fams = IO.gather_blobs(ft, "o/r", run.artifacts, ["cell-rpm"], {"cell-rpm": "rpm"}, str(tmp_path / "dl3"))
    assert c["receipt_id"] in blobs and c["pkg_id"] not in blobs and c["pkg_id"] in gone


# --------------------------------------------------------------------------- #
# I. end-to-end orchestration through the pure capture core
# --------------------------------------------------------------------------- #
def test_run_capture_rpm_and_deb_available(tmp_path, monkeypatch):
    use_fake_tools(tmp_path, monkeypatch)
    run = Run()
    cr = run.add_cell(tmp_path, "rpm", "rag-rpm-el9-amd64", "el-9", "amd64", "rpm_runtime")
    cd = run.add_cell(tmp_path, "deb", "rag-deb-noble-arm64", "noble", "arm64", "deb_runtime")
    cells = [{k: c[k] for k in ("cell_id", "family", "os", "normalized_arch")} for c in (cr, cd)]
    env, ev, plan = _run_capture(run, cells, tmp_path)
    assert plan["plan_resolved"] is True
    byid = {c["cell_id"]: c for c in plan["cells"]}
    assert byid["rag-rpm-el9-amd64"]["build_state"] == "available"
    assert byid["rag-deb-noble-arm64"]["build_state"] == "available"
    assert ev["counts"]["accepted_receipt_cells"] == 2
    assert ev["counts"]["verified_package_artifacts"] == 2
    # evidence + plan are credential-free
    blob = (json.dumps(ev) + json.dumps(plan)).lower()
    assert "http" not in blob and "sig=" not in blob and "token" not in blob


def test_run_capture_disappeared_package_not_available(tmp_path, monkeypatch):
    use_fake_tools(tmp_path, monkeypatch)
    run = Run()
    cr = run.add_cell(tmp_path, "rpm", "rag-rpm-el9-amd64", "el-9", "amd64", "rpm_runtime")
    cell = {k: cr[k] for k in ("cell_id", "family", "os", "normalized_arch")}
    ft = FakeTransport(run, gone_zip={cr["pkg_id"]}, gone_meta={cr["pkg_id"]})
    (tmp_path / "dl").mkdir(exist_ok=True)
    env, ev, plan = IO.run_capture(transport=ft, repo="o/r", run_id="1", matrices=_matrices([cell]),
                                   release_intent=_RI, publication_results={"rpm": "success"},
                                   component_policy=_POL, provenance=_PROV, tmp_root=str(tmp_path / "dl"))
    c = {x["cell_id"]: x for x in plan["cells"]}["rag-rpm-el9-amd64"]
    assert c["build_state"] != "available"                         # missing package -> not a false green
    assert not any(t.get("eligibility") == "eligible" for t in c["targets"])


def test_run_capture_missing_tool_is_systemic(tmp_path, monkeypatch):
    use_fake_tools(tmp_path, monkeypatch)
    monkeypatch.setattr(IO.shutil, "which", lambda t: None)         # no host tooling
    run = Run()
    cr = run.add_cell(tmp_path, "rpm", "rag-rpm-el9-amd64", "el-9", "amd64", "rpm_runtime")
    cell = {k: cr[k] for k in ("cell_id", "family", "os", "normalized_arch")}
    (tmp_path / "dl").mkdir(exist_ok=True)
    with pytest.raises(IO.CaptureIOError):
        _run_capture(run, [cell], tmp_path)


# --------------------------------------------------------------------------- #
# J. outputs + provenance + main() failure classification
# --------------------------------------------------------------------------- #
def test_compact_outputs_are_credentialfree_and_no_reducer_leak():
    ev = {"schema": "capture-evidence/1", "counts": {"planned_cells": 2, "accepted_receipt_cells": 1,
          "rejected_receipt_cells": 0, "ambiguous_receipt_cells": 0, "absent_receipt_cells": 1,
          "verified_package_artifacts": 1}}
    plan = {"schema": "cert-plan/1", "plan_resolved": True,
            "coverage_denominators": {"available_build_cells": 1, "selected_targets": 1, "eligible_targets": 0}}
    out = IO.compact_outputs(ev, plan)
    assert out["plan_schema"] == "cert-plan/1" and out["plan_resolved"] is True
    assert out["available_build_cells"] == 1 and out["eligible_targets"] == 0
    assert "live_inventory" not in out and "cells" not in out


def test_write_github_output_bools_lowercase(tmp_path):
    gh = tmp_path / "ghout"
    gh.write_text("")
    IO._write_github_output({"a": 1, "b": None, "plan_resolved": True, "ok": False}, str(gh))
    lines = dict(l.split("=", 1) for l in gh.read_text().splitlines() if "=" in l)
    # Booleans render lowercase on BOTH the success (True) and failure (False) paths.
    assert lines["a"] == "1" and lines["b"] == ""
    assert lines["plan_resolved"] == "true" and lines["ok"] == "false"


def test_context_provenance_scalar_only(monkeypatch):
    prov = IO.context_provenance({"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "9",
                                  "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "abc", "GITHUB_REF": "refs/heads/x"},
                                 "deadbeef", "deadbeef")
    for v in prov.values():
        assert v is None or isinstance(v, (str, int, float, bool))
    assert prov["repository"] == "o/r" and prov["captured_at"].endswith("Z")


def test_main_fails_closed_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    gh = tmp_path / "ghout"; gh.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh))
    for nm, val in (("rpm", {"include": []}), ("deb", {"include": []}),
                    ("ri", _RI), ("pub", {}),
                    ("pol", {"schema": IO.POLICY_SCHEMA, "components": {"pgedge-rag-server2": _POL}})):
        (tmp_path / (nm + ".json")).write_text(json.dumps(val))
    rc = IO.main(["--rpm-matrix", str(tmp_path / "rpm.json"), "--deb-matrix", str(tmp_path / "deb.json"),
                  "--release-intent", str(tmp_path / "ri.json"), "--publication-results", str(tmp_path / "pub.json"),
                  "--policy", str(tmp_path / "pol.json"), "--out-dir", str(tmp_path / "out")])
    assert rc == 1                                                 # systemic -> nonzero, no plan
    out = dict(l.split("=", 1) for l in gh.read_text().splitlines() if "=" in l)
    assert out["capture_status"] == "failed" and out["plan_resolved"] == "false"


# --------------------------------------------------------------------------- #
# K. operational exception boundary (convert to redacted CaptureIOError; never a
#    raw traceback; never a half-written download)
# --------------------------------------------------------------------------- #
class _RaiseNotFoundTransport:
    def get_json(self, url):
        raise IO._NotFound()

    def download(self, url, dest):
        raise AssertionError("list-404 path must not download")


def test_list_endpoint_404_is_systemic():
    # A 404/410 on a LIST endpoint (run/scope gone) is systemic, not a cell-local absence.
    with pytest.raises(IO.CaptureIOError):
        IO._list_pages(_RaiseNotFoundTransport(), lambda p: IO._jobs_url("o/r", "1", p), "jobs")


def test_get_json_read_failure_is_redacted_systemic(monkeypatch):
    class _BadRead(_FakeResp):
        def read(self, n=-1):
            raise OSError("boom https://api.github.com/x?sig=SECRET&token=abc")
    monkeypatch.setattr(IO, "_urlopen", lambda req, timeout: _BadRead(b""))
    with pytest.raises(IO.CaptureIOError) as ei:
        IO.UrllibTransport("TKN").get_json("https://api.github.com/x")
    msg = str(ei.value)
    assert "SECRET" not in msg and "sig=" not in msg and "token=abc" not in msg


def test_download_read_failure_is_systemic_and_removes_partial(tmp_path, monkeypatch):
    api = "https://api.github.com/repos/o/r/actions/artifacts/5/zip"

    class _BadStream(_FakeResp):
        def read(self, n=-1):
            raise TimeoutError("read timed out")
    monkeypatch.setattr(IO, "_urlopen", lambda req, timeout: _BadStream(b""))
    dest = tmp_path / "part.zip"
    with pytest.raises(IO.CaptureIOError):
        IO.UrllibTransport("TKN").download(api, str(dest))
    assert not dest.exists()                                       # partial removed on failure


def test_download_destination_open_failure_is_systemic(tmp_path, monkeypatch):
    api = "https://api.github.com/repos/o/r/actions/artifacts/5/zip"
    monkeypatch.setattr(IO, "_urlopen", lambda req, timeout: _FakeResp(b"DATA"))
    bad_dest = tmp_path / "no-such-dir" / "x.zip"                  # parent does not exist
    with pytest.raises(IO.CaptureIOError):
        IO.UrllibTransport("TKN").download(api, str(bad_dest))


def _min_inputs(tmp_path):
    for nm, val in (("rpm", {"include": []}), ("deb", {"include": []}), ("ri", _RI), ("pub", {}),
                    ("pol", {"schema": IO.POLICY_SCHEMA, "components": {"pgedge-rag-server2": _POL}})):
        (tmp_path / (nm + ".json")).write_text(json.dumps(val))
    return ["--rpm-matrix", str(tmp_path / "rpm.json"), "--deb-matrix", str(tmp_path / "deb.json"),
            "--release-intent", str(tmp_path / "ri.json"), "--publication-results", str(tmp_path / "pub.json"),
            "--policy", str(tmp_path / "pol.json")]


def _main_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "TKN")
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    gh = tmp_path / "ghout"; gh.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh))
    return gh


def _failed(gh):
    out = dict(l.split("=", 1) for l in gh.read_text().splitlines() if "=" in l)
    return out.get("capture_status") == "failed" and out.get("plan_resolved") == "false"


def test_main_unusable_output_dir_fails_closed(tmp_path, monkeypatch):
    gh = _main_env(tmp_path, monkeypatch)
    blocker = tmp_path / "blocker"; blocker.write_text("x")        # a FILE where a dir is needed
    rc = IO.main(_min_inputs(tmp_path) + ["--out-dir", str(blocker / "out")])
    assert rc == 1 and _failed(gh)                                 # marker/output, not a traceback


def test_main_unusable_tmp_root_fails_closed(tmp_path, monkeypatch):
    gh = _main_env(tmp_path, monkeypatch)
    blocker = tmp_path / "tblock"; blocker.write_text("x")
    rc = IO.main(_min_inputs(tmp_path) + ["--out-dir", str(tmp_path / "out"),
                                          "--tmp-root", str(blocker / "sub")])
    assert rc == 1 and _failed(gh)


def test_main_output_write_failure_fails_closed(tmp_path, monkeypatch):
    gh = _main_env(tmp_path, monkeypatch)
    out_dir = tmp_path / "out"; out_dir.mkdir()
    (out_dir / IO.CAPTURE_EVIDENCE_FILE).mkdir()                   # a dir blocks the file write
    monkeypatch.setattr(IO, "run_capture", lambda **kw: (
        {}, {"schema": "capture-evidence/1", "counts": {}},
        {"schema": "cert-plan/1", "plan_resolved": True, "coverage_denominators": {}}))
    rc = IO.main(_min_inputs(tmp_path) + ["--out-dir", str(out_dir)])
    assert rc == 1 and _failed(gh)                                 # verified but unpersistable -> failed

#!/usr/bin/env python3
"""GitHub I/O shell for PEP capture (turns same-run release evidence into capture
evidence + a certification plan).

This is the IMPURE half of the capture stage. It reads a workflow run's jobs and
artifacts from the GitHub Actions REST API, downloads only the receipt artifacts
and the package artifacts they reference, and hands the local bytes to the
committed, PURE ``pep_capture`` module, which owns EVERY verification rule
(receipt parsing, archive/member digests, package re-inspection, association).
This shell never re-implements those rules.

Trust and failure model:
  * All receipt/package verification and cell-local rejection live in
    ``pep_capture`` (``EvidenceError`` with stable codes, recorded in the
    evidence). This shell adds only INFRASTRUCTURE failures — unusable global
    input, API/pagination faults, undownloadable-yet-present artifacts, missing
    host tooling — which it raises as ``CaptureIOError`` and which fail the job
    (no plan is published). ``pep_capture.CaptureSystemError`` is treated the same.
  * A referenced artifact that 404s is re-checked against a fresh single-artifact
    read: genuinely gone -> cell-local absence (dropped from the reconciled
    inventory so the pure layer records it as absent/incomplete, never systemic);
    still present but undownloadable -> systemic. Never a false resolved plan.

Security:
  * The GITHUB_TOKEN is sent ONLY to the validated ``api.github.com`` origin.
    Archive downloads follow the API's 302 to signed blob storage on a SEPARATE
    origin WITHOUT forwarding Authorization; an HTTPS->HTTP downgrade is refused;
    redirects are bounded. Tokens and signed query strings never appear in error
    messages, the capture evidence, the plan, or the workflow outputs.
  * Downloads STREAM into a run-scoped temporary directory in bounded chunks;
    package archives are never loaded fully into memory, and unrelated artifacts
    are never fetched. Response MEMORY is bounded, but temporary DISK use can reach
    the sum of the referenced artifact ZIPs (each is verified, then left in the
    run-scoped temp dir until the run ends). Capture is sequential; concurrency would
    improve latency, not peak disk. This is acceptable for today's small matrices and
    is recorded honestly for the future all-platform run.

Policy ownership: certification ``component_policy`` is PEP-owned. It is resolved
internally from the PEP-owned capture policy file (``pep_capture_policy.json``,
schema ``pep-capture-policy/1``) keyed by the release's logical component; it is
NEVER a caller-supplied workflow input. That file defines ONLY capture-time
component/package identity policy — it is NOT the set of platforms PEP can execute.
Detector evidence says what was BUILT; the later coordinator forms the execution set
by INTERSECTING the built cells with PEP's own execution catalog. Detector matrices,
release intent and publication results ARE caller-supplied evidence.

Stdlib only (plus the committed ``pep_capture`` / ``pep_cert_adapter`` /
``pep_cert_plan``). Offline-testable via ``pytest utillities/test_pep_capture_io.py``.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import pep_capture as C          # noqa: E402  pure verification core
import pep_cert_adapter as A     # noqa: E402  strict pagination + structuring
import pep_cert_plan as R        # noqa: E402  cert-plan/1 reducer

API_HOST = "api.github.com"
API_VERSION = "2022-11-28"
API_BASE = "https://api.github.com"
MAX_REDIRECTS = 5
PER_PAGE = 100                   # <=100 per the REST API; never create >100 artifacts just to page
_CHUNK = 1024 * 1024
_JSON_CAP = 16 * 1024 * 1024     # a jobs/artifacts page is small; cap defensively
_GITHUB_ONLY_HEADERS = ("authorization", "accept", "x-github-api-version")
_RECEIPT_PREFIX = C.RECEIPT_ARTIFACT_PREFIX
_TOKEN_RE = re.compile(r"(?i)(gh[a-z]_[A-Za-z0-9_]+|bearer\s+[A-Za-z0-9._-]+)")

CAPTURE_EVIDENCE_FILE = "capture-evidence.json"
CERT_PLAN_FILE = "cert-plan.json"
POLICY_SCHEMA = "pep-capture-policy/1"    # exact schema of the PEP-owned capture policy file


class CaptureIOError(Exception):
    """A systemic capture-infrastructure failure. Fails the job; no plan is produced.
    Carries only a bounded, credential-free message."""


class _NotFound(Exception):
    """Internal: a 404/410 from the API (artifact/endpoint gone)."""


# --- redaction --------------------------------------------------------------
def redact(text):
    """Strip anything token- or signature-bearing from a diagnostic string: any
    URL is reduced to scheme://host/path (query + userinfo dropped) and obvious
    token shapes are masked. Bounded length."""
    if not isinstance(text, str):
        text = str(text)

    def _strip_url(m):
        try:
            p = urllib.parse.urlsplit(m.group(0))
            host = (p.hostname or "")
            return "%s://%s%s" % (p.scheme, host, p.path) if p.scheme else host + p.path
        except ValueError:
            return "<url>"

    text = re.sub(r"https?://[^\s'\"]+", _strip_url, text)
    text = _TOKEN_RE.sub("<redacted>", text)
    return text[:300]


# --- URL origin + redirect safety (credentials only to the API origin) ------
def _is_github_api_url(url):
    p = urllib.parse.urlsplit(url)
    return p.scheme == "https" and (p.hostname or "").lower() == API_HOST


def _same_origin(u1, u2):
    a, b = urllib.parse.urlsplit(u1), urllib.parse.urlsplit(u2)
    return ((a.scheme, (a.hostname or "").lower(), a.port)
            == (b.scheme, (b.hostname or "").lower(), b.port))


def _next_hop_headers(from_url, to_url, headers):
    """Headers for a redirect target: refuse an HTTPS->non-HTTPS downgrade; keep
    headers on a same-origin hop; drop Authorization + API headers on any
    cross-origin hop so credentials never leave the API origin."""
    src, dst = urllib.parse.urlsplit(from_url), urllib.parse.urlsplit(to_url)
    if src.scheme == "https" and dst.scheme != "https":
        raise CaptureIOError("refusing HTTPS->non-HTTPS redirect during capture download")
    if _same_origin(from_url, to_url):
        return dict(headers)
    return {k: v for k, v in headers.items() if k.lower() not in _GITHUB_ONLY_HEADERS}


class _NoAutoRedirect(urllib.request.HTTPRedirectHandler):
    """Suppress urllib's automatic redirect following so each hop's headers are
    chosen explicitly (a 3xx surfaces as HTTPError carrying Location)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NOREDIR_OPENER = urllib.request.build_opener(_NoAutoRedirect)


def _urlopen(req, timeout):
    """The single network seam (tests patch this). Returns an open response."""
    return _NOREDIR_OPENER.open(req, timeout)   # nosec B310 - origin validated by caller


def _initial_headers(token):
    return {"Authorization": "Bearer %s" % token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION}


def _safe_fetch(url, token, timeout):
    """Fetch ``url`` with the token, following bounded redirects safely: the token
    reaches ONLY the api.github.com origin; a cross-origin hop drops it; a downgrade
    is refused. Returns the final open response. Raises ``_NotFound`` on 404/410 and
    ``CaptureIOError`` (redacted) otherwise."""
    if not _is_github_api_url(url):
        raise CaptureIOError("refusing to send credentials to a non-GitHub URL")
    headers = _initial_headers(token)
    cur = url
    for _ in range(MAX_REDIRECTS + 1):
        req = urllib.request.Request(cur, headers=headers, method="GET")
        try:
            return _urlopen(req, timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                nxt = urllib.parse.urljoin(cur, e.headers["Location"])
                headers = _next_hop_headers(cur, nxt, headers)
                cur = nxt
                continue
            if e.code in (404, 410):
                raise _NotFound()
            raise CaptureIOError("GitHub API request failed (HTTP %d)" % e.code)
        except urllib.error.URLError as e:
            raise CaptureIOError("GitHub API request failed: %s" % redact(str(e.reason)))
        except (TimeoutError, http.client.HTTPException, OSError) as e:
            # connect timeouts / transport errors not surfaced as URLError -> systemic.
            raise CaptureIOError("GitHub API request failed: %s" % redact(str(e)))
    raise CaptureIOError("too many redirects during capture fetch")


# --- transport (network vs offline fake) ------------------------------------
class UrllibTransport:
    """Production transport: authenticated GitHub API reads and safe streaming
    artifact downloads."""

    def __init__(self, token, timeout=60):
        self._token = token
        self._timeout = timeout

    def get_json(self, url):
        with _safe_fetch(url, self._token, self._timeout) as resp:
            try:
                data = resp.read(_JSON_CAP + 1)
            except (TimeoutError, http.client.HTTPException, OSError) as e:
                raise CaptureIOError("reading GitHub API response failed: %s" % redact(str(e)))
        if len(data) > _JSON_CAP:
            raise CaptureIOError("GitHub API response exceeds size cap")
        try:
            return json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise CaptureIOError("GitHub API returned malformed JSON")

    def download(self, url, dest_path):
        """Stream an artifact ZIP to ``dest_path`` in bounded chunks (never a whole
        response into memory). Raises ``_NotFound`` if gone. An expected transport
        (read), or destination (open/write) failure becomes a redacted
        ``CaptureIOError`` and the partially written file is removed (no half-download
        is ever handed to the verifier)."""
        resp = _safe_fetch(url, self._token, max(self._timeout, 120))
        try:
            with resp:
                try:
                    out = open(dest_path, "wb")
                except OSError as e:
                    raise CaptureIOError("opening capture download destination failed: %s" % redact(str(e)))
                try:
                    while True:
                        try:
                            chunk = resp.read(_CHUNK)
                        except (TimeoutError, http.client.HTTPException, OSError) as e:
                            raise CaptureIOError("reading artifact download failed: %s" % redact(str(e)))
                        if not chunk:
                            break
                        try:
                            out.write(chunk)
                        except OSError as e:
                            raise CaptureIOError("writing artifact download failed: %s" % redact(str(e)))
                finally:
                    out.close()
        except BaseException:
            _quiet_remove(dest_path)     # never leave a partial ZIP behind on any failure
            raise


# --- API URL builders -------------------------------------------------------
def _jobs_url(repo, run_id, page):
    return ("%s/repos/%s/actions/runs/%s/jobs?filter=all&per_page=%d&page=%d"
            % (API_BASE, repo, run_id, PER_PAGE, page))


def _artifacts_url(repo, run_id, page):
    return ("%s/repos/%s/actions/runs/%s/artifacts?per_page=%d&page=%d"
            % (API_BASE, repo, run_id, PER_PAGE, page))


def _artifact_url(repo, artifact_id):
    return "%s/repos/%s/actions/artifacts/%d" % (API_BASE, repo, artifact_id)


def _archive_url(repo, artifact_id):
    return "%s/repos/%s/actions/artifacts/%d/zip" % (API_BASE, repo, artifact_id)


def _list_pages(transport, url_for, items_key):
    """Read every page of a paginated list endpoint, stopping when a page is
    short or empty. Returns raw page objects for the strict adapter combiner."""
    pages, page = [], 1
    while True:
        try:
            env = transport.get_json(url_for(page))
        except _NotFound:
            # A 404/410 on a LIST endpoint means the run (or the token's scope for it)
            # is gone -> systemic. There is no single artifact to reconcile here.
            raise CaptureIOError("GitHub API list endpoint returned 404/410")
        if not isinstance(env, dict):
            raise CaptureIOError("GitHub API page %d is not an object" % page)
        items = env.get(items_key)
        if not isinstance(items, list):
            raise CaptureIOError("GitHub API page %d missing %r list" % (page, items_key))
        pages.append(env)
        if not items or len(items) < PER_PAGE:
            break
        page += 1
        if page > 10000:
            raise CaptureIOError("pagination exceeded a sane page ceiling")
    return pages


# --- component policy (PEP-owned; resolved by component, never caller-supplied) -
def _validate_policy_entry(policy, component):
    """Validate the SELECTED component's policy entry shape. Raises CaptureIOError."""
    if not isinstance(policy, dict):
        raise CaptureIOError("PEP capture policy entry for %r is not an object" % (component,))
    names = policy.get("allowed_runtime_package_names")
    if not (isinstance(names, list) and names
            and all(isinstance(x, str) and x.strip() != "" for x in names)):
        raise CaptureIOError(
            "PEP capture policy entry for %r needs a nonempty allowed_runtime_package_names "
            "list of nonblank strings" % (component,))
    ebv = policy.get("expected_binary_version")
    if not (ebv is None or isinstance(ebv, str)):
        raise CaptureIOError(
            "PEP capture policy entry for %r: expected_binary_version must be a string" % (component,))


def resolve_component_policy(policy_path, component):
    """Resolve the PEP-owned certification policy for ``component`` from the PEP-owned
    capture policy file, validating the file's EXACT schema and the SELECTED entry's shape
    BEFORE any network access (a malformed/unknown policy fails the job cleanly rather than
    surfacing mid-capture). The consumer supplies only the component NAME (release evidence);
    policy content is never a workflow input.

    This file is capture-time component/package identity policy ONLY. It is NOT the set of
    platforms/OS/arch/PG versions PEP can execute: detector evidence says what was BUILT, and
    the later coordinator forms the execution set by INTERSECTING those built cells with PEP's
    own execution catalog. This file never carries a caller-owned platform list."""
    try:
        raw = Path(policy_path).read_text(encoding="utf-8")
    except OSError:
        raise CaptureIOError("PEP capture policy file is unreadable")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise CaptureIOError("PEP capture policy file is malformed JSON")
    if not isinstance(doc, dict) or doc.get("schema") != POLICY_SCHEMA:
        raise CaptureIOError("PEP capture policy schema must be %r" % (POLICY_SCHEMA,))
    comps = doc.get("components")
    if not isinstance(comps, dict):
        raise CaptureIOError("PEP capture policy has no 'components' object")
    if not (isinstance(component, str) and component in comps):
        raise CaptureIOError("PEP capture policy has no entry for component %r" % (component,))
    policy = comps[component]
    _validate_policy_entry(policy, component)
    return policy


# --- tool preflight (only families actually present) ------------------------
_FAMILY_TOOL = {C.I.RPM: "rpm", C.I.DEB: "dpkg-deb"}


def preflight_tools(families):
    """Verify the package tool for EACH family that will actually be inspected is
    present. A missing host tool is INFRASTRUCTURE (raised systemic) so it can never
    be misreported downstream as a package identity mismatch."""
    missing = []
    for fam in sorted(families):
        tool = _FAMILY_TOOL.get(fam)
        if tool and shutil.which(tool) is None:
            missing.append(tool)
    if missing:
        raise CaptureIOError("required package tool(s) not on PATH: %s" % ", ".join(missing))


# --- provenance from trusted workflow context -------------------------------
def context_provenance(env, pep_ref, pep_resolved_sha):
    """Build injected provenance from trusted workflow context only (no clock inside
    the pure layer; ``captured_at`` is stamped here). Scalars only, no URLs/tokens."""
    return {
        "repository": env.get("GITHUB_REPOSITORY"),
        "run_id": env.get("GITHUB_RUN_ID"),
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "sha": env.get("GITHUB_SHA"),
        "ref": env.get("GITHUB_REF"),
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pep_implementation_ref": pep_ref,
        "pep_resolved_sha": pep_resolved_sha,
    }


# --- the download orchestration ---------------------------------------------
def _refresh_or_absent(transport, repo, artifact_id):
    """A download 404'd: refresh the single-artifact metadata. Returns the fresh
    artifact dict if it is still present (caller retries), or None if it is gone
    (cell-local absence). A present-but-still-undownloadable artifact is the caller's
    systemic concern."""
    try:
        art = transport.get_json(_artifact_url(repo, artifact_id))
    except _NotFound:
        return None                                  # genuinely disappeared
    if not isinstance(art, dict) or art.get("expired") is True:
        return None                                  # gone / expired -> absent
    return art


def _download_artifact(transport, repo, art, tmp_root):
    """Download one artifact ZIP by its exact id, streaming to a run-scoped temp file.
    Returns (path, 'ok') on success or (None, 'absent') if it disappeared. Raises
    ``CaptureIOError`` if it is present yet undownloadable (systemic)."""
    aid = art["id"]
    dest = os.path.join(tmp_root, "artifact-%d.zip" % aid)
    url = _archive_url(repo, aid)
    try:
        transport.download(url, dest)
        return dest, "ok"
    except _NotFound:
        pass
    # 404: refresh the inventory (single-artifact read) before classifying.
    fresh = _refresh_or_absent(transport, repo, aid)
    if fresh is None:
        _quiet_remove(dest)
        return None, "absent"
    try:
        transport.download(_archive_url(repo, aid), dest)
        return dest, "ok"
    except _NotFound:
        _quiet_remove(dest)
        raise CaptureIOError("artifact %d is present in inventory but not downloadable" % aid)


def _quiet_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def gather_blobs(transport, repo, inventory, planned_ids, family_by_cell, tmp_root):
    """Download exactly the associated receipt artifacts and the package artifacts
    they reference, by exact id, streaming into ``tmp_root``.

    Returns ``(blobs, disappeared_ids, families_present)``:
      * ``blobs`` maps artifact_id -> local ZIP path;
      * ``disappeared_ids`` are live-inventory ids that 404'd and were confirmed gone
        (dropped from the reconciled inventory so the pure layer records absence);
      * ``families_present`` is the set of families whose cells have a downloaded
        package to inspect (drives the tool preflight).
    Verification is delegated to ``pep_capture``; here we parse a downloaded receipt
    ONLY to learn which package artifact id it references."""
    receipt_names = {_RECEIPT_PREFIX + cid: cid for cid in planned_ids}
    blobs, disappeared, needed_pkgs = {}, set(), {}
    for art in inventory:
        if not isinstance(art, dict):
            continue
        name, aid = art.get("name"), art.get("id")
        if art.get("expired") is True or not _is_pos_int(aid):
            continue
        cid = receipt_names.get(name) if isinstance(name, str) else None
        if cid is None:
            continue                                  # not an associated receipt artifact
        path, status = _download_artifact(transport, repo, art, tmp_root)
        if status == "absent":
            disappeared.add(aid)
            continue
        blobs[aid] = path
        # Learn the referenced package id from a fully VERIFIED receipt (delegated).
        # A bad receipt raises -> we simply download no package; the pure layer will
        # reject the cell using the receipt blob we already have.
        try:
            receipt = C.verify_receipt_artifact(path, art.get("digest"))
        except C.EvidenceError:
            continue
        pkg_id = receipt["artifact_id"]
        if _is_pos_int(pkg_id):
            needed_pkgs[pkg_id] = cid
    inv_by_id = {a["id"]: a for a in inventory if isinstance(a, dict) and _is_pos_int(a.get("id"))}
    families_present = set()
    for pkg_id, cid in needed_pkgs.items():
        if pkg_id in blobs:
            families_present.add(family_by_cell.get(cid))
            continue
        art = inv_by_id.get(pkg_id)
        if art is None or art.get("expired") is True:
            continue                                  # pure layer records PACKAGE_ARTIFACT_ABSENT/EXPIRED
        path, status = _download_artifact(transport, repo, art, tmp_root)
        if status == "absent":
            disappeared.add(pkg_id)
            continue
        blobs[pkg_id] = path
        families_present.add(family_by_cell.get(cid))
    families_present.discard(None)
    return blobs, disappeared, families_present


def _is_pos_int(x):
    return isinstance(x, int) and not isinstance(x, bool) and x >= 1


# --- top-level capture ------------------------------------------------------
def run_capture(*, transport, repo, run_id, matrices, release_intent, publication_results,
                component_policy, provenance, tmp_root):
    """Read the run's jobs+artifacts, download the associated evidence, and delegate
    to the pure capture core. Returns ``(reducer_env, capture_evidence, cert_plan)``.
    Raises ``CaptureIOError`` / ``CaptureSystemError`` on systemic failure."""
    planned = A.planned_cells_from_detector(*matrices)
    planned_ids, family_by_cell, seen = [], {}, set()
    for c in planned:
        cid = c.get("cell_id") if isinstance(c, dict) else None
        if isinstance(cid, str) and cid.strip() != "" and cid not in seen:
            seen.add(cid)
            planned_ids.append(cid)
            family_by_cell[cid] = c.get("family")

    job_pages = _list_pages(transport, lambda p: _jobs_url(repo, run_id, p), "jobs")
    art_pages = _list_pages(transport, lambda p: _artifacts_url(repo, run_id, p), "artifacts")
    try:
        inventory = A.combine_pages(art_pages, "artifacts")
    except A.AdapterError as e:
        raise CaptureIOError("artifact inventory unusable: %s" % redact(str(e)))

    blobs, disappeared, families_present = gather_blobs(
        transport, repo, inventory, planned_ids, family_by_cell, tmp_root)

    # Only inspect families that are actually present -> preflight only those tools.
    preflight_tools(families_present)

    # Reconcile: drop artifacts confirmed gone so the pure layer sees a consistent
    # inventory (a disappeared receipt -> absent cell; a disappeared package ->
    # PACKAGE_ARTIFACT_ABSENT) rather than a live-but-missing-blob systemic error.
    final_inv = [a for a in inventory if a.get("id") not in disappeared]
    art_pages_final = [{"total_count": len(final_inv), "artifacts": final_inv}]

    env, evidence = C.capture_to_reducer_input(
        detector_matrices=matrices, job_pages=job_pages, artifact_pages=art_pages_final,
        blobs=blobs, release_intent=release_intent, component_policy=component_policy,
        publication_results=publication_results, provenance=provenance, tmp_root=tmp_root)
    plan = R.reduce(env)
    return env, evidence, plan


# --- outputs + CLI ----------------------------------------------------------
def compact_outputs(evidence, plan, artifact_ids=None):
    """Compact, credential-free workflow outputs (no URLs, no member detail)."""
    counts = evidence.get("counts", {})
    cov = plan.get("coverage_denominators", {})
    out = {
        "plan_schema": plan.get("schema"),
        "evidence_schema": evidence.get("schema"),
        "plan_resolved": bool(plan.get("plan_resolved")),
        "planned_cells": counts.get("planned_cells"),
        "accepted_receipt_cells": counts.get("accepted_receipt_cells"),
        "rejected_receipt_cells": counts.get("rejected_receipt_cells"),
        "ambiguous_receipt_cells": counts.get("ambiguous_receipt_cells"),
        "absent_receipt_cells": counts.get("absent_receipt_cells"),
        "verified_package_artifacts": counts.get("verified_package_artifacts"),
        "available_build_cells": cov.get("available_build_cells"),
        "selected_targets": cov.get("selected_targets"),
        "eligible_targets": cov.get("eligible_targets"),
    }
    if artifact_ids:
        out.update(artifact_ids)
    return out


def _gh_scalar(v):
    """Render one workflow-output value. Booleans are lowercase ``true``/``false`` so the
    success and failure paths use ONE representation; None is the empty string."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    return str(v)


def _write_github_output(outputs, gh_output_path):
    if not gh_output_path:
        return
    with open(gh_output_path, "a", encoding="utf-8") as fh:
        for k, v in outputs.items():
            fh.write("%s=%s\n" % (k, _gh_scalar(v)))


def _load_json_file(path, label):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise CaptureIOError("%s file is unreadable" % label)
    except ValueError:
        raise CaptureIOError("%s file is malformed JSON" % label)


def _require_matrix(obj, label):
    """A detector family matrix must be a JSON object with an 'include' list (an empty
    family is the explicit {"include": []}). A missing family is passed as {"include": []}."""
    if obj is None:
        return {"include": []}
    if not (isinstance(obj, dict) and isinstance(obj.get("include"), list)):
        raise CaptureIOError("%s must be an object with an 'include' list" % label)
    return obj


def main(argv=None):
    ap = argparse.ArgumentParser(description="PEP capture: GitHub run evidence -> capture-evidence + cert-plan")
    ap.add_argument("--rpm-matrix", required=True, help="detector rpm_matrix JSON file")
    ap.add_argument("--deb-matrix", required=True, help="detector deb_matrix JSON file")
    ap.add_argument("--release-intent", required=True, help="release_intent JSON file (caller evidence)")
    ap.add_argument("--publication-results", required=True, help="publication_results JSON file (caller evidence)")
    ap.add_argument("--policy", required=True, help="PEP-owned capture policy JSON (pep-capture-policy/1)")
    ap.add_argument("--out-dir", required=True, help="directory for capture-evidence.json + cert-plan.json")
    ap.add_argument("--tmp-root", default=None, help="run-scoped scratch dir for streamed downloads")
    args = ap.parse_args(argv)

    env = os.environ
    out_dir = Path(args.out_dir)
    gh_output = env.get("GITHUB_OUTPUT")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # The output directory itself is unusable: still emit the failed marker + output
        # (best-effort) and a nonzero exit rather than a raw traceback.
        return _fail(out_dir, gh_output, "capture output directory is unusable")
    try:
        repo = env["GITHUB_REPOSITORY"]
        run_id = env["GITHUB_RUN_ID"]
    except KeyError:
        return _fail(out_dir, gh_output,
                     "missing GITHUB_REPOSITORY / GITHUB_RUN_ID in workflow context")
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if not token:
        return _fail(out_dir, gh_output, "no GITHUB_TOKEN available for the REST API")

    tmp_root = args.tmp_root or os.path.join(env.get("RUNNER_TEMP", str(out_dir)), "pep-capture-dl")
    try:
        try:
            os.makedirs(tmp_root, exist_ok=True)
        except OSError:
            raise CaptureIOError("capture scratch directory is unusable")
        rpm_matrix = _require_matrix(_load_json_file(args.rpm_matrix, "rpm_matrix"), "rpm_matrix")
        deb_matrix = _require_matrix(_load_json_file(args.deb_matrix, "deb_matrix"), "deb_matrix")
        release_intent = _load_json_file(args.release_intent, "release_intent")
        publication_results = _load_json_file(args.publication_results, "publication_results")
        if not isinstance(release_intent, dict):
            raise CaptureIOError("release_intent must be an object")
        component = release_intent.get("logical_component")
        component_policy = resolve_component_policy(args.policy, component)
        provenance = context_provenance(env, env.get("PEP_IMPLEMENTATION_REF"), env.get("PEP_RESOLVED_SHA"))
        transport = UrllibTransport(token)
        reducer_env, evidence, plan = run_capture(
            transport=transport, repo=repo, run_id=run_id,
            matrices=[rpm_matrix, deb_matrix], release_intent=release_intent,
            publication_results=publication_results, component_policy=component_policy,
            provenance=provenance, tmp_root=tmp_root)
    except (CaptureIOError, C.CaptureSystemError) as e:
        return _fail(out_dir, gh_output, redact(str(e)))

    try:
        (out_dir / CAPTURE_EVIDENCE_FILE).write_text(C.capture_evidence_to_json(evidence), encoding="utf-8")
        (out_dir / CERT_PLAN_FILE).write_text(R.to_json(plan), encoding="utf-8")
    except OSError:
        # Verification succeeded but persisting the outputs failed: fail closed with the
        # normal marker/output so no half-written plan is presented as resolved.
        return _fail(out_dir, gh_output, "writing capture outputs failed")
    outputs = compact_outputs(evidence, plan)
    outputs["capture_status"] = "ok"
    _write_github_output(outputs, gh_output)
    print("pep-capture: plan_resolved=%s planned=%s accepted=%s rejected=%s ambiguous=%s absent=%s available=%s eligible=%s"
          % (_gh_scalar(outputs["plan_resolved"]), outputs["planned_cells"], outputs["accepted_receipt_cells"],
             outputs["rejected_receipt_cells"], outputs["ambiguous_receipt_cells"],
             outputs["absent_receipt_cells"], outputs["available_build_cells"], outputs["eligible_targets"]))
    return 0


def _fail(out_dir, gh_output, message):
    """Systemic failure: write a redacted marker, emit capture_status=failed, exit nonzero
    so the job fails and NO plan is published (never a false resolved plan)."""
    msg = redact(message)
    try:
        (Path(out_dir) / "capture-error.json").write_text(
            json.dumps({"capture_status": "failed", "error": msg}, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    _write_github_output({"capture_status": "failed", "plan_resolved": False, "error": msg}, gh_output)
    sys.stderr.write("pep-capture: FAILED: %s\n" % msg)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

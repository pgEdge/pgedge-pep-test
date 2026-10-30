#!/usr/bin/env python3
"""Evidence check for the temporary repository-authentication validation (direct legs).

Run by the harness's assert job; also run offline against fixtures. Usage:

    verify_direct.py <summaries-dir>

The summaries directory holds this run's downloaded pep-summary-* artifacts. Run metadata,
the attempt's jobs and the called jobs' logs come from the GitHub API, or, when
VERIFY_FIXTURES names a directory, from run.json, jobs.json and logs/<job id>.log there.
NEEDS_JSON is the assert job's toJSON(needs). Exit status 0 only if every leg matches.

The verdict comes from the uploaded evidence and the logs, never from job colours: in observe
mode every handled outcome, including the expected infrastructure failures, leaves a job green.
"""
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

FROZEN = "dde10a527dfae56a3c85e278efb460f86275e16f"
IDENTITY = {"l2a": "proven", "l2b": "not_attempted", "l1": "proven"}
NAMES = ("PGEDGE_REPO_CLIENT_CRT", "PGEDGE_REPO_CLIENT_KEY")
# Fragments of the rejected leg's synthetic literals.
SYNTHETIC = ("c3ludGhldGljLWNlcnQ", "c3ludGhldGljLWtleQ")
INFRA = {
    "missing": "repository client certificate not supplied (setup)",
    "incomplete": "repository client certificate incomplete: certificate or key missing (setup)",
    "rejected": "repository client certificate could not be installed (setup)",
}
SPOCK_DEB = dict(comp="spock", fam="deb", alias="debian12-amd64", pkg="pgedge-postgresql-17-spock50")
LEGS = {
    "rag-rpm": dict(comp="rag", fam="rpm", alias="oel9-amd64", pkg="pgedge-rag-server2", mode="full",
                    token="2.0.0-1.el9", version="2.0.0",
                    sha="01717af9c7ba89cebba85ec9399d9de6b144cf0a8183ba2d38cbb0e7d21bc617"),
    "rag-deb": dict(comp="rag", fam="deb", alias="debian12-amd64", pkg="pgedge-rag-server2", mode="full",
                    token="2.0.0-1.bookworm", version="2.0.0",
                    sha="eba1a2c463ad2ad8ed392e953387a80353103fff144bb72b377d22dc51fe0754"),
    "spock-rpm": dict(comp="spock", fam="rpm", alias="oel9-amd64", pkg="pgedge-spock50_17", mode="full",
                      token="5.0.13-1.el9", version="5.0.13",
                      sha="b91123bef742ec7b961a24fb758532dd3d10195548e3b6b615d9ae4233561748"),
    "spock-deb": dict(SPOCK_DEB, mode="full", token="5.0.13-1.bookworm", version="5.0.13",
                      sha="3be07f1108af6e038369d21d2f84cb10dff8f7963589141b104bdc3294b18918"),
    "preview": dict(SPOCK_DEB, mode="preview"),
    "missing": dict(SPOCK_DEB, mode="full"),
    "incomplete": dict(SPOCK_DEB, mode="full"),
    "rejected": dict(SPOCK_DEB, mode="full"),
}
STATE = {"rag-rpm": "complete", "rag-deb": "complete", "spock-rpm": "complete", "spock-deb": "complete",
         "preview": None, "missing": "missing", "incomplete": "incomplete", "rejected": "complete"}
FW_STEP = "Run framework (preview or full)"

CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")
ENV_LINE = re.compile(r"^\S+\s+(PGEDGE_REPO_CLIENT_(?:CRT|KEY)):(?: (.*))?$")


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise urllib.error.URLError("refused non-HTTPS redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Live:
    """The GitHub API. Job logs are read over HTTPS (gh refuses their colour codes); the token
    is an unredirected header, so it reaches api.github.com only."""

    def __init__(self, env):
        self.repo, self.run_id, self.att = env["GITHUB_REPOSITORY"], env["GITHUB_RUN_ID"], env["GITHUB_RUN_ATTEMPT"]
        self.token = env.get("GH_TOKEN", "")

    def _gh(self, path, tries=3):
        for i in range(tries):
            r = subprocess.run(["gh", "api", path], capture_output=True, text=True)
            if r.returncode == 0:
                return json.loads(r.stdout)
            if i + 1 < tries:
                time.sleep(15)
        raise RuntimeError("gh api %s failed: %s" % (path, r.stderr.strip()[:300]))

    def run(self):
        return self._gh("repos/%s/actions/runs/%s" % (self.repo, self.run_id))

    def jobs(self):
        return self._gh("repos/%s/actions/runs/%s/attempts/%s/jobs?per_page=100" % (self.repo, self.run_id, self.att))["jobs"]

    def log(self, jid, tries=4):
        req = urllib.request.Request("https://api.github.com/repos/%s/actions/jobs/%s/logs" % (self.repo, jid))
        req.add_unredirected_header("Authorization", "Bearer " + self.token)
        for i in range(tries):
            try:
                with urllib.request.build_opener(_HttpsOnlyRedirect).open(req, timeout=120) as r:
                    return r.read().decode("utf-8", "replace")
            except (OSError, ValueError, http.client.HTTPException) as e:
                err = e
            if i + 1 < tries:
                time.sleep(15)
        raise RuntimeError("job %s log: %s" % (jid, err))


class Fixtures:
    def __init__(self, d):
        self.d = Path(d)

    def run(self):
        return json.loads((self.d / "run.json").read_text())

    def jobs(self):
        return json.loads((self.d / "jobs.json").read_text())["jobs"]

    def log(self, jid):
        return (self.d / "logs" / ("%s.log" % jid)).read_text()


def step_groups(lines):
    """Each executed step's ``##[group]Run`` block, as a list of lines."""
    groups, cur = [], None
    for line in lines:
        if "##[group]Run " in line:
            cur = [line]
            groups.append(cur)
        elif cur is not None:
            cur.append(line)
            if "##[endgroup]" in line:
                cur = None
    return groups


def pair_env(lines):
    """[(name, value)] for every repository-pair env line among ``lines`` ('' when empty)."""
    out = []
    for line in lines:
        m = ENV_LINE.match(line)
        if m:
            out.append((m.group(1), (m.group(2) or "").strip()))
    return out


def framework_group(lines):
    """The test step's block: the one whose script runs the time-limited framework."""
    found = [g for g in step_groups(lines) if any("pep_time_limit.py --limit-seconds" in l for l in g)]
    return found[0] if len(found) == 1 else None


def verify(summaries, source, env):
    run_id, att, run_no = env["GITHUB_RUN_ID"], env["GITHUB_RUN_ATTEMPT"], env["GITHUB_RUN_NUMBER"]
    needs = json.loads(env["NEEDS_JSON"])
    problems, rows = [], []

    def check(ok, what):
        if not ok:
            problems.append(what)
        return ok

    try:
        refs = source.run().get("referenced_workflows") or []
        check([(r.get("path"), r.get("sha")) for r in refs] ==
              [("pgEdge/pgedge-pep-test/.github/workflows/pep-integration.yml@" + FROZEN, FROZEN)],
              "run: referenced_workflows %r" % refs)
        jobs = source.jobs()
    except (RuntimeError, OSError, ValueError) as e:
        problems.append("run: metadata unavailable: %s" % e)
        jobs = []

    for job, leg in LEGS.items():
        tag = job + ": "
        iid = "vdde10a5-" + job
        name = "pep-summary-%s-%s-amd64-pg17-release-%s-certification-%s-%s-r%s-a%s" % (
            leg["comp"], leg["fam"], leg["alias"], leg["mode"], iid, run_no, att)
        n = needs.get(job) or {}
        check(n.get("result") == "success", tag + "job result %r" % n.get("result"))
        check((n.get("outputs") or {}).get("summary_artifact") == name,
              tag + "summary_artifact %r" % (n.get("outputs") or {}).get("summary_artifact"))
        d = Path(summaries) / name
        try:
            s = json.loads((d / "summary.json").read_text())
        except (OSError, ValueError) as e:
            problems.append(tag + "summary missing/unreadable: %s" % e)
            rows.append((job, "?", "?", "", "?"))
            continue
        check(s.get("invocation_id") == iid, tag + "invocation_id %r" % s.get("invocation_id"))
        status, verdict, reason = s.get("execution_status"), s.get("test_verdict"), s.get("reason")
        counts = s.get("counts") or {}
        inst_file = d / "install-evidence.json"
        if "sha" in leg:
            check((status, verdict, reason) == ("completed", "pass", None),
                  tag + "status/verdict/reason %r" % ((status, verdict, reason),))
            check(counts.get("tests", 0) > 0 and counts.get("failures") == 0 and counts.get("errors") == 0,
                  tag + "counts %r" % counts)
            check(s.get("installed_package_sha256") == leg["sha"], tag + "summary installed sha256 %r" % s.get("installed_package_sha256"))
            check(s.get("identity_evidence") == IDENTITY, tag + "identity %r" % s.get("identity_evidence"))
            try:
                ie = json.loads(inst_file.read_text())
            except (OSError, ValueError) as e:
                problems.append(tag + "install evidence missing/unreadable: %s" % e)
                ie = {}
            want_target = {"package_name": leg["pkg"], "family": leg["fam"], "arch": "amd64", "pg_major": "17",
                           "container_alias": leg["alias"], "channel": "release"}
            check(ie.get("installed") is True and ie.get("install_kind") == "pinned", tag + "not a pinned install: %r" % ((ie.get("installed"), ie.get("install_kind")),))
            check(ie.get("install_token") == leg["token"], tag + "installed %r, expected %r" % (ie.get("install_token"), leg["token"]))
            check(ie.get("installed_sha256") == leg["sha"], tag + "installed sha256 %r, expected %s" % (ie.get("installed_sha256"), leg["sha"]))
            check(ie.get("target") == want_target, tag + "install target %r" % ie.get("target"))
            check(ie.get("expected_version") == leg["version"], tag + "expected_version %r" % ie.get("expected_version"))
            check(ie.get("run_token") == "%s-%s" % (run_id, att), tag + "install run_token %r" % ie.get("run_token"))
        elif leg["mode"] == "preview":
            check((status, verdict) == ("preview", "not_run"), tag + "status/verdict %r" % ((status, verdict),))
            check(counts.get("tests", 0) == 0, tag + "product tests ran: %r" % counts)
            check(s.get("installed_package_sha256") is None, tag + "an installed package is recorded")
            check(not inst_file.exists(), tag + "install evidence present in a preview")
        else:
            check((status, verdict, reason) == ("infra_failure", "not_run", INFRA[job]),
                  tag + "status/verdict/reason %r" % ((status, verdict, reason),))
            check(s.get("installed_package_sha256") is None, tag + "an installed package is recorded")

        # No report the leg uploaded may carry key material or the synthetic literals.
        for f in sorted(p for p in d.rglob("*") if p.is_file()):
            data = f.read_bytes()
            check(b"PRIVATE KEY" not in data, tag + "%s contains 'PRIVATE KEY'" % f.relative_to(d))
            check(not any(m.encode() in data for m in SYNTHETIC), tag + "%s contains a synthetic literal" % f.relative_to(d))

        jl = [j for j in jobs if j.get("name", "").startswith(job + ": ") and " / " in j.get("name", "")]
        if not check(len(jl) == 1, tag + "called job not found (%d matches)" % len(jl)):
            rows.append((job, status, verdict, reason or "", "?"))
            continue
        try:
            lines = CONTROL.sub("", source.log(jl[0]["id"])).splitlines()
        except (RuntimeError, OSError) as e:
            problems.append(tag + str(e))
            lines = []
        log = "\n".join(lines)
        steps = {st.get("name"): st.get("conclusion") for st in jl[0].get("steps") or []}
        fw = steps.get(FW_STEP)
        state = STATE[job]
        if state:
            check(("[repo-auth] client certificate: %s" % state) in log, tag + "no '[repo-auth] client certificate: %s' line" % state)
        else:
            check("[repo-auth] client certificate:" not in log, tag + "repo-auth check ran in a preview")
        check(fw == ("skipped" if job in ("missing", "incomplete") else "success"), tag + "framework step %r" % fw)

        everywhere = pair_env(lines)
        fwg = framework_group(lines)
        fw_env = dict(pair_env(fwg)) if fwg else {}
        if "sha" in leg:
            check(fwg is not None, tag + "framework step block not found in the log")
            for nm in NAMES:
                check(fw_env.get(nm) == "***", tag + "%s did not reach the framework step (%r)" % (nm, fw_env.get(nm)))
            check("Installed client certificate, pgEdge repositories enabled" in log, tag + "no certificate install line")
        elif job == "rejected":
            for nm in NAMES:
                check(bool(fw_env.get(nm)), tag + "%s did not reach the framework step" % nm)
            check("Installed client certificate, pgEdge repositories enabled" not in log, tag + "a synthetic pair was installed")
        elif job in ("preview", "missing"):
            # Neither value may reach ANY executed step.
            for nm, val in everywhere:
                check(val == "", tag + "%s reached a step (%s)" % (nm, "masked" if val == "***" else "unmasked"))
        elif job == "incomplete":
            check(any(nm == NAMES[0] and val == "***" for nm, val in everywhere), tag + "certificate not seen by the check step")
            check(all(val == "" for nm, val in everywhere if nm == NAMES[1]), tag + "a key reached a step")

        outside_env = [l for l in lines if not ENV_LINE.match(l)]
        check(not [l for l in outside_env if any(m in l for m in SYNTHETIC)],
              tag + "synthetic literal in a log line outside the env header")
        check(not [l for l in outside_env if "PRIVATE KEY-----" in l],
              tag + "PEM key armour in a log line (suspected exposure; not reproduced)")
        rows.append((job, status, verdict, reason or "", fw or "-"))
    return problems, rows


def main(argv):
    env = os.environ
    source = Fixtures(env["VERIFY_FIXTURES"]) if env.get("VERIFY_FIXTURES") else Live(env)
    problems, rows = verify(argv[1], source, env)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as out:
            out.write("## Repository-authentication validation at %s\n\n" % FROZEN[:7])
            out.write("Direct `pep-integration.yml` legs. Existing published packages; nothing built or "
                      "published. Not evidence for the product caller chain.\n\n")
            out.write("| leg | execution | verdict | reason | framework step |\n|---|---|---|---|---|\n")
            for r in rows:
                out.write("| %s | %s | %s | %s | %s |\n" % r)
            out.write("\n**%s**\n" % ("PASS" if not problems else "FAIL: %d problem(s)" % len(problems)))
            for p in problems:
                out.write("- %s\n" % p)
    print("\n".join(problems) if problems else "all legs as expected")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

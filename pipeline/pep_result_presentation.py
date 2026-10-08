#!/usr/bin/env python3
"""Reader-facing presentation of PEP certification results (stdlib only).

Reporting only: nothing here decides, changes or re-derives a result. It turns
evidence that already exists into text a reviewer can find without downloading
anything:

  * ``result-line``: one line at the end of each test run's log, read from the
    run's own ``summary.json``. It is shown only when the summary passes the
    reducer's own structural and run-binding checks against this job's live
    context (caller run, attempt and PEP revision); missing, malformed, foreign or
    mismatched evidence reads as UNAVAILABLE or UNVERIFIED -- never as zero tests
    passed or as a successful preview. It is one test run's result, never the
    package certification decision.
  * ``certification-summary``: the aggregate job's GitHub job summary, made of the
    Markdown body ``pep_cert_report.py --markdown`` renders from the same checked
    view as the HTML report, plus a download link to the uploaded report artifact.
  * ``artifact_url``: the one place a report download link is formed. A link is
    written only from a real upload's positive artifact id; otherwise the link is
    reported as unavailable (which does not by itself mean nothing was uploaded).

CLI (environment-driven, so caller values are never interpolated into a shell
script; both commands print to stdout and always exit 0):
  result-line SUMMARY_JSON           env IN_ALIAS, IN_PG, IN_PKG, IN_EXEC,
                                     IN_INVOCATION_ID_VALID, IN_PEP_REF, EXPECTED_PEP_SHA,
                                     GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT,
                                     GITHUB_SHA, GITHUB_REF
  certification-summary --body FILE  env EVIDENCE_ARTIFACT_ID, EVIDENCE_ARTIFACT_NAME,
                                     GITHUB_SERVER_URL, GITHUB_REPOSITORY, GITHUB_RUN_ID
"""
import html
import json
import os
import re
import sys
from pathlib import Path

# The reducer's own atomic-summary validator: a summary it would not accept is not
# shown as a result here either.
import pep_cert_result

_SERVER_RE = re.compile(r"https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_MAX_ID_DIGITS = 20          # GitHub ids are 64-bit integers
_MD_SPECIAL = re.compile(r"([\\`*_\[\]|~])")   # inline and table-cell control characters
SUMMARY_HEADING = "## PEP certification results"


# --------------------------------------------------------------------------- #
# Safe Markdown text
# --------------------------------------------------------------------------- #
def _flat(value, limit) -> str:
    """A JSON scalar as one line of plain text (control characters and runs of
    whitespace collapse to one space), truncated to ``limit`` characters."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    text = " ".join("".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in str(value)).split())
    return text[:limit] + "..." if len(text) > limit else text


def md_text(value, limit=120) -> str:
    """Untrusted text, safe inline and inside a table cell: HTML is escaped and every
    Markdown control character is backslash-escaped, so it can neither break a table
    nor form a link, image, heading, emphasis or raw HTML."""
    return _MD_SPECIAL.sub(r"\\\1", html.escape(_flat(value, limit), quote=False))


def md_code(value, limit=120) -> str:
    """Untrusted text as an inline code span that cannot break a table; blank is '-'."""
    text = _flat(value, limit).replace("`", "'").replace("|", "/")
    return "`%s`" % text if text else "-"


# --------------------------------------------------------------------------- #
# Report download link
# --------------------------------------------------------------------------- #
def _positive_id(value) -> bool:
    """A positive ASCII decimal string of at most 20 digits (booleans, ints, blanks,
    zero, signs and non-ASCII digits are rejected)."""
    return (isinstance(value, str) and value.isascii() and value.isdigit()
            and len(value) <= _MAX_ID_DIGITS and int(value) > 0)


def artifact_url(server_url, repository, run_id, artifact_id):
    """The browser download URL of one uploaded artifact, or None when any part is
    not a well-formed value (so no link is ever built from a failed upload)."""
    if not (isinstance(server_url, str) and _SERVER_RE.fullmatch(server_url)):
        return None
    if not (isinstance(repository, str) and _REPO_RE.fullmatch(repository)):
        return None
    if not (_positive_id(run_id) and _positive_id(artifact_id)):
        return None
    return "%s/%s/actions/runs/%s/artifacts/%s" % (server_url, repository, run_id, artifact_id)


def link_unavailable_reason(artifact_id) -> str:
    """Why no link was formed, stating only what is known."""
    if not (isinstance(artifact_id, str) and artifact_id.strip()):
        return "no report artifact ID was provided"
    return "the link could not be formed from the run context and artifact ID"


def report_link_line(url, artifact_name, unavailable_reason=None) -> str:
    """One Markdown line pointing at the report artifact; without a link it says only
    that the download link is unavailable (and why, when known)."""
    if not url:
        why = _flat(unavailable_reason, 160)
        return "**Full report:** download link unavailable%s." % ((": %s" % why) if why else "")
    name = _flat(artifact_name, 100)
    label = "Download %s" % md_code(name) if name else "Download the certification report"
    return ("**Full report:** [%s](%s), a zip with `consolidated-report.html` and the "
            "JSON evidence, which is authoritative." % (label, url))


def certification_summary(body, url, artifact_name, unavailable_reason=None) -> str:
    """The aggregate job's summary: heading, report link, then the rendered body (or
    a truthful note that the body is unavailable)."""
    out = [SUMMARY_HEADING, "", report_link_line(url, artifact_name, unavailable_reason), ""]
    if isinstance(body, str) and body.strip():
        out.append(body.rstrip("\n"))
    else:
        out.append("The results summary could not be rendered, so no results are shown "
                   "here. The JSON evidence in the report artifact is authoritative.")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# One result line per test run
# --------------------------------------------------------------------------- #
def _where(alias, pg, package) -> str:
    bits = [_flat(alias, 60) or "unknown platform",
            ("PG%s" % _flat(pg, 8)) if _flat(pg, 8) else "PG ?",
            _flat(package, 80) or "unknown package"]
    return " · ".join(bits)


def _sha_short(value) -> str:
    return value[:12] if isinstance(value, str) else ""


# The live context a test run's summary must come from: this workflow run and attempt (as
# GitHub reports it to the job) and the PEP revision this job requested and checked out.
RUN_CONTEXT_KEYS = ("repository", "run_id", "run_attempt", "sha", "ref",
                    "pep_requested_ref", "pep_resolved_sha")
_STABLE = (("repository", "caller_repo"), ("run_id", "caller_run_id"),
           ("sha", "caller_sha"), ("ref", "caller_ref"))
_PREFIX = "PEP test-run result: "
_NOT_THE_DECISION = " This is one test run's result, not the package certification decision."


def _context_problem(s, run_context):
    """Why a structurally valid summary is not this job's own result, or None. The reducer's
    own binding check decides repository/run/sha/ref; the producing attempt and PEP revision
    must be exactly this job's. Reporting only: nothing here classifies or certifies."""
    ctx = run_context if isinstance(run_context, dict) else {}
    missing = [k for k in RUN_CONTEXT_KEYS if not _flat(ctx.get(k), 200)]
    if missing:
        return "this job's run context is incomplete (missing %s)" % ", ".join(missing)
    prov = s["provenance"]
    if pep_cert_result.summary_binding_errors(s, {k: ctx[k] for k, _ in _STABLE}):
        differ = [p for k, p in _STABLE if _flat(prov.get(p), 200) != _flat(ctx[k], 200)]
        return "it comes from another workflow run (%s differs)" % ", ".join(differ)
    if _flat(prov.get("caller_run_attempt"), 20) != _flat(ctx["run_attempt"], 20):
        return "it comes from attempt %s, not this attempt %s" % (
            _flat(prov.get("caller_run_attempt"), 20), _flat(ctx["run_attempt"], 20))
    if _flat(prov.get("pep_requested_ref"), 200) != _flat(ctx["pep_requested_ref"], 200):
        return "it names PEP ref %s, not this job's %s" % (
            _flat(prov.get("pep_requested_ref"), 64), _flat(ctx["pep_requested_ref"], 64))
    if _flat(prov.get("pep_resolved_sha"), 64).lower() != _flat(ctx["pep_resolved_sha"], 64).lower():
        return "it was produced by PEP %s, not this job's checkout %s" % (
            _sha_short(prov.get("pep_resolved_sha")), _sha_short(ctx["pep_resolved_sha"]))
    return None


def result_line(summary_path, *, alias=None, pg=None, package=None, execution_mode=None,
                expected_invocation_id=None, run_context=None) -> str:
    """One readable line for a test run's log, from its summary.json.

    A result is shown only when the summary exists, passes the reducer's structural check,
    names this test run, binds to this job's live run context and fits the requested
    execution mode; otherwise the line says UNAVAILABLE or UNVERIFIED. A shown result is
    one test run's, never the certification decision. Never raises."""
    where = _where(alias, pg, package)
    try:
        raw = Path(summary_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ("%sUNAVAILABLE · %s: no result summary was written (summary.json is missing), "
                "so this run's outcome is unverified." % (_PREFIX, where))
    except (OSError, ValueError) as exc:
        return "%sUNAVAILABLE · %s: summary.json could not be read (%s)." % (_PREFIX, where, _flat(exc, 120))
    try:
        s = json.loads(raw)
    except ValueError:
        return ("%sUNAVAILABLE · %s: summary.json is not valid JSON, so this run's outcome is "
                "unverified." % (_PREFIX, where))
    try:
        problem = pep_cert_result.summary_problem(s)
        if problem:
            problem = "the result summary is not usable evidence (%s)" % _flat(problem, 160)
        elif s["invocation_id"] != _flat(expected_invocation_id, 64):
            problem = "the result summary names test run %s, not this run (%s)" % (
                s["invocation_id"], _flat(expected_invocation_id, 64) or "no valid test-run id was requested")
        else:
            why = _context_problem(s, run_context)
            problem = ("the result summary is not this job's own result: %s" % why) if why else None
    except Exception as exc:  # the checks never raise for JSON input; if they do, nothing is verified
        problem = "the result summary could not be checked (%s)" % _flat(exc, 120)
    if problem:
        return "%sUNVERIFIED · %s: %s." % (_PREFIX, where, problem)

    inv, status, verdict, counts = s["invocation_id"], s["execution_status"], s["test_verdict"], s["counts"]
    mode = execution_mode if execution_mode in ("preview", "full") else None   # exact, as the preflight
    if mode is None:
        return ("%sUNVERIFIED · %s: this run requested an unknown execution mode (%s), so its "
                "result is not shown." % (_PREFIX, where, _flat(execution_mode, 16) or "none"))
    if (status == "preview" and mode != "preview") or (status == "completed" and mode != "full"):
        return ("%sUNVERIFIED · %s: the result summary reports %s, but this run requested "
                "execution mode %s." % (_PREFIX, where, status, mode))
    reason = _flat(s.get("reason"), 160)
    because = (" (%s)" % reason) if reason else ""
    tail = " Test run %s." % inv
    if status == "preview":
        return ("%sPREVIEW · %s: nothing was installed and no product tests ran (preview only, "
                "not a certification).%s" % (_PREFIX, where, tail))
    if status in ("infra_failure", "incomplete"):
        word = "INFRA FAILURE" if status == "infra_failure" else "INCOMPLETE"
        if mode == "preview":
            return ("%s%s · %s: the preview request did not complete%s; nothing is verified.%s"
                    % (_PREFIX, word, where, because, tail))
        return ("%s%s · %s: the run did not complete%s; product tests are not verified.%s"
                % (_PREFIX, word, where, because, tail))
    tests, skipped = counts["tests"], counts["skipped"]
    failed = counts["failures"] + counts["errors"]
    cases = "%d test cases: %d passed, %d failed, %d skipped" % (
        tests, tests - failed - skipped, failed, skipped)
    digest = _sha_short(s.get("installed_package_sha256"))
    pkg = (" Installed package SHA-256 %s." % digest) if digest else \
        " No verified installed-package SHA-256 was recorded."
    if verdict == "pass":
        return "%sPASS · %s: %s.%s%s%s" % (_PREFIX, where, cases, pkg, tail, _NOT_THE_DECISION)
    if verdict == "fail":
        return "%sFAIL · %s: %s.%s%s%s" % (_PREFIX, where, cases, pkg, tail, _NOT_THE_DECISION)
    return ("%sNOT RUN · %s: no test case executed (%d collected, %d skipped).%s%s%s"
            % (_PREFIX, where, tests, skipped, pkg, tail, _NOT_THE_DECISION))


def run_context_from_env(env) -> dict:
    """This job's live context: GitHub's own run variables (the same ones the provenance
    step records) plus the PEP ref this job requested and the commit it checked out."""
    return {"repository": env.get("GITHUB_REPOSITORY"), "run_id": env.get("GITHUB_RUN_ID"),
            "run_attempt": env.get("GITHUB_RUN_ATTEMPT"), "sha": env.get("GITHUB_SHA"),
            "ref": env.get("GITHUB_REF"), "pep_requested_ref": env.get("IN_PEP_REF"),
            "pep_resolved_sha": env.get("EXPECTED_PEP_SHA")}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None, env=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    try:
        if len(argv) == 2 and argv[0] == "result-line":
            print(result_line(argv[1], alias=env.get("IN_ALIAS"), pg=env.get("IN_PG"),
                              package=env.get("IN_PKG"), execution_mode=env.get("IN_EXEC"),
                              expected_invocation_id=env.get("IN_INVOCATION_ID_VALID"),
                              run_context=run_context_from_env(env)))
            return 0
        if len(argv) == 3 and argv[0] == "certification-summary" and argv[1] == "--body":
            try:
                body = Path(argv[2]).read_text(encoding="utf-8")
            except (OSError, ValueError):
                body = None
            artifact_id = env.get("EVIDENCE_ARTIFACT_ID")
            url = artifact_url(env.get("GITHUB_SERVER_URL"), env.get("GITHUB_REPOSITORY"),
                               env.get("GITHUB_RUN_ID"), artifact_id)
            sys.stdout.write(certification_summary(body, url, env.get("EVIDENCE_ARTIFACT_NAME"),
                                                   None if url else link_unavailable_reason(artifact_id)))
            return 0
    except Exception as exc:  # pragma: no cover - reporting must never fail the job
        print("PEP result presentation failed (reporting only): %s" % _flat(exc, 160))
        return 0
    print("usage: pep_result_presentation.py result-line SUMMARY_JSON | "
          "certification-summary --body FILE", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

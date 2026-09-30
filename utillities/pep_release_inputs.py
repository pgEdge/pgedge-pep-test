#!/usr/bin/env python3
"""Release-pipeline inputs for pep-release-certify.yml (the release adapter).

A release pipeline knows a handful of facts: the cells its detector reported, its
release identity, whether the run was simulated, and what each publication job actually
concluded. This module turns those facts into pep-certify.yml's EXISTING inputs
(release_intent, publication_results, execution_mode, enforcement), so each release
pipeline does not re-implement the same validation, and it renders the adapter's
always-run summary. It owns no certification logic: pep-certify.yml decides
certification exactly as it does for any other caller.

Rules (fail closed; nothing is guessed or manufactured):
  * Each detector matrix must be a JSON object whose "include" is a list of cell objects,
    each with a nonblank cell_id and the matrix's own family; cell ids are unique across
    both families. Only a structurally valid matrix can count as having zero cells.
  * A family's publication result is required when its matrix has cells, and must be the
    publication job's own conclusion: success, failure, cancelled or skipped. An explicit
    "skipped" is a real outcome and is kept. An absent (empty) result is accepted only for
    a family with zero cells, and that family is then left out of publication_results
    rather than reported as skipped or successful.
  * simulated must be exactly "true" or "false"; only "true" selects preview.
  * Identity fields must be nonblank, without surrounding whitespace or control characters.
  * enforcement must be observe or gate.

CLI (environment-driven, so the facts are never interpolated into a shell script):
  compose   append the pep-certify inputs to $GITHUB_OUTPUT; exit 2 if a fact is rejected
  summary   print the Markdown summary to stdout; never fails
"""
import json
import os
import sys

FAMILIES = ("rpm", "deb")
PUBLICATION_RESULTS = ("success", "failure", "cancelled", "skipped")
ENFORCEMENTS = ("observe", "gate")
# adapter input name -> release_intent key (order is the release_intent key order)
IDENTITY = (("component", "logical_component"), ("version", "intended_version"),
            ("buildnum", "intended_buildnum"), ("effective_tag", "effective_tag"),
            ("channel", "channel"))
# adapter input name -> environment variable used by the workflow
ENV = {"rpm_matrix": "RPM_MATRIX", "deb_matrix": "DEB_MATRIX", "component": "COMPONENT",
       "version": "VERSION", "buildnum": "BUILDNUM", "effective_tag": "EFFECTIVE_TAG",
       "channel": "CHANNEL", "simulated": "SIMULATED", "rpm_publication": "RPM_PUBLICATION",
       "deb_publication": "DEB_PUBLICATION", "enforcement": "ENFORCEMENT"}
# pep-certify outputs shown in the summary -> environment variable
CERTIFY_OUTPUTS = (("certification_state", "CERT_STATE"), ("certification_conclusion", "CERT_CONCLUSION"),
                   ("execution_status", "CERT_EXECUTION"), ("test_verdict", "CERT_TESTS"),
                   ("coverage_status", "CERT_COVERAGE"), ("reason_code", "CERT_REASON"),
                   ("evidence_artifact_name", "CERT_EVIDENCE"))


class InputError(ValueError):
    """A release fact is missing or malformed, so the adapter rejects the inputs."""


def _clean_str(v):
    return (isinstance(v, str) and v != "" and v == v.strip()
            and not any(ord(c) < 32 or ord(c) == 127 for c in v))


def _json(obj):
    # Compact, key order preserved, UTF-8 kept: the same bytes jq -nc produces.
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def matrix_cells(raw, family):
    """The cell ids of one family's detector matrix, after validating its structure."""
    if not isinstance(raw, str) or raw.strip() == "":
        raise InputError("%s_matrix is missing" % family)
    try:
        matrix = json.loads(raw)
    except ValueError:
        raise InputError("%s_matrix is not valid JSON" % family)
    if not isinstance(matrix, dict) or not isinstance(matrix.get("include"), list):
        raise InputError("%s_matrix must be a JSON object with an 'include' list" % family)
    ids = []
    for i, cell in enumerate(matrix["include"]):
        if not isinstance(cell, dict):
            raise InputError("%s_matrix include[%d] is not an object" % (family, i))
        cid = cell.get("cell_id")
        if not _clean_str(cid):
            raise InputError("%s_matrix include[%d] has no valid cell_id" % (family, i))
        if cell.get("family") != family:
            raise InputError("%s_matrix include[%d] (%s) has family %r, expected %r"
                             % (family, i, cid, cell.get("family"), family))
        ids.append(cid)
    return ids


def compose(facts):
    """Build pep-certify's inputs from the adapter inputs (a dict of strings; an absent
    input is None or ""). Returns the four pep-certify inputs plus per-family cell counts.
    Raises InputError when a fact is missing or malformed."""
    cells = {f: matrix_cells(facts.get(f + "_matrix"), f) for f in FAMILIES}
    ids = cells["rpm"] + cells["deb"]
    dups = sorted({c for c in ids if ids.count(c) > 1})
    if dups:
        raise InputError("cell_id appears more than once across the detector matrices: %s" % ", ".join(dups))
    intent = {}
    for name, key in IDENTITY:
        value = facts.get(name)
        if not _clean_str(value):
            raise InputError("%s must be a nonblank string without surrounding whitespace or "
                             "control characters, got %r" % (name, value))
        intent[key] = value
    simulated = facts.get("simulated")
    if simulated not in ("true", "false"):
        raise InputError("simulated must be exactly 'true' or 'false', got %r" % (simulated,))
    intent["simulated"] = simulated == "true"
    enforcement = facts.get("enforcement", "observe")
    if enforcement not in ENFORCEMENTS:
        raise InputError("enforcement must be one of %s, got %r" % ("|".join(ENFORCEMENTS), enforcement))
    publication = {}
    for family in FAMILIES:
        result = facts.get(family + "_publication")
        count = len(cells[family])
        if result in (None, ""):
            if count:
                raise InputError("%s_publication is absent although the detector reported %d %s cell(s); "
                                 "pass that family's publication job result" % (family, count, family))
            continue                     # no cells: nothing was published, so no result to report
        if result not in PUBLICATION_RESULTS:
            raise InputError("%s_publication must be one of %s, got %r"
                             % (family, "|".join(PUBLICATION_RESULTS), result))
        publication[family] = result
    return {"release_intent": _json(intent),
            "publication_results": _json(publication),
            "execution_mode": "preview" if intent["simulated"] else "full",
            "enforcement": enforcement,
            "rpm_cells": str(len(cells["rpm"])),
            "deb_cells": str(len(cells["deb"]))}


# --------------------------------------------------------------------------- summary
_PUBLICATION_TEXT = {
    "success": "published (the publication job succeeded)",
    "failure": "not confirmed: the publication job failed",
    "cancelled": "not confirmed: the publication job was cancelled",
    "skipped": "not published: the publication job was skipped",
}


def _md(value, limit=160):
    """A caller-supplied value as an inline code span that cannot break the table."""
    text = "" if value is None else str(value)
    text = " ".join(text.split()).replace("`", "'").replace("|", "/")
    if len(text) > limit:
        text = text[:limit] + "..."
    return "`%s`" % text if text else "`<empty>`"


def _publication_rows(facts):
    """Table rows, plus the families whose success contradicts a simulated run."""
    simulated = facts.get("simulated") == "true"
    rows, conflicts = [], []
    for family in FAMILIES:
        try:
            count = len(matrix_cells(facts.get(family + "_matrix"), family))
            cells = "%d cell%s" % (count, "" if count == 1 else "s")
        except InputError:
            count, cells = None, "matrix invalid"
        result = facts.get(family + "_publication")
        if result in (None, ""):
            text = ("no cells, nothing to publish" if count == 0
                    else "missing: no publication result was provided")
        elif result == "success" and simulated:
            # pep_cert_plan fails closed on this contradiction (publish_unconfirmed,
            # simulated_with_family_push_success); never present it as a publication.
            text = ("`success`: conflict: the run is simulated, yet this publication job reports "
                    "success; PEP treats it as unconfirmed")
            conflicts.append(family.upper())
        elif result in _PUBLICATION_TEXT:
            text = "`%s`: %s" % (result, _PUBLICATION_TEXT[result])
        else:
            text = "invalid result %s" % _md(result)
        rows.append("| %s | %s | %s |" % (family.upper(), cells, text))
    return rows, conflicts


def render_summary(facts, normalize_result, certify_result, outputs, pep_sha):
    """The adapter's summary. Publication rows are the caller's own publication results;
    the certification rows are pep-certify's outputs. Never raises."""
    try:
        return _render(facts, normalize_result, certify_result, outputs, pep_sha)
    except Exception as exc:                   # the summary must always be written
        return ("## PEP release certification\n\nThe summary could not be rendered (%s). "
                "normalize job: %s, certify job: %s.\n" % (_md(exc), _md(normalize_result), _md(certify_result)))


def _render(facts, normalize_result, certify_result, outputs, pep_sha):
    try:
        composed, rejection = compose(facts), None
    except InputError as exc:
        composed, rejection = None, str(exc)
    out = ["## PEP release certification", "",
           "Certification runs after publication and never changes it. The publication rows are "
           "the pipeline's own publication job results, as passed to PEP.", ""]
    rows, conflicts = _publication_rows(facts)
    if facts.get("simulated") == "true":
        out += ["Simulated run: no publication is expected, so certification is a preview (dry run).", ""]
        if conflicts:
            out += ["Conflict: this run is simulated, but the %s publication %s success. PEP treats a "
                    "simulated run's publication as unconfirmed, not as published." % (
                        " and ".join(conflicts), "jobs report" if len(conflicts) > 1 else "job reports"), ""]
    out += ["| family | detector | publication |", "| --- | --- | --- |"] + rows + [""]

    state = outputs.get("certification_state") or ""
    reason = outputs.get("reason_code") or ""
    conclusion = outputs.get("certification_conclusion") or ""
    if rejection is not None:
        headline = "Certification did not run: the release inputs were rejected (%s)." % rejection
    elif normalize_result == "failure":
        headline = ("Certification did not run: the release inputs job failed before handing certify its "
                    "inputs (for example during checkout, Python setup or writing its outputs). The release "
                    "facts themselves are valid; see the release inputs job.")
    elif normalize_result == "cancelled":
        headline = "Certification was cancelled."
    elif certify_result == "success":
        headline = "Certification completed: state %s, reason %s, workflow conclusion %s." % (
            _md(state), _md(reason), _md(conclusion))
    elif certify_result == "failure":
        headline = "Certification did not pass: the certify job failed"
        headline += (" (state %s, reason %s)." % (_md(state), _md(reason))) if (state or reason) else \
            "; see the certify jobs for the failing step."
        headline += " This does not undo or change publication; see the publication rows above."
    elif certify_result == "cancelled":
        headline = "Certification was cancelled."
    else:
        headline = "Certification did not run (release inputs job: %s, certify job: %s)." % (
            _md(normalize_result), _md(certify_result))
    out += [headline, ""]

    rows = [("release", "%s %s-%s, tag %s, channel %s" % (
                _md(facts.get("component")), _md(facts.get("version")), _md(facts.get("buildnum")),
                _md(facts.get("effective_tag")), _md(facts.get("channel"))))]
    if composed:
        rows.append(("mode", "%s, enforcement %s" % (_md(composed["execution_mode"]), _md(composed["enforcement"]))))
    rows += [(name, _md(outputs.get(name)) if outputs.get(name) else "-") for name, _ in CERTIFY_OUTPUTS]
    rows += [("certify job", _md(certify_result)), ("PEP revision", _md(pep_sha))]
    out += ["| certification | value |", "| --- | --- |"] + ["| %s | %s |" % r for r in rows]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- CLI
def facts_from_env(env):
    return {name: env.get(var) for name, var in ENV.items()}


def main(argv=None, env=None):
    argv = sys.argv[1:] if argv is None else argv
    env = os.environ if env is None else env
    if argv == ["compose"]:
        try:
            composed = compose(facts_from_env(env))
        except InputError as exc:
            print("::error::release inputs rejected: %s" % exc)
            return 2
        lines = "".join("%s=%s\n" % kv for kv in composed.items())
        path = env.get("GITHUB_OUTPUT")
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(lines)
        sys.stdout.write(lines)
        return 0
    if argv == ["summary"]:
        outputs = {name: env.get(var) for name, var in CERTIFY_OUTPUTS}
        sys.stdout.write(render_summary(facts_from_env(env), env.get("NORMALIZE_RESULT"),
                                        env.get("CERTIFY_RESULT"), outputs, env.get("PEP_SHA")))
        return 0
    print("usage: pep_release_inputs.py compose|summary", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())

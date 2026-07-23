"""Integration-mode resolver CLI. Reads PEP_* env, resolves the small set of
resolver-controlled fields, VALIDATES them against allowlists, and writes
test-logs/resolved-config.json. With `--get KEY` it prints ONE already-validated
value from that JSON, so run_pep_tf.sh reads the decision via `$(...)` capture
WITHOUT eval-ing any caller-controlled string (issue #3). Only run when
PEP_INTEGRATION_MODE=1. Stdlib only."""
import json, os, sys
from pathlib import Path
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "pep_resolve", str(Path(__file__).with_name("pep_resolve.py")))
rz = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rz
_spec.loader.exec_module(rz)

# Load pep_request.py's allowlists so the resolver's validation stays in
# lockstep with the request validator (single source of truth, no drift).
_pr_spec = importlib.util.spec_from_file_location(
    "pep_request", str(Path(__file__).with_name("pep_request.py")))
_pr = importlib.util.module_from_spec(_pr_spec)
sys.modules[_pr_spec.name] = _pr
_pr_spec.loader.exec_module(_pr)

_ALLOW = {"repo": set(_pr.VALID_CHANNELS), "scenario": set(_pr.VALID_SCENARIOS)}
_OUT = Path("test-logs/resolved-config.json")


def _resolve():
    # PEP_CONFIG_REPO = the config{PG}.env REPO captured BEFORE the --repo
    # override (issue #4); PEP_CALLER_REPO = the --repo override. Both layers are
    # recorded so provenance stays truthful.
    return rz.resolve(
        ["repo", "scenario", "upgrade"],
        caller={"repo": os.environ.get("PEP_CALLER_REPO") or None,
                "scenario": os.environ.get("PEP_CALLER_SCENARIO") or None},
        config_pg={"repo": os.environ.get("PEP_CONFIG_REPO") or None,
                   "upgrade": os.environ.get("PEP_CFG_UPGRADE") or None},
        defaults={"scenario": "certification", "upgrade": "false"},
    )


def _validate(resolved):
    for key, allowed in _ALLOW.items():
        v = resolved[key]["value"]
        if v is not None and v not in allowed:      # reject BEFORE the shell uses it
            sys.stderr.write(f"[resolve] {key}={v!r} not in {sorted(allowed)}\n")
            raise SystemExit(3)   # == PEP_RC_VALIDATION (Task 3): a KNOWN request-
                                  # validation rejection, distinct from a runtime error


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--get":
        data = json.loads(_OUT.read_text())
        print(data[argv[1]]["value"] or "")        # single validated token
        return 0
    resolved = _resolve()
    _validate(resolved)
    Path("test-logs").mkdir(exist_ok=True)
    _OUT.write_text(json.dumps(resolved, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Build a normalized PEP request from a PEP_* environment mapping. Pure (takes
an explicit dict, defaults to os.environ) so it unit-tests without Docker."""
from __future__ import annotations
import os
import importlib.util as _ilu
from pathlib import Path as _Path
_spec = _ilu.spec_from_file_location("pep_request", str(_Path(__file__).with_name("pep_request.py")))
_pr = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_pr)


def build_request_from_env(env=None):
    """Return a normalized request if PEP_INTEGRATION_MODE == '1', else None.
    Maps the Task 3 env contract to raw request keys and delegates validation to
    pep_request.normalize_request (which raises RequestError on bad input)."""
    env = os.environ if env is None else env
    if env.get("PEP_INTEGRATION_MODE") != "1":
        return None
    raw = {
        "component": env.get("PEP_COMPONENT"),
        "package_name": env.get("PEP_PACKAGE_NAME"),
        "channel": env.get("PEP_CHANNEL"),
        "expected_version": env.get("PEP_EXPECTED_VERSION"),
        "family": env.get("PEP_FAMILY"),
        "arch": env.get("PEP_ARCH_FILTER"),
        "pg_major": env.get("PG_MAJOR_VERSION"),
        "container_alias": env.get("PEP_CONTAINER_ALIAS"),
        "expected_buildnum": env.get("PEP_EXPECTED_BUILDNUM"),
        "effective_tag": env.get("PEP_EFFECTIVE_TAG"),
        "expected_rpm": env.get("PEP_EXPECTED_RPM"),
        "expected_deb": env.get("PEP_EXPECTED_DEB"),
        "expected_binary": env.get("PEP_EXPECTED_BINARY"),
        "scenario": env.get("PEP_SCENARIO"),
        "mode": env.get("PEP_MODE"),
    }
    return _pr.normalize_request({k: v for k, v in raw.items() if v is not None})


if __name__ == "__main__":
    import sys
    try:
        req = build_request_from_env()   # reads os.environ
    except _pr.RequestError as e:
        print(f"[request] {e}", file=sys.stderr)
        raise SystemExit(3)              # == PEP_RC_VALIDATION: expected rejection
    if req is None:                      # integration marker unset -> treat as rejection
        print("[request] PEP_INTEGRATION_MODE != 1", file=sys.stderr)
        raise SystemExit(3)
    raise SystemExit(0)                  # valid complete env
    # NOTE: no broad `except Exception` — an unexpected error propagates with its own
    # (non-3) traceback exit code, which the bridge preserves as infra_failure.

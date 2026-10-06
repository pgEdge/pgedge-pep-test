"""Config resolver for PEP integration mode. PURE: no env mutation, no I/O.

`resolve(keys, caller, config_pg, dotenv, defaults)` walks the fixed precedence
    caller > config_pg > dotenv > defaults
and returns, per requested key, {"value": <v>, "source": <layer or None>}. A
missing/empty ("" or None) value at a layer is skipped so the next layer wins.

Invoked ONLY in integration mode (spec §7). A legacy standalone run does not call
this at all; its config loading follows the existing path unchanged. Stdlib only.
"""
from __future__ import annotations

LAYERS = ("caller", "config_pg", "dotenv", "defaults")


def resolve(keys, *, caller=None, config_pg=None, dotenv=None, defaults=None):
    layers = {
        "caller": caller or {},
        "config_pg": config_pg or {},
        "dotenv": dotenv or {},
        "defaults": defaults or {},
    }
    out = {}
    for key in keys:
        chosen = {"value": None, "source": None}
        for name in LAYERS:
            v = layers[name].get(key)
            if v is not None and v != "":
                chosen = {"value": v, "source": name}
                break
        out[key] = chosen
    return out

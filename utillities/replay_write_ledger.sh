#!/usr/bin/env bash
# Write the per-cell success ledger AFTER retrieval + upload + receipt succeeded.
# A ledger file exists ONLY when a cell has an exact verified package and a valid
# receipt, so counting ledgers is an independent check of family completeness.
#
# Arg 1: family (rpm|deb). Required env: CELL_ID RECEIPT_NAME
set -euo pipefail
FAMILY="${1:?family arg required}"
: "${CELL_ID:?}" "${RECEIPT_NAME:?receipt_artifact_name missing -- receipt step did not succeed}"

case "$FAMILY" in rpm|deb) : ;; *) echo "::error::bad family $FAMILY" >&2; exit 1 ;; esac

mkdir -p ledger-out
python3 - "$CELL_ID" "$FAMILY" "$RECEIPT_NAME" <<'PY'
import json, sys
cell_id, family, receipt = sys.argv[1], sys.argv[2], sys.argv[3]
rec = {"schema": "pep-replay-ledger/1", "cell_id": cell_id, "family": family,
       "verified": True, "receipt_artifact_name": receipt}
with open("ledger-out/%s.json" % cell_id, "w") as fh:
    json.dump(rec, fh, sort_keys=True)
print("[replay-ledger] wrote ledger for %s (%s)" % (cell_id, family))
PY

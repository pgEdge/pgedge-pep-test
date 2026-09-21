#!/usr/bin/env bash
# Reconcile per-family retrieval (all-or-nothing) and compose the certification
# inputs. Runs from a PEP checkout. EVERY value is read from the environment by
# the tested Python module (data, never interpolated into script source); this
# script only sets defaults and invokes it.
#
# Consumed env (set by the workflow): RPM_MATRIX DEB_MATRIX LOGICAL_COMPONENT
# VERSION BUILDNUM TAG CHANNEL RPM_JOB_RESULT DEB_JOB_RESULT plus GITHUB_* and
# GITHUB_OUTPUT. Zero downloaded ledgers is handled inside reconciliation, which
# still writes a truthful failure/skipped result.
set -euo pipefail
export OUT_DIR="${OUT_DIR:-recon-out}"
export LEDGER_DIR="${LEDGER_DIR:-ledgers}"
mkdir -p "$LEDGER_DIR" "$OUT_DIR"
python3 utillities/pep_replay_reconcile.py

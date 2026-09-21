#!/usr/bin/env bash
# Automated nested-marker acceptance. Proves that pep-capture (running nested,
# several levels deep) saw the SAME-RUN [pep-cell:<cell_id>] jobs, package
# artifacts and receipts through the current run's Jobs/Artifacts API.
#
# Locates -- from the current run's paginated Artifacts API -- the single
# non-expired capture-evidence/1 and cert-plan/1 artifacts for THIS run+attempt
# (exact name, expired==false, positive id), downloads them by immutable id,
# safely reads the one root-level JSON from each ZIP, and asserts full acceptance.
#
# Required env: GH_TOKEN EXPECTED_CELLS and GITHUB_* run context.
set -euo pipefail
: "${EXPECTED_CELLS:?}" "${GITHUB_REPOSITORY:?}" "${GITHUB_RUN_ID:?}"
: "${GITHUB_RUN_NUMBER:?}" "${GITHUB_RUN_ATTEMPT:?}"

mkdir -p accept
printf '%s' "$EXPECTED_CELLS" > accept/expected-cells.json

# List every artifact of this run (paginated), one JSON object per line -> array.
gh api --paginate \
  "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/artifacts?per_page=100" \
  -q '.artifacts[]' > accept/arts.ndjson
python3 -c '
import json
arts=[json.loads(l) for l in open("accept/arts.ndjson") if l.strip()]
json.dump(arts, open("accept/arts.json","w"))
print("[replay-accept] %d run artifacts listed" % len(arts))
'

fetch_by_kind() {
  kind="$1"; out="$2"
  id="$(python3 utillities/pep_replay_accept.py select \
          --artifacts-listing accept/arts.json --kind "$kind" \
          --run-number "$GITHUB_RUN_NUMBER" --run-attempt "$GITHUB_RUN_ATTEMPT")"
  echo "[replay-accept] $kind artifact id=$id"
  gh api "repos/${GITHUB_REPOSITORY}/actions/artifacts/${id}/zip" > "accept/${kind}.zip"
  python3 utillities/pep_replay_accept.py extract --zip "accept/${kind}.zip" --kind "$kind" --out "$out"
}

fetch_by_kind capture-evidence accept/capture-evidence.json
fetch_by_kind cert-plan accept/cert-plan.json

python3 utillities/pep_replay_accept.py assert \
  --capture-evidence accept/capture-evidence.json \
  --cert-plan accept/cert-plan.json \
  --expected-cells accept/expected-cells.json \
  --run-id "$GITHUB_RUN_ID" \
  --run-attempt "$GITHUB_RUN_ATTEMPT" \
  --repository "$GITHUB_REPOSITORY"

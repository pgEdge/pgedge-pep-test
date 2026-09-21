#!/usr/bin/env bash
# Retrieve + verify the EXACT published runtime package for one detector cell.
#
# Runs from a PEP checkout (utillities/ present). Resolves the validated retrieval
# plan with the tested Python helper from the cell_id (structured JSON transport --
# no shell `eval`), downloads the exact package inside an arch-matched container
# using package-manager metadata resolution (never a filename), and verifies
# identity. Leaves exactly one runtime package in ./fetch-out.
#
# Untrusted values reach the container ONLY as env vars (PKG_TARGET, CHANNEL),
# which are grammar-validated tokens; nothing is interpolated into script text.
#
# Required env: CELL_ID IMAGE ARCH FAMILY LOGICAL_COMPONENT VERSION BUILDNUM CHANNEL
set -euo pipefail

: "${CELL_ID:?}" "${IMAGE:?}" "${ARCH:?}" "${FAMILY:?}"
: "${LOGICAL_COMPONENT:?}" "${VERSION:?}" "${BUILDNUM:?}" "${CHANNEL:?}"

POLICY="utillities/pep_capture_policy.json"
FETCH_HELPER="utillities/pep_staging_fetch.py"

# Validated retrieval plan (parses the cell_id, rejects PG-coupled, cross-checks
# the matrix family/arch, resolves the canonical package + exact NVR). Structured
# JSON is read field-by-field -- no `eval`.
PLAN="$(python3 "$FETCH_HELPER" plan \
          --component-policy "$POLICY" --logical-component "$LOGICAL_COMPONENT" \
          --cell-id "$CELL_ID" --version "$VERSION" --buildnum "$BUILDNUM" \
          --channel "$CHANNEL" --expect-family "$FAMILY" --expect-arch "$ARCH")"
rj() { printf '%s' "$PLAN" | python3 -c "import json,sys;print(json.load(sys.stdin)$1)"; }
PKG_NAME="$(rj "['package_name']")"
PKG_TARGET="$(rj "['download_target']")"
EXP_VERSION="$(rj "['expected']['version']")"
EXP_RELEASE="$(rj "['expected']['release']")"
export PKG_TARGET CHANNEL

# Retrieve inside an arch-matched container. Fixed script; env-only inputs.
rm -rf fetch-out; mkdir -p fetch-out
ABS_OUT="$(cd fetch-out && pwd)"

# shellcheck disable=SC2016  # $PKG_TARGET/$CHANNEL are expanded INSIDE the container, by design.
if [ "$FAMILY" = "rpm" ]; then
  FETCH='set -euo pipefail
    dnf -y install dnf-plugins-core >/dev/null 2>&1 || dnf -y install "dnf-command(download)" >/dev/null 2>&1 || true
    dnf -y install "https://dnf.pgedge.com/reporpm/pgedge-release-latest.noarch.rpm" >/dev/null
    if [ "$CHANNEL" != "release" ]; then sed -i "s|release|$CHANNEL|g" /etc/yum.repos.d/pgedge.repo; fi
    dnf -y makecache >/dev/null 2>&1 || true
    cd /out && dnf download "$PKG_TARGET" && ls -1 /out'
else
  # shellcheck disable=SC2016
  FETCH='set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update >/dev/null
    apt-get install -y curl ca-certificates >/dev/null
    curl -fsSL "https://apt.pgedge.com/repodeb/pgedge-release_latest_all.deb" -o /tmp/r.deb
    dpkg -i /tmp/r.deb >/dev/null 2>&1 || { apt-get -y -f install >/dev/null; dpkg -i /tmp/r.deb >/dev/null; }
    if [ "$CHANNEL" != "release" ]; then
      sed -i "s|release|$CHANNEL|g" /etc/apt/sources.list.d/pgedge.sources 2>/dev/null \
        || sed -i "s|release|$CHANNEL|g" /etc/apt/sources.list.d/pgedge.list 2>/dev/null || true
    fi
    apt-get update >/dev/null
    cd /out && apt-get download "$PKG_TARGET" && ls -1 /out'
fi

docker run --rm --platform "linux/${ARCH}" \
  -e PKG_TARGET -e CHANNEL \
  -v "${ABS_OUT}:/out" \
  "$IMAGE" bash -c "$FETCH"

# Inspect + verify identity (name/version/release/arch; exactly one runtime).
python3 "$FETCH_HELPER" verify \
  --package-dir fetch-out --family "$FAMILY" \
  --package-name "$PKG_NAME" --exp-version "$EXP_VERSION" \
  --exp-release "$EXP_RELEASE" --arch "$ARCH"
echo "[replay-fetch] verified $PKG_NAME $EXP_VERSION-$EXP_RELEASE ($ARCH) for cell $CELL_ID"

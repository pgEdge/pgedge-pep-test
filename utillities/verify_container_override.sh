#!/usr/bin/env bash
# verify_container_override.sh
# Runs ./run_pep_tf.sh --dry-run for a small matrix of override scenarios
# and asserts on the [container-override] / [container-resolution] log lines.
#
# Exit 0 = all assertions passed. Exit 1 = at least one mismatch (details on stderr).
set -uo pipefail

PASS=0
FAIL=0

_check() {
  local label="$1"; shift
  local pattern="$1"; shift
  local output="$1"; shift
  if grep -qE "$pattern" <<<"$output"; then
    echo "  PASS  $label"
    PASS=$((PASS+1))
  else
    echo "  FAIL  $label"
    echo "        expected pattern: $pattern"
    echo "        got (last 10 lines):"
    echo "$output" | tail -10 | sed 's/^/          /'
    FAIL=$((FAIL+1))
  fi
}

_check_absent() {
  local label="$1"; shift
  local pattern="$1"; shift
  local output="$1"; shift
  if grep -qE "$pattern" <<<"$output"; then
    echo "  FAIL  $label"
    echo "        unexpected pattern: $pattern"
    echo "        offending lines:"
    grep -E "$pattern" <<<"$output" | sed 's/^/          /'
    FAIL=$((FAIL+1))
  else
    echo "  PASS  $label"
    PASS=$((PASS+1))
  fi
}

echo "=== Scenario 1: default path, no override (must be silent on [container-override]) ==="
OUT=$(./run_pep_tf.sh --pgver 17 --platforms rpm --arch arm64 --components server --dry-run 2>&1)
RC=$?
_check_absent "default path silent on [container-override]" '\[container-override\]' "$OUT"
_check "[container-resolution] still emitted" 'container-resolution\] platforms=rpm' "$OUT"
if [[ $RC -eq 0 ]]; then
  echo "  PASS  default path exits 0"; PASS=$((PASS+1))
else
  echo "  FAIL  default path exit code: $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 2: explicit override, fully in scope ==="
OUT=$(./run_pep_tf.sh --pgver 17 --platforms rpm --arch arm64 --components server --containers alma9-arm64 --dry-run 2>&1)
RC=$?
_check "source=cli" 'container-override\] source=cli' "$OUT"
_check "requested line" 'requested: auto-alma9-arm' "$OUT"
_check "effective line (rpm/arm64 narrowed)" 'effective for target rpm/arm64: auto-alma9-arm' "$OUT"
_check "[container-resolution] narrowed to 1" 'platforms=rpm arch=arm64 -> 1 container.*auto-alma9-arm' "$OUT"
if [[ $RC -eq 0 ]]; then
  echo "  PASS  override exits 0"; PASS=$((PASS+1))
else
  echo "  FAIL  override exit code: $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 3: cross-family override under --platforms all ==="
OUT=$(./run_pep_tf.sh --pgver 17 --platforms all --arch arm64 --components server --containers alma9-arm64,debian12-arm64 --dry-run 2>&1)
RC=$?
_check "rpm side: alma9 effective"     'effective for target rpm/arm64: auto-alma9-arm' "$OUT"
_check "deb side: debian12 effective"  'effective for target deb/arm64: auto-debian12-arm' "$OUT"
_check "rpm [container-resolution] -> 1 alma" 'platforms=rpm arch=arm64 -> 1 container.*auto-alma9-arm' "$OUT"
_check "deb [container-resolution] -> 1 debian" 'platforms=deb arch=arm64 -> 1 container.*auto-debian12-arm' "$OUT"
if [[ $RC -eq 0 ]]; then
  echo "  PASS  cross-family override exits 0"; PASS=$((PASS+1))
else
  echo "  FAIL  cross-family override exit code: $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 4: global-zero (--platforms rpm + deb-only override) -> exit 2 ==="
OUT=$(./run_pep_tf.sh --pgver 17 --platforms rpm --arch arm64 --components server --containers debian12-arm64 --dry-run 2>&1)
RC=$?
_check "global-zero diagnostic" 'out of scope for the selected' "$OUT"
if [[ $RC -eq 2 ]]; then
  echo "  PASS  global-zero exits 2"; PASS=$((PASS+1))
else
  echo "  FAIL  global-zero expected exit 2, got $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 5: --target aws + --containers -> fail-fast ==="
OUT=$(./run_pep_tf.sh --target aws --pgver 17 --containers rocky9-arm64 2>&1)
RC=$?
_check "aws+containers diagnostic" 'supported with --target docker' "$OUT"
if [[ $RC -eq 2 ]]; then
  echo "  PASS  aws+containers exits 2"; PASS=$((PASS+1))
else
  echo "  FAIL  aws+containers expected exit 2, got $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 6: 'all' shortcut expands the catalog ==="
OUT=$(./run_pep_tf.sh --pgver 17 --platforms all --components server --containers all --dry-run 2>&1)
RC=$?
_check "all -> source=cli" 'container-override\] source=cli' "$OUT"
# 'all' includes enabled:false entries. Effective set per family/arch may
# vary, but at minimum rpm and deb both have non-empty effective lines.
_check "all -> rpm effective non-empty" 'effective for target rpm/.*: auto-' "$OUT"
_check "all -> deb effective non-empty" 'effective for target deb/.*: auto-' "$OUT"
if [[ $RC -eq 0 ]]; then
  echo "  PASS  'all' shortcut exits 0"; PASS=$((PASS+1))
else
  echo "  FAIL  'all' shortcut exit code: $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 6b: 'all' includes enabled:false catalog entries (shell-layer proof) ==="
# Specifically run rpm/amd64 + --containers all. The catalog currently has
# auto-alma9-amd as enabled:false; a default-path rpm/amd64 run would omit
# it. The 'all' shortcut must pull it back in through the resolver -> shell
# integration path, end-to-end (not just at the unit-test layer).
OUT=$(./run_pep_tf.sh --pgver 17 --platforms rpm --arch amd64 --components server --containers all --dry-run 2>&1)
RC=$?
_check "rpm/amd64 + all: alma9-amd in effective set" 'effective for target rpm/amd64:.*auto-alma9-amd' "$OUT"
_check "rpm/amd64 + all: alma9-amd in [container-resolution]" 'platforms=rpm arch=amd64 ->.*auto-alma9-amd' "$OUT"
if [[ $RC -eq 0 ]]; then
  echo "  PASS  rpm/amd64 + all exits 0"; PASS=$((PASS+1))
else
  echo "  FAIL  rpm/amd64 + all exit code: $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 7: unknown container -> exit 2 with helpful list ==="
OUT=$(./run_pep_tf.sh --pgver 17 --platforms rpm --arch arm64 --components server --containers not-a-real-thing-arm64 --dry-run 2>&1)
RC=$?
_check "unknown container diagnostic" "Unknown container 'not-a-real-thing-arm64'" "$OUT"
_check "valid aliases listed in diagnostic" 'Valid aliases' "$OUT"
if [[ $RC -eq 2 ]]; then
  echo "  PASS  unknown container exits 2"; PASS=$((PASS+1))
else
  echo "  FAIL  unknown container expected exit 2, got $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 8: PEP_CONTAINERS env fallback ==="
OUT=$(PEP_CONTAINERS="alma9-arm64" ./run_pep_tf.sh --pgver 17 --platforms rpm --arch arm64 --components server --dry-run 2>&1)
RC=$?
_check "PEP_CONTAINERS -> source=env" 'container-override\] source=env' "$OUT"
_check "PEP_CONTAINERS -> requested resolves to alma" 'requested: auto-alma9-arm' "$OUT"
if [[ $RC -eq 0 ]]; then
  echo "  PASS  PEP_CONTAINERS env fallback exits 0"; PASS=$((PASS+1))
else
  echo "  FAIL  PEP_CONTAINERS env fallback exit code: $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 9: implicit opposite-arch counterpart accepted (alias form) ==="
# ubuntu2404 exists in the catalog only as -arm; ubuntu2404-amd64 is a genuine
# lookup miss that must synthesize auto-ubuntu2404-amd and resolve to the same
# image (ubuntu:24.04) + platform (linux/amd64) as real execution would.
OUT=$(./run_pep_tf.sh --pgver 17 --platforms deb --arch amd64 --components server --containers ubuntu2404-amd64 --dry-run 2>&1)
RC=$?
_check "counterpart source=cli" 'container-override\] source=cli' "$OUT"
_check "counterpart requested resolves to synthesized name" 'requested: auto-ubuntu2404-amd' "$OUT"
_check "counterpart effective for deb/amd64" 'effective for target deb/amd64: auto-ubuntu2404-amd' "$OUT"
_check "counterpart image/platform resolves" '\[image-resolution\] family=deb container=auto-ubuntu2404-amd image=ubuntu:24.04 platform=linux/amd64' "$OUT"
if [[ $RC -eq 0 ]]; then
  echo "  PASS  counterpart (alias) exits 0"; PASS=$((PASS+1))
else
  echo "  FAIL  counterpart (alias) exit code: $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 10: implicit counterpart accepted (canonical-name form) ==="
OUT=$(./run_pep_tf.sh --pgver 17 --platforms deb --arch amd64 --components server --containers auto-ubuntu2404-amd --dry-run 2>&1)
RC=$?
_check "counterpart (name) effective for deb/amd64" 'effective for target deb/amd64: auto-ubuntu2404-amd' "$OUT"
_check "counterpart (name) image/platform resolves" '\[image-resolution\] family=deb container=auto-ubuntu2404-amd image=ubuntu:24.04 platform=linux/amd64' "$OUT"
if [[ $RC -eq 0 ]]; then
  echo "  PASS  counterpart (name) exits 0"; PASS=$((PASS+1))
else
  echo "  FAIL  counterpart (name) exit code: $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 11: counterpart out of scope for the wrong arch target -> exit 2 ==="
# The counterpart is deb/amd64; asking for it under --arch arm64 is global-zero.
OUT=$(./run_pep_tf.sh --pgver 17 --platforms deb --arch arm64 --components server --containers ubuntu2404-amd64 --dry-run 2>&1)
RC=$?
_check "counterpart wrong-arch diagnostic" 'out of scope for the selected' "$OUT"
if [[ $RC -eq 2 ]]; then
  echo "  PASS  counterpart wrong-arch exits 2"; PASS=$((PASS+1))
else
  echo "  FAIL  counterpart wrong-arch expected exit 2, got $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 12: alias + canonical of the SAME synthesized counterpart dedup to one ==="
OUT=$(./run_pep_tf.sh --pgver 17 --platforms deb --arch amd64 --components server --containers ubuntu2404-amd64,auto-ubuntu2404-amd --dry-run 2>&1)
RC=$?
_check "synthesized dedup emitted" "dedup'd" "$OUT"
_check "synthesized dedup -> exactly 1 container" 'platforms=deb arch=amd64 -> 1 container.*auto-ubuntu2404-amd' "$OUT"
if [[ $RC -eq 0 ]]; then
  echo "  PASS  synthesized dedup exits 0"; PASS=$((PASS+1))
else
  echo "  FAIL  synthesized dedup exit code: $RC"; FAIL=$((FAIL+1))
fi

echo "=== Scenario 13: 'all' does NOT include implicit counterparts ==="
# Under deb/amd64, 'all' yields only real amd64 deb entries (debian13-amd,
# ubuntu2604-amd). The synthesized ubuntu2404 counterpart must be absent.
OUT=$(./run_pep_tf.sh --pgver 17 --platforms deb --arch amd64 --components server --containers all --dry-run 2>&1)
RC=$?
_check_absent "'all' excludes synthesized ubuntu2404-amd" 'auto-ubuntu2404-amd' "$OUT"
_check "'all' still yields real deb/amd64 entries" 'effective for target deb/amd64:.*auto-' "$OUT"
if [[ $RC -eq 0 ]]; then
  echo "  PASS  'all' excludes counterparts exits 0"; PASS=$((PASS+1))
else
  echo "  FAIL  'all' excludes counterparts exit code: $RC"; FAIL=$((FAIL+1))
fi

echo ""
echo "summary: PASS=$PASS  FAIL=$FAIL"
[[ $FAIL -eq 0 ]]

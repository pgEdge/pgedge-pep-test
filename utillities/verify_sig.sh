#!/bin/bash

# SBOM Signature Verification Script
# Verifies all SBOM files in the current directory using sq verify

set -uo pipefail

# Configuration
SIGNER_FILE="/tmp/pgedge.pub"
SBOM_PATTERN="*-sbom.json"
SIG_EXTENSION=".asc"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Counters
total=0
passed=0
failed=0

echo "================================================"
echo "SBOM Signature Verification"
echo "================================================"
echo ""

# Check if sq command exists
if ! command -v sq &> /dev/null; then
    echo -e "${RED}Error: 'sq' command not found. Please install Sequoia PGP.${NC}"
    exit 1
fi

# Check if signer file exists
if [[ ! -f "$SIGNER_FILE" ]]; then
    echo -e "${RED}Error: Signer file not found: $SIGNER_FILE${NC}"
    exit 1
fi

echo -e "${BLUE}Using signer file:${NC} $SIGNER_FILE"
echo ""

# Find all SBOM files
shopt -s nullglob
sbom_files=($SBOM_PATTERN)
shopt -u nullglob

if [[ ${#sbom_files[@]} -eq 0 ]]; then
    echo -e "${YELLOW}Warning: No SBOM files found matching pattern: $SBOM_PATTERN${NC}"
    exit 0
fi

echo -e "${BLUE}Found ${#sbom_files[@]} SBOM file(s) to verify${NC}"
echo ""
echo "Starting verification..."
echo ""

# Verify each SBOM file
for sbom_file in "${sbom_files[@]}"; do
    [[ ! -f "$sbom_file" ]] && continue
    sig_file="${sbom_file}${SIG_EXTENSION}"

    ((total++))

    echo "[$total] Verifying: $sbom_file"

    # Check if signature file exists
    if [[ ! -f "$sig_file" ]]; then
        echo -e "    ${RED}✗ FAILED${NC} - Signature file not found: $sig_file"
        ((failed++))
        echo ""
        continue
    fi

    # Verify signature and capture output
    verify_output=$(sq verify --signer-file "$SIGNER_FILE" --detached "$sig_file" "$sbom_file" 2>&1)
    verify_status=$?

    if [[ $verify_status -eq 0 ]]; then
        echo -e "    ${GREEN}✓ PASSED${NC}"
        echo "$verify_output" | sed 's/^/    /'
        ((passed++))
    else
        echo -e "    ${RED}✗ FAILED${NC}"
        echo "$verify_output" | sed 's/^/    /'
        ((failed++))
    fi

    echo ""
done

# Summary
echo "================================================"
echo "Verification Summary"
echo "================================================"
echo "Total files checked: $total"
echo -e "Passed: ${GREEN}$passed${NC}"
echo -e "Failed: ${RED}$failed${NC}"
echo ""

# Exit with appropriate code
if [[ $failed -gt 0 ]]; then
    echo -e "${RED}❌ Some verifications failed!${NC}"
    exit 1
else
    echo -e "${GREEN}✅ All verifications passed!${NC}"
    exit 0
fi
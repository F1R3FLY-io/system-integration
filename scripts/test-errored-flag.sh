#!/usr/bin/env bash
# test-errored-flag.sh — Test whether the observer reports errored deploys correctly
#
# Deploys two contracts:
# 1. A successful deploy (should have errored=false)
# 2. A deploy that calls abort!() (should have errored=true)
#
# Then checks what the observer's /api/deploy/{id} and embers'
# /api/service/deploys/{id}/status return for each.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_CLI="$SCRIPT_DIR/../services/rust-client/target/release/node_cli"
PRIVATE_KEY="5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"
OBSERVER="http://localhost:40453"
EMBERS="http://localhost:8080"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
info() { echo -e "${YELLOW}[TEST]${NC} $1"; }

echo "=== Errored Flag Test ==="
echo ""

# Test 1: Successful deploy
info "Test 1: Deploy a successful contract"
cat << 'RHOEOF' > "$SCRIPT_DIR/.test-success.rho"
new stdout(`rho:io:stdout`) in {
    stdout!("errored-flag-test: SUCCESS deploy")
}
RHOEOF

OUTPUT=$($NODE_CLI deploy-and-wait \
    --host localhost --port 40411 --http-port 40413 \
    --observer-host localhost --observer-port 40451 \
    --private-key "$PRIVATE_KEY" \
    --file "$SCRIPT_DIR/.test-success.rho" 2>&1)
SUCCESS_ID=$(echo "$OUTPUT" | grep "Deploy ID:" | sed 's/.*Deploy ID: //')
echo "  Deploy ID: ${SUCCESS_ID:0:40}..."
pass "Successful deploy finalized"

# Test 2: Errored deploy (abort)
info "Test 2: Deploy a contract that aborts"
cat << 'RHOEOF' > "$SCRIPT_DIR/.test-abort.rho"
new abort(`rho:execution:abort`), stdout(`rho:io:stdout`) in {
    stdout!("errored-flag-test: ABOUT TO ABORT") |
    abort!("intentional test abort")
}
RHOEOF

OUTPUT=$($NODE_CLI deploy-and-wait \
    --host localhost --port 40411 --http-port 40413 \
    --observer-host localhost --observer-port 40451 \
    --private-key "$PRIVATE_KEY" \
    --file "$SCRIPT_DIR/.test-abort.rho" 2>&1)
ABORT_ID=$(echo "$OUTPUT" | grep "Deploy ID:" | sed 's/.*Deploy ID: //')
echo "  Deploy ID: ${ABORT_ID:0:40}..."
pass "Abort deploy finalized"

echo ""
info "Checking observer /api/deploy/{id} responses..."

# Check successful deploy on observer
echo ""
info "Observer response for SUCCESS deploy:"
RESP=$(curl -s "$OBSERVER/api/deploy/$SUCCESS_ID" 2>&1)
echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  blockHash: {d.get(\"blockHash\",\"?\")[:20]}...')
print(f'  blockNumber: {d.get(\"blockNumber\",\"?\")}')
print(f'  errored: {d.get(\"errored\",\"MISSING\")}')
print(f'  cost: {d.get(\"cost\",\"?\")}')
# Show all top-level keys
print(f'  keys: {list(d.keys())}')
"

echo ""
info "Observer response for ABORT deploy:"
RESP=$(curl -s "$OBSERVER/api/deploy/$ABORT_ID" 2>&1)
echo "$RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  blockHash: {d.get(\"blockHash\",\"?\")[:20]}...')
print(f'  blockNumber: {d.get(\"blockNumber\",\"?\")}')
print(f'  errored: {d.get(\"errored\",\"MISSING\")}')
print(f'  cost: {d.get(\"cost\",\"?\")}')
print(f'  keys: {list(d.keys())}')
"

# Check via embers status endpoint
echo ""
info "Checking embers /api/service/deploys/{id}/status responses..."

echo ""
info "Embers status for SUCCESS deploy:"
curl -s "$EMBERS/api/service/deploys/$SUCCESS_ID/status" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  found: {d.get(\"found\")}')
print(f'  finalized: {d.get(\"finalized\")}')
print(f'  errored: {d.get(\"errored\")}')
print(f'  block_number: {d.get(\"block_number\")}')
"

echo ""
info "Embers status for ABORT deploy:"
curl -s "$EMBERS/api/service/deploys/$ABORT_ID/status" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  found: {d.get(\"found\")}')
print(f'  finalized: {d.get(\"finalized\")}')
print(f'  errored: {d.get(\"errored\")}')
print(f'  block_number: {d.get(\"block_number\")}')
"

# Also check validator logs for the abort
echo ""
info "Validator log for abort deploy:"
docker logs rnode.validator1 2>&1 | grep "intentional test abort" | tail -2

echo ""
echo "=== Test Complete ==="

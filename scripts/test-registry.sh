#!/usr/bin/env bash
# test-registry.sh — Test registry operations directly on the shard
# Usage: ./scripts/test-registry.sh
#
# Tests:
# 1. explore-deploy connectivity
# 2. registry lookup (rl!) for a known system URI
# 3. registry insertRandom (rr!) — creates a new registry entry
# 4. registry insertSigned (rs!) — the operation that embers uses

set -euo pipefail

OBSERVER=${OBSERVER_URL:-http://localhost:40453}
VALIDATOR=${VALIDATOR_URL:-http://localhost:40401}

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

echo "=== Registry Diagnostics ==="
echo "Observer: $OBSERVER"
echo ""

# Test 1: Basic explore-deploy works
info "Test 1: explore-deploy connectivity"
RESULT=$(curl -s "$OBSERVER/api/explore-deploy" \
  -H 'Content-Type: application/json' \
  -d '{"term": "@0!(42)"}')
if echo "$RESULT" | grep -q '"expr"'; then
    pass "explore-deploy returns valid response"
else
    fail "explore-deploy failed: $RESULT"
    exit 1
fi

# Test 2: Registry lookup for treeHashMap (known system URI)
info "Test 2: registry lookup (rl!) for rho:lang:treeHashMap"
RESULT=$(curl -s "$OBSERVER/api/explore-deploy" \
  -H 'Content-Type: application/json' \
  -d '{"term": "new ret, rl(`rho:registry:lookup`) in { rl!(`rho:lang:treeHashMap`, *ret) }"}')
EXPR=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR" -gt 0 ]; then
    pass "registry lookup works (treeHashMap found)"
else
    fail "registry lookup returned empty: $(echo "$RESULT" | head -c 200)"
fi

# Test 3: Registry insertRandom
info "Test 3: registry insertRandom (rr!)"
RESULT=$(curl -s "$OBSERVER/api/explore-deploy" \
  -H 'Content-Type: application/json' \
  -d '{"term": "new ret, rr(`rho:registry:insertRandom`) in { rr!(\"test-value\", *ret) }"}')
EXPR=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR" -gt 0 ]; then
    pass "insertRandom works"
    URI=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['expr'][0])" 2>/dev/null || echo "unknown")
    info "  returned: $(echo "$URI" | head -c 100)"
else
    fail "insertRandom returned empty (may need a real deploy, not explore-deploy)"
    info "  This is expected — insertRandom requires a real deploy to modify state"
fi

# Test 4: Check if embers env URIs exist
info "Test 4: Check embers agents_teams env URI"
# The agents_teams env_key from embers.env: 85348C6D6AEF0B4761F8B8047111B3A2F7C9DF8CB24F91B66B77893DDE21DEE5
# This produces a deterministic URI. Let's check what embers tries to look up.
RESULT=$(curl -s "$OBSERVER/api/explore-deploy" \
  -H 'Content-Type: application/json' \
  -d '{"term": "new ret, rl(`rho:registry:lookup`) in { rl!(`rho:id:p1k4nm1iccsfusn76dz9pxndh9tacfiezigry4f5e9upajxqepbirq`, *ret) }"}')
EXPR=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR" -gt 0 ]; then
    pass "agents env URI found in registry"
else
    info "agents env URI NOT in registry (expected on fresh shard before bootstrap)"
fi

# Test 5: Check vault address system process
info "Test 5: Check rho:vault:address system process"
RESULT=$(curl -s "$OBSERVER/api/explore-deploy" \
  -H 'Content-Type: application/json' \
  -d '{"term": "new ret, va(`rho:vault:address`) in { va!(\"fromPublicKey\", \"04baad1a03c988ea4d4d0e637694364b38c4305aab9b979a5221f204d074a675eae04782e9120c02ad49b69623cb23c4192becc6605f6c1d8695bc84e704889f07\".hexToBytes(), *ret) }"}')
EXPR=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR" -gt 0 ]; then
    pass "rho:vault:address works"
    ADDR=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); e=d['expr'][0]; print(list(e.values())[0].get('data',''))" 2>/dev/null || echo "unknown")
    info "  address: $ADDR"
else
    # Try the old URI
    info "rho:vault:address returned empty, trying rho:rev:address..."
    RESULT=$(curl -s "$OBSERVER/api/explore-deploy" \
      -H 'Content-Type: application/json' \
      -d '{"term": "new ret, va(`rho:rev:address`) in { va!(\"fromPublicKey\", \"04baad1a03c988ea4d4d0e637694364b38c4305aab9b979a5221f204d074a675eae04782e9120c02ad49b69623cb23c4192becc6605f6c1d8695bc84e704889f07\".hexToBytes(), *ret) }"}')
    EXPR2=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
    if [ "$EXPR2" -gt 0 ]; then
        fail "rho:vault:address DOES NOT EXIST but rho:rev:address DOES — URI mismatch!"
    else
        fail "Neither rho:vault:address nor rho:rev:address works"
    fi
fi

# Test 6: Check secp256k1 verify system process
info "Test 6: Check secp256k1 verify"
RESULT=$(curl -s "$OBSERVER/api/explore-deploy" \
  -H 'Content-Type: application/json' \
  -d '{"term": "new ret, verify(`rho:crypto:secp256k1Verify`) in { ret!(\"secp256k1Verify exists\") }"}')
EXPR=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR" -gt 0 ]; then
    pass "secp256k1Verify system process accessible"
else
    fail "secp256k1Verify not found"
fi

echo ""
echo "=== Diagnostics Complete ==="

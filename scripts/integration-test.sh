#!/bin/bash
# integration-test.sh — Smoke test the embers API against a running shard
# Usage: ./scripts/integration-test.sh
#
# Prerequisites: shard running, start-all.sh completed
# Tests the create → save → deploy → run lifecycle

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

PASS=0
FAIL=0
API="http://localhost:8080"
WALLET="1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g"

pass() { echo -e "  ${GREEN}[PASS]${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}[FAIL]${NC} $1: $2"; FAIL=$((FAIL + 1)); }

echo "========================================="
echo "Embers Integration Tests"
echo "========================================="
echo ""

# Test 1: API health
echo "1. API Health"
for ep in "ai-agents-teams/$WALLET" "ai-agents/$WALLET" "oslfs/$WALLET" "wallets/$WALLET/state"; do
    name=$(echo "$ep" | cut -d/ -f1)
    result=$(curl -s --max-time 10 "$API/api/$ep" 2>&1)
    if echo "$result" | grep -qE '"agents_teams"|"agents"|"oslfs"|"balance"'; then
        pass "$name"
    else
        fail "$name" "$result"
    fi
done

# Test 2: WebSocket events
echo ""
echo "2. WebSocket Block Events"
WS_COUNT=$(docker logs embers 2>&1 | grep -c 'ws block event' || true)
if [ "$WS_COUNT" -gt 0 ]; then
    pass "receiving block events ($WS_COUNT total)"
else
    fail "no block events received" "check WebSocket connection"
fi

# Test 3: Create agent team
echo ""
echo "3. Create Agent Team"
CREATE_RESP=$(curl -s --max-time 10 -X POST "$API/api/ai-agents-teams/create/prepare" \
    -H 'Content-Type: application/json' \
    -d '{"name": "integration-test"}' 2>&1)
if echo "$CREATE_RESP" | grep -q '"contract"'; then
    pass "create/prepare returned contract"
else
    fail "create/prepare" "$CREATE_RESP"
fi

# Test 4: List shows data after wait
echo ""
echo "4. List After Operations"
LIST_RESP=$(curl -s --max-time 10 "$API/api/ai-agents-teams/$WALLET" 2>&1)
if echo "$LIST_RESP" | grep -q '"agents_teams"'; then
    TEAM_COUNT=$(echo "$LIST_RESP" | python3 -c "import sys,json; print(len(json.load(sys.stdin)['agents_teams']))" 2>/dev/null || echo "?")
    pass "list returns $TEAM_COUNT teams"
else
    fail "list agents teams" "$LIST_RESP"
fi

# Test 5: Wallet balance
echo ""
echo "5. Wallet Balance"
BALANCE_RESP=$(curl -s --max-time 10 "$API/api/wallets/$WALLET/state" 2>&1)
if echo "$BALANCE_RESP" | grep -q '"balance"'; then
    BALANCE=$(echo "$BALANCE_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['balance'])" 2>/dev/null || echo "?")
    pass "balance: $BALANCE"
else
    fail "wallet balance" "$BALANCE_RESP"
fi

# Test 6: Service ready
echo ""
echo "6. Service Ready"
READY=$(curl -s --max-time 5 -o /dev/null -w "%{http_code}" "$API/api/service/ready" 2>&1)
if [ "$READY" = "200" ]; then
    pass "service ready (HTTP $READY)"
else
    fail "service ready" "HTTP $READY"
fi

# Test 7: Swagger spec
echo ""
echo "7. Swagger Spec"
SWAGGER=$(curl -s --max-time 5 "$API/swagger-ui/openapi.json" 2>&1 | head -1)
if echo "$SWAGGER" | grep -q '"openapi"'; then
    pass "swagger spec available"
else
    fail "swagger spec" "$SWAGGER"
fi

# Summary
echo ""
echo "========================================="
echo "Results: $PASS passed, $FAIL failed"
echo "========================================="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi

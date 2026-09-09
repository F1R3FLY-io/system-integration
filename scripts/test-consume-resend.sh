#!/usr/bin/env bash
# test-consume-resend.sh — Test consume+resend pattern on the shard
#
# Proves whether the consume+resend workaround (used instead of peek)
# corrupts tuplespace state after multiple sequential operations.
#
# Usage: ./scripts/test-consume-resend.sh

set -euo pipefail

VALIDATOR_GRPC="localhost:40411"
VALIDATOR_HTTP="localhost:40413"
OBSERVER_HTTP="localhost:40453"
OBSERVER_GRPC="localhost:40451"
NODE_CLI="/Users/spreston/src/firefly/system-integration/services/rust-client/target/release/node_cli"
PRIVATE_KEY="5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
info() { echo -e "${YELLOW}[TEST]${NC} $1"; }

deploy_and_wait() {
    local file="$1"
    local label="$2"
    info "Deploying: $label"
    local output
    output=$($NODE_CLI deploy-and-wait \
        --host localhost --port 40411 --http-port 40413 \
        --observer-host localhost --observer-port 40451 \
        --private-key "$PRIVATE_KEY" \
        --bigger-phlo \
        --file "$file" 2>&1)
    local rc=$?
    if [ $rc -ne 0 ]; then
        fail "Deploy failed: $label"
        echo "$output" | tail -5
        return 1
    fi
    local deploy_time
    deploy_time=$(echo "$output" | grep "Total time" | grep -o '[0-9.]*s' || echo "?")
    pass "Deployed: $label ($deploy_time)"
    return 0
}

explore() {
    local term="$1"
    curl -s "$OBSERVER_HTTP/api/explore-deploy" \
        -H 'Content-Type: application/json' \
        -d "{\"term\": $(echo "$term" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')}" 2>&1
}

echo "=== Consume+Resend Tuplespace Corruption Test ==="
echo ""

# -----------------------------------------------------------------------
# Test 1: Basic consume+resend — single channel, read after write
# -----------------------------------------------------------------------
info "Test 1: Basic consume+resend — write then read"

cat << 'RHOEOF' > /tmp/test-cr-init.rho
new rl(`rho:registry:insertRandom`), dataCh in {
    dataCh!({"count": 0, "items": []}) |

    contract @"cr-test-read"(ret) = {
        for(@data <- dataCh) {
            dataCh!(data) |
            ret!(data)
        }
    } |

    contract @"cr-test-write"(@item, ret) = {
        for(@data <- dataCh) {
            dataCh!(data.set("count", data.get("count") + 1).set("items", data.get("items") ++ [item])) |
            ret!("ok")
        }
    } |

    rl!(bundle+{*dataCh}, *rl)
}
RHOEOF

deploy_and_wait /tmp/test-cr-init.rho "Test 1: Init consume+resend contract"

info "Test 1a: Read initial state"
RESULT=$(explore 'new ret in { @"cr-test-read"!(*ret) }')
EXPR_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR_COUNT" -gt 0 ]; then
    DATA=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['expr'][0]))" 2>/dev/null)
    pass "Read initial state: $DATA"
else
    fail "Read initial state returned empty (expr count: $EXPR_COUNT)"
    echo "  Response: $(echo "$RESULT" | head -c 200)"
fi

# -----------------------------------------------------------------------
# Test 2: Sequential writes then read
# -----------------------------------------------------------------------
info "Test 2: Sequential writes (3x) then read"

cat << 'RHOEOF' > /tmp/test-cr-write1.rho
new ret in { @"cr-test-write"!("item-1", *ret) }
RHOEOF

cat << 'RHOEOF' > /tmp/test-cr-write2.rho
new ret in { @"cr-test-write"!("item-2", *ret) }
RHOEOF

cat << 'RHOEOF' > /tmp/test-cr-write3.rho
new ret in { @"cr-test-write"!("item-3", *ret) }
RHOEOF

deploy_and_wait /tmp/test-cr-write1.rho "Test 2a: Write item-1"

info "Test 2a: Read after write 1"
RESULT=$(explore 'new ret in { @"cr-test-read"!(*ret) }')
EXPR_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR_COUNT" -gt 0 ]; then
    DATA=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['expr'][0]))" 2>/dev/null)
    pass "After write 1: $DATA"
else
    fail "After write 1: empty (channel consumed and not resent?)"
fi

deploy_and_wait /tmp/test-cr-write2.rho "Test 2b: Write item-2"

info "Test 2b: Read after write 2"
RESULT=$(explore 'new ret in { @"cr-test-read"!(*ret) }')
EXPR_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR_COUNT" -gt 0 ]; then
    DATA=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['expr'][0]))" 2>/dev/null)
    pass "After write 2: $DATA"
else
    fail "After write 2: empty (tuplespace corrupted)"
fi

deploy_and_wait /tmp/test-cr-write3.rho "Test 2c: Write item-3"

info "Test 2c: Read after write 3"
RESULT=$(explore 'new ret in { @"cr-test-read"!(*ret) }')
EXPR_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR_COUNT" -gt 0 ]; then
    DATA=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['expr'][0]))" 2>/dev/null)
    pass "After write 3: $DATA"
else
    fail "After write 3: empty (tuplespace corrupted after sequential writes)"
fi

# -----------------------------------------------------------------------
# Test 3: Rapid writes in same block (concurrent consumes)
# -----------------------------------------------------------------------
info "Test 3: Two writes in same deploy (concurrent consume race)"

cat << 'RHOEOF' > /tmp/test-cr-concurrent.rho
new ret1, ret2 in {
    @"cr-test-write"!("concurrent-A", *ret1) |
    @"cr-test-write"!("concurrent-B", *ret2)
}
RHOEOF

deploy_and_wait /tmp/test-cr-concurrent.rho "Test 3: Concurrent writes"

info "Test 3: Read after concurrent writes"
RESULT=$(explore 'new ret in { @"cr-test-read"!(*ret) }')
EXPR_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR_COUNT" -gt 0 ]; then
    DATA=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['expr'][0]))" 2>/dev/null)
    pass "After concurrent writes: $DATA"
else
    fail "After concurrent writes: empty (concurrent consume race destroyed channel)"
fi

# -----------------------------------------------------------------------
# Test 4: Embers-like pattern — registry lookup + treeHashMap operations
# -----------------------------------------------------------------------
info "Test 4: Embers-like treeHashMap consume+resend"

cat << 'RHOEOF' > /tmp/test-embers-init.rho
new rl(`rho:registry:lookup`), treeHashMapCh in {
    rl!(`rho:lang:treeHashMap`, *treeHashMapCh) |
    for(treeHashMap <- treeHashMapCh) {
        treeHashMap!("init", 3, *treeHashMapCh) |
        for(@map <- treeHashMapCh) {
            treeHashMapCh!(*treeHashMap, map) |

            contract @"embers-test-set"(@key, @value, ret) = {
                for(treeHashMap, @map <- treeHashMapCh) {
                    treeHashMapCh!(*treeHashMap, map) |
                    treeHashMap!("set", map, key, value, *ret)
                }
            } |

            contract @"embers-test-get"(@key, ret) = {
                for(treeHashMap, @map <- treeHashMapCh) {
                    treeHashMapCh!(*treeHashMap, map) |
                    treeHashMap!("get", map, key, *ret)
                }
            } |

            contract @"embers-test-list"(ret) = {
                for(treeHashMap, @map <- treeHashMapCh) {
                    treeHashMapCh!(*treeHashMap, map) |
                    treeHashMap!("toMap", map, *ret)
                }
            }
        }
    }
}
RHOEOF

deploy_and_wait /tmp/test-embers-init.rho "Test 4: Init treeHashMap contract"

info "Test 4a: Set key 'team1'"
cat << 'RHOEOF' > /tmp/test-embers-set1.rho
new ret in { @"embers-test-set"!("team1", {"name": "Alpha", "version": 1}, *ret) }
RHOEOF
deploy_and_wait /tmp/test-embers-set1.rho "Test 4a: Set team1"

info "Test 4a: Read team1"
RESULT=$(explore 'new ret in { @"embers-test-get"!("team1", *ret) }')
EXPR_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR_COUNT" -gt 0 ]; then
    DATA=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['expr'][0]))" 2>/dev/null)
    pass "team1: $DATA"
else
    fail "team1: not found"
fi

info "Test 4b: Set key 'team2'"
cat << 'RHOEOF' > /tmp/test-embers-set2.rho
new ret in { @"embers-test-set"!("team2", {"name": "Beta", "version": 1}, *ret) }
RHOEOF
deploy_and_wait /tmp/test-embers-set2.rho "Test 4b: Set team2"

info "Test 4b: Read team2"
RESULT=$(explore 'new ret in { @"embers-test-get"!("team2", *ret) }')
EXPR_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR_COUNT" -gt 0 ]; then
    DATA=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['expr'][0]))" 2>/dev/null)
    pass "team2: $DATA"
else
    fail "team2: not found (treeHashMap corrupted after second set)"
fi

info "Test 4c: Read team1 again (should still exist)"
RESULT=$(explore 'new ret in { @"embers-test-get"!("team1", *ret) }')
EXPR_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR_COUNT" -gt 0 ]; then
    pass "team1 still readable after team2 set"
else
    fail "team1 LOST after team2 set (consume+resend corruption)"
fi

info "Test 4d: List all"
RESULT=$(explore 'new ret in { @"embers-test-list"!(*ret) }')
EXPR_COUNT=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR_COUNT" -gt 0 ]; then
    DATA=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['expr'][0]))" 2>/dev/null)
    pass "List all: $DATA"
else
    fail "List returned empty (treeHashMap channel consumed)"
fi

echo ""
echo "=== Test Complete ==="

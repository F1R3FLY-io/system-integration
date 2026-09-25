#!/usr/bin/env bash
# test-triple-consume.sh — Reproduce the treeHashMapCh consume+resend flakiness
#
# Deploys a simplified version of agents_teams/init.rho with stdout logging,
# then runs sequential operations (create-like, recordDeploy-like) to see
# if/when the treeHashMapCh channel gets stuck.
#
# Usage: ./scripts/test-triple-consume.sh

set -euo pipefail

NODE_CLI="/Users/spreston/src/firefly/system-integration/services/rust-client/target/release/node_cli"
PRIVATE_KEY="5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"
OBSERVER="http://localhost:40453"

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
    pass "Deployed: $label"
    return 0
}

explore() {
    local term="$1"
    curl -s "$OBSERVER/api/explore-deploy" \
        -H 'Content-Type: application/json' \
        -d "{\"term\": $(echo "$term" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')}" 2>&1
}

check_stdout() {
    echo ""
    info "Validator stdout (Rholang logging):"
    docker logs rnode.validator1 2>&1 | grep "STDOUT\|stdout" | tail -20
}

echo "=== Triple Consume Repro Test ==="
echo ""

# Step 1: Deploy the test contract
info "Step 1: Deploy test contract with nested consume+resend"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
deploy_and_wait "$SCRIPT_DIR/test-triple-consume.rho" "Init triple-consume contract"
check_stdout

# Step 2: Read the data to verify setup
info "Step 2: Verify initial data"
RESULT=$(explore 'new ret in { @"readData"!("addr1", *ret) }')
EXPR=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR" -gt 0 ]; then
    pass "Initial data readable"
else
    fail "Initial data NOT readable (setup may have failed)"
fi

# Step 3: Call recordDeploy (3 sequential consumes)
info "Step 3: Call recordDeploy (triple consume)"
cat << 'RHOEOF' > /Users/spreston/src/firefly/system-integration/scripts/.test-record1.rho
new ret, stdout(`rho:io:stdout`) in {
    stdout!("=== recordDeploy call 1 ===") |
    @"recordDeploy"!("addr1", "team1", {"name": "Alpha", "version": "v1", "deployed": true, "uri": "rho:id:test1"}, *ret)
}
RHOEOF
deploy_and_wait /Users/spreston/src/firefly/system-integration/scripts/.test-record1.rho "recordDeploy call 1"
check_stdout

# Step 4: Read again — did it survive?
info "Step 4: Read after recordDeploy 1"
RESULT=$(explore 'new ret in { @"readData"!("addr1", *ret) }')
EXPR=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR" -gt 0 ]; then
    pass "Data still readable after recordDeploy 1"
else
    fail "Data NOT readable after recordDeploy 1 (channel stuck!)"
fi

# Step 5: Call recordDeploy again
info "Step 5: Call recordDeploy again (second triple consume)"
cat << 'RHOEOF' > /Users/spreston/src/firefly/system-integration/scripts/.test-record2.rho
new ret, stdout(`rho:io:stdout`) in {
    stdout!("=== recordDeploy call 2 ===") |
    @"recordDeploy"!("addr1", "team1", {"name": "Alpha", "version": "v1", "deployed": true, "uri": "rho:id:test2"}, *ret)
}
RHOEOF
deploy_and_wait /Users/spreston/src/firefly/system-integration/scripts/.test-record2.rho "recordDeploy call 2"

# Step 6: Read again
info "Step 6: Read after recordDeploy 2"
RESULT=$(explore 'new ret in { @"readData"!("addr1", *ret) }')
EXPR=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR" -gt 0 ]; then
    pass "Data still readable after recordDeploy 2"
else
    fail "Data NOT readable after recordDeploy 2 (channel stuck!)"
fi

# Step 7: Rapid fire — 3 recordDeploys quickly
info "Step 7: Rapid fire — submit 3 deploys quickly"
for i in 3 4 5; do
    cat << RHOEOF > /Users/spreston/src/firefly/system-integration/scripts/.test-record${i}.rho
new ret, stdout(\`rho:io:stdout\`) in {
    stdout!("=== recordDeploy call ${i} ===") |
    @"recordDeploy"!("addr1", "team1", {"name": "Alpha", "version": "v1", "deployed": true, "uri": "rho:id:test${i}"}, *ret)
}
RHOEOF
    deploy_and_wait /Users/spreston/src/firefly/system-integration/scripts/.test-record${i}.rho "recordDeploy call ${i}"
done

# Step 8: Final read
info "Step 8: Read after rapid fire"
RESULT=$(explore 'new ret in { @"readData"!("addr1", *ret) }')
EXPR=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('expr',[])))" 2>/dev/null || echo "0")
if [ "$EXPR" -gt 0 ]; then
    pass "Data still readable after all operations"
else
    fail "Data NOT readable after rapid fire (channel stuck!)"
fi

check_stdout

echo ""
echo "=== Test Complete ==="

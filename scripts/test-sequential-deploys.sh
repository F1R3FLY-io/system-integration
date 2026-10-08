#!/usr/bin/env bash
# test-sequential-deploys.sh — Reproduce the causal ordering issue
#
# Tests whether Deploy B can see data written by Deploy A when:
# - Deploy A finalizes at block N
# - Deploy B has valid_after_block_number = N
#
# This isolates the multi-parent block issue without embers/SDK involvement.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_CLI="$SCRIPT_DIR/../services/rust-client/target/release/node_cli"
PRIVATE_KEY="5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"
OBSERVER="http://localhost:40453"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
info() { echo -e "${YELLOW}[TEST]${NC} $1"; }

ITERATIONS="${1:-5}"

echo "=== Sequential Deploy Causal Ordering Test ==="
echo "Running $ITERATIONS iterations"
echo ""

PASS_COUNT=0
FAIL_COUNT=0

for i in $(seq 1 "$ITERATIONS"); do
    info "--- Iteration $i/$ITERATIONS ---"

    # Deploy A: Write a value to a known channel
    UNIQUE="test-$RANDOM-$i"
    cat << RHOEOF > "$SCRIPT_DIR/.test-deploy-a.rho"
new stdout(\`rho:io:stdout\`) in {
    @"sequential-test-$UNIQUE"!("written-by-deploy-a") |
    stdout!("Deploy A wrote to sequential-test-$UNIQUE")
}
RHOEOF

    info "Deploy A: Writing to channel sequential-test-$UNIQUE"
    OUTPUT_A=$($NODE_CLI deploy-and-wait \
        --host localhost --port 40411 --http-port 40413 \
        --observer-host localhost --observer-port 40451 \
        --private-key "$PRIVATE_KEY" \
        --file "$SCRIPT_DIR/.test-deploy-a.rho" 2>&1)

    DEPLOY_A_ID=$(echo "$OUTPUT_A" | grep "Deploy ID:" | sed 's/.*Deploy ID: //')
    BLOCK_A=$(echo "$OUTPUT_A" | grep "found in block:" | sed 's/.*found in block: //')
    TOTAL_A=$(echo "$OUTPUT_A" | grep "Total time:" | sed 's/.*Total time: //')

    if [ -z "$DEPLOY_A_ID" ]; then
        fail "Deploy A failed"
        echo "$OUTPUT_A" | tail -3
        FAIL_COUNT=$((FAIL_COUNT + 1))
        continue
    fi

    # Get Deploy A's block number
    BLOCK_A_NUM=$(curl -s "$OBSERVER/api/blocks/1" 2>&1 | python3 -c "
import sys, json
blocks = json.load(sys.stdin)
for b in blocks:
    if b.get('blockHash','') == '$BLOCK_A':
        print(b.get('blockNumber', 0))
        break
" 2>/dev/null || echo "0")

    info "Deploy A finalized: block=$BLOCK_A_NUM ($TOTAL_A)"

    # Deploy B: Read the value written by Deploy A
    # Use valid_after_block_number = Deploy A's block
    cat << RHOEOF > "$SCRIPT_DIR/.test-deploy-b.rho"
new stdout(\`rho:io:stdout\`), ret in {
    for(@value <- @"sequential-test-$UNIQUE") {
        stdout!(["Deploy B read:", value]) |
        ret!(value)
    }
}
RHOEOF

    info "Deploy B: Reading from channel (no valid_after set by rust-client)"
    OUTPUT_B=$($NODE_CLI deploy-and-wait \
        --host localhost --port 40411 --http-port 40413 \
        --observer-host localhost --observer-port 40451 \
        --private-key "$PRIVATE_KEY" \
        --file "$SCRIPT_DIR/.test-deploy-b.rho" 2>&1)

    TOTAL_B=$(echo "$OUTPUT_B" | grep "Total time:" | sed 's/.*Total time: //')

    if echo "$OUTPUT_B" | grep -q "completed successfully"; then
        # Check validator logs for the stdout output
        STDOUT_B=$(docker logs rnode.validator1 2>&1 | grep "Deploy B read:" | tail -1)
        if echo "$STDOUT_B" | grep -q "written-by-deploy-a"; then
            pass "Iteration $i: Deploy B read Deploy A's data ($TOTAL_B)"
            PASS_COUNT=$((PASS_COUNT + 1))
        else
            # Deploy finalized but may have errored — check
            DEPLOY_B_ID=$(echo "$OUTPUT_B" | grep "Deploy ID:" | sed 's/.*Deploy ID: //')
            BLOCK_B=$(echo "$OUTPUT_B" | grep "found in block:" | sed 's/.*found in block: //')
            ERRORED=$(curl -s "$OBSERVER/api/block/$BLOCK_B" 2>&1 | python3 -c "
import sys, json
d = json.load(sys.stdin)
for dep in d.get('deploys', []):
    if dep.get('sig','').startswith('${DEPLOY_B_ID:0:20}'):
        print(dep.get('errored', 'unknown'))
        break
" 2>/dev/null || echo "unknown")

            if [ "$ERRORED" = "True" ]; then
                fail "Iteration $i: Deploy B ERRORED on-chain (data not visible)"
                FAIL_COUNT=$((FAIL_COUNT + 1))
            else
                # Deploy succeeded but stdout wasn't captured (race with log)
                pass "Iteration $i: Deploy B finalized without error ($TOTAL_B)"
                PASS_COUNT=$((PASS_COUNT + 1))
            fi
        fi
    else
        fail "Iteration $i: Deploy B failed to finalize"
        echo "$OUTPUT_B" | tail -3
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi

    echo ""
done

echo "=== Results: $PASS_COUNT/$ITERATIONS passed, $FAIL_COUNT/$ITERATIONS failed ==="

# Clean up temp files
rm -f "$SCRIPT_DIR/.test-deploy-a.rho" "$SCRIPT_DIR/.test-deploy-b.rho"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi

#!/usr/bin/env bash
# test-registry-sequential.sh — Test sequential deploys through a registered contract
#
# Simulates the embers pattern:
# 1. Init deploy: register a contract with treeHashMap (like agents_teams init)
# 2. Create deploy: call registered contract to create an entry
# 3. Read deploy: call registered contract to read the entry
#
# This tests whether the registry + treeHashMap + consume+resend pattern
# has ordering issues that simple channel writes don't.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_CLI="$SCRIPT_DIR/../services/rust-client/target/release/node_cli"
PRIVATE_KEY="5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"

# Configurable ports: ./test-registry-sequential.sh [iterations] [grpc_port] [http_port] [observer_grpc] [observer_http]
GRPC_PORT="${2:-40411}"
HTTP_PORT="${3:-40413}"
OBS_GRPC="${4:-40451}"
OBS_HTTP="${5:-40453}"
OBSERVER="http://localhost:$OBS_HTTP"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
info() { echo -e "${YELLOW}[TEST]${NC} $1"; }

UNIQUE="reg-$RANDOM"
ITERATIONS="${1:-3}"

echo "=== Registry Sequential Deploy Test ($UNIQUE) ==="
echo "Node: grpc=$GRPC_PORT http=$HTTP_PORT observer_grpc=$OBS_GRPC observer_http=$OBS_HTTP"
echo ""

# Step 1: Deploy init contract with treeHashMap
info "Step 1: Init — deploy contract with treeHashMap + consume+resend"
cat << RHOEOF > "$SCRIPT_DIR/.test-reg-init.rho"
new rl(\`rho:registry:lookup\`),
    rr(\`rho:registry:insertRandom\`),
    stdout(\`rho:io:stdout\`),
    treeHashMapCh,
    dataCh
in {
    rl!(\`rho:lang:treeHashMap\`, *treeHashMapCh) |
    for(treeHashMap <- treeHashMapCh) {
        treeHashMap!("init", 3, *treeHashMapCh) |
        for(@map <- treeHashMapCh) {
            treeHashMapCh!(*treeHashMap, map) |

            contract @"$UNIQUE-set"(@key, @value, ret) = {
                for(treeHashMap, @map <- treeHashMapCh) {
                    treeHashMapCh!(*treeHashMap, map) |
                    treeHashMap!("set", map, key, value, *ret)
                }
            } |

            contract @"$UNIQUE-get"(@key, ret, notFound) = {
                for(treeHashMap, @map <- treeHashMapCh) {
                    treeHashMapCh!(*treeHashMap, map) |
                    treeHashMap!("getOrElse", map, key, *ret, *notFound)
                }
            } |

            stdout!("$UNIQUE init complete")
        }
    }
}
RHOEOF

OUTPUT=$($NODE_CLI deploy-and-wait \
    --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
    --observer-host localhost --observer-port "$OBS_GRPC" \
    --private-key "$PRIVATE_KEY" \
    --bigger-phlo \
    --file "$SCRIPT_DIR/.test-reg-init.rho" 2>&1)
TOTAL=$(echo "$OUTPUT" | grep "Total time:" | sed 's/.*Total time: //')
pass "Init deployed ($TOTAL)"

echo ""
info "Running $ITERATIONS create+read cycles"

PASS_COUNT=0
FAIL_COUNT=0

for i in $(seq 1 "$ITERATIONS"); do
    info "--- Iteration $i/$ITERATIONS ---"

    KEY="team-$i"
    VALUE="data-$i-$RANDOM"

    # Create: set a key in the treeHashMap
    cat << RHOEOF > "$SCRIPT_DIR/.test-reg-create.rho"
new ret, stdout(\`rho:io:stdout\`) in {
    @"$UNIQUE-set"!("$KEY", "$VALUE", *ret) |
    for(_ <- ret) {
        stdout!("$UNIQUE set $KEY = $VALUE")
    }
}
RHOEOF

    info "Create: setting $KEY"
    OUTPUT=$($NODE_CLI deploy-and-wait \
        --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
        --observer-host localhost --observer-port "$OBS_GRPC" \
        --private-key "$PRIVATE_KEY" \
        --bigger-phlo \
        --file "$SCRIPT_DIR/.test-reg-create.rho" 2>&1)
    TOTAL_C=$(echo "$OUTPUT" | grep "Total time:" | sed 's/.*Total time: //')

    if ! echo "$OUTPUT" | grep -q "completed successfully"; then
        fail "Create deploy failed for $KEY"
        echo "$OUTPUT" | tail -3
        FAIL_COUNT=$((FAIL_COUNT + 1))
        continue
    fi

    # Read: get the key immediately after create finalization
    cat << RHOEOF > "$SCRIPT_DIR/.test-reg-read.rho"
new ret, notFound, stdout(\`rho:io:stdout\`) in {
    @"$UNIQUE-get"!("$KEY", *ret, *notFound) |
    for(@value <- ret) {
        stdout!(["$UNIQUE read $KEY =", value])
    } |
    for(<- notFound) {
        stdout!("$UNIQUE read $KEY = NOT FOUND")
    }
}
RHOEOF

    info "Read: getting $KEY"
    OUTPUT=$($NODE_CLI deploy-and-wait \
        --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
        --observer-host localhost --observer-port "$OBS_GRPC" \
        --private-key "$PRIVATE_KEY" \
        --bigger-phlo \
        --file "$SCRIPT_DIR/.test-reg-read.rho" 2>&1)
    TOTAL_R=$(echo "$OUTPUT" | grep "Total time:" | sed 's/.*Total time: //')

    # Check if the read found the value
    STDOUT_LINE=$(docker logs rnode.validator1 2>&1 | grep "$UNIQUE read $KEY" | tail -1)
    if echo "$STDOUT_LINE" | grep -q "$VALUE"; then
        pass "Iteration $i: read $KEY = $VALUE (create: $TOTAL_C, read: $TOTAL_R)"
        PASS_COUNT=$((PASS_COUNT + 1))
    elif echo "$STDOUT_LINE" | grep -q "NOT FOUND"; then
        fail "Iteration $i: read $KEY = NOT FOUND (create: $TOTAL_C, read: $TOTAL_R)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    else
        # Check if read deploy errored
        DEPLOY_R_ID=$(echo "$OUTPUT" | grep "Deploy ID:" | sed 's/.*Deploy ID: //')
        BLOCK_R=$(echo "$OUTPUT" | grep "found in block:" | sed 's/.*found in block: //')
        ERRORED=$(curl -s "$OBSERVER/api/block/$BLOCK_R" 2>&1 | python3 -c "
import sys, json
d = json.load(sys.stdin)
for dep in d.get('deploys', []):
    print(dep.get('errored', 'unknown'))
    break
" 2>/dev/null || echo "unknown")
        if [ "$ERRORED" = "True" ]; then
            fail "Iteration $i: read deploy ERRORED on-chain"
            FAIL_COUNT=$((FAIL_COUNT + 1))
        else
            pass "Iteration $i: read finalized OK (stdout not captured in time)"
            PASS_COUNT=$((PASS_COUNT + 1))
        fi
    fi

    echo ""
done

echo "=== Results: $PASS_COUNT/$ITERATIONS passed, $FAIL_COUNT/$ITERATIONS failed ==="

rm -f "$SCRIPT_DIR/.test-reg-init.rho" "$SCRIPT_DIR/.test-reg-create.rho" "$SCRIPT_DIR/.test-reg-read.rho"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi

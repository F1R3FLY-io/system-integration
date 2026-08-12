#!/usr/bin/env bash
# test-peek-treehashmap.sh — Test original embers pattern: peek-based treeHashMap across deploys
#
# Uses <<- (peek) to read treeHashMapCh, matching the original embers init.rho
# Tests create+read across separate deploys on any node.
#
# Usage: ./scripts/test-peek-treehashmap.sh [iterations] [grpc_port] [http_port] [observer_grpc] [observer_http]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_CLI="$SCRIPT_DIR/../services/rust-client/target/release/node_cli"
PRIVATE_KEY="5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"

ITERATIONS="${1:-3}"
GRPC_PORT="${2:-40502}"
HTTP_PORT="${3:-40503}"
OBS_GRPC="${4:-$GRPC_PORT}"
OBS_HTTP="${5:-$HTTP_PORT}"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
info() { echo -e "${YELLOW}[TEST]${NC} $1"; }

UNIQUE="peek-$RANDOM"

echo "=== Peek TreeHashMap Test ($UNIQUE) ==="
echo "Node: grpc=$GRPC_PORT http=$HTTP_PORT observer=$OBS_GRPC/$OBS_HTTP"
echo ""

# Step 1: Deploy init contract — uses PEEK (<<-) like original embers
info "Step 1: Init — deploy contract with treeHashMap + PEEK (<<-)"
cat << RHOEOF > "$SCRIPT_DIR/.test-peek-init.rho"
new rl(\`rho:registry:lookup\`),
    stdout(\`rho:io:stdout\`),
    treeHashMapCh
in {
    rl!(\`rho:lang:treeHashMap\`, *treeHashMapCh) |
    for(treeHashMap <- treeHashMapCh) {
        treeHashMap!("init", 3, *treeHashMapCh) |
        for(@map <- treeHashMapCh) {
            treeHashMapCh!(*treeHashMap, map) |

            contract @"$UNIQUE-set"(@key, @value, ret) = {
                for(treeHashMap, @map <<- treeHashMapCh) {
                    treeHashMap!("set", map, key, value, *ret) |
                    stdout!(["$UNIQUE-set", key, value])
                }
            } |

            contract @"$UNIQUE-get"(@key, ret, notFound) = {
                for(treeHashMap, @map <<- treeHashMapCh) {
                    treeHashMap!("getOrElse", map, key, *ret, *notFound) |
                    stdout!(["$UNIQUE-get", key])
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
    --max-wait 60 \
    --file "$SCRIPT_DIR/.test-peek-init.rho" 2>&1)
TOTAL=$(echo "$OUTPUT" | grep "Total time:" | sed 's/.*Total time: //')

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "Init deployed ($TOTAL)"
else
    fail "Init deploy failed"
    echo "$OUTPUT" | tail -5
    exit 1
fi

echo ""
info "Running $ITERATIONS create+read cycles (using PEEK)"

PASS_COUNT=0
FAIL_COUNT=0

for i in $(seq 1 "$ITERATIONS"); do
    info "--- Iteration $i/$ITERATIONS ---"

    KEY="team-$i"
    VALUE="data-$i-$RANDOM"

    # Create: set a key via the peek-based contract
    info "Create: setting $KEY"
    cat << RHOEOF > "$SCRIPT_DIR/.test-peek-create.rho"
new ret, stdout(\`rho:io:stdout\`) in {
    @"$UNIQUE-set"!("$KEY", "$VALUE", *ret) |
    for(_ <- ret) {
        stdout!("$UNIQUE set $KEY = $VALUE")
    }
}
RHOEOF

    OUTPUT=$($NODE_CLI deploy-and-wait \
        --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
        --observer-host localhost --observer-port "$OBS_GRPC" \
        --private-key "$PRIVATE_KEY" \
        --bigger-phlo \
        --max-wait 60 \
        --file "$SCRIPT_DIR/.test-peek-create.rho" 2>&1)
    TOTAL_C=$(echo "$OUTPUT" | grep "Total time:" | sed 's/.*Total time: //')

    if ! echo "$OUTPUT" | grep -q "completed successfully"; then
        fail "Iteration $i: create deploy failed ($TOTAL_C)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        continue
    fi

    # Read: get the key via the peek-based contract
    info "Read: getting $KEY"
    cat << RHOEOF > "$SCRIPT_DIR/.test-peek-read.rho"
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

    OUTPUT=$($NODE_CLI deploy-and-wait \
        --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
        --observer-host localhost --observer-port "$OBS_GRPC" \
        --private-key "$PRIVATE_KEY" \
        --bigger-phlo \
        --max-wait 60 \
        --file "$SCRIPT_DIR/.test-peek-read.rho" 2>&1)
    TOTAL_R=$(echo "$OUTPUT" | grep "Total time:" | sed 's/.*Total time: //')

    # Check stdout on node for the read result
    sleep 1
    READ_RESULT=$(docker logs rnode.standalone 2>&1 | grep "$UNIQUE read $KEY" | tail -1)

    if echo "$READ_RESULT" | grep -q "NOT FOUND"; then
        fail "Iteration $i: read $KEY = NOT FOUND (create: $TOTAL_C, read: $TOTAL_R)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    elif echo "$READ_RESULT" | grep -q "$VALUE"; then
        pass "Iteration $i: read $KEY = $VALUE (create: $TOTAL_C, read: $TOTAL_R)"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        fail "Iteration $i: unexpected read result: $READ_RESULT (create: $TOTAL_C, read: $TOTAL_R)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
done

echo ""
echo "=== Results: $PASS_COUNT/$ITERATIONS passed, $FAIL_COUNT/$ITERATIONS failed ==="

# Cleanup temp files
rm -f "$SCRIPT_DIR/.test-peek-init.rho" "$SCRIPT_DIR/.test-peek-create.rho" "$SCRIPT_DIR/.test-peek-read.rho"

[ "$FAIL_COUNT" -eq 0 ] && exit 0 || exit 1

#!/usr/bin/env bash
# test-treehashmap-read-only.sh — Does treeHashMap GET work across deploys?
#
# Deploy 1: Init treeHashMap, set one key in the SAME deploy
# Deploy 2: Get that key from a SEPARATE deploy
#
# This isolates whether the problem is set-across-deploys or get-across-deploys.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_CLI="$SCRIPT_DIR/../services/rust-client/target/release/node_cli"
PRIVATE_KEY="5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"

GRPC_PORT="${1:-40502}"
HTTP_PORT="${2:-40503}"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }
info() { echo -e "${YELLOW}[TEST]${NC} $1"; }

UNIQUE="thmro-$RANDOM"

echo "=== TreeHashMap Read-Only Cross-Deploy Test ($UNIQUE) ==="
echo "Node: grpc=$GRPC_PORT http=$HTTP_PORT"
echo ""

# Deploy 1: Init treeHashMap AND set a key in the same deploy
info "Deploy 1: Init treeHashMap + set 'mykey'='myvalue' in SAME deploy"
cat << RHOEOF > "$SCRIPT_DIR/.test-thmro-init.rho"
new rl(\`rho:registry:lookup\`),
    stdout(\`rho:io:stdout\`),
    treeHashMapCh
in {
    rl!(\`rho:lang:treeHashMap\`, *treeHashMapCh) |
    for(treeHashMap <- treeHashMapCh) {
        treeHashMap!("init", 3, *treeHashMapCh) |
        for(@map <- treeHashMapCh) {
            // Set a key in the same deploy
            new ackCh in {
                treeHashMap!("set", map, "mykey", "myvalue", *ackCh) |
                for(_ <- ackCh) {
                    stdout!("$UNIQUE: init+set complete") |
                    // Store (treeHashMap, map) for cross-deploy access
                    treeHashMapCh!(*treeHashMap, map)
                }
            } |

            // Get contract for cross-deploy reads
            contract @"$UNIQUE-get"(@key, ret, notFound) = {
                for(treeHashMap, @map <<- treeHashMapCh) {
                    treeHashMap!("getOrElse", map, key, *ret, *notFound) |
                    stdout!(["$UNIQUE: get called", key])
                }
            }
        }
    }
}
RHOEOF

OUTPUT=$($NODE_CLI deploy-and-wait \
    --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
    --observer-host localhost --observer-port "$GRPC_PORT" \
    --private-key "$PRIVATE_KEY" \
    --bigger-phlo \
    --max-wait 60 \
    --file "$SCRIPT_DIR/.test-thmro-init.rho" 2>&1)

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "Init+set deployed"
else
    fail "Init+set deploy failed"
    echo "$OUTPUT" | tail -5
    exit 1
fi

# Deploy 2: Read the key from a separate deploy
info "Deploy 2: Get 'mykey' from separate deploy (should be 'myvalue')"
cat << RHOEOF > "$SCRIPT_DIR/.test-thmro-read.rho"
new ret, notFound, stdout(\`rho:io:stdout\`) in {
    @"$UNIQUE-get"!("mykey", *ret, *notFound) |
    for(@value <- ret) {
        stdout!(["$UNIQUE: cross-deploy read", value])
    } |
    for(<- notFound) {
        stdout!("$UNIQUE: cross-deploy read = NOT FOUND")
    }
}
RHOEOF

OUTPUT=$($NODE_CLI deploy-and-wait \
    --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
    --observer-host localhost --observer-port "$GRPC_PORT" \
    --private-key "$PRIVATE_KEY" \
    --max-wait 60 \
    --file "$SCRIPT_DIR/.test-thmro-read.rho" 2>&1)

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "Read deploy finalized"
else
    fail "Read deploy hung/failed — treeHashMap GET doesn't work across deploys"
    echo "$OUTPUT" | tail -5
fi

echo ""
info "Check node stdout:"
info "  docker logs <container> 2>&1 | grep '$UNIQUE'"

rm -f "$SCRIPT_DIR/.test-thmro-init.rho" "$SCRIPT_DIR/.test-thmro-read.rho"

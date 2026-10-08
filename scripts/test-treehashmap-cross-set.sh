#!/usr/bin/env bash
# test-treehashmap-cross-set.sh — Does treeHashMap SET work from a separate deploy?
#
# Deploy 1: Init treeHashMap + set key1 in SAME deploy
# Deploy 2: SET key2 from SEPARATE deploy
# Deploy 3: GET both keys
#
# If Deploy 2 hangs, the SET across deploys is broken.

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

UNIQUE="xset-$RANDOM"

echo "=== TreeHashMap Cross-Deploy SET Test ($UNIQUE) ==="
echo "Node: grpc=$GRPC_PORT http=$HTTP_PORT"
echo ""

# Deploy 1: Init + set key1 in same deploy, expose set/get contracts
info "Deploy 1: Init treeHashMap + set key1='value1' + expose set/get"
cat << RHOEOF > "$SCRIPT_DIR/.test-xset-init.rho"
new rl(\`rho:registry:lookup\`),
    stdout(\`rho:io:stdout\`),
    treeHashMapCh
in {
    rl!(\`rho:lang:treeHashMap\`, *treeHashMapCh) |
    for(treeHashMap <- treeHashMapCh) {
        treeHashMap!("init", 3, *treeHashMapCh) |
        for(@map <- treeHashMapCh) {
            new ackCh in {
                treeHashMap!("set", map, "key1", "value1", *ackCh) |
                for(_ <- ackCh) {
                    stdout!("$UNIQUE: init+set key1=value1 done") |
                    treeHashMapCh!(*treeHashMap, map)
                }
            } |

            contract @"$UNIQUE-set"(@key, @value, ret) = {
                stdout!(["$UNIQUE: set contract entered", key, value]) |
                for(treeHashMap, @map <<- treeHashMapCh) {
                    stdout!(["$UNIQUE: set got treeHashMapCh, calling set", key]) |
                    treeHashMap!("set", map, key, value, *ret) |
                    for(_ <<- ret) {
                        stdout!(["$UNIQUE: set completed", key, value])
                    }
                }
            } |

            contract @"$UNIQUE-get"(@key, ret, notFound) = {
                for(treeHashMap, @map <<- treeHashMapCh) {
                    treeHashMap!("getOrElse", map, key, *ret, *notFound)
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
    --file "$SCRIPT_DIR/.test-xset-init.rho" 2>&1)

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "Deploy 1: Init+set key1 done"
else
    fail "Deploy 1 failed"
    echo "$OUTPUT" | tail -5
    exit 1
fi

# Deploy 2: SET key2 from separate deploy
info "Deploy 2: Set key2='value2' from SEPARATE deploy"
cat << RHOEOF > "$SCRIPT_DIR/.test-xset-set.rho"
new ret, stdout(\`rho:io:stdout\`) in {
    stdout!("$UNIQUE: deploy2 starting set") |
    @"$UNIQUE-set"!("key2", "value2", *ret) |
    for(_ <- ret) {
        stdout!("$UNIQUE: deploy2 set key2=value2 COMPLETE")
    }
}
RHOEOF

OUTPUT=$($NODE_CLI deploy-and-wait \
    --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
    --observer-host localhost --observer-port "$GRPC_PORT" \
    --private-key "$PRIVATE_KEY" \
    --bigger-phlo \
    --max-wait 60 \
    --file "$SCRIPT_DIR/.test-xset-set.rho" 2>&1)

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "Deploy 2: Cross-deploy SET finalized"
else
    fail "Deploy 2: Cross-deploy SET hung/failed"
    echo "$OUTPUT" | tail -5
    echo ""
    info "Checking node stdout for debug info:"
    CONTAINER=$(docker ps --format '{{.Names}}' | grep -E "standalone|rnode.standalone" | head -1)
    docker logs "$CONTAINER" 2>&1 | grep "$UNIQUE" | grep -v "Received\|contract\|stdout!" | tail -10
    rm -f "$SCRIPT_DIR/.test-xset-init.rho" "$SCRIPT_DIR/.test-xset-set.rho" "$SCRIPT_DIR/.test-xset-read.rho"
    exit 1
fi

# Deploy 3: GET both keys
info "Deploy 3: Get key1 and key2"
cat << RHOEOF > "$SCRIPT_DIR/.test-xset-read.rho"
new ret1, ret2, notFound1, notFound2, stdout(\`rho:io:stdout\`) in {
    @"$UNIQUE-get"!("key1", *ret1, *notFound1) |
    @"$UNIQUE-get"!("key2", *ret2, *notFound2) |
    for(@v1 <- ret1) { stdout!(["$UNIQUE: key1 =", v1]) } |
    for(<- notFound1) { stdout!("$UNIQUE: key1 = NOT FOUND") } |
    for(@v2 <- ret2) { stdout!(["$UNIQUE: key2 =", v2]) } |
    for(<- notFound2) { stdout!("$UNIQUE: key2 = NOT FOUND") }
}
RHOEOF

OUTPUT=$($NODE_CLI deploy-and-wait \
    --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
    --observer-host localhost --observer-port "$GRPC_PORT" \
    --private-key "$PRIVATE_KEY" \
    --max-wait 60 \
    --file "$SCRIPT_DIR/.test-xset-read.rho" 2>&1)

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "Deploy 3: Read finalized"
else
    fail "Deploy 3: Read hung/failed"
fi

echo ""
info "Node stdout results:"
CONTAINER=$(docker ps --format '{{.Names}}' | grep -E "standalone|rnode.standalone" | head -1)
docker logs "$CONTAINER" 2>&1 | grep "$UNIQUE" | grep -v "Received\|contract\|stdout!" | tail -15

rm -f "$SCRIPT_DIR/.test-xset-init.rho" "$SCRIPT_DIR/.test-xset-set.rho" "$SCRIPT_DIR/.test-xset-read.rho"

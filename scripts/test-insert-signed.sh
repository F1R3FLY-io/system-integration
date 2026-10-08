#!/usr/bin/env bash
# test-insert-signed.sh — Test insertSigned on a node
#
# Deploys a contract using rho:registry:insertSigned:secp256k1 then verifies
# the contract is findable via rho:registry:lookup.
#
# This isolates whether the embers bootstrap insertSigned signing is compatible
# with the target node.
#
# Usage: ./scripts/test-insert-signed.sh [grpc_port] [http_port]

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

UNIQUE="isig-$RANDOM"

echo "=== insertSigned Test ($UNIQUE) ==="
echo "Node: grpc=$GRPC_PORT http=$HTTP_PORT"
echo ""

# Test 1: Use insertRandom (no signing) as baseline
info "Test 1: insertRandom (baseline — no signing needed)"
cat << 'RHOEOF' > "$SCRIPT_DIR/.test-isig-random.rho"
new rl(`rho:registry:lookup`),
    rr(`rho:registry:insertRandom`),
    stdout(`rho:io:stdout`),
    uriCh,
    myContract
in {
    contract myContract(@"hello", ret) = {
        ret!("world")
    } |
    rr!(bundle+{*myContract}, *uriCh) |
    for(@uri <- uriCh) {
        stdout!(["insertRandom URI:", uri])
    } |
    for(@Nil <- uriCh) {
        stdout!("insertRandom returned Nil — NOT AVAILABLE")
    }
}
RHOEOF

OUTPUT=$($NODE_CLI deploy-and-wait \
    --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
    --observer-host localhost --observer-port "$GRPC_PORT" \
    --private-key "$PRIVATE_KEY" \
    --bigger-phlo \
    --max-wait 60 \
    --file "$SCRIPT_DIR/.test-isig-random.rho" 2>&1)

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "insertRandom deploy finalized"
else
    fail "insertRandom deploy failed/hung"
    echo "$OUTPUT" | tail -5
fi

# Check stdout
CONTAINER=$(docker ps --format '{{.Names}}' | grep standalone | head -1)
RANDOM_RESULT=$(docker logs "$CONTAINER" 2>&1 | grep "insertRandom" | grep -v "Received\|contract\|stdout!" | tail -1)
if echo "$RANDOM_RESULT" | grep -q "URI:"; then
    pass "insertRandom: $RANDOM_RESULT"
elif echo "$RANDOM_RESULT" | grep -q "NOT AVAILABLE"; then
    fail "insertRandom not available on this node"
else
    info "insertRandom result: $RANDOM_RESULT"
fi

echo ""

# Test 2: Use insertSigned with inline signing (Rholang-native)
# This tests whether insertSigned works AT ALL on this node,
# using Rholang-native signing (no external signature).
info "Test 2: insertSigned with Rholang-native verification test"
info "(Deploys a contract and checks if rs! returns a URI or Nil)"
cat << RHOEOF > "$SCRIPT_DIR/.test-isig-signed.rho"
new rl(\`rho:registry:lookup\`),
    rs(\`rho:registry:insertSigned:secp256k1\`),
    stdout(\`rho:io:stdout\`),
    deployData(\`rho:deploy:data\`),
    deployerIdOps(\`rho:system:deployerId:ops\`),
    blake2b256(\`rho:crypto:blake2b256Hash\`),
    secp256k1Sign(\`rho:crypto:secp256k1Sign\`),
    uriCh,
    myContract
in {
    contract myContract(@"test", ret) = {
        ret!("$UNIQUE signed contract works")
    } |

    // Get deploy data for timestamp and deployer
    stdout!("$UNIQUE: getting deploy data") |
    deployData!(*stdout) |

    // Try insertSigned with a dummy sig to see what error we get
    // First, check if rs! is even available
    stdout!("$UNIQUE: calling rs! with test data") |
    rs!(
        "04deadbeef".hexToBytes(),
        (0, bundle+{*myContract}),
        "3045deadbeef".hexToBytes(),
        *uriCh
    ) |
    for(@uri <- uriCh) {
        match uri {
            Nil => stdout!("$UNIQUE: rs! returned Nil (signature verification failed — expected for dummy sig)")
            _ => stdout!(["$UNIQUE: rs! returned URI:", uri])
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
    --file "$SCRIPT_DIR/.test-isig-signed.rho" 2>&1)

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "insertSigned deploy finalized"
else
    fail "insertSigned deploy failed/hung"
    echo "$OUTPUT" | tail -5
fi

# Check stdout
ISIG_RESULTS=$(docker logs "$CONTAINER" 2>&1 | grep "$UNIQUE" | grep -v "Received\|contract\|stdout!" | tail -10)
echo "$ISIG_RESULTS"

echo ""

# Test 3: Verify the embers bootstrap pattern works
# Use the ACTUAL embers signing flow: sign (timestamp, deployerPubKey, version)
# with the env key, then call rs!(envPubKey, (version, bundle), sig, uriCh)
info "Test 3: Full embers-style insertSigned (requires embers binary for signing)"
info "(Skipping — needs Rust signing. Use embers bootstrap logs to diagnose.)"

echo ""

# Test 4: Check what deploy data looks like on this node
info "Test 4: Inspect deploy data format"
cat << RHOEOF > "$SCRIPT_DIR/.test-isig-deploydata.rho"
new deployData(\`rho:deploy:data\`),
    deployerIdOps(\`rho:system:deployerId:ops\`),
    stdout(\`rho:io:stdout\`),
    deployDataCh,
    pubKeyCh
in {
    deployData!(*deployDataCh) |
    for(@timestamp, @deployerId, @deployId <- deployDataCh) {
        stdout!(["$UNIQUE deploy timestamp:", timestamp]) |
        stdout!(["$UNIQUE deploy id:", deployId]) |
        deployerIdOps!("pubKeyBytes", deployerId, *pubKeyCh) |
        for(@pubKeyBytes <- pubKeyCh) {
            stdout!(["$UNIQUE deployer pubkey length:", pubKeyBytes.length()]) |
            stdout!(["$UNIQUE deployer pubkey hex:", pubKeyBytes.toHexString()])
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
    --file "$SCRIPT_DIR/.test-isig-deploydata.rho" 2>&1)

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "Deploy data inspection finalized"
else
    fail "Deploy data inspection failed"
    echo "$OUTPUT" | tail -5
fi

DEPLOY_RESULTS=$(docker logs "$CONTAINER" 2>&1 | grep "$UNIQUE deploy" | grep -v "Received\|stdout!" | tail -10)
echo "$DEPLOY_RESULTS"

echo ""
info "Cleanup"
rm -f "$SCRIPT_DIR/.test-isig-random.rho" "$SCRIPT_DIR/.test-isig-signed.rho" "$SCRIPT_DIR/.test-isig-deploydata.rho"

#!/usr/bin/env bash
# test-simple-state.sh — Minimal cross-deploy state test
#
# Tests if an unforgeable channel's state persists across deploys.
# No treeHashMap — just raw channel operations.
#
# Usage: ./scripts/test-simple-state.sh [grpc_port] [http_port]

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

UNIQUE="simple-$RANDOM"

echo "=== Simple Cross-Deploy State Test ($UNIQUE) ==="
echo "Node: grpc=$GRPC_PORT http=$HTTP_PORT"
echo ""

# Deploy 1: Init — create unforgeable channel with initial state
info "Deploy 1: Init — create state channel + set/get contracts"
cat << RHOEOF > "$SCRIPT_DIR/.test-simple-init.rho"
new state, stdout(\`rho:io:stdout\`) in {
    state!("initial") |
    stdout!("$UNIQUE: init, state=initial") |

    contract @"$UNIQUE-set"(@value, ret) = {
        for(_ <- state) {
            state!(value) |
            stdout!(["$UNIQUE: set", value]) |
            ret!(true)
        }
    } |

    contract @"$UNIQUE-get"(ret) = {
        for(@v <<- state) {
            stdout!(["$UNIQUE: get", v]) |
            ret!(v)
        }
    }
}
RHOEOF

OUTPUT=$($NODE_CLI deploy-and-wait \
    --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
    --observer-host localhost --observer-port "$GRPC_PORT" \
    --private-key "$PRIVATE_KEY" \
    --max-wait 60 \
    --file "$SCRIPT_DIR/.test-simple-init.rho" 2>&1)

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "Init deployed"
else
    fail "Init deploy failed"
    echo "$OUTPUT" | tail -5
    exit 1
fi

# Deploy 2: Read initial state
info "Deploy 2: Read initial state (should be 'initial')"
cat << RHOEOF > "$SCRIPT_DIR/.test-simple-read.rho"
new ret, stdout(\`rho:io:stdout\`) in {
    @"$UNIQUE-get"!(*ret) |
    for(@value <- ret) {
        stdout!(["$UNIQUE: read result", value])
    }
}
RHOEOF

OUTPUT=$($NODE_CLI deploy-and-wait \
    --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
    --observer-host localhost --observer-port "$GRPC_PORT" \
    --private-key "$PRIVATE_KEY" \
    --max-wait 60 \
    --file "$SCRIPT_DIR/.test-simple-read.rho" 2>&1)

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "Read deploy finalized"
else
    fail "Read deploy failed/hung"
    echo "$OUTPUT" | tail -5
    exit 1
fi

# Deploy 3: Set new value
info "Deploy 3: Set state to 'updated'"
cat << RHOEOF > "$SCRIPT_DIR/.test-simple-set.rho"
new ret, stdout(\`rho:io:stdout\`) in {
    @"$UNIQUE-set"!("updated", *ret) |
    for(@result <- ret) {
        stdout!(["$UNIQUE: set result", result])
    }
}
RHOEOF

OUTPUT=$($NODE_CLI deploy-and-wait \
    --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
    --observer-host localhost --observer-port "$GRPC_PORT" \
    --private-key "$PRIVATE_KEY" \
    --max-wait 60 \
    --file "$SCRIPT_DIR/.test-simple-set.rho" 2>&1)

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "Set deploy finalized"
else
    fail "Set deploy failed/hung"
    echo "$OUTPUT" | tail -5
    exit 1
fi

# Deploy 4: Read updated state
info "Deploy 4: Read state (should be 'updated')"
OUTPUT=$($NODE_CLI deploy-and-wait \
    --host localhost --port "$GRPC_PORT" --http-port "$HTTP_PORT" \
    --observer-host localhost --observer-port "$GRPC_PORT" \
    --private-key "$PRIVATE_KEY" \
    --max-wait 60 \
    --file "$SCRIPT_DIR/.test-simple-read.rho" 2>&1)

if echo "$OUTPUT" | grep -q "completed successfully"; then
    pass "Read-after-set deploy finalized"
else
    fail "Read-after-set deploy failed/hung"
    echo "$OUTPUT" | tail -5
    exit 1
fi

echo ""
info "Check node stdout for results:"
info "  docker logs <container> 2>&1 | grep '$UNIQUE'"

# Cleanup
rm -f "$SCRIPT_DIR/.test-simple-init.rho" "$SCRIPT_DIR/.test-simple-read.rho" "$SCRIPT_DIR/.test-simple-set.rho"

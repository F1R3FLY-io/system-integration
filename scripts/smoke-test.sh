#!/usr/bin/env bash
# =================================================================
# System Integration Smoke Test
# =================================================================
# Validates that all compose topologies start, reach Running state,
# and that the integration test harness can bring up its own stacks.
#
# Uses the exact commands from README.md and integration-tests/README.md
# so that any breakage a real user would hit is caught here.
#
# Usage:
#   ./scripts/smoke-test.sh                    # Run all phases
#   ./scripts/smoke-test.sh --validate-only    # Compose file validation only
#   ./scripts/smoke-test.sh --rust-only        # Rust topologies only
#   ./scripts/smoke-test.sh --scala-only       # Scala topologies only
#   ./scripts/smoke-test.sh --skip-integration # Skip integration test phase
#   ./scripts/smoke-test.sh --continue         # Don't stop on first failure
#
# Phases:
#   1. Validate: docker compose config on all compose files
#   2. Topology: Start each topology, wait for Running, tear down
#   3. Integration: Run one representative test per environment
#
# Requirements:
#   - Docker & Docker Compose
#   - Poetry (with dependencies installed: poetry install --with integration)
#   - Node images available locally or pullable

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# --- Configuration ---
WAIT_TIMEOUT=300
PASS=0
FAIL=0
SKIP=0
FAILED_TESTS=()

# --- Flags ---
VALIDATE_ONLY=false
RUST_ONLY=false
SCALA_ONLY=false
SKIP_INTEGRATION=false
STOP_ON_FAIL=true

for arg in "$@"; do
    case "$arg" in
        --validate-only)    VALIDATE_ONLY=true ;;
        --rust-only)        RUST_ONLY=true ;;
        --scala-only)       SCALA_ONLY=true ;;
        --skip-integration) SKIP_INTEGRATION=true ;;
        --continue)         STOP_ON_FAIL=false ;;
        --help|-h)
            sed -n '2,/^$/p' "$0" | sed 's/^# //' | sed 's/^#//'
            exit 0
            ;;
        *)
            echo "Unknown flag: $arg"
            exit 1
            ;;
    esac
done

# --- Helpers ---
log_header() { echo -e "\n====== $1 ======"; }
log_pass()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
log_fail()   {
    echo "  FAIL: $1"
    FAIL=$((FAIL + 1))
    FAILED_TESTS+=("$1")
    if $STOP_ON_FAIL; then
        echo ""
        echo "Stopping on first failure. Use --continue to run all tests."
        print_summary
        exit 1
    fi
}
log_skip()   { echo "  SKIP: $1"; SKIP=$((SKIP + 1)); }

print_summary() {
    echo ""
    echo "====== Summary ======"
    echo "  PASS: $PASS"
    echo "  FAIL: $FAIL"
    echo "  SKIP: $SKIP"
    if [ ${#FAILED_TESTS[@]} -gt 0 ]; then
        echo ""
        echo "  Failed:"
        for t in "${FAILED_TESTS[@]}"; do
            echo "    - $t"
        done
    fi
}

image_exists() {
    docker image inspect "$1" >/dev/null 2>&1
}

# Start a topology using the exact README commands, wait, tear down.
# Args: test_name shardctl_service
run_topology_test() {
    local test_name="$1"
    local service="$2"

    echo "  Starting $test_name ..."

    # Clean slate (README: shardctl reset -y)
    poetry run shardctl reset -y >/dev/null 2>&1 || true

    # README: poetry run shardctl up <service>
    if ! poetry run shardctl up "$service" 2>&1 | tail -5; then
        poetry run shardctl reset -y >/dev/null 2>&1 || true
        log_fail "$test_name (shardctl up $service failed)"
        return
    fi

    # README: poetry run shardctl wait
    if poetry run shardctl wait --timeout "$WAIT_TIMEOUT" 2>&1 | tail -5; then
        # README: poetry run shardctl status
        poetry run shardctl status 2>&1 | tail -10
        log_pass "$test_name"
    else
        log_fail "$test_name (nodes did not reach Running within ${WAIT_TIMEOUT}s)"
    fi

    # README: poetry run shardctl down (+ volumes for clean slate)
    poetry run shardctl reset -y >/dev/null 2>&1 || true
}

# Run a single integration test using the README command pattern.
# Args: test_name node_type test_expression
run_integration_test() {
    local test_name="$1"
    local node_type="$2"
    local test_expr="$3"

    echo "  Running $test_name ..."

    # integration-tests/README.md: poetry run shardctl test --rust/--scala <suite>
    # with -k for specific test selection
    if poetry run shardctl test "--${node_type}" \
        --pytest-args "-k" --pytest-args "$test_expr" \
        --pytest-args "--timeout=600" \
        2>&1 | tail -10; then
        log_pass "$test_name"
    else
        log_fail "$test_name"
    fi
}


# =================================================================
# Phase 1: Validate compose files
# =================================================================
# Runs: docker compose --env-file .env.node -f <file> config
# This is the same validation docker compose does before any up/down.
log_header "Phase 1: Compose File Validation"

COMPOSE_FILES=(
    compose/f1r3node-rust.yml
    compose/f1r3node-rust-standalone.yml
    compose/f1r3node-rust-observer.yml
    compose/f1r3node-rust-validator4.yml
    compose/f1r3node.yml
    compose/f1r3node-standalone.yml
    compose/f1r3node-observer.yml
    compose/f1r3node-validator4.yml
    compose/f1r3node-shard-light.yml
    compose/embers.yml
    compose/f1r3sky.yml
    compose/monitoring.yml
    integration-tests/docker-compose.rust.yml
    integration-tests/docker-compose.scala.yml
    integration-tests/docker-compose.standalone-rust.yml
    integration-tests/docker-compose.standalone-scala.yml
)

for cf in "${COMPOSE_FILES[@]}"; do
    if [ ! -f "$cf" ]; then
        log_fail "validate $cf (file not found)"
        continue
    fi
    if docker compose --env-file .env.node -f "$cf" config >/dev/null 2>&1; then
        log_pass "validate $cf"
    else
        log_fail "validate $cf (invalid config)"
    fi
done

if $VALIDATE_ONLY; then
    print_summary
    exit $FAIL
fi


# =================================================================
# Phase 2: Topology health checks
# =================================================================
# Uses the exact shardctl commands from README.md:
#   poetry run shardctl up <service>
#   poetry run shardctl wait
#   poetry run shardctl status
#   poetry run shardctl reset -y
log_header "Phase 2: Topology Health Checks"

RUST_IMAGE="f1r3flyindustries/f1r3fly-rust-node:latest"
SCALA_IMAGE="f1r3flyindustries/f1r3fly-scala-node:latest"

# README: poetry run shardctl up f1r3node-rust
if ! $SCALA_ONLY; then
    if image_exists "$RUST_IMAGE"; then
        run_topology_test "Rust shard"      f1r3node-rust
        run_topology_test "Rust standalone" f1r3node-rust-standalone
    else
        log_skip "Rust topologies (image $RUST_IMAGE not found locally)"
    fi
fi

# README: poetry run shardctl up f1r3node
if ! $RUST_ONLY; then
    if image_exists "$SCALA_IMAGE"; then
        run_topology_test "Scala shard"       f1r3node
        run_topology_test "Scala standalone"  f1r3node-standalone
        run_topology_test "Scala light shard" f1r3node-shard-light
    else
        log_skip "Scala topologies (image $SCALA_IMAGE not found locally)"
    fi
fi


# =================================================================
# Phase 3: Integration test harness
# =================================================================
# Uses the exact shardctl test command from integration-tests/README.md:
#   poetry run shardctl test --rust
#   poetry run shardctl test --scala
# With -k to select one representative test per environment.
if $SKIP_INTEGRATION; then
    log_header "Phase 3: Integration Tests (skipped)"
    log_skip "Integration tests (--skip-integration)"
else
    log_header "Phase 3: Integration Test Harness"

    # Rust integration tests
    if ! $SCALA_ONLY; then
        if image_exists "$RUST_IMAGE"; then
            run_integration_test "Rust shard: test_status"                    rust "test_status"
            run_integration_test "Rust standalone: test_heartbeat_creates_block" rust "test_heartbeat_creates_block"
            run_integration_test "Rust custom: test_synchrony_constraint"     rust "test_synchrony_constraint"
        else
            log_skip "Rust integration tests (image not found)"
        fi
    fi

    # Scala integration tests
    if ! $RUST_ONLY; then
        if image_exists "$SCALA_IMAGE"; then
            run_integration_test "Scala shard: test_status"                    scala "test_status"
            run_integration_test "Scala standalone: test_heartbeat_creates_block" scala "test_heartbeat_creates_block"
            run_integration_test "Scala custom: test_synchrony_constraint"     scala "test_synchrony_constraint"
        else
            log_skip "Scala integration tests (image not found)"
        fi
    fi
fi


# =================================================================
# Summary
# =================================================================
print_summary

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi

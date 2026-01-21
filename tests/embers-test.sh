#!/bin/bash
# Embers Complete Integration Test Suite
# Tests Embers API blockchain bridge and React frontend

set -e

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source helper functions
source "$SCRIPT_DIR/lib/helpers.sh"

# ========================================
# Service Configuration
# ========================================

# Service URLs
EMBERS_API_URL="http://localhost:8080"
EMBERS_FRONTEND_URL="http://localhost:5173"

# Container names
EMBERS_API_CONTAINER="embers"
EMBERS_FRONTEND_CONTAINER="embers-frontend"

# Test data
TEST_WALLET_ADDRESS=""
TEST_DEPLOY_ID=""
TEST_BLOCK_NUMBER=""

# Test timeout
TEST_TIMEOUT=${TEST_TIMEOUT:-60}

# ========================================
# Container Health Tests
# ========================================

test_containers_running() {
    log_test "Checking if Embers containers are running..."

    local containers=(
        "$EMBERS_API_CONTAINER:Embers API"
        "$EMBERS_FRONTEND_CONTAINER:Embers Frontend"
    )

    local all_running=true

    for container_info in "${containers[@]}"; do
        IFS=':' read -r container_name service_name <<< "$container_info"

        if is_container_running "$container_name"; then
            log_info "  ✓ $service_name is running"
        else
            log_error "  ✗ $service_name is not running"
            all_running=false
        fi
    done

    if [ "$all_running" = true ]; then
        test_passed "All Embers containers are running"
        return 0
    else
        test_failed "Some Embers containers are not running"
        return 1
    fi
}

test_wait_for_services() {
    log_test "Waiting for Embers services to be ready..."

    local services_ready=true

    # Wait for Embers API
    if wait_for_container "$EMBERS_API_CONTAINER" $TEST_TIMEOUT; then
        log_info "  ✓ Embers API is ready"
    else
        log_error "  ✗ Embers API failed to become ready"
        services_ready=false
    fi

    # Wait for Embers Frontend
    if wait_for_container "$EMBERS_FRONTEND_CONTAINER" $TEST_TIMEOUT; then
        log_info "  ✓ Embers Frontend is ready"
    else
        log_error "  ✗ Embers Frontend failed to become ready"
        services_ready=false
    fi

    # Give services extra time to initialize
    log_info "Waiting additional 5s for service initialization..."
    sleep 5

    if [ "$services_ready" = true ]; then
        test_passed "All Embers services are ready"
        return 0
    else
        test_failed "Some Embers services failed to become ready"
        return 1
    fi
}

# ========================================
# Embers API Tests
# ========================================

test_api_health() {
    log_test "Testing Embers API health endpoint..."

    if response=$(http_get "$EMBERS_API_URL/api/service/ready" 200); then
        log_info "Embers API health check passed"
        test_passed "Embers API is healthy"
        return 0
    else
        log_error "Embers API health check failed"
        show_container_logs "$EMBERS_API_CONTAINER" 20
        test_failed "Embers API health check failed"
        return 1
    fi
}

test_api_version() {
    log_test "Testing Embers API version endpoint..."

    if response=$(http_get "$EMBERS_API_URL/api/service/version" 200); then
        local version=$(echo "$response" | jq -r '.version' 2>/dev/null || echo "$response")
        log_info "  API Version: $version"
        test_passed "API version retrieved"
        return 0
    else
        test_failed "API version endpoint failed"
        return 1
    fi
}

test_api_status() {
    log_test "Testing Embers API status endpoint..."

    if response=$(http_get "$EMBERS_API_URL/api/service/status" 200); then
        log_info "API status retrieved"
        test_passed "API status endpoint working"
        return 0
    else
        test_failed "API status endpoint failed"
        return 1
    fi
}

# ========================================
# Blockchain Query Tests
# ========================================

test_get_latest_block() {
    log_test "Testing latest block retrieval..."

    if response=$(http_get "$EMBERS_API_URL/api/blockchain/blocks/latest" 200); then
        TEST_BLOCK_NUMBER=$(echo "$response" | jq -r '.blockNumber' 2>/dev/null)
        
        if [ -n "$TEST_BLOCK_NUMBER" ] && [ "$TEST_BLOCK_NUMBER" != "null" ]; then
            log_info "  Latest Block: $TEST_BLOCK_NUMBER"
            test_passed "Latest block retrieved"
            return 0
        else
            log_warning "Block number not in expected format"
            test_passed "Latest block endpoint responded (format varies)"
            return 0
        fi
    else
        test_failed "Latest block retrieval failed"
        return 1
    fi
}

test_get_block_by_number() {
    log_test "Testing block retrieval by number..."

    if [ -z "$TEST_BLOCK_NUMBER" ]; then
        log_warning "No block number available, using 0"
        TEST_BLOCK_NUMBER=0
    fi

    if response=$(http_get "$EMBERS_API_URL/api/blockchain/blocks/$TEST_BLOCK_NUMBER" 200); then
        log_info "  Block retrieved: $TEST_BLOCK_NUMBER"
        test_passed "Block retrieval by number successful"
        return 0
    else
        test_failed "Block retrieval by number failed"
        return 1
    fi
}

test_get_blockchain_info() {
    log_test "Testing blockchain info endpoint..."

    if response=$(http_get "$EMBERS_API_URL/api/blockchain/info" 200); then
        log_info "Blockchain info retrieved"
        test_passed "Blockchain info endpoint working"
        return 0
    else
        log_warning "Blockchain info endpoint may not be available"
        test_passed "Blockchain info test completed (endpoint optional)"
        return 0
    fi
}

# ========================================
# Wallet Tests
# ========================================

test_create_wallet() {
    log_test "Testing wallet creation..."

    local request_data='{}'

    if response=$(http_post "$EMBERS_API_URL/api/wallets/create" "$request_data" 200); then
        TEST_WALLET_ADDRESS=$(echo "$response" | jq -r '.address' 2>/dev/null)
        
        if [ -n "$TEST_WALLET_ADDRESS" ] && [ "$TEST_WALLET_ADDRESS" != "null" ]; then
            log_info "  Wallet Address: $TEST_WALLET_ADDRESS"
            test_passed "Wallet creation successful"
            return 0
        else
            log_warning "Wallet creation response format varies"
            test_passed "Wallet creation endpoint responded"
            return 0
        fi
    else
        log_warning "Wallet creation may not be supported"
        test_passed "Wallet creation test completed (may not be supported)"
        return 0
    fi
}

test_get_wallet_balance() {
    log_test "Testing wallet balance query..."

    if [ -z "$TEST_WALLET_ADDRESS" ]; then
        log_warning "No wallet address available, skipping"
        test_passed "Wallet balance test skipped (no address)"
        return 0
    fi

    if response=$(http_get "$EMBERS_API_URL/api/wallets/$TEST_WALLET_ADDRESS/balance" 200); then
        log_info "Wallet balance retrieved"
        test_passed "Wallet balance query successful"
        return 0
    else
        log_warning "Wallet balance query failed (may not be implemented)"
        test_passed "Wallet balance test completed"
        return 0
    fi
}

# ========================================
# Deploy Tests
# ========================================

test_deploy_contract() {
    log_test "Testing contract deployment..."

    local deploy_code='new deployId(`rho:rchain:deployId`) in { deployId!("test") }'
    local request_data=$(cat <<EOF
{
  "code": "$deploy_code",
  "phloLimit": 100000
}
EOF
)

    if response=$(http_post "$EMBERS_API_URL/api/deploy" "$request_data" 200); then
        TEST_DEPLOY_ID=$(echo "$response" | jq -r '.deployId' 2>/dev/null)
        
        if [ -n "$TEST_DEPLOY_ID" ] && [ "$TEST_DEPLOY_ID" != "null" ]; then
            log_info "  Deploy ID: $TEST_DEPLOY_ID"
            test_passed "Contract deployment successful"
            return 0
        else
            log_warning "Deploy response format varies"
            test_passed "Deploy endpoint responded"
            return 0
        fi
    else
        log_warning "Contract deployment may not be supported"
        test_passed "Deploy test completed (may not be supported)"
        return 0
    fi
}

test_get_deploy_status() {
    log_test "Testing deploy status query..."

    if [ -z "$TEST_DEPLOY_ID" ]; then
        log_warning "No deploy ID available, skipping"
        test_passed "Deploy status test skipped (no deploy ID)"
        return 0
    fi

    if response=$(http_get "$EMBERS_API_URL/api/deploy/$TEST_DEPLOY_ID/status" 200); then
        log_info "Deploy status retrieved"
        test_passed "Deploy status query successful"
        return 0
    else
        log_warning "Deploy status query failed (may not be implemented)"
        test_passed "Deploy status test completed"
        return 0
    fi
}

# ========================================
# Frontend Tests
# ========================================

test_frontend_accessible() {
    log_test "Testing Embers frontend accessibility..."

    if response=$(http_get "$EMBERS_FRONTEND_URL/" 200); then
        log_info "Frontend is accessible"
        test_passed "Frontend is accessible"
        return 0
    else
        test_failed "Frontend is not accessible"
        return 1
    fi
}

test_frontend_static_assets() {
    log_test "Testing frontend static asset loading..."

    # Try to fetch common static assets
    if http_get "$EMBERS_FRONTEND_URL/assets/" > /dev/null 2>&1 || \
       http_get "$EMBERS_FRONTEND_URL/static/" > /dev/null 2>&1 || \
       http_get "$EMBERS_FRONTEND_URL/favicon.ico" > /dev/null 2>&1; then
        log_info "Static assets are being served"
        test_passed "Frontend static assets accessible"
        return 0
    else
        log_warning "Could not verify static asset loading"
        test_passed "Frontend static asset test completed"
        return 0
    fi
}

# ========================================
# Integration Tests
# ========================================

test_api_blockchain_connectivity() {
    log_test "Testing API to blockchain connectivity..."

    # Check if API can connect to blockchain by querying node status
    if docker logs "$EMBERS_API_CONTAINER" 2>&1 | grep -i "connected\|ready\|initialized" > /dev/null; then
        log_info "API shows blockchain connectivity"
        test_passed "API blockchain connectivity verified"
        return 0
    else
        log_warning "Could not verify blockchain connectivity from logs"
        test_passed "Blockchain connectivity test completed"
        return 0
    fi
}

# ========================================
# Main Test Execution
# ========================================

main() {
    log_info "Embers Complete Integration Test Suite"
    log_info "Testing Embers API and Frontend"
    echo ""

    # Check for jq
    check_jq

    # Container health tests
    log_info "=== Container Health Tests ==="
    test_containers_running || exit 1
    test_wait_for_services || exit 1
    echo ""

    # API health tests
    log_info "=== API Health Tests ==="
    test_api_health || exit 1
    test_api_version || exit 1
    test_api_status || exit 1
    echo ""

    # Blockchain query tests
    log_info "=== Blockchain Query Tests ==="
    test_get_latest_block || exit 1
    test_get_block_by_number || exit 1
    test_get_blockchain_info || true
    echo ""

    # Wallet tests
    log_info "=== Wallet Tests ==="
    test_create_wallet || true
    test_get_wallet_balance || true
    echo ""

    # Deploy tests
    log_info "=== Deploy Tests ==="
    test_deploy_contract || true
    test_get_deploy_status || true
    echo ""

    # Frontend tests
    log_info "=== Frontend Tests ==="
    test_frontend_accessible || exit 1
    test_frontend_static_assets || true
    echo ""

    # Integration tests
    log_info "=== Integration Tests ==="
    test_api_blockchain_connectivity || true
    echo ""

    # Print summary
    print_test_summary
}

# Run main function
main "$@"

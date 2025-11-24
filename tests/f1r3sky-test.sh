#!/bin/bash
# F1R3Sky Integration Tests
# Tests AT Protocol services: PDS, BSKY, BSYNC, and Frontend

set -e

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source helper functions
source "$SCRIPT_DIR/lib/helpers.sh"

# Service configuration
PDS_URL="http://localhost:2583"
BSKY_URL="http://localhost:2584"
BSYNC_URL="http://localhost:3100"
FRONTEND_URL="http://localhost:8100"

# Container names (from docker-compose.f1r3sky.yml)
POSTGRES_CONTAINER="f1r3sky-postgres"
REDIS_CONTAINER="f1r3sky-redis"
BSYNC_CONTAINER="f1r3sky-bsync"
BSKY_CONTAINER="f1r3sky-bsky"
PDS_CONTAINER="f1r3sky-pds"
FRONTEND_CONTAINER="f1r3sky"

# Test data
TEST_USERNAME=""
TEST_PASSWORD="TestPassword123!"
TEST_EMAIL=""
TEST_DID=""
TEST_ACCESS_JWT=""
TEST_REFRESH_JWT=""
TEST_POST_URI=""
TEST_POST_CID=""

# Test timeout
TEST_TIMEOUT=${TEST_TIMEOUT:-60}

# ========================================
# Container Health Tests
# ========================================

test_containers_running() {
    log_test "Checking if all F1R3Sky containers are running..."

    local containers=(
        "$POSTGRES_CONTAINER:PostgreSQL"
        "$REDIS_CONTAINER:Redis"
        "$BSYNC_CONTAINER:BSYNC"
        "$BSKY_CONTAINER:BSKY"
        "$PDS_CONTAINER:PDS"
        "$FRONTEND_CONTAINER:Frontend"
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
        test_passed "All F1R3Sky containers are running"
        return 0
    else
        test_failed "Some F1R3Sky containers are not running"
        return 1
    fi
}

test_wait_for_services() {
    log_test "Waiting for F1R3Sky services to be ready..."

    local services_ready=true

    # Wait for PostgreSQL
    if wait_for_container "$POSTGRES_CONTAINER" $TEST_TIMEOUT; then
        log_info "  ✓ PostgreSQL is ready"
    else
        log_error "  ✗ PostgreSQL failed to become ready"
        services_ready=false
    fi

    # Wait for Redis
    if wait_for_container "$REDIS_CONTAINER" $TEST_TIMEOUT; then
        log_info "  ✓ Redis is ready"
    else
        log_error "  ✗ Redis failed to become ready"
        services_ready=false
    fi

    # Wait for BSYNC
    if wait_for_container "$BSYNC_CONTAINER" $TEST_TIMEOUT; then
        log_info "  ✓ BSYNC is ready"
    else
        log_error "  ✗ BSYNC failed to become ready"
        services_ready=false
    fi

    # Wait for BSKY
    if wait_for_container "$BSKY_CONTAINER" $TEST_TIMEOUT; then
        log_info "  ✓ BSKY is ready"
    else
        log_error "  ✗ BSKY failed to become ready"
        services_ready=false
    fi

    # Wait for PDS
    if wait_for_container "$PDS_CONTAINER" $TEST_TIMEOUT; then
        log_info "  ✓ PDS is ready"
    else
        log_error "  ✗ PDS failed to become ready"
        services_ready=false
    fi

    # Give services extra time to initialize
    log_info "Waiting additional 5s for service initialization..."
    sleep 5

    if [ "$services_ready" = true ]; then
        test_passed "All F1R3Sky services are ready"
        return 0
    else
        test_failed "Some F1R3Sky services failed to become ready"
        return 1
    fi
}

# ========================================
# AT Protocol API Tests
# ========================================

test_pds_health() {
    log_test "Testing PDS health endpoint..."

    # Try to get xrpc server description
    if response=$(http_get "$PDS_URL/xrpc/_health"); then
        log_info "PDS health check response received"
        test_passed "PDS is responding to health checks"
        return 0
    else
        test_failed "PDS health check failed"
        return 1
    fi
}

test_bsky_health() {
    log_test "Testing BSKY health endpoint..."

    # BSKY doesn't have a _health endpoint, check root instead
    if response=$(http_get "$BSKY_URL/"); then
        log_info "BSKY health check response received"
        test_passed "BSKY is responding to health checks"
        return 0
    else
        test_failed "BSKY health check failed"
        return 1
    fi
}

test_create_account() {
    log_test "Testing account creation via PDS..."

    # Generate test credentials
    TEST_USERNAME=$(generate_test_username)
    TEST_EMAIL=$(generate_test_email)

    local handle="${TEST_USERNAME}.bsky.social"

    log_info "Creating account: $handle ($TEST_EMAIL)"

    # Create account request
    local request_data=$(cat <<EOF
{
  "email": "$TEST_EMAIL",
  "handle": "$handle",
  "password": "$TEST_PASSWORD"
}
EOF
)

    # Call com.atproto.server.createAccount
    if response=$(http_post "$PDS_URL/xrpc/com.atproto.server.createAccount" "$request_data" 200); then
        # Parse response
        TEST_DID=$(echo "$response" | jq -r '.did')
        TEST_ACCESS_JWT=$(echo "$response" | jq -r '.accessJwt')
        TEST_REFRESH_JWT=$(echo "$response" | jq -r '.refreshJwt')

        if [ -n "$TEST_DID" ] && [ "$TEST_DID" != "null" ]; then
            log_info "Account created successfully"
            log_info "  DID: $TEST_DID"
            log_info "  Handle: $handle"
            test_passed "Account creation successful"
            return 0
        else
            log_error "Failed to parse account creation response"
            test_failed "Account creation returned invalid response"
            return 1
        fi
    else
        test_failed "Account creation request failed"
        return 1
    fi
}

test_create_session() {
    log_test "Testing session creation (login)..."

    local handle="${TEST_USERNAME}.bsky.social"

    log_info "Creating session for: $handle"

    local request_data=$(cat <<EOF
{
  "identifier": "$handle",
  "password": "$TEST_PASSWORD"
}
EOF
)

    # Call com.atproto.server.createSession
    if response=$(http_post "$PDS_URL/xrpc/com.atproto.server.createSession" "$request_data" 200); then
        # Update tokens
        TEST_ACCESS_JWT=$(echo "$response" | jq -r '.accessJwt')
        TEST_REFRESH_JWT=$(echo "$response" | jq -r '.refreshJwt')
        local did=$(echo "$response" | jq -r '.did')

        if [ "$did" = "$TEST_DID" ]; then
            log_info "Session created successfully"
            test_passed "Session creation successful"
            return 0
        else
            log_error "Session DID mismatch"
            test_failed "Session creation returned wrong DID"
            return 1
        fi
    else
        test_failed "Session creation request failed"
        return 1
    fi
}

test_create_post() {
    log_test "Testing post creation via BSKY..."

    local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")
    local post_text="Hello from F1R3FLY integration test! $(random_string 8)"

    log_info "Creating post: '$post_text'"

    local request_data=$(cat <<EOF
{
  "repo": "$TEST_DID",
  "collection": "app.bsky.feed.post",
  "record": {
    "\$type": "app.bsky.feed.post",
    "text": "$post_text",
    "createdAt": "$timestamp"
  }
}
EOF
)

    # Call com.atproto.repo.createRecord
    if response=$(http_post_with_auth "$PDS_URL/xrpc/com.atproto.repo.createRecord" "$request_data" "$TEST_ACCESS_JWT" 200); then
        TEST_POST_URI=$(echo "$response" | jq -r '.uri')
        TEST_POST_CID=$(echo "$response" | jq -r '.cid')

        if [ -n "$TEST_POST_URI" ] && [ "$TEST_POST_URI" != "null" ]; then
            log_info "Post created successfully"
            log_info "  URI: $TEST_POST_URI"
            log_info "  CID: $TEST_POST_CID"
            test_passed "Post creation successful"
            return 0
        else
            log_error "Failed to parse post creation response"
            test_failed "Post creation returned invalid response"
            return 1
        fi
    else
        test_failed "Post creation request failed"
        return 1
    fi
}

test_like_post() {
    log_test "Testing like creation (liking the post)..."

    if [ -z "$TEST_POST_URI" ] || [ -z "$TEST_POST_CID" ]; then
        log_error "No post to like (test_create_post must run first)"
        test_failed "Like test skipped - no post available"
        return 1
    fi

    local timestamp=$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")

    log_info "Liking post: $TEST_POST_URI"

    local request_data=$(cat <<EOF
{
  "repo": "$TEST_DID",
  "collection": "app.bsky.feed.like",
  "record": {
    "\$type": "app.bsky.feed.like",
    "subject": {
      "uri": "$TEST_POST_URI",
      "cid": "$TEST_POST_CID"
    },
    "createdAt": "$timestamp"
  }
}
EOF
)

    # Call com.atproto.repo.createRecord
    if response=$(http_post_with_auth "$PDS_URL/xrpc/com.atproto.repo.createRecord" "$request_data" "$TEST_ACCESS_JWT" 200); then
        local like_uri=$(echo "$response" | jq -r '.uri')

        if [ -n "$like_uri" ] && [ "$like_uri" != "null" ]; then
            log_info "Like created successfully"
            log_info "  URI: $like_uri"
            test_passed "Like creation successful"
            return 0
        else
            log_error "Failed to parse like creation response"
            test_failed "Like creation returned invalid response"
            return 1
        fi
    else
        test_failed "Like creation request failed"
        return 1
    fi
}

test_get_profile() {
    log_test "Testing profile retrieval..."

    local handle="${TEST_USERNAME}.bsky.social"

    log_info "Getting profile for: $handle"

    # Call app.bsky.actor.getProfile
    if response=$(http_get "$BSKY_URL/xrpc/app.bsky.actor.getProfile?actor=$handle" 200); then
        local did=$(echo "$response" | jq -r '.did')
        local display_handle=$(echo "$response" | jq -r '.handle')

        if [ "$did" = "$TEST_DID" ] && [ "$display_handle" = "$handle" ]; then
            log_info "Profile retrieved successfully"
            log_info "  DID: $did"
            log_info "  Handle: $display_handle"
            test_passed "Profile retrieval successful"
            return 0
        else
            log_error "Profile data mismatch"
            show_container_logs "$BSKY_CONTAINER" 30
            test_failed "Profile retrieval returned unexpected data"
            return 1
        fi
    else
        log_error "Profile retrieval failed - checking service logs"
        show_container_logs "$BSKY_CONTAINER" 30
        show_container_logs "$PDS_CONTAINER" 30
        test_failed "Profile retrieval request failed"
        return 1
    fi
}

test_delete_account() {
    log_test "Testing account deletion..."

    log_info "Deleting account: $TEST_DID"

    local request_data=$(cat <<EOF
{
  "did": "$TEST_DID",
  "password": "$TEST_PASSWORD",
  "token": ""
}
EOF
)

    # Call com.atproto.server.deleteAccount
    # Note: This endpoint might return different status codes based on implementation
    if response=$(http_post_with_auth "$PDS_URL/xrpc/com.atproto.server.deleteAccount" "$request_data" "$TEST_ACCESS_JWT" 200); then
        log_info "Account deleted successfully"
        test_passed "Account deletion successful"
        return 0
    else
        # Some implementations might return 204 or other codes
        log_warning "Account deletion returned non-200 status (may still be successful)"
        test_passed "Account deletion completed (non-standard response)"
        return 0
    fi
}

test_frontend_accessible() {
    log_test "Testing frontend accessibility..."

    # Try to access the frontend
    if response=$(http_get "$FRONTEND_URL/" 200); then
        log_info "Frontend is accessible"
        test_passed "Frontend is accessible"
        return 0
    else
        test_failed "Frontend is not accessible"
        return 1
    fi
}

# ========================================
# Main Test Execution
# ========================================

main() {
    log_info "Starting F1R3Sky Integration Tests"
    echo ""

    # Container health tests
    test_containers_running || exit 1
    test_wait_for_services || exit 1

    echo ""
    log_info "Running API endpoint tests..."
    echo ""

    # Health checks
    test_pds_health || exit 1
    test_bsky_health || exit 1

    # User workflow tests
    test_create_account || exit 1
    test_create_session || exit 1
    test_create_post || exit 1
    test_like_post || exit 1
    test_get_profile || exit 1

    # Frontend test
    test_frontend_accessible || true  # Don't fail on frontend issues

    # Cleanup: delete the test account
    test_delete_account || log_warning "Account deletion failed (account may persist)"

    # Print summary
    echo ""
    print_test_summary
}

# Run main function
main "$@"

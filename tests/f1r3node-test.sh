#!/bin/bash
# F1R3node Integration Tests
# Tests F1R3node blockchain network

set -e

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source helper functions
source "$SCRIPT_DIR/lib/helpers.sh"

# Container names
BOOTSTRAP_CONTAINER="rnode.bootstrap"
VALIDATOR1_CONTAINER="rnode.validator1"
VALIDATOR2_CONTAINER="rnode.validator2"
VALIDATOR3_CONTAINER="rnode.validator3"
READONLY_CONTAINER="rnode.readonly"
PROMETHEUS_CONTAINER="prometheus"
GRAFANA_CONTAINER="grafana"

# ========================================
# Container Health Tests
# ========================================

test_containers_running() {
    log_test "Checking if F1R3node containers are running..."

    local containers=(
        "$BOOTSTRAP_CONTAINER:Bootstrap Node"
        "$VALIDATOR1_CONTAINER:Validator 1"
        "$VALIDATOR2_CONTAINER:Validator 2"
        "$VALIDATOR3_CONTAINER:Validator 3"
        "$READONLY_CONTAINER:Read-only Node"
        "$PROMETHEUS_CONTAINER:Prometheus"
        "$GRAFANA_CONTAINER:Grafana"
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
        test_passed "All F1R3node containers are running"
        return 0
    else
        test_failed "Some F1R3node containers are not running"
        return 1
    fi
}

test_prometheus_accessible() {
    log_test "Testing Prometheus accessibility..."

    # Try to access Prometheus
    if response=$(http_get "http://localhost:9090/-/healthy" 200); then
        log_info "Prometheus is accessible"
        test_passed "Prometheus is accessible"
        return 0
    else
        test_failed "Prometheus is not accessible"
        return 1
    fi
}

test_grafana_accessible() {
    log_test "Testing Grafana accessibility..."

    # Try to access Grafana
    if response=$(http_get "http://localhost:3000/api/health" 200); then
        log_info "Grafana is accessible"
        test_passed "Grafana is accessible"
        return 0
    else
        test_failed "Grafana is not accessible"
        return 1
    fi
}

# ========================================
# Main Test Execution
# ========================================

main() {
    log_info "Starting F1R3node Integration Tests"
    echo ""

    # Container health tests
    test_containers_running || exit 1

    echo ""
    log_info "Running service tests..."
    echo ""

    # Monitoring stack tests
    test_prometheus_accessible || true
    test_grafana_accessible || true

    log_info "Note: Full F1R3node tests (blockchain operations, consensus) not yet implemented"

    # Print summary
    echo ""
    print_test_summary
}

# Run main function
main "$@"

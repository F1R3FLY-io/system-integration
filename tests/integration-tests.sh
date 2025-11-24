#!/bin/bash
# F1R3FLY Integration Test Suite
#
# Usage: ./integration-tests.sh [services...] [options]
#
# Services:
#   f1r3sky    - Test F1R3Sky AT Protocol services
#   embers     - Test Embers blockchain API bridge
#   f1r3node   - Test F1R3node blockchain
#   (none)     - Test all services
#
# Options:
#   --no-build  - Skip building services before testing
#   --no-up     - Skip starting services (assumes already running)
#   --clean     - Clean up services after testing
#   --help      - Show this help message

set -e

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source helper functions
source "$SCRIPT_DIR/lib/helpers.sh"

# Test configuration
SERVICES_TO_TEST=()
NO_BUILD=false
NO_UP=false
CLEAN=false
VERBOSE=false

# Available test services
AVAILABLE_SERVICES=("f1r3sky" "embers" "f1r3node")

# Show help
show_help() {
    cat << EOF
F1R3FLY Integration Test Suite

Usage: $0 [services...] [options]

Services:
  f1r3sky    - Test F1R3Sky AT Protocol services (PDS, BSKY, BSYNC, Frontend)
  embers     - Test Embers blockchain API bridge
  f1r3node   - Test F1R3node blockchain network
  (none)     - Test all available services

Options:
  --no-build     - Skip building services before testing
  --no-up        - Skip starting services (assumes they are already running)
  --clean        - Clean up and stop services after testing
  --verbose, -v  - Enable verbose output (show HTTP details and container logs)
  --help, -h     - Show this help message

Examples:
  $0                          # Test all services (build, up, test, leave running)
  $0 f1r3sky                  # Test only f1r3sky services
  $0 f1r3sky embers           # Test f1r3sky and embers
  $0 --no-build               # Test all without rebuilding
  $0 f1r3sky --no-up          # Test f1r3sky (assume already running)
  $0 --clean                  # Test all and clean up afterward
  $0 --verbose                # Test all with verbose output
  $0 f1r3sky -v --no-up       # Test f1r3sky verbosely (already running)

Environment Variables:
  TEST_VERBOSE=1              - Enable verbose output (same as --verbose)
  TEST_TIMEOUT=120            - Set container wait timeout (default: 60s)

EOF
    exit 0
}

# Parse arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --help|-h)
                show_help
                ;;
            --no-build)
                NO_BUILD=true
                shift
                ;;
            --no-up)
                NO_UP=true
                shift
                ;;
            --clean)
                CLEAN=true
                shift
                ;;
            --verbose|-v)
                VERBOSE=true
                export TEST_VERBOSE=true
                shift
                ;;
            -*)
                log_error "Unknown option: $1"
                echo "Use --help for usage information"
                exit 1
                ;;
            *)
                # Check if it's a valid service
                local valid=false
                for service in "${AVAILABLE_SERVICES[@]}"; do
                    if [ "$1" = "$service" ]; then
                        valid=true
                        break
                    fi
                done

                if [ "$valid" = true ]; then
                    SERVICES_TO_TEST+=("$1")
                else
                    log_error "Unknown service: $1"
                    echo "Available services: ${AVAILABLE_SERVICES[*]}"
                    exit 1
                fi
                shift
                ;;
        esac
    done

    # If no services specified, test all
    if [ ${#SERVICES_TO_TEST[@]} -eq 0 ]; then
        SERVICES_TO_TEST=("${AVAILABLE_SERVICES[@]}")
    fi
}

# Build services
build_services() {
    if [ "$NO_BUILD" = true ]; then
        log_info "Skipping build (--no-build specified)"
        return 0
    fi

    log_info "Building services..."

    cd "$ROOT_DIR"

    for service in "${SERVICES_TO_TEST[@]}"; do
        case $service in
            f1r3sky)
                log_info "Building f1r3sky services..."
                poetry run shardctl compose -f docker-compose.f1r3sky.yml build
                ;;
            embers)
                log_info "Building embers services..."
                poetry run shardctl compose -f docker-compose.embers.yml build
                ;;
            f1r3node)
                log_info "Building f1r3node services..."
                poetry run shardctl compose -f docker-compose.yml build
                ;;
        esac
    done

    log_success "Build completed"
}

# Start services
start_services() {
    if [ "$NO_UP" = true ]; then
        log_info "Skipping service startup (--no-up specified)"
        return 0
    fi

    log_info "Starting services..."

    cd "$ROOT_DIR"

    for service in "${SERVICES_TO_TEST[@]}"; do
        case $service in
            f1r3sky)
                log_info "Starting f1r3sky services..."
                poetry run shardctl compose -f docker-compose.f1r3sky.yml up -d
                ;;
            embers)
                log_info "Starting embers services..."
                poetry run shardctl compose -f docker-compose.embers.yml up -d
                ;;
            f1r3node)
                log_info "Starting f1r3node services..."
                poetry run shardctl compose -f docker-compose.yml up -d
                ;;
        esac
    done

    log_success "Services started"
}

# Run tests for a specific service
run_service_tests() {
    local service=$1
    local test_script="$SCRIPT_DIR/${service}-test.sh"

    if [ ! -f "$test_script" ]; then
        log_warning "Test script not found: $test_script"
        return 1
    fi

    log_info "Running tests for: $service"
    echo "========================================"

    if bash "$test_script"; then
        log_success "$service tests passed"
        return 0
    else
        log_error "$service tests failed"
        return 1
    fi
}

# Clean up services
cleanup_services() {
    if [ "$CLEAN" != true ]; then
        log_info "Services left running (use --clean to stop them)"
        return 0
    fi

    log_info "Cleaning up services..."

    cd "$ROOT_DIR"

    for service in "${SERVICES_TO_TEST[@]}"; do
        case $service in
            f1r3sky)
                log_info "Stopping f1r3sky services..."
                poetry run shardctl compose -f docker-compose.f1r3sky.yml down
                ;;
            embers)
                log_info "Stopping embers services..."
                poetry run shardctl compose -f docker-compose.embers.yml down
                ;;
            f1r3node)
                log_info "Stopping f1r3node services..."
                poetry run shardctl compose -f docker-compose.yml down
                ;;
        esac
    done

    log_success "Cleanup completed"
}

# Main execution
main() {
    log_info "F1R3FLY Integration Test Suite"
    echo ""

    # Check for jq
    check_jq

    # Parse arguments
    parse_args "$@"

    log_info "Services to test: ${SERVICES_TO_TEST[*]}"
    echo ""

    # Build services
    build_services

    # Start services
    start_services

    # Run tests
    log_info "Running integration tests..."
    echo ""

    local failed_services=()

    for service in "${SERVICES_TO_TEST[@]}"; do
        if ! run_service_tests "$service"; then
            failed_services+=("$service")
        fi
        echo ""
    done

    # Clean up if requested
    cleanup_services

    # Print overall summary
    echo ""
    echo "========================================"
    echo "Overall Test Summary"
    echo "========================================"
    echo "Tested services: ${SERVICES_TO_TEST[*]}"

    if [ ${#failed_services[@]} -eq 0 ]; then
        log_success "All service tests passed!"
        exit 0
    else
        log_error "Failed services: ${failed_services[*]}"
        exit 1
    fi
}

# Run main function
main "$@"

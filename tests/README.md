# F1R3FLY Integration Tests

Integration test suite for F1R3FLY microservices ecosystem.

## Overview

This test suite provides comprehensive integration testing for all F1R3FLY services:

- **f1r3sky**: AT Protocol services (PDS, BSKY, BSYNC, Frontend)
- **embers**: Blockchain API bridge and frontend
- **f1r3node**: Blockchain network with monitoring

## Prerequisites

- Docker and Docker Compose
- `jq` - JSON processor for parsing API responses
- Poetry (for running shardctl commands)
- All services cloned and configured (run `poetry run shardctl clone`)

### Install jq

```bash
# macOS
brew install jq

# Ubuntu/Debian
sudo apt-get install jq

# Other systems: https://jqlang.github.io/jq/download/
```

## Quick Start

```bash
# Run all tests (build, start services, test, leave running)
./tests/integration-tests.sh

# Run only f1r3sky tests
./tests/integration-tests.sh f1r3sky

# Run multiple service tests
./tests/integration-tests.sh f1r3sky embers

# Test with services already running (skip build and start)
./tests/integration-tests.sh --no-build --no-up

# Test with verbose output (show HTTP requests/responses and logs)
./tests/integration-tests.sh --verbose

# Test and clean up afterward
./tests/integration-tests.sh --clean
```

## Usage

```
./tests/integration-tests.sh [services...] [options]

Services:
  f1r3sky    - Test F1R3Sky AT Protocol services
  embers     - Test Embers blockchain API bridge
  f1r3node   - Test F1R3node blockchain network
  (none)     - Test all services

Options:
  --no-build     - Skip building services before testing
  --no-up        - Skip starting services (assumes already running)
  --clean        - Clean up and stop services after testing
  --verbose, -v  - Enable verbose output (HTTP details, container logs)
  --help, -h     - Show help message
```

## Test Suites

### F1R3Sky Tests (`f1r3sky-test.sh`)

Tests the AT Protocol implementation including:

**Container Health Checks:**
- PostgreSQL database
- Redis cache
- BSYNC (background sync)
- BSKY (AppView)
- PDS (Personal Data Server)
- Frontend web app

**API Functionality Tests:**
1. **Account Creation** - Create a new AT Protocol account via PDS
2. **Session Management** - Login and JWT token handling
3. **Post Creation** - Create posts using app.bsky.feed.post
4. **Social Interactions** - Like posts using app.bsky.feed.like
5. **Profile Retrieval** - Fetch user profiles via BSKY AppView
6. **Account Deletion** - Delete test accounts (cleanup)
7. **Frontend Access** - Verify web UI is accessible

**Endpoints Tested:**
- `POST /xrpc/com.atproto.server.createAccount` - Account creation
- `POST /xrpc/com.atproto.server.createSession` - Login
- `POST /xrpc/com.atproto.repo.createRecord` - Create posts/likes
- `GET /xrpc/app.bsky.actor.getProfile` - Get user profile
- `POST /xrpc/com.atproto.server.deleteAccount` - Delete account

### Embers Tests (`embers-test.sh`)

Currently implements:
- Container health checks (API and Frontend)
- Basic accessibility tests

**Planned Tests:**
- Wallet creation via Embers API
- Blockchain transactions
- Tipping functionality
- Transaction confirmation in F1R3node

### F1R3node Tests (`f1r3node-test.sh`)

Currently implements:
- Container health checks (Bootstrap, Validators, Read-only nodes)
- Prometheus metrics accessibility
- Grafana dashboard accessibility

**Planned Tests:**
- Blockchain consensus
- Transaction validation
- Smart contract deployment

## Test Architecture

### Directory Structure

```
tests/
├── README.md                    # This file
├── integration-tests.sh         # Main test orchestrator
├── f1r3sky-test.sh             # F1R3Sky test suite
├── embers-test.sh              # Embers test suite
├── f1r3node-test.sh            # F1R3node test suite
└── lib/
    └── helpers.sh              # Shared test utilities
```

### Helper Library (`lib/helpers.sh`)

Provides reusable functions for all test scripts:

**Logging Functions:**
- `log_info()` - Information messages
- `log_success()` - Success messages
- `log_error()` - Error messages
- `log_warning()` - Warning messages
- `log_test()` - Test descriptions

**Test Tracking:**
- `test_passed()` - Mark test as passed
- `test_failed()` - Mark test as failed
- `print_test_summary()` - Display results summary

**Docker Helpers:**
- `is_container_running()` - Check if container is running
- `is_container_healthy()` - Check container health status
- `wait_for_container()` - Wait for container to be ready

**HTTP Helpers:**
- `http_get()` - Make GET request
- `http_post()` - Make POST request
- `http_post_with_auth()` - POST with JWT authentication
- `http_delete_with_auth()` - DELETE with JWT authentication

**Test Data Generators:**
- `random_string()` - Generate random strings
- `generate_test_username()` - Generate test usernames
- `generate_test_email()` - Generate test email addresses

## Environment Variables

```bash
# Enable verbose output
TEST_VERBOSE=1 ./tests/integration-tests.sh

# Set custom container wait timeout (default: 60s)
TEST_TIMEOUT=120 ./tests/integration-tests.sh
```

## Examples

### Test All Services

```bash
# Full test suite with build
./tests/integration-tests.sh

# Quick test (services already running)
./tests/integration-tests.sh --no-build --no-up

# Test and clean up
./tests/integration-tests.sh --clean
```

### Test Specific Service

```bash
# Test only f1r3sky (most complete test suite)
./tests/integration-tests.sh f1r3sky

# Test f1r3sky with already running services
./tests/integration-tests.sh f1r3sky --no-build --no-up
```

### Debugging with Verbose Mode

```bash
# Run tests with verbose output to see HTTP details and logs
./tests/integration-tests.sh f1r3sky --verbose

# Or use the short flag
./tests/integration-tests.sh f1r3sky -v --no-build --no-up

# Set via environment variable
TEST_VERBOSE=true ./tests/integration-tests.sh f1r3sky
```

**Verbose output includes:**
- HTTP request methods and URLs
- Request and response bodies (formatted JSON)
- HTTP status codes
- Container logs (30 lines) when tests fail
- Additional debug information

### Development Workflow

```bash
# 1. Start services manually for development
poetry run shardctl up

# 2. Run tests without rebuilding or restarting
./tests/integration-tests.sh --no-build --no-up

# 3. Make changes to services

# 4. Rebuild and re-test specific service
poetry run shardctl compose -f docker-compose.f1r3sky.yml down
poetry run shardctl compose -f docker-compose.f1r3sky.yml up -d --build
./tests/integration-tests.sh f1r3sky --no-build --no-up

# 5. Clean up when done
./tests/integration-tests.sh --clean
```

## Test Output

The tests provide color-coded output:

- **Blue [INFO]** - Informational messages
- **Green [SUCCESS]** - Successful operations
- **Yellow [WARNING]** - Warnings (non-fatal)
- **Red [ERROR]** - Errors (may be fatal)
- **Blue [TEST]** - Test descriptions

Example output:

```
[INFO] F1R3FLY Integration Test Suite

[INFO] Services to test: f1r3sky

[TEST] Checking if all F1R3Sky containers are running...
  ✓ PostgreSQL is running
  ✓ Redis is running
  ✓ BSYNC is running
  ✓ BSKY is running
  ✓ PDS is running
  ✓ Frontend is running
[SUCCESS] ✓ All F1R3Sky containers are running

[TEST] Testing account creation via PDS...
[INFO] Creating account: testuser-abc12345.test (test-xyz98765@example.com)
[INFO] Account created successfully
  DID: did:plc:abcdef123456
  Handle: testuser-abc12345.test
[SUCCESS] ✓ Account creation successful

========================================
Test Summary
========================================
Total:  10
Passed: 10
Failed: 0
========================================
All tests passed!
```

## Troubleshooting

### jq not installed

```
Error: jq is not installed. Please install jq to run these tests.
```

**Solution:** Install jq using your package manager (see Prerequisites)

### Containers not running

```
[ERROR] ✗ Some F1R3Sky containers are not running
```

**Solution:** Start services first:
```bash
poetry run shardctl up
```

Or let the test script start them:
```bash
./tests/integration-tests.sh  # Don't use --no-up
```

### Container timeout

```
[ERROR] Container system-integration-pds-1 failed to become healthy after 60s
```

**Solution:** Increase timeout:
```bash
TEST_TIMEOUT=120 ./tests/integration-tests.sh
```

### API request failures

If tests fail with HTTP errors:

1. Check service logs:
   ```bash
   poetry run shardctl logs pds
   poetry run shardctl logs bsky
   ```

2. Verify services are healthy:
   ```bash
   docker ps
   docker inspect system-integration-pds-1
   ```

3. Check network connectivity:
   ```bash
   curl -v http://localhost:2583/xrpc/_health
   curl -v http://localhost:2584/xrpc/_health
   ```

### Port conflicts

If services fail to start, check for port conflicts:

```bash
# Check what's using f1r3sky ports
lsof -i :2583  # PDS
lsof -i :2584  # BSKY
lsof -i :8100  # Frontend
lsof -i :5433  # PostgreSQL
lsof -i :6380  # Redis
```

## Contributing

When adding new tests:

1. Follow the existing test structure
2. Use helper functions from `lib/helpers.sh`
3. Add proper test descriptions with `log_test()`
4. Mark tests as passed/failed with `test_passed()`/`test_failed()`
5. Handle errors gracefully
6. Clean up test resources (accounts, posts, etc.)
7. Update this README

## Future Enhancements

- [ ] Add wallet and transaction tests for Embers
- [ ] Add blockchain consensus tests for F1R3node
- [ ] Integration tests between f1r3sky and embers (tipping)
- [ ] Performance benchmarks
- [ ] Load testing capabilities
- [ ] CI/CD integration
- [ ] Test data fixtures and mocking
- [ ] Parallel test execution
- [ ] Test coverage reporting

## Resources

- [AT Protocol Specification](https://atproto.com/)
- [XRPC Documentation](https://atproto.com/specs/xrpc)
- [F1R3FLY Documentation](../README.md)
- [shardctl CLI Reference](../README.md#shardctl-cli)

# F1R3FLY Integration Tests

Comprehensive integration test suites for all F1R3FLY services.

## Quick Start

```bash
cd /Users/aidanstay/Documents/Work/F1R3FLY/firefly-system-integration

# Run all tests (builds, starts services, waits 90s, then tests)
./tests/run-tests.sh

# Test specific service
./tests/run-tests.sh f1r3sky

# Skip build (services already built)
./tests/run-tests.sh --no-build

# Skip startup (services already running)
./tests/run-tests.sh --no-up

# Custom wait time (default 90s)
./tests/run-tests.sh --wait 120

# Verbose output
./tests/run-tests.sh --verbose

# Clean up after testing
./tests/run-tests.sh --clean
```

## Test Suites

### F1R3Sky (`f1r3sky-test.sh`)
Tests AT Protocol services: PDS, BSKY, DataPlane, BSYNC, Ozone, Frontend

**Coverage:**
- 25+ test cases
- Infrastructure (PostgreSQL, Redis)
- Service health checks
- Account workflows (create, login, refresh)
- Content creation (posts, likes, profiles)
- AppView integration
- DataPlane subscription
- Frontend accessibility

### Embers (`embers-test.sh`)
Tests blockchain bridge API and React frontend

**Coverage:**
- 15+ test cases
- API health endpoints
- Blockchain queries
- Wallet operations
- Contract deployment
- Frontend accessibility

### F1R3Node (`f1r3node-test.sh`)
Tests blockchain network with validators and monitoring

**Coverage:**
- 30+ test cases
- All 5 nodes (bootstrap, 3 validators, read-only)
- RPC endpoints
- Consensus mechanism
- Block production
- Prometheus & Grafana monitoring

## Options

### `--no-build`
Skip building Docker images. Use when images are already built.

```bash
./tests/run-tests.sh --no-build
```

### `--no-up`
Skip starting services. Use when services are already running.

```bash
./tests/run-tests.sh --no-up
```

### `--clean`
Stop and remove services after testing.

```bash
./tests/run-tests.sh --clean
```

### `--verbose` or `-v`
Enable detailed output including HTTP request/response bodies and container logs.

```bash
./tests/run-tests.sh --verbose
# or
TEST_VERBOSE=1 ./tests/run-tests.sh
```

### `--wait TIME`
Set how many seconds to wait after starting services before running tests. Default is 90 seconds.

```bash
# Wait 2 minutes
./tests/run-tests.sh --wait 120

# Wait 3 minutes (for slower systems)
./tests/run-tests.sh --wait 180

# or via environment variable
TEST_WAIT_TIME=120 ./tests/run-tests.sh
```

**Why wait?** Services need time to:
- Complete database migrations
- Establish connections between services
- Start health check endpoints
- Begin indexing/subscription processes

## Environment Variables

- `TEST_VERBOSE=1` - Enable verbose output
- `TEST_TIMEOUT=180` - Set container wait timeout (seconds)
- `TEST_WAIT_TIME=90` - Seconds to wait after starting services (default: 90)

## Test Runner

### `run-tests.sh`
Main test runner that orchestrates building, starting, testing, and cleanup.

**Features:**
- Automatic service build and startup
- Multi-service testing
- Comprehensive error reporting
- Optional cleanup
- Flexible flags

## Individual Test Scripts

You can also run test scripts directly:

```bash
# F1R3Sky tests
./tests/f1r3sky-test.sh

# Embers tests
./tests/embers-test.sh

# F1R3Node tests  
./tests/f1r3node-test.sh

# With verbose output
TEST_VERBOSE=1 ./tests/f1r3sky-test.sh
```

## Documentation

- `QUICK_START.md` - Quick reference guide
- `NEW_TEST_SUITES.md` - Detailed test documentation
- `../NEW_TESTS_SUMMARY.md` - Implementation summary

## Common Scenarios

### Scenario 1: First Time Setup
```bash
# Build, start, wait 90s, and test everything
./tests/run-tests.sh

# Or wait longer for slower systems
./tests/run-tests.sh --wait 120
```

### Scenario 2: Development Testing
```bash
# Services already running, just test
./tests/run-tests.sh --no-up --no-build
```

### Scenario 3: CI/CD Pipeline
```bash
# Build, test, and clean up
./tests/run-tests.sh --clean
```

### Scenario 4: Debugging
```bash
# Verbose output for a specific service
./tests/run-tests.sh f1r3sky --verbose --no-build --no-up
```

### Scenario 5: Slow Systems
```bash
# Increase wait time for slower systems or cold starts
./tests/run-tests.sh --wait 180
```

## Troubleshooting

### Tests Fail

1. **Check service status:**
   ```bash
   docker ps
   poetry run shardctl ps
   ```

2. **View container logs:**
   ```bash
   docker logs <container_name>
   ```

3. **Run with verbose mode:**
   ```bash
   TEST_VERBOSE=1 ./tests/run-tests.sh
   ```

4. **Increase wait time:**
   ```bash
   # Services need more time to start
   ./tests/run-tests.sh --wait 180
   
   # Or increase individual test timeouts
   TEST_TIMEOUT=300 ./tests/run-tests.sh
   ```

### "Profile not found" (F1R3Sky)
DataPlane needs time to index from PDS. Wait a few seconds or check DataPlane logs:
```bash
docker logs f1r3sky-dataplane
```

### "Container not running"
Start services manually:
```bash
poetry run shardctl up
```

### "Connection refused"
Verify port mappings:
```bash
docker ps | grep <service>
```

## Test Output

### Success
```
[INFO] F1R3FLY Integration Test Runner
[INFO] Services to test: f1r3sky
[INFO] Building services...
[SUCCESS] Build completed
[INFO] Starting services...
[SUCCESS] Services started
[INFO] Running integration tests...
[TEST] Testing PDS health endpoint...
[SUCCESS] ✓ PDS is healthy and responding
...
========================================
Overall Test Summary
========================================
Tested services: f1r3sky
[SUCCESS] All service tests passed!
```

### Failure
```
[ERROR] ✗ Some tests failed
========================================
Overall Test Summary
========================================
Tested services: f1r3sky
[ERROR] Failed services: f1r3sky
```

## Helper Functions

All tests use shared helper functions from `lib/helpers.sh`:

- **Container:** `is_container_running()`, `wait_for_container()`, `show_container_logs()`
- **HTTP:** `http_get()`, `http_post()`, `http_post_with_auth()`, `http_delete_with_auth()`
- **Test:** `test_passed()`, `test_failed()`, `print_test_summary()`
- **Logging:** `log_info()`, `log_success()`, `log_error()`, `log_warning()`, `log_verbose()`

## File Structure

```
tests/
├── run-tests.sh          # Main test runner ⭐
├── f1r3sky-test.sh       # F1R3Sky comprehensive tests
├── embers-test.sh        # Embers comprehensive tests
├── f1r3node-test.sh      # F1R3Node comprehensive tests
├── lib/
│   └── helpers.sh        # Shared helper functions
├── README.md             # This file
├── QUICK_START.md        # Quick reference
└── NEW_TEST_SUITES.md    # Detailed documentation
```

## Contributing

When adding new tests:
1. Follow existing test structure
2. Use helper functions from `lib/helpers.sh`
3. Include descriptive test names
4. Add proper error handling
5. Update documentation
6. Test in both normal and verbose modes

## Statistics

- **Total Tests:** 70+
- **Services Covered:** 16
- **Test Scripts:** 3
- **Lines of Code:** ~2,000+
- **Documentation:** 1,200+ lines

## Next Steps

1. Run tests: `./tests/run-tests.sh`
2. Review output
3. Fix any failures
4. Integrate with CI/CD
5. Schedule regular test runs






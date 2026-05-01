# Integration Tests

Integration tests verify F1R3FLY node behavior through gRPC and HTTP APIs against
Docker-managed node clusters. Tests cover consensus, wallets, deploys, finalization,
heartbeat, state trimming, bonding, slashing, and more.

For a detailed description of every test case, see [TEST_CASES.md](TEST_CASES.md).

## Prerequisites

- **Docker & Docker Compose** -- containers are managed automatically by the test fixtures
- **Python 3.10** -- see the [main README](../README.md) for pyenv setup
- **Poetry** -- Python dependency manager
- **Memory** -- Scala node tests require at least **16 GB** RAM; Rust node tests require at least **12 GB** RAM

Install dependencies (from the repository root):

```bash
poetry install --with integration
```

This installs pytest, pytest-xdist, pytest-timeout, the Docker SDK, the pyf1r3fly
gRPC client, and all other packages needed by the test suite.

## Running Tests

All commands are run from the **repository root** (not from `integration-tests/`).

### Full Suite

```bash
poetry run pytest integration-tests/test/ -v --tb=short --log-cli-level=WARNING
```

### Single Test File

```bash
poetry run pytest integration-tests/test/test_wallets.py -v --tb=short
```

### Single Test Function

```bash
poetry run pytest integration-tests/test/test_wallets.py::test_validator1_pay_validator2 -v --tb=short
```

### Pytest Flag Reference

| Flag | Description |
| ---- | ----------- |
| `-v` | Verbose output -- prints each test name and its PASSED/FAILED status |
| `--tb=short` | Short tracebacks on failure (other options: `long`, `line`, `no`) |
| `--log-cli-level=WARNING` | Only show WARNING+ log messages on the console (suppresses INFO noise from Docker/gRPC). The full log is always written to the log file regardless of this setting |
| `-x` | Stop after the first failure |
| `--maxfail=N` | Stop after N failures |
| `-k EXPR` | Run only tests matching the expression (e.g. `-k "wallets or deploy"`) |
| `-s` | Disable output capture (show print statements in real-time) |
| `--timeout=N` | Override the per-test timeout (default: 300s; CI uses 600s) |
| `--timeout-scale=F` | Multiply all hardcoded polling timeouts by factor F (default: 1.0; CI uses 1.5 on arm64) |
| `--skip-setup` | Skip shard start and teardown (assume shard is already running, leave it running) |
| `--keep-running` | Start shard normally but skip teardown (leave running for debugging) |
| `--collect-only` | List tests that would run without executing them |

### Node Image Selection

Image priority: **env var > `--image` flag > `--rust`/`--scala` flag > hardcoded default**.

```bash
# Via shardctl (recommended)
poetry run shardctl test --rust                    # Rust (default image)
poetry run shardctl test --scala                   # Scala (default image)
poetry run shardctl test --image mynode:dev         # Custom image
F1R3FLY_RUST_IMAGE=mynode:dev shardctl test --rust  # Env var override (highest priority)

# Via pytest directly (set DEFAULT_IMAGE + compose image var)
DEFAULT_IMAGE=f1r3flyindustries/f1r3fly-rust-node:latest \
F1R3FLY_RUST_IMAGE=f1r3flyindustries/f1r3fly-rust-node:latest \
  poetry run pytest integration-tests/test/ -v --tb=short --log-cli-level=WARNING
```

`shardctl test` sets both `DEFAULT_IMAGE` (used by conftest.py for compose file selection and custom shard) and the appropriate `F1R3FLY_RUST_IMAGE`/`F1R3FLY_SCALA_IMAGE` (used by the static compose files via `${VAR:-default}` substitution).

## Test Infrastructure

### Docker Environments

Tests use three separate Docker environments with non-overlapping port ranges.
Each environment is managed by its own compose project and Docker network:

| Environment | Project Name | Network | Port Range | Lifecycle |
| ----------- | ------------ | ------- | ---------- | --------- |
| **Shard** | f1r3fly-shard | f1r3fly-test-shard | 40400-40455 | Session-scoped (shared across shard tests) |
| **Standalone** | f1r3fly-standalone | f1r3fly-test-standalone | same as shard (sequential) | Per-test (fresh node each time) |
| **Custom** | f1r3fly-custom | f1r3fly-test-custom | 40500-40545 | Per-test (fresh shard each time) |

### Shard Topology

The session-scoped shard (used by the majority of tests) runs:

| Node | Container | gRPC External | HTTP API |
| ---- | --------- | ------------- | -------- |
| Bootstrap | rnode.bootstrap | 40401 | 40403 |
| Validator 1 | rnode.validator1 | 40411 | 40413 |
| Validator 2 | rnode.validator2 | 40421 | 40423 |
| Validator 3 | rnode.validator3 | 40431 | 40433 |
| Read-only | rnode.readonly | 40451 | 40453 |

Deploys and proposes go through **validator nodes**. The bootstrap node is the
ceremony master and does not participate in consensus.

### Custom Shard

Tests in the custom group (`test_synchrony_constraint`, `test_asymmetric_bonds`,
`test_bonding_validators`, `test_trim_state`) each start a fresh shard with custom
bond weights, fault tolerance thresholds, and CLI overrides via
`start_custom_shard()` in `conftest.py`. These use port range 40500+ and the
`f1r3fly-test-custom` Docker network.

### Standalone Node

Standalone tests (`test_heartbeat` standalone functions, `test_propose` phlo price
test) start a single ephemeral node via `start_standalone_node()` in `conftest.py`.

## Test Suites

Tests execute in the order listed below. `test_consensus_health` must remain the
last **shard** test because it scans shard logs accumulated from all preceding
shard tests. It is placed before the custom tests so the shard can be torn down
(by a `pytest_runtest_teardown` hook in `conftest.py`) before the resource-heavy
custom shard tests begin.

| Suite | Group | Description |
| ----- | ----- | ----------- |
| `test_web_api` | shard | HTTP API endpoints (status, deploy, blocks, data-at-name) |
| `test_wallets` | shard | Token wallet transfers, balance checks, error handling |
| `test_heartbeat` | shard + standalone | Heartbeat auto-proposer (block creation, max-parents guard) |
| `test_deployment` | shard | Deploy error handling (insufficient phlo) |
| `test_storage` | shard | Data storage and cross-validator retrieval via registry |
| `test_genesis_ceremony` | shard | Genesis ceremony completion validation |
| `test_internal` | shard | Pure Python unit tests for test utilities |
| `test_dag_correctness` | shard | DAG structure, fault tolerance, cross-validator state agreement |
| `test_finalization` | shard | Block finalization advancement |
| `test_propose` | shard + standalone | Deploy validation, phlo price enforcement, cross-validator lookup |
| `test_replay_determinism` | shard | Cross-validator replay determinism and network resilience (bridge.rho, validator pause, slow deploy) |
| `test_consensus_health` | shard | Post-suite shard log scan for consensus errors (last shard test) |
| | | **-- shard torn down here --** |
| `test_synchrony_constraint` | custom | Per-validator synchrony constraint enforcement |
| `test_asymmetric_bonds` | custom | Consensus with non-equal validator stakes |
| `test_bonding_validators` | custom | Dynamic validator bonding at epoch boundaries |
| `test_trim_state` | custom | LFS (Last Finalized State) joiner synchronization |
| `test_shard_degradation` | shard | Production readiness under sustained deploy load |

## Parallel Execution

Tests are tagged with `@pytest.mark.xdist_group()` markers that assign each test
to one of three groups: `shard`, `standalone`, or `custom`. These groups correspond
to the three Docker environments above.

To run tests in parallel (3 workers, one per group):

```bash
poetry run pytest integration-tests/test/ -v --tb=short --log-cli-level=WARNING \
  -n 3 --dist=loadgroup
```

| Flag | Description |
| ---- | ----------- |
| `-n 3` | Run 3 parallel worker processes |
| `--dist=loadgroup` | Send all tests in the same `xdist_group` to the same worker |
| `-n 0` | Disable parallelism (run sequentially in the main process) |

Each worker gets its own session-scoped fixtures, so the shard worker brings up
the shard independently, the standalone worker starts/stops standalone nodes, and
the custom worker starts/stops custom shards. The three Docker environments use
non-overlapping port ranges so they do not conflict.

**Sequential execution (the default) is strongly recommended.** Parallel mode
(`-n 3`) is unreliable on a single machine because three Docker Compose
environments compete for CPU, memory, ports, and LMDB file locks. Observed
symptoms under parallel load include:

- `docker-compose up -d` failing with non-zero exit (port collisions or Docker
  daemon resource exhaustion)
- LMDB errors (`/var/lib/rnode/deploystorage`, `/var/lib/rnode/transaction`)
  caused by filesystem contention
- Deploy-inclusion timeouts (nodes are too slow to propose under heavy I/O)
- Massively inflated test durations (e.g. a simple `/api/status` call taking
  400+ seconds instead of <1s)

Parallel execution should only be attempted on machines with dedicated resources
per environment (separate port ranges are already configured, but CPU/memory
isolation is not).

## Log Files and Debugging

### Test Output Files

The test suite produces two output files in the `integration-tests/` directory:

| File | Description |
| ---- | ----------- |
| `integration-tests.log` | Full debug-level log of the entire test run. Contains all Docker operations, gRPC calls, node log parsing, and fixture lifecycle events. Written regardless of `--log-cli-level`. |
| `report.json` | Machine-readable JSON test report (pytest-json). Contains pass/fail status, durations, and error details for every test. |

To view results from the last run:

```bash
poetry run shardctl test-report           # Full summary
poetry run shardctl test-report --failures # Failed tests only
```

These paths are configured in `pyproject.toml` under `[tool.pytest.ini_options]`.

The `--log-cli-level` flag controls what appears on the **console** during the run.
Setting it to `WARNING` suppresses the verbose Docker and gRPC log messages while
still showing test progress. The log file always captures everything at `DEBUG` level.

### Container Logs

While the shard is running, individual node logs are available via Docker:

```bash
docker logs rnode.bootstrap
docker logs rnode.validator1
docker logs rnode.validator2
docker logs rnode.validator3
docker logs rnode.readonly
```

For custom shard containers, the names are prefixed with `rnode.custom.`:

```bash
docker logs rnode.custom.boot
docker logs rnode.custom.validator1
```

### Debugging Workflow

To keep the shard running after tests complete (for inspecting logs, RSpace state, or API responses):

```bash
# Option 1: Start shard normally, leave running after tests
poetry run shardctl test --rust --keep-running test_replay_determinism

# Inspect the shard
docker logs rnode.validator1 2>&1 | grep ReplayCostMismatch
curl http://localhost:40413/api/status
poetry run shardctl status

# Clean up when done
poetry run shardctl test-reset
```

```bash
# Option 2: Start shard yourself, run tests without touching it
poetry run shardctl up f1r3node-rust
poetry run shardctl wait

poetry run shardctl test --rust --skip-setup test_replay_determinism

# Shard is still running — inspect freely
docker logs rnode.validator2 2>&1 | tail -50

# Clean up when done
poetry run shardctl reset -y
```

Use `-x` (stop on first failure) with `--keep-running` to pause at the exact point of failure with the shard still running.

## Cleanup

```bash
# Clean up test data and containers
poetry run shardctl test-reset

# Or manually:
# Stop test containers and remove data volumes
docker compose --project-name f1r3fly-shard down --volumes --remove-orphans
docker compose --project-name f1r3fly-standalone down --volumes --remove-orphans
docker compose --project-name f1r3fly-custom down --volumes --remove-orphans
```

## Configuration

Test configuration is centralized in `pyproject.toml` under `[tool.pytest.ini_options]`.
Key settings:

- `python_files` -- Ordered list of test files. Controls execution order.
- `testpaths` -- Points to `integration-tests/test`.
- `timeout` -- Default per-test timeout (300s locally, CI overrides to 600s). Override with `@pytest.mark.timeout(N)`.
- `markers` -- Registers the `xdist_group` marker for parallel execution.
- `RUST_LOG` -- Can be passed to containers for debug log filtering (e.g. `RUST_LOG="info,f1r3fly.compute_parents_post_state.timing=debug"`). Set this env var before running `shardctl test` or export it so Docker Compose forwards it to node containers.

The `integration-tests/` directory contains its own `genesis/` and `certs/`
for test isolation. Node config files (`rust.conf`, `scala.conf`, `standalone-dev.conf`)
and `.env.node` are shared from the top-level `conf/` and root directories —
role-specific behavior is controlled via CLI flags in compose commands and
conftest.py. Only `logback.xml` remains in `integration-tests/conf/`.
Rholang contracts for tests are in `integration-tests/resources/`.

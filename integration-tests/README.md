# Integration Tests

Integration tests verify F1R3FLY node behavior through gRPC and HTTP APIs against Docker-managed node clusters. Tests cover consensus, wallets, deploys, finalization, heartbeat, state trimming, bonding, and more.

## Prerequisites

- **Docker & Docker Compose** — containers are managed automatically by the test fixtures
- **Python 3.10** — see the [main README](../README.md) for pyenv setup
- **Poetry** — Python dependency manager
- **Memory** — Rust node tests require at least **12 GB** RAM

Install dependencies (from the repository root):

```bash
poetry install --with integration
```

## Architecture

```
test/
├── conftest.py              Fixtures + hooks (shared_shard, provider, timeouts)
├── infra/                   Framework infrastructure
│   ├── types.py             NodeRole, ValidatorIdentity, PortMapping
│   ├── keys.py              Pre-defined validator keys (single source of truth)
│   ├── config.py            TimeoutConfig, ShardConfig, NodeConfig, ResourcePaths, NodeConf (HOCON parser)
│   ├── timeouts.py          TimeoutHierarchy (one scale factor → all timeouts)
│   ├── ports.py             PortAllocator (socket-verified, thread-safe)
│   ├── cleanup.py           CleanupRegistry (crash-resilient: atexit + session hooks)
│   ├── polling.py           Node-aware wrappers around f1r3fly.polling (deploy_and_read, wait_for_finalized, wait_for_deploy_finalized, etc.)
│   ├── assertions.py        Deploy/shard assertions (re-exported from f1r3fly.deploy + f1r3fly.par)
│   ├── log_events.py        Structured log event parsing + log scanning
│   ├── token_metadata.py    HTTP /api/status token helper (on-chain queries via pyf1r3fly)
│   ├── genesis.py           Custom genesis file generation
│   ├── compose.py           Docker Compose YAML generation
│   ├── node.py              Node (wraps pyf1r3fly F1r3flyClient + VaultAPI + HTTP helpers; _external_client + _internal_client)
│   ├── shard.py             Shard (collection of Nodes + joiner lifecycle)
│   └── providers/
│       ├── base.py          Provider + NodeHandle protocols
│       ├── docker.py        DockerProvider implementation
│       └── kubernetes.py    K8sProvider stub
└── tests/
    ├── shared/              Tests using session-scoped shared shard
    ├── custom/              Tests creating their own shard
    └── standalone/          Tests using standalone nodes
```

## Test Organization

| Directory | Tests | Description | Shard lifecycle |
|-----------|-------|-------------|-----------------|
| `tests/shared/` | 32 | Standard shard tests (10 files) | One shard per session, shared across all tests |
| `tests/custom/` | 19 | Custom configuration tests (8 files) | Each test creates/destroys its own shard |
| `tests/standalone/` | 16 | Standalone node tests | Each test creates/destroys standalone nodes |

## Running Tests

All commands are run from the **repository root**.

### Sequential (default)

```bash
# All tests
poetry run pytest

# By category
poetry run pytest integration-tests/test/tests/shared/       # 32 tests, one shard
poetry run pytest integration-tests/test/tests/custom/        # 11 tests, one shard per test
poetry run pytest integration-tests/test/tests/standalone/    # 16 tests, no shard

# Single file
poetry run pytest integration-tests/test/tests/shared/test_deployment.py -v -s

# Single test
poetry run pytest integration-tests/test/tests/shared/test_wallets.py::test_validator1_pay_validator2

# Stop on first failure (useful for debugging — keeps shard running for inspection with --keep-running)
poetry run pytest integration-tests/test/tests/shared/ -x -v -s

# Skip a known-problematic test
poetry run pytest integration-tests/test/tests/shared/ --deselect integration-tests/test/tests/shared/test_convergence.py::test_network_converges_after_slow_deploy

# CI (longer timeout, scaled polling)
poetry run pytest --timeout=600 --timeout-scale=1.5
```

### Parallel

Use pytest-xdist to run tests in parallel. Each worker gets its own pytest session with independent Docker resources (session-prefixed names, non-overlapping port ranges).

Port ranges are automatically partitioned per worker: worker 0 gets 41000-41499, worker 1 gets 41500-41999, etc. No cross-worker coordination needed.

```bash
# Maximum parallelism — auto-detect worker count
poetry run pytest -n auto --dist=loadgroup \
  integration-tests/test/tests/shared/ \
  integration-tests/test/tests/custom/ \
  integration-tests/test/tests/standalone/ \
  --monitor
```

How it works:
- **Shared tests** (`xdist_group("shared")`): All 32 tests run on ONE worker sequentially (they share a session-scoped shard)
- **Custom tests** (`xdist_group("custom")`): All tests run on one worker (to be parallelized in future)
- **Standalone tests**: Each test runs on its own worker — fully parallel. Exception: Group B joiner tests share a module-scoped fixture and use `xdist_group("token_metadata_b")` to stay together.

**Resource usage** (measured, standalone parallel): Peak 713MB total, 8 concurrent containers, ~80-170MB per node.

```bash
# Conservative — 3 workers (one per directory)
poetry run pytest -n 3 --dist=loadgroup \
  integration-tests/test/tests/shared/ \
  integration-tests/test/tests/custom/ \
  integration-tests/test/tests/standalone/
```

### Mixed

```bash
# Shared + standalone (skip expensive custom tests during dev)
poetry run pytest integration-tests/test/tests/shared/ integration-tests/test/tests/standalone/

# Just the heavy tests
poetry run pytest integration-tests/test/tests/custom/test_load.py \
  integration-tests/test/tests/custom/test_shard_degradation.py
```

### Resource Monitoring

Add `--monitor` to any run to track Docker resource usage across all test containers. The monitor discovers all `rnode.test.*` containers dynamically via `docker stats`. In parallel mode, only worker gw0 runs the monitor — it sees all containers globally across all workers.

The report is embedded in `report.json` (in the last test's teardown log) and logged to the console in sequential mode.

```bash
# Sequential with monitoring
poetry run pytest --monitor

# Parallel with monitoring
poetry run pytest -n auto --dist=loadgroup \
  integration-tests/test/tests/shared/ \
  integration-tests/test/tests/standalone/ \
  --monitor
```

Example output (from `report.json`):

```
RESOURCE USAGE
==========================================================================================
  Container                                       Peak Mem    Avg Mem   Peak CPU    Avg CPU
  --------------------------------------------------------------------------------------
  rnode.test.4969ad98.standalone1                    167MB      149MB     109.5%       3.9%
  rnode.test.73baceac.boot                           128MB      109MB      97.5%       8.4%
  rnode.test.73baceac.validator1                     118MB       91MB      99.7%       8.4%
  ...
  --------------------------------------------------------------------------------------
  Peak total memory (all containers): 713MB
  Peak container count: 8
  Samples collected: 39
==========================================================================================
```

Use this to determine resource requirements for parallel execution on CI.

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--timeout=N` | 300 | Global per-test timeout (seconds) |
| `--startup-timeout` | 90 | Max seconds for a node to reach Running state |
| `--timeout-scale` | 1.0 | Multiplier for all internal polling timeouts |
| `--skip-setup` | false | Skip shard creation (assume already running) |
| `--keep-running` | false | Don't tear down shard after tests |
| `--monitor` | false | Log peak memory/CPU per container at session end |
| `-v` | — | Verbose output |
| `--tb=short` | — | Short tracebacks on failure |
| `-x` | — | Stop after first failure |
| `-s` | — | Disable output capture |
| `--collect-only` | — | List tests without running |

### Timeouts

All timeouts are configurable — no hardcoded per-test timeouts:

| Timeout | Default | Used for |
|---------|---------|----------|
| `node_startup` | 90s | Node reaching Running state |
| `deploy_inclusion` | 10s | Deploy appearing in a block |
| `finalization` | 45s | LFB advancing past a block (per block) |
| `command` | 60s | gRPC/HTTP calls, WS connect, exit waits |
| `poll_interval` | 2.0s | Seconds between poll iterations |

### Configuration

- **NodeConf** — `NodeConf` dataclass (in `infra/config.py`) parses `defaults.conf` + `conf/rust.conf` via pyhocon. Exposes `shard_id`, `ftt`, `native_token_name`, `native_token_symbol`, `native_token_decimals`. Available as the `node_conf` session-scoped fixture in `conftest.py`. Tests derive expected values from `node_conf` instead of hardcoding them.
- **FTT** — Read from `node_conf.ftt` (parsed from `conf/rust.conf`), not hardcoded. Tests that need specific overrides (e.g. `ftt=-1`) set it explicitly in `ShardConfig`.
- **Node image** — `F1R3FLY_NODE_IMAGE` env var, defaults to `f1r3flyindustries/f1r3fly-rust-node:latest`.
- **pytest config** — `pyproject.toml` under `[tool.pytest.ini_options]`.
- **pyhocon** — Added as an integration dependency for HOCON config parsing (`NodeConf`).

## Key Concepts

### Tests interact with Node and Shard, never with Docker

```python
def test_something(shared_shard, timeouts):
    v1 = shared_shard.node("validator1")
    v1.deploy_string("@1!(1)", VALIDATOR1_ID.private_key())
    # assertions...
```

### Provider abstraction

The `NodeHandle` protocol abstracts all infrastructure operations:

| Method | Docker | K8s (future) | Local (future) |
|--------|--------|--------------|----------------|
| `logs()` | `docker logs` | `kubectl logs` | process stdout |
| `is_running()` | `docker inspect` | pod phase | process alive |
| `pause()` / `unpause()` | `docker pause/unpause` | network policy | `SIGSTOP/SIGCONT` |
| `exit_code()` | container exit code | pod termination status | `waitpid` |
| `restart()` | `docker restart` | pod delete | kill + restart |

### Node wraps pyf1r3fly

**gRPC:** `deploy_string`, `deploy_rho_file`, `send_deploy` (pre-built proto), `propose` (via `_internal_client` on port 40402), `exploratory_deploy`, `find_deploy`, `get_block`, `get_blocks`, `last_finalized_block`, `is_finalized`, `deploy_finalization_status` (canonical-state per-deploy tracking — prefer over `is_finalized` for deploy tracking). Uses `_external_client` (port 40401) for most operations and `_internal_client` (port 40402) for `ProposeService`.

**Vault:** `node.vault.get_balance(addr)` (exploratory deploy, readonly only on Rust node), `node.vault.deploy_get_balance(addr, key)` (real deploy, works on validators), `node.vault.transfer_ensure(from, to, amount, key)`, `node.vault.read_transfer_result(deploy_id, block_hash)`. Use `node.get_vault(shard_id)` to construct a `VaultAPI` with an explicit shard_id.

**HTTP:** `node.api_get(path)`, `node.api_post(path, json)`, `node.http_get(path)`

**Infrastructure:** `node.logs()`, `node.is_running()`, `node.pause()`, `node.unpause()`, `node.exit_code()`, `node.wait_for_exit(timeout)`

### Log scanning

The `check_node_logs_after_test` autouse fixture runs after every test (shared, custom, standalone). It queries the provider for all active node handles and scans their logs via the provider-agnostic `handle.logs()` method for PANIC entries. If any panic is found, the test fails with a formatted error showing which node panicked.

This catches crashes that don't fail the test directly — e.g. a panic on a node the test didn't query, or a crash triggered by background heartbeat activity.

Per-test (not per-session) so it:
- Pinpoints which test caused the panic
- Fails fast before teardown destroys the evidence
- Works for shared, custom, and standalone tests uniformly
- Provider-agnostic: works with Docker, Kubernetes, or any future provider

Currently checks for PANIC only. ERROR/WARN checking will be enabled once the acceptable-patterns whitelist in `infra/log_events.py` is populated by running tests and triaging normal log entries.

### Cleanup

Three layers of defense:

1. **Normal**: `Shard.destroy()` in fixture teardown
2. **atexit**: `CleanupRegistry` registered with Python `atexit` (fires on SIGTERM, SIGALRM)
3. **Next session**: `pytest_sessionstart` scans for leftover containers/volumes/networks

To manually clean up stale resources:

```bash
docker ps -a --filter "name=rnode.test." -q | xargs -r docker rm -f
docker network ls --filter "name=f1r3fly-test-" -q | xargs -r docker network rm
docker volume ls --filter "name=test-" -q | xargs -r docker volume rm
```

### Resource naming

| Resource | Pattern | Example |
|----------|---------|---------|
| Container | `rnode.test.{session_id}.{role}` | `rnode.test.a3f7b2c1.validator1` |
| Network | `f1r3fly-test-{session_id}` | `f1r3fly-test-a3f7b2c1` |
| Volume | `test-{session_id}-{role}-data` | `test-a3f7b2c1-boot-data` |

Port range 41000-42999. Socket-verified before allocation.

## Logging

### Console output

By default, pytest shows INFO-level log messages on the console (configured in `pyproject.toml`). Control verbosity with `--log-cli-level`:

```bash
# Suppress noisy Docker/gRPC logs, show only warnings+
poetry run pytest --log-cli-level=WARNING

# Full debug output on console
poetry run pytest --log-cli-level=DEBUG -s

# Live output with test names
poetry run pytest -v -s
```

### Log files

Two files are written automatically to `integration-tests/` regardless of console settings:

| File | Description |
|------|-------------|
| `integration-tests.log` | Full DEBUG-level log — Docker operations, gRPC calls, fixture lifecycle, node logs |
| `report.json` | Machine-readable JSON test report — pass/fail, durations, error details |

The log file always captures everything at DEBUG level. `--log-cli-level` only controls what appears on the console.

### Redirecting output

For CI or background runs, redirect all output to a file:

```bash
poetry run pytest integration-tests/test/tests/shared/ -v -s --timeout=600 2>&1 > /tmp/test_run.log

# Follow the log in another terminal
tail -f /tmp/test_run.log
```

### Container logs

While tests are running, individual node logs are available via Docker. Container names follow the pattern `rnode.test.{session_id}.{role}`:

```bash
docker logs rnode.test.a3f7b2c1.boot
docker logs rnode.test.a3f7b2c1.validator1
```

### Debugging

```bash
# Stop on first failure, keep shard running for inspection
poetry run pytest integration-tests/test/tests/shared/test_deployment.py -x --keep-running -v -s

# Inspect the running shard
docker logs rnode.test.<session_id>.validator1
curl http://localhost:<port>/api/status

# Clean up when done
docker ps -a --filter "name=rnode.test." -q | xargs -r docker rm -f
```

## Writing a New Test

### Shared shard test (place in `tests/shared/`)

```python
def test_something(shared_shard, timeouts):
    v1 = shared_shard.node("validator1")
    v1.deploy_string("@1!(1)", VALIDATOR1_ID.private_key())
    # assertions...
```

### Custom shard test (place in `tests/custom/`)

```python
def test_something(provider, timeouts):
    config = ShardConfig(
        bonds=[(VALIDATOR1_ID, 100), (VALIDATOR2_ID, 100)],
        ftt=-1,
        heartbeat=False,
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")
        # assertions...
    finally:
        shard.destroy()
```

### Deploy, finalize, and read

For contract tests that deploy code and read results from the `deployId` channel, use `deploy_and_read`. It handles the full workflow: deploy -> wait for inclusion -> wait for canonical-state finalization (`wait_for_deploy_finalized`) -> read deployId channel at the canonical block:

```python
from ...infra.polling import deploy_and_read

pars, block_hash, block_number = deploy_and_read(
    v1, code, VALIDATOR1_ID.private_key(),
    timeouts.deploy_inclusion, timeouts.finalization,
    phlo_limit=500_000_000,
)
# pars is a list of Par values from the deployId channel
value = par_as_int(pars[0])
```

`block_hash` and `block_number` refer to the canonical-state block containing the deploy's effects. If the deploy was first included in block X, merge-rejected, then re-included in block Y, the returned values point at Y. Terminal failures raise `DeployError` (Rholang execution failure or past-`deployLifespan` expiration).

For `.rho` files with substitutions:

```python
pars, _, _ = deploy_and_read(
    v1, "", key,
    timeouts.deploy_inclusion, timeouts.finalization,
    rho_file="resources/bridge-v2.rho",
    substitutions={"@placeholder@": "value"},
)
```

The core workflow lives in `f1r3fly.polling.deploy_and_read` — the infra wrapper adds `.rho` file resolution and string substitution.

### Deploy with validator fallback

For heavy deploys under sustained load, use `deploy_with_fallback` to try multiple validators. The deploy proto is built and signed once, then submitted to each validator in turn until one includes it:

```python
from ...infra.polling import deploy_with_fallback

deploy_id, block_info = deploy_with_fallback(
    shared_shard.validators,         # try V1, then V2, then V3
    code,                            # Rholang code (or use rho_file=)
    VALIDATOR1_ID.private_key(),
    timeouts.deploy_inclusion,       # timeout per validator
    phlo_limit=500_000_000,
)
```

If V1's proposer is busy, the same signed deploy is resubmitted to V2. If V2 also times out, it tries V3. Raises `TimeoutError` only if all validators fail.

### Standalone test (place in `tests/standalone/`)

```python
def test_something(provider, timeouts):
    config = NodeConfig(role=NodeRole.STANDALONE, cli_options={"--flag": "value"})
    handle = provider.create_standalone(config)
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    try:
        # assertions...
    finally:
        node.close()
        provider.destroy_standalone(handle)
```

## pyf1r3fly Boundary

| pyf1r3fly (reusable client library) | Test infra (Node-aware wrappers) |
|---|---|
| `F1r3flyClient` (gRPC) | Docker/K8s lifecycle (`Provider`) |
| `VaultAPI` (balance, transfer, deploy_get_balance) | `.rho` file resolution + substitution |
| `par.py` (Par extraction) | Log event parsing (`log_events.py`) |
| `polling.py` (`poll_until`, `deploy_and_read`, `wait_for_finalized`, `wait_for_deploy_finalized`, `deploy_with_fallback`) | Node-aware polling wrappers (`infra/polling.py`) |
| `deploy.py` (`check_deploy_succeeded/errored`, `find_deploy_in_block`) | Assert wrappers + multi-node checks (`infra/assertions.py`) |
| `contracts.py` (`registry_lookup`, `registry_query`) | `Node.registry_lookup()`, `Node.registry_query()` |
| `system_contracts.py` (token metadata queries) | Resource monitoring, cleanup, port allocation |
| `sign_deploy_data()`, `PrivateKey`, `PublicKey` | Compose generation, genesis files |

## Test Suite

### `tests/shared/` — Shared shard (32 tests)

One shard startup per session. Tests run sequentially against the same shard.

| Test | Docs |
|------|------|
| test_deployment | [docs](test/docs/test_deployment.md) |
| test_genesis_ceremony | [docs](test/docs/test_genesis_ceremony.md) |
| test_dag_correctness | [docs](test/docs/test_dag_correctness.md) |
| test_web_api | [docs](test/docs/test_web_api.md) |
| test_wallets | [docs](test/docs/test_wallets.md) |
| test_storage | [docs](test/docs/test_storage.md) |
| test_heartbeat | [docs](test/docs/test_heartbeat.md) |
| test_bridge_admin | [docs](test/docs/test_bridge_admin.md) |
| test_convergence | [docs](test/docs/test_convergence.md) |
| test_token_metadata | [docs](test/docs/test_token_metadata.md) |

### `tests/custom/` — Custom shard (19 tests, 8 files)

Each test creates its own shard with specific configuration.

| Test | Docs | Config |
|------|------|--------|
| test_consensus_safety | [docs](test/docs/test_consensus_safety.md) | Validator failure, FTT boundaries, epoch transition, merge determinism |
| test_asymmetric_bonds | [docs](test/docs/test_asymmetric_bonds.md) | Bonds 60/20/15, FTT=0.33, readonly |
| test_bonding_validators | [docs](test/docs/test_bonding_validators.md) | 2 validators, epoch-length=4, dynamic joiner, readonly |
| test_synchrony_constraint | [docs](test/docs/test_synchrony_constraint.md) | Bonds 100/102/98, per-node thresholds, FTT=-1 |
| test_trim_state | [docs](test/docs/test_trim_state.md) | 2 validators (10M/1), FTT=-1, dynamic joiner |
| test_load | [docs](test/docs/test_load.md) | Throughput benchmark, Prometheus metrics |
| test_shard_degradation | [docs](test/docs/test_shard_degradation.md) | 150 deploys, production readiness gate |
| test_websocket | [docs](test/docs/test_websocket.md) | 2-validator shard + readonly, WS event streaming |

### `tests/standalone/` — Standalone nodes (16 tests)

Each test creates standalone nodes or joiners. No shared shard.

| Test | Docs | Pattern |
|------|------|---------|
| test_heartbeat | [docs](test/docs/test_heartbeat.md) | Heartbeat config variations |
| test_propose | [docs](test/docs/test_propose.md) | Phlo price enforcement |
| test_token_metadata | [docs](test/docs/test_token_metadata.md) | Joiner mismatch, config validation, restart drift, multi-shard isolation, ceremony mismatch |

# test_heartbeat

## Purpose

Verifies the heartbeat proposer, which automatically creates blocks to maintain blockchain liveness when the Last Finalized Block (LFB) becomes stale. The heartbeat is critical for production networks: without it, the chain would stall when no user deploys are submitted.

Tests cover both standalone (single node) and shard (multi-validator) modes to exercise different heartbeat behaviors: solo block creation vs. coordinated multi-parent DAG construction.

## Heartbeat mechanism

The heartbeat proposer (`node/src/rust/instances/heartbeat_proposer.rs`) runs a background check loop:

1. Every `heartbeat-check-interval` seconds, check if the LFB is older than `heartbeat-max-lfb-age` seconds
2. If stale, propose an empty block to advance the chain
3. Uses a `Semaphore(1)` non-blocking lock to prevent concurrent proposals — if the lock is held (by a user deploy or another heartbeat), the attempt is skipped with a log message

Key log markers:
- `"Heartbeat: Starting with random initial delay of Xs ..."` — initialization
- `"Heartbeat: Successfully created block"` — block created
- `"CONFIGURATION ERROR: Heartbeat incompatible with max-number-of-parents=1"` — config guard

## Tests (4)

### test_heartbeat_creates_blocks_when_idle (standalone)

Starts a standalone node with heartbeat enabled (check-interval=5s, max-lfb-age=3s, max-parents=10). Without any user deploys, the heartbeat should automatically create blocks. Polls until at least 4 blocks exist (genesis + 3 heartbeat) and 3 "Successfully created block" log entries appear. Also checks:
- Heartbeat initialization log is present
- No "has not made progress" error (regression guard for standalone mode)
- Block shardId matches `node_conf.shard_id` (parsed from config)

### test_heartbeat_disabled_when_max_parents_is_one (standalone)

Starts a standalone node with `--max-number-of-parents=1` and heartbeat enabled. The heartbeat proposer detects this incompatible configuration and logs a `CONFIGURATION ERROR`. Waits 15s then verifies no new blocks were created — heartbeat is effectively disabled.

This guard exists because empty heartbeat blocks can't include all required parents when limited to 1 parent, which would cause `InvalidParents` validation failures.

### test_heartbeat_creates_blocks_when_idle_shard (shard)

Uses the session-scoped `shared_shard` fixture. Verifies **all validators** log the heartbeat startup message. Records the highest block number on each validator, then polls until all three have advanced by at least 2 blocks. This exercises the heartbeat under multi-validator coordination where the multi-parent DAG merge path is active.

### test_manual_propose_during_heartbeat_shard (shard)

Regression test for concurrent propose handling. Deploys and attempts a manual propose on **each validator** (V1, V2, V3) until one returns an expected response:
- `"NoNewDeploys"` — deploy was already included by the auto-proposer (PR #472 adds informative message)
- `"another propose is in progress"` — heartbeat holds the propose lock

After receiving the expected response, verifies **all nodes** (validators + readonly) advance LFB by 3+ blocks, proving no crash or stall. Panic detection is handled by the `check_node_logs_after_test` conftest fixture.

## Setup

### Standalone tests (`tests/standalone/test_heartbeat.py`)
- **Node**: Single standalone node via `provider.create_standalone()`
- **Heartbeat config**: `check-interval=5s`, `max-lfb-age=3s`, `max-number-of-parents=10` (default) or `1` (disabled test)

### Shard tests (`tests/shared/test_heartbeat.py`)
- **Topology**: Session-scoped `shared_shard` fixture (3 validators + readonly)
- **FTT**: From `conf/rust.conf` (0.1)
- **Heartbeat**: Enabled (default from shard config)

## What it proves

- Heartbeat creates blocks without user deploys (liveness guarantee)
- Heartbeat is correctly disabled when max-number-of-parents=1
- All validators log heartbeat startup
- Multi-validator heartbeat coordination works (no deadlocks, no crashes)
- Concurrent manual+heartbeat proposals handled safely via semaphore on all validators
- NoNewDeploys response explains auto-proposer behavior (PR #472)
- All nodes (including readonly) continue advancing LFB after propose contention
- The "has not made progress" self-validation check doesn't fire in standalone mode

## Infrastructure used

- `provider.create_standalone()` / `provider.destroy_standalone()` for standalone tests (`tests/standalone/test_heartbeat.py`)
- Session-scoped `shared_shard` fixture for shard tests (`tests/shared/test_heartbeat.py`)
- `NodeConfig` with `cli_flags` and `cli_options` for heartbeat configuration
- `poll_until()` for block count and block number advancement polling
- `Node.get_blocks()`, `Node.propose()` (via `_internal_client` on port 40402), `Node.deploy_string()`, `Node.logs()`

## Related

- [Heartbeat proposer source](../../../services/f1r3node-rust/node/src/rust/instances/heartbeat_proposer.rs)
- [test_shard_degradation](test_shard_degradation.md) — finalizer timeouts under sustained load (heartbeat interacts with propose backpressure)

# TODO

## Bugs



### Fault tolerance inconsistency across nodes for finalized blocks

Nodes disagree on `faultTolerance` values for the same finalized block. Confirmed in integration tests: boot node reports FT=0.0 for a finalized block at height #50 (the LFB itself), while validator1 reports FT=-1.0 (the "not yet computed" sentinel) for the same block via `get_block()`.

This matches the client-reported issue where rust-client shows FT=1.0 for all validators at a given block, but the HTTP API reports FT=0.0.

**Reproduction:**
- 3-validator shard (100/100/100 bonds), heartbeat enabled
- Deploy on all validators, wait for 10+ blocks
- Query `last_finalized_block()` on V1 to get LFB number
- Query `get_block(hash)` for blocks at or below LFB on all nodes
- Compare FT values — they disagree

**Integration test:** `test_dag_correctness` phase 4 (cross-node FT agreement on finalized blocks) — currently deselected.

**Impact:** Clients cannot trust FT values from the API. A finalized block may appear unfinalized (FT=-1) or weakly finalized (FT=0.0) depending on which node is queried.

**Fix locations:** Likely in the clique oracle FT scoring path — FT should be recomputed and consistent after finalization across all node roles (validator, boot, readonly).

### Contract query deploy returns empty deployId after finalization (intermittent)

A query deploy against a finalized bridge contract occasionally returns empty data from the `deployId` channel. The deploy is included in a block and not errored, but the contract's response chain (`lookup → queryCh → ret!(v) → deployId!(result)`) doesn't complete within the block's execution.

**Reproduction (intermittent):**
- Deploy bridge-v2.rho on V1, wait for finalization on all nodes
- Deploy getNonce query on V1 (or any validator)
- Query deploy included in block, not errored, but `get_data_at_deploy_id` returns empty par list

**Observed:** Passes in some runs, fails in others. When it passes, the bridge is typically in block #2+ and queries start at block #5+. When it fails, the bridge is in block #1 and queries start at block #4.

**Root cause hypothesis:** The multi-parent DAG merge may not include the bridge block's post-state in the query block's execution context, even though the bridge block is finalized. This is related to the `validAfterBlockNumber` semantics issue — VABN is a minimum, not a state inclusion guarantee.

**Workaround:** Retry. The test is correct — if finalization guarantees state inclusion, the query should always succeed.

### Network cannot self-recover from DAG tip divergence (f1r3node#437, f1r3node#224)

When validators temporarily desynchronize (any cause), they create independent blocks and diverge into separate DAG tips. The network has no convergence mechanism and permanently stalls.

**Triggers:**
- Long-running deploy execution (f1r3node#224: `loop!(1000000000)` exhausts phlo, blocking the proposing validator)
- Docker container pause/unpause (simulates network partition)
- Resource contention causing block processing delays
- Any cause of temporary validator desynchronization

**Integration tests written (reproduce the bug):**
File: `integration-tests/test/test_replay_determinism.py`

| Test                                         | Trigger                                                             | What it asserts                              | Result (3 runs)                                             |
| -------------------------------------------- | ------------------------------------------------------------------- | -------------------------------------------- | ----------------------------------------------------------- |
| `test_network_recovers_from_validator_pause` | Pause validator1 container for 15s, then unpause                    | LFB advances 3+ blocks after unpause         | FAILED 2/3 — non-deterministic, depends on divergence depth |
| `test_network_recovers_from_slow_deploy`     | Deploy `loop!(1000000000)` (phlo-exhausting loop from f1r3node#224) | LFB advances 3+ blocks after deploy included | FAILED 3/3 — reliable reproduction                          |

The slow deploy test (`loop!(1000000000)`) is the most reliable trigger — the proposing validator is blocked long enough for other validators to create multiple independent blocks via heartbeat.

**Root cause (from f1r3node#437):** Circular dependency between synchrony constraint, finalized-block baseline, stale-LFB recovery, and recovery throttling. After tips exchange populates the DAG with all diverged tips, no validator proposes a convergence block that justifies all known tips.

**Fix locations (from f1r3node#437):**
- `casper/src/rust/synchrony_constraint_checker.rs:346-365, 500-561` — primary synchrony calculation + finalized-block baseline fallback
- `node/src/rust/instances/heartbeat_proposer.rs:540-553, 879-896` — stale-LFB recovery + leader selection
- `casper/src/rust/engine/running.rs:84-129, 491-515` — tips update mechanism
- `casper/src/rust/multi_parent_casper_impl.rs:328-348` — justification building from snapshot

**Suggested fixes (from f1r3node#437):**
1. Post-tips-exchange convergence proposal: after receiving diverged tips, propose a block justifying all known latest messages
2. Single-leader recovery: deterministic leader selection to prevent N competing recovery blocks
3. Synchrony constraint should account for received-but-not-yet-justified blocks

### `/api/block/{hash}` returns empty transfers on validator nodes without indication

On validator nodes, `/api/block/{hash}` returns deploys with an empty `transfers` array even when the block contains transfers. This is because the transfer extraction requires the Block Report API which replays block execution — an expensive operation restricted to readonly nodes.

The problem: clients receiving `"transfers": []` on a validator cannot distinguish "no transfers in this block" from "transfers exist but aren't available on this node type." This leads to silent data loss — a client querying a validator thinks there were no transfers.

**Fix options:**
1. Return `null` (or omit the field) for `transfers` on validators instead of `[]`, so clients can distinguish "not available" from "empty"
2. Add a `transfersAvailable: bool` field to the response
3. Return an error or warning header when transfer data is unavailable

**File:** `node/src/rust/web/shared_handlers.rs` (`get_block_handler`) and `node/src/rust/api/web_api.rs`

### `shardctl down` and `test-reset` don't remove volumes from `shardctl up` compose project

When the shard is started via `shardctl up f1r3node-rust`, Docker Compose uses the project name `compose` (derived from the `compose/` directory), creating volumes like `compose_boot-data`, `compose_validator1-data`, etc.

But `test-reset` looks for volumes with the `f1r3fly-shard_` prefix (the integration test compose project name). This means:
- `shardctl down f1r3node-rust` stops containers but leaves volumes
- `shardctl test-reset` doesn't find the `compose_*` volumes to remove
- Starting a new shard reuses stale data

**Workaround:** `docker volume rm compose_boot-data compose_validator1-data compose_validator2-data compose_validator3-data compose_readonly-data`

**Additional issue:** `shardctl down f1r3node-rust` stops containers but does NOT remove volumes. It uses `docker compose down --remove-orphans` which doesn't include `-v`. Volumes persist across restarts, causing stale state.

**Fix needed:**
1. `shardctl down` should pass `-v` to `docker compose down` to remove volumes
2. `test-reset` and `down` operate on different compose projects (`f1r3fly-shard` vs `compose`) — they don't clean up after each other
3. Either use a consistent project name, or have each command check for both project prefixes

### shardctl status fails when running multiple compose configs

`poetry run shardctl status` builds a broken docker compose command when multiple compose files are involved — it duplicates `--env-file` and `-f` flags into a single command invocation instead of running separate commands per compose file.

**Error:**
```
unknown flag: --env-file
Command '['docker', 'compose', '--env-file', '.env.node', '-f', 'compose/f1r3node.yml', 'ps',
  '--env-file', '.env.node', '-f', 'compose/monitoring.yml', 'ps']'
```

**Expected:** Each compose file should be queried independently, or combined correctly with a single `--env-file` and multiple `-f` flags before the `ps` subcommand.

### f1r3node: `getDataAtName` returns error instead of empty payload when no data found

**File:** `node/src/rust/api/deploy_grpc_service_v1.rs`

When `getDataAtName(DataAtNameByBlockQuery)` finds no data on the queried channel for the specified block, it returns an error response (`RhoDataResponse { error: "No data found" }`) instead of a success response with an empty payload (`RhoDataResponse { payload: RhoDataPayload { par: [] } }`).

This forces clients to distinguish "no data exists" from "something went wrong" by parsing error message strings. The correct behavior is to return a success with empty data — "no data" is a valid query result, not an error.

**Impact:** Client libraries (pyf1r3fly) must special-case "No data found" errors to avoid treating valid empty results as failures.

### f1r3node-rust: `OPENAI_API_KEY` not passed to containers via `shard.yml`

`docker/shard.yml` declares `OPENAI_ENABLED=${OPENAI_ENABLED:-false}` in the `environment:` section but does NOT declare `OPENAI_API_KEY`. When `OPENAI_ENABLED=true` is set in `docker/.env`, the node starts with OpenAI enabled but no API key — causing a panic at `openai_service.rs:92`.

The `.env` file has the key, but Docker Compose only passes env vars from `--env-file` that are also declared in the compose `environment:` section (or used in `${VAR}` substitutions). Since `OPENAI_API_KEY` isn't declared in `shard.yml`, it never reaches the container.

**Fix:** Add `OPENAI_API_KEY=${OPENAI_API_KEY:-}` to the `x-rnode` anchor's `environment:` in `shard.yml` (and `standalone.yml`). Or make the node gracefully handle missing API key when enabled (warn and disable instead of panic).

## Structured gRPC Error Codes

Tests and client applications currently match gRPC error messages with ad-hoc string patterns (e.g. `(?i)pars` for parse errors, `"NoNewDeploys"` for propose contention). These strings are undocumented, unstable across versions, and fragile.

The node should return structured error codes so clients can match on codes instead of parsing error text.

### Scope
1. Define `ErrorCode` enum in protobuf (`DeployServiceV1.proto`) — e.g. `PARSE_ERROR`, `INSUFFICIENT_PHLO`, `NO_NEW_DEPLOYS`, `PROPOSE_CONTENTION`, `INVALID_PHLO_PRICE`, etc.
2. Update ~17 gRPC error handlers in f1r3node-rust (`deploy_grpc_service_v1.rs`, `block_api.rs`) to include the error code alongside the message
3. Update `F1r3flyClientException` in pyf1r3fly to expose the error code
4. Update integration tests to match on codes instead of strings

### Context
PR #472 improved error logging (actual messages in all 17 handlers) but did not add structured codes. Docs updated in `docs/node/README.md` and `docs/rnode-api/index.md` — method listings only, no error catalog.

## Exploratory Deploy Cannot Query Contracts with Persistent State Channels

Exploratory deploy (read-only, no block created) works for:
- Direct registry lookups (`registry_lookup`) — returns stored values
- System contracts like `TokenMetadata` — respond synchronously

But fails for contracts that read from persistent state channels. Tested against bridge-v2.rho:
- `getNonce` reads from `nonceCh` via `for (@v <- nonceCh) { nonceCh!(v) | ret!(v) }`
- All four Rholang patterns tested return 0 pars on the readonly node
- Direct `lookup!` (pattern 3) returns 1 par — the registry lookup itself works
- The contract method call doesn't complete within exploratory deploy's execution window

**Root cause hypothesis:** Exploratory deploy creates an isolated execution environment. When the contract tries `for (@v <- nonceCh)`, that channel exists in the persistent tuplespace from the original deploy, but exploratory deploy may not have access to it — or the async response via `*ret` isn't captured before the exploratory deploy returns.

**Impact:** Read-only queries against stateful contracts (bridge, DEX, governance) must use real deploys with `deployId` channel, which creates a block and consumes phlo. This makes client-side reads expensive.

**Investigation needed:**
1. Does the Rust node's exploratory deploy execute against the full tuplespace state? Or just a snapshot?
2. Is there a timing/capture issue where the contract responds after the exploratory deploy has already returned?
3. Can the contract be restructured to respond synchronously (inline the state read)?
4. Can exploratory deploy be extended to wait for responses on `new` channels?

**File:** `f1r3node-rust/casper/src/rust/api/block_api.rs:1443-1506` — exploratory deploy execution path

## Background Traffic Generator for Integration Tests

Integration tests currently run against a mostly idle shard — deploys only happen when a specific test sends them. Real networks have continuous activity. A background traffic generator would:

1. Run as an opt-in conftest fixture (`active_traffic`)
2. Send deploys to all validators on a loop (unique channels per session to avoid test conflicts)
3. Create realistic network conditions: DAG growth, propose contention, state accumulation
4. Tests that need realistic conditions opt in via fixture dependency

This would make convergence, heartbeat, and degradation tests more meaningful — they'd exercise the node under conditions closer to production.

### Design considerations
- Must not interfere with test assertions (unique deploy channels)
- Must be stoppable (fixture teardown)
- Deploy rate should be configurable
- Should distribute across validators (round-robin or random)

## Log Scanner Whitelist

The log scanning infrastructure exists (`infra/log_events.py`: `scan_for_errors`, `ACCEPTABLE_PATTERNS`) but is disabled because the whitelist is empty. To enable it:

1. Run all tests with scanning enabled (collect all WARN/ERROR/PANIC messages)
2. Triage each message: expected during normal operation → add to `ACCEPTABLE_PATTERNS` with comment
3. Enable as autouse fixture in conftest.py — any unexpected log error fails the test

This catches problems that don't crash the node but indicate issues (e.g. repeated gRPC errors, state corruption warnings, resource exhaustion).

## Testing Roadmap

### Level 1: Local dev (current — 3 validators, Docker Compose, single machine)
- FTT=0.1, sync=0, equal stake
- Tests: smoke test, integration tests, `test_convergence` (slow deploy recovery)
- **Status:** `test_convergence` passes 1/1 with correct timeout (300s find, 480s advance). Previous 3/3 failures were timeout bugs (deploy takes ~200s, old timeout was 180s).
- `synchrony-constraint-threshold = 0.67` is incompatible with 3 validators — requires all 3 active to propose. When 1 is blocked, the other 2 only reach 0.50 (needs 0.67). This is by design, not a code bug. See `docs/consensus-configuration.md`.

### Level 2: Local multi-node (next — 7+ validators, Docker Compose)
- FTT=0.67 (default), sync=0, equal stake
- Can actually test BFT tolerance (lose 1-2 validators and still finalize)
- Still single machine but realistic consensus dynamics
- Requires: 7-validator compose file, expanded genesis bonds.txt, integration test suite adaptation
- The `start_custom_shard()` in conftest.py already supports custom validator counts
- **Pending fixes for `synchrony-constraint-threshold > 0`:** Three changes stashed on f1r3node-rust branch `fix/convergence-after-divergence` address #437 when the synchrony constraint is active:
  1. Convergence override in `synchrony_constraint_checker.rs` — count unjustified parent blocks as "seen senders"
  2. Single-leader gating on convergence recovery in `heartbeat_proposer.rs`
  3. Lift frontier chase cap during convergence scenario in `heartbeat_proposer.rs`
  These are no-ops at `sync=0` but needed at `sync=0.67` with 7+ validators where losing 1-2 validators should still allow convergence. Verified they compile clean and don't break consensus rules.
- **Production-config regression tests:** The Level 1 custom shard tests (`test_bonding_validators`, `test_trim_state`, `test_synchrony_constraint`) use `ftt=-1` for deterministic block numbering. They verify mechanisms work in isolation but don't test under production finalization dynamics. Level 2 should add shard-level variants that run with `ftt=0.67, sync=0` and heartbeat enabled:
  - Bonding at epoch boundary when finalization is delayed by 2-3 blocks
  - Trim state / LFS sync when finalization lags behind the tip
  - Validator loss (1 of 7) with continued finalization
  - Convergence after validator stall (the #437 scenario with threshold>0)

### Level 3: Kubernetes staging
- Validators on separate nodes (real network latency, independent failure domains)
- Helm chart or Kubernetes manifests for node deployment
- Tests: network partitions, node restarts, rolling upgrades, clock skew, real P2P discovery, disk I/O under load, memory limits

### Level 4: Production canary
- Subset of real validators running new version alongside stable validators
- Gradual rollout with monitoring (Prometheus/Grafana)
- Rollback capability if consensus issues emerge

## Rust/Scala Node Incompatibilities

Tracked during E2E demo stabilization (2026-03-26). These affect embers and scoped test scripts.

### Missing system contracts on Rust node
- `rho:registry:insertRandom` — `No value set`. Not available on Rust standalone or Rust shard. Available on Scala (older builds), missing on Scala `dev`.
- `rho:crypto:secp256k1Sign` — `No value set`. Cannot do Rholang-native signing on either Rust or Scala `dev` nodes.

### Missing system contracts on Scala `dev` node
- `rho:registry:insertRandom` — also missing on `f1r3fly-scala-node:dev`
- `rho:crypto:secp256k1Sign` — also missing


### Peek (`<<-`) operator
- Works on Scala standalone (confirmed 3/3 pass with treeHashMap)
- Works on Rust standalone (confirmed via cross-set test + stdout verification)
- Previously believed broken on Rust — this was incorrect. The scoped test failures were caused by insufficient phlo, not peek incompatibility.

### Consume+resend pattern hangs
- Hangs on Scala standalone (peek test passes 3/3, consume+resend test hangs)
- Hangs on Rust standalone (same behavior — stdout shows correct values but `deploy-and-wait` never returns)
- The consume+resend workaround (replacing peek) is WORSE than the original peek pattern

### `node_cli deploy-and-wait` finalization detection
- Works correctly on Rust shard (with observer on separate node)
- Intermittently hangs on Rust standalone — deploys execute correctly (stdout confirms) but `is_finalized` check never returns true
- Does NOT affect embers (which uses its own HTTP polling finalization)
### `rho:deploy:data` format
- Scala `dev` node: `for(@timestamp, @deployerId, @deployId <- deployDataCh)` pattern may not match — stdout from deploy-data inspection test didn't fire (no error either)
- Needs further investigation

### `rho:registry:insertSigned:secp256k1` on Scala `dev`
- `rs!()` available and deploys finalize without errors
- But `rl!()` via explore-deploy returns empty after 30s — contract not findable in registry
- No `abort!("failed to insert env")` fires — suggesting `rs!()` returned a URI (not Nil)
- Root cause unclear — may be explore-deploy state visibility issue on Scala `dev`

## Node API Improvements for Client Applications

These are improvements to the f1r3node Rust node HTTP API that would make client applications (like embers) more robust. Currently clients need workarounds for missing or incomplete information.

### 1. `/api/deploy/{id}` should return deploy execution details

**Current:** Returns the block header (BlockInfo) containing the deploy — no deploy-specific fields like `errored`, `cost`, or `systemDeployError`.

**Needed:** Return the deploy's execution info including `errored: bool`, `cost: u64`, `systemDeployError: string`. Currently the only way to get this is a two-step lookup: find the block via `/api/deploy/{id}`, then fetch the block via `/api/block/{hash}`, then match the deploy by signature in the `deploys[]` array.

**Impact:** Without this, clients can't detect whether a deploy's Rholang execution errored. A deploy that aborts (e.g., `abort!("in saveAiAgentsTeam")`) appears as successfully finalized.

### 2. `/api/deploy/{id}` should include block number

**Current:** Returns `blockHash` but not `blockNumber`.

**Needed:** Include `blockNumber: u64` in the response. Clients need the block number to verify observer sync state and set `valid_after_block_number` for dependent deploys.

### 3. `valid_after_block_number` semantics documentation

**Current:** The `valid_after_block_number` field in `DeployDataProto` means "don't include this deploy before block N". But it does NOT guarantee the deploy executes against block N's post-state, because multi-parent blocks may not include block N in their parent lineage.

**Needed:** Either:
- (a) Document the exact semantics clearly — clients need to understand that `valid_after_block_number` is a minimum, not a state guarantee
- (b) Add a `requires_block_hash` field that guarantees the deploy only executes in a block whose state includes a specific prior block's changes

**Impact:** This is the root cause of flaky sequential deploys (create → save → deploy). The save deploy can execute against state that doesn't include the create's changes, even when `valid_after_block_number` is set to the create's block number.

### 4. explore-deploy should accept a block hash parameter

**Current:** `explore-deploy` always evaluates against the latest tip. The `/api/explore-deploy-by-block-hash` endpoint exists but its availability and behavior on the observer need verification.

**Needed:** Clients should be able to target a specific finalized block's state for reads, ensuring consistency between writes and subsequent reads.

### 5. Observer `explore-deploy` state consistency

**Current:** The observer's `explore-deploy` can return stale state even when `last-finalized-block` reports a newer block number. This creates a window where reads don't see recent writes.

**Needed:** Either guarantee that `explore-deploy` evaluates against the state reported by `last-finalized-block`, or provide an API that allows clients to specify a minimum block number for reads.



## Monitoring follow-ups

- **Rust metric dashboard queries** ([system-integration#22](https://github.com/F1R3FLY-io/system-integration/pull/22)): `f1r3node.json` panels use Scala Kamon metric names (`rchain_*`). Rust node uses `metric_name{source="f1r3fly.*"}`. Dashboard queries need rewriting for Rust — blocked on [f1r3node#405](https://github.com/F1R3FLY-io/f1r3node/pull/405) (Phase 1 observability gauges)

## shardctl commands not tested in CI

The following shardctl commands are not exercised in CI. Commands marked interactive or destructive are impractical to test in CI; the rest could be added when feasible.

### Could be tested

| Command   | Reason not tested yet                                           |
| --------- | --------------------------------------------------------------- |
| `restart` | Needs a running shard + verify it comes back healthy            |
| `pull`    | CI uses `docker pull` directly; could switch to `shardctl pull` |
| `compose` | Generic passthrough; hard to assert on                          |

### Impractical to test in CI

| Command                   | Reason                              |
| ------------------------- | ----------------------------------- |
| `clone` / `setup`         | Requires SSH keys for private repos |
| `build` / `build-service` | Source builds take 30+ minutes      |
| `exec` / `shell`          | Interactive (requires TTY)          |
| `clean`                   | Destructive; nothing to clean in CI |

### Tested in CI

`up`, `wait`, `status`, `down`, `reset`, `logs`, `test`, `test-report`, `test-reset`, `ps`, `--help`

---

## shardctl needs updating for new test framework

The integration test framework was rewritten. `shardctl` still references the old v1 infrastructure in several places:

### Remove static compose files from integration-tests/

The following files are v1 artifacts no longer used by any test code:
- `integration-tests/docker-compose.rust.yml`
- `integration-tests/docker-compose.scala.yml`
- `integration-tests/docker-compose.standalone-rust.yml`
- `integration-tests/docker-compose.standalone-scala.yml`

The new framework generates compose YAML dynamically via `test/infra/compose.py`.

### Update `test-reset` command

`shardctl test-reset` (`shardctl/cli.py` ~line 1151) references the old compose files and project names (`f1r3fly-shard`, `f1r3fly-standalone`). It needs to clean up the new naming:
- Containers: `rnode.test.*`
- Networks: `f1r3fly-test-*`
- Volumes: `test-*`

The `CleanupRegistry.cleanup_stale_sessions()` in the test framework already handles this at session start, but `shardctl test-reset` should be a manual equivalent.

### Update `test` command

`shardctl test` sets `DEFAULT_IMAGE` and `F1R3FLY_RUST_IMAGE` env vars. The new framework uses only `F1R3FLY_NODE_IMAGE`. Update `shardctl test` to set `F1R3FLY_NODE_IMAGE` instead.

### Update `test-report` command

Verify `shardctl test-report` still finds `integration-tests/report.json` — the path is configured in `pyproject.toml` and should be unchanged.

### Remove `--rust` / `--scala` flags

The framework is Rust-only. The `--rust` and `--scala` flags on `shardctl test` are no longer meaningful. Remove them and simplify to `shardctl test` (default) with optional `--image` override.

## `--skip-setup` incompatible with `--keep-running` across `shardctl test` invocations

`shardctl test --keep-running` leaves the shard running after tests. A subsequent `shardctl test --skip-setup` should reuse those containers, but it fails with `No such container: rnode.bootstrap` because `shardctl test` creates a temporary compose project with a unique name each invocation. The `--skip-setup` run looks for containers under the new project name, not the previous one.

Workaround: use `--keep-running` on every run (it detects existing containers and reuses them). Fix: `--skip-setup` should discover containers by name regardless of compose project, or persist the project name between runs.

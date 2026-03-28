# TODO

## Bugs

### RSpace `remove_datum` out-of-bounds causes ReplayCostMismatch (f1r3node-rust)

`hot_store.rs:378` — when `remove_datum` receives an out-of-bounds index, it logs a warning and returns `None` instead of propagating an error. This causes the block creator and replayers to take different execution paths, producing different costs.

**Trigger:** Any Rholang contract with duplicate channel initialization (e.g., `requiredSigsCh!(2)` sent twice). The duplicate sends create 2 datums on the channel. Depending on execution order, `remove_datum` may target index 1 on a channel with only 1 element.

**Reproduced:** Deploying `docs/27-03-rspace-error/bridge.rho` against `f1r3flyindustries/f1r3fly-rust-node:dev`. V1 executes with cost 503383, V2/V3 replay with cost 503559 (delta: 176). Block rejected with `ReplayCostMismatch`. Network permanently stalls (see #437).

**How to reproduce:**
1. Pull latest dev image: `docker pull f1r3flyindustries/f1r3fly-rust-node:dev`
2. Start shard: `cd services/f1r3node-rust/docker && F1R3FLY_RUST_IMAGE=f1r3flyindustries/f1r3fly-rust-node:dev docker compose -f shard.yml up -d`
3. Wait for all validators to reach Running state: `docker compose -f shard.yml logs 2>&1 | grep "Making a transition to Running state"` (should see 5 nodes)
4. Deploy the bridge contract: `cd services/rust-client && cargo run -q --release -- deploy -f ../../docs/27-03-rspace-error/bridge.rho -H localhost -p 40412 -b`
5. Wait ~30 seconds, then check validator2 logs: `docker logs rnode.validator2 2>&1 | grep -E "Index out of bounds|ReplayCostMismatch"`
6. Expected: "Index out of bounds when removing datum" WARN followed by `ReplayCostMismatch { initial_cost: 503383, replay_cost: 503559 }`
7. After ~2 minutes, confirm network stall: `docker logs rnode.validator1 --tail=5` shows "newest latest message is more than 60s old"

**Bridge contract bug (trigger):** `docs/27-03-rspace-error/bridge.rho` has duplicate channel initialization — `requiredSigsCh!(2)` on lines 53 and 56, `oracleCountCh!(3)` on lines 54 and 59. This puts 2 datums on each channel. The node should handle this deterministically regardless — duplicate sends are valid Rholang.

**Unit tests written (pass — bug only manifests cross-validator):**
- `rspace++/tests/hot_store_spec.rs`: `remove_datum_out_of_bounds_on_duplicate_datums_returns_none_and_preserves_state` — confirms `remove_datum` returns None on OOB
- `rholang/tests/accounting/replay_cost_mismatch_spec.rs`: 4 tests using `evaluate_and_replay()` with duplicate channel patterns — all pass because play/replay share the same checkpoint. The bug requires independent RSpace state between creator and replayer.

**Why unit tests pass but shard fails:** The `evaluate_and_replay()` helper resets the replay runtime to the exact same RSpace root and rigs the event log from play. In a real shard, each validator maintains its own RSpace history store. When the block creator's hot store has data loaded via the `Occupied` path and the replayer loads via the `Vacant` path (from its own history store), `remove_datum` can target different indices, producing the cost delta.

**Fix location:** `rspace++/src/rspace/hot_store.rs:378-407` — `remove_datum` must produce deterministic behavior between creator and replayer. Either error the deploy or handle the out-of-bounds identically on both paths.

**Integration test needed:** A Docker-based test in `integration-tests/` that deploys `bridge.rho` to a running shard and verifies all validators accept the block without `ReplayCostMismatch`. This is the only test level that reproduces the bug because it uses real multi-validator RSpace state.

**Related:** The bridge contract itself has a logic bug (duplicate channel sends), but the node must handle any valid Rholang deterministically regardless. The network stall after rejection is tracked separately below.

### Single rejected block permanently stalls the entire shard (f1r3node-rust)

When a block is rejected by replaying validators (e.g., due to `ReplayCostMismatch`), the network has no recovery path. The rejecting validators stop proposing entirely and the network permanently halts.

**Observed sequence:**
1. V1 creates block containing the bridge deploy, self-validates as Valid
2. V2/V3 reject the block with `ReplayCostMismatch`
3. V1 continues building on top of the rejected block — all subsequent blocks depend on it
4. V2/V3 log "missing dependencies" for every new block from V1, since they all depend on the rejected block
5. V2/V3 stop proposing entirely — no heartbeat triggers or propose attempts
6. V1 sees "newest latest message is more than 60s old" — no other validator acknowledges its blocks
7. Network is permanently stalled. No automatic recovery. Requires full volume wipe.

**Expected behavior:** V2/V3 should fork from the last-known-good parent (before the rejected block) and continue proposing. V1 should eventually see its fork wasn't finalized and converge to the consensus branch. The bad deploy should be excluded but the network should continue.

**Impact:** Any contract that triggers a replay divergence — even a rare edge case — is effectively a denial-of-service vector against the entire shard.

**Related:** Same fundamental issue as GitHub #437 (network cannot self-recover from DAG tip divergence). The `remove_datum` bug above and long-running AI deploys (#437) are both triggers, but the inability to recover is the deeper architectural gap.

### shardctl status fails when running multiple compose configs
`poetry run shardctl status` builds a broken docker compose command when multiple compose files are involved — it duplicates `--env-file` and `-f` flags into a single command invocation instead of running separate commands per compose file.

**Error:**
```
unknown flag: --env-file
Command '['docker', 'compose', '--env-file', '.env.node', '-f', 'compose/f1r3node.yml', 'ps',
  '--env-file', '.env.node', '-f', 'compose/monitoring.yml', 'ps']'
```

**Expected:** Each compose file should be queried independently, or combined correctly with a single `--env-file` and multiple `-f` flags before the `ps` subcommand.

## Rust/Scala Node Incompatibilities

Tracked during E2E demo stabilization (2026-03-26). These affect embers and scoped test scripts.

### Missing system contracts on Rust node
- `rho:registry:insertRandom` — `No value set`. Not available on Rust standalone or Rust shard. Available on Scala (older builds), missing on Scala `dev`.
- `rho:crypto:secp256k1Sign` — `No value set`. Cannot do Rholang-native signing on either Rust or Scala `dev` nodes.

### Missing system contracts on Scala `dev` node
- `rho:registry:insertRandom` — also missing on `f1r3fly-scala-node:dev`
- `rho:crypto:secp256k1Sign` — also missing

### URI renames (both Rust and Scala `dev`)
Old names no longer resolve. All code must use new names:
- `rho:rev:address` → `rho:vault:address`
- `rho:rchain:deployerId` → `rho:system:deployerId`
- `rho:rchain:revVault` → `rho:vault:system`
- `rho:rchain:deployId` → `rho:system:deployId`

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

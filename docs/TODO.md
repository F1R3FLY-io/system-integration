# TODO

## Bugs

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

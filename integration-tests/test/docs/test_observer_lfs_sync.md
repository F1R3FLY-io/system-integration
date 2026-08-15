# test_observer_lfs_sync

## Purpose

Verifies that a fresh readonly observer can attach to a live, actively producing shard, run its full Initializing → LFS-sync → Running transition without consensus or storage errors, and remain consistent with the existing nodes' view of the chain.

This is the production scenario for joiner-side LFS coverage: an operator brings up a new readonly node against a running shard. The new node has no local rspace, no DAG, no blocks. During Initializing it must fetch every block from genesis to the LFB, the LFB rspace tuple-space (chunk-by-chunk), the mergeable-channels store entry per block, and the rspace history for every ancestor root reachable within `max_parent_depth + depth_buffer` of the LFB — including pre-state hashes for multi-parent merge intermediates that wouldn't otherwise reach the joiner.

The test runs against a dedicated module-scoped shard (`observer_shard`, marked `isolated_shard` so it runs before the session `shared_shard` exists). A fresh shard means the pre-attach DAG window is entirely produced under this test's own background load, which makes the multi-parent precondition (below) verifiable rather than dependent on whatever a long-lived session shard happens to have near its tip. The shard has no baked-in readonly — a genesis-attached readonly does not exercise the "attach against live shard" code path. Instead the test attaches a TRANSIENT observer mid-run via `add_observer` (context-managed cleanup) so the assertion target is the same code an operator would hit in production.

The test is structured to fail loudly on three classes of regression:

1. **Block-storage gaps** — `DAGStorageMissingHash` / `KvStoreError` from a joiner that downloaded the LFB but lacks side-branch ancestors a subsequent gossip block references.
2. **Rspace history gaps** — `RootRepositoryDivergence` / `UnknownRootError` from a joiner that's missing the rspace post-state (or merge-intermediate pre-state) of an ancestor block, surfacing during validation of a child that references it.
3. **Drift / stall** — observer's LFB stays well behind v1's after sync, indicating a gossip-path or finalization regression masked by a successful initial sync.

## Tests (1)

### test_observer_lfs_sync_against_active_shard

Single comprehensive test that:

1. Starts a `_BackgroundLoad` thread on V1/V2/V3 (round-robin deploys, ~2.5 s per producer) so the fresh shard's chain advances — and merges — from the start.
2. Polls v1 until the DAG has at least `_MIN_PRE_ATTACH_DEPTH` (12) blocks. Without depth, the forward-horizon collapses to ~genesis and the new code paths emit early — the test would silently turn into a thinner check.
3. Polls v1's recent window (`_MULTI_PARENT_SCAN_DEPTH` = 24) until a **finalized** multi-parent merge block exists, and records it (most recent finalized merge wins, keeping it near the forward horizon). Finalized-only is load-bearing: a finalized pre-attach block is inside the observer's genesis→LFB bulk-sync range, so the later visibility check proves LFS coverage — an unfinalized tip could instead arrive via post-attach gossip, or be orphaned and never arrive at all.
4. Immediately attaches a transient observer via `with observer_shard.add_observer() as observer:` — attaching right after the precondition keeps the recorded merge block within the forward-horizon depth of the LFB. (Context-managed: removed and volume cleaned up on exit, but its post-attach errors are still captured by the autouse log scanner before cleanup.)
5. Polls until observer's LFB is within `_LFB_DRIFT_TOLERANCE` (10 blocks) of v1's LFB.
6. Holds load for `_OBSERVER_LOAD_WINDOW_SEC` (20 s) post-attach so the observer ingests via gossip after the bulk sync — confirms the gossip path works post-LFS, not just the bulk LFS path.
7. Stops load and re-polls drift convergence — catches "synced then stalled" regressions where bulk sync succeeded but ongoing ingest is broken.
8. Runs cross-node consistency assertions, including that the observer holds the recorded multi-parent block (see Key assertions below).

The autouse fixture `check_node_logs_after_test` in `conftest.py` runs after the test body but before observer cleanup, so observer logs are scanned for forbidden patterns. A `DAGStorageMissingHash` / `RootRepositoryDivergence` / `KvStoreError` / `UnknownRootError` in the observer's logs fails the test even if every explicit assertion passes.

## Setup

- **Topology**: Dedicated module-scoped `observer_shard` (boot + 3 validators, no baked-in readonly) plus a transient observer attached during the test; `isolated_shard`-ordered before the session `shared_shard` spins up
- **Heartbeat**: Enabled (drives multi-parent merge construction)
- **FTT**: From `conf/rust.conf`
- **Background load**: 3 producers (V1/V2/V3), 2.5 s per-producer interval, ~1.2 deploys/sec aggregate
- **Pre-attach depth gate**: `_MIN_PRE_ATTACH_DEPTH = 12` blocks on v1 before observer attach
- **Multi-parent precondition**: a **finalized** merge block must exist in v1's last `_MULTI_PARENT_SCAN_DEPTH = 24` blocks before the observer attaches; its hash is recorded for the post-sync coverage assertion
- **Drift tolerance**: `_LFB_DRIFT_TOLERANCE = 10` blocks (observer LFB vs v1 LFB)
- **Load window post-attach**: `_OBSERVER_LOAD_WINDOW_SEC = 20` s

## What it proves

- A fresh observer can LFS-sync against an actively producing shard and reach Running.
- The forward-horizon rspace history sync correctly walks ancestor roots within `max_parent_depth + depth_buffer` of the LFB, including pre-state hashes for multi-parent merge intermediates.
- The mergeable-channels store entry per block is correctly fetched and stored on the joiner.
- The `lfs_block_requester` lower-bound covers both the deploy-lifespan window and the forward-horizon parent reach (so side-branch ancestors aren't rejected as out-of-window).
- The observer's local PoS state, finalization view, and per-block post-state hashes match the validators' for the LFB and several ancestors.
- The post-LFS gossip path works — observer keeps up after bulk sync completes.

## Key assertions

| # | Assertion | What it proves |
|---|---|---|
| A | `len(observer_bonds) == 3` and each V1/V2/V3 in `observer_bonds` | Observer's PoS state is complete |
| B | `wait_for_block_visible(v1, observer_lfb_hash)` | Observer's LFB is in v1's block store too — cross-node propagation, not just LFB advancement |
| C | `assert_block_finalized_on_all_nodes([v1, observer], observer_lfb_hash)` | Both report `isFinalized=True` for the same hash |
| D | `assert_bonds_map_consistent_across_nodes([v1, v2, v3, observer], observer_lfb_hash, observer_bonds)` | All four nodes report identical bonds at that hash |
| E | `assert_all_nodes_agree_on_block` for ≥ 5 of observer's last finalized ancestor blocks | Deep cross-node post-state agreement, not just LFB-tip — catches latent storage/replay divergence |
| F | `wait_for_block_visible(observer, multi_parent.blockHash)` for the **finalized** merge block recorded pre-attach | Observer's genesis→LFB bulk sync provably covered a multi-parent merge — gossip cannot satisfy it by accident and the block cannot be orphaned. Deterministic, unlike the earlier post-hoc sample of v1's recent window, which falsely failed when the sampled tail was single-parent (soak preflight, f1r3node-rust PR #273) |
| G | Drift remains within `_LFB_DRIFT_TOLERANCE` after settle (Step 7 poll) | Catches "synced then stalled" regressions |
| H | (Implicit, autouse) Observer logs free of `DAGStorageMissingHash` / `RootRepositoryDivergence` / `KvStoreError` / `UnknownRootError` | The whole reason this PR exists |

## Infrastructure used

- Dedicated module-scoped `observer_shard` fixture (3 validators, heartbeat, no readonly), `isolated_shard`-marked
- `check_node_logs_after_test` autouse fixture for forbidden-log detection (see [ARCHITECTURE.md § 7](ARCHITECTURE.md#7-log-scanning))
- `Shard.add_observer()` (context-managed transient observer)
- Local `_BackgroundLoad` class — copy of the pattern in `test_bonding_validators` so cadence/failure-tolerance knobs stay explicit at the call site
- `Node.deploy_string()`, `Node.last_finalized_block()`, `Node.get_blocks()`, `Node.get_block()`
- `assert_all_nodes_agree_on_block()`, `assert_block_finalized_on_all_nodes()`, `assert_bonds_map_consistent_across_nodes()` from assertions
- `wait_for_block_visible()`, `poll_until()`, `all_blocks_visible()`, `get_blocks_if_enough()` from polling

## Related

- [test_bonding_validators](test_bonding_validators.md) — Phase C also attaches a fresh observer, but on a 5-bonded shard after mid-test bonding (V4 + V5). That coverage depends on bonding correctness; this test isolates the LFS-sync code paths.
- [test_dag_correctness](test_dag_correctness.md) — multi-parent DAG structure on the session shared shard; complementary check that the structure an observer must sync is itself well-formed.
- [test_trim_state](test_trim_state.md) — alternate joiner path that LFS-syncs from the LFB instead of replaying genesis (different config, different code path).

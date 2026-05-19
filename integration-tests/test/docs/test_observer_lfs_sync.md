# test_observer_lfs_sync

## Purpose

Verifies that a fresh readonly observer can attach to a live, actively producing shard, run its full Initializing → LFS-sync → Running transition without consensus or storage errors, and remain consistent with the existing nodes' view of the chain.

This is the production scenario for joiner-side LFS coverage: an operator brings up a new readonly node against a running shard. The new node has no local rspace, no DAG, no blocks. During Initializing it must fetch every block from genesis to the LFB, the LFB rspace tuple-space (chunk-by-chunk), the mergeable-channels store entry per block, and the rspace history for every ancestor root reachable within `max_parent_depth + depth_buffer` of the LFB — including pre-state hashes for multi-parent merge intermediates that wouldn't otherwise reach the joiner.

The test runs against the existing `shared_shard` fixture, but the readonly node from that fixture was attached at genesis and therefore does not exercise the "attach against live shard" code path. This test attaches a TRANSIENT observer mid-session via `add_observer` (context-managed cleanup) so the assertion target is the same code an operator would hit in production.

The test is structured to fail loudly on three classes of regression:

1. **Block-storage gaps** — `DAGStorageMissingHash` / `KvStoreError` from a joiner that downloaded the LFB but lacks side-branch ancestors a subsequent gossip block references.
2. **Rspace history gaps** — `RootRepositoryDivergence` / `UnknownRootError` from a joiner that's missing the rspace post-state (or merge-intermediate pre-state) of an ancestor block, surfacing during validation of a child that references it.
3. **Drift / stall** — observer's LFB stays well behind v1's after sync, indicating a gossip-path or finalization regression masked by a successful initial sync.

## Tests (1)

### test_observer_lfs_sync_against_active_shard

Single comprehensive test that:

1. Polls v1 until the DAG has at least `_MIN_PRE_ATTACH_DEPTH` (12) blocks. Without depth, the forward-horizon collapses to ~genesis and the new code paths emit early — the test would silently turn into a thinner check.
2. Starts a `_BackgroundLoad` thread on V1/V2/V3 (round-robin deploys, ~2.5 s per producer) so the chain advances during sync — observer chases a moving LFB.
3. Attaches a transient observer via `with shared_shard.add_observer() as observer:` (context-managed: removed and volume cleaned up on exit, but its post-attach errors are still captured by the autouse log scanner before cleanup).
4. Polls until observer's LFB is within `_LFB_DRIFT_TOLERANCE` (10 blocks) of v1's LFB.
5. Holds load for `_OBSERVER_LOAD_WINDOW_SEC` (20 s) post-attach so the observer ingests via gossip after the bulk sync — confirms the gossip path works post-LFS, not just the bulk LFS path.
6. Stops load and re-polls drift convergence — catches "synced then stalled" regressions where bulk sync succeeded but ongoing ingest is broken.
7. Runs cross-node consistency assertions (see Key assertions below).

The autouse fixture `check_node_logs_after_test` in `conftest.py` runs after the test body but before observer cleanup, so observer logs are scanned for forbidden patterns. A `DAGStorageMissingHash` / `RootRepositoryDivergence` / `KvStoreError` / `UnknownRootError` in the observer's logs fails the test even if every explicit assertion passes.

## Setup

- **Topology**: Session-scoped `shared_shard` (boot + 3 validators + readonly) plus a transient observer attached during the test
- **Heartbeat**: Enabled (`shared_shard` default — drives multi-parent merge construction)
- **FTT**: From `conf/rust.conf`
- **Background load**: 3 producers (V1/V2/V3), 2.5 s per-producer interval, ~1.2 deploys/sec aggregate
- **Pre-attach depth gate**: `_MIN_PRE_ATTACH_DEPTH = 12` blocks on v1 before observer attach
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
| F | `multi_parent_count > 0` in last 24 blocks | Test exercised the pre-state inclusion path for multi-parent merges; would loud-fail if shard config degenerated to single-parent |
| G | Drift remains within `_LFB_DRIFT_TOLERANCE` after settle (Step 6 poll) | Catches "synced then stalled" regressions |
| H | (Implicit, autouse) Observer logs free of `DAGStorageMissingHash` / `RootRepositoryDivergence` / `KvStoreError` / `UnknownRootError` | The whole reason this PR exists |

## Infrastructure used

- Session-scoped `shared_shard` fixture (3 validators + readonly)
- `check_node_logs_after_test` autouse fixture for forbidden-log detection (see [ARCHITECTURE.md § 7](ARCHITECTURE.md#7-log-scanning))
- `Shard.add_observer()` (context-managed transient observer)
- Local `_BackgroundLoad` class — copy of the pattern in `test_bonding_validators` so cadence/failure-tolerance knobs stay explicit at the call site
- `Node.deploy_string()`, `Node.last_finalized_block()`, `Node.get_blocks()`, `Node.get_block()`
- `assert_all_nodes_agree_on_block()`, `assert_block_finalized_on_all_nodes()`, `assert_bonds_map_consistent_across_nodes()` from assertions
- `wait_for_block_visible()`, `poll_until()`, `all_blocks_visible()`, `get_blocks_if_enough()` from polling

## Related

- [test_bonding_validators](test_bonding_validators.md) — Phase C also attaches a fresh observer, but on a 5-bonded shard after mid-test bonding (V4 + V5). That coverage depends on bonding correctness; this test isolates the LFS-sync code paths.
- [test_dag_correctness](test_dag_correctness.md) — multi-parent DAG structure on the same shared shard; complementary check that the structure the observer must sync is itself well-formed.
- [test_trim_state](test_trim_state.md) — alternate joiner path that LFS-syncs from the LFB instead of replaying genesis (different config, different code path).

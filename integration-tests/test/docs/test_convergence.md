# test_convergence

## Purpose

Verifies that the network recovers from DAG tip divergence and that fault tolerance values converge across nodes. Tests cover temporary disruptions (validator pause, slow deploy) and FT convergence for finalized blocks.

## How DAG divergence happens

In a multi-validator network with heartbeat enabled, each validator independently proposes blocks. When one validator is temporarily unable to communicate (paused, slow, network partition), the other validators continue creating blocks. This creates independent "forks" in the DAG that must be merged when the disrupted validator recovers.

The merge happens through multi-parent blocks: a validator that sees tips from multiple forks creates a block referencing all of them as parents, effectively merging the diverged branches.

## Tests (3)

### test_network_recovers_from_validator_pause

**Marker:** `@pytest.mark.allow_forbidden_patterns("DAGStorageMissingHash")` — the paused validator's DAG store legitimately reports missing hashes on resume while it backfills from peers.

Simulates a temporary network partition by pausing V1's Docker container for 30 seconds:

1. Deploy on all 3 validators to create active state before pause
2. Record baseline LFB
3. Pause V1 container (`docker pause`)
4. Wait 30 seconds (V2 and V3 create independent heartbeat blocks)
5. Unpause V1 container (`docker unpause`)
6. Deploy on all 3 validators again to stimulate convergence
7. Wait for LFB to advance by 3+ on **all nodes** (validators + readonly)

The 30-second pause forces V2 and V3 to create several independent blocks each. When V1 resumes, it receives these blocks and must create multi-parent convergence blocks to merge the forks. Active deploys on all validators (before and after pause) ensure the divergence happens under realistic conditions, not just idle heartbeat.

### test_network_converges_after_slow_deploy

Deploys a phlo-exhausting loop contract (`loop!(100000)`) on V1 with a 20M phlo limit. This blocks V1's proposer for ~25 seconds while the contract executes and phlo is exhausted. Meanwhile, V2 and V3 produce independent heartbeat blocks, causing DAG tip divergence.

1. Wait for initial LFB > 0 if needed
2. Deploy on V2 and V3 to create active state alongside the slow deploy
3. Deploy slow loop contract on V1
4. Wait for deploy inclusion (up to `finalization * 10` since execution is slow)
5. Verify the slow deploy is errored via `assert_deploy_errored` (phlo exhausted)
6. Wait for LFB to advance 3+ blocks past the deploy block on **all nodes** (validators + readonly)
7. Assert LFB spread across all nodes is ≤ 2 blocks

This reproduces two known issues:
- **#224**: phlo-exhausting deploys stall the proposing validator
- **#437**: resulting DAG tip divergence causes permanent LFB stall

**Note:** This test is deselected during normal runs (`--deselect`) because the slow deploy can trigger the #437 shard stall bug which is not yet fixed.

### test_ft_convergence

Verifies that cached fault tolerance for finalized blocks converges to 1.0 across all nodes. FT is cached in `BlockMetadata.fault_tolerance_value` at finalization time and monotonically increases as later finalization rounds propagate higher values to all finalized blocks (including orphaned branches).

1. Wait for LFB to advance past genesis
2. Pick a finalized block from V1's LFB ancestor chain
3. Assert FT >= FTT on V1 (cache correctness)
4. Poll all nodes until they all report FT = 1.0 for the block (convergence)
5. Verify FT stays at 1.0 (stability -- FT never decreases)

With all validators active and heartbeat producing blocks, the clique oracle eventually computes FT=1.0 (all stake agrees) for some LFB. The `propagate_ft_to_finalized_blocks` pass in `record_directly_finalized` then updates all finalized blocks with the higher value. This test verifies the full lifecycle: cache → monotonic increase → convergence → stability.

**Note:** Convergence depends on the clique oracle finding 3/3 agreement, which requires sufficient DAG depth. The timeout is `finalization * 6` (~270s) to allow enough blocks to accumulate.

## Setup

- **Topology**: Session-scoped `shared_shard` (3 validators + readonly)
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled (triggers independent block production during disruption)

## What it proves

- Multi-parent DAG merging works correctly after fork divergence
- A paused validator can recover and converge with the network
- Phlo-exhausting deploys are correctly marked as errored
- LFB advances on all nodes (including readonly) after convergence
- Post-recovery LFB has FT >= FTT on all nodes
- FT caching works and converges to 1.0 across all nodes
- Cached FT is stable (never decreases after convergence)

## Key assertions

- Pause test: LFB advances by 3+ on all nodes (including readonly) after unpause
- Pause test: post-recovery FT >= FTT on all nodes
- Slow deploy test: `assert_deploy_errored` on the phlo-exhausted deploy
- Slow deploy test: LFB advances 3+ past deploy block on all nodes
- Slow deploy test: post-recovery FT >= FTT on all nodes
- Slow deploy test: `max_lfb - min_lfb <= 2` across all nodes
- FT convergence: FT >= FTT on reference node (immediate)
- FT convergence: all nodes report FT = 1.0 (polled)
- FT convergence: FT stays at 1.0 after convergence (stability)

## Infrastructure used

- Session-scoped `shared_shard` fixture (3 validators + readonly)
- `node_conf` fixture for FTT value (parsed from config via pyhocon)
- `check_node_logs_after_test` autouse fixture for fatal-log detection — scans all `rnode.test.*` containers after each test against `FATAL_PATTERNS` (panics, `InvalidBondsCache`, `RootRepository` divergence, structural self-validation failures, `FATAL`); see [ARCHITECTURE.md § 7](ARCHITECTURE.md#7-log-scanning)
- `Node.pause()` / `Node.unpause()` for container pause simulation
- `Node.deploy_string()` on all validators for active state
- `Node.get_block()`, `Node.last_finalized_block()` for FT queries
- `assert_deploy_errored()` from `infra/assertions.py`
- `wait_for_deploy_included()` from `infra/polling.py`
- `poll_until()` for LFB advancement and FT convergence polling

## Related

- f1r3node issue [#224](https://github.com/F1R3FLY-io/f1r3node/issues/224) -- phlo-exhausting deploy stalls proposer
- f1r3node issue [#437](https://github.com/F1R3FLY-io/f1r3node/issues/437) -- DAG tip divergence causes permanent LFB stall
- f1r3node#462 -- FT caching fix
- [test_dag_correctness](test_dag_correctness.md) -- basic FT cache correctness (FT >= FTT on finalized blocks)
- [test_heartbeat](test_heartbeat.md) -- heartbeat-driven block production

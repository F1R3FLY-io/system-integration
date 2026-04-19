# test_dag_correctness

## Purpose

Verifies structural correctness of the multi-parent DAG produced by heartbeat-driven block creation across multiple validators. Regression test for the determinism fixes (Phases 1-3) that resolved InvalidBondsCache crashes caused by non-deterministic LCA computation and merge ordering. All checks run against all nodes (validators + readonly).

## Tests (1)

### test_dag_correctness

Single comprehensive test that deploys on all 3 validators, waits for 10+ blocks on all nodes, then verifies four properties:

1. **Multi-parent blocks exist** -- at least one block has multiple parents, proving the multi-parent merge path is exercised (not just a linear chain).
2. **FT >= FTT on finalized blocks** -- finalized blocks must have fault tolerance at or above the configured FTT threshold. The FTT value is read from `node_conf.ftt` (parsed from `conf/rust.conf`), not hardcoded.
3. **FT monotonicity on ALL nodes** -- fault tolerance values are non-increasing by block height on every node (validators + readonly). Older blocks (lower height) have higher FT because more validators have built on top of them. A violation would indicate a clique oracle bug.
4. **Cross-node post-state agreement** -- all nodes (validators + readonly) compute identical `postStateHash` for each deploy block. Uses `assert_all_nodes_agree_on_block()`. Direct regression test for Phase 1 (deterministic LCA) and Phase 3 (deterministic merge ordering).
5. **Cross-node FT agreement on finalized blocks** -- all nodes report the same FT for finalized blocks (below LFB). Only finalized blocks are compared because unfinalized blocks have unstable FT across validators.

## Setup

- **Topology**: Session-scoped `shared_shard` (3 validators + readonly)
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled (for multi-parent DAG construction)

## What it proves

- The multi-parent DAG merge path works correctly under concurrent heartbeat
- Fault tolerance computation is correct on all nodes (monotonically non-increasing by height)
- All nodes (including readonly) agree on FT values for finalized blocks
- All nodes compute identical post-states for the same blocks (determinism)
- Readonly node's view of the DAG is consistent with validators
- The Phase 1-3 determinism fixes have not regressed

## Key assertions

- `multi_parent_count > 0` -- multi-parent blocks exist
- `ft >= node_conf.ftt` for finalized blocks -- FT meets FTT threshold
- `ft_cur >= ft_next` for all successive heights on every node -- FT monotonicity
- `assert_all_nodes_agree_on_block()` for all 3 deploy blocks across all nodes -- post-state hash agreement
- `ft_v1 == ft_node` for finalized blocks across all nodes -- cross-node FT agreement
- All nodes have 10+ blocks before assertions begin

## Infrastructure used

- Session-scoped `shared_shard` fixture (3 validators + readonly)
- `check_node_logs_after_test` autouse fixture for panic detection
- `node_conf` fixture for FTT value (parsed from `conf/rust.conf` via pyhocon)
- `Node.deploy_string()`, `Node.get_blocks()`, `Node.get_block()`, `Node.find_deploy()`
- `Node.last_finalized_block()` for LFB boundary
- `assert_all_nodes_agree_on_block()` from assertions (checks all nodes including readonly)
- `poll_until()` for block accumulation, deploy inclusion, and propagation

## Related

- [test_asymmetric_bonds](test_asymmetric_bonds.md) -- similar FT and state agreement tests with unequal stakes
- [test_convergence](test_convergence.md) -- DAG divergence and recovery tests

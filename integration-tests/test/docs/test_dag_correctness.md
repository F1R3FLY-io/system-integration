# test_dag_correctness

## Purpose

Verifies structural correctness of the multi-parent DAG produced by heartbeat-driven block creation across multiple validators. Regression test for the determinism fixes (Phases 1-3) and the FT caching fix (f1r3node#462). All checks run against all nodes (validators + readonly).

## Tests (1)

### test_dag_correctness

Single comprehensive test that deploys on all 3 validators, waits for 10+ blocks on all nodes, then verifies three properties:

1. **Multi-parent blocks exist** -- at least one block has multiple parents, proving the multi-parent merge path is exercised (not just a linear chain).
2. **Cross-node post-state agreement** -- all nodes (validators + readonly) compute identical `postStateHash` for each deploy block. Uses `assert_all_nodes_agree_on_block()`. Direct regression test for Phase 1 (deterministic LCA) and Phase 2 (deterministic merge ordering).
3. **Cached FT >= FTT on finalized blocks** -- walks the LFB's actual ancestor chain (via main parent pointers) and verifies each block has cached `faultTolerance >= FTT` on the reference node. FT is cached in `BlockMetadata.fault_tolerance_value` at finalization time. Cross-node FT convergence is tested separately in `test_convergence.py::test_ft_convergence`.

Before walking the ancestor chain, the test calls `assert_block_finalized_on_all_nodes(all_nodes, lfb_hash)` to confirm the LFB itself is finalized on every node — not just the reference validator. This catches the case where a peer accepted the block at the protocol level but rejected it at validation time (e.g. `Invalid(InvalidBondsCache)`).

Note: FT monotonicity across heights is NOT tested because it is not a valid property in a multi-parent DAG. Multiple blocks at the same height can be on different branches with unrelated FT values, and cached FT for indirectly finalized ancestors (conservative lower bounds) can be lower than the directly finalized descendant's FT.

## Setup

- **Topology**: Session-scoped `shared_shard` (3 validators + readonly)
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled (for multi-parent DAG construction)

## What it proves

- The multi-parent DAG merge path works correctly under concurrent heartbeat
- FT caching works: finalized blocks in the LFB ancestor chain have cached FT >= FTT
- All nodes compute identical post-states for the same blocks (determinism)
- Readonly node's view of the DAG is consistent with validators
- The Phase 1-2 determinism fixes have not regressed
- The FT caching fix (f1r3node#462) has not regressed

## Key assertions

- `multi_parent_count > 0` -- multi-parent blocks exist
- `ft_ref >= ftt` for each block in the LFB ancestor chain -- cached FT meets threshold
- `assert_all_nodes_agree_on_block()` for all 3 deploy blocks across all nodes -- post-state hash agreement
- `assert_block_finalized_on_all_nodes()` on the LFB -- every node reports `isFinalized=True`, not just the reference validator
- All nodes have 10+ blocks before assertions begin

## Infrastructure used

- Session-scoped `shared_shard` fixture (3 validators + readonly)
- `check_node_logs_after_test` autouse fixture for panic detection
- `node_conf` fixture for FTT value (parsed from `conf/rust.conf` via pyhocon)
- `Node.deploy_string()`, `Node.get_blocks()`, `Node.get_block()`, `Node.find_deploy()`
- `Node.last_finalized_block()` for LFB
- `assert_all_nodes_agree_on_block()` from assertions (checks all nodes including readonly)
- `assert_block_finalized_on_all_nodes()` from assertions (asserts `isFinalized=True` on every node)
- `poll_until()` for block accumulation, deploy inclusion, and propagation

## Related

- [test_convergence](test_convergence.md) -- FT convergence test (`test_ft_convergence`) and DAG divergence recovery
- [test_asymmetric_bonds](test_asymmetric_bonds.md) -- similar FT and state agreement tests with unequal stakes
- f1r3node#462 -- FT caching fix

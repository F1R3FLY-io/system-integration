# test_asymmetric_bonds

## Purpose

Verifies that CBC Casper consensus operates correctly when validators have unequal stake weights. Most consensus tests use equal bonds (100/100/100). These tests use asymmetric bonds (60/20/15) to exercise:

- Fault tolerance computation with weighted validators
- Multi-parent DAG merging with unequal stake influence
- Finalization under conditions where no single validator can finalize alone
- Cross-validator state determinism despite weight asymmetry

## Setup

- **Topology**: Custom 3-validator shard + readonly observer
- **Heartbeat**: Enabled (automatic block production)
- **FTT**: 0.33 (standard BFT)
- **Scope**: Module-scoped shard (shared across all tests)

### Bond configuration

| Validator | Stake | % of Total |
|-----------|-------|------------|
| V1 | 60 | 63.2% |
| V2 | 20 | 21.1% |
| V3 | 15 | 15.8% |
| **Total** | **95** | **100%** |

### Finalization math

FT = (agreeing_weight × 2 − total_weight) / total_weight. Needs FT > 0.33 to finalize.

| Agreement set | Weight | FT | Finalizes? |
|---------------|--------|-----|------------|
| V1 alone | 60 | 0.26 | No |
| V1 + V3 | 75 | 0.58 | Yes |
| V1 + V2 | 80 | 0.68 | Yes |
| V2 + V3 | 35 | -0.26 | No |
| All three | 95 | 1.0 | Yes |

Key property: V1 is necessary for finalization but not sufficient alone. V2+V3 together cannot finalize without V1.

## Tests (4)

### test_genesis_asymmetric_bonds

Verifies the genesis block has the correct shard_id (from `node_conf`) and that the bond weights match the asymmetric configuration. Checks that all 3 validators are present in genesis bonds with the expected stakes (60/20/15).

### test_fault_tolerance_asymmetric_bonds

Deploys on all 3 validators, waits for 10+ blocks on all nodes (including readonly) via heartbeat, then verifies:

1. **Multi-parent blocks exist** — at least one block references multiple parents. This proves the multi-parent merge path is exercised (not just a linear chain where V1 dominates due to higher stake).
2. **FT monotonicity on ALL validators** — fault tolerance values are non-increasing by block height on each validator (was V1 only). Earlier blocks have higher FT than later ones because older blocks have more agreement built on top of them. A violation would indicate a clique oracle bug under asymmetric weights.

### test_finalization_asymmetric_bonds

Deploys on all 3 validators, then polls until the LFB advances on ALL nodes (including readonly). Verifies:

1. **LFB advances within timeout** — finalization works despite unequal stakes. With heartbeat producing blocks, V1's high weight ensures V1+V2 or V1+V3 agreements happen naturally.
2. **All nodes agree on finalization** — every node (including readonly) sees finalization advance, confirming consensus isn't split by weight asymmetry.
3. **FT >= FTT assertion** — finalized blocks on all nodes must have FT >= 0.33.

### test_cross_validator_state_agreement_asymmetric

Deploys on each validator, waits for inclusion, then verifies all nodes (including readonly) compute identical post-state hashes for each block. Block propagation is checked across all nodes before agreement verification. This is a regression test for determinism: the merge algorithm, LCA computation, and conflict resolution must produce the same result regardless of the validator's own weight.

Uses `assert_all_nodes_agree_on_block()` from the assertions module.

## What it proves

- Genesis block correctly reflects asymmetric bond weights and shard_id
- The safety oracle correctly weights validator stakes (not just counts)
- Multi-parent merging works under weight asymmetry
- Finalization requires sufficient weighted agreement (V1 alone can't finalize)
- Finalized blocks have FT >= FTT on all nodes including readonly
- State determinism is preserved regardless of the proposing validator's weight, including on readonly observers
- The heartbeat proposer creates blocks across all validators despite weight differences

## Infrastructure used

- `ShardConfig` with custom bond weights and `include_readonly=True`
- `Shard.create()` / `shard.destroy()` lifecycle
- `poll_until()` for block accumulation and finalization polling
- `get_blocks_if_enough()`, `try_find_deploy()`, `all_blocks_visible()` from `infra/polling.py`
- `assert_all_nodes_agree_on_block()` for cross-validator state checks
- `Node.deploy_string()`, `Node.get_blocks()`, `Node.last_finalized_block()`, `Node.get_block()`
- `node_conf` fixture for shard_id verification

## Related

- [Consensus Protocol docs](../../../services/f1r3node-rust/docs/casper/CONSENSUS_PROTOCOL.md) § Finalization (Clique Oracle)
- [Byzantine Fault Tolerance docs](../../../services/f1r3node-rust/docs/casper/BYZANTINE_FAULT_TOLERANCE.md) § Safety Oracle
- [Consensus Configuration Guide](../../../docs/consensus-configuration.md) — FTT formula and recommended values

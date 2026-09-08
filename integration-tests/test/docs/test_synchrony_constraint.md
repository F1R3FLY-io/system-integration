# test_synchrony_constraint

## Purpose

Verifies that the per-validator synchrony constraint threshold is enforced correctly by the Casper consensus engine. The synchrony constraint prevents validators from proposing blocks without sufficient network coordination — a validator must see recent blocks from a stake-weighted fraction of other validators before it can propose again.

## Setup

- **Topology**: Custom 3-validator shard, no observer
- **Heartbeat**: Disabled (manual block orchestration only)
- **FTT**: production default from `conf/rust.conf` (no override). No phase
  depends on finalization, and a negative FTT would void the
  FinalityDivergence-sentinel invariant the suite's forbidden-log check
  asserts.
- **Bond configuration**:

| Validator | Stake | Threshold | Needs from others |
|-----------|-------|-----------|-------------------|
| V1 | 100 | 0.67 | >= 134 of {V2=102, V3=98} = 200. Needs both V2+V3. |
| V2 | 102 | 0.33 | >= 65.3 of {V1=100, V3=98} = 198. V1 alone suffices. |
| V3 | 98 | 0.99 | >= 199.98 of {V1=100, V2=102} = 202. Needs both V1+V2. |

Bootstrap is not bonded (ceremony master only) and has zero weight in the synchrony calculation.

## Phases

### Phase 1: First proposals (exempt)

Every validator proposes once after genesis. The synchrony constraint is bypassed when a validator's last block is the genesis block (blockNum == 0).

### Phase 2: V2 proposes (V1 alone meets 0.33)

V2 needs >= 65.3 stake from others. V1 has 100 > 65.3 → V2 can propose.

### Phase 3: V1 proposes (V2+V3 = 200 >= 134)

V1 needs >= 134 from others. Since V1's last proposal, V2 (102) and V3 (98) have both proposed. Total = 200 >= 134 → V1 can propose.

### Phase 4: V3 proposes (V1+V2 = 202 >= 199.98)

V3 needs >= 199.98 from others. Both V1 (100) and V2 (102) have proposed since V3's last proposal. Total = 202 >= 199.98 → V3 can propose.

### Phase 5: V1 rejected (only V3=98 < 134)

V1 tries to propose again. Since V1's last proposal (Phase 3), only V3 proposed (Phase 4, stake 98). V2 hasn't proposed since before Phase 3. 98 < 134 → V1 is rejected with `SynchronyConstraintError`.

### Phase 6-7: V2 proposes, unlocking V1

V2 proposes a third block. Now V1 has V3 (98) + V2 (102) = 200 >= 134 since its last proposal → V1 can propose again.

### Phase 8: V3 proposes (V1+V2 again)

V3 has seen both V1 (Phase 7) and V2 (Phase 6) propose since its last block. Total = 202 >= 199.98 → V3 can propose.

## What it proves

- The synchrony constraint is stake-weighted, not validator-count-weighted
- A single high-stake validator can satisfy a low threshold (Phase 2)
- Multiple validators are required for high thresholds (Phase 4)
- Proposals are correctly rejected when the threshold isn't met (Phase 5)
- The constraint resets after each successful proposal (Phase 7 unlocks after Phase 5 rejection)
- Genesis-exempt behavior works (Phase 1)

## Key assertions

- Phases 1-4, 6-8: `v.propose()` succeeds (returns a block hash)
- Phase 5: `pytest.raises(F1r3flyClientException, match="(?i)synchrony|not enough")`
- Block visibility: each validator waits for other validators' blocks to propagate before asserting

## Infrastructure used

- `ShardConfig` with `per_node_cli_options` for per-validator thresholds
- `Shard.create(provider, config, timeouts)` for custom shard lifecycle
- `wait_for_block_visible()` from `infra/polling.py` for block visibility polling
- `Node.deploy_string()` + `Node.propose()` via pyf1r3fly

## Related

- [Synchrony Constraint docs](../../../services/f1r3node-rust/docs/casper/SYNC_CONSTRAINT.md)
- [Consensus Protocol docs](../../../services/f1r3node-rust/docs/casper/CONSENSUS_PROTOCOL.md) § Synchrony Recovery
- f1r3node issue [#437](https://github.com/F1R3FLY-io/f1r3node/issues/437) — network cannot self-recover from DAG tip divergence (related: synchrony constraint + recovery interaction)

# test_trim_state

## Purpose

Verifies that a new node joining an existing network correctly syncs from the Last Finalized State (LFS / "trimmed state") rather than replaying the entire blockchain from genesis. This is the primary mechanism for efficient validator onboarding in production networks: instead of downloading and replaying every historical block, a joiner fetches only the finalized state snapshot and starts from there.

## Setup

- **Topology**: Custom 2-validator shard (V1, V2) + V2 re-joins mid-chain as joiner
- **Heartbeat**: Disabled (manual block orchestration for deterministic chain)
- **FTT**: -1 (instant finalization -- every block with FT > -1 is finalized)
- **Synchrony constraint threshold**: 0 (disabled, via `_SYNC_THRESHOLD` constant)

Sync constraint and FTT values are extracted to module-level constants (`_SYNC_THRESHOLD`, `_FTT`, `_SHARD_CLI_OPTIONS`) so that both the shard config and the joiner's CLI options reference the same values.

### Bond configuration

| Validator | Stake | Purpose |
|-----------|-------|---------|
| V1 | 10,000,000 | Primary proposer, controls finalization |
| V2 | 1 | Minimal stake, needed for genesis ceremony completion |

V2's bond of 1 ensures `required_signatures` defaults to 1 (len(bonds)-1), so the bootstrap waits for V2's genesis signature before transitioning to Running. V1 controls >99.99% of stake and can finalize blocks alone.

## Phases

### Phase 1: Create finalized blocks (9 blocks)

V1 deploys 9 diverse Rholang contracts (channel sends, pattern matching, new channels) and proposes each one. With FTT=-1, every block is immediately finalized. After this phase, the LFB number is verified > 0.

The diverse contracts ensure the LFS contains non-trivial state (channel bindings, tuple space entries) that the joiner must correctly reconstruct.

### Phase 2: Add joiner, verify LFS sync

V2 (already a genesis-bonded validator) joins the shard mid-chain via `shard.add_joiner()`. The joiner uses `--fault-tolerance-threshold=-1` and `--synchrony-constraint-threshold=0` (via the same `_SYNC_THRESHOLD` / `_FTT` constants used by the shard) to match the shard's configuration. It syncs from the LFS rather than replaying all 9 blocks from genesis. The timeout is `timeouts.node_startup * 3` (semantically: startup + LFS download + state replay). After the joiner sees the latest block, its LFB is verified to be within 2 of V1's LFB, confirming finalization state transferred correctly.

### Phase 3: Continuous sync verification (4 blocks)

V1 deploys 4 more contracts and proposes. After each block, the test verifies the joiner can see the new block. This confirms the joiner isn't just catching up once -- it continues syncing new blocks in real time after the initial LFS catch-up.

### Phase 4: Full sync verification

Three checks confirm complete synchronization:

1. **Block count**: The joiner's block count is within 2 of V1's (timing tolerance).
2. **LFB agreement**: After continued operation, V1 and the joiner's last finalized block numbers must be within 2 of each other, confirming finalization is progressing on the joiner.
3. **Post-state agreement**: V1 and the joiner must report identical `postStateHash` for the most recent block. This proves the joiner computed the correct state from the LFS snapshot, not just received block headers.

## What it proves

- The LFS/trim state mechanism works: joiners sync from the last finalized state, not genesis
- Post-state is identical whether computed from full history (V1) or from LFS snapshot (joiner)
- Joiners continue syncing new blocks after the initial LFS catch-up
- The diverse contract types (channel sends, pattern matching, new channels) all reconstruct correctly from LFS
- Genesis-bonded validators can re-join mid-chain without issues

## Key assertions

- Phase 1: `lfb_number > 0` (finalization working with FTT=-1)
- Phase 2: joiner sees `latest_block_hash` within `node_startup * 3` timeout
- Phase 2: `joiner_lfb_number >= v1_lfb_number - 2` (LFB agreement after initial sync)
- Phase 3: joiner sees each post-join block within `deploy_inclusion` timeout
- Phase 4: `len(joiner_blocks) >= len(v1_blocks) - 2`
- Phase 4: `joiner_final_lfb >= v1_final_lfb - 2` (LFB agreement after continued operation)
- Phase 4: `v1_state == joiner_state` (post-state hash agreement)

## Infrastructure used

- `ShardConfig` with `global_cli_options`
- `Shard.create()` / `shard.destroy()` lifecycle
- `shard.add_joiner()` context manager for mid-test joiner attachment
- `Node.deploy_string()`, `Node.propose()`, `Node.get_block()`, `Node.get_blocks()`, `Node.last_finalized_block()`
- `wait_for_block_visible()` from `infra/polling.py` for block visibility polling

## Related

- [test_bonding_validators](test_bonding_validators.md) -- tests dynamic bonding at epoch boundaries (complementary joiner test)
- [Consensus Protocol docs](../../../services/f1r3node-rust/docs/casper/CONSENSUS_PROTOCOL.md) -- Finalization (Clique Oracle)
- [Shard Degradation context](../../../docs/shard-degradation-context.md) -- known finalizer timeout issues under sustained load

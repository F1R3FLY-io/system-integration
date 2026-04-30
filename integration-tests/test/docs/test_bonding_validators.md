# test_bonding_validators

## Purpose

End-to-end validator-bonding lifecycle on the session-shared shard. Verifies that a new validator can dynamically bond via the PoS contract, become an active block proposer at the next epoch boundary, and that subsequent bonds (V5 after V4) succeed under the multi-proposer bonds-cache composition path. The second-bond test exists specifically to catch the failure mode where the second bond never finalizes — the symptom Stacy reported in v0.4.13 that surfaces as `InvalidBondsCache` on the bond block.

## Setup

Both tests run on the session-scoped `shared_shard` fixture: bootstrap + V1, V2, V3 + readonly observer. Production-config inherited from `conf/rust.conf` (no per-test config builder).

- **Topology**: 3 genesis validators (V1, V2, V3, all stake=100) + readonly observer + dynamically-added joiner (V4, then V5)
- **Heartbeat**: Enabled (production semantics, real propagation timing)
- **FTT**: From `rust.conf` (`fault-tolerance-threshold = 0.1`)
- **include_readonly**: True
- **Epoch length**: 4 blocks (`epoch-length = 4` in `conf/rust.conf` — keeps the bonding-test cadence tight)
- **Quarantine length**: 10
- **Number of active validators**: 10000

### Genesis wallet seeding

V4's and V5's vaults are seeded at genesis with `50_000_000_000_000_000` tokens each via the `shared_shard` fixture's `extra_wallets` so they have phlo to cover bond + transaction costs. Vault addresses are derived from the public key via `PrivateKey.get_public_key().get_vault_address()`.

### Bond configuration

| Validator | Stake | When bonded | Test |
|---|---|---|---|
| V1, V2, V3 | 100 each | Genesis | (already bonded) |
| V4 | 10,000,000 | First bond | `test_bonding_validators` |
| V5 | 10,000,000 | Second bond | `test_double_bond_succession` |

### Test ordering

Pytest collects tests in source order. `test_double_bond_succession` includes a precondition assertion at the top that V4 must already be bonded — if you reorder or skip `test_bonding_validators`, the second test fails fast with a clear "test order changed?" message.

## Tests

### `test_bonding_validators`

Bonds V4 via V1 (genesis validator V1 deploys the bond contract). Runs the full 8-phase `_bond_lifecycle` helper. After this test, V4 is permanently in the on-chain bonds map; the joiner subprocess/container is removed at exit.

### `test_double_bond_succession`

Bonds V5 on a shard that already has V4 bonded (precondition asserted at top). Bond is deployed via **V2** (different proposer than V4's bond) to exercise the multi-proposer path through the bonds_cache / justification-set composition. **This test reproduces Stacy's bonding bug** when run against a binary that contains the second-bond regression — symptom is `InvalidBondsCache` on the V5 bond block, then a cascade of invalid blocks as the bonds-cache divergence between V1's replay path and V2's proposer path propagates.

## Phases (`_bond_lifecycle`)

Both tests share the same 8-phase helper.

### Phase 1: Pre-bond LFB inspection

Read the LFB on V1, build the current bonds map. Assert the joiner is NOT in the current bonds (precondition for a meaningful bonding test). Logs the LFB block number and existing bond count.

### Phase 2: Joiner attaches as a follower

The joiner is added to the shard via `provider.add_node(...)` — same path used in `Shard.add_joiner()`. Joiner starts as a non-bonded follower, syncs the existing chain, and reaches Running.

### Phase 3: Verify joiner cannot propose pre-bond

Joiner deploys a small Rholang term and attempts to propose. Must fail because the joiner is not in the active validator set. Logged as "Joiner correctly rejected on propose pre-bond".

### Phase 4: Bond block finalizes cross-node

Proposer (V1 for first bond, V2 for second bond) deploys `bond.rho` with the joiner's private key and stake amount. The bond deploy lands in a block (typically within a few rounds), and the framework waits for that block's height to be finalized via LFB advancement, then asserts the **specific block hash** is finalized on **all five nodes** (V1, V2, V3, joiner, readonly) — this catches the InvalidBondsCache failure mode where the proposer's block is finalized locally but a peer rejected it at validation time. Polls per-node `isFinalized` to ride out the FT-propagation lag (see `assert_block_finalized_on_all_nodes` in `infra/assertions.py`).

After finalization, reads the bonds map from the bond block and asserts:
- Joiner appears in bonds with the expected stake
- Total bond count increased by exactly 1

### Phase 5: Wait for epoch boundary

LFB must advance past the next epoch boundary (`epoch-length = 4`). Bonded validators are not immediately active — they activate at the next epoch boundary. Logged as "LFB advanced past epoch boundary (#N)".

### Phase 6: Joiner produces its first block

Joiner deploys + proposes (this time successfully — it's now in the active set). The framework polls `_joiner_proposed` for any block where `sender == joiner_pubkey`, then waits for that block to finalize, then asserts the specific block hash is finalized on all 5 nodes. Logged as "Joiner proposed block #N (hash); finalized on all nodes".

### Phase 7: V1 produces a block justifying the joiner

The framework polls until V1 produces a block whose justification set includes the joiner's latest block. Then waits for finalization, then asserts the specific V1-block hash is finalized on all 5 nodes. This phase exercises peak DAG contention — V1, V2, V3, joiner all racing at the same height — and would race a "test polls by block_number, asserts by block_hash" mismatch without the polling fix in `assert_block_finalized_on_all_nodes`.

### Phase 8: Post-bond network liveness

For each of V1, V2, V3, joiner: deploy a `liveness-{validator}` term, wait for inclusion, wait for finalization, then `wait_for_block_visible_on_all_nodes` (rides out the gRPC `received but not added yet` window — see TODO §2.9), then `assert_block_finalized_on_all_nodes` on all 5 nodes. Confirms the network is fully healthy after the joiner integrates.

## What the tests prove

- PoS `bond.rho` correctly records a new validator's stake in the on-chain bonds map
- Bond count increases by exactly 1 after each successful bond
- Bonded validators activate at the next epoch boundary (not immediately)
- The bond block finalizes consistently on every node — same hash, same `isFinalized=True`, same FT
- Joiner's first block produces and finalizes cross-node post-activation
- V1 (genesis validator) produces blocks that justify the joiner's blocks (justification set wiring)
- All 4 active validators (incl. joiner) produce blocks that finalize cross-node post-bond (network liveness)
- Sequential bonds (V4 then V5, via different proposers) compose correctly through the bonds-cache layer

## Key assertions

- Pre-bond: `joiner.public_hex not in bonds`
- Pre-bond propose: raises `F1r3flyClientException`
- Bond block: `bonds[joiner.public_hex] == stake`, `len(bonds) == expected_bonds_after`
- Bond block: `isFinalized=True` on **every** node (V1, V2, V3, joiner, readonly)
- Joiner first block: `isFinalized=True` on every node
- V1 justifies-joiner block: `isFinalized=True` on every node
- Post-bond liveness: each validator's deploy block is visible AND finalized on every node

## Forbidden-pattern coverage

The autouse `check_node_logs_after_test` fixture scans every node's logs after the test for forbidden patterns defined in `infra/log_events.py`:
- `InvalidBondsCache`
- `RecordingInvalidBlock`
- `DAGStorageMissingHash`

These tests are **not** opted out via `@pytest.mark.allow_forbidden_patterns` — the bond lifecycle is expected to be clean. If the V5 second-bond regression is present, the scanner will fire on `Bonds in proof of stake contract do not match block's bond cache` and `Recording invalid block ... for InvalidBondsCache`.

## Infrastructure used

- `shared_shard` (session-scoped fixture) — 3-validator shard + readonly with V4/V5 vaults pre-seeded
- `provider.add_node(...)` — attaches a non-bonded follower mid-session
- `Node.deploy_rho_file("bond.rho", ...)` — bond contract deployment with substitutions
- `Node.deploy_string()` / `Node.propose()` / `Node.get_block()` / `Node.last_finalized_block()`
- `wait_for_deploy_included()`, `wait_for_finalized()`, `wait_for_block_visible_on_all_nodes()` — all from `infra/polling.py`
- `assert_block_finalized_on_all_nodes(...)` with `timeout=timeouts.finalization` — rides out FT propagation lag
- `poll_until()` for `_joiner_proposed` / `_v1_justifies_joiner` predicates

## Related

- [PoS bond contract](../../resources/wallets/bond.rho) — the Rholang contract used for bonding
- [Consensus Protocol docs](../../../services/f1r3node-rust/docs/casper/CONSENSUS_PROTOCOL.md) — Epoch Boundaries
- [test_asymmetric_bonds](test_asymmetric_bonds.md) — tests consensus with unequal stakes (complementary)
- [test_trim_state](test_trim_state.md) — tests joiner synchronization via LFS

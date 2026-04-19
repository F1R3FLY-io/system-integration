# test_bonding_validators

## Purpose

Verifies that a new validator can dynamically bond to a running network via the PoS (Proof of Stake) contract and become an active block proposer at the next epoch boundary. This is the core validator onboarding flow: a new node joins the network, submits a bond transaction, waits for the epoch to change, and then participates in consensus.

## Setup

- **Topology**: Custom 2-validator shard (V1, V2) + 1 dynamically added joiner (V4)
- **Heartbeat**: Disabled (manual block orchestration for deterministic block numbering)
- **FTT**: -1 (instant finalization)
- **Epoch length**: 4 blocks (epoch changes at blocks 4, 8, 12, ...)
- **Quarantine length**: 20
- **Synchrony constraint threshold**: 0 (disabled)

### Bond configuration

| Validator | Stake | Role |
|-----------|-------|------|
| V1 | 10,000,000 | Genesis validator |
| V2 | 10,000,000 | Genesis validator |
| V4 | 10,000,000 | Bonds at block 2, active at block 5 |

### Genesis wallet seeding

V4's vault is seeded at genesis with 50,000,000,000,000,000 tokens (same as other genesis validators) so it has sufficient funds to cover the bond amount plus phlo costs. The vault address is derived from V4's public key via `PrivateKey.get_public_key().get_vault_address()`.

## Phases

### Phase 1: Block 1 — Initial deploy, verify joiner not bonded

V1 deploys and proposes. The bonds map in block 1 is checked to confirm V4's public key is not present. This establishes the baseline: the joiner has not bonded yet.

### Phase 2: Add joiner to network

V4 is added to the shard via `shard.add_joiner()`. The joiner node starts, syncs with the bootstrap node, and waits until block 1 is visible. The joiner uses the same epoch/quarantine/synchrony CLI options as the genesis validators plus `--heartbeat-disabled`.

### Phase 3: Verify joiner cannot propose pre-bond

V4 deploys code and attempts to propose. This fails with `F1r3flyClientException` because V4 is not in the active validator set — it hasn't bonded yet.

### Phase 4: Block 2 — Bond transaction

V1 deploys the `bond.rho` contract with V4's private key and the bond amount (10,000,000). The contract invokes `PoS.bond()` which records V4's stake in the bonds map. After V1 proposes, the bonds map is verified to contain V4 with the correct stake.

### Phase 5: Block 3 — Filler deploy, verify joiner still inactive

V1 deploys and proposes block 3. V4 syncs this block and attempts to propose again. This still fails — the bond is recorded but the epoch hasn't changed yet (block 3 < epoch boundary at block 4). Bonded validators become active at the *next* epoch boundary, not immediately.

### Phase 6: Block 4 — Epoch boundary

V1 deploys and proposes block 4, which triggers the epoch change. V4 syncs this block.

### Phase 7: Block 5 — V2 filler to advance DAG

V2 deploys and proposes block 5. This is necessary because V4's earlier rejected propose attempts left a "previous block" context. If V4 proposed immediately after the epoch change, its parent set would reference the rejected context and the self-validation check would reject with `InvalidParents` ("validator has not made progress"). Having V2 propose first gives V4 a fresh parent that breaks the no-progress condition.

### Phase 8: Block 6 — Joiner proposes

V4 deploys and proposes successfully. The block is verified visible on V1, confirming the joiner is now an active participant in consensus.

## What it proves

- The PoS bond contract correctly records a new validator's stake in the bonds map
- Bonded validators are not immediately active — they must wait for the epoch boundary
- The epoch-length parameter controls when bonded validators become active
- A joiner node can sync with the network, bond, and become a proposer
- The DAG parent validation works correctly across the bonding/activation transition
- `shard.add_joiner()` correctly manages joiner node lifecycle (start, sync, cleanup)

## Key assertions

- Block 1: V4's public key not in bonds map
- Pre-bond propose: `pytest.raises(F1r3flyClientException)`
- Block 2: `bonds_map[V4.public_hex] == 10,000,000`
- Pre-epoch propose: `pytest.raises(F1r3flyClientException)`
- Block 6: `joiner.propose()` succeeds (returns block hash)
- Block 6 visible on V1: `wait_for_block_visible(v1, b6)` succeeds

## Infrastructure used

- `ShardConfig` with `global_cli_options`, `extra_wallets`
- `Shard.create()` / `shard.destroy()` lifecycle
- `shard.add_joiner()` context manager for mid-test joiner attachment
- `Node.deploy_rho_file()` for deploying bond.rho with substitutions
- `Node.deploy_string()`, `Node.propose()`, `Node.get_block()`
- `poll_until()` for block visibility polling

## Related

- [PoS bond contract](../../resources/wallets/bond.rho) — the Rholang contract used for bonding
- [Consensus Protocol docs](../../../services/f1r3node-rust/docs/casper/CONSENSUS_PROTOCOL.md) -- Epoch Boundaries
- [test_asymmetric_bonds](test_asymmetric_bonds.md) — tests consensus with unequal stakes (complementary)
- [test_trim_state](test_trim_state.md) — tests joiner synchronization via LFS (next migration target)

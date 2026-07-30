# test_bonding_validators

## Purpose

End-to-end validator-bonding lifecycle on the session-shared shard. Verifies that a new validator can dynamically bond via the PoS contract, become an active block proposer at the next epoch boundary, and that subsequent bonds (V5 after V4) succeed under the multi-proposer bonds-cache composition path. Also exercises the production scenario for the LFS forward-horizon rspace history sync code path: a fresh observer LFS-syncing against a busy 5-bonded shard.

The test covers two distinct failure modes:
1. **`InvalidBondsCache`** — the second bond (V5) is computed differently on the proposer vs. its peers. Surfaced as a `Recording invalid block` cascade following the bond block.
2. **`UnknownRootError` on sibling-of-LFB blocks** — joiners post-LFS only have the LFB's rspace state; sibling blocks reference parent rspace state the joiner doesn't have. Surfaced as a validation cascade on a busy shard. Closed by the forward-horizon sync landed in `feat/d-thin-mutex-state` (see `services/f1r3node-rust/casper/src/rust/engine/lfs_horizon_requester.rs`).

## Setup

The test runs on the session-scoped `shared_shard` fixture: bootstrap + V1, V2, V3 + readonly observer. Production-config inherited from `conf/rust.conf` (no per-test config builder).

- **Topology**: 3 genesis validators (V1, V2, V3, all stake=100) + readonly observer + dynamically-attached joiners (V4 in Phase A, V5 in Phase B) + dynamically-attached observer (Phase C)
- **Heartbeat**: Enabled (production semantics, real propagation timing)
- **FTT**: From `rust.conf` (`fault-tolerance-threshold = 0.1`)
- **include_readonly**: True
- **Epoch length**: 4 blocks (`epoch-length = 4` in `conf/rust.conf` — keeps the bonding-test cadence tight)
- **Quarantine length**: 10
- **Number of active validators**: 10000

### Genesis wallet seeding

V4's and V5's vaults are seeded at genesis with `50_000_000_000_000_000` tokens each via the `shared_shard` fixture's `extra_wallets` so they have phlo to cover bond + transaction costs. Vault addresses are derived from the public key via `PrivateKey.get_public_key().get_vault_address()`.

### Bond configuration

| Validator | Stake | When bonded | Phase |
|---|---|---|---|
| V1, V2, V3 | 100 each | Genesis | (already bonded) |
| V4 | 100 | First bond | Phase A |
| V5 | 100 | Second bond | Phase B |

### Persistent attachments

- V4 and V5 attach via `Shard.attach_joiner(...)` — they remain part of the shard for the rest of the session so the on-chain bonds map and live node count stay aligned for downstream `@shared` tests (which see 5 bonded / 5 alive).
- The Phase C observer attaches via `Shard.attach_observer()` — also persistent, named `observer1` by the provider, addressable as `shared_shard.node("observer1")`.

### Why one test, not three

Phase B's preconditions (V4 already bonded; second-bond-after-first state) only exist as a consequence of Phase A's success, and Phase C exercises the steady state after V4+V5 are bonded — splitting them would hide cascade failures in earlier phases. All three phases run as a single test.

## Test

### `test_bonding_validators`

Three-phase test wrapped in a `try/finally` that drives a background-load thread throughout Phases A and B.

**Background load** — A daemon thread (`_BackgroundLoad`) sends round-robin deploys to V1/V2/V3 every ~0.5 s. This drives concurrent block production from the genesis validators while the joiners are syncing, so the joiners' LFS forward-horizon contains a non-trivial number of side-branch blocks (deeper rspace history horizon = the new code paths fire on real input). Without this, the shard is quiet during bonding and the new sync code is a no-op. Deploy errors from the bg thread are logged at WARN, never fail the test.

**Phase A** — Bonds V4 via V1 (genesis validator V1 deploys the bond contract). Runs the full 8-sub-phase `_bond_lifecycle` helper. After this phase, V4 is permanently in the on-chain bonds map.

**Phase B** — Bonds V5 on the now-4-bonded shard via **V2** (different proposer than V4's bond) to exercise the multi-proposer path through the bonds_cache / justification-set composition. The failure mode this phase covers is `InvalidBondsCache` on the V5 bond block followed by a cascade of invalid blocks as the bonds-cache divergence between V1's replay path and V2's proposer path propagates.

A sanity assertion between Phase A and Phase B confirms V4 is in the on-chain bonds map, so a Phase A regression that "passes" but doesn't actually bond V4 surfaces here loudly.

**Phase C** — Stops the background load (so the observer's sync reaches a stable conclusion rather than chasing a moving tip), then attaches a fresh readonly observer via `Shard.attach_observer()`. Asserts:
- The observer LFS-syncs to the current LFB (the 5-bonded tip).
- The observer's view of that block is `isFinalized=True`.
- The observer's bonds map for that block matches every other node's, exactly (key + stake).
- After a brief settle (`timeouts.finalization`), the observer's LFB is within one block of V1's — catches the case where the observer LFS-synced to an old LFB but stalled.

This is the production scenario for forward-horizon rspace history sync — every fresh node coming up against a live shard exercises this code path. V4/V5 don't cover it because they joined when the shard was small and quiet.

## Sub-phases (`_bond_lifecycle`)

Both Phase A and Phase B run the same 8-sub-phase helper described below.

### Sub-phase 1: Pre-bond LFB inspection

Read the LFB on V1, build the current bonds map. Assert the joiner is NOT in the current bonds (precondition for a meaningful bonding test). Logs the LFB block number and existing bond count. The pre-bond bonds map is also captured for use in sub-phase 4's cross-node assertion.

### Sub-phase 2: Joiner attaches as a follower

The joiner is added to the shard via `Shard.attach_joiner(identity)` — persistent, included in `_handles` and `_nodes`. Joiner starts as a non-bonded follower, syncs the existing chain, and reaches Running. The joiner deploys a small term and attempts to propose; this must fail (the joiner is not in the active set yet).

### Sub-phase 3: Verify joiner cannot propose pre-bond

Joiner deploys a small Rholang term and attempts to propose. Must fail because the joiner is not in the active validator set. Logged as "Joiner correctly rejected on propose pre-bond".

### Sub-phase 4: Bond block finalizes cross-node + cross-node bonds-map check

Proposer (V1 for first bond, V2 for second bond) deploys `bond.rho` with the joiner's private key and stake amount. The bond deploy lands in a block, and the framework waits for that block's height to be finalized via LFB advancement, then asserts the **specific block hash** is finalized on **all five nodes** (V1, V2, V3, joiner, readonly).

After finalization, the framework asserts:
- `bonds_post[joiner.public_hex] == _BOND_AMOUNT` on the proposer node
- `len(bonds_post) == expected_bonds_after`
- **Cross-node bonds-map consistency** via `assert_bonds_map_consistent_across_nodes(...)`. Every node's view of the bond block must carry the **exact same** bonds map (same keys + same stakes). The original `InvalidBondsCache` bug was a per-node divergence — the bond block validated on the proposer but a peer computed a different bonds map. This assertion is its direct regression detector.

### Sub-phase 5: Wait for epoch boundary

LFB must advance past the next epoch boundary (`epoch-length = 4`). Bonded validators are not immediately active — they activate at the next epoch boundary. Logged as "LFB advanced past epoch boundary (#N)".

### Sub-phase 6: Joiner produces its first block

Joiner deploys + proposes (this time successfully — it's now in the active set). The framework polls `_joiner_proposed` for any block where `sender == joiner_pubkey`, then waits for that block to finalize, then asserts the specific block hash is finalized on all 5 nodes. Logged as "Joiner proposed block #N (hash); finalized on all nodes".

### Sub-phase 7: V1 produces a block justifying the joiner

The framework polls until V1 produces a block whose justification set includes the joiner's latest block. Then waits for finalization, then asserts the specific V1-block hash is finalized on all 5 nodes. This sub-phase exercises peak DAG contention — V1, V2, V3, joiner all racing at the same height — and would race a "test polls by block_number, asserts by block_hash" mismatch without the polling fix in `assert_block_finalized_on_all_nodes`.

### Sub-phase 8: Post-bond network liveness

For each of V1, V2, V3, joiner: deploy a `liveness-{validator}` term, wait for inclusion, wait for finalization, then `wait_for_block_visible_on_all_nodes` (rides out the gRPC `received but not added yet` window), then `assert_block_finalized_on_all_nodes` on all 5 nodes. Confirms the network is fully healthy after the joiner integrates.

## What the tests prove

- PoS `bond.rho` correctly records a new validator's stake in the on-chain bonds map
- Bond count increases by exactly 1 after each successful bond
- Bonded validators activate at the next epoch boundary (not immediately)
- The bond block finalizes consistently on every node — same hash, same `isFinalized=True`, same bonds map (cross-node bonds-map equality is the direct `InvalidBondsCache` regression detector)
- Joiner's first block produces and finalizes cross-node post-activation
- V1 (genesis validator) produces blocks that justify the joiner's blocks (justification set wiring)
- All 4 active validators (incl. joiner) produce blocks that finalize cross-node post-bond (network liveness)
- Sequential bonds (V4 then V5, via different proposers) compose correctly through the bonds-cache layer
- A fresh observer can LFS-sync against the live 5-bonded shard with a deep forward-horizon (concurrent producer load throughout Phases A/B), reach the current LFB, and stay in sync with V1 (production scenario for forward-horizon rspace history sync)

## Key assertions

- Pre-bond: `joiner.public_hex not in bonds`
- Pre-bond propose: raises `F1r3flyClientException`
- Bond block: `bonds[joiner.public_hex] == stake`, `len(bonds) == expected_bonds_after`
- Bond block: `isFinalized=True` on **every** node (V1, V2, V3, joiner, readonly)
- Bond block: bonds map identical on **every** node — `assert_bonds_map_consistent_across_nodes`
- Joiner first block: `isFinalized=True` on every node
- V1 justifies-joiner block: `isFinalized=True` on every node
- Post-bond liveness: each validator's deploy block is visible AND finalized on every node
- Phase C observer: visible at current LFB, finalized, bonds map matches V1's, LFB lag ≤ 1 block after settle

## Forbidden-pattern coverage

The autouse `check_node_logs_after_test` fixture scans every node's logs after the test for forbidden patterns defined in `infra/log_events.py`:
- `InvalidBondsCache`
- `RecordingInvalidBlock`
- `DAGStorageMissingHash`

These tests are **not** opted out via `@pytest.mark.allow_forbidden_patterns` — the bond lifecycle is expected to be clean. If the V5 second-bond regression is present, the scanner will fire on `Bonds in proof of stake contract do not match block's bond cache` and `Recording invalid block ... for InvalidBondsCache`. If the forward-horizon sync gap is reintroduced, the bg-load + Phase C combination will surface `UnknownRootError` cascades that the FATAL pattern matcher catches.

## Infrastructure used

- `shared_shard` (session-scoped fixture) — 3-validator shard + readonly with V4/V5 vaults pre-seeded
- `Shard.attach_joiner(...)` — persistent joiner with validator identity (Phases A, B)
- `Shard.attach_observer(...)` — persistent readonly observer, no identity, `--heartbeat-disabled` (Phase C)
- `_BackgroundLoad` (test-local class) — daemon thread issuing round-robin deploys at `_BG_LOAD_INTERVAL`
- `Node.deploy_rho_file("bond.rho", ...)` — bond contract deployment with substitutions
- `Node.deploy_string()` / `Node.propose()` / `Node.get_block()` / `Node.last_finalized_block()`
- `wait_for_block_visible()`, `wait_for_deploy_included()`, `wait_for_finalized()`, `wait_for_block_visible_on_all_nodes()` — all from `infra/polling.py`
- `assert_block_finalized_on_all_nodes(...)` with `timeout=timeouts.finalization * 3` — rides out FT propagation lag
- `assert_bonds_map_consistent_across_nodes(...)` — cross-node bonds-map regression detector
- `poll_until()` for `_joiner_proposed` / `_v1_justifies_joiner` predicates

## Related

- [PoS bond contract](../../resources/wallets/bond.rho) — the Rholang contract used for bonding
- [Consensus Protocol docs](../../../services/f1r3node-rust/docs/casper/CONSENSUS_PROTOCOL.md) — Epoch Boundaries
- [test_asymmetric_bonds](test_asymmetric_bonds.md) — tests consensus with unequal stakes (complementary)
- [test_trim_state](test_trim_state.md) — tests joiner synchronization via LFS (complementary; covers the explicit trim-state path)

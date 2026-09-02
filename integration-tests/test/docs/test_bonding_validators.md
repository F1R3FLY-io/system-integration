# test_bonding_validators

## Purpose

End-to-end validator-bonding lifecycle on a dedicated shard. Verifies that a
new validator can dynamically bond via the PoS contract, become an active
block proposer at the next epoch boundary, and that subsequent bonds (V5
after V4) succeed under the multi-proposer bonds-cache composition path.
Also exercises the production scenario for the LFS forward-horizon rspace
history sync code path: a fresh observer LFS-syncing against a busy
5-bonded shard.

The test covers two distinct failure modes:
1. **`InvalidBondsCache`** — the second bond (V5) is computed differently on
   the proposer vs. its peers. Surfaced as a `Recording invalid block`
   cascade following the bond block.
2. **`UnknownRootError` on sibling-of-LFB blocks** — joiners post-LFS only
   have the LFB's rspace state; sibling blocks reference parent rspace state
   the joiner doesn't have. Surfaced as a validation cascade on a busy
   shard. Closed by the forward-horizon sync (see
   `services/f1r3node-rust/casper/src/rust/engine/lfs_horizon_requester.rs`).

## Setup

The test runs on its own module-scoped `bonding_shard` fixture — NOT the
session `shared_shard`: bonding permanently changes the validator set, so
the shard is dedicated and destroyed at fixture teardown, before downstream
shared tests run.

- **Topology**: 3 genesis validators (V1, V2, V3, all stake=100) + readonly
  observer + dynamically-attached joiners (V4 in Phase A, V5 in Phase B) +
  a dynamically-attached observer (Phase C)
- **Heartbeat**: Enabled (production semantics, no manual propose)
- **FTT**: From `conf/rust.conf`
- **Epoch length**: 4 — passed explicitly by the fixture via
  `--epoch-length` (deliberately not inherited from `conf/rust.conf`, which
  carries a long epoch for suites that never bond, so the two cannot drift)
- **Quarantine length**: 10, also passed explicitly

### Genesis wallet seeding

V4's and V5's vaults are seeded at genesis with `50_000_000_000_000_000`
tokens each via the fixture's `extra_wallets` so they have phlo to cover
bond + transaction costs. Vault addresses are derived from the public key
via `PrivateKey.get_public_key().get_vault_address()`.

### Bond configuration

| Validator | Stake | When bonded | Phase |
|---|---|---|---|
| V1, V2, V3 | 100 each | Genesis | (already bonded) |
| V4 | 100 | First bond | Phase A |
| V5 | 100 | Second bond | Phase B |

### Why one test, not three

Phase B's preconditions (V4 already bonded; second-bond-after-first state)
only exist as a consequence of Phase A's success, and Phase C exercises the
steady state after V4+V5 are bonded — splitting them would hide cascade
failures in earlier phases. All three phases run as a single test.

## Test

### `test_bonding_validators`

Three-phase test wrapped in a `try/finally` around a background-load
thread.

**Background load** — A daemon thread (`_BackgroundLoad`) sends round-robin
deploys to V1/V2/V3 every 2.0s per producer (~1.5 deploys/sec shard-wide).
This drives concurrent block production while the joiners sync, so the
joiners' LFS forward-horizon contains a non-trivial number of side-branch
blocks. The load runs through Phase A sub-phases 1-5 only and is stopped
inside `_bond_lifecycle` once V4 activates — continued load past that point
creates fork-choice contention that starves FT accumulation for the
joiner's first blocks. Deploy errors from the bg thread are logged at WARN,
never fail the test.

**Phase A** — Bonds V4 via V1. Runs the full 8-sub-phase `_bond_lifecycle`
helper with the bg-load handle (so the helper can stop it after
activation). After this phase V4 is permanently in the on-chain bonds map,
and every bg-load deploy is required to have finalized (the
orphan-recovery regression detector).

**Phase B** — Bonds V5 on the now-4-bonded shard via **V2** (different
proposer than V4's bond) to exercise the multi-proposer path through the
bonds_cache / justification-set composition. Runs against the deeper DAG
Phase A's stress window built up, which the state retains. A sanity
assertion between the phases confirms V4 is in the on-chain bonds map.

**Phase C** — Attaches a fresh readonly observer via
`Shard.attach_observer()` and requires it to LFS-sync against the 5-bonded
shard:
- The observer's LFB must come within 10 blocks of V1's (poll-based;
  observer and V1 finalize independently once the observer is Running, and
  drift a few blocks apart at steady state).
- The observer's LFB block must be visible and finalized on V1 too
  (cross-node propagation, not just LFB advancement), with an exactly
  matching 5-entry bonds map.
- The drift must remain within tolerance through a settle window
  (poll-based, catching the "synced then stalled" case while tolerating
  transient spikes).

This is the production scenario for forward-horizon rspace history sync —
every fresh node coming up against a live shard exercises this path. V4/V5
don't cover it because they joined when the shard was small and quiet.

## Sub-phases (`_bond_lifecycle`)

Both Phase A and Phase B run the same 8-sub-phase helper.

### Sub-phase 1: Pre-bond LFB inspection

Read the LFB, build the current bonds map, assert the joiner is NOT in it.
The pre-bond map is captured for sub-phase 4's assertion. The joiner then
attaches via `Shard.attach_joiner(...)` and must LFS-sync to the current
LFB within `timeouts.finalization * 3` — by Phase B the LFB is 50+ blocks
deep with side-branch history, so the attach window legitimately exceeds
the plain node-startup budget.

### Sub-phase 2: Joiner cannot propose pre-bond

Joiner deploys a small Rholang term and attempts to propose. Must raise
`F1r3flyClientException` — the joiner is not in the active set yet.

### Sub-phase 3: Bond deploy

The proposer (V1 for the first bond, V2 for the second) deploys `bond.rho`
signed with the joiner's private key. Inclusion gets a generous budget
(`deploy_inclusion * 3`): under heartbeat-only config the latency depends
on the proposer's next heartbeat round.

### Sub-phase 4: Bond deploy finalizes cross-node + bonds-map check

**Deploy-centric, not block-centric**: under bg-load contention the FIRST
containing block can lose fork choice and be orphaned while the bond deploy
is re-homed into a finalized descendant (observed live). The helper waits
for the DEPLOY to finalize (`wait_for_deploy_finalized`), anchors every
downstream assertion to the resolver's canonical inclusion block, and
asserts that block finalized on all five nodes.

The bond block's active bonds map is then checked against independently
computed candidates: off an epoch boundary it must equal the pre-bond set
(the joiner is sealed but not active); ON a boundary either side of the
closeBlock transition is accepted (heartbeat + multi-parent processing can
validly leave the header on pre-activation weights). Activation is never
waived — sub-phase 5 still requires the exact post-bond map later.
`assert_bonds_map_consistent_across_nodes` then pins every node to the same
map — the direct `InvalidBondsCache` regression detector. Finally the
ledger itself (`pos.get_bonds()` via the readonly node) must report the
joiner at full stake with the expected total bond count.

### Sub-phase 5: Epoch boundary activation

Polls until a finalized LFB at or past `bond_block + epoch_length` carries
exactly the post-bond active map, then asserts that block finalized and its
bonds map consistent on all five nodes. Phase A stops the background load
here.

### Sub-phase 6: Joiner produces a block as active proposer

The joiner deploys, and heartbeat produces its block. **Orphan-aware**: an
individual joiner block can legitimately lose a merge under contention, so
the helper selects a joiner-authored block that already reports
`isFinalized` on the joiner, then holds the cross-node assertion to that
block. On poll timeout, `_dump_block_search_diagnostic` logs what the
queried nodes' recent blocks actually show (heights, senders,
justifications) so the failure is diagnosable.

### Sub-phase 7: V1 justifies the joiner

Same orphan-safety rule: polls for a finalized V1-authored block whose
justifications cite the joiner, asserts it finalized on all five nodes, and
dumps the justification diagnostic on timeout (querying both V1 and a peer
so a node-local API problem is distinguishable from a real justification
gap).

### Sub-phase 8: Post-bond liveness

For each of V1, V2, V3, joiner: deploy, wait for inclusion, then
`assert_all_deploys_finalized_on_all_nodes` — deploy-centric for the same
re-homing reason as sub-phase 4. On the Phase A call, every background-load
deploy must also have finalized (`assert_all_deploys_finalized_on_all_nodes`
over `bg_load.deploy_ids()`): a deploy that loses fork choice and is never
re-included within the parent horizon is silently dropped user work.

## What the tests prove

- PoS `bond.rho` correctly records a new validator's stake in the on-chain
  bonds map
- Bonded validators activate exactly at the next epoch boundary (not
  immediately), and the activated map finalizes identically on every node
- The bond block finalizes consistently on every node — same hash, same
  bonds map (the direct `InvalidBondsCache` regression detector)
- The joiner proposes post-activation and its block finalizes cross-node
- Genesis validators justify the joiner's blocks (justification wiring)
- All active validators (incl. joiner) produce finalizing blocks post-bond
- Sequential bonds (V4 then V5, via different proposers) compose correctly
  through the bonds-cache layer
- Deploys are never silently dropped under fork-choice contention (bg-load
  finalization sweep)
- A fresh observer LFS-syncs against the live 5-bonded shard with a deep
  forward-horizon, reaches the live LFB window, and stays in sync

## Forbidden-pattern coverage

The autouse `check_node_logs_after_test` fixture scans every node's logs
after the test against `infra/log_events.py`. This test carries no
`allow_forbidden_patterns` opt-out — the bond lifecycle is expected to be
clean. A V5 second-bond regression fires `InvalidBondsCache` /
`BondsCacheMismatch` / `RecordingInvalidBlock`; a reintroduced
forward-horizon gap surfaces `UnknownRootError` cascades under the bg-load
+ joiner-attach combination.

## Infrastructure used

- `bonding_shard` (module-scoped dedicated fixture) — 3-validator shard +
  readonly with V4/V5 vaults pre-seeded, destroyed at teardown
- `Shard.attach_joiner(...)` (Phases A, B) / `Shard.attach_observer()`
  (Phase C)
- `_BackgroundLoad` (test-local) — daemon thread, round-robin deploys at
  `_BG_LOAD_INTERVAL` (2.0s per producer)
- `Node.deploy_rho_file("bond.rho", ...)` with substitutions
- `wait_for_block_visible`, `wait_for_deploy_included`,
  `wait_for_deploy_finalized`, `poll_until`
- `assert_block_finalized_on_all_nodes`,
  `assert_bonds_map_consistent_across_nodes`,
  `assert_all_deploys_finalized_on_all_nodes`
- `_dump_block_search_diagnostic` (test-local) — justification/visibility
  diagnostics on poll timeouts

## Related

- [PoS bond contract](../../resources/wallets/bond.rho) — the Rholang contract used for bonding
- [Consensus Protocol docs](../../../services/f1r3node-rust/docs/casper/CONSENSUS_PROTOCOL.md) — Epoch Boundaries
- [test_asymmetric_bonds](test_asymmetric_bonds.md) — tests consensus with unequal stakes (complementary)
- [test_trim_state](test_trim_state.md) — tests joiner synchronization via LFS (complementary; covers the explicit trim-state path)

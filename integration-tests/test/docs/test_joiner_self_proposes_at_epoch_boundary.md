# test_joiner_self_proposes_at_epoch_boundary

## Purpose

**Negative-control / forward-regression** for the bug observed in v19 of `test_bonding_validators` (joiner silently dropped from bonds when self-proposing first epoch-boundary block). After 6 variants — linear, concurrent multi-parent, V4 lagging, continuous bg proposers, multi-iteration scan covering 4 epoch boundaries — the bug **does not reproduce** under deterministic manual propose control.

The test is preserved as:
1. **Negative control** — proves the simple architectural shape (joiner produces first epoch-boundary block + multi-parent merges + bg chaos) is **not sufficient** to trigger §2.15. The bug needs something more — most likely the heartbeat-driven actor-message timing race that manual propose can't replicate.
2. **Forward regression** — when §2.15 is fixed at the node level, this test continues to pass, confirming the deterministic shape stays correct.

For the actual flake repro of §2.15, run `test_bonding_validators` (heartbeat=True + bg load) under subprocess provider — it surfaces the bug intermittently (~33% rate). See TODO §2.15 for the conditions ruled in/out.

## Setup

Custom shard (NOT shared) — needs heartbeat-disabled config that the shared shard doesn't provide.

- **Topology**: 3 genesis validators (V1/V2/V3 at stake 100 each) + readonly observer + V4 attached as joiner mid-test
- **Heartbeat**: **Disabled**. All blocks come from explicit `node.propose()` calls.
- **FTT**: -1 (instant finalization). Avoids needing additional confirmations after propose.
- **Synchrony constraint threshold**: 0 (any validator can propose at any time).
- **Epoch length**: 4 (inherited from `conf/rust.conf`).
- **V4's joiner attach**: `cli_flags={"--heartbeat-disabled"}` is passed explicitly. By default joiners auto-inherit heartbeat-enabled — only readonly observers auto-disable. Without this flag V4 would heartbeat-propose in parallel with the manual schedule and break the determinism.

### Genesis wallet seeding

V4's vault is seeded at genesis with `50_000_000_000_000_000` tokens via `extra_wallets` so the bond.rho deploy can pay phlo + stake.

## Test

### `test_joiner_self_proposes_at_epoch_boundary`

Single test, single phase. Block-by-block sequence:

| # | Producer | Action | Why |
|---|---|---|---|
| 1 | V1 | filler deploy + propose | advance height |
| 2 | V2 | filler deploy + propose | advance height |
| 3 | V3 | filler deploy + propose | advance height |
| **4** | **V1** | **bond.rho(V4) deploy + propose** | **bond block at exactly #4. closeBlock runs at end of #4 (4 % 4 == 0) and activates V4 via `pickActiveValidators`.** |
| 5 | V1 | filler deploy + propose | post-activation: V4 active starting here |
| 6 | V2 | filler deploy + propose | V4 still active |
| 7 | V3 | filler deploy + propose | V4 still active |
| **8** | **V4** | **filler deploy + propose** | **§2.15 TRIGGER: V4's first block IS the next epoch boundary after activation. closeBlock at #8 reads V4's local snapshot of allBondsTHM and per the §2.15 hypothesis silently drops V4.** |
| 9 | V4 | filler deploy + propose | liveness check — does V4 still propose post-#8? |

### Why V4 must NOT propose during #5-#7

If V4 propose-rotates with V1/V2/V3 and produces, say, #6 (a non-epoch-boundary block), V4's local state catches up and the §2.15 trigger no longer fires at #8. The flake nature of the bug in `test_bonding_validators` is exactly this — it depends on whether V4 happens to produce the epoch-boundary block as its first block.

### Block-numbering invariant

Manual propose with heartbeat off advances by exactly +1 per call. Each call is `deploy + propose` (propose requires non-empty mempool). Asserts at #3 and #4 verify the chain doesn't drift via multi-parent merge or auto-block-creation.

## What the test proves

- PoS bond.rho correctly records V4's bond at the bond block (verified at #4)
- closeBlock at #4 epoch boundary correctly activates V4 (verified by V4 being able to propose #8)
- **(currently failing — the bug)** closeBlock at the joiner's first self-proposed epoch boundary preserves the joiner in the active validator set
- **(when fixed)** V4 stays bonded across epoch boundaries it produces

## Key assertions

| Stage | Assertion |
|---|---|
| #3 | `blockNumber == 3` (block-numbering invariant) |
| #4 | bonds map = {V1, V2, V3, V4}, cross-node consistent |
| #7 | V4 in bonds (mid-epoch, V4 must remain bonded leading into next boundary) |
| #8 | sender == V4, blockNumber == 8 (epoch boundary) |
| #8 | **V4 still in bonds map** ← the §2.15 check; currently fails |
| #8 | bonds map cross-node consistent at {V1, V2, V3, V4} |
| #9 | V4 still proposing (liveness) |

## Forbidden-pattern coverage

The test does **NOT** opt out of any forbidden patterns by default. §2.15 is a **silent** bug — no `RecordingInvalidBlock`, no `InvalidBondsCache`, no `KvStoreError`, no FATAL signals. If observation later shows additional patterns firing, add the marker.

## Infrastructure used

- `provider.create_shard(config)` via `Shard.create()` — fresh per-test shard
- `shard.attach_joiner(VALIDATOR4_ID, cli_flags={"--heartbeat-disabled"}, ...)` — joiner with manual propose
- `Node.deploy_string()` / `Node.deploy_rho_file()` / `Node.propose()` / `Node.get_block()`
- `wait_for_block_visible()` from `infra/polling.py`
- `assert_bonds_map_consistent_across_nodes()` from `infra/assertions.py`
- `pytest.mark.xfail(strict=True)` — flips when bug is fixed

## Related

- [test_bonding_validators](test_bonding_validators.md) — heartbeat-driven test that exposes the bug ~33% of the time as a flake
- [PoS.rhox](../../../services/f1r3node-rust/casper/src/main/resources/PoS.rhox) — `closeBlock` (line 559) and `pickActiveValidators` (line 790)

## Notes for future maintenance

- If V4's `propose()` at step #8 raises `F1r3flyClientException`, that's an **earlier** §2.15 manifestation (V4 thinks it's not active locally before even reaching closeBlock). The test treats this as a fail with a clear message rather than catching it.
- The `xfail(strict=True)` marker means a passing run is itself a signal — when this test passes, §2.15 is fixed. Don't silently flip to `xfail(strict=False)` if the test starts passing intermittently; that would mask noise where deterministic signal is the whole point.

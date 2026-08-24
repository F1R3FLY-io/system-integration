# test_joiner_self_proposes_at_epoch_boundary

## Purpose

**Negative-control turned forward-regression** for the historical
joiner-bond-drop bug (v19 of `test_bonding_validators`: a freshly-bonded
joiner silently vanished from the bonds map after producing an
epoch-boundary block, ~33% flake). Six deterministic manual-propose
variants — linear, concurrent multi-parent, V4 lagging, continuous bg
proposers, multi-iteration boundary scan — never reproduced it: the bug
needed heartbeat-driven actor-message timing races. The bug class has
since been fixed (the seal-base / fresh-joiner-latest-message work) and
the bonding suite runs green under heartbeat.

The test remains as the deterministic regression for the shape nothing
else covers deterministically: a bond sealed exactly ON an epoch
boundary, activation via closeBlock, and the joiner self-proposing
across multiple later boundaries with the bonds map held node-identical
throughout.

## Setup

Dedicated per-test shard (heartbeat-disabled config the shared shard
doesn't provide).

- **Topology**: 3 genesis validators (V1/V2/V3, stake 100 each) + readonly
  observer + V4 attached as a joiner mid-test
- **Heartbeat**: disabled everywhere. Every block comes from an explicit
  `node.propose()`.
- **FTT**: -1 (instant finalization — no confirmation rounds after propose)
- **Synchrony constraint threshold**: 0 (any validator can propose any time)
- **Epoch length 4 / quarantine 10**: pinned explicitly via the shard's
  `global_cli_options` AND V4's attach `cli_options` — never inherited from
  `conf/rust.conf`. The whole test is built around block #4 being a
  boundary; rust.conf carries a long epoch for suites that never bond, and
  the two drifted (rust.conf moved to 50) while this test assumed 4.
- **V4's attach**: `cli_flags={"--heartbeat-disabled"}` is passed
  explicitly — joiners default to heartbeat-enabled (only readonly
  observers auto-disable), and a heartbeat-proposing V4 would break the
  manual schedule. V4 also carries the matching synchrony/FTT/epoch/
  quarantine options, since consensus parameters must agree across nodes.
- **Genesis wallet**: V4's vault seeded with `50_000_000_000_000_000` via
  `extra_wallets` so bond.rho can pay phlo + stake.

## Test flow

1. **Blocks #1–#3** — linear fillers from V1, V2, V3, each waited visible
   on every node. A block-numbering assert at #3 confirms manual propose
   advances exactly +1 (no drift via merge or auto-creation).
2. **Block #4** — V1 deploys `bond.rho` for V4 and proposes: the bond
   block lands exactly on the epoch boundary, so closeBlock activates V4
   in the same block. The boundary block's bonds map may validly show
   EITHER side of the transition (pre- or post-activation weights,
   depending on when the transition applies relative to header
   construction — same semantics as the bonding suite; an earlier soak
   preflight failed by demanding the post-set unconditionally). Whichever
   side #4 shows is pinned, and all five nodes must agree on that exact
   map. Block #5 gets the same either-side treatment: a preflight showed
   every node uniformly still carrying the pre-activation map at #5 —
   activation can surface in headers at a later transition. The
   behavioral proof of activation is V4's own successful self-propose
   below.
3. **Chaos phase** — three daemon threads (`_BgProposers`) continuously
   deploy+propose on V1/V2/V3 at 0.4s cadence until the LFB crosses the
   epoch boundary at #8 (a bond made during epoch 1 only surfaces in
   headers from the next boundary, so stopping at #7 would freeze the
   chain inside the header-lag window), approximating production propose
   churn so V4's boundary blocks land on a chain advanced under
   contention. V4 stays idle (receiving gossip).
4. **Pre-propose guard** — V4 must surface in the bonds map of the
   current LFB before it proposes, polled with a 60s deadline rather
   than asserted at a single instant (an LFB read inside the header-lag
   window shows the bond as absent — that is lag, not a drop; CI run
   32588262605 caught the old single-shot assert at exactly LFB #7).
   This is the check that catches an activation failure first — it
   fired when the epoch pin drifted.
5. **V4 boundary scan** — V4 deploys + proposes 12 sequential blocks
   (sole producer now, so heights advance +1 each). At least one — in
   practice three (e.g. #12/#16/#20) — lands on an epoch boundary. At
   every V4-authored boundary block: V4 still in the bonds map (the
   historical drop), and the full 4-validator map cross-node consistent.
   A V4 propose failure at any iteration is treated as a bond-drop
   manifestation (a node that believes it is not active), not tolerated.

## Key assertions

| Stage | Assertion |
|---|---|
| #3 | `blockNumber == 3` — block-numbering invariant |
| #4 | bonds map matches ONE transition side (pre- or post-set), cross-node consistent on that side |
| #5 | same either-side + cross-node agreement |
| pre-scan | V4 present in the current LFB's bonds map |
| scan | every V4 propose succeeds; `sender == V4` on each block |
| scan | ≥ 1 V4 block lands on an epoch boundary |
| boundary blocks | V4 still in bonds; full map `{V1,V2,V3,V4}` node-identical |

## Forbidden-pattern coverage

No `allow_forbidden_patterns` opt-out: the historical bug was silent (no
`RecordingInvalidBlock`, `InvalidBondsCache`, or storage errors), and a
clean run is expected to stay clean.

## Infrastructure used

- `Shard.create()` / `shard.destroy()` — fresh per-test shard
- `shard.attach_joiner(VALIDATOR4_ID, cli_flags={"--heartbeat-disabled"}, cli_options={...})`
- `_BgProposers` (test-local) — three daemon deploy+propose threads
- `Node.deploy_string()` / `Node.deploy_rho_file()` / `Node.propose()` / `Node.get_block()`
- `wait_for_block_visible()` from `infra/polling.py`
- `assert_bonds_map_consistent_across_nodes()` from `infra/assertions.py`

## Related

- [test_bonding_validators](test_bonding_validators.md) — the heartbeat-driven suite where the historical flake surfaced; now green
- [test_validator_lifecycle](test_validator_lifecycle.md) — full PoS lifecycle including epoch-boundary activation under heartbeat
- [PoS.rhox](../../../services/f1r3node-rust/casper/src/main/resources/PoS.rhox) — `closeBlock` / `pickActiveValidators`

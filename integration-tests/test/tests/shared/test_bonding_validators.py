"""
Bonding Validators Integration Test

Verifies the full bonding lifecycle on the shared session shard:

  1. test_bonding_validators — V4 bonds against the running 3-validator
     shared_shard, activates at the epoch boundary, proposes blocks, and
     other validators justify V4 in subsequent blocks.

  2. test_double_bond_succession — V5 bonds against a shard that already
     has V4 bonded (carry-over state from the first test). Catches the
     "second bond never finalizes" failure mode (Stacy 2026-04-23).

Both tests run under production config (heartbeat=true, ftt from rust.conf,
no manual propose). Cross-node finalization is asserted on every step via
``assert_block_finalized_on_all_nodes`` so a peer that rejects a block at
validation time (``Invalid(InvalidBondsCache)``) fails the test loudly.

POST-CONDITIONS for downstream shared tests:
  - After test_bonding_validators: V4 is permanently in the on-chain bonds
    map; the joiner container is removed at test exit. Shard runs with
    4 bonded / 3 active.
  - After test_double_bond_succession: V5 is also permanently bonded.
    Shard runs with 5 bonded / 3 active.

The shared_shard fixture seeds vaults for V4 and V5 at genesis (see
conftest.py) so the bond deploys can pay phlo + stake.

See docs/bonding-bug-test-plan.md and docs/bonding-bug-layer2-design.md
for context.
"""

import logging

import pytest
from f1r3fly.client import F1r3flyClientException

from ...infra.assertions import assert_block_finalized_on_all_nodes
from ...infra.keys import (
    VALIDATOR1_ID,
    VALIDATOR2_ID,
    VALIDATOR3_ID,
    VALIDATOR4_ID,
    VALIDATOR5_ID,
)
from ...infra.polling import (
    poll_until,
    wait_for_block_visible,
    wait_for_block_visible_on_all_nodes,
    wait_for_deploy_included,
    wait_for_finalized,
)

pytestmark = pytest.mark.xdist_group("shared")

_BOND_AMOUNT = 10_000_000

# Matches conf/rust.conf:genesis-block-data.epoch-length
_EPOCH_LENGTH = 4


def _bond_lifecycle(
    shard,
    timeouts,
    proposer_node,
    joiner_identity,
    expected_bonds_after: int,
) -> None:
    """Run the full bonding lifecycle for one joiner.

    Phases (per docs/bonding-bug-layer2-design.md §4 Test 1):
      1. Pre-bond state — confirm joiner not in current bonds map.
      2. Joiner cannot propose pre-bond.
      3. Bond deploy on `proposer_node` signed by `joiner_identity`.
      4. Bond block finalizes on every node; bonds map includes joiner.
      5. LFB advances past the next epoch boundary.
      6. Joiner produces a block as proposer.
      7. Other validators include the joiner in justifications of
         subsequent blocks.
      8. Post-bond liveness — every active validator + joiner deploys;
         every block finalizes on every node.
    """
    v1, v2, v3, ro = (
        shard.node("validator1"),
        shard.node("validator2"),
        shard.node("validator3"),
        shard.readonly,
    )

    with shard.add_joiner(joiner_identity) as joiner:
        # ── Phase 1: pre-bond state ──────────────────────────────────
        current_lfb = v1.last_finalized_block()
        bonds_pre = {b.validator: b.stake for b in current_lfb.blockInfo.bonds}
        assert joiner_identity.public_hex not in bonds_pre, (
            f"Joiner {joiner_identity.name} already in bonds pre-bond: "
            f"{sorted(bonds_pre)}"
        )
        logging.info(
            "Pre-bond LFB #%d: %d bonded validators, joiner %s not present",
            current_lfb.blockInfo.blockNumber,
            len(bonds_pre),
            joiner_identity.name,
        )

        # Wait for the joiner to sync to the current LFB before continuing.
        wait_for_block_visible(
            joiner, current_lfb.blockInfo.blockHash, timeout=timeouts.deploy_inclusion,
        )

        # ── Phase 2: joiner cannot propose pre-bond ──────────────────
        joiner.deploy_string(
            f'@"pre-bond-{joiner_identity.name}"!(0)',
            joiner_identity.private_key(),
            phlo_limit=100_000_000,
            phlo_price=1,
        )
        with pytest.raises(F1r3flyClientException):
            joiner.propose()
        logging.info("Joiner %s correctly rejected on propose pre-bond",
                     joiner_identity.name)

        # ── Phase 3: bond deploy ─────────────────────────────────────
        bond_deploy_id = proposer_node.deploy_rho_file(
            rho_file_path="resources/wallets/bond.rho",
            private_key=joiner_identity.private_key(),
            substitutions={"%AMOUNT": str(_BOND_AMOUNT)},
            phlo_limit=100_000_000,
            phlo_price=1,
        )
        bond_block = wait_for_deploy_included(
            proposer_node, bond_deploy_id, timeouts.deploy_inclusion,
        )
        bond_block_hash = bond_block.blockHash
        bond_block_number = bond_block.blockNumber
        logging.info(
            "Bond deploy %s landed in block #%d (%s)",
            bond_deploy_id[:24], bond_block_number, bond_block_hash[:16],
        )

        # ── Phase 4: bond block finalizes cross-node ─────────────────
        wait_for_finalized(proposer_node, bond_block_number, timeouts.finalization)
        assert_block_finalized_on_all_nodes(
            [v1, v2, v3, joiner, ro], bond_block_hash,
            timeout=timeouts.finalization,
        )
        bond_block_info = proposer_node.get_block(bond_block_hash)
        bonds_post = {
            b.validator: b.stake for b in bond_block_info.blockInfo.bonds
        }
        assert bonds_post.get(joiner_identity.public_hex) == _BOND_AMOUNT, (
            f"Bond block {bond_block_hash[:16]} bonds map missing or wrong "
            f"stake for {joiner_identity.name}: {bonds_post}"
        )
        assert len(bonds_post) == expected_bonds_after, (
            f"Bond block bonds map has {len(bonds_post)} entries, "
            f"expected {expected_bonds_after}: {sorted(bonds_post)}"
        )
        logging.info(
            "Bond block #%d finalized on all nodes (bonds: %d entries)",
            bond_block_number, len(bonds_post),
        )

        # ── Phase 5: epoch boundary ──────────────────────────────────
        epoch_target = bond_block_number + _EPOCH_LENGTH
        poll_until(
            predicate=lambda: (
                proposer_node.last_finalized_block().blockInfo.blockNumber
                if proposer_node.last_finalized_block().blockInfo.blockNumber >= epoch_target
                else None
            ),
            timeout=timeouts.finalization * 2,
            interval=3.0,
            description=f"LFB advances past epoch boundary at #{epoch_target}",
        )
        logging.info("LFB advanced past epoch boundary (#%d)", epoch_target)

        # ── Phase 6: joiner produces a block as active proposer ──────
        # Submit a deploy via the joiner so it has work in its mempool;
        # heartbeat will produce a block from joiner once activation lands.
        joiner.deploy_string(
            f'@"joiner-active-{joiner_identity.name}"!(1)',
            joiner_identity.private_key(),
            phlo_limit=100_000_000,
            phlo_price=1,
        )

        def _joiner_proposed():
            for blk in joiner.get_blocks(50):
                if (
                    blk.sender == joiner_identity.public_hex
                    and blk.blockNumber > bond_block_number
                ):
                    return blk
            return None

        joiner_block = poll_until(
            predicate=_joiner_proposed,
            timeout=timeouts.finalization * 2,
            interval=3.0,
            description=f"{joiner_identity.name} proposes a block post-activation",
        )
        wait_for_finalized(joiner, joiner_block.blockNumber, timeouts.finalization)
        assert_block_finalized_on_all_nodes(
            [v1, v2, v3, joiner, ro], joiner_block.blockHash,
            timeout=timeouts.finalization,
        )
        logging.info(
            "Joiner %s proposed block #%d (%s); finalized on all nodes",
            joiner_identity.name,
            joiner_block.blockNumber,
            joiner_block.blockHash[:16],
        )

        # ── Phase 7: other validators justify the joiner ─────────────
        v1.deploy_string(
            f'@"v1-after-{joiner_identity.name}"!(2)',
            VALIDATOR1_ID.private_key(),
            phlo_limit=100_000_000,
            phlo_price=1,
        )

        def _v1_justifies_joiner():
            for blk in v1.get_blocks(50):
                if blk.blockNumber <= joiner_block.blockNumber:
                    continue
                if blk.sender != VALIDATOR1_ID.public_hex:
                    continue
                if any(
                    j.validator == joiner_identity.public_hex
                    for j in blk.justifications
                ):
                    return blk
            return None

        v1_post_block = poll_until(
            predicate=_v1_justifies_joiner,
            timeout=timeouts.finalization * 2,
            interval=3.0,
            description=f"V1 produces a block justifying {joiner_identity.name}",
        )
        wait_for_finalized(v1, v1_post_block.blockNumber, timeouts.finalization)
        assert_block_finalized_on_all_nodes(
            [v1, v2, v3, joiner, ro], v1_post_block.blockHash,
            timeout=timeouts.finalization,
        )
        logging.info(
            "V1 block #%d (%s) justifies %s; finalized on all nodes",
            v1_post_block.blockNumber,
            v1_post_block.blockHash[:16],
            joiner_identity.name,
        )

        # ── Phase 8: post-bond liveness ──────────────────────────────
        for node, key in [
            (v1, VALIDATOR1_ID),
            (v2, VALIDATOR2_ID),
            (v3, VALIDATOR3_ID),
            (joiner, joiner_identity),
        ]:
            deploy_id = node.deploy_string(
                f'@"liveness-{node.name}-{joiner_identity.name}"!(1)',
                key.private_key(),
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            block = wait_for_deploy_included(
                node, deploy_id, timeouts.deploy_inclusion,
            )
            wait_for_finalized(node, block.blockNumber, timeouts.finalization)
            wait_for_block_visible_on_all_nodes(
                [v1, v2, v3, joiner, ro], block.blockHash,
                timeout=timeouts.finalization,
            )
            assert_block_finalized_on_all_nodes(
                [v1, v2, v3, joiner, ro], block.blockHash,
                timeout=timeouts.finalization,
            )

        logging.info(
            "Post-bond liveness verified: all 4 active nodes (incl. joiner %s) "
            "produce blocks that finalize cross-node",
            joiner_identity.name,
        )


def test_bonding_validators(shared_shard, timeouts) -> None:
    """End-to-end bonding lifecycle on the shared session shard.

    Bonds V4 against the running 3-validator shard. Verifies cross-node
    finalization, epoch activation, joiner participation, and network
    liveness. After this test V4 is permanently in the on-chain bonds map
    (the joiner container is removed at exit; subsequent shared tests run
    with 4 bonded / 3 active).
    """
    _bond_lifecycle(
        shared_shard,
        timeouts,
        proposer_node=shared_shard.node("validator1"),
        joiner_identity=VALIDATOR4_ID,
        expected_bonds_after=4,
    )


def test_double_bond_succession(shared_shard, timeouts) -> None:
    """Bond V5 on a shard that already has V4 bonded (from the previous
    test). Catches the failure mode where the second bond never finalizes.

    Bond is deployed via V2 (different proposer than V4's bond from V1)
    to exercise the multi-proposer path through the bonds_cache /
    justification-set composition.
    """
    v1 = shared_shard.node("validator1")
    bonds = {b.validator: b.stake for b in v1.last_finalized_block().blockInfo.bonds}
    assert VALIDATOR4_ID.public_hex in bonds, (
        f"Expected V4 already bonded from test_bonding_validators "
        f"(test order changed?). Current bonds: {sorted(bonds)}"
    )

    _bond_lifecycle(
        shared_shard,
        timeouts,
        proposer_node=shared_shard.node("validator2"),
        joiner_identity=VALIDATOR5_ID,
        expected_bonds_after=5,
    )

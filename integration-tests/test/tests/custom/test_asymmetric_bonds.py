"""
Asymmetric Bond Weight Integration Tests

Verifies that consensus behaves correctly with non-equal validator stakes.
Uses a custom shard with asymmetric bond weights (60/20/15), readonly
observer, and heartbeat enabled, exercising the production multi-parent
merge path with unequal weights.

Bond configuration:
    validator1  60   (63.2% of total)
    validator2  20   (21.1% of total)
    validator3  15   (15.8% of total)
    Total:      95

With FTT=0.33 (standard BFT), a block needs normalized FT > 0.33 to finalize.
    V1 alone (60):       FT = (60*2 - 95) / 95  = 0.26  (not enough)
    V2 alone (20):       FT = (40*2 - 95) / 95  = -0.16 (not enough)
    V3 alone (15):       FT = (30*2 - 95) / 95  = -0.37 (not enough)
    V1 + V3 (60+15=75):  FT = (75*2 - 95) / 95  = 0.58  (finalizes)
    V1 + V2 (60+20=80):  FT = (80*2 - 95) / 95  = 0.68  (finalizes)
    V2 + V3 (20+15=35):  FT = (35*2 - 95) / 95  = -0.26 (not enough)
    All three (95):       FT = (95*2 - 95) / 95  = 1.0   (finalizes)

No single validator can finalize alone. V1 + at least one other is needed.
V2 + V3 alone cannot finalize — they need V1 (the heaviest validator).
"""
import logging

import pytest

from ...infra.assertions import assert_all_nodes_agree_on_block
from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import (
    all_blocks_visible,
    get_blocks_if_enough,
    poll_until,
    try_find_deploy,
    wait_for_lfb_with_ft,
)
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

_ASYMMETRIC_BONDS = [
    (VALIDATOR1_ID, 60),
    (VALIDATOR2_ID, 20),
    (VALIDATOR3_ID, 15),
]
_FTT = 0.33


@pytest.fixture(scope="module")
def asymmetric_shard(provider, timeouts):
    """Module-scoped shard with asymmetric bonds and readonly. Shared across all tests."""
    config = ShardConfig(
        bonds=_ASYMMETRIC_BONDS,
        ftt=_FTT,
        heartbeat=True,
        include_readonly=True,
    )
    shard = Shard.create(provider, config, timeouts)
    yield shard
    shard.destroy()


def test_genesis_asymmetric_bonds(asymmetric_shard, node_conf) -> None:
    """Verify genesis block has correct asymmetric bonds and shard_id."""
    v1 = asymmetric_shard.validators[0]
    blocks = v1.get_blocks(100)
    genesis = None
    for b in blocks:
        if b.blockNumber == 0:
            genesis = b
            break
    assert genesis is not None, "Could not find block #0"

    genesis_block = v1.get_block(genesis.blockHash)
    genesis_info = genesis_block.blockInfo

    # shardId from config
    assert (
        genesis_info.shardId == node_conf.shard_id
    ), f"Genesis shardId '{genesis_info.shardId}' != config '{node_conf.shard_id}'"

    # Bonds match asymmetric config
    expected_bonds = {identity.public_hex: stake for identity, stake in _ASYMMETRIC_BONDS}
    actual_bonds = {b.validator: b.stake for b in genesis_info.bonds}
    assert len(actual_bonds) == len(
        expected_bonds
    ), f"Genesis has {len(actual_bonds)} bonds, expected {len(expected_bonds)}"
    for pubkey, expected_stake in expected_bonds.items():
        assert pubkey in actual_bonds, f"Validator {pubkey[:24]}... not found in genesis bonds"
        assert actual_bonds[pubkey] == expected_stake, (
            f"Validator {pubkey[:24]}... has stake {actual_bonds[pubkey]}, "
            f"expected {expected_stake}"
        )

    logging.info(
        "Genesis verified: shardId=%s, bonds=%s",
        node_conf.shard_id,
        {k[:16]: v for k, v in actual_bonds.items()},
    )


def test_fault_tolerance_asymmetric_bonds(asymmetric_shard, timeouts) -> None:
    """Verify FT monotonicity and multi-parent blocks with asymmetric bonds.

    1. Deploy on all validators
    2. Wait for 10+ blocks on all nodes (including readonly)
    3. Multi-parent blocks exist
    4. FT non-increasing by height on ALL validators
    """
    validators = asymmetric_shard.validators
    all_nodes = asymmetric_shard.all_nodes

    # Deploy on each validator
    validators[0].deploy_string("@100!(1)", VALIDATOR1_ID.private_key())
    validators[1].deploy_string("@200!(2)", VALIDATOR2_ID.private_key())
    validators[2].deploy_string("@300!(3)", VALIDATOR3_ID.private_key())

    # Wait for DAG depth on all nodes (including readonly)
    for node in all_nodes:
        poll_until(
            predicate=lambda n=node: get_blocks_if_enough(n, 10),
            timeout=timeouts.custom(300),
            interval=5.0,
            description=f"{node.name} accumulate 10+ blocks",
        )

    blocks = validators[0].get_blocks(50)
    assert len(blocks) >= 10, f"Expected at least 10 blocks, got {len(blocks)}"

    # Multi-parent blocks
    multi_parent_count = sum(1 for b in blocks if len(b.parentsHashList) > 1)
    logging.info("Asymmetric DAG: %d blocks, %d multi-parent", len(blocks), multi_parent_count)
    assert multi_parent_count > 0, "No multi-parent blocks in asymmetric-bond DAG"

    # FT monotonicity on ALL validators
    for node in validators:
        node_blocks = node.get_blocks(50)
        sorted_blocks = sorted(node_blocks, key=lambda b: b.blockNumber)
        ft_by_height: dict[int, float] = {}
        for b in sorted_blocks:
            ft = float(b.faultTolerance)
            height = b.blockNumber
            if height not in ft_by_height or ft > ft_by_height[height]:
                ft_by_height[height] = ft

        heights = sorted(ft_by_height.keys())
        for i in range(len(heights) - 1):
            h_cur = heights[i]
            h_next = heights[i + 1]
            assert ft_by_height[h_cur] >= ft_by_height[h_next], (
                f"FT not monotonically non-increasing on {node.name}: "
                f"height {h_cur} FT={ft_by_height[h_cur]} < "
                f"height {h_next} FT={ft_by_height[h_next]}"
            )

        logging.info("FT monotonicity verified on %s across %d heights", node.name, len(heights))


def test_finalization_asymmetric_bonds(asymmetric_shard, timeouts) -> None:
    """Verify finalization advances on ALL nodes with FTT=0.33.

    No single validator can finalize alone. V1 + at least one other
    validator must build on a block for it to finalize (FT > 0.33).
    Finalized blocks must have FT >= FTT on all nodes including readonly.

    In a multi-parent DAG with asymmetric bonds, the LFB pointer can
    advance ahead of the cached per-block FT field — a block becomes
    the LFB via V1 alone (FT = 60/95 = 0.263) before V2's signature
    lifts the clique to FT = 80/95 = 0.368. ``wait_for_lfb_with_ft``
    polls the combined predicate (single gRPC call per iteration, no
    torn reads).
    """
    all_nodes = asymmetric_shard.all_nodes
    validators = asymmetric_shard.validators

    initial_lfb_number = validators[0].last_finalized_block().blockInfo.blockNumber
    logging.info("Initial LFB: block #%d", initial_lfb_number)

    # Deploy on all validators to stimulate block creation
    validators[0].deploy_string("@2001!(1)", VALIDATOR1_ID.private_key())
    validators[1].deploy_string("@2002!(2)", VALIDATOR2_ID.private_key())
    validators[2].deploy_string("@2003!(3)", VALIDATOR3_ID.private_key())

    target = initial_lfb_number + 3
    timeout = timeouts.finalization * 6

    for node in all_nodes:
        info = wait_for_lfb_with_ft(node, target, _FTT, timeout=timeout, interval=5.0)
        logging.info(
            "%s: LFB #%d, FT=%.2f",
            node.name,
            info.blockNumber,
            float(info.faultTolerance),
        )

    logging.info("Finalization verified on all %d nodes (FT >= %.2f)", len(all_nodes), _FTT)


def test_cross_validator_state_agreement_asymmetric(asymmetric_shard, timeouts) -> None:
    """Verify all nodes compute identical post-states with asymmetric bonds.

    Regression test for determinism: with unequal stakes, the merge and LCA
    computations must still produce identical results across all nodes,
    including the readonly observer.
    """
    validators = asymmetric_shard.validators
    all_nodes = asymmetric_shard.all_nodes

    # Deploy on each validator
    deploy_ids = [
        validators[0].deploy_string("@3001!(1)", VALIDATOR1_ID.private_key()),
        validators[1].deploy_string("@3002!(2)", VALIDATOR2_ID.private_key()),
        validators[2].deploy_string("@3003!(3)", VALIDATOR3_ID.private_key()),
    ]

    # Wait for inclusion
    block_hashes = []
    for i, deploy_id in enumerate(deploy_ids):
        node = validators[i]
        block = poll_until(
            predicate=lambda n=node, d=deploy_id: try_find_deploy(n, d),
            timeout=timeouts.deploy_inclusion,
            interval=3.0,
            description=f"deploy {deploy_id[:16]} inclusion on {node.name}",
        )
        block_hashes.append(block.blockHash)

    # Wait for propagation to ALL nodes (including readonly)
    poll_until(
        predicate=lambda: all_blocks_visible(all_nodes, block_hashes),
        timeout=timeouts.custom(30),
        interval=3.0,
        description="block propagation to all nodes (including readonly)",
    )

    # Verify agreement across all nodes (including readonly)
    for block_hash in block_hashes:
        assert_all_nodes_agree_on_block(all_nodes, block_hash)

    logging.info(
        "Cross-node state agreement verified for %d blocks across %d nodes "
        "(including readonly) with asymmetric bonds",
        len(block_hashes),
        len(all_nodes),
    )

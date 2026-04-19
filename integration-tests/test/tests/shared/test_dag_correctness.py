"""
DAG Correctness Integration Test

Verifies the structure and properties of the multi-parent DAG produced by
heartbeat-driven block creation across multiple validators.

Expected FTT is derived from the node_conf fixture (parsed from
defaults.conf + rust.conf).

Single comprehensive test covering:
1. Multi-parent block merging (heartbeat produces merge blocks)
2. Fault tolerance monotonicity on all nodes
3. Cross-node post-state hash agreement (determinism regression)
4. Cross-node FT agreement on finalized blocks (FT >= FTT)
"""

import logging

import pytest

from ...infra.assertions import assert_all_nodes_agree_on_block
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import (
    all_blocks_visible,
    get_blocks_if_enough,
    poll_until,
    try_find_deploy,
)

pytestmark = pytest.mark.xdist_group("shared")


def test_dag_correctness(shared_shard, node_conf, timeouts) -> None:
    """Verify DAG structure, FT properties, and cross-node agreement.

    Deploys on all 3 validators, waits for 10+ blocks, then asserts:
    1. Multi-parent blocks exist (heartbeat merge blocks)
    2. FT is monotonically non-increasing by height on every node
    3. Deploy blocks propagate and all nodes agree on post-state hashes
    4. All nodes agree on FT values for finalized blocks, and FT >= FTT

    Regression coverage for the InvalidBondsCache bug (deterministic LCA
    computation, Phase 1) and deterministic merge ordering (Phase 3).
    """
    validators = shared_shard.validators
    all_nodes = shared_shard.all_nodes
    keys = [VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID]
    ftt = node_conf.ftt

    # Deploy on each validator — use distinct channels to avoid conflicts
    deploy_ids = []
    for i, (node, key) in enumerate(zip(validators, keys)):
        deploy_id = node.deploy_string(f"@{500 + i}!({i})", key.private_key())
        deploy_ids.append(deploy_id)

    # ── Wait for deploy inclusion ──────────────────────────────────
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

    logging.info("All 3 deploys included in blocks: %s",
                 [bh[:16] for bh in block_hashes])

    # ── Wait for DAG depth on all nodes ────────────────────────────
    for node in all_nodes:
        poll_until(
            predicate=lambda n=node: get_blocks_if_enough(n, 10),
            timeout=timeouts.finalization * 10,
            interval=5.0,
            description=f"{node.name} accumulate 10+ blocks",
        )

    blocks = validators[0].get_blocks(50)
    assert len(blocks) >= 10, f"Expected at least 10 blocks, got {len(blocks)}"

    # ── 1. Multi-parent blocks ─────────────────────────────────────
    multi_parent_count = sum(1 for b in blocks if len(b.parentsHashList) > 1)
    logging.info("DAG has %d blocks, %d multi-parent", len(blocks), multi_parent_count)
    assert multi_parent_count > 0, (
        "No multi-parent blocks found in DAG -- heartbeat should produce "
        "merge blocks with 3 concurrent validators"
    )

    # ── 2. FT monotonicity on ALL nodes ──────────────────────────
    for node in all_nodes:
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
            ft_cur = ft_by_height[h_cur]
            ft_next = ft_by_height[h_next]
            assert ft_cur >= ft_next, (
                f"FT not monotonically non-increasing on {node.name}: "
                f"height {h_cur} FT={ft_cur} < height {h_next} FT={ft_next}"
            )

        logging.info(
            "FT monotonicity verified on %s across %d heights",
            node.name, len(heights),
        )

    # ── 3. Cross-node post-state agreement on deploy blocks ────────
    poll_until(
        predicate=lambda: all_blocks_visible(all_nodes, block_hashes),
        timeout=timeouts.finalization,
        interval=3.0,
        description="deploy block propagation to all nodes",
    )

    for block_hash in block_hashes:
        assert_all_nodes_agree_on_block(all_nodes, block_hash)

    logging.info(
        "Post-state agreement verified for %d deploy blocks across %d nodes",
        len(block_hashes), len(all_nodes),
    )

    # ── 4. Cross-node FT agreement on finalized blocks ─────────────
    lfb = validators[0].last_finalized_block()
    lfb_number = lfb.blockInfo.blockNumber
    logging.info("LFB at block #%d (FTT=%.2f) -- comparing FT on finalized blocks",
                 lfb_number, ftt)

    v1_blocks = validators[0].get_blocks(50)
    finalized_blocks = [b for b in v1_blocks if b.blockNumber <= lfb_number]

    assert len(finalized_blocks) > 0, "No finalized blocks to compare"

    for b in finalized_blocks[:10]:
        ft_ref = float(b.faultTolerance)
        # Finalized blocks should have FT >= FTT
        assert ft_ref >= ftt, (
            f"Finalized block {b.blockHash[:16]}... (height #{b.blockNumber}) "
            f"has FT={ft_ref}, expected >= FTT={ftt}"
        )
        # All nodes should agree on FT
        for node in all_nodes[1:]:
            node_block = node.get_block(b.blockHash)
            ft_node = float(node_block.blockInfo.faultTolerance)
            assert ft_ref == ft_node, (
                f"FT mismatch on finalized block {b.blockHash[:16]}... "
                f"(height #{b.blockNumber}): "
                f"{all_nodes[0].name}={ft_ref}, {node.name}={ft_node}"
            )

    logging.info(
        "Cross-node FT agreement verified on %d finalized blocks across %d nodes (FT >= %.2f)",
        min(len(finalized_blocks), 10), len(all_nodes), ftt,
    )

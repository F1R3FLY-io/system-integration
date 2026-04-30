"""
DAG Correctness Integration Test

Verifies the structure and properties of the multi-parent DAG produced by
heartbeat-driven block creation across multiple validators.

Expected FTT is derived from the node_conf fixture (parsed from
defaults.conf + rust.conf).

Single comprehensive test covering:
1. Multi-parent block merging (heartbeat produces merge blocks)
2. Cross-node post-state hash agreement (determinism regression)
3. Cross-node FT agreement on finalized blocks (FT >= FTT, cached at finalization time)
"""

import logging

import pytest

from ...infra.assertions import (
    assert_all_nodes_agree_on_block,
    assert_block_finalized_on_all_nodes,
)
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
    2. Deploy blocks propagate and all nodes agree on post-state hashes
    3. Finalized blocks in the LFB ancestor chain have cached FT >= FTT
       on the reference node. Cross-node FT convergence is tested
       separately in test_convergence.py::test_ft_convergence.

    Regression coverage for the InvalidBondsCache bug (deterministic LCA
    computation, Phase 1) and deterministic merge ordering (Phase 2).
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

    # ── 2. (Removed) FT monotonicity ───────────────────────────────
    # FT monotonicity across heights is not a valid property in a multi-parent
    # DAG. Blocks at the same height can be on different branches with unrelated
    # FT values, and finalized blocks use cached FT (conservative lower bounds
    # for ancestors) which inverts the expected ordering.

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
    # Walk the LFB's actual ancestor chain rather than assuming all blocks
    # at height <= LFB are finalized. In a multi-parent DAG, multiple blocks
    # can exist at the same height — only the LFB and its ancestors are finalized.
    lfb = validators[0].last_finalized_block()
    lfb_hash = lfb.blockInfo.blockHash
    lfb_number = lfb.blockInfo.blockNumber
    logging.info("LFB at block #%d (FTT=%.2f) -- verifying FT on LFB ancestor chain",
                 lfb_number, ftt)

    # The LFB itself must be finalized on every node, not just V1. Catches
    # the case where a peer accepted the block at the protocol level but
    # rejected it at validation time (e.g. InvalidBondsCache).
    assert_block_finalized_on_all_nodes(all_nodes, lfb_hash)

    # Collect finalized block hashes by walking the parent chain from LFB
    finalized_hashes = []
    current_hash = lfb_hash
    while current_hash:
        finalized_hashes.append(current_hash)
        block = validators[0].get_block(current_hash)
        parents = list(block.blockInfo.parentsHashList)
        # Follow main parent (first in list) to stay on the finalized chain
        current_hash = parents[0] if parents else None

    assert len(finalized_hashes) > 0, "No finalized blocks in ancestor chain"
    logging.info("Found %d blocks in LFB ancestor chain", len(finalized_hashes))

    for block_hash in finalized_hashes:
        ref_block = validators[0].get_block(block_hash)
        ft_ref = float(ref_block.blockInfo.faultTolerance)
        block_num = ref_block.blockInfo.blockNumber

        # Finalized blocks should have cached FT >= FTT
        assert ft_ref >= ftt, (
            f"Finalized block {block_hash[:16]}... (height #{block_num}) "
            f"has FT={ft_ref}, expected >= FTT={ftt}"
        )

        # isFinalized field should be True for all blocks in the ancestor chain
        assert ref_block.blockInfo.isFinalized is True, (
            f"Finalized block {block_hash[:16]}... (height #{block_num}) "
            f"should have isFinalized=True"
        )

    logging.info(
        "FT cache verified on %d finalized blocks on %s (FT >= %.2f)",
        len(finalized_hashes), validators[0].name, ftt,
    )

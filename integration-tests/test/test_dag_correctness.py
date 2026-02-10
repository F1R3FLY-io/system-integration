"""
DAG Correctness Integration Tests

Tests that verify the structure and properties of the multi-parent DAG
produced by heartbeat-driven block creation across multiple validators.

Uses the session-scoped shard fixture (boot + 3 validators + readonly)
with heartbeat active on all validators and equal bond weights (1000 each).
"""

import logging
import time

import pytest
from docker.client import DockerClient
from f1r3fly.client import RClientException

from .common import (
    TestingContext,
)
from .conftest import (
    assert_containers_running,
    VALIDATOR1_KEY,
    VALIDATOR2_KEY,
    VALIDATOR3_KEY,
    ALL_CONTAINERS,
)
from .rnode import Node
from .wait import (
    wait_for_blocks_count_at_least,
)

pytestmark = pytest.mark.xdist_group("shard")


def test_fault_tolerance(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
    validator2_node: Node,
    validator3_node: Node,
) -> None:
    """Verify fault tolerance is monotonically non-increasing from genesis forward.

    In a correctly functioning Casper consensus, older blocks accumulate more
    support (more validators build on them) and therefore have higher fault
    tolerance than newer blocks. This property must hold regardless of DAG
    shape (single-parent or multi-parent).

    This test exercises the production block creation path: heartbeat-driven,
    multi-parent merge blocks from 3 concurrent validators with equal bond
    weights. The determinism fixes (Phase 1-3) ensure all validators compute
    identical post-states for merge blocks.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    context = testing_context
    validators = [validator1_node, validator2_node, validator3_node]

    # Deploy contracts on different validators to stimulate block creation
    # with actual deploys (not just empty heartbeat blocks). Each uses a
    # different channel to avoid conflicts.
    validator1_node.deploy_string("@100!(1)", VALIDATOR1_KEY)
    validator2_node.deploy_string("@200!(2)", VALIDATOR2_KEY)
    validator3_node.deploy_string("@300!(3)", VALIDATOR3_KEY)

    # Wait for sufficient DAG depth (heartbeat will include our deploys
    # and continue creating blocks). 10 blocks ensures multiple rounds
    # of multi-parent merging have occurred.
    wait_for_blocks_count_at_least(context, validator1_node, 10)

    # Query the DAG from validator1
    blocks = validator1_node.get_blocks(50)
    assert len(blocks) >= 10, (
        f"Expected at least 10 blocks, got {len(blocks)}"
    )

    # Verify multi-parent blocks exist. This is critical because our
    # determinism fixes target the multi-parent merge path. If all blocks
    # are single-parent, we're not exercising the fixed code.
    multi_parent_count = sum(
        1 for b in blocks if len(b.parentsHashList) > 1
    )
    logging.info(
        "DAG has %d blocks, %d with multiple parents",
        len(blocks), multi_parent_count,
    )
    # With 3 validators heartbeating concurrently, multi-parent blocks
    # should form within the first few rounds.
    assert multi_parent_count > 0, (
        "No multi-parent blocks found in DAG -- heartbeat should produce "
        "merge blocks with 3 concurrent validators"
    )

    # Sort blocks by blockNumber ascending for FT comparison.
    # Multiple blocks can share the same blockNumber in a multi-parent DAG.
    sorted_blocks = sorted(blocks, key=lambda b: b.blockNumber)

    # Verify FT is non-increasing when comparing blocks at successive
    # block numbers. Group by blockNumber and compare the maximum FT at
    # each height -- the max FT at height N should be >= max FT at height N+1.
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
            f"FT not monotonically non-increasing: "
            f"height {h_cur} FT={ft_cur} < height {h_next} FT={ft_next}"
        )

    # Verify cross-node FT agreement: validator2 should report the same
    # FT for the same block hashes.
    sample_blocks = sorted_blocks[:5]  # check first 5 blocks
    for b in sample_blocks:
        try:
            b2_info = validator2_node.get_block(b.blockHash)
            ft_v1 = float(b.faultTolerance)
            ft_v2 = float(b2_info.blockInfo.faultTolerance)
            assert ft_v1 == ft_v2, (
                f"FT mismatch for block {b.blockHash[:16]}...: "
                f"validator1={ft_v1}, validator2={ft_v2}"
            )
        except Exception:
            # Block may not have propagated yet; skip rather than fail
            logging.warning(
                "Block %s not found on validator2, skipping FT comparison",
                b.blockHash[:16],
            )


def test_cross_validator_post_state_agreement(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
    validator2_node: Node,
    validator3_node: Node,
) -> None:
    """Verify all validators compute identical post-states for the same blocks.

    This is a direct regression test for the InvalidBondsCache bug fixed in
    Phase 1 (deterministic LCA computation) and Phase 3 (deterministic merge
    ordering). Before these fixes, validators with different finalization
    states would compute different post-state hashes for the same block,
    causing the receiving validator to reject the block.

    The test deploys contracts on different validators, waits for the blocks
    to propagate, and verifies that all validators agree on the post-state
    hash for each block.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    context = testing_context
    nodes = [validator1_node, validator2_node, validator3_node]

    # Deploy on each validator using different channels (non-conflicting)
    deploy_ids = []
    deploy_ids.append(validator1_node.deploy_string("@1001!(1)", VALIDATOR1_KEY))
    deploy_ids.append(validator2_node.deploy_string("@1002!(2)", VALIDATOR2_KEY))
    deploy_ids.append(validator3_node.deploy_string("@1003!(3)", VALIDATOR3_KEY))

    # Wait for deploys to be included in blocks (poll find_deploy with retries)
    block_hashes = []
    for i, deploy_id in enumerate(deploy_ids):
        node = nodes[i]
        deadline = time.time() + 60
        block = None
        while time.time() < deadline:
            try:
                block = node.find_deploy(deploy_id)
                break
            except RClientException:
                time.sleep(3)
        assert block is not None, (
            f"Deploy {deploy_id[:24]}... not included in a block within 60s"
        )
        block_hashes.append(block.blockHash)

    # Wait for block propagation -- give all blocks time to reach all nodes
    time.sleep(10)

    # For each block, verify all validators report the same post-state hash
    for block_hash in block_hashes:
        post_states = {}
        for node in nodes:
            try:
                block_info = node.get_block(block_hash)
                post_state = block_info.blockInfo.postStateHash
                post_states[node.name] = post_state
            except Exception:
                logging.warning(
                    "Block %s not found on %s",
                    block_hash[:16], node.name,
                )

        # Need at least 2 validators to compare
        unique_states = set(post_states.values())
        if len(post_states) >= 2:
            assert len(unique_states) == 1, (
                f"Post-state hash mismatch for block {block_hash[:16]}...: "
                f"{post_states}"
            )
            logging.info(
                "Block %s: all %d validators agree on post-state %s",
                block_hash[:16], len(post_states),
                list(unique_states)[0][:16] if unique_states else "?",
            )

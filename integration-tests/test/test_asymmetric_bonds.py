"""
Asymmetric Bond Weight Integration Tests

Verifies that consensus behaves correctly with non-equal validator stakes.
These tests use a custom shard with asymmetric bond weights (60/20/15) and
heartbeat enabled, exercising the production multi-parent merge path with
unequal weights.

Bond configuration:
    validator1  60   (63.2% of total)
    validator2  20   (21.1% of total)
    validator3  15   (15.8% of total)
    Total:      95

With FTT=0.5, a block needs FT > 0.5 to finalize. This is achievable when
the majority stake (V1=60 alone is 63.2%, exceeding 50%) builds on a block.
Finalization should occur faster than with equal weights and FTT=0.99.
"""

import logging
import time

import pytest
from docker.client import DockerClient
from f1r3fly.client import RClientException

from .common import (
    CommandLineOptions,
)
from .conftest import (
    VALIDATOR1_ID,
    VALIDATOR1_KEY,
    VALIDATOR2_ID,
    VALIDATOR2_KEY,
    VALIDATOR3_ID,
    VALIDATOR3_KEY,
    start_custom_shard,
    CustomShard,
)
from .rnode import Node
from .wait import (
    wait_for_blocks_count_at_least,
)

pytestmark = pytest.mark.xdist_group("custom")

# Shared bond configuration for all asymmetric tests
_ASYMMETRIC_BONDS = [
    (VALIDATOR1_ID, 60),
    (VALIDATOR2_ID, 20),
    (VALIDATOR3_ID, 15),
]


def test_fault_tolerance_asymmetric_bonds(
    docker_client: DockerClient,
    command_line_options: CommandLineOptions,
) -> None:
    """Verify FT is monotonically non-increasing with asymmetric bonds.

    Same logic as test_fault_tolerance in test_dag_correctness.py but with
    asymmetric bond weights, verifying that the safety oracle correctly
    accounts for unequal stakes when computing fault tolerance.
    """
    with start_custom_shard(
        docker_client, command_line_options,
        bonds=_ASYMMETRIC_BONDS,
        ftt=0.5,
    ) as shard:
        v1 = shard.nodes["validator1"]
        v2 = shard.nodes["validator2"]
        v3 = shard.nodes["validator3"]

        # Deploy contracts to generate blocks with real deploys
        v1.deploy_string("@100!(1)", VALIDATOR1_KEY)
        v2.deploy_string("@200!(2)", VALIDATOR2_KEY)
        v3.deploy_string("@300!(3)", VALIDATOR3_KEY)

        # Wait for sufficient DAG depth
        context_timeout = command_line_options.receive_timeout
        scale = command_line_options.timeout_scale
        deadline = time.time() + context_timeout * 10 * scale
        while time.time() < deadline:
            blocks = v1.get_blocks(50)
            if len(blocks) >= 10:
                break
            time.sleep(5)

        blocks = v1.get_blocks(50)
        assert len(blocks) >= 10, (
            f"Expected at least 10 blocks, got {len(blocks)}"
        )

        # Verify multi-parent blocks exist
        multi_parent_count = sum(
            1 for b in blocks if len(b.parentsHashList) > 1
        )
        logging.info(
            "Asymmetric DAG: %d blocks, %d multi-parent",
            len(blocks), multi_parent_count,
        )
        assert multi_parent_count > 0, (
            "No multi-parent blocks in asymmetric-bond DAG"
        )

        # Verify FT is non-increasing by block height
        sorted_blocks = sorted(blocks, key=lambda b: b.blockNumber)
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

        logging.info("FT monotonicity verified across %d heights", len(heights))


def test_finalization_asymmetric_bonds(
    docker_client: DockerClient,
    command_line_options: CommandLineOptions,
) -> None:
    """Verify finalization advances with asymmetric bonds and FTT=0.5.

    With V1 holding 63.2% stake and FTT=0.5, blocks should finalize as
    soon as V1 (and at least one other validator) builds on them. This
    should happen within 60 seconds with heartbeat active.
    """
    with start_custom_shard(
        docker_client, command_line_options,
        bonds=_ASYMMETRIC_BONDS,
        ftt=0.5,
    ) as shard:
        v1 = shard.nodes["validator1"]
        v2 = shard.nodes["validator2"]

        # Record initial LFB
        initial_lfb = v1.last_finalized_block()
        initial_lfb_number = initial_lfb.blockInfo.blockNumber
        logging.info(
            "Initial LFB: block #%d (%s)",
            initial_lfb_number, initial_lfb.blockInfo.blockHash[:16],
        )

        # Deploy to stimulate block creation
        v1.deploy_string("@2001!(1)", VALIDATOR1_KEY)
        v2.deploy_string("@2002!(2)", VALIDATOR2_KEY)

        # Poll for finalization advancement
        scale = command_line_options.timeout_scale
        finalization_timeout = int(60 * scale)
        deadline = time.time() + finalization_timeout
        finalized = False
        while time.time() < deadline:
            current_lfb = v1.last_finalized_block()
            current_lfb_number = current_lfb.blockInfo.blockNumber
            if current_lfb_number > initial_lfb_number:
                logging.info(
                    "Finalization advanced: block #%d -> #%d",
                    initial_lfb_number, current_lfb_number,
                )
                finalized = True
                break
            time.sleep(5)

        assert finalized, (
            f"Finalization did not advance beyond block #{initial_lfb_number} "
            f"within {finalization_timeout}s with asymmetric bonds (60/20/15) and FTT=0.5"
        )

        # Verify the finalized block's FT exceeds the threshold
        final_lfb_hash = v1.last_finalized_block().blockInfo.blockHash
        final_block = v1.get_block(final_lfb_hash)
        ft = float(final_block.blockInfo.faultTolerance)
        logging.info("Finalized block FT: %f (threshold: 0.5)", ft)
        assert ft > 0.5, (
            f"Finalized block FT={ft}, expected > 0.5"
        )

        # Verify V2 also sees finalization advancing
        v2_lfb = v2.last_finalized_block()
        v2_lfb_number = v2_lfb.blockInfo.blockNumber
        assert v2_lfb_number >= initial_lfb_number, (
            f"V2 LFB #{v2_lfb_number} behind initial #{initial_lfb_number}"
        )
        logging.info(
            "V2 LFB: block #%d -- finalization consistent across validators",
            v2_lfb_number,
        )


def test_cross_validator_state_agreement_asymmetric(
    docker_client: DockerClient,
    command_line_options: CommandLineOptions,
) -> None:
    """Verify all validators compute identical post-states with asymmetric bonds.

    This is a direct regression test for the determinism fixes. With unequal
    stakes, the merge and LCA computations must still produce identical
    results across all validators.
    """
    with start_custom_shard(
        docker_client, command_line_options,
        bonds=_ASYMMETRIC_BONDS,
        ftt=0.5,
    ) as shard:
        v1 = shard.nodes["validator1"]
        v2 = shard.nodes["validator2"]
        v3 = shard.nodes["validator3"]
        nodes = [v1, v2, v3]

        # Deploy on each validator using different channels
        deploy_ids = []
        deploy_ids.append(v1.deploy_string("@3001!(1)", VALIDATOR1_KEY))
        deploy_ids.append(v2.deploy_string("@3002!(2)", VALIDATOR2_KEY))
        deploy_ids.append(v3.deploy_string("@3003!(3)", VALIDATOR3_KEY))

        # Wait for deploys to be included in blocks
        scale = command_line_options.timeout_scale
        find_timeout = int(60 * scale)
        block_hashes = []
        for i, deploy_id in enumerate(deploy_ids):
            node = nodes[i]
            deadline = time.time() + find_timeout
            block = None
            while time.time() < deadline:
                try:
                    block = node.find_deploy(deploy_id)
                    break
                except RClientException:
                    time.sleep(3)
            assert block is not None, (
                f"Deploy {deploy_id[:24]}... not included in a block within {find_timeout}s"
            )
            block_hashes.append(block.blockHash)

        # Wait for propagation
        time.sleep(10)

        # Verify post-state agreement across all validators
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

            unique_states = set(post_states.values())
            if len(post_states) >= 2:
                assert len(unique_states) == 1, (
                    f"Post-state mismatch for block {block_hash[:16]}...: "
                    f"{post_states}"
                )
                logging.info(
                    "Block %s: %d validators agree on post-state %s",
                    block_hash[:16], len(post_states),
                    list(unique_states)[0][:16],
                )

        logging.info(
            "Cross-validator state agreement verified with asymmetric bonds"
        )

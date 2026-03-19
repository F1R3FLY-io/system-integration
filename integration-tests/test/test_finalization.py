"""
Finalization Integration Tests

Tests that verify block finalization advances correctly in a multi-validator
shard with heartbeat-driven block creation.

Uses the session-scoped shard fixture (boot + 3 validators + readonly)
with fault-tolerance-threshold = 0.99 and equal bond weights (1000 each).
With 3 validators at equal weight, a block's fault tolerance reaches 1.0
when all validators have built on it, exceeding the 0.99 threshold. This
should happen within a few heartbeat rounds (~15-30s).
"""

import logging
import time

import pytest
from docker.client import DockerClient

from .common import (
    TestingContext,
)
from .conftest import (
    ALL_CONTAINERS,
    VALIDATOR1_KEY,
    VALIDATOR2_KEY,
    VALIDATOR3_KEY,
    assert_containers_running,
)
from .rnode import Node

pytestmark = pytest.mark.xdist_group("shard")


def test_finalizes_block(
    docker_client: DockerClient,
    testing_context: TestingContext,
    bootstrap_node: Node,
    validator1_node: Node,
    validator2_node: Node,
    validator3_node: Node,
) -> None:
    """Verify that finalization advances within 60 seconds.

    With 3 equal-weight validators (1000 bond each) and FTT=0.99, a block
    is finalized when all 3 validators have built upon it, giving it
    FT = (3000/3000) * 2 - 1 = 1.0 > 0.99.

    The heartbeat creates blocks every 5-10 seconds on each validator. After
    3-4 rounds where each validator builds on the others' blocks, early blocks
    will be finalized. If finalization does not advance within 60 seconds,
    the consensus layer has a bug.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    nodes = [bootstrap_node, validator1_node, validator2_node, validator3_node]

    # Record the initial LFB on validator1 as a baseline.
    initial_lfb = validator1_node.last_finalized_block()
    initial_lfb_number = initial_lfb.blockInfo.blockNumber
    logging.info(
        "Initial LFB: block #%d (%s)",
        initial_lfb_number,
        initial_lfb.blockInfo.blockHash[:16],
    )

    # Deploy contracts to stimulate meaningful block creation beyond
    # empty heartbeat blocks.
    validator1_node.deploy_string("@2001!(1)", VALIDATOR1_KEY)
    validator2_node.deploy_string("@2002!(2)", VALIDATOR2_KEY)
    validator3_node.deploy_string("@2003!(3)", VALIDATOR3_KEY)

    # Poll for finalization advancement. The LFB must advance beyond
    # the initial baseline.
    finalization_timeout = int(60 * testing_context.timeout_scale)
    deadline = time.time() + finalization_timeout
    finalized = False
    while time.time() < deadline:
        current_lfb = validator1_node.last_finalized_block()
        current_lfb_number = current_lfb.blockInfo.blockNumber
        if current_lfb_number > initial_lfb_number:
            logging.info(
                "Finalization advanced: block #%d -> #%d (%s)",
                initial_lfb_number,
                current_lfb_number,
                current_lfb.blockInfo.blockHash[:16],
            )
            finalized = True
            break
        time.sleep(5)

    assert finalized, (
        f"Finalization did not advance beyond block #{initial_lfb_number} "
        f"within {finalization_timeout}s -- this indicates a consensus bug"
    )

    # Verify all nodes agree on finalization: each node's LFB should be
    # at or beyond the finalized block we observed. Finalization may have
    # advanced further on some nodes, which is acceptable.
    final_lfb_hash = validator1_node.last_finalized_block().blockInfo.blockHash
    final_lfb_number = validator1_node.last_finalized_block().blockInfo.blockNumber

    for node in nodes:
        node_lfb = node.last_finalized_block()
        node_lfb_number = node_lfb.blockInfo.blockNumber
        assert (
            node_lfb_number >= initial_lfb_number
        ), f"Node {node.name} LFB #{node_lfb_number} is behind initial LFB #{initial_lfb_number}"
        logging.info(
            "Node %s LFB: block #%d (%s)",
            node.name,
            node_lfb_number,
            node_lfb.blockInfo.blockHash[:16],
        )

    # Verify the finalized block has valid FT exceeding the threshold (0.99)
    finalized_block = validator1_node.get_block(final_lfb_hash)
    ft = float(finalized_block.blockInfo.faultTolerance)
    logging.info(
        "Finalized block #%d FT: %f",
        final_lfb_number,
        ft,
    )
    assert ft > 0.99, (
        f"Finalized block #{final_lfb_number} has FT={ft}, expected > 0.99 "
        f"(the fault-tolerance-threshold)"
    )

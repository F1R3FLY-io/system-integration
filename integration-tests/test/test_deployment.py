"""
Deployment Integration Tests

Tests for deploy error handling: insufficient phlo, phlo exhaustion.
Uses the session-scoped shard fixture with heartbeat-driven block creation.

Previously, deploying with insufficient phlo triggered NeglectedInvalidBlock
crashes. This was resolved by fixing non-deterministic merge ordering in
the consensus layer (EventLogIndex, DeployChainIndex, ConflictSetMerger)
and adding transient-error recovery in the Proposer.
"""

import time

import pytest
from docker.client import DockerClient

from .conftest import (
    assert_containers_running,
    VALIDATOR1_KEY,
    ALL_CONTAINERS,
)
from .rnode import Node

pytestmark = pytest.mark.xdist_group("shard")


def test_deploy_with_not_enough_phlo(
    docker_client: DockerClient,
    validator1_node: Node,
) -> None:
    """Deploy with insufficient phlo should be included in a block but marked as errored.

    Deploys a simple contract with phlo_limit=10 (too low -- even '@1!(1)' costs ~97 phlo).
    Heartbeat auto-proposes the block. The deploy should be in the block with
    errored=True.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    node = validator1_node

    # Record block count before deploy
    initial_count = node.get_blocks_count(20)

    # Deploy with intentionally low phlo limit (97 phlo needed, only 10 allowed)
    node.deploy_string(
        '@1!(1)',
        VALIDATOR1_KEY,
        phlo_limit=10,
        phlo_price=1,
    )

    # Wait for heartbeat to propose a block containing the deploy
    deadline = time.time() + 60
    block_with_deploy = None
    while time.time() < deadline:
        current_count = node.get_blocks_count(20)
        if current_count > initial_count:
            # Check recent blocks for our deploy
            blocks = node.get_blocks(20)
            for block in blocks:
                if block.deployCount > 0 and block.blockNumber > 0:
                    block_info = node.get_block(block.blockHash)
                    for deploy in block_info.deploys:
                        if deploy.term == '@1!(1)':
                            block_with_deploy = block_info
                            break
                if block_with_deploy:
                    break
        if block_with_deploy:
            break
        time.sleep(3)

    assert block_with_deploy is not None, (
        "Deploy should have been included in a block within 60s"
    )

    # Find the deploy with our term
    errored_deploy = None
    for deploy in block_with_deploy.deploys:
        if deploy.term == '@1!(1)':
            errored_deploy = deploy
            break

    assert errored_deploy is not None, "Should find our deploy in the block"
    assert errored_deploy.errored, (
        f"Deploy with phlo_limit=10 should be errored, got errored={errored_deploy.errored}"
    )

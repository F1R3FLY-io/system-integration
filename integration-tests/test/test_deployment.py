"""
Deployment Integration Tests

Tests for deploy error handling: insufficient phlo, phlo exhaustion.
Uses the session-scoped shard fixture with heartbeat-driven block creation.

Previously, deploying with insufficient phlo triggered NeglectedInvalidBlock
crashes. This was resolved by fixing non-deterministic merge ordering in
the consensus layer (EventLogIndex, DeployChainIndex, ConflictSetMerger)
and adding transient-error recovery in the Proposer.
"""

import logging
import time

import pytest
from docker.client import DockerClient
from f1r3fly.client import F1r3flyClientException

from .common import TestingContext
from .conftest import (
    assert_containers_running,
    VALIDATOR1_KEY,
    ALL_CONTAINERS,
)
from .rnode import Node

pytestmark = pytest.mark.xdist_group("shard")


def test_deploy_with_not_enough_phlo(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
) -> None:
    """Deploy with insufficient phlo should be included in a block but marked as errored.

    Deploys a simple contract with phlo_limit=10 (too low -- even '@1!(1)' costs ~97 phlo).
    Heartbeat auto-proposes the block. The deploy should be in the block with
    errored=True.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    node = validator1_node

    # Deploy with intentionally low phlo limit (97 phlo needed, only 10 allowed)
    deploy_id = node.deploy_string(
        '@1!(1)',
        VALIDATOR1_KEY,
        phlo_limit=10,
        phlo_price=1,
    )
    logging.info("Deployed with insufficient phlo, deploy_id=%s", deploy_id[:24])

    # Poll find_deploy until the heartbeat includes the deploy in a block.
    # find_deploy is a direct index lookup (deploy-id -> block-hash) and does
    # not depend on the DAG tip depth window, unlike get_blocks(N).
    find_timeout = int(120 * testing_context.timeout_scale)
    deadline = time.time() + find_timeout
    block_hash = None
    while time.time() < deadline:
        try:
            light_block = node.find_deploy(deploy_id)
            block_hash = light_block.blockHash
            logging.info(
                "Deploy found in block %s (blockNumber=%d)",
                block_hash[:16], light_block.blockNumber,
            )
            break
        except F1r3flyClientException:
            time.sleep(3)

    assert block_hash is not None, (
        f"Deploy {deploy_id[:24]} should have been included in a block within {find_timeout}s"
    )

    # Fetch full block to inspect the deploy's errored status.
    # find_deploy guarantees our deploy is in this block; match by term.
    block_info = node.get_block(block_hash)
    errored_deploy = None
    for deploy in block_info.deploys:
        if deploy.term == '@1!(1)':
            errored_deploy = deploy
            break

    assert errored_deploy is not None, (
        f"Should find deploy with term '@1!(1)' in block {block_hash[:16]}"
    )
    assert errored_deploy.errored, (
        f"Deploy with phlo_limit=10 should be errored, got errored={errored_deploy.errored}"
    )

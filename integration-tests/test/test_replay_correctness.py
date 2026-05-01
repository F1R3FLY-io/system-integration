"""
Replay Correctness Test

Verifies that the shard handles cross-validator replay correctly by
deploying bridge.rho — a contract with duplicate channel sends that
exercises complex replay paths (duplicate sends, join consumes,
evaluation order). All validators must accept the block and LFB
must advance.

Network convergence after DAG fork divergence is tested separately in
test_convergence.py.
"""

import logging
import os
import time
from typing import List

import pytest
from docker.client import DockerClient

from .common import TestingContext
from .conftest import (
    ALL_CONTAINERS,
    VALIDATOR1_KEY,
    assert_containers_running,
)
from .rnode import Node

pytestmark = pytest.mark.xdist_group("shard")


def _load_bridge_contract() -> str:
    """Load bridge.rho from integration-tests/resources/bridge.rho."""
    integration_tests_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(integration_tests_dir, "resources", "bridge.rho")
    with open(path) as f:
        return f.read()


def test_duplicate_sends_accepted_by_all_validators(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
    all_nodes: List[Node],
) -> None:
    """Deploy bridge.rho and verify the block is accepted and finalized.

    bridge.rho has duplicate channel sends (requiredSigsCh!(2) twice,
    oracleCountCh!(3) twice). This exercises:
    - Replay determinism for duplicate channel sends
    - Observer block processing and reporting replay
    - Cross-validator consensus on complex contracts

    Comprehensive error scanning (panics, BugFoundError, ERROR-level logs)
    is handled by the autouse fixture in conftest.py after this test completes.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    deploy_id = validator1_node.deploy_string(
        _load_bridge_contract(),
        VALIDATOR1_KEY,
        phlo_limit=500_000_000,
        phlo_price=1,
    )
    logging.info("Deployed bridge.rho, deploy_id=%s", deploy_id[:24])

    find_timeout = int(60 * testing_context.timeout_scale)
    deadline = time.time() + find_timeout
    deploy_block = None
    while time.time() < deadline:
        try:
            light_block = validator1_node.find_deploy(deploy_id)
            logging.info(
                "Deploy included in block #%d (%s)",
                light_block.blockNumber,
                light_block.blockHash[:16],
            )
            deploy_block = light_block.blockNumber
            break
        except Exception:
            time.sleep(3)
    assert deploy_block is not None, (
        f"Deploy {deploy_id[:24]} was not included in a block within {find_timeout}s"
    )

    advance_timeout = int(120 * testing_context.timeout_scale)
    deadline = time.time() + advance_timeout
    reached = {node.name: 0 for node in all_nodes}
    target = deploy_block + 3
    while time.time() < deadline:
        all_done = True
        for node in all_nodes:
            if reached[node.name] >= target:
                continue
            try:
                lfb = node.last_finalized_block()
                current = lfb.blockInfo.blockNumber
                reached[node.name] = current
                if current >= target:
                    logging.info("%s LFB reached #%d", node.name, current)
                else:
                    all_done = False
            except Exception:
                all_done = False
        if all_done:
            return
        time.sleep(5)
    stalled = [f"{name} at #{blk}" for name, blk in reached.items() if blk < target]
    assert not stalled, (
        f"Network stalled: {', '.join(stalled)}; "
        f"expected at least #{target} within {advance_timeout}s"
    )

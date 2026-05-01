"""
Network Convergence Tests

Tests that the network recovers after DAG tip divergence caused by:
1. Validator pause — pausing a container forces other validators to
   produce independent blocks, creating DAG forks that must be merged
   after unpause.
2. Slow deploy — a phlo-exhausting deploy (#224) blocks one validator
   while others produce heartbeat blocks, causing divergence (#437).

With synchrony-constraint-threshold=0 (recommended for multi-parent DAG),
the synchrony constraint does not block proposals. The affected validator
eventually recovers, proposes, and the network converges normally.
"""

import logging
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

# Phlo-exhausting loop contract. Runs until phlo runs out, blocking the
# proposing validator long enough for other validators to create independent
# blocks via heartbeat, causing DAG tip divergence.
SLOW_LOOP_CONTRACT = """
new stdout(`rho:io:stdout`) in {
  new loop in {
    contract loop(@n) = {
      if (n <= 0) {
        stdout!("done")
      } else {
        loop!(n - 1)
      }
    } |
    loop!(100000)
  }
}
"""


def _get_lfb_number(node: Node) -> int:
    """Return current LFB block number."""
    return node.last_finalized_block().blockInfo.blockNumber


def _wait_for_deploy_in_block(node: Node, deploy_id: str, timeout: float):
    """Poll find_deploy until the deploy is included in a block.

    Returns (block_hash, block_number) or raises AssertionError on timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            light_block = node.find_deploy(deploy_id)
            logging.info(
                "Deploy included in block #%d (%s)",
                light_block.blockNumber,
                light_block.blockHash[:16],
            )
            return light_block.blockHash, light_block.blockNumber
        except Exception:
            time.sleep(3)
    raise AssertionError(f"Deploy {deploy_id[:24]} was not included in a block within {timeout}s")


def _wait_for_lfb(node: Node, target: int, timeout: float) -> int:
    """Poll until LFB reaches target. Returns final LFB number."""
    deadline = time.time() + timeout
    current = 0
    while time.time() < deadline:
        try:
            current = _get_lfb_number(node)
            if current >= target:
                logging.info("%s: LFB advanced to #%d", node.name, current)
                return current
        except Exception:
            pass
        time.sleep(5)
    raise AssertionError(
        f"{node.name}: LFB stuck at #{current}, expected >= #{target} within {timeout}s"
    )


def _assert_lfb_advances_all_nodes(
    nodes: List[Node],
    target: int,
    timeout: float,
) -> None:
    """Assert LFB reaches target on all nodes within timeout."""
    deadline = time.time() + timeout
    remaining = set(node.name for node in nodes)
    while time.time() < deadline and remaining:
        for node in nodes:
            if node.name not in remaining:
                continue
            try:
                lfb = _get_lfb_number(node)
                if lfb >= target:
                    logging.info("%s: LFB reached #%d", node.name, lfb)
                    remaining.discard(node.name)
            except Exception:
                pass
        if remaining:
            time.sleep(5)
    if remaining:
        stalled = []
        for node in nodes:
            if node.name in remaining:
                try:
                    lfb = _get_lfb_number(node)
                except Exception:
                    lfb = -1
                stalled.append(f"{node.name} at #{lfb}")
        raise AssertionError(
            f"LFB did not reach #{target} on: {', '.join(stalled)} within {timeout}s"
        )


def test_network_recovers_from_validator_pause(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
    all_nodes: List[Node],
) -> None:
    """Pause validator1 for 15s to force DAG tip divergence, then verify
    the network converges and LFB advances on all nodes.

    While validator1 is paused, other validators create blocks via heartbeat.
    After unpause, the validators exchange tips and must propose multi-parent
    convergence blocks to merge the diverged forks.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    baseline_lfb = _get_lfb_number(validator1_node)
    logging.info("Baseline LFB: block #%d", baseline_lfb)

    container = docker_client.containers.get("rnode.validator1")
    logging.info("Pausing validator1 for 15s to force DAG divergence...")
    container.pause()
    time.sleep(15)
    container.unpause()
    logging.info("Validator1 unpaused. Waiting for network convergence...")

    advance_timeout = int(120 * testing_context.timeout_scale)
    _assert_lfb_advances_all_nodes(
        all_nodes,
        baseline_lfb + 3,
        advance_timeout,
    )


@pytest.mark.timeout(1200)
def test_network_converges_after_slow_deploy(
    docker_client: DockerClient,
    validator1_node: Node,
    validator2_node: Node,
    validator3_node: Node,
) -> None:
    """Deploy a phlo-exhausting loop and verify the shard converges.

    The loop contract blocks V1 for ~25s while phlo is exhausted.
    During this time, V2 and V3 create independent heartbeat blocks,
    causing DAG tip divergence. After the deploy completes (errored),
    the network must converge and LFB must advance.

    This reproduces both:
    - #224: phlo-exhausting deploy stalls the proposing validator
    - #437: resulting DAG tip divergence causes permanent LFB stall
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    baseline_lfb = _get_lfb_number(validator1_node)
    if baseline_lfb == 0:
        logging.info("Waiting for initial LFB advancement...")
        _wait_for_lfb(validator1_node, 1, timeout=60)
        baseline_lfb = _get_lfb_number(validator1_node)

    logging.info("Baseline LFB: #%d", baseline_lfb)

    deploy_id = validator1_node.deploy_string(
        SLOW_LOOP_CONTRACT,
        VALIDATOR1_KEY,
        phlo_limit=20_000_000,
        phlo_price=1,
    )
    logging.info("Deployed loop contract, deploy_id=%s", deploy_id[:24])

    # Wait for the deploy to be included in a block. The phlo-exhausting
    # loop takes ~200s to execute on the proposing validator, during which
    # V2+V3 produce independent heartbeat blocks.
    find_timeout = 300
    _, deploy_block = _wait_for_deploy_in_block(
        validator1_node,
        deploy_id,
        find_timeout,
    )

    # LFB must advance past the deploy block. If convergence fails,
    # LFB stays stuck and this assertion times out.
    advance_timeout = 480
    target_lfb = deploy_block + 3
    logging.info(
        "Waiting for LFB to advance past deploy block #%d (target=#%d, timeout=%ds)...",
        deploy_block,
        target_lfb,
        advance_timeout,
    )
    _wait_for_lfb(validator1_node, target_lfb, timeout=advance_timeout)

    # Verify all validators converged.
    lfb_values = {}
    for name, node in [
        ("V1", validator1_node),
        ("V2", validator2_node),
        ("V3", validator3_node),
    ]:
        lfb_values[name] = _get_lfb_number(node)
    logging.info("Final LFB values: %s", lfb_values)

    max_lfb = max(lfb_values.values())
    min_lfb = min(lfb_values.values())
    assert max_lfb - min_lfb <= 2, (
        f"Validators diverged: LFB spread = {max_lfb - min_lfb}, values = {lfb_values}"
    )

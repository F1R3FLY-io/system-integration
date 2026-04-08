"""
Network Convergence After DAG Fork Divergence (#437, #224)

Tests that the network recovers after a phlo-exhausting deploy (#224)
blocks one validator while others produce heartbeat blocks, causing
DAG tip divergence (#437).

With synchrony-constraint-threshold=0 (recommended for multi-parent DAG),
the synchrony constraint does not block proposals. The validator blocked
by the slow deploy (~200s) eventually finishes, proposes the errored
block, and the network converges normally. The test verifies this
end-to-end recovery.

With synchrony-constraint-threshold=0.67 (requires 7+ validators),
the synchrony constraint actively blocks convergence — see the stashed
fixes on f1r3node-rust branch fix/convergence-after-divergence and
docs/TODO.md Level 2 notes.
"""

import logging
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
                light_block.blockNumber, light_block.blockHash[:16],
            )
            return light_block.blockHash, light_block.blockNumber
        except Exception:
            time.sleep(3)
    raise AssertionError(
        f"Deploy {deploy_id[:24]} was not included in a block within {timeout}s"
    )


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
        validator1_node, deploy_id, find_timeout,
    )

    # LFB must advance past the deploy block. If convergence fails,
    # LFB stays stuck and this assertion times out.
    advance_timeout = 480
    target_lfb = deploy_block + 3
    logging.info(
        "Waiting for LFB to advance past deploy block #%d (target=#%d, timeout=%ds)...",
        deploy_block, target_lfb, advance_timeout,
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
        f"Validators diverged: LFB spread = {max_lfb - min_lfb}, "
        f"values = {lfb_values}"
    )

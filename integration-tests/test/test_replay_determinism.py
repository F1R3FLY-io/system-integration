"""
Replay Determinism and Network Resilience Tests

Verifies that the shard handles replay correctness and recovers from
validator disagreement and temporary desynchronization:

1. Cross-validator replay determinism — deploying a contract with duplicate
   channel sends (bridge.rho) produces identical costs on all validators.
2. Shard recovery after block rejection — LFB continues advancing even when
   a deploy triggers a validation divergence.
3. Shard recovery from validator pause — pausing a container causes DAG tip
   divergence; the network must converge after unpause.
4. Shard recovery from slow deploy — a phlo-exhausting loop blocks one
   validator long enough for others to diverge; the network must recover.
"""

import logging
import os
import re
import time
from typing import List

import pytest
from docker.client import DockerClient

from .common import TestingContext
from .conftest import (
    assert_containers_running,
    VALIDATOR1_KEY,
    ALL_CONTAINERS,
)
from .rnode import Node

pytestmark = pytest.mark.xdist_group("shard")


def _load_bridge_contract() -> str:
    """Load bridge.rho from integration-tests/resources/bridge.rho."""
    integration_tests_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(integration_tests_dir, "resources", "bridge.rho")
    with open(path) as f:
        return f.read()


# Patterns that indicate replay divergence between validators.
REPLAY_ERROR_PATTERNS = [
    (re.compile(r"ReplayCostMismatch"), "ReplayCostMismatch"),
    (re.compile(r"Index out of bounds when removing datum"), "remove_datum OOB"),
    (re.compile(r"InvalidBlock"), "InvalidBlock"),
    (re.compile(r"NeglectedInvalidBlock"), "NeglectedInvalidBlock"),
]

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


def _scan_logs_for_replay_errors(nodes: List[Node]) -> List[str]:
    """Scan all node logs for replay/consensus error patterns.

    Returns a list of error descriptions found. Empty list means no errors.
    """
    errors = []
    for node in nodes:
        node_logs = node.logs()
        for pattern, description in REPLAY_ERROR_PATTERNS:
            matches = pattern.findall(node_logs)
            if matches:
                errors.append(
                    f"[{node.name}] {description}: {len(matches)} occurrence(s)"
                )
    return errors


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


def _assert_lfb_advances(node: Node, target_block: int, timeout: float):
    """Assert that LFB advances to at least target_block within timeout.

    Polls last_finalized_block every 5s. Raises AssertionError if LFB
    doesn't reach the target.
    """
    deadline = time.time() + timeout
    current = 0
    while time.time() < deadline:
        try:
            lfb = node.last_finalized_block()
            current = lfb.blockInfo.blockNumber
            if current >= target_block:
                logging.info("LFB advanced to #%d", current)
                return
        except Exception:
            pass
        time.sleep(5)
    raise AssertionError(
        f"Network stalled: LFB stuck at #{current}, expected at least #{target_block} "
        f"within {timeout}s"
    )


# ---------------------------------------------------------------------------
# Test 1: Replay determinism with duplicate channel sends
# ---------------------------------------------------------------------------

def test_duplicate_sends_accepted_by_all_validators(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
    validator2_node: Node,
    validator3_node: Node,
    bootstrap_node: Node,
    readonly_node: Node,
) -> None:
    """Deploy bridge.rho and verify all validators replay the block
    with identical costs.

    bridge.rho has duplicate channel sends (requiredSigsCh!(2) twice,
    oracleCountCh!(3) twice). This exercises:
    - remove_datum with out-of-bounds indices (hot_store.rs)
    - COMM matching for join consumes with duplicate produces
    - Evaluation order consistency between block creator and replayers
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    all_nodes = [
        bootstrap_node, validator1_node, validator2_node,
        validator3_node, readonly_node,
    ]

    deploy_id = validator1_node.deploy_string(
        _load_bridge_contract(),
        VALIDATOR1_KEY,
        phlo_limit=500_000_000,
        phlo_price=1,
    )
    logging.info("Deployed bridge.rho, deploy_id=%s", deploy_id[:24])

    find_timeout = int(60 * testing_context.timeout_scale)
    _wait_for_deploy_in_block(validator1_node, deploy_id, find_timeout)

    # Wait for cross-validator replay (~30s for block propagation + validation)
    logging.info("Waiting 30s for cross-validator replay...")
    time.sleep(30)

    errors = _scan_logs_for_replay_errors(all_nodes)

    assert len(errors) == 0, (
        "Replay errors detected after deploying bridge.rho:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )


# ---------------------------------------------------------------------------
# Test 2: Shard recovery after block rejection
# ---------------------------------------------------------------------------

def test_network_continues_after_duplicate_sends_deploy(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
) -> None:
    """Deploy bridge.rho and verify the shard does not stall.

    Even if a block is rejected by replaying validators, the network must
    continue finalizing. LFB must advance at least 3 blocks past the deploy.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    baseline_lfb = validator1_node.last_finalized_block()
    baseline = baseline_lfb.blockInfo.blockNumber
    logging.info("Baseline LFB: block #%d", baseline)

    deploy_id = validator1_node.deploy_string(
        _load_bridge_contract(),
        VALIDATOR1_KEY,
        phlo_limit=500_000_000,
        phlo_price=1,
    )
    logging.info("Deployed bridge.rho, deploy_id=%s", deploy_id[:24])

    find_timeout = int(60 * testing_context.timeout_scale)
    _, deploy_block = _wait_for_deploy_in_block(
        validator1_node, deploy_id, find_timeout,
    )

    advance_timeout = int(120 * testing_context.timeout_scale)
    logging.info(
        "Waiting for LFB to advance past deploy block #%d (timeout=%ds)...",
        deploy_block, advance_timeout,
    )
    _assert_lfb_advances(
        validator1_node, deploy_block + 3, advance_timeout,
    )


# ---------------------------------------------------------------------------
# Test 3: Shard recovery from validator pause (DAG tip divergence)
# ---------------------------------------------------------------------------

def test_network_recovers_from_validator_pause(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
) -> None:
    """Pause validator1 for 15s to force DAG tip divergence, then verify
    the network converges and LFB advances.

    While validator1 is paused, other validators create blocks via heartbeat.
    After unpause, the validators exchange tips and must propose multi-parent
    convergence blocks to merge the diverged forks.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    baseline_lfb = validator1_node.last_finalized_block()
    baseline = baseline_lfb.blockInfo.blockNumber
    logging.info("Baseline LFB: block #%d", baseline)

    container = docker_client.containers.get("rnode.validator1")
    logging.info("Pausing validator1 for 15s to force DAG divergence...")
    container.pause()
    time.sleep(15)
    container.unpause()
    logging.info("Validator1 unpaused. Waiting for network convergence...")

    advance_timeout = int(120 * testing_context.timeout_scale)
    _assert_lfb_advances(
        validator1_node, baseline + 3, advance_timeout,
    )


# ---------------------------------------------------------------------------
# Test 4: Shard recovery from slow deploy (DAG tip divergence)
# ---------------------------------------------------------------------------

def test_network_recovers_from_slow_deploy(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
) -> None:
    """Deploy a phlo-exhausting loop and verify the shard recovers.

    The loop contract blocks the proposing validator for ~100s while phlo
    is exhausted. Other validators create independent blocks via heartbeat,
    causing DAG tip divergence. After the deploy completes (errored), the
    network must converge and LFB must advance.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    baseline_lfb = validator1_node.last_finalized_block()
    baseline = baseline_lfb.blockInfo.blockNumber
    logging.info("Baseline LFB: block #%d", baseline)

    deploy_id = validator1_node.deploy_string(
        SLOW_LOOP_CONTRACT,
        VALIDATOR1_KEY,
        phlo_limit=20_000_000,
        phlo_price=1,
    )
    logging.info("Deployed loop contract, deploy_id=%s", deploy_id[:24])

    # 20M phlo with loop!(100000) takes ~100s to exhaust, long enough for
    # other validators to diverge via heartbeat.
    find_timeout = int(180 * testing_context.timeout_scale)
    _, deploy_block = _wait_for_deploy_in_block(
        validator1_node, deploy_id, find_timeout,
    )

    # Lower target than test 2: the phlo-exhausting deploy blocks V1 for ~100s,
    # leaving less time for convergence within the timeout window.
    advance_timeout = int(120 * testing_context.timeout_scale)
    logging.info(
        "Waiting for LFB to advance past deploy block #%d (timeout=%ds)...",
        deploy_block, advance_timeout,
    )
    _assert_lfb_advances(
        validator1_node, deploy_block + 2, advance_timeout,
    )

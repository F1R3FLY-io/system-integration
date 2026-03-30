"""
Replay Determinism and Network Resilience Integration Tests

Tests that verify:
1. Cross-validator replay determinism (no ReplayCostMismatch from duplicate
   channel sends in bridge.rho — RSpace remove_datum OOB bug)
2. Network does not permanently stall after a rejected block
3. Network recovers from temporary validator pause (DAG tip divergence)
4. Network recovers from a slow/phlo-exhausting deploy

Tests 1-2 currently FAIL due to the remove_datum bug in
rspace++/src/rspace/hot_store.rs:378.

Tests 3-4 currently FAIL due to the DAG tip divergence recovery gap
(f1r3node#437, f1r3node#224).

See docs/TODO.md for full analysis of each bug.
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


# Patterns that indicate replay divergence
REPLAY_ERROR_PATTERNS = [
    (re.compile(r"ReplayCostMismatch"), "ReplayCostMismatch"),
    (re.compile(r"Index out of bounds when removing datum"), "remove_datum OOB"),
    (re.compile(r"InvalidBlock"), "InvalidBlock"),
    (re.compile(r"NeglectedInvalidBlock"), "NeglectedInvalidBlock"),
]

# Contract that exhausts phlo by looping. From f1r3node#224.
# With phlo_limit=500M and loop!(1000000000), this will run until phlo is
# exhausted. During execution, the proposing validator is busy and other
# validators create independent blocks via heartbeat, causing DAG divergence.
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
# Tests 1-2: RSpace replay determinism (bridge.rho duplicate sends)
# Bug: rspace++/src/rspace/hot_store.rs:378 (remove_datum OOB)
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
    """Deploy bridge.rho (has duplicate channel sends) and verify all validators
    accept the block without ReplayCostMismatch.

    bridge.rho sends requiredSigsCh!(2) twice and oracleCountCh!(3) twice.
    The block creator and replayers must handle remove_datum identically.

    EXPECTED: Fails today due to remove_datum OOB bug (hot_store.rs:378).
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


def test_network_continues_after_duplicate_sends_deploy(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
) -> None:
    """Deploy bridge.rho and verify the network does not stall.

    When validators reject a block (ReplayCostMismatch), they stop proposing
    and the network permanently halts. This asserts LFB advances at least 3
    blocks after the deploy.

    EXPECTED: Fails today — rejected blocks cause permanent network stall.
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
# Tests 3-4: Network resilience / DAG tip divergence recovery
# Bugs: f1r3node#437 (DAG divergence), f1r3node#224 (slow deploy stall)
# ---------------------------------------------------------------------------

def test_network_recovers_from_validator_pause(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
) -> None:
    """Pause validator1 for 15s to force DAG tip divergence, then verify
    the network converges and LFB advances.

    While validator1 is paused, other validators create blocks via heartbeat.
    When validator1 resumes, its tip diverges from the other validators.
    The network must converge: validators should propose blocks that justify
    all known tips, and LFB should advance.

    Related: f1r3node#437 (lists "Docker container pause/unpause" as trigger)
    EXPECTED: Fails today — network permanently stalls after divergence.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    baseline_lfb = validator1_node.last_finalized_block()
    baseline = baseline_lfb.blockInfo.blockNumber
    logging.info("Baseline LFB: block #%d", baseline)

    # Pause validator1 — other validators continue heartbeating
    container = docker_client.containers.get("rnode.validator1")
    logging.info("Pausing validator1 for 15s to force DAG divergence...")
    container.pause()
    time.sleep(15)
    container.unpause()
    logging.info("Validator1 unpaused. Waiting for network convergence...")

    # After unpause, the network should converge and LFB should advance.
    # We expect at least 3 blocks beyond baseline within 120s.
    advance_timeout = int(120 * testing_context.timeout_scale)
    _assert_lfb_advances(
        validator1_node, baseline + 3, advance_timeout,
    )


def test_network_recovers_from_slow_deploy(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
) -> None:
    """Deploy a phlo-exhausting loop contract and verify the network does
    not permanently stall.

    The loop!(1000000000) contract runs until phlo is exhausted. During
    execution, the proposing validator is busy and other validators create
    independent blocks via heartbeat, causing DAG tip divergence. After the
    deploy completes (errored due to phlo exhaustion), the network must
    recover and LFB must continue advancing.

    Related: f1r3node#224 (exceeding phlo_limit deploy stalls shard)
    Related: f1r3node#437 (DAG tip divergence from long-running deploy)
    EXPECTED: Fails today — network stalls after validator desynchronization.
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

    # Wait for the loop deploy to be included in a block.
    # The loop runs until phlo is exhausted. 1M phlo exhausts in ~10-30s,
    # long enough for other validators to diverge via heartbeat but short enough
    # to complete within the test timeout.
    find_timeout = int(180 * testing_context.timeout_scale)
    _, deploy_block = _wait_for_deploy_in_block(
        validator1_node, deploy_id, find_timeout,
    )

    # After the deploy is included (errored or not), LFB should advance.
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

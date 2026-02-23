"""
Propose / Deploy Integration Tests

Tests for deploy validation (invalid contracts, phlo price enforcement) and
cross-validator deploy-to-block lookup in a multi-validator shard.

Standalone tests use a fresh node via docker-compose (start_standalone_node).
Shard tests use the session-scoped shard fixture (shared across the suite).
"""

import logging
import os
import time

import pytest
from f1r3fly.client import F1r3flyClientException
from f1r3fly.crypto import PrivateKey
from docker.client import DockerClient

from .common import (
    CommandLineOptions,
    ParsingError,
    TestingContext,
)
from .conftest import (
    assert_containers_running,
    start_standalone_node,
    STANDALONE_PRIVATE_KEY,
    VALIDATOR1_KEY,
    ALL_CONTAINERS,
)
from .rnode import Node


STANDALONE_KEY = PrivateKey.from_hex(STANDALONE_PRIVATE_KEY)


def _resources_path(relative: str) -> str:
    """Resolve a path relative to integration-tests/resources/."""
    tests_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(tests_dir, "resources", relative)


def _poll_find_deploy(node: Node, deploy_id: str, timeout: int = 30, scale: float = 1.0):
    """Poll find_deploy until the block containing the deploy is committed.

    Returns the LightBlockInfo for the block that includes the deploy.
    Raises AssertionError if the deploy is not found within the timeout.
    """
    scaled_timeout = int(timeout * scale)
    deadline = time.time() + scaled_timeout
    while time.time() < deadline:
        try:
            return node.find_deploy(deploy_id)
        except F1r3flyClientException:
            logging.info(
                "find_deploy: block not committed yet for %s, retrying...",
                deploy_id[:24],
            )
            time.sleep(2)
    raise AssertionError(
        f"Block containing deploy {deploy_id[:24]}... not found within {scaled_timeout}s"
    )


# ===========================================================================
# SHARD TESTS
# ===========================================================================


@pytest.mark.xdist_group("shard")
def test_deploy_invalid_contract(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
) -> None:
    """Deploying syntactically invalid Rholang must be rejected at the API level.

    After the rejection, deploying a valid contract must succeed normally --
    the prior failure should not poison the deploy pipeline.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    # Read the intentionally broken Rholang (syntax error: trailing comma)
    with open(_resources_path("invalid.rho"), "r") as f:
        invalid_content = f.read()

    # The node must reject the invalid contract with a ParsingError
    with pytest.raises(ParsingError):
        validator1_node.deploy_string(invalid_content, VALIDATOR1_KEY)

    # A subsequent valid deploy must succeed
    deploy_id = validator1_node.deploy_string(
        '@"valid-after-invalid"!(42)',
        VALIDATOR1_KEY,
        phlo_limit=100_000_000,
        phlo_price=1,
    )

    # Wait for heartbeat to include the valid deploy in a block
    block_info = _poll_find_deploy(validator1_node, deploy_id, scale=testing_context.timeout_scale)
    block_hash = block_info.blockHash

    # Verify the block contains our valid deploy.  The heartbeat proposer
    # may batch deploys from earlier tests into the same block, so we check
    # for presence rather than an exact count.
    full_block = validator1_node.get_block(block_hash)
    deploy_terms = [d.term for d in full_block.deploys]
    assert any('@"valid-after-invalid"!(42)' in t for t in deploy_terms), (
        f"Block should contain the valid deploy, but found terms: "
        f"{[t[:40] for t in deploy_terms]}"
    )


# ===========================================================================
# STANDALONE TESTS
# ===========================================================================


@pytest.mark.xdist_group("standalone")
def test_deploy_phlo_price_too_small(
    command_line_options: CommandLineOptions,
    docker_client: DockerClient,
) -> None:
    """Node must reject deploys whose phlo price is below the configured minimum.

    Starts a standalone node with --min-phlo-price=10, then attempts a deploy
    with phlo_price=1. The gRPC API must reject it immediately with an
    F1r3flyClientException containing the price mismatch details.
    """
    with start_standalone_node(
        docker_client=docker_client,
        command_line_options=command_line_options,
        cli_options={"--min-phlo-price": "10"},
    ) as node:
        with pytest.raises(
            F1r3flyClientException,
            match=r"Phlo price 1 is less than minimum price 10",
        ):
            node.deploy_string(
                '@"phlo-price-test"!(1)',
                STANDALONE_KEY,
                phlo_limit=100_000_000,
                phlo_price=1,
            )


# ===========================================================================
# SHARD CROSS-VALIDATOR TESTS
# ===========================================================================


@pytest.mark.xdist_group("shard")
def test_find_block_by_deploy_id_shard(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
    validator2_node: Node,
) -> None:
    """Deploy-to-block lookup must be consistent across validators.

    Deploys a contract on validator1, waits for heartbeat to include it in a
    block, then verifies that both validator1 and validator2 resolve the same
    deploy ID to the same block hash. This confirms deploy indexing and block
    propagation are working correctly in the multi-parent DAG.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    deploy_id = validator1_node.deploy_string(
        '@"find-block-shard-test"!(1)',
        VALIDATOR1_KEY,
        phlo_limit=100_000_000,
        phlo_price=1,
    )

    # Wait for the deploy to land in a block on validator1
    v1_block = _poll_find_deploy(validator1_node, deploy_id, scale=testing_context.timeout_scale)
    v1_hash = v1_block.blockHash

    logging.info(
        "Deploy %s found in block %s on validator1",
        deploy_id[:24], v1_hash[:16],
    )

    # The same block must be visible on validator2 after propagation
    v2_block = _poll_find_deploy(validator2_node, deploy_id, scale=testing_context.timeout_scale)
    v2_hash = v2_block.blockHash

    logging.info(
        "Deploy %s found in block %s on validator2",
        deploy_id[:24], v2_hash[:16],
    )

    assert v1_hash == v2_hash, (
        f"Deploy {deploy_id[:24]}... resolved to different blocks: "
        f"validator1={v1_hash[:16]}, validator2={v2_hash[:16]}"
    )

    assert_containers_running(docker_client, ALL_CONTAINERS)

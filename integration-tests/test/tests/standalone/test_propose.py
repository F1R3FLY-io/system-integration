"""
Deploy Validation Standalone Integration Tests

Tests for deploy validation on a standalone node with custom config.
Requires a custom --min-phlo-price setting, so cannot run on the shared shard.
"""

import logging

import pytest
from f1r3fly.client import F1r3flyClientException

from ...infra.config import NodeConfig
from ...infra.keys import BOOTSTRAP_ID
from ...infra.node import Node
from ...infra.polling import wait_for_deploy_included
from ...infra.types import NodeRole

# No xdist_group — each test creates/destroys its own node, safe to parallelize


def test_deploy_phlo_price_too_small(provider, timeouts) -> None:
    """Node rejects deploys below configured min phlo price, accepts at threshold.

    1. Start standalone node with --min-phlo-price=10
    2. Verify /api/status reports minPhloPrice=10
    3. Deploy with phlo_price=1 — rejected with error message
    4. Deploy with phlo_price=10 — accepted and included in block
    """
    config = NodeConfig(
        role=NodeRole.STANDALONE,
        cli_options={"--min-phlo-price": "10"},
    )
    handle = provider.create_standalone(config)
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    try:
        # Verify API reports the configured min phlo price
        status = node.api_get("/status")
        assert status["minPhloPrice"] == 10, (
            f"Expected minPhloPrice=10, got {status['minPhloPrice']}"
        )
        logging.info("Node reports minPhloPrice=%d", status["minPhloPrice"])

        # Deploy below threshold — must be rejected
        with pytest.raises(
            F1r3flyClientException,
            match=r"Phlo price 1 is less than minimum price 10",
        ):
            node.deploy_string(
                '@"phlo-price-test-reject"!(1)',
                BOOTSTRAP_ID.private_key(),
                phlo_limit=100_000_000,
                phlo_price=1,
            )
        logging.info("Deploy with phlo_price=1 correctly rejected")

        # Deploy at threshold — must succeed
        deploy_id = node.deploy_string(
            '@"phlo-price-test-accept"!(1)',
            BOOTSTRAP_ID.private_key(),
            phlo_limit=100_000_000,
            phlo_price=10,
        )
        block_info = wait_for_deploy_included(node, deploy_id, timeouts.deploy_inclusion)
        logging.info(
            "Deploy with phlo_price=10 accepted, included in block #%d",
            block_info.blockNumber,
        )
    finally:
        node.close()
        provider.destroy_standalone(handle)

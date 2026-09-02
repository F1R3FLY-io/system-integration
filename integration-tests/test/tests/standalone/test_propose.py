"""
Protocol-v6 Deploy Validation Standalone Integration Tests

Tests the retained economic margin on a standalone node with custom configuration.
"""

import logging

from ...infra.config import NodeConfig
from ...infra.keys import BOOTSTRAP_ID
from ...infra.node import Node
from ...infra.polling import wait_for_deploy_included
from ...infra.types import NodeRole

# No xdist_group — each test creates/destroys its own node, safe to parallelize


def test_protocol_v6_deploy_has_no_client_price_gate(provider, timeouts) -> None:
    """A protocol-v6 deploy has no client-selected price or limit.

    1. Start standalone node with --min-phlo-price=10
    2. Verify /api/status reports minPhloPrice=10
    3. Submit a protocol-v6 deploy without retired phlo fields
    4. Verify the authority-funded deploy enters a block
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

        deploy_id = node.deploy_string(
            '@"protocol-v6-price-independent"!(1)',
            BOOTSTRAP_ID.private_key(),
        )
        block_info = wait_for_deploy_included(node, deploy_id, timeouts.deploy_inclusion)
        logging.info(
            "Authority-funded protocol-v6 deploy entered block #%d",
            block_info.blockNumber,
        )
    finally:
        node.close()
        provider.destroy_standalone(handle)

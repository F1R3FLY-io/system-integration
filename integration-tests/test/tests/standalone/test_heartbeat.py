"""
Heartbeat Standalone Integration Tests

Tests for heartbeat behavior on a single standalone node:
  - Block creation when idle (no deploys)
  - Heartbeat disabled when max-number-of-parents=1

Standalone tests create/destroy their own nodes. No shared shard.
"""

import logging

from ...infra.config import NodeConfig
from ...infra.node import Node
from ...infra.polling import poll_until
from ...infra.types import NodeRole

# No xdist_group — each test creates/destroys its own node, safe to parallelize


def _create_standalone_heartbeat_node(
    provider,
    timeouts,
    heartbeat_enabled: bool = True,
    heartbeat_check_interval: int = 5,
    heartbeat_max_lfb_age: int = 3,
    max_number_of_parents: int = 10,
) -> tuple:
    """Create a standalone node with heartbeat config. Returns (handle, node)."""
    cli_options = {
        "--heartbeat-check-interval": f"{heartbeat_check_interval}seconds",
        "--heartbeat-max-lfb-age": f"{heartbeat_max_lfb_age}seconds",
        "--max-number-of-parents": str(max_number_of_parents),
    }
    cli_flags = set()
    if heartbeat_enabled:
        cli_flags.add("--heartbeat-enabled")
    else:
        cli_options["--heartbeat-enabled"] = "false"

    config = NodeConfig(
        role=NodeRole.STANDALONE,
        cli_flags=frozenset(cli_flags),
        cli_options=cli_options,
    )
    handle = provider.create_standalone(config)
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    return handle, node


def test_heartbeat_creates_blocks_when_idle(provider, node_conf, timeouts) -> None:
    """Heartbeat automatically creates blocks on an idle standalone node.

    1. Node starts with heartbeat enabled
    2. Heartbeat logs initialization message
    3. Multiple blocks created beyond genesis (>= 4: genesis + 3 heartbeat)
    4. Log contains >= 3 successful heartbeat creation entries
    5. No 'has not made progress' regression error
    6. Block shardId matches node config
    """
    handle, node = _create_standalone_heartbeat_node(provider, timeouts)
    try:
        # Wait for heartbeat to produce blocks
        def _enough_heartbeat_blocks():
            blocks = node.get_blocks(10)
            block_count = len(blocks)
            logs = node.logs()
            success_count = logs.count("Heartbeat: Successfully created block")
            if block_count >= 4 and success_count >= 3:
                return block_count, success_count
            return None

        block_count, success_count = poll_until(
            predicate=_enough_heartbeat_blocks,
            timeout=timeouts.finalization * 6,
            interval=5.0,
            description="heartbeat creates 4+ blocks with 3+ success logs",
        )

        logs = node.logs()

        # Heartbeat startup message
        assert (
            "Heartbeat: Starting with random initial delay" in logs
        ), "Should log heartbeat startup message"

        # Block count
        assert block_count >= 4, f"Expected >= 4 blocks (genesis + 3 heartbeat), got {block_count}"

        # Success log entries
        assert success_count >= 3, f"Expected >= 3 heartbeat success logs, got {success_count}"

        # No regression error
        assert (
            "has not made progress" not in logs
        ), "Should NOT see 'has not made progress' error in standalone mode"

        # Verify a block's shardId matches config
        blocks = node.get_blocks(5)
        for b in blocks:
            if b.blockNumber > 0:
                assert b.shardId == node_conf.shard_id, (
                    f"Block #{b.blockNumber} shardId '{b.shardId}' != "
                    f"config '{node_conf.shard_id}'"
                )
                break

        logging.info(
            "Standalone heartbeat verified: %d blocks, %d success logs, shardId=%s",
            block_count,
            success_count,
            node_conf.shard_id,
        )
    finally:
        node.close()
        provider.destroy_standalone(handle)


def test_heartbeat_disabled_when_max_parents_is_one(provider, timeouts) -> None:
    """Heartbeat is disabled with warning when max-number-of-parents=1.

    1. Node starts with heartbeat enabled but max-parents=1
    2. Node logs configuration error about incompatible settings
    3. No new blocks created for 15s (heartbeat effectively disabled)
    """
    handle, node = _create_standalone_heartbeat_node(
        provider,
        timeouts,
        heartbeat_enabled=True,
        max_number_of_parents=1,
    )
    try:
        logs = node.logs()
        assert (
            "Heartbeat incompatible with max-number-of-parents=1" in logs
            or "CONFIGURATION ERROR" in logs
        ), "Should log warning about max-number-of-parents=1 incompatibility"

        initial_count = len(node.get_blocks(10))
        logging.info("Initial block count: %d (expecting no change)", initial_count)

        # Wait 15s and verify no new blocks appear
        import time

        time.sleep(15)

        final_count = len(node.get_blocks(10))
        assert final_count == initial_count, (
            f"Heartbeat should be disabled with max-parents=1: "
            f"block count changed from {initial_count} to {final_count}"
        )

        logging.info(
            "Heartbeat correctly disabled: %d blocks before and after 15s wait",
            initial_count,
        )
    finally:
        node.close()
        provider.destroy_standalone(handle)

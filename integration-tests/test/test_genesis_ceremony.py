"""
Genesis Ceremony Integration Test

Validates that the shard's genesis ceremony completed successfully.
The ceremony is performed implicitly by docker-compose startup:
  - bootstrap-ceremony.conf has required-signatures=2, ceremony-master-mode=true
  - Validators have genesis-validator-mode=true
  - The shard fixture waits for all nodes to reach Running state

This test verifies the ceremony results post-startup rather than
orchestrating the ceremony in real-time.
"""

import logging
import re

import pytest
from docker.client import DockerClient

from .conftest import (
    ALL_CONTAINERS,
    assert_containers_running,
)
from .rnode import Node

pytestmark = pytest.mark.xdist_group("shard")

log = logging.getLogger(__name__)

# Matches "Approved Block #0 (cb0d904a0d...) with empty parents" in logs
_GENESIS_HASH_PATTERN = re.compile(r"Approved Block #0 \(([a-f0-9]+)\.\.\.\) with empty parents")


def test_successful_genesis_ceremony(
    docker_client: DockerClient,
    bootstrap_node: Node,
    validator1_node: Node,
    validator2_node: Node,
    validator3_node: Node,
    readonly_node: Node,
) -> None:
    """Verify genesis ceremony completed successfully across all nodes.

    The shard fixture guarantees all 5 nodes reached Running state, which
    requires the ceremony protocol to complete (required-signatures=2 in
    bootstrap-ceremony.conf). This test validates the resulting state:

    1. All containers are running
    2. All nodes have at least 1 block (the genesis block)
    3. All nodes share the same genesis block hash (extracted from logs)
    4. Genesis block can be fetched and has no parents
    5. Ceremony-related logs are present on bootstrap
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    all_nodes = [bootstrap_node, validator1_node, validator2_node, validator3_node, readonly_node]

    # All nodes should have at least 1 block (genesis)
    for node in all_nodes:
        block_count = node.get_blocks_count(5)
        assert (
            block_count >= 1
        ), f"{node.name}: expected at least 1 block (genesis), got {block_count}"

    # Extract genesis block hash from each node's logs.
    # The log line "Approved Block #0 (<hash>...) with empty parents" is
    # emitted during ceremony completion and reliably identifies genesis.
    genesis_hashes = {}
    for node in all_nodes:
        match = _GENESIS_HASH_PATTERN.search(node.logs())
        assert match is not None, (
            f"{node.name}: could not find genesis block hash in logs "
            f"(expected 'Approved Block #0 (...) with empty parents')"
        )
        genesis_hash_prefix = match.group(1)
        genesis_hashes[node.name] = genesis_hash_prefix
        log.info("%s: genesis hash prefix = %s", node.name, genesis_hash_prefix)

    # All nodes must agree on the genesis block hash prefix
    unique_prefixes = set(genesis_hashes.values())
    assert (
        len(unique_prefixes) == 1
    ), f"All nodes should share the same genesis hash, but got: {genesis_hashes}"

    # Fetch the full genesis block and verify it has no parents
    genesis_prefix = unique_prefixes.pop()

    # Find the full genesis hash via show_blocks -- look for the hash starting
    # with the prefix we extracted from logs
    blocks = bootstrap_node.get_blocks(100)
    genesis_block_info = None
    for b in blocks:
        if b.blockHash.startswith(genesis_prefix):
            genesis_block_info = b
            break

    if genesis_block_info is not None:
        assert (
            len(genesis_block_info.parentsHashList) == 0
        ), f"Genesis block should have no parents, got: {list(genesis_block_info.parentsHashList)}"
    else:
        # Genesis may not appear in show_blocks if depth doesn't reach it.
        # Fall back to show_block with the prefix.
        genesis_block = bootstrap_node.get_block(genesis_prefix)
        assert len(genesis_block.blockInfo.parentsHashList) == 0, (
            f"Genesis block should have no parents, got: "
            f"{list(genesis_block.blockInfo.parentsHashList)}"
        )

    # Verify ceremony logs on bootstrap (ceremony master)
    boot_logs = bootstrap_node.logs()
    assert (
        "Making a transition to Running state" in boot_logs
    ), "Bootstrap should have reached Running state"

"""
Genesis Ceremony Integration Test

Validates that a shard's genesis ceremony completed successfully.
The ceremony is performed implicitly by Shard.create():
  - Bootstrap uses --ceremony-master-mode and --required-signatures via CLI
  - Validators use --genesis-validator via CLI
  - Shard.create() waits for all nodes to reach Running state

This test verifies the ceremony results post-startup rather than
orchestrating the ceremony in real-time. Expected values are derived
from the shard's ShardConfig and node_conf fixture.
"""

import logging

import pytest

pytestmark = pytest.mark.xdist_group("shared")

log = logging.getLogger(__name__)


def test_successful_genesis_ceremony(shared_shard, node_conf) -> None:
    """Verify genesis ceremony completed successfully across all nodes.

    1. All nodes report the same genesis block hash
    2. Genesis block has no parents
    3. Genesis block shardId matches node config
    4. Genesis block bonds match shard config (count and stakes)
    """
    all_nodes = shared_shard.all_nodes
    config = shared_shard.config

    # Get genesis block from each node by fetching deep enough to find block #0
    genesis_hashes = {}
    for node in all_nodes:
        blocks = node.get_blocks(100)
        genesis = None
        for b in blocks:
            if b.blockNumber == 0:
                genesis = b
                break
        assert genesis is not None, f"{node.name}: could not find block #0 in get_blocks(100)"
        genesis_hashes[node.name] = genesis.blockHash
        log.info("%s: genesis hash = %s", node.name, genesis.blockHash[:16])

    # All nodes must agree on the full genesis block hash
    unique_hashes = set(genesis_hashes.values())
    assert len(unique_hashes) == 1, f"Nodes disagree on genesis block hash: {genesis_hashes}"

    # Fetch full genesis block for detailed checks
    genesis_hash = unique_hashes.pop()
    genesis_block = shared_shard.boot.get_block(genesis_hash)
    genesis_info = genesis_block.blockInfo

    # Genesis block has no parents
    assert len(genesis_info.parentsHashList) == 0, (
        f"Genesis block should have no parents, got: " f"{list(genesis_info.parentsHashList)}"
    )

    # shardId matches node config
    assert genesis_info.shardId == node_conf.shard_id, (
        f"Genesis block shardId '{genesis_info.shardId}' != "
        f"config shard_id '{node_conf.shard_id}'"
    )

    # Bonds match shard config
    expected_bonds = {identity.public_hex: stake for identity, stake in config.bonds}
    actual_bonds = {b.validator: b.stake for b in genesis_info.bonds}

    assert len(actual_bonds) == len(expected_bonds), (
        f"Genesis block has {len(actual_bonds)} bonds, "
        f"expected {len(expected_bonds)} from shard config"
    )

    for pubkey, expected_stake in expected_bonds.items():
        assert pubkey in actual_bonds, f"Validator {pubkey[:24]}... not found in genesis bonds"
        assert actual_bonds[pubkey] == expected_stake, (
            f"Validator {pubkey[:24]}... has stake {actual_bonds[pubkey]}, "
            f"expected {expected_stake} from shard config"
        )

    logging.info(
        "Genesis ceremony verified: %d nodes, hash %s, shardId=%s, %d bonds",
        len(all_nodes),
        genesis_hash[:16],
        node_conf.shard_id,
        len(actual_bonds),
    )

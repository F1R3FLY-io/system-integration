"""
Storage Integration Tests

Tests for storing and retrieving data via the Rholang registry.
Uses the session-scoped shard fixture with heartbeat-driven block creation.

Each test generates a unique random string and builds data-specific regex
patterns so that log matching works correctly across session-scoped containers
where logs accumulate between tests.
"""

import logging
import os
import re
import time

import pytest
from docker.client import DockerClient
from f1r3fly.client import F1r3flyClientException

from .common import (
    TestingContext,
    random_string,
)
from .conftest import (
    ALL_CONTAINERS,
    VALIDATOR1_KEY,
    VALIDATOR2_KEY,
    assert_containers_running,
)
from .rnode import Node
from .wait import wait_for_log_match

pytestmark = pytest.mark.xdist_group("shard")


STORE_DATA_CONTRACT = os.path.join("resources", "storage", "store-data.rho")
READ_DATA_CONTRACT = os.path.join("resources", "storage", "read-data.rho")


def _store_pattern_for(data: str) -> re.Pattern:
    """Build a store regex specific to the given data string."""
    return re.compile(rf'"Store data {re.escape(data)} in rho:id:(?P<id_address>[a-zA-Z0-9]+)"')


def _read_pattern_for(id_address: str) -> re.Pattern:
    """Build a read regex specific to the given registry ID."""
    return re.compile(rf'"Read data (?P<data>[a-zA-Z]+) from {re.escape(id_address)}"')


def test_data_is_stored_and_served_by_node(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
) -> None:
    """Store data via registry on a validator and read it back on the same node.

    1. Deploy store-data.rho with random data on validator1
    2. Wait for log output confirming storage with registry ID
    3. Deploy read-data.rho with the registry ID on validator1
    4. Wait for log output confirming read
    5. Assert stored data matches read data
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    context = testing_context
    node = validator1_node

    random_data = random_string(context, 20)
    store_pattern = _store_pattern_for(random_data)

    # Deploy store contract -- heartbeat will auto-propose
    node.deploy_contract_with_substitution(
        {"@store_data@": random_data},
        STORE_DATA_CONTRACT,
        VALIDATOR1_KEY,
    )

    # Wait for this specific store confirmation in logs
    wait_for_log_match(context, node, store_pattern)

    store_match = store_pattern.search(node.logs())
    assert store_match is not None, "Store pattern should match after wait_for_log_match"

    id_address = store_match.group("id_address")
    read_pattern = _read_pattern_for(id_address)

    # Deploy read contract with the registry ID -- heartbeat will auto-propose
    node.deploy_contract_with_substitution(
        {"@id_address@": id_address},
        READ_DATA_CONTRACT,
        VALIDATOR1_KEY,
    )

    # Wait for this specific read confirmation in logs
    wait_for_log_match(context, node, read_pattern)

    read_match = read_pattern.search(node.logs())
    assert read_match is not None, "Read pattern should match after wait_for_log_match"

    read_data = read_match.group("data")
    assert (
        read_data == random_data
    ), f"Read data '{read_data}' should match stored data '{random_data}'"


def test_data_stored_on_one_validator_served_by_another(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
    validator2_node: Node,
) -> None:
    """Store data on validator1 and read it back on validator2.

    Tests cross-node state propagation: data stored via the registry on one
    validator should be readable on another after block propagation.

    1. Deploy store-data.rho on validator1
    2. Wait for store confirmation on validator1
    3. Use find_deploy to get the store block number
    4. Wait for store block to propagate to validator2
    5. Deploy read-data.rho on validator2 with validAfterBlockNumber set to
       the store block number (ensures the read executes in a block whose
       merge base includes the store's registry state)
    6. Wait for read confirmation on validator2
    7. Assert data matches
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    context = testing_context

    random_data = random_string(context, 20)
    store_pattern = _store_pattern_for(random_data)

    # Store on validator1
    store_deploy_id = validator1_node.deploy_contract_with_substitution(
        {"@store_data@": random_data},
        STORE_DATA_CONTRACT,
        VALIDATOR1_KEY,
    )

    wait_for_log_match(context, validator1_node, store_pattern)

    store_match = store_pattern.search(validator1_node.logs())
    assert store_match is not None
    id_address = store_match.group("id_address")

    # Get the block number that included the store deploy. The read deploy
    # must set validAfterBlockNumber to this value so it is only included
    # in a block whose parents are at or after the store block, guaranteeing
    # the registry state is in the merge base.
    #
    # The Rholang stdout output appears in the Docker logs during block
    # creation (deploy execution), before the block is committed to the
    # blockstore. Poll find_deploy until the block is committed.
    find_timeout = int(30 * context.timeout_scale)
    deadline = time.time() + find_timeout
    store_block = None
    while time.time() < deadline:
        try:
            store_block = validator1_node.find_deploy(store_deploy_id)
            break
        except F1r3flyClientException:
            logging.info("find_deploy: block not committed yet, retrying...")
            time.sleep(2)
    assert (
        store_block is not None
    ), f"Block containing store deploy {store_deploy_id[:24]}... not found within {find_timeout}s"
    store_block_number = store_block.blockNumber

    # Wait for the store block to propagate to validator2.
    # When validator2 replays the block from validator1, the store contract's
    # stdout output appears in validator2's logs.
    wait_for_log_match(context, validator2_node, store_pattern)

    # Wait for the store block to be FINALIZED on validator2. In a multi-proposer
    # DAG, validAfterBlockNumber provides sequencing but not state visibility.
    # Only finalization guarantees the store block's state is in the LCA base
    # of all future blocks. Without this wait, the read deploy may execute
    # against a merge base that does not include the store block's state,
    # causing rho:registry:lookup to return nothing.
    lfb_timeout = int(180 * context.timeout_scale)
    lfb_deadline = time.time() + lfb_timeout
    lfb_number = 0
    while time.time() < lfb_deadline:
        lfb = validator2_node.last_finalized_block()
        lfb_number = lfb.blockInfo.blockNumber
        if lfb_number >= store_block_number:
            logging.info(
                "LFB #%d >= store block #%d on validator2 -- finalization caught up",
                lfb_number,
                store_block_number,
            )
            break
        time.sleep(5)
    else:
        pytest.fail(
            f"LFB #{lfb_number} on validator2 did not reach store block "
            f"#{store_block_number} within {lfb_timeout}s -- "
            f"registry state not guaranteed in merge base"
        )

    read_pattern = _read_pattern_for(id_address)

    # Read on validator2 with causal ordering enforced via validAfterBlockNumber.
    # Combined with the finalization wait above, this ensures the read deploy
    # executes in a block whose merge base includes the store's registry state.
    validator2_node.deploy_contract_with_substitution(
        {"@id_address@": id_address},
        READ_DATA_CONTRACT,
        VALIDATOR2_KEY,
        valid_after_block_no=store_block_number,
    )

    # Wait for this specific read confirmation in validator2's logs
    wait_for_log_match(context, validator2_node, read_pattern)

    read_match = read_pattern.search(validator2_node.logs())
    assert read_match is not None
    read_data = read_match.group("data")

    assert read_data == random_data, (
        f"Data read on validator2 '{read_data}' should match data stored on "
        f"validator1 '{random_data}'"
    )

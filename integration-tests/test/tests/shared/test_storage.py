"""
Storage Integration Tests

Tests for storing and retrieving data via the Rholang registry.
Uses the session-scoped shared shard with heartbeat-driven block creation.

Each test generates a unique random string, stores it via a real deploy
(state change), then reads it back via exploratory deploy (read-only,
no block created).
"""

import logging
import random
import string

import pytest
from f1r3fly.par import par_as_string, par_as_uri

from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import wait_for_deploy_finalized, wait_for_deploy_included

pytestmark = pytest.mark.xdist_group("shared")


STORE_DATA_CONTRACT = "resources/storage/store-data.rho"

VALIDATOR_KEYS = [VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID]


def _random_string(length: int = 20) -> str:
    return "".join(random.choices(string.ascii_letters, k=length))


def _store_data(node, key, random_data, timeouts):
    """Deploy store-data.rho (real deploy — creates state).

    Returns ``(uri, store_deploy_id, block_number)``. The ``deploy_id``
    lets callers poll ``wait_for_deploy_finalized`` on other nodes for the
    actual canonical-state inclusion.
    """
    store_deploy_id = node.deploy_rho_file(
        STORE_DATA_CONTRACT,
        key.private_key(),
        substitutions={"@store_data@": random_data},
    )
    block_info = wait_for_deploy_included(node, store_deploy_id, timeouts.deploy_inclusion)
    data = node.get_deploy_data(store_deploy_id, block_hash=block_info.blockHash)
    assert data is not None, (
        f"Deploy {store_deploy_id[:24]} returned None from get_deploy_data"
    )
    assert len(data.par) == 1, (
        f"Deploy {store_deploy_id[:24]} should write exactly 1 value to deployId, "
        f"got {len(data.par)}"
    )
    uri = par_as_uri(data.par[0])
    assert uri.startswith("rho:id:"), (
        f"Registry URI should start with 'rho:id:', got '{uri}'"
    )
    return uri, store_deploy_id, block_info.blockNumber


def _read_data(readonly_node, uri):
    """Read stored data via exploratory deploy on the readonly node.

    Exploratory deploy is restricted to read-only nodes on the Rust node.
    """
    results = readonly_node.registry_lookup(uri)
    return par_as_string(results[0])


def test_data_is_stored_and_served_by_node(shared_shard, timeouts) -> None:
    """Store data via registry on V1 and read it back via readonly node.

    1. Deploy store-data.rho with random data on V1 (real deploy)
    2. Read the registry URI from the deployId channel, verify rho:id: prefix
    3. Read the value back via exploratory deploy on readonly (no block created)
    4. Assert stored data matches read data
    """
    v1 = shared_shard.node("validator1")
    ro = shared_shard.readonly
    random_data = _random_string(20)

    uri, store_deploy_id, _ = _store_data(v1, VALIDATOR1_ID, random_data, timeouts)
    logging.info("Stored '%s' at %s on V1", random_data, uri)

    # Wait for readonly to see the deploy's canonical-state inclusion.
    # Unlike LFB-height polling, this correctly handles the case where the
    # deploy's initial block finalizes but effects were merge-rejected.
    wait_for_deploy_finalized(ro, store_deploy_id, timeouts.finalization)

    read_data = _read_data(ro, uri)

    assert read_data == random_data, (
        f"Read data '{read_data}' should match stored data '{random_data}'"
    )
    logging.info("Store on V1, read on readonly verified: '%s'", random_data)


def test_data_stored_on_one_validator_readable_on_readonly(shared_shard, timeouts) -> None:
    """Store data on each validator and read it back via readonly node.

    Tests cross-node state propagation: data stored via the registry on
    each validator should be readable on the readonly node after block
    propagation and finalization.

    1. For each validator (V1, V2, V3):
       a. Deploy store-data.rho with unique random data (real deploy)
       b. Wait for readonly to finalize past the store block
       c. Read the value via exploratory deploy on readonly
       d. Assert data matches
    """
    ro = shared_shard.readonly
    validators = shared_shard.validators

    for node, key in zip(validators, VALIDATOR_KEYS):
        random_data = _random_string(20)

        uri, store_deploy_id, store_block_number = _store_data(node, key, random_data, timeouts)
        logging.info("Stored '%s' at %s on %s (block #%d)",
                     random_data, uri, node.name, store_block_number)

        wait_for_deploy_finalized(ro, store_deploy_id, timeouts.finalization)

        read_data = _read_data(ro, uri)

        assert read_data == random_data, (
            f"Data stored on {node.name} '{read_data}' should match "
            f"original '{random_data}' when read from readonly"
        )
        logging.info("Store on %s, read on readonly verified: '%s'", node.name, random_data)

    logging.info("Data from all validators readable on readonly")

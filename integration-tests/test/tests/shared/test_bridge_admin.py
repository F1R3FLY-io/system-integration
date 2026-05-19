"""
Bridge Contract Integration Test

Deploys bridge-v2.rho and exercises the query API via two paths:
1. Exploratory deploy on readonly (fast, no blocks for queries)
2. Real deploy on validators (exercises cross-validator merge pipeline)

Both tests deploy the bridge contract (real deploy, state-changing) then
query getNonce, getTotalLocked, and getAddress to verify contract state.

This was the test that originally exposed the Blake2b512Random
count_view index bug causing GPrivate ID collisions (fixed in PR #468).
"""

import logging

import pytest
from f1r3fly.par import par_as_int, par_as_list, par_as_string, par_as_uri

from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import deploy_and_read, wait_for_finalized

pytestmark = pytest.mark.xdist_group("shared")


BRIDGE_CONTRACT = "resources/bridge-v2.rho"


def _make_query_rho(query_uri: str, method: str, param: str = "Nil") -> str:
    return f"""
new deployId(`rho:system:deployId`),
    lookup(`rho:registry:lookup`),
    queryCh,
    ret
in {{
  lookup!(`{query_uri}`, *queryCh) |
  for (query <- queryCh) {{
    query!("{method}", {param}, *ret) |
    for (@result <- ret) {{
      deployId!(result)
    }}
  }}
}}
"""


def _extract_bridge_uris(pars):
    """Extract and validate the 3 bridge URIs from the deploy data.

    bridge-v2.rho writes multiple values to deployId during deploy:
      deployId!(["address", bridgeVaultAddr])
      deployId!("initialized state channels")
      deployId!([queryUri, lockUri, unlockUri])

    We find the Par that is a list of 3 URIs.
    Returns (query_uri, lock_uri, unlock_uri).
    """
    for par in pars:
        try:
            items = par_as_list(par)
        except ValueError:
            continue
        if len(items) != 3:
            continue
        try:
            uris = [par_as_uri(item) for item in items]
        except ValueError:
            continue
        if all(u.startswith("rho:id:") for u in uris):
            return uris[0], uris[1], uris[2]

    par_summaries = [str(p)[:80] for p in pars]
    raise AssertionError(
        f"Could not find [queryUri, lockUri, unlockUri] in deploy data. "
        f"Got {len(pars)} par entries: {par_summaries}"
    )


def _deploy_bridge(shared_shard, timeouts):
    """Deploy bridge-v2.rho on V1 and return (query_uri, lock_uri, unlock_uri)."""
    validators = shared_shard.validators
    all_nodes = shared_shard.all_nodes
    find_timeout = timeouts.deploy_inclusion
    lfb_timeout = timeouts.finalization

    logging.info("Deploying bridge-v2.rho on V1...")
    deploy_pars, _, block_number = deploy_and_read(
        validators[0],
        "",
        VALIDATOR1_ID.private_key(),
        find_timeout,
        lfb_timeout,
        rho_file=BRIDGE_CONTRACT,
        phlo_limit=500_000_000,
    )

    target = block_number + 1
    for node in all_nodes:
        wait_for_finalized(node, target, lfb_timeout)
    logging.info("All nodes finalized past bridge block #%d", block_number)

    query_uri, lock_uri, unlock_uri = _extract_bridge_uris(deploy_pars)
    logging.info("  queryUri:  %s", query_uri)
    logging.info("  lockUri:   %s", lock_uri)
    logging.info("  unlockUri: %s", unlock_uri)
    return query_uri, lock_uri, unlock_uri


def test_bridge_api_exploratory(shared_shard, timeouts) -> None:
    """Deploy bridge, then query via exploratory deploy on readonly node.

    Uses registry_query which constructs the correct return channel
    pattern (first ``new`` name, not ``deployId``).
    """
    ro = shared_shard.readonly
    query_uri, _, _ = _deploy_bridge(shared_shard, timeouts)

    lfb_hash = ro.last_finalized_block().blockInfo.blockHash
    logging.info("Querying against LFB: %s...", lfb_hash[:16])

    logging.info("Querying getNonce via exploratory deploy...")
    nonce_pars = ro.registry_query(query_uri, "getNonce", block_hash=lfb_hash)
    nonce = par_as_int(nonce_pars[0])
    assert nonce == 0, f"Initial bridge nonce should be 0, got {nonce}"
    logging.info("  getNonce: %d", nonce)

    logging.info("Querying getTotalLocked via exploratory deploy...")
    locked_pars = ro.registry_query(query_uri, "getTotalLocked", block_hash=lfb_hash)
    total_locked = par_as_int(locked_pars[0])
    assert total_locked == 0, f"Initial totalLocked should be 0, got {total_locked}"
    logging.info("  getTotalLocked: %d", total_locked)

    logging.info("Querying getAddress via exploratory deploy...")
    addr_pars = ro.registry_query(query_uri, "getAddress", block_hash=lfb_hash)
    address = par_as_string(addr_pars[0])
    assert len(address) > 0, "Bridge vault address should be non-empty"
    logging.info("  getAddress: %s", address)


def test_bridge_api_real_deploy(shared_shard, timeouts) -> None:
    """Deploy bridge, then query via real deploys across validators.

    Real deploys create blocks and exercise the cross-validator merge
    pipeline. Each query is submitted to a different validator.
    """
    validators = shared_shard.validators
    find_timeout = timeouts.deploy_inclusion
    lfb_timeout = timeouts.finalization
    query_uri, _, _ = _deploy_bridge(shared_shard, timeouts)

    logging.info("Querying getNonce on V1 (real deploy)...")
    nonce_pars, _, _ = deploy_and_read(
        validators[0],
        _make_query_rho(query_uri, "getNonce"),
        VALIDATOR1_ID.private_key(),
        find_timeout,
        lfb_timeout,
        phlo_limit=500_000_000,
    )
    nonce = par_as_int(nonce_pars[0])
    assert nonce == 0, f"Initial bridge nonce should be 0, got {nonce}"
    logging.info("  getNonce: %d", nonce)

    logging.info("Querying getTotalLocked on V2 (real deploy)...")
    locked_pars, _, _ = deploy_and_read(
        validators[1],
        _make_query_rho(query_uri, "getTotalLocked"),
        VALIDATOR2_ID.private_key(),
        find_timeout,
        lfb_timeout,
        phlo_limit=500_000_000,
    )
    total_locked = par_as_int(locked_pars[0])
    assert total_locked == 0, f"Initial totalLocked should be 0, got {total_locked}"
    logging.info("  getTotalLocked: %d", total_locked)

    logging.info("Querying getAddress on V3 (real deploy)...")
    addr_pars, _, _ = deploy_and_read(
        validators[2],
        _make_query_rho(query_uri, "getAddress"),
        VALIDATOR3_ID.private_key(),
        find_timeout,
        lfb_timeout,
        phlo_limit=500_000_000,
    )
    address = par_as_string(addr_pars[0])
    assert len(address) > 0, "Bridge vault address should be non-empty"
    logging.info("  getAddress: %s", address)

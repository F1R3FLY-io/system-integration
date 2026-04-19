"""
Bridge Contract Integration Test

Deploys bridge-v2.rho and exercises the query API and verifies URI registration:
1. Deploy bridge-v2.rho -> extract 3 URIs (query, lock, unlock)
2. Query getNonce via queryUri -> verify returns 0
3. Query getTotalLocked via queryUri -> verify returns 0
4. Query getAddress via queryUri -> verify returns a string

Queries use real deploys (not exploratory deploy) because the bridge
contract responds asynchronously via state channels, which doesn't
complete within exploratory deploy's execution window.

Queries are distributed across validators. The sustained load test
repeats the query cycle 3 times across all validators.

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
VALIDATOR_KEYS = [VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID]


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


def test_bridge_api(shared_shard, timeouts) -> None:
    """Deploy bridge-v2.rho and verify query API functions respond correctly.

    Steps:
    1. Deploy bridge on V1 -> extract 3 URIs (query, lock, unlock)
    2. getNonce query on V1 -> verify returns integer 0
    3. getTotalLocked query on V2 -> verify returns integer 0
    4. getAddress query on V3 -> verify returns a non-empty string
    """
    validators = shared_shard.validators
    all_nodes = shared_shard.all_nodes
    find_timeout = timeouts.deploy_inclusion
    lfb_timeout = timeouts.finalization

    # Step 1: Deploy bridge on V1 (creates state)
    logging.info("Step 1: Deploying bridge-v2.rho on V1...")
    deploy_pars, _, block_number = deploy_and_read(
        validators[0], "", VALIDATOR1_ID.private_key(),
        find_timeout, lfb_timeout,
        rho_file=BRIDGE_CONTRACT, phlo_limit=500_000_000,
    )

    # Verify all nodes finalized past the bridge block
    target = block_number + 1
    for node in all_nodes:
        wait_for_finalized(node, target, lfb_timeout)
    logging.info("All nodes finalized past bridge block #%d", block_number)

    query_uri, lock_uri, unlock_uri = _extract_bridge_uris(deploy_pars)
    logging.info("  queryUri:  %s", query_uri)
    logging.info("  lockUri:   %s", lock_uri)
    logging.info("  unlockUri: %s", unlock_uri)

    # Step 2: Query getNonce on V1
    logging.info("Step 2: Querying getNonce on V1...")
    nonce_pars, _, _ = deploy_and_read(
        validators[0], _make_query_rho(query_uri, "getNonce"),
        VALIDATOR1_ID.private_key(), find_timeout, lfb_timeout,
        phlo_limit=500_000_000,
    )
    nonce = par_as_int(nonce_pars[0])
    assert nonce == 0, f"Initial bridge nonce should be 0, got {nonce}"
    logging.info("  getNonce result: %d", nonce)

    # Step 3: Query getTotalLocked on V2
    logging.info("Step 3: Querying getTotalLocked on V2...")
    locked_pars, _, _ = deploy_and_read(
        validators[1], _make_query_rho(query_uri, "getTotalLocked"),
        VALIDATOR2_ID.private_key(), find_timeout, lfb_timeout,
        phlo_limit=500_000_000,
    )
    total_locked = par_as_int(locked_pars[0])
    assert total_locked == 0, f"Initial totalLocked should be 0, got {total_locked}"
    logging.info("  getTotalLocked result: %d", total_locked)

    # Step 4: Query getAddress on V3
    logging.info("Step 4: Querying getAddress on V3...")
    addr_pars, _, _ = deploy_and_read(
        validators[2], _make_query_rho(query_uri, "getAddress"),
        VALIDATOR3_ID.private_key(), find_timeout, lfb_timeout,
        phlo_limit=500_000_000,
    )
    address = par_as_string(addr_pars[0])
    assert len(address) > 0, "Bridge vault address should be non-empty"
    logging.info("  getAddress result: %s", address)


def test_bridge_sustained_load(shared_shard, timeouts) -> None:
    """Deploy bridge once, then run query calls 3 times across all validators.

    Queries are distributed round-robin across validators to exercise
    cross-validator query handling.

    Each operation is validated with typed Par extraction.
    """
    validators = shared_shard.validators
    find_timeout = timeouts.deploy_inclusion
    lfb_timeout = timeouts.finalization
    iterations = 3

    # Deploy bridge once on V1
    logging.info("Deploying bridge-v2.rho (once) on V1...")
    deploy_pars, _, _ = deploy_and_read(
        validators[0], "", VALIDATOR1_ID.private_key(),
        find_timeout, lfb_timeout,
        rho_file=BRIDGE_CONTRACT, phlo_limit=500_000_000,
    )

    query_uri, _, _ = _extract_bridge_uris(deploy_pars)
    logging.info("  queryUri: %s", query_uri)

    query_steps = [
        ("getNonce", "getNonce", par_as_int, lambda v: v >= 0,
         "nonce should be >= 0"),
        ("getTotalLocked", "getTotalLocked", par_as_int, lambda v: v >= 0,
         "totalLocked should be >= 0"),
        ("getAddress", "getAddress", par_as_string, lambda v: len(v) > 0,
         "address should be non-empty"),
    ]

    failures = []
    for i in range(1, iterations + 1):
        logging.info("=== Iteration %d/%d ===", i, iterations)
        for step_idx, (step_name, method, extractor, validator_fn, fail_msg) in enumerate(query_steps):
            # Round-robin across validators
            node = validators[(i + step_idx) % len(validators)]
            key = VALIDATOR_KEYS[(i + step_idx) % len(VALIDATOR_KEYS)]
            code = _make_query_rho(query_uri, method)
            try:
                result_pars, _, _ = deploy_and_read(
                    node, code, key.private_key(),
                    find_timeout, lfb_timeout,
                    phlo_limit=500_000_000,
                )
                value = extractor(result_pars[0])
                if not validator_fn(value):
                    failures.append(f"iter {i}: {step_name} on {node.name} = {value} -- {fail_msg}")
                    logging.error("  iter %d: %s on %s = %s -- %s", i, step_name, node.name, value, fail_msg)
                else:
                    logging.info("  %s on %s: %s", step_name, node.name, value)
            except Exception as e:
                failures.append(f"iter {i}: {step_name} on {node.name} error: {e}")
                logging.error("  iter %d: %s on %s error: %s", i, step_name, node.name, e)

    logging.info(
        "=== Results: %d/%d iterations fully successful ===",
        iterations - len(failures), iterations,
    )
    if failures:
        logging.error("Failures:\n%s", "\n".join(failures))

    assert len(failures) == 0, (
        f"Bridge sustained load: {len(failures)} failures in "
        f"{iterations} iterations:\n" + "\n".join(failures)
    )

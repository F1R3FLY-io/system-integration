"""
Bridge Contract Admin API Test

Deploys bridge.rho and exercises the full admin API:
1. Deploy bridge.rho → extract 4 URIs (query, lock, unlock, admin)
2. Query getNonce via queryUri
3. setVerifier via adminUri
4. setRelayer via adminUri
5. setRequiredSignatures via adminUri
6. addOracle via adminUri
7. removeOracle via adminUri

Each admin call deploys a contract that looks up the admin URI in the
registry, resolves the caller's vault address, dispatches the admin
method, and writes the result to the deployId channel.
"""

import logging
import os
import re
import time

import pytest
from docker.client import DockerClient

from .common import TestingContext
from .conftest import (
    ALL_CONTAINERS,
    VALIDATOR1_KEY,
    assert_containers_running,
)
from .rnode import Node

pytestmark = pytest.mark.xdist_group("shard")


def _load_bridge_contract() -> str:
    integration_tests_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(integration_tests_dir, "resources", "bridge.rho")
    with open(path) as f:
        return f.read()


def _wait_for_deploy_in_block(node: Node, deploy_id: str, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            light_block = node.find_deploy(deploy_id)
            logging.info(
                "Deploy included in block #%d (%s)",
                light_block.blockNumber,
                light_block.blockHash[:16],
            )
            return light_block.blockHash, light_block.blockNumber
        except Exception:
            time.sleep(3)
    raise AssertionError(f"Deploy {deploy_id[:24]} was not included in a block within {timeout}s")


def _wait_for_lfb(node: Node, target_block: int, timeout: float):
    deadline = time.time() + timeout
    current = 0
    while time.time() < deadline:
        try:
            lfb = node.last_finalized_block()
            current = lfb.blockInfo.blockNumber
            if current >= target_block:
                logging.info("LFB reached #%d", current)
                return
        except Exception:
            pass
        time.sleep(5)
    raise AssertionError(
        f"LFB stuck at #{current}, expected at least #{target_block} within {timeout}s"
    )


def _deploy_finalize_and_read(node, code, private_key, find_timeout, lfb_timeout):
    """Deploy code, wait for finalization, read deployId data."""
    deploy_id = node.deploy_string(
        code,
        private_key,
        phlo_limit=500_000_000,
        phlo_price=1,
    )
    logging.info("Deployed, deploy_id=%s", deploy_id[:24])
    block_hash, block_number = _wait_for_deploy_in_block(node, deploy_id, find_timeout)
    _wait_for_lfb(node, block_number, lfb_timeout)

    block_info = node.get_block(block_hash)
    for d in block_info.deploys:
        if d.sig == deploy_id:
            logging.info(
                "Deploy in block: cost=%d, errored=%s, systemDeployError='%s'",
                d.cost,
                d.errored,
                d.systemDeployError,
            )
            break

    data = node.get_deploy_data(deploy_id, block_hash=block_hash)
    return data, block_hash, block_number


def _make_query_rho(query_uri: str, method: str, param: str = "Nil") -> str:
    """Generate a query contract that calls a bridge query method."""
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


def _make_admin_rho(admin_uri: str, method: str, param: str) -> str:
    """Generate an admin call contract that dispatches a bridge admin method."""
    return f"""
new deployId(`rho:system:deployId`),
    deployerId(`rho:system:deployerId`),
    lookup(`rho:registry:lookup`),
    VaultAddress(`rho:vault:address`),
    adminBridgeCh,
    callerAddrCh,
    ret
in {{
  lookup!(`{admin_uri}`, *adminBridgeCh) |
  VaultAddress!("fromDeployerId", *deployerId, *callerAddrCh) |
  for (adminBridge <- adminBridgeCh; @callerAddr <- callerAddrCh) {{
    adminBridge!("{method}", callerAddr, {param}, *ret) |
    for (@result <- ret) {{
      deployId!(result)
    }}
  }}
}}
"""


@pytest.mark.timeout(900)
def test_bridge_admin_api(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
) -> None:
    """Deploy bridge.rho and verify all admin API functions respond.

    Steps:
    1. Deploy bridge → extract 4 URIs
    2. getNonce query (control)
    3-7. Admin function calls (setVerifier, setRelayer, setRequiredSignatures,
         addOracle, removeOracle)
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    find_timeout = int(30 * testing_context.timeout_scale)
    lfb_timeout = int(60 * testing_context.timeout_scale)

    # Step 1: Deploy bridge and extract URIs
    logging.info("Step 1: Deploying bridge.rho...")
    deploy_data, _, _ = _deploy_finalize_and_read(
        validator1_node,
        _load_bridge_contract(),
        VALIDATOR1_KEY,
        find_timeout,
        lfb_timeout,
    )
    assert deploy_data is not None, "Bridge deploy returned no data"

    data_str = str(deploy_data)
    uris = re.findall(r"rho:id:[a-zA-Z0-9]+", data_str)
    assert len(uris) >= 4, f"Expected 4 URIs, got {len(uris)}: {uris}"
    query_uri = uris[0]
    admin_uri = uris[3]
    logging.info("  queryUri:  %s", query_uri)
    logging.info("  adminUri:  %s", admin_uri)

    # Step 2: Query getNonce
    logging.info("Step 2: Querying getNonce...")
    nonce_data, _, _ = _deploy_finalize_and_read(
        validator1_node,
        _make_query_rho(query_uri, "getNonce"),
        VALIDATOR1_KEY,
        find_timeout,
        lfb_timeout,
    )
    assert nonce_data is not None, "getNonce query returned no data"
    logging.info("  getNonce result: %s", str(nonce_data)[:200])

    # Steps 3-7: Admin function calls
    admin_steps = [
        ("Step 3: setVerifier", "setVerifier", '"verifier_v2"'),
        ("Step 4: setRelayer", "setRelayer", '"relayer_addr_1"'),
        ("Step 5: setRequiredSignatures", "setRequiredSignatures", "2"),
        ("Step 6: addOracle", "addOracle", '"oracle-4"'),
        ("Step 7: removeOracle", "removeOracle", '"oracle-4"'),
    ]

    for step_name, method, param in admin_steps:
        logging.info("%s...", step_name)
        admin_data, _, _ = _deploy_finalize_and_read(
            validator1_node,
            _make_admin_rho(admin_uri, method, param),
            VALIDATOR1_KEY,
            find_timeout,
            lfb_timeout,
        )
        logging.info(
            "  %s result: %s",
            method,
            str(admin_data)[:200] if admin_data else "None",
        )
        assert admin_data is not None, (
            f"{step_name} ({method}) returned no data. "
            f"The persistent contract did not respond to the cross-deploy send."
        )

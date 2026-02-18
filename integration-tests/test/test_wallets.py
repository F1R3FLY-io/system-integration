"""Wallet / REV transfer integration tests.

Tests run against the compose-managed shard. Wallet balances come from
the shared genesis/wallets.txt. All validator keys have funded wallets:

    VALIDATOR1 (111127RX...): 50,000,000,000,000,000 REV
    VALIDATOR2 (111129p3...): 50,000,000,000,000,000 REV
    VALIDATOR3 (1111LAd2...): 500,000,000,000,000,000 REV

Deploys and proposes go through validator1.
"""
import re
import time
from typing import Pattern, Tuple
import pytest
from f1r3fly.crypto import PrivateKey

from .common import (
    TestingContext,
    TransderFundsError,
    random_string,
)
from .rnode import Node, default_shard_id
from .conftest import (
    VALIDATOR1_KEY,
    VALIDATOR2_KEY,
    VALIDATOR3_KEY,
    _is_rust_node,
)
from .http_client import HttpClient, HttpRequestException
from .wait import (
    wait_for_log_match_result,
    wait_for_log_match_result_raise,
)

pytestmark = pytest.mark.xdist_group("shard")


def wait_transfer_result(context: TestingContext, node: Node,
                         transfer_funds_result_pattern: Pattern) -> None:
    transfer_result_match = wait_for_log_match_result_raise(
        context, node, transfer_funds_result_pattern,
    )
    reason = transfer_result_match.group('reason')
    if reason != "Nil":
        raise TransderFundsError(reason)


def deploy_transfer(log_marker: str, node: Node, from_rev_addr: str,
                    to_rev_addr: str, amount: int, private_key: PrivateKey,
                    phlo_limit: int, phlo_price: int) -> str:
    return node.deploy_contract_with_substitution(
        substitute_dict={
            "%FROM": from_rev_addr,
            "%TO": to_rev_addr,
            "%AMOUNT": str(amount),
            "%LOG_MARKER": log_marker,
        },
        rho_file_path="resources/wallets/transfer_funds.rho",
        private_key=private_key,
        phlo_limit=phlo_limit,
        phlo_price=phlo_price,
        shard_id=default_shard_id,
    )


def transfer_funds(context: TestingContext, node: Node, from_rev_addr: str,
                   to_rev_addr: str, amount: int, private_key: PrivateKey,
                   phlo_limit: int, phlo_price: int) -> None:
    """Transfer rev from one vault to another vault.

    If the transfer is processed successfully, returns None.
    If the transfer fails, raises TransderFundsError.
    """
    log_marker = random_string(context, 10)
    transfer_funds_result_pattern = re.compile(
        f'"{log_marker} (Successfully|Failing) reason: (?P<reason>[a-zA-Z0-9 ]*)"'
    )
    deploy_transfer(
        log_marker, node, from_rev_addr, to_rev_addr, amount,
        private_key, phlo_limit, phlo_price,
    )
    wait_transfer_result(context, node, transfer_funds_result_pattern)


def get_vault_balance(context: TestingContext, node: Node, rev_addr: str,
                      private_key: PrivateKey, phlo_limit: int,
                      phlo_price: int) -> Tuple[str, int]:
    log_marker = random_string(context, 10)
    check_balance_pattern = re.compile(
        f'"{log_marker} Vault (?P<rev_addr>[a-zA-Z0-9]*) balance is (?P<balance>[0-9]*)"'
    )
    block_hash = node.deploy_contract_with_substitution(
        substitute_dict={"%REV_ADDR": rev_addr, "%LOG_MARKER": log_marker},
        rho_file_path="resources/wallets/get_vault_balance.rho",
        private_key=private_key,
        phlo_limit=phlo_limit,
        phlo_price=phlo_price,
        shard_id=default_shard_id,
    )
    check_balance_match = wait_for_log_match_result(context, node, check_balance_pattern)
    return (block_hash, int(check_balance_match.group("balance")))


def test_validator1_pay_validator2(testing_context: TestingContext,
                                   validator1_node: Node) -> None:
    """Validator1 transfers REV to Validator2. Both have funded wallets in genesis."""
    context = testing_context
    node = validator1_node

    transfer_amount = 20000000
    v1_rev_address = VALIDATOR1_KEY.get_public_key().get_rev_address()
    v2_rev_address = VALIDATOR2_KEY.get_public_key().get_rev_address()

    _, v1_balance_before = get_vault_balance(
        context, node, v1_rev_address, VALIDATOR1_KEY, 1000000, 1,
    )
    assert v1_balance_before > 0

    _, v2_balance_before = get_vault_balance(
        context, node, v2_rev_address, VALIDATOR1_KEY, 1000000, 1,
    )

    transfer_funds(
        context, node, v1_rev_address, v2_rev_address,
        transfer_amount, VALIDATOR1_KEY, 1000000, 1,
    )

    _, v2_balance_after = get_vault_balance(
        context, node, v2_rev_address, VALIDATOR1_KEY, 1000000, 1,
    )
    assert v2_balance_after == v2_balance_before + transfer_amount


def test_transfer_failed_with_invalid_key(testing_context: TestingContext,
                                           validator1_node: Node) -> None:
    """Transferring from Validator3's vault with Validator2's key fails."""
    context = testing_context
    node = validator1_node

    v2_rev_address = VALIDATOR2_KEY.get_public_key().get_rev_address()
    v3_rev_address = VALIDATOR3_KEY.get_public_key().get_rev_address()

    with pytest.raises(TransderFundsError) as e:
        # Sign with VALIDATOR2_KEY but try to transfer FROM VALIDATOR3's vault
        transfer_funds(
            context, node, v3_rev_address, v2_rev_address,
            100, VALIDATOR2_KEY, 1000000, 1,
        )
    assert e.value.reason == "Invalid AuthKey"


def test_transfer_failed_with_insufficient_funds(
    testing_context: TestingContext,
    validator1_node: Node,
) -> None:
    """Transferring more than available balance fails with Insufficient funds."""
    context = testing_context
    node = validator1_node

    # Generate a fresh key with no genesis wallet (0 balance)
    unfunded_key = PrivateKey.generate()
    unfunded_rev_address = unfunded_key.get_public_key().get_rev_address()
    v1_rev_address = VALIDATOR1_KEY.get_public_key().get_rev_address()

    # Create the vault for the unfunded key by checking balance (findOrCreate)
    _, unfunded_balance = get_vault_balance(
        context, node, unfunded_rev_address, VALIDATOR1_KEY, 1000000, 1,
    )
    assert unfunded_balance == 0

    # Transfer enough to cover phlo costs for the subsequent deploy.
    # The unfunded key will sign the "insufficient funds" transfer deploy,
    # so it needs enough REV to pay phlo (up to phlo_limit * phlo_price).
    # With phlo_limit=1,000,000 and phlo_price=1, we need at least 1M REVlette
    # in the deployer's vault. Transfer 5M to have comfortable headroom.
    seed_amount = 5000000
    transfer_funds(
        context, node, v1_rev_address, unfunded_rev_address,
        seed_amount, VALIDATOR1_KEY, 1000000, 1,
    )

    # Poll until the balance reflects the transfer. In a heartbeat-driven
    # multi-validator shard, the transfer block may not be in the merge base
    # of the next balance-check block on the first attempt.
    balance_timeout = int(60 * context.timeout_scale)
    deadline = time.time() + balance_timeout
    unfunded_balance = 0
    while time.time() < deadline:
        _, unfunded_balance = get_vault_balance(
            context, node, unfunded_rev_address, VALIDATOR1_KEY, 1000000, 1,
        )
        if unfunded_balance == seed_amount:
            break
        time.sleep(5)
    assert unfunded_balance == seed_amount, (
        f"Expected balance {seed_amount} after transfer, got {unfunded_balance}"
    )

    # Now try to transfer MORE than the unfunded key has.
    # The deploy is signed by the unfunded key (deployer pays phlo from this
    # vault), and the transfer amount exceeds the vault balance.
    with pytest.raises(TransderFundsError) as e:
        transfer_funds(
            context, node, unfunded_rev_address, v1_rev_address,
            seed_amount + 1, unfunded_key, 1000000, 1,
        )
    assert e.value.reason == "Insufficient funds"


def transfer_funds_with_block_hash(
    context: TestingContext, node: Node, from_rev_addr: str,
    to_rev_addr: str, amount: int, private_key: PrivateKey,
    phlo_limit: int, phlo_price: int,
) -> str:
    """Transfer REV from one vault to another and return the block hash.

    Raises TransderFundsError if the transfer fails.

    Note: deploy_transfer returns the deploy ID (signature hex), not a block
    hash. After the transfer completes, we use find_deploy to look up the
    block that included the deploy.
    """
    log_marker = random_string(context, 10)
    transfer_funds_result_pattern = re.compile(
        f'"{log_marker} (Successfully|Failing) reason: (?P<reason>[a-zA-Z0-9 ]*)"'
    )
    deploy_id = deploy_transfer(
        log_marker, node, from_rev_addr, to_rev_addr, amount,
        private_key, phlo_limit, phlo_price,
    )
    wait_transfer_result(context, node, transfer_funds_result_pattern)

    # Look up the block that included this deploy. Poll in case there is a
    # small window between the log output appearing and the block being
    # fully committed to the DAG.
    find_timeout = int(60 * context.timeout_scale)
    deadline = time.time() + find_timeout
    while time.time() < deadline:
        try:
            light_block = node.find_deploy(deploy_id)
            if light_block.blockHash:
                return light_block.blockHash
        except Exception:
            pass
        time.sleep(3)

    raise AssertionError(
        f"Deploy {deploy_id} not included in any block within {find_timeout}s"
    )


@pytest.mark.skipif(
    _is_rust_node(),
    reason="BlockReportAPI/TransactionAPI not implemented in Rust node (f1r3node#346, backport #315)",
)
def test_block_api_returns_transfer_info(
    testing_context: TestingContext,
    validator1_node: Node,
    readonly_node: Node,
) -> None:
    """Test that the Block API returns transfer information in DeployInfo.

    Verifies Issue #212: Expose native-token transfer details in DeployInfo.

    Transfer extraction uses BlockReportAPI.blockReport which replays the
    block to capture RSpace events. In non-dev-mode shards, block reports
    are restricted to read-only nodes (BlockReportAPI.validateReadOnlyNode).
    Therefore this test queries the read-only node's HTTP API for transfer
    data, while still using validator1 to submit the transfer deploy.
    """
    context = testing_context
    node = validator1_node

    transfer_amount = 5000000
    v1_rev_address = VALIDATOR1_KEY.get_public_key().get_rev_address()
    v2_rev_address = VALIDATOR2_KEY.get_public_key().get_rev_address()

    # Ensure Validator2's vault exists (findOrCreate via balance check)
    get_vault_balance(context, node, v2_rev_address, VALIDATOR1_KEY, 1000000, 1)

    # Perform transfer and get the block hash
    block_hash = transfer_funds_with_block_hash(
        context, node, v1_rev_address, v2_rev_address,
        transfer_amount, VALIDATOR1_KEY, 1000000, 1,
    )

    # Query the read-only node's HTTP API for block info with transfers.
    # BlockReportAPI.blockReport (used by transfer extraction) only executes
    # on read-only nodes when dev-mode is disabled.
    # The block may not have propagated to the readonly node yet, so we
    # catch HttpRequestException (e.g. 404) and retry within the loop.
    http_client = HttpClient('localhost', readonly_node.get_http_port())
    deploy_with_transfers = None
    api_timeout = int(120 * context.timeout_scale)
    deadline = time.time() + api_timeout

    while time.time() < deadline:
        try:
            block_info = http_client.get_block(block_hash)
        except HttpRequestException:
            # Block hasn't propagated to the readonly node yet
            time.sleep(5)
            continue

        assert "deploys" in block_info, "Block info should contain deploys"
        deploys = block_info["deploys"]
        assert len(deploys) > 0, "Block should have at least one deploy"

        for deploy in deploys:
            if "transfers" in deploy and len(deploy["transfers"]) > 0:
                deploy_with_transfers = deploy
                break

        if deploy_with_transfers is not None:
            break
        time.sleep(5)

    assert deploy_with_transfers is not None, (
        f"Transfers not populated within {api_timeout}s for block {block_hash}. "
        "BlockReportAPI should be available on the read-only node."
    )

    # Structural check: every deploy should have a transfers field
    block_info = http_client.get_block(block_hash)
    for deploy in block_info["deploys"]:
        assert "transfers" in deploy, "Each deploy should have a transfers field"

    # Verify transfer content
    transfers = deploy_with_transfers["transfers"]
    assert len(transfers) >= 1, "Should have at least one transfer"
    transfer = transfers[0]
    assert "fromAddr" in transfer, "Transfer should have fromAddr"
    assert "toAddr" in transfer, "Transfer should have toAddr"
    assert "amount" in transfer, "Transfer should have amount"
    assert "success" in transfer, "Transfer should have success"
    assert "failReason" in transfer, "Transfer should have failReason"

    assert transfer["fromAddr"] == v1_rev_address, \
        f"fromAddr should be {v1_rev_address}"
    assert transfer["toAddr"] == v2_rev_address, \
        f"toAddr should be {v2_rev_address}"
    assert transfer["amount"] == transfer_amount, \
        f"amount should be {transfer_amount}"
    assert transfer["success"] is True, "Transfer should be successful"
    assert transfer["failReason"] == "", \
        "Successful transfer should have empty failReason"

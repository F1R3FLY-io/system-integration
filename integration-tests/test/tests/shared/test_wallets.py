"""
Wallet / Token Transfer Integration Tests

Tests for wallet-based token transfers using the PoS vault system.
Uses pyf1r3fly's VaultAPI for all operations:
  - ``get_balance()`` for balance queries (exploratory deploy on readonly)
  - ``deploy_get_balance()`` for balance queries (real deploy on validators,
    used only in test_validator1_pay_validator2 to cross-check against readonly)
  - ``transfer_ensure()`` + ``read_transfer_result()`` for transfers
    (real deploys on validators)

Balance queries use exploratory deploy on readonly. Transfers are
submitted via validators since they are state-changing operations.
"""

import logging

import pytest
from f1r3fly.vault import TransferResult

from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import poll_until, wait_for_deploy_included, wait_for_finalized

pytestmark = pytest.mark.xdist_group("shared")


def _transfer_and_read_result(node, from_addr, to_addr, amount, key, timeouts) -> TransferResult:
    """Submit a transfer and read the result after block inclusion."""
    deploy_id = node.vault.transfer_ensure(from_addr, to_addr, amount, key)

    block_info = wait_for_deploy_included(node, deploy_id, timeouts.deploy_inclusion)

    wait_for_finalized(node, block_info.blockNumber, timeouts.finalization)

    return node.vault.read_transfer_result(deploy_id, block_hash=block_info.blockHash)


# ===========================================================================
# Tests
# ===========================================================================


def test_validator1_pay_validator2(shared_shard, timeouts) -> None:
    """Validator1 transfers tokens to Validator2 via V1's node.

    Balance checked on both readonly (exploratory) and V1 (deploy).
    """
    v1 = shared_shard.node("validator1")
    ro = shared_shard.readonly
    v1_key = VALIDATOR1_ID.private_key()
    v1_vault = v1_key.get_public_key().get_vault_address()
    v2_vault = VALIDATOR2_ID.private_key().get_public_key().get_vault_address()
    transfer_amount = 20_000_000

    # Check balance on readonly (exploratory) and V1 (deploy)
    v1_balance_ro = ro.vault.get_balance(v1_vault)
    assert v1_balance_ro > 0

    v1_balance_v1 = v1.vault.deploy_get_balance(
        v1_vault, v1_key, timeouts.deploy_inclusion, timeouts.finalization,
    )
    assert v1_balance_v1 > 0
    logging.info("V1 balance: readonly=%d, V1 deploy=%d", v1_balance_ro, v1_balance_v1)

    v2_balance_before = ro.vault.get_balance(v2_vault)

    result = _transfer_and_read_result(
        v1, v1_vault, v2_vault, transfer_amount, v1_key, timeouts,
    )
    assert result.success, f"Transfer failed: {result.reason}"

    # Poll until balance reflects the transfer (query via readonly)
    def _balance_updated():
        bal = ro.vault.get_balance(v2_vault)
        return bal if bal == v2_balance_before + transfer_amount else None

    v2_balance_after = poll_until(
        predicate=_balance_updated,
        timeout=timeouts.finalization,
        interval=5.0,
        description="V2 balance updated after transfer",
    )

    assert v2_balance_after == v2_balance_before + transfer_amount

    logging.info(
        "Transfer verified: V2 balance %d -> %d (+%d)",
        v2_balance_before, v2_balance_after, transfer_amount,
    )


def test_validator2_pay_validator3(shared_shard, timeouts) -> None:
    """Validator2 transfers tokens to Validator3 via V2's node.

    Balance checked via exploratory deploy on readonly.
    """
    v2 = shared_shard.node("validator2")
    ro = shared_shard.readonly
    v2_key = VALIDATOR2_ID.private_key()
    v2_vault = v2_key.get_public_key().get_vault_address()
    v3_vault = VALIDATOR3_ID.private_key().get_public_key().get_vault_address()
    transfer_amount = 10_000_000

    v2_balance_before = ro.vault.get_balance(v2_vault)
    assert v2_balance_before > 0

    v3_balance_before = ro.vault.get_balance(v3_vault)

    result = _transfer_and_read_result(
        v2, v2_vault, v3_vault, transfer_amount, v2_key, timeouts,
    )
    assert result.success, f"Transfer failed: {result.reason}"

    def _balance_updated():
        bal = ro.vault.get_balance(v3_vault)
        return bal if bal == v3_balance_before + transfer_amount else None

    v3_balance_after = poll_until(
        predicate=_balance_updated,
        timeout=timeouts.finalization,
        interval=5.0,
        description="V3 balance updated after transfer",
    )

    assert v3_balance_after == v3_balance_before + transfer_amount

    logging.info(
        "Transfer verified: V3 balance %d -> %d (+%d)",
        v3_balance_before, v3_balance_after, transfer_amount,
    )


def test_transfer_failed_with_invalid_key(shared_shard, timeouts) -> None:
    """Transferring from Validator3's vault with Validator2's key fails.

    Submitted via V3's node to exercise a different validator's deploy pipeline.
    """
    v3 = shared_shard.node("validator3")
    v2_key = VALIDATOR2_ID.private_key()
    v2_vault = v2_key.get_public_key().get_vault_address()
    v3_vault = VALIDATOR3_ID.private_key().get_public_key().get_vault_address()

    result = _transfer_and_read_result(
        v3, v3_vault, v2_vault, 100, v2_key, timeouts,
    )
    assert not result.success, "Transfer should have failed with Invalid AuthKey"
    assert result.reason == "Invalid AuthKey", (
        f"Expected 'Invalid AuthKey', got '{result.reason}'"
    )


def test_transfer_failed_with_insufficient_funds(shared_shard, timeouts) -> None:
    """Transferring more than sender balance fails with Insufficient funds.

    Submitted via V2's node. Balance queried via exploratory deploy on readonly.
    """
    v2 = shared_shard.node("validator2")
    ro = shared_shard.readonly
    v1_key = VALIDATOR1_ID.private_key()
    v1_vault = v1_key.get_public_key().get_vault_address()
    v2_vault = VALIDATOR2_ID.private_key().get_public_key().get_vault_address()

    v1_balance = ro.vault.get_balance(v1_vault)
    assert v1_balance > 0
    overdraw_amount = v1_balance + 1

    result = _transfer_and_read_result(
        v2, v1_vault, v2_vault, overdraw_amount, v1_key, timeouts,
    )
    assert not result.success, "Transfer should have failed with Insufficient funds"
    assert result.reason == "Insufficient funds", (
        f"Expected 'Insufficient funds', got '{result.reason}'"
    )


def test_block_api_returns_transfer_info(shared_shard, timeouts) -> None:
    """Block API returns transfer information in DeployInfo.

    Uses VaultAPI for the transfer, then queries both the readonly node
    and a validator for BlockReportAPI transfer extraction.
    """
    v1 = shared_shard.node("validator1")
    ro = shared_shard.readonly
    v1_key = VALIDATOR1_ID.private_key()
    v1_vault = v1_key.get_public_key().get_vault_address()
    v2_vault = VALIDATOR2_ID.private_key().get_public_key().get_vault_address()
    transfer_amount = 5_000_000

    # Ensure V2's vault exists (query via readonly)
    ro.vault.get_balance(v2_vault)

    # Transfer via VaultAPI
    deploy_id = v1.vault.transfer_ensure(
        v1_vault, v2_vault, transfer_amount, v1_key,
    )

    # Find the block containing the transfer deploy
    def _find():
        try:
            block = v1.find_deploy(deploy_id)
            return block.blockHash if block.blockHash else None
        except Exception:
            return None

    block_hash = poll_until(
        predicate=_find,
        timeout=timeouts.deploy_inclusion,
        interval=3.0,
        description=f"transfer deploy {deploy_id[:24]} inclusion",
    )

    # Verify transfer info on readonly (block report API is readonly-only on Rust node)
    for node in [ro]:
        def _has_transfers(n=node):
            try:
                info = n.api_get(f"/block/{block_hash}")
                deploys = info.get("deploys", [])
                for deploy in deploys:
                    if deploy.get("transfers") and len(deploy["transfers"]) > 0:
                        return info
            except Exception:
                pass
            return None

        block_info = poll_until(
            predicate=_has_transfers,
            timeout=timeouts.finalization * 3,
            interval=5.0,
            description=f"block report with transfers on {node.name}",
        )

        # Structural check
        for deploy in block_info["deploys"]:
            assert "transfers" in deploy, (
                f"{node.name}: each deploy should have a transfers field"
            )

        # Find the deploy with actual transfers
        deploy_with_transfers = None
        for deploy in block_info["deploys"]:
            if deploy.get("transfers") and len(deploy["transfers"]) > 0:
                deploy_with_transfers = deploy
                break

        assert deploy_with_transfers is not None, (
            f"{node.name}: block should contain a deploy with transfer records"
        )

        # Verify transfer content
        transfer = deploy_with_transfers["transfers"][0]
        assert transfer["fromAddr"] == v1_vault, (
            f"{node.name}: fromAddr mismatch"
        )
        assert transfer["toAddr"] == v2_vault, (
            f"{node.name}: toAddr mismatch"
        )
        assert transfer["amount"] == transfer_amount, (
            f"{node.name}: amount mismatch"
        )
        assert transfer["success"] is True, (
            f"{node.name}: transfer not marked successful"
        )
        assert transfer["failReason"] == "", (
            f"{node.name}: expected empty failReason, got '{transfer['failReason']}'"
        )

    logging.info("Block API transfer info verified on readonly for block %s", block_hash[:16])

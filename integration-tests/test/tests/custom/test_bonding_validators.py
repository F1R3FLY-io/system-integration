"""
Bonding Validators Integration Test

Verifies that a new validator can bond to the network via the PoS contract
and become an active validator at the next epoch boundary.

Uses a custom shard with:
- 2 genesis validators (V1, V2) with equal bonds
- epoch-length = 4 (epoch change at block numbers 4, 8, 12, ...)
- quarantine-length = 20
- heartbeat disabled (manual block orchestration)
- FTT = -1 (immediate finalization for deterministic block numbering)

The test:
1. Confirms the joiner is not initially bonded
2. Adds the joiner node to the shard network
3. Verifies the joiner cannot propose before bonding
4. Bonds the joiner via the PoS contract (deploy on an existing validator)
5. Verifies the bond is recorded but the joiner is not yet active
6. Advances the chain to the epoch boundary
7. Confirms the joiner can now propose blocks

VALIDATOR4_ID is added dynamically via shard.add_joiner() after the shard
is running.
"""

import logging

import pytest
from f1r3fly.client import F1r3flyClientException

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR4_ID
from ...infra.polling import wait_for_block_visible
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

_BOND_AMOUNT = 10_000_000

_EPOCH_CLI_OPTIONS = {
    "--epoch-length": "4",
    "--quarantine-length": "20",
    "--synchrony-constraint-threshold": "0",
}


def test_bonding_validators(provider, timeouts) -> None:
    """Verify that a new validator can bond and become active at the epoch boundary."""

    # Seed the joiner's vault at genesis so it has tokens for the bond + phlo
    joiner_vault_address = VALIDATOR4_ID.private_key().get_public_key().get_vault_address()
    joiner_genesis_balance = 50_000_000_000_000_000

    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, _BOND_AMOUNT),
            (VALIDATOR2_ID, _BOND_AMOUNT),
        ],
        ftt=-1,
        heartbeat=False,
        global_cli_options=_EPOCH_CLI_OPTIONS,
        extra_wallets=[(joiner_vault_address, joiner_genesis_balance)],
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")
        v2 = shard.node("validator2")

        # ── Block 1: Initial deploy by V1 ──
        logging.info("Block 1: Initial deploy by V1")
        v1.deploy_string(
            '@"hello-pre-bond"!(1)',
            VALIDATOR1_ID.private_key(),
            phlo_limit=100_000_000,
            phlo_price=1,
        )
        b1 = v1.propose()
        logging.info("Block 1: %s", b1[:16])

        # Verify the joiner is not yet bonded
        block_info = v1.get_block(b1)
        bonded_validators = {b.validator for b in block_info.blockInfo.bonds}
        assert VALIDATOR4_ID.public_hex not in bonded_validators, (
            f"Joiner {VALIDATOR4_ID.public_hex[:16]}... should not be bonded yet"
        )

        # ── Add joiner node to the shard ──
        joiner_cli = {
            "--epoch-length": "4",
            "--quarantine-length": "20",
            "--synchrony-constraint-threshold": "0",
        }
        with shard.add_joiner(
            VALIDATOR4_ID,
            cli_options=joiner_cli,
            cli_flags={"--heartbeat-disabled"},
        ) as joiner:
            # Wait for joiner to see the latest block
            wait_for_block_visible(joiner, b1, timeout=timeouts.deploy_inclusion)

            # ── Joiner cannot propose before bonding ──
            joiner.deploy_string(
                '@"should-fail"!(0)',
                VALIDATOR4_ID.private_key(),
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            with pytest.raises(F1r3flyClientException):
                joiner.propose()

            # ── Block 2: Deploy the bond contract ──
            logging.info("Block 2: Bonding deploy (amount=%d)", _BOND_AMOUNT)
            v1.deploy_rho_file(
                rho_file_path="resources/wallets/bond.rho",
                private_key=VALIDATOR4_ID.private_key(),
                substitutions={"%AMOUNT": str(_BOND_AMOUNT)},
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            b2 = v1.propose()
            logging.info("Block 2 (bond): %s", b2[:16])

            # Verify the bond is recorded in the bonds map
            block_info = v1.get_block(b2)
            bonds_map = {b.validator: b.stake for b in block_info.blockInfo.bonds}
            assert bonds_map.get(VALIDATOR4_ID.public_hex) == _BOND_AMOUNT, (
                f"Expected joiner bond={_BOND_AMOUNT}, got "
                f"{bonds_map.get(VALIDATOR4_ID.public_hex)}"
            )
            logging.info(
                "Bond recorded: %s -> %d",
                VALIDATOR4_ID.public_hex[:16], _BOND_AMOUNT,
            )

            # ── Block 3: Filler deploy ──
            logging.info("Block 3: Filler deploy")
            v1.deploy_string(
                '@"filler-3"!(3)',
                VALIDATOR1_ID.private_key(),
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            b3 = v1.propose()
            logging.info("Block 3: %s", b3[:16])

            # Joiner still cannot propose (epoch change hasn't happened)
            wait_for_block_visible(joiner, b3, timeout=timeouts.deploy_inclusion)
            joiner.deploy_string(
                '@"still-inactive"!(0)',
                VALIDATOR4_ID.private_key(),
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            with pytest.raises(F1r3flyClientException):
                joiner.propose()

            # ── Block 4: Epoch boundary (block number 4) ──
            logging.info("Block 4: Epoch boundary")
            v1.deploy_string(
                '@"epoch-change"!(4)',
                VALIDATOR1_ID.private_key(),
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            b4 = v1.propose()
            logging.info("Block 4 (epoch change): %s", b4[:16])

            # Wait for joiner to see the epoch change block
            wait_for_block_visible(joiner, b4, timeout=timeouts.deploy_inclusion)

            # ── Block 5: V2 filler after epoch boundary ──
            # The joiner's earlier (rejected) propose attempt left a "previous
            # block" context. If the joiner tries to propose immediately, its
            # parent set is all ancestors of that previous context and the
            # self-validation check rejects with InvalidParents ("validator has
            # not made progress"). Having another validator propose first gives
            # the joiner a fresh parent that breaks the no-progress condition.
            logging.info("Block 5: V2 filler to advance DAG for joiner")
            v2.deploy_string(
                '@"post-epoch-filler"!(5)',
                VALIDATOR2_ID.private_key(),
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            b5 = v2.propose()
            logging.info("Block 5 (V2 filler): %s", b5[:16])
            wait_for_block_visible(joiner, b5, timeout=timeouts.deploy_inclusion)

            # ── Block 6: Joiner can now propose ──
            logging.info("Block 6: Joiner proposes after epoch activation")
            joiner.deploy_string(
                '@"joiner-active"!(6)',
                VALIDATOR4_ID.private_key(),
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            b6 = joiner.propose()
            logging.info("Block 6 (joiner): %s", b6[:16])

            # Verify the joiner's block is visible on V1
            wait_for_block_visible(v1, b6, timeout=timeouts.deploy_inclusion)

            logging.info("Bonding test passed -- joiner activated at epoch boundary")
    finally:
        shard.destroy()

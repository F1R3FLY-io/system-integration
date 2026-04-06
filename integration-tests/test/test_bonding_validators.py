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
2. Bonds the joiner via the PoS contract (deploy on an existing validator)
3. Verifies the bond is recorded in the bonds map but the joiner is not
   yet active (cannot propose)
4. Advances the chain to the epoch boundary
5. Confirms the joiner can now propose blocks

A fourth validator (VALIDATOR4_ID) is added dynamically via add_peer_to_shard
after the shard is running.
"""

import logging
import time

import pytest
from docker.client import DockerClient
from f1r3fly.client import F1r3flyClientException

from .common import (
    CommandLineOptions,
    NotAnActiveValidatorError,
)
from .conftest import (
    VALIDATOR1_ID,
    VALIDATOR1_KEY,
    VALIDATOR2_ID,
    VALIDATOR2_KEY,
    VALIDATOR4_ID,
    VALIDATOR4_KEY,
    start_custom_shard,
    add_peer_to_shard,
)
from .rnode import Node

pytestmark = pytest.mark.xdist_group("custom")


def _poll_block_visible(node: Node, block_hash: str, timeout: int = 120, scale: float = 1.0) -> None:
    """Poll until a block is visible on the given node."""
    scaled_timeout = int(timeout * scale)
    deadline = time.time() + scaled_timeout
    while time.time() < deadline:
        try:
            node.get_block(block_hash)
            return
        except Exception:
            time.sleep(3)
    raise AssertionError(
        f"Block {block_hash[:16]}... not visible on {node.name} within {scaled_timeout}s"
    )


def test_bonding_validators(
    docker_client: DockerClient,
    command_line_options: CommandLineOptions,
) -> None:
    """Verify that a new validator can bond and become active at the epoch boundary."""

    bonds = [
        (VALIDATOR1_ID, 10_000_000),
        (VALIDATOR2_ID, 10_000_000),
    ]

    # The joiner needs tokens in its vault to cover the bond amount + phlo costs.
    # Compute the vault address from VALIDATOR4's public key and seed it at genesis.
    joiner_vault_address = VALIDATOR4_KEY.get_public_key().get_vault_address()
    joiner_genesis_balance = 50_000_000_000_000_000  # same as other genesis validators

    with start_custom_shard(
        docker_client, command_line_options,
        bonds=bonds,
        ftt=-1,
        heartbeat=False,
        port_base=40700,
        extra_wallets=[(joiner_vault_address, joiner_genesis_balance)],
        global_cli_options={
            "--epoch-length": "4",
            "--quarantine-length": "20",
            "--synchrony-constraint-threshold": "0",
        },
    ) as shard:
        v1 = shard.nodes["validator1"]
        v2 = shard.nodes["validator2"]
        scale = command_line_options.timeout_scale

        # ── Block 1: Initial deploy by V1 ──
        logging.info("Block 1: Initial deploy by V1")
        v1.deploy_string(
            '@"hello-pre-bond"!(1)',
            VALIDATOR1_KEY,
            phlo_limit=100_000_000,
            phlo_price=1,
        )
        b1 = v1.propose()
        logging.info("Block 1: %s", b1[:16])

        # Verify the joiner is not yet bonded
        block_info = v1.get_block(b1)
        bonded_validators = {
            b.validator for b in block_info.blockInfo.bonds
        }
        joiner_pub_hex = VALIDATOR4_ID.public_hex
        assert joiner_pub_hex not in bonded_validators, (
            f"Joiner {joiner_pub_hex[:16]}... should not be bonded yet"
        )

        # ── Add joiner node to the shard ──
        with add_peer_to_shard(
            docker_client, command_line_options,
            shard, VALIDATOR4_ID,
            cli_options={
                "--epoch-length": "4",
                "--quarantine-length": "20",
                "--synchrony-constraint-threshold": "0",
                "--heartbeat-disabled": "",
            },
        ) as joiner:
            # Wait for joiner to see the latest block
            _poll_block_visible(joiner, b1, scale=scale)

            # Joiner cannot propose before bonding (not an active validator)
            joiner.deploy_string(
                '@"should-fail"!(0)',
                VALIDATOR4_KEY,
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            with pytest.raises((NotAnActiveValidatorError, F1r3flyClientException)):
                joiner.propose()

            # ── Block 2: Deploy the bond contract ──
            # Next epoch change is at block number 4
            bond_amount = 10_000_000
            logging.info("Block 2: Bonding deploy (amount=%d)", bond_amount)
            v1.deploy_contract_with_substitution(
                substitute_dict={"%AMOUNT": str(bond_amount)},
                rho_file_path="resources/wallets/bond.rho",
                private_key=VALIDATOR4_KEY,
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            b2 = v1.propose()
            logging.info("Block 2 (bond): %s", b2[:16])

            # Verify the bond is recorded in the bonds map
            block_info = v1.get_block(b2)
            bonds_map = {
                b.validator: b.stake for b in block_info.blockInfo.bonds
            }
            assert bonds_map.get(joiner_pub_hex) == bond_amount, (
                f"Expected joiner bond={bond_amount}, got "
                f"{bonds_map.get(joiner_pub_hex)}"
            )
            logging.info("Bond recorded: %s -> %d", joiner_pub_hex[:16], bond_amount)

            # ── Block 3: Filler deploy ──
            logging.info("Block 3: Filler deploy")
            v1.deploy_string(
                '@"filler-3"!(3)',
                VALIDATOR1_KEY,
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            b3 = v1.propose()
            logging.info("Block 3: %s", b3[:16])

            # Joiner still cannot propose (epoch change hasn't happened)
            _poll_block_visible(joiner, b3, scale=scale)
            joiner.deploy_string(
                '@"still-inactive"!(0)',
                VALIDATOR4_KEY,
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            with pytest.raises((NotAnActiveValidatorError, F1r3flyClientException)):
                joiner.propose()

            # ── Block 4: Epoch boundary (block number 4) ──
            logging.info("Block 4: Epoch boundary")
            v1.deploy_string(
                '@"epoch-change"!(4)',
                VALIDATOR1_KEY,
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            b4 = v1.propose()
            logging.info("Block 4 (epoch change): %s", b4[:16])

            # Wait for joiner to see the epoch change block
            _poll_block_visible(joiner, b4, scale=scale)

            # ── Block 5: V2 filler after epoch boundary ──
            # The joiner's earlier (rejected) propose attempt left a
            # "previous block" context.  If the joiner tries to propose
            # immediately, its parent set is all ancestors of that previous
            # context and the self-validation check rejects with
            # InvalidParents ("validator has not made progress").
            # Having another validator propose first gives the joiner a
            # fresh parent that breaks the no-progress condition.
            logging.info("Block 5: V2 filler to advance DAG for joiner")
            v2.deploy_string(
                '@"post-epoch-filler"!(5)',
                VALIDATOR2_KEY,
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            b5 = v2.propose()
            logging.info("Block 5 (V2 filler): %s", b5[:16])
            _poll_block_visible(joiner, b5, scale=scale)

            # ── Block 6: Joiner can now propose ──
            logging.info("Block 6: Joiner proposes after epoch activation")
            joiner.deploy_string(
                '@"joiner-active"!(6)',
                VALIDATOR4_KEY,
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            b6 = joiner.propose()
            logging.info("Block 6 (joiner): %s", b6[:16])

            # Verify the joiner's block is visible on V1
            _poll_block_visible(v1, b6, scale=scale)

            logging.info("Bonding test passed -- joiner activated at epoch boundary")

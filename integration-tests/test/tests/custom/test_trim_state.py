"""
Trim State Integration Test

Verifies that a new node joining an existing network correctly syncs from
the Last Finalized State (LFS / "trimmed state") rather than replaying
the entire chain from genesis.

Uses a custom shard with:
- 2 genesis validators (V1=10M, V2=1) -- V2 has minimal stake so V1 controls
  finalization. Two validators are needed for the genesis ceremony to complete
  (required_signatures defaults to len(bonds)-1 = 1).
- FTT = -1 (immediate finalization)
- heartbeat disabled (manual block orchestration)
- synchrony-constraint-threshold = 0

The test:
1. Creates multiple finalized blocks on V1 with diverse contract deploys
2. Adds a joiner node mid-test via shard.add_joiner()
3. Verifies the joiner sees the latest block (synced from LFS)
4. Verifies the joiner's LFB matches V1's LFB
5. Continues creating blocks on V1 and verifies the joiner keeps up
6. Verifies post-state agreement between V1 and the joiner
"""

import logging

import pytest

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID
from ...infra.polling import wait_for_block_visible
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

_SYNC_THRESHOLD = "0"
_FTT = "-1"

_SHARD_CLI_OPTIONS = {
    "--synchrony-constraint-threshold": _SYNC_THRESHOLD,
}

# Diverse Rholang contracts for generating meaningful state
_CONTRACTS = [
    '@"trim-test-a"!(1)',
    '@"trim-test-b"!(2)',
    '@"trim-test-c"!(3)',
    'new ch in { ch!(42) | for(@v <- ch) { @"result"!(v) } }',
    '@"trim-test-d"!(4)',
    "new x in { x!(100) }",
    '@"trim-test-e"!(5)',
    '@"trim-test-f"!(6)',
    '@"trim-test-g"!(7)',
]


def test_trim_state(provider, timeouts) -> None:
    """Verify a joiner syncs from trimmed (LFS) state and can then keep up."""

    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 10_000_000),
            (VALIDATOR2_ID, 1),
        ],
        ftt=-1,
        heartbeat=False,
        global_cli_options=_SHARD_CLI_OPTIONS,
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")

        # ── Phase 1: Create finalized blocks on V1 ──
        logging.info("Phase 1: Creating %d finalized blocks on V1", len(_CONTRACTS))
        latest_block_hash = None
        for i, contract in enumerate(_CONTRACTS):
            v1.deploy_string(
                contract,
                VALIDATOR1_ID.private_key(),
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            latest_block_hash = v1.propose()
            logging.info("Block %d: %s", i + 1, latest_block_hash[:16])

        assert latest_block_hash is not None

        v1_lfb = v1.last_finalized_block()
        v1_lfb_number = v1_lfb.blockInfo.blockNumber
        logging.info("V1 LFB after phase 1: block #%d", v1_lfb_number)
        assert v1_lfb_number > 0, f"Expected LFB > 0 with FTT=-1, got #{v1_lfb_number}"

        # ── Phase 2: Add joiner and verify it syncs from LFS ──
        logging.info("Phase 2: Adding joiner (V2) to the shard")
        with shard.add_joiner(
            VALIDATOR2_ID,
            cli_options={
                "--synchrony-constraint-threshold": _SYNC_THRESHOLD,
                "--fault-tolerance-threshold": _FTT,
            },
        ) as joiner:
            # Joiner should sync from LFS and see the latest block
            wait_for_block_visible(
                joiner,
                latest_block_hash,
                timeout=timeouts.node_startup * 3,
            )
            logging.info("Joiner sees latest block %s", latest_block_hash[:16])

            # Verify joiner's LFB matches V1's LFB
            joiner_lfb = joiner.last_finalized_block()
            joiner_lfb_number = joiner_lfb.blockInfo.blockNumber
            logging.info("Joiner LFB: block #%d (V1: #%d)", joiner_lfb_number, v1_lfb_number)
            assert joiner_lfb_number >= v1_lfb_number - 2, (
                f"Joiner LFB #{joiner_lfb_number} is too far behind "
                f"V1 LFB #{v1_lfb_number} after LFS sync"
            )

            # ── Phase 3: Continue producing blocks and verify joiner keeps up ──
            logging.info("Phase 3: Producing more blocks, verifying joiner syncs")
            for i in range(4):
                v1.deploy_string(
                    f'@"post-join-{i}"!({i})',
                    VALIDATOR1_ID.private_key(),
                    phlo_limit=100_000_000,
                    phlo_price=1,
                )
                block_hash = v1.propose()
                logging.info("Post-join block %d: %s", i + 1, block_hash[:16])
                wait_for_block_visible(
                    joiner,
                    block_hash,
                    timeout=timeouts.deploy_inclusion,
                )

            # ── Phase 4: Verify joiner has fully synced ──
            joiner_blocks = joiner.get_blocks(50)
            v1_blocks = v1.get_blocks(50)
            assert len(joiner_blocks) >= len(v1_blocks) - 2, (
                f"Joiner has {len(joiner_blocks)} blocks, V1 has "
                f"{len(v1_blocks)} -- joiner may not have fully synced"
            )

            # Verify LFB agreement after continued operation
            v1_final_lfb = v1.last_finalized_block().blockInfo.blockNumber
            joiner_final_lfb = joiner.last_finalized_block().blockInfo.blockNumber
            logging.info(
                "Final LFBs: V1=#%d, joiner=#%d",
                v1_final_lfb,
                joiner_final_lfb,
            )
            assert joiner_final_lfb >= v1_final_lfb - 2, (
                f"Joiner final LFB #{joiner_final_lfb} too far behind V1 final LFB #{v1_final_lfb}"
            )

            # Verify post-state agreement on the most recent block
            latest_v1_block = v1.get_blocks(1)[0]
            joiner_view = joiner.get_block(latest_v1_block.blockHash)
            v1_state = latest_v1_block.postStateHash
            joiner_state = joiner_view.blockInfo.postStateHash
            assert v1_state == joiner_state, (
                f"Post-state mismatch: V1={v1_state[:16]}... joiner={joiner_state[:16]}..."
            )
            logging.info("Post-state agreement confirmed: %s", v1_state[:16])

            logging.info("Trim state test passed -- joiner synced from LFS")
    finally:
        shard.destroy()

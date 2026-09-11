"""
Bonded-but-absent validator: finalization must survive a validator that
bonds stake and never runs a node.

Red acceptance test for F1R3FLY-io/f1r3node-rust#18. A validator whose
bond is sealed and then epoch-activated while its node never comes
online enters the fault-tolerance denominator without ever contributing
a latest message. With pivotal stake it can never be part of the
agreeing set, so no block carrying the post-activation weight map
finalizes: block production continues, the LFB freezes at the last
pre-activation block, and every later deploy waits forever.

The stake geometry makes the stall deterministic rather than
FTT-marginal: 400 absent vs 300 live trips the majority short-circuit
(agreeing * 2 <= total stake) regardless of the threshold.

The test asserts the DESIRED behavior — deploys keep finalizing after
the absent validator activates — so today it fails at the final
assertion by finalization timeout. It goes green when the committee
becomes participation-aware (issue #18's remaining work).
"""

import logging

import pytest

from ...infra.assertions import assert_all_deploys_finalized_on_all_nodes
from ...infra.config import ShardConfig
from ...infra.keys import (
    VALIDATOR1_ID,
    VALIDATOR2_ID,
    VALIDATOR3_ID,
    VALIDATOR4_ID,
)
from ...infra.polling import (
    poll_until,
    wait_for_deploy_finalized,
    wait_for_deploy_included,
)
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

_GENESIS_STAKE = 100
_ABSENT_BOND_AMOUNT = 400
_EPOCH_LENGTH = 4
_WALLET_BALANCE = 50_000_000_000_000_000
_PHLO_LIMIT = 100_000_000


@pytest.fixture(scope="module")
def absent_bond_shard(provider, timeouts):
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, _GENESIS_STAKE),
            (VALIDATOR2_ID, _GENESIS_STAKE),
            (VALIDATOR3_ID, _GENESIS_STAKE),
        ],
        ftt=0.1,
        heartbeat=True,
        include_readonly=True,
        extra_wallets=[
            (
                VALIDATOR4_ID.private_key().get_public_key().get_vault_address(),
                _WALLET_BALANCE,
            )
        ],
        global_cli_options={
            # Activation happens at an epoch boundary, so the shard needs a
            # short epoch; set explicitly rather than inherited from
            # conf/rust.conf.
            "--epoch-length": str(_EPOCH_LENGTH),
            "--quarantine-length": "10",
            "--bond-minimum": "100",
            "--bond-maximum": "1000",
        },
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        yield shard
    finally:
        shard.destroy()


def test_finalization_survives_bonded_absent_validator(absent_bond_shard, timeouts):
    shard = absent_bond_shard
    v1, v2, v3, ro = (
        shard.node("validator1"),
        shard.node("validator2"),
        shard.node("validator3"),
        shard.readonly,
    )
    all_nodes = [v1, v2, v3, ro]

    # Healthy baseline: a pre-bond deploy finalizes everywhere.
    baseline_id = v1.deploy_string(
        '@"absent-bond-baseline"!(0)',
        VALIDATOR1_ID.private_key(),
        phlo_limit=_PHLO_LIMIT,
        phlo_price=1,
    )
    assert_all_deploys_finalized_on_all_nodes(
        all_nodes,
        [baseline_id],
        timeouts.finalization * 3,
        label="pre-bond baseline",
    )

    # Bond VALIDATOR4. No node is ever attached for it — that absence is
    # the scenario under test.
    bond_deploy_id = v1.deploy_rho_file(
        rho_file_path="resources/wallets/bond.rho",
        private_key=VALIDATOR4_ID.private_key(),
        substitutions={"%AMOUNT": str(_ABSENT_BOND_AMOUNT)},
        phlo_limit=_PHLO_LIMIT,
        phlo_price=1,
    )
    wait_for_deploy_included(v1, bond_deploy_id, timeouts.deploy_inclusion * 3)
    # Blocks up to the activation boundary carry pre-activation weights, so
    # the bond block itself still finalizes.
    status = wait_for_deploy_finalized(v1, bond_deploy_id, timeouts.finalization * 3)
    logging.info(
        "Bond for %s sealed and finalized in %s",
        VALIDATOR4_ID.name,
        status.latestBlockHash.hex()[:16],
    )

    # Activation: after the next epoch boundary the tip's bonds map carries
    # the absent validator's stake.
    def _absent_validator_active():
        tips = v1.get_blocks(1)
        if not tips:
            return None
        info = v1.get_block(tips[0].blockHash)
        bonds = {b.validator: b.stake for b in info.blockInfo.bonds}
        if bonds.get(VALIDATOR4_ID.public_hex) == _ABSENT_BOND_AMOUNT:
            return info
        return None

    activated = poll_until(
        predicate=_absent_validator_active,
        timeout=timeouts.epoch_transition,
        interval=timeouts.poll_interval,
        description="absent validator activates into the tip's bonds map",
    )
    lfb_at_activation = v1.get_current_block_number()
    logging.info(
        "Absent validator active in tip block #%d (LFB #%d); asserting finalization survives",
        activated.blockInfo.blockNumber,
        lfb_at_activation,
    )

    # Desired behavior (issue #18): finalization survives an activated
    # validator that never participates. Today the LFB freezes at the last
    # pre-activation block and this assertion times out.
    post_activation_id = v1.deploy_string(
        '@"absent-bond-post-activation"!(0)',
        VALIDATOR1_ID.private_key(),
        phlo_limit=_PHLO_LIMIT,
        phlo_price=1,
    )
    assert_all_deploys_finalized_on_all_nodes(
        all_nodes,
        [post_activation_id],
        timeouts.finalization * 3,
        label="post-activation",
    )

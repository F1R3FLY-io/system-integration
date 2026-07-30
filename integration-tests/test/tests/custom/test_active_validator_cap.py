"""Active-validator cap — ``pickActiveValidators`` take(numberOfActiveValidators).

A dedicated shard whose active-validator cap is SMALLER than the bonded set it grows to,
exercising the PoS contract's ``take($$numberOfActiveValidators$$)`` limit: more
validators bond than the cap allows active, so the over-cap ones stay bonded-but-inactive.

Lives in its OWN file (not woven into ``test_validator_lifecycle``) because:
  - the cap is a genesis-time parameter, and the lifecycle shard must boot UNCAPPED so
    its joiners can all activate (it peaks at 6 active) — contradictory genesis needs; and
  - the subprocess provider uses FIXED per-session data dirs (`<session>/<role>`), so two
    live module-scoped shards in one file collide on them. A separate module's shard is
    created only after the previous module tears its shard down, so the dirs are reused
    sequentially (no overlap) — provided the run does not use ``--keep-running``.

Reuses the lifecycle module's shard helpers (`_attach_prebond`, `_validators_on`, etc.)
rather than duplicating them.
"""

import logging

import pytest

from ...infra.assertions import assert_block_finalized_on_all_nodes
from ...infra.config import ShardConfig
from ...infra.keys import (
    VALIDATOR1_ID,
    VALIDATOR2_ID,
    VALIDATOR3_ID,
    VALIDATOR4_ID,
    VALIDATOR5_ID,
)
from ...infra.polling import (
    poll_until,
    wait_for_deploy_included,
    wait_for_finalized,
)
from ...infra.shard import Shard
from .test_validator_lifecycle import (
    _BOND_PHLO_LIMIT,
    _BOND_PHLO_PRICE,
    _EPOCH_LENGTH,
    _GENESIS_CLI,
    _GENESIS_STAKE,
    _JOINER_STAKE,
    _WALLET_BALANCE,
    _advance_lfb,
    _attach_prebond,
    _validators_on,
    _vault_addr,
)

pytestmark = pytest.mark.xdist_group("custom")

_ACTIVE_CAP = 3


@pytest.fixture(scope="module")
def cap_shard(provider, timeouts):
    """A shard whose active-validator cap (== genesis count) is smaller than the bonded
    set it will grow to, so ``pickActiveValidators`` take(N) is actually exercised."""
    extra_wallets = [
        (_vault_addr(ident), _WALLET_BALANCE) for ident in (VALIDATOR4_ID, VALIDATOR5_ID)
    ]
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, _GENESIS_STAKE),
            (VALIDATOR2_ID, _GENESIS_STAKE),
            (VALIDATOR3_ID, _GENESIS_STAKE),
        ],
        ftt=0.1,
        heartbeat=True,
        include_readonly=True,
        global_cli_options={**_GENESIS_CLI, "--number-of-active-validators": str(_ACTIVE_CAP)},
        extra_wallets=extra_wallets,
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        yield shard
    finally:
        shard.destroy()


def test_active_validator_cap(cap_shard, timeouts) -> None:
    """``pickActiveValidators`` take(numberOfActiveValidators): with the cap == 3 and 5
    validators bonded, exactly 3 are ACTIVE (``/validators``) while all 5 are BONDED
    (``getBonds``) — the 2 over-cap joiners are bonded-but-inactive. Count-based (which 3
    are active depends on pubkey sort order); the cap (== 3) and the full bonded set
    (== 5) are the invariants, and the capped shard must keep finalizing on all nodes."""
    shard = cap_shard
    v1, v2, v3 = (shard.node("validator1"), shard.node("validator2"), shard.node("validator3"))
    ro = shard.readonly

    # Genesis fills the cap exactly at boot.
    poll_until(
        predicate=lambda: True if len(_validators_on(ro)) == _ACTIVE_CAP else None,
        timeout=timeouts.epoch_transition,
        interval=timeouts.poll_interval,
        description=f"genesis active set == cap ({_ACTIVE_CAP})",
    )

    # Bring two joiners online and bond them OVER the cap. They need not enter the active
    # set (that is the property under test), so bond directly rather than via _submit_bonds
    # (which waits for /validators membership).
    _attach_prebond(shard, VALIDATOR4_ID, timeouts)
    _attach_prebond(shard, VALIDATOR5_ID, timeouts)
    for proposer, ident in [(v1, VALIDATOR4_ID), (v2, VALIDATOR5_ID)]:
        did = proposer.pos.bond(ident.private_key(), _JOINER_STAKE[ident.name])
        blk = wait_for_deploy_included(proposer, did, timeouts.deploy_inclusion * 3)
        wait_for_finalized(proposer, blk.blockNumber, timeouts.finalization * 3)
        assert proposer.pos.read_result(did, blk.blockHash).success, f"{ident.name} bond failed"

    # All five land in allBonds, but the active set stays capped.
    poll_until(
        predicate=lambda: True if len(ro.pos.get_bonds()) == 5 else None,
        timeout=timeouts.epoch_transition,
        interval=timeouts.poll_interval,
        description="all 5 validators in allBonds",
    )
    # Cross an epoch boundary so pickActiveValidators runs on the 5-bonded set.
    _advance_lfb(v1, _EPOCH_LENGTH + 1, timeouts)

    bonded = ro.pos.get_bonds()
    active = _validators_on(ro)
    assert len(bonded) == 5, f"expected 5 bonded, got {len(bonded)}: {bonded}"
    assert len(active) == _ACTIVE_CAP, (
        f"active set must cap at {_ACTIVE_CAP} despite 5 bonded, got {len(active)}: {active}"
    )
    assert set(active) <= set(bonded), f"active not a subset of bonded: {active} vs {bonded}"

    # The capped shard must still finalize — prove via an ACTIVE genesis (at most 2 of 5
    # are inactive, so at least one genesis remains active).
    live_node, live_id = next(
        (n, ident)
        for n, ident in [(v1, VALIDATOR1_ID), (v2, VALIDATOR2_ID), (v3, VALIDATOR3_ID)]
        if ident.public_hex in active
    )
    lid = live_node.deploy_string(
        '@"cap-liveness"!(1)',
        live_id.private_key(),
        phlo_limit=_BOND_PHLO_LIMIT,
        phlo_price=_BOND_PHLO_PRICE,
    )
    lb = wait_for_deploy_included(live_node, lid, timeouts.deploy_inclusion * 5)
    wait_for_finalized(live_node, lb.blockNumber, timeouts.finalization * 5)
    assert_block_finalized_on_all_nodes(
        shard.all_nodes, lb.blockHash, timeout=timeouts.finalization * 5
    )
    logging.info(
        "active-validator cap: 5 bonded, exactly %d active, shard still finalizes", _ACTIVE_CAP
    )

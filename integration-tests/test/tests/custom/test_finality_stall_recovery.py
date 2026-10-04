"""Finality stalls cleanly under quorum loss and resumes when quorum returns.

With two of three equal-stake validators paused nothing can finalize. The
survivor must hold the LFB still while emitting empty recovery blocks only at
the bounded stale-recovery cadence (one per --heartbeat-stale-recovery-min-
interval, with the empty-frontier width cap engaged between rounds), must
still admit a deploy into a block while stalled, and must finalize that
deploy once quorum comes back.
"""

import time

import pytest

from ...infra.assertions import assert_all_deploys_finalized_on_all_nodes
from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.log_events import marker
from ...infra.polling import (
    wait_for_deploy_included,
    wait_for_lfb_converged,
    wait_for_node_quiet,
)
from ...infra.shard import Shard

pytestmark = [
    pytest.mark.xdist_group("custom"),
    pytest.mark.requires_node_capabilities("finality-stall-recovery"),
]

_HEARTBEAT_SUCCESS = marker("HeartbeatBlockCreated")
_BACKPRESSURE_ACTIVE = marker("HeartbeatBackpressureActive")

# Unscaled budget for a paused node to stop answering, via the timeouts fixture.
_QUIET_BUDGET = 10

# Unscaled deadline for the LFB to settle. stable_for and the sampling window
# below are NOT scaled: they define the measurement, not how long to wait for it.
_STABLE_LFB_BUDGET = 90

# The stale-recovery cadence pinned on the shard, and the fixed sampling window
# the emission bound is measured over. Each stale-recovery mint requires the
# survivor to have been idle for a full interval, so the window admits at most
# floor(window / interval) + 1 stale-lane blocks; +1 for the once-per-stalled-LFB
# convergence round (fires only if the LFB-hash-seeded leader is the survivor)
# and +1 for an async-propose completing after the next check already sampled a
# stale self-age. Anything above this ceiling means the cadence gate regressed
# toward per-check minting (which would produce ~window/check-interval blocks).
_RECOVERY_INTERVAL_S = 5
_STALL_WINDOW_S = 45
_MAX_WINDOW_BLOCKS = _STALL_WINDOW_S // _RECOVERY_INTERVAL_S + 3


def _wait_for_stable_lfb(node, stable_for: float = 35, timeout: float = 90) -> int:
    deadline = time.monotonic() + timeout
    current = node.last_finalized_block().blockInfo.blockNumber
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(1)
        observed = node.last_finalized_block().blockInfo.blockNumber
        if observed != current:
            current = observed
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= stable_for:
            return current
    raise TimeoutError(f"LFB did not remain stable for {stable_for}s within {timeout}s")


@pytest.fixture(scope="module")
def recovery_shard(provider, timeouts):
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        heartbeat=True,
        global_cli_options={
            # check-interval strictly below stale-recovery-min-interval, so at
            # least one heartbeat check always lands inside the survivor's
            # post-mint throttle window — the backpressure log line below is
            # structural evidence, not a phase-drift lottery.
            "--heartbeat-check-interval": "2seconds",
            "--heartbeat-max-lfb-age": "5seconds",
            "--heartbeat-self-propose-cooldown": "5seconds",
            "--heartbeat-stale-recovery-min-interval": f"{_RECOVERY_INTERVAL_S}seconds",
            "--heartbeat-advanced-empty-frontier-max-unfinalized-blocks": "4",
        },
    )
    shard = Shard.create(provider, config, timeouts)
    yield shard
    shard.destroy()


def test_finality_stall_bounded_recovery(recovery_shard, timeouts) -> None:
    """LFB freezes without runaway empty blocks, then advances once quorum returns."""
    v1 = recovery_shard.node("validator1")
    validators = [
        v1,
        recovery_shard.node("validator2"),
        recovery_shard.node("validator3"),
    ]
    unavailable = [
        validators[1],
        validators[2],
    ]

    warmup_deploys = [
        v1.deploy_string(
            '@"finality-stall-warmup"!(1)',
            VALIDATOR1_ID.private_key(),
        )
    ]
    assert_all_deploys_finalized_on_all_nodes(
        recovery_shard.all_nodes,
        warmup_deploys,
        timeouts.finalization * 4,
        label="pre-stall-warmup",
    )
    wait_for_lfb_converged(
        recovery_shard.all_nodes,
        timeout=timeouts.finalization * 2,
        max_spread=3,
        description="healthy finalized baseline before quorum loss",
    )
    for node in unavailable:
        node.pause()
    try:
        for node in unavailable:
            wait_for_node_quiet(node, timeout=timeouts.custom(_QUIET_BUDGET))

        baseline_lfb = _wait_for_stable_lfb(v1, timeout=timeouts.custom(_STABLE_LFB_BUDGET))
        logs_before = v1.logs()
        successes_before = logs_before.count(_HEARTBEAT_SUCCESS)
        backpressure_before = logs_before.count(_BACKPRESSURE_ACTIVE)
        time.sleep(_STALL_WINDOW_S)
        window_logs = v1.logs()
        empty_recovery_blocks = window_logs.count(_HEARTBEAT_SUCCESS) - successes_before
        backpressure_firings = window_logs.count(_BACKPRESSURE_ACTIVE) - backpressure_before
        # The stall must be a bounded heartbeat, not silence and not churn:
        # at least one recovery block proves the stale-recovery lane is alive
        # (a dead lane would pass any pure ceiling vacuously), and the derived
        # ceiling proves each mint waited out the full recovery interval.
        assert 1 <= empty_recovery_blocks <= _MAX_WINDOW_BLOCKS, (
            f"stalled LFB produced {empty_recovery_blocks} empty recovery blocks in "
            f"{_STALL_WINDOW_S}s; expected 1..{_MAX_WINDOW_BLOCKS} at one per "
            f"{_RECOVERY_INTERVAL_S}s stale-recovery interval"
        )
        # The width cap must have engaged between recovery rounds: by the time
        # the window opens, the stable-LFB wait has already accumulated more
        # unfinalized blocks than the cap of 4, so every post-mint throttle
        # window reports backpressure with the unfinalized count and the cap.
        assert backpressure_firings >= 1, (
            "empty-frontier backpressure never engaged during the stall window; "
            "the width cap is not bounding empty-block churn"
        )
        stalled_lfb = v1.last_finalized_block().blockInfo.blockNumber
        assert stalled_lfb == baseline_lfb

        deploy_id = v1.deploy_string(
            '@"pending-bypasses-empty-frontier-pressure"!(1)',
            VALIDATOR1_ID.private_key(),
        )
        included = wait_for_deploy_included(
            v1,
            deploy_id,
            timeout=timeouts.deploy_inclusion * 3,
        )
    finally:
        for node in unavailable:
            node.unpause()

    assert_all_deploys_finalized_on_all_nodes(
        recovery_shard.all_nodes,
        [deploy_id],
        timeouts.finalization * 4,
        label="post-stall-pending-deploy",
    )
    # min_height carries the recovery assertion: every node must be at least three
    # blocks past the stalled LFB *and* past the block the pending deploy landed in,
    # with the whole shard inside max_spread of each other at that same sample.
    wait_for_lfb_converged(
        recovery_shard.all_nodes,
        timeout=timeouts.finalization * 4,
        min_height=max(baseline_lfb + 3, included.blockNumber),
        max_spread=5,
        description="finality resumes after quorum returns",
    )

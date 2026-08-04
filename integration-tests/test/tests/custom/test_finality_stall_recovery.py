import time

import pytest

from ...infra.assertions import assert_all_deploys_finalized_on_all_nodes
from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import (
    wait_for_deploy_included,
    wait_for_lfb_converged,
    wait_for_node_quiet,
)
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

_HEARTBEAT_SUCCESS = "Heartbeat: Successfully created block"


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
            "--heartbeat-check-interval": "5seconds",
            "--heartbeat-max-lfb-age": "5seconds",
            "--heartbeat-self-propose-cooldown": "5seconds",
            "--heartbeat-stale-recovery-min-interval": "5seconds",
            "--heartbeat-advanced-empty-frontier-max-unfinalized-blocks": "4",
        },
    )
    shard = Shard.create(provider, config, timeouts)
    yield shard
    shard.destroy()


def test_finality_stall_bounded_recovery(recovery_shard, timeouts) -> None:
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
            wait_for_node_quiet(node, timeout=10)

        baseline_lfb = _wait_for_stable_lfb(v1)
        successes_before = v1.logs().count(_HEARTBEAT_SUCCESS)
        time.sleep(45)
        empty_recovery_blocks = v1.logs().count(_HEARTBEAT_SUCCESS) - successes_before
        assert empty_recovery_blocks <= 2, (
            f"stalled LFB produced {empty_recovery_blocks} empty recovery blocks "
            "during bounded recovery"
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
    lfbs = wait_for_lfb_converged(
        recovery_shard.all_nodes,
        timeout=timeouts.finalization * 4,
        min_height=max(baseline_lfb + 3, included.blockNumber),
        max_spread=5,
        description="finality resumes after quorum returns",
    )
    assert min(lfbs.values()) > baseline_lfb

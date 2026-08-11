"""An observer retries blocks its peers could not serve, then reproduces their state.

While an observer is performing approved-state sync, every source node is paused so
its block requests go unanswered. It must schedule resends rather than giving up,
and once the sources return it must reach the target LFB and compute the same
post-state hash the shard did — a retry that resumed but diverged would be worse
than one that failed.
"""

import pytest

from ...infra.config import deterministic_history_shard_config
from ...infra.keys import VALIDATOR1_ID
from ...infra.log_events import marker
from ...infra.polling import (
    poll_until,
    propose_until_included,
    wait_for_block_visible,
    wait_for_lfb_at_least,
    wait_for_node_running,
)
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

# Each of these predicates fetches the node's whole log file over a
# docker exec, so the interval is what keeps the loop from running that
# continuously for the length of a node-startup budget.
_LOG_POLL_INTERVAL = 0.5

# Unscaled budget for a log marker to appear, via the timeouts fixture.
_LOG_WAIT_BUDGET = 15


@pytest.fixture(scope="module")
def active_shard(provider, timeouts):
    shard = Shard.create(provider, deterministic_history_shard_config(), timeouts)
    yield shard
    shard.destroy()


def test_observer_retries_missing_block_after_peer_returns(active_shard, timeouts) -> None:
    """Resends are scheduled while sources are down; state matches once they return."""
    v1 = active_shard.node("validator1")
    sources = list(active_shard.all_nodes)
    key = VALIDATOR1_ID.private_key()
    baseline = v1.last_finalized_block().blockInfo.blockNumber
    latest_hash = ""
    for index in range(5):
        deploy_id = v1.deploy_string(
            f"new ch in {{ ch!({index}) | for (_ <- ch) {{ Nil }} }}",
            key,
            valid_after_block_no=0,
        )
        latest_hash = propose_until_included(
            v1,
            deploy_id,
            timeout=timeouts.custom(120),
        )
    for source in sources:
        wait_for_block_visible(source, latest_hash, timeouts.command)
    wait_for_lfb_at_least(
        v1,
        baseline + 5,
        timeout=timeouts.finalization * 2,
    )
    target = v1.last_finalized_block().blockInfo
    assert target.blockHash == latest_hash

    with active_shard.add_observer(
        cli_options={"--network-timeout": "2seconds"},
        wait_running=False,
    ) as observer:
        poll_until(
            lambda: True if marker("ApprovedStateRequestStarted") in observer.logs() else None,
            timeout=timeouts.node_startup,
            interval=_LOG_POLL_INTERVAL,
            description="observer starts approved-state synchronization",
        )

        try:
            for source in sources:
                source.pause()
            poll_until(
                lambda: True if marker("LfsBlockRequesterStarted") in observer.logs() else None,
                timeout=timeouts.custom(_LOG_WAIT_BUDGET),
                interval=_LOG_POLL_INTERVAL,
                description="observer starts block retrieval while sources are unavailable",
            )
            poll_until(
                lambda: True if marker("BlockRequestResend") in observer.logs() else None,
                timeout=timeouts.custom(_LOG_WAIT_BUDGET),
                interval=_LOG_POLL_INTERVAL,
                description="observer schedules a resend for missing blocks",
            )
        finally:
            for source in sources:
                source.unpause()

        wait_for_node_running(
            get_logs=observer.logs,
            is_running=observer.is_running,
            node_name=observer.name,
            timeout=timeouts.node_startup,
            status_url=f"{observer.http_url}/api/status",
        )
        wait_for_lfb_at_least(
            observer,
            target.blockNumber,
            timeout=timeouts.node_startup,
        )
        wait_for_block_visible(
            observer,
            target.blockHash,
            timeout=timeouts.deploy_inclusion * 3,
        )
        observer_view = observer.get_block(target.blockHash)
        assert observer_view.blockInfo.postStateHash == target.postStateHash

"""An observer retries blocks its peers could not serve, then reproduces their state.

While an observer is performing approved-state sync, every source node is paused so
its block requests go unanswered. It must schedule resends rather than giving up,
and once the sources return it must reach the target LFB and compute the same
post-state hash the shard did — a retry that resumed but diverged would be worse
than one that failed.
"""

import pytest
from f1r3fly.client import F1r3flyClientException

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import (
    poll_until,
    wait_for_block_visible,
    wait_for_lfb_at_least,
    wait_for_node_running,
)
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")


def _propose_until_included(node, deploy_id: str, timeout: int) -> str:
    def attempt():
        try:
            return node.find_deploy(deploy_id).blockHash
        except F1r3flyClientException:
            pass

        try:
            node.propose()
        except F1r3flyClientException as exc:
            if "No new deploys" not in str(exc) and "another propose is in progress" not in str(
                exc
            ):
                raise

        try:
            return node.find_deploy(deploy_id).blockHash
        except F1r3flyClientException:
            return None

    return poll_until(
        attempt,
        timeout=timeout,
        interval=0.5,
        description=f"deploy {deploy_id[:24]} becomes available and is proposed",
    )


@pytest.fixture(scope="module")
def active_shard(provider, timeouts):
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 10_000_000),
            (VALIDATOR2_ID, 1),
            (VALIDATOR3_ID, 1),
        ],
        ftt=-1,
        heartbeat=False,
    )
    shard = Shard.create(provider, config, timeouts)
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
        latest_hash = _propose_until_included(
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
            lambda: True if "request_approved_state: start" in observer.logs() else None,
            timeout=timeouts.node_startup,
            interval=0.01,
            description="observer starts approved-state synchronization",
        )

        try:
            for source in sources:
                source.pause()
            poll_until(
                lambda: (
                    True if "LFS Block Requester stream initialized" in observer.logs() else None
                ),
                timeout=15,
                interval=0.05,
                description="observer starts block retrieval while sources are unavailable",
            )
            poll_until(
                lambda: True if "No responses for" in observer.logs() else None,
                timeout=15,
                interval=0.25,
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

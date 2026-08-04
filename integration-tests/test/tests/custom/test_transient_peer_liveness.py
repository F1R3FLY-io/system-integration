import time

import pytest

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import poll_until, wait_for_node_quiet
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")


@pytest.fixture(scope="module")
def fast_liveness_shard(provider, timeouts):
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        heartbeat=True,
        global_cli_options={
            "--network-timeout": "1second",
            "--discovery-cleanup-interval": "2seconds",
            "--discovery-lookup-interval": "2seconds",
        },
    )
    shard = Shard.create(provider, config, timeouts)
    yield shard
    shard.destroy()


def test_transient_peer_failure_does_not_disconnect(fast_liveness_shard, timeouts) -> None:
    observer = fast_liveness_shard.node("validator1")
    peer = fast_liveness_shard.node("validator3")
    baseline_peers = poll_until(
        lambda: count if (count := observer.api_get("/status")["peers"]) >= 3 else None,
        timeout=timeouts.deploy_inclusion * 3,
        interval=1,
        description="validator1 connects to all shard peers",
    )

    first_marker = "failed (1/3); retaining connection"
    second_marker = "failed (2/3); retaining connection"
    first_count = observer.logs().count(first_marker)

    peer.pause()
    try:
        wait_for_node_quiet(peer, timeout=10)
        poll_until(
            lambda: True if observer.logs().count(first_marker) > first_count else None,
            timeout=10,
            interval=0.25,
            description="first failed heartbeat retains peer",
        )
        assert observer.api_get("/status")["peers"] >= baseline_peers
    finally:
        peer.unpause()

    poll_until(
        lambda: True if observer.api_get("/status")["peers"] >= baseline_peers else None,
        timeout=15,
        interval=1,
        description="peer reconnects after transient failure",
    )
    time.sleep(4)

    first_count = observer.logs().count(first_marker)
    second_count = observer.logs().count(second_marker)
    peer.pause()
    try:
        wait_for_node_quiet(peer, timeout=10)
        poll_until(
            lambda: True if observer.logs().count(first_marker) > first_count else None,
            timeout=10,
            interval=0.25,
            description="successful heartbeat reset failure streak",
        )
        assert observer.logs().count(second_marker) == second_count

        poll_until(
            lambda: (
                True
                if "Removing peer" in observer.logs()
                and observer.api_get("/status")["peers"] < baseline_peers
                else None
            ),
            timeout=15,
            interval=0.5,
            description="three consecutive failures remove peer",
        )
    finally:
        peer.unpause()

    poll_until(
        lambda: True if observer.api_get("/status")["peers"] >= baseline_peers else None,
        timeout=20,
        interval=1,
        description="removed peer is rediscovered",
    )

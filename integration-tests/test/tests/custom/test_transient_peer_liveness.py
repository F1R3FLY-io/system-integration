"""Peer cleanup tolerates transient heartbeat failures but still evicts dead peers.

A single missed heartbeat must not drop a peer, a successful heartbeat must reset
the failure streak, and only a sustained streak may remove one — after which
discovery must find the peer again. Runs with one-second network timeouts and
two-second cleanup/discovery intervals so the streak plays out in seconds.
"""

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
    """One failure retains the peer, a success resets the streak, three evict, then rediscovery."""
    observer = fast_liveness_shard.node("validator1")
    peer = fast_liveness_shard.node("validator3")
    baseline_peers = poll_until(
        lambda: count if (count := observer.api_get("/status")["peers"]) >= 3 else None,
        timeout=timeouts.deploy_inclusion * 3,
        interval=1,
        description="validator1 connects to all shard peers",
    )

    first_marker = "failed (1/3); retaining connection"
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
    # The streak resets on a SUCCESSFUL heartbeat after the peer returns, so wait
    # for evidence of one rather than sleeping a fixed interval: the count of
    # first-failure lines must stop growing, which it only does once the peer is
    # answering again.
    settled = observer.logs().count(first_marker)

    def _failures_stopped():
        nonlocal settled
        current = observer.logs().count(first_marker)
        if current == settled:
            return True
        settled = current
        return None

    poll_until(
        _failures_stopped,
        timeout=20,
        interval=1.0,
        description="heartbeat failures stop once the restored peer answers",
    )

    first_count = observer.logs().count(first_marker)
    peer.pause()
    try:
        wait_for_node_quiet(peer, timeout=10)
        # A *fresh* 1/3 line is itself the proof that the streak reset: had the
        # earlier failure carried over, the next one would have been logged 2/3.
        # Do not additionally snapshot the 2/3 count here — the streak is meant to
        # continue to 3/3 immediately below, and failures accrue about once per
        # --network-timeout (1s), so any such snapshot races the 1/3 -> 2/3
        # transition while the predicate itself costs a log read.
        poll_until(
            lambda: True if observer.logs().count(first_marker) > first_count else None,
            timeout=10,
            interval=0.25,
            description="a new first-failure line proves the success reset the streak",
        )

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

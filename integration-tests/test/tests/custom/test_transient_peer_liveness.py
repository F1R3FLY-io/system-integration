"""Peer cleanup tolerates transient heartbeat failures but still evicts dead peers.

A single missed heartbeat must not drop a peer, a successful heartbeat must reset
the failure streak, and only a sustained streak may remove one — after which
discovery must find the peer again. Runs with one-second network timeouts and
two-second cleanup/discovery intervals so the streak plays out in seconds.
"""

import pytest

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.log_events import marker
from ...infra.polling import poll_until, wait_for_node_quiet
from ...infra.shard import Shard

pytestmark = [
    pytest.mark.xdist_group("custom"),
    pytest.mark.requires_node_capabilities("transient-peer-liveness"),
]

# Unscaled budgets, run through the timeouts fixture so --timeout-scale reaches
# them. Sized against --network-timeout=1second and the two-second cleanup and
# discovery intervals this shard runs with: a failure or a reconnect is expected
# within a few of those cycles, and removal after three consecutive failures.
_QUIET_BUDGET = 10
_MARKER_BUDGET = 10
_RECONNECT_BUDGET = 15
_SETTLE_BUDGET = 20
_REMOVAL_BUDGET = 20
_REDISCOVERY_BUDGET = 20

# Consecutive unchanged failure-count samples required to call the streak settled,
# and the gap between them. Three samples one second apart span two real intervals
# — longer than the ~1s failure cadence and the 2s discovery cleanup cycle — so a
# quiet run cannot be one sample landing between two failures.
_QUIET_SAMPLES = 3
_QUIET_SAMPLE_INTERVAL = 1.0


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
        wait_for_node_quiet(peer, timeout=timeouts.custom(_QUIET_BUDGET))
        poll_until(
            lambda: True if observer.logs().count(first_marker) > first_count else None,
            timeout=timeouts.custom(_MARKER_BUDGET),
            interval=0.25,
            description="first failed heartbeat retains peer",
        )
        assert observer.api_get("/status")["peers"] >= baseline_peers
    finally:
        peer.unpause()

    poll_until(
        lambda: True if observer.api_get("/status")["peers"] >= baseline_peers else None,
        timeout=timeouts.custom(_RECONNECT_BUDGET),
        interval=1,
        description="peer reconnects after transient failure",
    )
    # The streak resets on a SUCCESSFUL heartbeat after the peer returns, so wait
    # for evidence of one rather than sleeping a fixed interval: the count of
    # first-failure lines must stop growing, which it only does once the peer is
    # answering again.
    #
    # Require _QUIET_SAMPLES *consecutive* unchanged observations. A single
    # unchanged reading proves nothing — failures accrue about once per
    # --network-timeout (1s), so any one sample can fall between two of them, and
    # poll_until evaluates its predicate before checking the deadline, so a
    # predicate satisfied by one reading returns immediately without waiting at
    # all. `count=None` initially so the first observation only establishes the
    # baseline and cannot score toward the streak.
    observed = {"count": None, "streak": 0}

    def _failures_stopped():
        current = observer.logs().count(first_marker)
        if observed["count"] == current:
            observed["streak"] += 1
        else:
            observed["count"] = current
            observed["streak"] = 0
        return True if observed["streak"] >= _QUIET_SAMPLES else None

    poll_until(
        _failures_stopped,
        timeout=timeouts.custom(_SETTLE_BUDGET),
        interval=_QUIET_SAMPLE_INTERVAL,
        description=(
            f"{_QUIET_SAMPLES} consecutive samples with no new failure line, "
            "proving the restored peer is answering again"
        ),
    )

    first_count = observer.logs().count(first_marker)
    peer.pause()
    try:
        wait_for_node_quiet(peer, timeout=timeouts.custom(_QUIET_BUDGET))
        # A *fresh* 1/3 line is itself the proof that the streak reset: had the
        # earlier failure carried over, the next one would have been logged 2/3.
        # Do not additionally snapshot the 2/3 count here — the streak is meant to
        # continue to 3/3 immediately below, and failures accrue about once per
        # --network-timeout (1s), so any such snapshot races the 1/3 -> 2/3
        # transition while the predicate itself costs a log read.
        poll_until(
            lambda: True if observer.logs().count(first_marker) > first_count else None,
            timeout=timeouts.custom(_MARKER_BUDGET),
            interval=0.25,
            description="a new first-failure line proves the success reset the streak",
        )

        poll_until(
            lambda: (
                True
                if marker("PeerRemoved") in observer.logs()
                and observer.api_get("/status")["peers"] < baseline_peers
                else None
            ),
            timeout=timeouts.custom(_REMOVAL_BUDGET),
            interval=0.5,
            description="three consecutive failures remove peer",
        )
    finally:
        peer.unpause()

    poll_until(
        lambda: True if observer.api_get("/status")["peers"] >= baseline_peers else None,
        timeout=timeouts.custom(_REDISCOVERY_BUDGET),
        interval=1,
        description="removed peer is rediscovered",
    )

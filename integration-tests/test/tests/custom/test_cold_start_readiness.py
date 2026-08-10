"""API readiness is gated on having a last finalized block during cold start.

A node that is serving HTTP but has not finalized anything yet must report
``isReady=false``, so orchestration cannot route traffic to a node whose
canonical state does not exist. Samples ``/api/status`` until every node has an
LFB and requires every response carrying ``lastFinalizedBlockNumber == -1`` to be
not-ready, then requires every node to reach ready with a real LFB.

Observing the pre-LFB window is best-effort — it can close between polls on a
fast host — so a run that misses it verifies only the becomes-ready path.
"""

import logging
import time

import pytest
import requests

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import poll_until, wait_for_node_running

pytestmark = pytest.mark.xdist_group("custom")

# Seconds between status sweeps during the pre-LFB window. Short enough to catch
# a fast genesis, long enough not to spin the API.
_SAMPLE_INTERVAL = 0.05


def test_cold_start_readiness_requires_lfb(provider, timeouts) -> None:
    """No node reports ready while its LFB is -1; every node reaches ready with one."""
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        heartbeat=True,
    )
    handles = provider.create_shard(config, wait_running=False)
    try:
        status_urls = {
            handle.name: f"http://{handle.grpc_host}:{handle.ports.http}/api/status"
            for handle in handles
        }

        # Sample until every node has an LFB, not until the first pre-LFB response
        # arrives: the invariant is "no status with LFB -1 ever claims readiness",
        # which a single sample cannot establish. Stopping early also made the
        # readiness gate — the whole subject of this test — untested in practice.
        pre_lfb_statuses = []
        ready_before_lfb = []
        deadline = time.time() + timeouts.node_startup
        pending = set(status_urls)
        while time.time() < deadline and pending:
            for name, url in status_urls.items():
                if name not in pending:
                    continue
                try:
                    response = requests.get(url, timeout=0.5)
                except requests.RequestException:
                    continue
                if response.status_code != 200:
                    continue
                status = response.json()
                if status.get("lastFinalizedBlockNumber") == -1:
                    pre_lfb_statuses.append(status)
                    if status.get("isReady") is not False:
                        ready_before_lfb.append((name, status.get("isReady")))
                else:
                    pending.discard(name)
            time.sleep(_SAMPLE_INTERVAL)

        # Observing the pre-LFB window is best-effort: it only exists between the
        # HTTP API accepting connections and the first block finalizing, and on a
        # fast host that can close between polls. Failing when it is missed would
        # fail a correctly-behaving node, so log it and let the readiness check
        # below pass vacuously — what must never happen is a node claiming ready
        # inside the window, not the window being narrow.
        if not pre_lfb_statuses:
            logging.warning(
                "cold-start readiness: never observed a status with LFB -1, so the "
                "pre-LFB readiness gate went unexercised this run; only the "
                "becomes-ready path below was verified"
            )
        assert not ready_before_lfb, (
            f"nodes reported ready while their LFB was -1: {ready_before_lfb} "
            f"(observed {len(pre_lfb_statuses)} pre-LFB responses in total)"
        )

        for handle in handles:
            wait_for_node_running(
                get_logs=handle.logs,
                is_running=handle.is_running,
                node_name=handle.name,
                timeout=timeouts.node_startup,
                status_url=status_urls[handle.name],
            )

        for handle in handles:
            url = status_urls[handle.name]
            poll_until(
                lambda url=url: (
                    status
                    if (status := requests.get(url, timeout=2).json()).get(
                        "lastFinalizedBlockNumber", -1
                    )
                    >= 0
                    and status.get("isReady") is True
                    else None
                ),
                timeout=timeouts.finalization,
                interval=0.5,
                description=f"{handle.name} becomes ready with an LFB",
            )
    finally:
        provider.destroy_shard(handles)

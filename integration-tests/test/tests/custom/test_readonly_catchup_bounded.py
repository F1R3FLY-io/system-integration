"""A readonly observer catching up keeps its API responsive and its memory bounded.

A 40-block history is built on a single-finalizer shard, then a fresh observer is
attached and must converge to the shard's canonical state while its `/api/status`
keeps answering and its resident memory stays under the ceiling shared with the
overload regression. Converging by wedging the API or ballooning memory is not
converging.
"""

import threading
import time

import pytest
import requests

from ...infra.config import deterministic_history_shard_config
from ...infra.keys import VALIDATOR1_ID
from ...infra.polling import (
    propose_until_included,
    wait_for_block_visible,
    wait_for_lfb_at_least,
    wait_for_node_running,
)
from ...infra.resource_monitor import OBSERVER_MEMORY_CEILING_MB, sample_peak_memory_mb
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

# Seconds the observer is allowed to take to issue a /api/status response.
_PROBE_BUDGET = 2.0

# Unscaled budget for the post-catch-up exploratory query, passed through
# timeouts.custom() so --timeout-scale reaches it.
_QUERY_HTTP_BUDGET = 15

# Floor on the share of probes answered inside the budget. A share rather than a
# count, so it does not depend on how long catch-up happens to take.
_MIN_RESPONSIVE_SHARE = 0.95

# Floor on probes completed, so a catch-up too short to sample says so rather
# than passing on one lucky reading. Kept at the original bar: probes only start
# landing once the observer answers at all, and nothing establishes how wide that
# window is. At counts this low the share below effectively requires every probe
# to be in budget.
_MIN_PROBES = 5


@pytest.fixture(scope="module")
def deep_shard(provider, timeouts):
    shard = Shard.create(provider, deterministic_history_shard_config(), timeouts)
    yield shard
    shard.destroy()


def test_readonly_catchup_parallelism_keeps_api_responsive(
    deep_shard, timeouts, readonly_history_blocks
) -> None:
    """The observer reaches canonical state with its API answering and memory bounded."""
    source = deep_shard.node("validator1")
    history_blocks = readonly_history_blocks
    baseline = source.last_finalized_block().blockInfo.blockNumber
    history = []
    for index in range(history_blocks):
        valid_after = max(
            0,
            source.last_finalized_block().blockInfo.blockNumber - 1,
        )
        deploy_id = source.deploy_string(
            f"new ch in {{ ch!({index}) | for (_ <- ch) {{ Nil }} }}",
            VALIDATOR1_ID.private_key(),
            valid_after_block_no=valid_after,
        )
        history.append(
            propose_until_included(
                source,
                deploy_id,
                timeout=timeouts.custom(120),
            )
        )
    assert len(set(history)) == history_blocks
    wait_for_lfb_at_least(
        source,
        baseline + history_blocks,
        timeout=timeouts.finalization * 2,
    )
    target = source.last_finalized_block().blockInfo

    with deep_shard.add_observer(wait_running=False) as observer:
        stop = threading.Event()
        latencies: list = []
        over_budget: list = []
        memory_samples: list = []

        def probe_status() -> None:
            """Sample ``/api/status``, recording blown budgets as well as fast replies.

            ``requests``' timeout bounds how long the observer may take to ISSUE a
            response, so a stalled probe raises rather than returning a large
            latency. Those have to be counted, not dropped: they are the whole
            signal that catch-up is starving the read API, and discarding them
            leaves a bound on ``max(latencies)`` that cannot fail.

            Connection errors before the observer has ever answered are startup,
            not latency, and are ignored.
            """
            url = f"{observer.http_url}/api/status"
            while not stop.is_set():
                started = time.monotonic()
                try:
                    response = requests.get(url, timeout=_PROBE_BUDGET)
                    if response.status_code == 200:
                        latencies.append(time.monotonic() - started)
                    else:
                        over_budget.append(f"HTTP {response.status_code}")
                except requests.Timeout:
                    over_budget.append(f"no response within {_PROBE_BUDGET}s")
                except requests.RequestException as err:
                    if latencies or over_budget:
                        over_budget.append(type(err).__name__)
                stop.wait(0.1)

        threads = [
            threading.Thread(target=probe_status, daemon=True),
            threading.Thread(
                target=sample_peak_memory_mb,
                args=(observer, stop, memory_samples),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()

        try:
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
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=5)

        probes = len(latencies) + len(over_budget)
        assert probes >= _MIN_PROBES, f"only {probes} status probes completed"
        responsive_share = len(latencies) / probes
        assert responsive_share >= _MIN_RESPONSIVE_SHARE, (
            f"observer answered /api/status inside {_PROBE_BUDGET}s for only "
            f"{responsive_share:.0%} of {probes} probes during catch-up "
            f"(floor {_MIN_RESPONSIVE_SHARE:.0%}); {len(over_budget)} exceeded it, "
            f"first few: {over_budget[:3]}"
        )
        assert memory_samples, "observer resource usage was never available"
        assert max(memory_samples) < OBSERVER_MEMORY_CEILING_MB

        observer_view = observer.get_block(target.blockHash)
        assert observer_view.blockInfo.postStateHash == target.postStateHash
        # api_post raises on any non-2xx, so reaching the next line IS the
        # assertion that the observer serves reads again after catching up.
        observer.api_post(
            "/explore-deploy",
            {"term": "new x in { x!(1) }"},
            timeout=timeouts.custom(_QUERY_HTTP_BUDGET),
        )

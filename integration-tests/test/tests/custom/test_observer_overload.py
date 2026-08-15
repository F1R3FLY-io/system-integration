"""A readonly observer bounds exploratory-query admission and recovers afterwards.

With the exploratory executor occupied, further queries must fail fast with a
bounded rejection rather than queueing without limit or growing observer memory.
Verifies the rejection shape, that the observer stays ready while rejecting, that
memory stays under the catch-up regression's ceiling, and that query capacity
returns once the permit is released.
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import poll_until
from ...infra.resource_monitor import OBSERVER_MEMORY_CEILING_MB, sample_peak_memory_mb
from ...infra.shard import Shard

pytestmark = [
    pytest.mark.xdist_group("custom"),
    pytest.mark.requires_node_capabilities("observer-exploratory-backpressure"),
]

_EXCESS_COUNT = 8

# Memory sampling gap. Kept tighter than the shared default because this test
# watches a short overload burst rather than a minutes-long catch-up.
_MEMORY_SAMPLE_INTERVAL = 0.1

# Unscaled client-side HTTP budgets, passed through timeouts.custom() so
# --timeout-scale reaches them like every other deadline in the suite.
_SLOW_HTTP_BUDGET = 90
_QUERY_HTTP_BUDGET = 15

# `exploratory_deploy_active` is incremented inside the execution task, after the
# permit is taken, so a non-zero reading means the permit is held.
_PERMIT_GAUGE = "exploratory_deploy_active"
_REJECTED_COUNTER = "exploratory_deploy_rejected"
_PERMIT_WAIT_BUDGET = 30
_PERMIT_POLL_INTERVAL = 0.05

# An attempt whose burst was not fully contended proves nothing about admission
# bounding, so it is retried rather than asserted on.
_MAX_OVERLOAD_ATTEMPTS = 5

# Occupies the permit by exhausting the exploratory phlo budget. The query always
# dies on out_of_phlogistons, so the loop count does not set the window width —
# api-server.exploratory-deploy-phlo-limit does.
_SLOW_QUERY = """
new loop in {
  contract loop(@n) = {
    if (n <= 0) { Nil } else { loop!(n - 1) }
  } |
  loop!(100000)
}
"""


def _metric_value(node, name: str) -> float:
    """Sum one Prometheus counter or gauge across its label sets.

    ``infra.metrics.scrape_metrics`` reads only its own allowlist of histogram
    pairs, so these series are read directly. A counter that has never been
    incremented is not exported, so absence is zero.
    """
    total = 0.0
    for line in node.http_get("/metrics", timeout=10).text.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith(f"{name}{{") or line.startswith(f"{name} "):
            match = re.search(r"\s+([\d.eE+-]+)$", line)
            if match:
                total += float(match.group(1))
    return total


def _wait_for_permit_held(node, timeouts) -> None:
    """Block until the observer reports an exploratory execution in flight."""
    poll_until(
        lambda: _metric_value(node, _PERMIT_GAUGE) > 0,
        timeout=timeouts.custom(_PERMIT_WAIT_BUDGET),
        interval=_PERMIT_POLL_INTERVAL,
        description=f"{_PERMIT_GAUGE} > 0 on {node.name}",
    )


def _attempt_overload(observer, url, timeouts):
    """Occupy the permit, then burst against it.

    Returns ``(responses, slow_response, rejected_delta)``.
    """
    with ThreadPoolExecutor(max_workers=_EXCESS_COUNT + 1) as executor:
        slow = executor.submit(
            requests.post,
            url,
            json={"term": _SLOW_QUERY},
            timeout=timeouts.custom(_SLOW_HTTP_BUDGET),
        )
        _wait_for_permit_held(observer, timeouts)
        rejected_before = _metric_value(observer, _REJECTED_COUNTER)
        excess = [
            executor.submit(
                requests.post,
                url,
                json={"term": "new x in { x!(1) }"},
                timeout=timeouts.custom(_QUERY_HTTP_BUDGET),
            )
            for _ in range(_EXCESS_COUNT)
        ]
        responses = [future.result() for future in excess]
        slow_response = slow.result()
    rejected_after = _metric_value(observer, _REJECTED_COUNTER)
    return responses, slow_response, rejected_after - rejected_before


@pytest.fixture(scope="module")
def observer_shard(provider, timeouts):
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        heartbeat=True,
        include_readonly=True,
    )
    shard = Shard.create(provider, config, timeouts)
    yield shard
    shard.destroy()


def test_observer_exploratory_overload_recovers(observer_shard, timeouts) -> None:
    """Excess exploratory queries fail fast with bounded memory, then capacity returns."""
    observer = observer_shard.readonly
    url = f"{observer.http_url}/api/explore-deploy"
    stop = threading.Event()
    memory_samples: list = []

    sampler = threading.Thread(
        target=sample_peak_memory_mb,
        args=(observer, stop, memory_samples, _MEMORY_SAMPLE_INTERVAL),
        daemon=True,
    )
    sampler.start()

    try:
        for _ in range(_MAX_OVERLOAD_ATTEMPTS):
            responses, slow_response, rejected_delta = _attempt_overload(observer, url, timeouts)
            statuses = [response.status_code for response in responses]
            # A 200 means the permit was released mid-burst, so the burst was not
            # contended and there is nothing to assert. Retry instead of failing.
            if 200 not in statuses:
                break
        else:
            pytest.fail(
                f"no fully contended burst in {_MAX_OVERLOAD_ATTEMPTS} attempts "
                f"(last statuses={statuses}); the window is one exploratory phlo "
                f"budget wide, so widening it means raising "
                f"api-server.exploratory-deploy-phlo-limit"
            )
    finally:
        stop.set()
        sampler.join(timeout=5)

    assert statuses == [503] * _EXCESS_COUNT
    assert all(response.json().get("error") == "observer_busy" for response in responses)

    # The rejections came from the admission bound, not another path that answers 503.
    assert rejected_delta == _EXCESS_COUNT, (
        f"{_REJECTED_COUNTER} advanced by {rejected_delta:.0f}, expected {_EXCESS_COUNT}"
    )

    # The occupying query completed, exhausted phlo, or hit the execution timeout.
    assert slow_response.status_code in {200, 422, 504}
    if slow_response.status_code == 504:
        assert slow_response.json().get("error") == "exploratory_timeout"
    assert memory_samples, "observer resource usage was never available"
    assert max(memory_samples) < OBSERVER_MEMORY_CEILING_MB
    assert observer.api_get("/status")["isReady"] is True

    recovered = requests.post(
        url,
        json={"term": "new x in { x!(1) }"},
        timeout=timeouts.custom(_QUERY_HTTP_BUDGET),
    )
    assert recovered.status_code == 200, recovered.text

"""A readonly observer bounds exploratory-query admission and recovers afterwards.

With the exploratory executor occupied, further queries must fail fast with a
bounded rejection rather than queueing without limit or growing observer memory.
Verifies the rejection shape, that the observer stays ready while rejecting, that
memory stays under the catch-up regression's ceiling, and that query capacity
returns once the permit is released.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

_OBSERVER_MEMORY_CEILING_MB = 1500

_EXCESS_COUNT = 8

# Seconds between occupying the permit and sending the excess requests.
_EXCESS_DELAY = 0.5

# How long a queued exploratory request waits for the permit before the observer
# rejects it as observer_busy. The occupying query therefore has to hold the
# permit for longer than _EXCESS_DELAY + this, or the excess requests are never
# actually contended and "all rejected" is not the property under test. Asserted
# below rather than assumed.
_QUEUE_TIMEOUT = 2.0

_SLOW_QUERY = """
new loop in {
  contract loop(@n) = {
    if (n <= 0) { Nil } else { loop!(n - 1) }
  } |
  loop!(100000)
}
"""


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


def test_observer_exploratory_overload_recovers(observer_shard) -> None:
    """Excess exploratory queries fail fast with bounded memory, then capacity returns."""
    observer = observer_shard.readonly
    url = f"{observer.http_url}/api/explore-deploy"
    stop = threading.Event()
    memory_samples = []

    def sample_memory() -> None:
        while not stop.is_set():
            memory = float(observer.resource_usage().get("memory_mb", 0) or 0)
            if memory > 0:
                memory_samples.append(memory)
            stop.wait(0.1)

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()

    try:
        with ThreadPoolExecutor(max_workers=_EXCESS_COUNT + 1) as executor:
            started = time.monotonic()
            slow = executor.submit(
                requests.post,
                url,
                json={"term": _SLOW_QUERY},
                timeout=90,
            )
            time.sleep(_EXCESS_DELAY)
            excess = [
                executor.submit(
                    requests.post,
                    url,
                    json={"term": "new x in { x!(1) }"},
                    timeout=15,
                )
                for _ in range(_EXCESS_COUNT)
            ]
            responses = [future.result() for future in excess]
            slow_response = slow.result()
            slow_held_permit_for = time.monotonic() - started
    finally:
        stop.set()
        sampler.join(timeout=5)

    # Precondition, asserted rather than assumed: the excess requests are only
    # contended if the permit was still held while they queued. The observer caps
    # exploratory phlo server-side, so a client cannot make _SLOW_QUERY arbitrarily
    # long — if it finishes early the run proves nothing about rejection, and that
    # must read as a setup failure rather than as a rejection-count mismatch.
    assert slow_held_permit_for > _EXCESS_DELAY + _QUEUE_TIMEOUT, (
        f"the occupying query released the permit after {slow_held_permit_for:.1f}s, "
        f"before the {_EXCESS_COUNT} excess requests finished queueing "
        f"({_EXCESS_DELAY + _QUEUE_TIMEOUT:.1f}s) — no overload window existed, so "
        f"this run cannot test admission bounding. Increase the work in _SLOW_QUERY, "
        f"or the server-side exploratory phlo limit."
    )

    # Having held the permit past the queue window, the occupying query either
    # completed, exhausted phlo, or hit the observer's bounded execution timeout.
    assert slow_response.status_code in {200, 422, 504}
    if slow_response.status_code == 504:
        assert slow_response.json().get("error") == "exploratory_timeout"
    assert [response.status_code for response in responses] == [503] * len(responses)
    assert all(response.json().get("error") == "observer_busy" for response in responses)
    assert memory_samples, "observer resource usage was never available"
    assert max(memory_samples) < _OBSERVER_MEMORY_CEILING_MB
    assert observer.api_get("/status")["isReady"] is True

    recovered = requests.post(
        url,
        json={"term": "new x in { x!(1) }"},
        timeout=15,
    )
    assert recovered.status_code == 200, recovered.text

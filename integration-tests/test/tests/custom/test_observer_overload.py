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
        with ThreadPoolExecutor(max_workers=9) as executor:
            slow = executor.submit(
                requests.post,
                url,
                json={"term": _SLOW_QUERY},
                timeout=90,
            )
            time.sleep(0.5)
            excess = [
                executor.submit(
                    requests.post,
                    url,
                    json={"term": "new x in { x!(1) }"},
                    timeout=15,
                )
                for _ in range(8)
            ]
            responses = [future.result() for future in excess]
            slow_response = slow.result()
    finally:
        stop.set()
        sampler.join(timeout=5)

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

"""A readonly observer catching up keeps its API responsive and its memory bounded.

A 40-block history is built on a single-finalizer shard, then a fresh observer is
attached and must converge to the shard's canonical state while its `/api/status`
keeps answering and its resident memory stays under the ceiling shared with the
overload regression. Converging by wedging the API or ballooning memory is not
converging.

NOTE: the latency bound here is weaker than it reads — see the comment on the
status-probe loop.
"""

import os
import threading
import time

import pytest
import requests
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

DEFAULT_HISTORY_BLOCKS = 40
_OBSERVER_MEMORY_CEILING_MB = 1500


def _history_block_count() -> int:
    raw = os.environ.get(
        "F1R3FLY_READONLY_HISTORY_BLOCKS",
        str(DEFAULT_HISTORY_BLOCKS),
    )
    try:
        count = int(raw)
    except ValueError as exc:
        raise ValueError("F1R3FLY_READONLY_HISTORY_BLOCKS must be positive") from exc
    if count <= 0:
        raise ValueError("F1R3FLY_READONLY_HISTORY_BLOCKS must be positive")
    return count


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
def deep_shard(provider, timeouts):
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


def test_readonly_catchup_parallelism_keeps_api_responsive(deep_shard, timeouts) -> None:
    """The observer reaches canonical state with its API answering and memory bounded."""
    source = deep_shard.node("validator1")
    history_blocks = _history_block_count()
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
            _propose_until_included(
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
        latencies = []
        memory_samples = []

        def probe_status() -> None:
            # NOTE: `timeout=2` bounds how long the observer may take to ISSUE a
            # response, so a probe that stalls past it raises and is dropped here
            # rather than recorded. That makes `max(latencies) < 2` below far weaker
            # than it reads — recorded samples are fast probes almost by
            # construction, and the interesting signal (probes that blew the budget)
            # is exactly what is discarded. Counting the timeouts would make the
            # bound falsifiable.
            url = f"{observer.http_url}/api/status"
            while not stop.is_set():
                started = time.monotonic()
                try:
                    response = requests.get(url, timeout=2)
                    if response.status_code == 200:
                        latencies.append(time.monotonic() - started)
                except requests.RequestException:
                    pass
                stop.wait(0.1)

        def sample_memory() -> None:
            while not stop.is_set():
                usage = observer.resource_usage()
                memory = float(usage.get("memory_mb", 0) or 0)
                if memory > 0:
                    memory_samples.append(memory)
                stop.wait(0.5)

        threads = [
            threading.Thread(target=probe_status, daemon=True),
            threading.Thread(target=sample_memory, daemon=True),
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

        assert len(latencies) >= 5, f"only {len(latencies)} status probes succeeded"
        assert max(latencies) < 2
        assert memory_samples, "observer resource usage was never available"
        assert max(memory_samples) < _OBSERVER_MEMORY_CEILING_MB

        observer_view = observer.get_block(target.blockHash)
        assert observer_view.blockInfo.postStateHash == target.postStateHash
        response = observer.api_post(
            "/explore-deploy",
            {"term": "new x in { x!(1) }"},
            timeout=15,
        )
        assert response.status_code == 200

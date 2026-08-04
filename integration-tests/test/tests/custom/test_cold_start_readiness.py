import time

import pytest
import requests

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import poll_until, wait_for_node_running

pytestmark = pytest.mark.xdist_group("custom")


def test_cold_start_readiness_requires_lfb(provider, timeouts) -> None:
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
        pre_lfb_statuses = []
        deadline = time.time() + timeouts.node_startup
        while time.time() < deadline and not pre_lfb_statuses:
            for handle in handles:
                url = f"http://{handle.grpc_host}:{handle.ports.http}/api/status"
                try:
                    response = requests.get(url, timeout=0.5)
                except requests.RequestException:
                    continue
                if response.status_code != 200:
                    continue
                status = response.json()
                if status.get("lastFinalizedBlockNumber") == -1:
                    pre_lfb_statuses.append(status)
            time.sleep(0.01)

        assert pre_lfb_statuses, "did not observe the HTTP API before first LFB"
        assert all(status["isReady"] is False for status in pre_lfb_statuses)

        for handle in handles:
            wait_for_node_running(
                get_logs=handle.logs,
                is_running=handle.is_running,
                node_name=handle.name,
                timeout=timeouts.node_startup,
                status_url=(f"http://{handle.grpc_host}:{handle.ports.http}/api/status"),
            )

        final_statuses = []
        for handle in handles:
            url = f"http://{handle.grpc_host}:{handle.ports.http}/api/status"
            final_statuses.append(
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
            )

        assert all(status["lastFinalizedBlockNumber"] >= 0 for status in final_statuses)
    finally:
        provider.destroy_shard(handles)

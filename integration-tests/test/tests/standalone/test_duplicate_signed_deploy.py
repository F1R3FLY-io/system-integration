"""Concurrent submissions of one signed deploy admit and execute exactly one copy.

Resubmitting an identical ``DeployDataProto`` — same signature, so the same deploy
id — must be refused after the first acceptance rather than queued twice. Beyond
the admission count, this asserts the property no unit test can reach: after
finalization the deploy appears exactly once in the canonical block, so a
duplicate cannot be executed twice.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from f1r3fly.client import F1r3flyClientException
from f1r3fly.util import create_deploy_data

from ...infra.config import NodeConfig
from ...infra.keys import BOOTSTRAP_ID
from ...infra.node import Node
from ...infra.polling import wait_for_deploy_finalized
from ...infra.types import NodeRole


def test_duplicate_signed_deploy_race(provider, timeouts) -> None:
    """One of 32 concurrent identical submissions is admitted and executed once."""
    config = NodeConfig(
        role=NodeRole.STANDALONE,
        cli_flags=frozenset({"--heartbeat-enabled"}),
        cli_options={
            "--heartbeat-check-interval": "2seconds",
            "--heartbeat-max-lfb-age": "1second",
        },
    )
    handle = provider.create_standalone(config)
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    try:
        vabn = node.get_current_block_number()
        deploy = create_deploy_data(
            BOOTSTRAP_ID.private_key(),
            '@"duplicate-race-state"!(1)',
            1,
            100_000,
            vabn,
            int(time.time() * 1000),
            "root",
        )
        expected_id = deploy.sig.hex()

        accepted = []
        rejected = []
        with ThreadPoolExecutor(max_workers=32) as executor:
            futures = [executor.submit(node.send_deploy, deploy) for _ in range(32)]
            for future in as_completed(futures):
                try:
                    accepted.append(future.result())
                except F1r3flyClientException as exc:
                    rejected.append(str(exc))

        assert accepted == [expected_id], f"expected one acceptance, got {accepted}"
        assert len(rejected) == 31, f"expected 31 duplicate rejections, got {len(rejected)}"
        assert all(
            "duplicate" in message.lower() or "already known" in message.lower()
            for message in rejected
        ), rejected

        status = wait_for_deploy_finalized(node, expected_id, timeouts.finalization)
        block = node.get_block(status.latestBlockHash.hex())
        occurrences = sum(1 for processed in block.deploys if processed.sig == expected_id)
        assert occurrences == 1, f"deploy executed {occurrences} times in canonical block"
    finally:
        node.close()
        provider.destroy_standalone(handle)

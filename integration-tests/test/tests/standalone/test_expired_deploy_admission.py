import re
import time

import pytest
from f1r3fly.client import F1r3flyClientException
from f1r3fly.util import create_deploy_data

from ...infra.config import NodeConfig
from ...infra.keys import BOOTSTRAP_ID
from ...infra.node import Node
from ...infra.polling import wait_for_deploy_finalized
from ...infra.types import NodeRole

_DEPLOY_LIFESPAN = 50


def test_expired_deploy_rejected_at_admission(provider, timeouts) -> None:
    config = NodeConfig(
        role=NodeRole.STANDALONE,
        cli_flags=frozenset({"--heartbeat-enabled"}),
        cli_options={
            "--heartbeat-check-interval": "1second",
            "--heartbeat-max-lfb-age": "1second",
        },
    )
    handle = provider.create_standalone(config)
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    try:
        timestamp = int(time.time() * 1000)
        probe = create_deploy_data(
            BOOTSTRAP_ID.private_key(),
            '@"expired-admission-probe"!(1)',
            1,
            100_000,
            -10_000,
            timestamp,
            "root",
        )

        with pytest.raises(F1r3flyClientException, match="expired") as rejected:
            node.send_deploy(probe)
        boundary = re.search(
            r"at block (-?\d+) with deploy lifespan (\d+)",
            str(rejected.value),
        )
        assert boundary is not None
        height, lifespan = map(int, boundary.groups())
        assert lifespan == _DEPLOY_LIFESPAN

        expired = create_deploy_data(
            BOOTSTRAP_ID.private_key(),
            '@"expired-admission"!(1)',
            1,
            100_000,
            height - lifespan,
            timestamp + 1,
            "root",
        )
        inside = create_deploy_data(
            BOOTSTRAP_ID.private_key(),
            '@"inside-admission-window"!(1)',
            1,
            100_000,
            height - lifespan + 1,
            timestamp + 2,
            "root",
        )

        with pytest.raises(F1r3flyClientException, match="expired"):
            node.send_deploy(expired)

        accepted_id = node.send_deploy(inside)
        wait_for_deploy_finalized(node, accepted_id, timeouts.finalization)

        with pytest.raises(F1r3flyClientException):
            node.find_deploy(expired.sig.hex())
        assert node.is_running()
    finally:
        node.close()
        provider.destroy_standalone(handle)

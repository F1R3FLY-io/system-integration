"""Block-height deploy expiry is enforced at admission, not at propose time.

A deploy whose ``validAfterBlockNumber`` has fallen outside the shard's
``deploy_lifespan`` window must be refused by ``doDeploy`` rather than accepted
and silently dropped later. Verifies the reject boundary exactly: at the
boundary is refused, one block past it is admitted and finalizes, and the
refused deploy leaves no trace in the DAG.
"""

import itertools
import re
import time
from typing import Tuple

import pytest
from f1r3fly.client import F1r3flyClientException
from f1r3fly.util import create_deploy_data

from ...infra.config import NodeConfig
from ...infra.keys import BOOTSTRAP_ID
from ...infra.node import Node
from ...infra.polling import poll_until, wait_for_deploy_finalized
from ...infra.types import NodeRole

_BOUNDARY_ATTEMPTS = 5
_DEPLOY_LIFESPAN = 50
_LIFESPAN_ADVANCE_TIMEOUT_SECONDS = 240


@pytest.mark.requires_node_capabilities("expired-deploy-admission")
def test_expired_deploy_rejected_at_admission(provider, timeouts) -> None:
    """At-boundary deploys are refused; one block past the boundary finalizes."""
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
    clock = itertools.count(int(time.time() * 1000))

    def _deploy_at(term: str, valid_after_block_no: int):
        return create_deploy_data(
            BOOTSTRAP_ID.private_key(),
            term,
            valid_after_block_no=valid_after_block_no,
            timestamp_millis=next(clock),
            shard_id="root",
        )

    def _current_boundary() -> Tuple[int, int]:
        """Return the next candidate height and deploy lifespan reported by the node.

        Read from a deliberately-expired deploy's rejection, because that message
        is the only surface exposing the DAG's ``latest_block_number``. This value
        is the next candidate height and can run ahead of the LFB returned by
        ``get_current_block_number``.
        """
        probe = _deploy_at('@"expired-admission-probe"!(1)', 0)
        with pytest.raises(F1r3flyClientException, match="expired") as rejected:
            node.send_deploy(probe)
        boundary = re.search(
            r"at block (-?\d+) with deploy lifespan (\d+)",
            str(rejected.value),
        )
        assert boundary is not None, (
            f"expiry rejection did not report the boundary in the expected form: {rejected.value}"
        )
        height, lifespan = map(int, boundary.groups())
        return height, lifespan

    try:
        poll_until(
            predicate=lambda: node.get_current_block_number() >= _DEPLOY_LIFESPAN,
            timeout=timeouts.custom(_LIFESPAN_ADVANCE_TIMEOUT_SECONDS),
            interval=1,
            description="standalone LFB reaches the protocol-v6 expiry boundary",
        )
        height, lifespan = _current_boundary()
        assert lifespan == _DEPLOY_LIFESPAN

        # At the boundary: refused. Safe against block advance — a growing
        # next candidate height only pushes this deploy further outside the window.
        expired = _deploy_at('@"expired-admission"!(1)', height - lifespan)
        with pytest.raises(F1r3flyClientException, match="expired"):
            node.send_deploy(expired)

        # One block past the boundary: admitted. Valid only while
        # the next candidate height has not advanced, so re-derive it per attempt.
        accepted_id = None
        for _ in range(_BOUNDARY_ATTEMPTS):
            height, lifespan = _current_boundary()
            inside = _deploy_at('@"inside-admission-window"!(1)', height - lifespan + 1)
            try:
                accepted_id = node.send_deploy(inside)
                break
            except F1r3flyClientException as exc:
                if "expired" not in str(exc):
                    raise
        assert accepted_id is not None, (
            f"deploy one block past the expiry boundary was refused on every one of "
            f"{_BOUNDARY_ATTEMPTS} attempts; the node proposes every second, so either "
            f"the boundary is off by one or block production is outrunning submission"
        )
        wait_for_deploy_finalized(node, accepted_id, timeouts.finalization)

        # The refused deploy must be absent from the DAG, not merely unfinalized.
        # match= keeps an unreachable node from passing this as an absence; the
        # phrasing is DeployNotFoundError's, "Couldn't find block containing
        # deploy with id: ..." (casper/src/rust/api/block_api.rs).
        with pytest.raises(F1r3flyClientException, match="(?i)find block containing deploy"):
            node.find_deploy(expired.deployId.hex())
        assert node.is_running()
    finally:
        node.close()
        provider.destroy_standalone(handle)

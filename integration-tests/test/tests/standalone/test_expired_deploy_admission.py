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
from ...infra.polling import wait_for_deploy_finalized
from ...infra.types import NodeRole

# Attempts allowed for the accepted-case submission. The boundary is only one
# block wide, so a heartbeat block landing between probing the boundary and
# submitting against it flips the accepted case to expired; re-probe and retry
# rather than widening the margin, which would stop testing the boundary.
_BOUNDARY_ATTEMPTS = 5

_PHLO_PRICE = 1
_PHLO_LIMIT = 100_000


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
    # Distinct timestamps keep every deploy's signature — and so its deploy id —
    # unique, including across retries of the same boundary offset.
    clock = itertools.count(int(time.time() * 1000))

    def _deploy_at(term: str, valid_after_block_no: int):
        return create_deploy_data(
            BOOTSTRAP_ID.private_key(),
            term,
            _PHLO_PRICE,
            _PHLO_LIMIT,
            valid_after_block_no,
            next(clock),
            "root",
        )

    def _current_boundary() -> Tuple[int, int]:
        """Return ``(latest_block_number, deploy_lifespan)`` as the node reports them.

        Read from a deliberately-expired deploy's rejection, because that message
        is the only surface exposing the DAG's ``latest_block_number`` — which is
        the height the node compares against, and which runs ahead of the LFB that
        ``get_current_block_number`` returns.
        """
        probe = _deploy_at('@"expired-admission-probe"!(1)', -10_000)
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
        height, lifespan = _current_boundary()

        # At the boundary: refused. Safe against block advance — a growing
        # latest_block_number only pushes this deploy further outside the window.
        expired = _deploy_at('@"expired-admission"!(1)', height - lifespan)
        with pytest.raises(F1r3flyClientException, match="expired"):
            node.send_deploy(expired)

        # One block past the boundary: admitted. Valid only while
        # latest_block_number has not advanced, so re-derive it per attempt.
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
            node.find_deploy(expired.sig.hex())
        assert node.is_running()
    finally:
        node.close()
        provider.destroy_standalone(handle)

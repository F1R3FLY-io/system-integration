"""
Synchrony Constraint Integration Test

Verifies that the per-validator synchrony constraint threshold is enforced
correctly. Each validator is configured with a different threshold, and the
test orchestrates block creation to confirm that:

1. Every validator can propose its first block after genesis (exempt from
   the constraint).
2. A validator whose threshold is not met is rejected when trying to propose
   a second time.
3. After sufficient blocks from other validators arrive (meeting the
   threshold), the validator can propose again.

Uses a custom shard with 3 validators, heartbeat disabled, and per-node
synchrony-constraint-threshold overrides.

Bond configuration:
    validator1  100  threshold=0.67
    validator2  102  threshold=0.33
    validator3   98  threshold=0.99

Bootstrap is not bonded (ceremony master only) and has zero weight in the
synchrony calculation.

Synchrony math (all weights among active/bonded validators only):
    V1 (100): other-stake = 102+98 = 200. Need >= 0.67*200 = 134. V2 alone (102) is not enough; need V2+V3.
    V2 (102): other-stake = 100+98 = 198. Need >= 0.33*198 = 65.3. V1 alone (100) suffices.
    V3 (98):  other-stake = 100+102 = 202. Need >= 0.99*202 = 199.98. Need both V1 and V2.
"""

import logging
import time

import pytest
from docker.client import DockerClient
from f1r3fly.client import F1r3flyClientException

from .common import (
    CommandLineOptions,
    SynchronyConstraintError,
)
from .conftest import (
    VALIDATOR1_ID,
    VALIDATOR1_KEY,
    VALIDATOR2_ID,
    VALIDATOR2_KEY,
    VALIDATOR3_ID,
    VALIDATOR3_KEY,
    start_custom_shard,
)
from .rnode import Node

pytestmark = pytest.mark.xdist_group("custom")


def _poll_block_visible(node: Node, block_hash: str, timeout: int = 120, scale: float = 1.0) -> None:
    """Poll until a block is visible on the given node."""
    scaled_timeout = int(timeout * scale)
    deadline = time.time() + scaled_timeout
    while time.time() < deadline:
        try:
            node.get_block(block_hash)
            return
        except Exception:
            time.sleep(3)
    raise AssertionError(
        f"Block {block_hash[:16]}... not visible on {node.name} within {scaled_timeout}s"
    )


def test_synchrony_constraint(
    docker_client: DockerClient,
    command_line_options: CommandLineOptions,
) -> None:
    """Verify per-validator synchrony constraint enforcement.

    With heartbeat disabled, blocks are only created via explicit propose
    calls, giving the test full control over block ordering.
    """
    bonds = [
        (VALIDATOR1_ID, 100),
        (VALIDATOR2_ID, 102),
        (VALIDATOR3_ID, 98),
    ]

    with start_custom_shard(
        docker_client, command_line_options,
        bonds=bonds,
        ftt=-1,
        heartbeat=False,
        global_cli_options={
            "--synchrony-constraint-threshold": "0",
        },
        per_node_cli_options={
            "validator1": {"--synchrony-constraint-threshold": "0.67"},
            "validator2": {"--synchrony-constraint-threshold": "0.33"},
            "validator3": {"--synchrony-constraint-threshold": "0.99"},
        },
    ) as shard:
        v1 = shard.nodes["validator1"]
        v2 = shard.nodes["validator2"]
        v3 = shard.nodes["validator3"]
        scale = command_line_options.timeout_scale

        # ── Phase 1: First block after genesis (exempt from constraint) ──
        # Every validator can propose once regardless of threshold because
        # their latest block is genesis (blockNum == 0).
        logging.info("Phase 1: First proposals (exempt from synchrony constraint)")

        v1.deploy_string("@1!(1)", VALIDATOR1_KEY, phlo_limit=100_000_000, phlo_price=1)
        b1 = v1.propose()
        logging.info("V1 first block: %s", b1[:16])

        _poll_block_visible(v2, b1, scale=scale)
        v2.deploy_string("@2!(2)", VALIDATOR2_KEY, phlo_limit=100_000_000, phlo_price=1)
        b2 = v2.propose()
        logging.info("V2 first block: %s", b2[:16])

        _poll_block_visible(v3, b2, scale=scale)
        v3.deploy_string("@3!(3)", VALIDATOR3_KEY, phlo_limit=100_000_000, phlo_price=1)
        b3 = v3.propose()
        logging.info("V3 first block: %s", b3[:16])

        # Ensure all nodes have seen all first-round blocks
        _poll_block_visible(v1, b3, scale=scale)
        _poll_block_visible(v2, b3, scale=scale)

        # ── Phase 2: V2 can propose (V1 alone meets 0.33 threshold) ──
        # V2 needs >= 65.3 of other stake. V1 has 100 > 65.3.
        logging.info("Phase 2: V2 proposes (V1 stake=100 meets 0.33 threshold)")
        v2.deploy_string("@20!(20)", VALIDATOR2_KEY, phlo_limit=100_000_000, phlo_price=1)
        b4 = v2.propose()
        logging.info("V2 second block: %s", b4[:16])

        # ── Phase 3: V1 cannot propose yet (needs V2+V3 or just V2+V3) ──
        # V1 needs >= 134 of other stake. After phase 1 round, V1 has seen
        # V2 (102) and V3 (98) but its constraint requires blocks SINCE its
        # last proposal. V1's last proposal was b1. Since b1, V1 has seen
        # b2 (V2, 102) and b3 (V3, 98). Total = 200 >= 134. So V1 should
        # be able to propose.
        # But after V2 proposed b4 and V3 has not proposed since b3, let's
        # verify V1 can propose after seeing V2's second block.
        _poll_block_visible(v1, b4, scale=scale)
        logging.info("Phase 3: V1 proposes (V2=102 + V3=98 = 200 >= 134)")
        v1.deploy_string("@10!(10)", VALIDATOR1_KEY, phlo_limit=100_000_000, phlo_price=1)
        b5 = v1.propose()
        logging.info("V1 second block: %s", b5[:16])

        # ── Phase 4: V3 cannot propose (needs V1+V2, threshold=0.99) ──
        # V3 needs >= 199.98 of other stake. Since V3's last proposal (b3),
        # V3 has seen V2's b4 (102) and V1's b5 (100) = 202 >= 199.98.
        # So V3 should now be able to propose.
        _poll_block_visible(v3, b5, scale=scale)
        _poll_block_visible(v3, b4, scale=scale)
        logging.info("Phase 4: V3 proposes (V1=100 + V2=102 = 202 >= 199.98)")
        v3.deploy_string("@30!(30)", VALIDATOR3_KEY, phlo_limit=100_000_000, phlo_price=1)
        b6 = v3.propose()
        logging.info("V3 second block: %s", b6[:16])

        # ── Phase 5: V1 tries to propose again without enough support ──
        # V1's last proposal was b5. Since b5, only V3 has proposed (b6,
        # stake 98). V1 needs >= 134. 98 < 134. V2 hasn't proposed since
        # b4 which was before b5. So V1 should be rejected.
        _poll_block_visible(v1, b6, scale=scale)
        logging.info("Phase 5: V1 should be rejected (only V3=98 < 134)")
        v1.deploy_string("@11!(11)", VALIDATOR1_KEY, phlo_limit=100_000_000, phlo_price=1)
        with pytest.raises(SynchronyConstraintError):
            v1.propose()

        # ── Phase 6: V2 proposes, unlocking V1 ──
        _poll_block_visible(v2, b6, scale=scale)
        v2.deploy_string("@21!(21)", VALIDATOR2_KEY, phlo_limit=100_000_000, phlo_price=1)
        b7 = v2.propose()
        logging.info("V2 third block: %s", b7[:16])

        # ── Phase 7: V1 can now propose (V3=98 + V2=102 = 200 >= 134) ──
        _poll_block_visible(v1, b7, scale=scale)
        logging.info("Phase 7: V1 proposes (V3=98 + V2=102 = 200 >= 134)")
        # The deploy from Phase 5 is still pending in V1's deploy pool
        b8 = v1.propose()
        logging.info("V1 third block: %s", b8[:16])

        # ── Phase 8: V3 cannot propose without both V1 and V2 ──
        # V3's last proposal was b6. Since b6, V2 proposed b7 (102) and
        # V1 proposed b8 (100). Total = 202 >= 199.98. V3 can propose.
        _poll_block_visible(v3, b8, scale=scale)
        _poll_block_visible(v3, b7, scale=scale)
        logging.info("Phase 8: V3 proposes (V1=100 + V2=102 = 202 >= 199.98)")
        v3.deploy_string("@31!(31)", VALIDATOR3_KEY, phlo_limit=100_000_000, phlo_price=1)
        b9 = v3.propose()
        logging.info("V3 third block: %s", b9[:16])

        logging.info("Synchrony constraint test passed -- all phases verified")

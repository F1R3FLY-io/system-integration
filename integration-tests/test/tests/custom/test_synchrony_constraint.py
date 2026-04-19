"""
Synchrony Constraint Integration Test

Verifies that the per-validator synchrony constraint threshold is enforced
correctly. Each validator is configured with a different threshold, and the
test orchestrates block creation to confirm that:

1. Every validator can propose its first block after genesis (exempt).
2. A validator whose threshold is not met is rejected.
3. After sufficient blocks from other validators arrive, proposal succeeds.

Bond configuration:
    validator1  100  threshold=0.67
    validator2  102  threshold=0.33
    validator3   98  threshold=0.99

Synchrony math (weights among bonded validators only):
    V1 (100): needs >= 0.67*(102+98) = 134. V2+V3 required.
    V2 (102): needs >= 0.33*(100+98)  = 65.3. V1 alone suffices.
    V3 (98):  needs >= 0.99*(100+102) = 199.98. Both V1 and V2 required.
"""
import logging

import pytest
from f1r3fly.client import F1r3flyClientException

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import poll_until
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")


def _wait_block_visible(node, block_hash, timeout):
    """Poll until block is visible on node."""
    poll_until(
        predicate=lambda: _try_get_block(node, block_hash),
        timeout=timeout,
        interval=3.0,
        description=f"block {block_hash[:16]} visible on {node.name}",
    )


def _try_get_block(node, block_hash):
    try:
        node.get_block(block_hash)
        return True
    except Exception:
        return False


def test_synchrony_constraint(provider, timeouts) -> None:
    """Verify per-validator synchrony constraint enforcement."""
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 102),
            (VALIDATOR3_ID, 98),
        ],
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
    )

    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")
        v2 = shard.node("validator2")
        v3 = shard.node("validator3")
        t = timeouts.custom(120)

        v1_key = VALIDATOR1_ID.private_key()
        v2_key = VALIDATOR2_ID.private_key()
        v3_key = VALIDATOR3_ID.private_key()

        # Phase 1: First block after genesis (exempt)
        logging.info("Phase 1: First proposals (exempt from synchrony constraint)")
        v1.deploy_string("@1!(1)", v1_key, phlo_limit=100_000_000)
        b1 = v1.propose()
        logging.info("V1 first block: %s", b1[:16])

        _wait_block_visible(v2, b1, t)
        v2.deploy_string("@2!(2)", v2_key, phlo_limit=100_000_000)
        b2 = v2.propose()
        logging.info("V2 first block: %s", b2[:16])

        _wait_block_visible(v3, b2, t)
        v3.deploy_string("@3!(3)", v3_key, phlo_limit=100_000_000)
        b3 = v3.propose()
        logging.info("V3 first block: %s", b3[:16])

        _wait_block_visible(v1, b3, t)
        _wait_block_visible(v2, b3, t)

        # Phase 2: V2 can propose (V1 alone meets 0.33 threshold)
        logging.info("Phase 2: V2 proposes (V1 stake=100 meets 0.33)")
        v2.deploy_string("@20!(20)", v2_key, phlo_limit=100_000_000)
        b4 = v2.propose()
        logging.info("V2 second block: %s", b4[:16])

        # Phase 3: V1 can propose (V2+V3 = 200 >= 134)
        _wait_block_visible(v1, b4, t)
        logging.info("Phase 3: V1 proposes (V2=102 + V3=98 = 200 >= 134)")
        v1.deploy_string("@10!(10)", v1_key, phlo_limit=100_000_000)
        b5 = v1.propose()
        logging.info("V1 second block: %s", b5[:16])

        # Phase 4: V3 can propose (V1+V2 = 202 >= 199.98)
        _wait_block_visible(v3, b5, t)
        _wait_block_visible(v3, b4, t)
        logging.info("Phase 4: V3 proposes (V1=100 + V2=102 = 202 >= 199.98)")
        v3.deploy_string("@30!(30)", v3_key, phlo_limit=100_000_000)
        b6 = v3.propose()
        logging.info("V3 second block: %s", b6[:16])

        # Phase 5: V1 should be rejected (only V3=98 since b5, < 134)
        _wait_block_visible(v1, b6, t)
        logging.info("Phase 5: V1 rejected (only V3=98 < 134)")
        v1.deploy_string("@11!(11)", v1_key, phlo_limit=100_000_000)
        with pytest.raises(F1r3flyClientException, match="(?i)synchrony|not enough"):
            v1.propose()

        # Phase 6: V2 proposes, unlocking V1
        _wait_block_visible(v2, b6, t)
        v2.deploy_string("@21!(21)", v2_key, phlo_limit=100_000_000)
        b7 = v2.propose()
        logging.info("V2 third block: %s", b7[:16])

        # Phase 7: V1 can now propose (V3=98 + V2=102 = 200 >= 134)
        _wait_block_visible(v1, b7, t)
        logging.info("Phase 7: V1 proposes (V3=98 + V2=102 = 200 >= 134)")
        b8 = v1.propose()
        logging.info("V1 third block: %s", b8[:16])

        # Phase 8: V3 can propose (V1+V2 since b6 = 202 >= 199.98)
        _wait_block_visible(v3, b8, t)
        _wait_block_visible(v3, b7, t)
        logging.info("Phase 8: V3 proposes (V1=100 + V2=102 = 202 >= 199.98)")
        v3.deploy_string("@31!(31)", v3_key, phlo_limit=100_000_000)
        b9 = v3.propose()
        logging.info("V3 third block: %s", b9[:16])

        logging.info("Synchrony constraint test passed -- all phases verified")
    finally:
        shard.destroy()

"""
Synchrony Constraint Integration Test

Verifies that the per-validator synchrony constraint threshold is enforced
correctly. Each validator is configured with a different threshold, and the
test orchestrates block creation to confirm that:

1. Every validator can propose its first block after genesis (exempt).
2. Validators can only propose when their synchrony threshold is met.

Bond configuration:
    validator1  100  threshold=0.67
    validator2  102  threshold=0.33
    validator3   98  threshold=0.99

Synchrony math (weights among bonded validators only):
    V1 (100): needs >= 0.67*(102+98) = 134. V2+V3 required.
    V2 (102): needs >= 0.33*(100+98)  = 65.3. V1 alone suffices.
    V3 (98):  needs >= 0.99*(100+102) = 199.98. Both V1 and V2 required.

Note: The rejection case (proposal denied when threshold not met) cannot
be reliably tested with FTT=-1 because the finalized-baseline fallback
rescues the proposer. The `--synchrony-finalized-baseline-enabled` config
key is not exposed as a CLI flag. See docs/TODO.md.
"""
import logging

import pytest

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import wait_for_block_visible
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

_BONDS = [
    (VALIDATOR1_ID, 100),
    (VALIDATOR2_ID, 102),
    (VALIDATOR3_ID, 98),
]

_PER_NODE_SYNC = {
    "validator1": {"--synchrony-constraint-threshold": "0.67"},
    "validator2": {"--synchrony-constraint-threshold": "0.33"},
    "validator3": {"--synchrony-constraint-threshold": "0.99"},
}


def test_synchrony_constraint(provider, timeouts) -> None:
    """Verify proposals succeed when synchrony threshold is met.

    Phases:
    1. First proposals (exempt from synchrony constraint)
    2. V2 proposes (V1 stake=100 meets 0.33)
    3. V1 proposes (V2+V3 = 200 >= 134)
    4. V3 proposes (V1+V2 = 202 >= 199.98)
    5. V2 proposes again
    6. V1 proposes (V3+V2 = 200 >= 134)
    7. V3 proposes (V1+V2 = 202 >= 199.98)
    """
    config = ShardConfig(
        bonds=_BONDS,
        ftt=-1,
        heartbeat=False,
        global_cli_options={"--synchrony-constraint-threshold": "0"},
        per_node_cli_options=_PER_NODE_SYNC,
    )

    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")
        v2 = shard.node("validator2")
        v3 = shard.node("validator3")
        t = timeouts.command

        v1_key = VALIDATOR1_ID.private_key()
        v2_key = VALIDATOR2_ID.private_key()
        v3_key = VALIDATOR3_ID.private_key()

        # Phase 1: First block after genesis (exempt)
        logging.info("Phase 1: First proposals (exempt from synchrony constraint)")
        v1.deploy_string("@1!(1)", v1_key, phlo_limit=100_000_000)
        b1 = v1.propose()
        logging.info("V1 first block: %s", b1[:16])

        wait_for_block_visible(v2, b1, t)
        v2.deploy_string("@2!(2)", v2_key, phlo_limit=100_000_000)
        b2 = v2.propose()
        logging.info("V2 first block: %s", b2[:16])

        wait_for_block_visible(v3, b2, t)
        v3.deploy_string("@3!(3)", v3_key, phlo_limit=100_000_000)
        b3 = v3.propose()
        logging.info("V3 first block: %s", b3[:16])

        wait_for_block_visible(v1, b3, t)
        wait_for_block_visible(v2, b3, t)

        # Phase 2: V2 can propose (V1 stake=100 meets 0.33)
        logging.info("Phase 2: V2 proposes (V1 stake=100 meets 0.33)")
        v2.deploy_string("@20!(20)", v2_key, phlo_limit=100_000_000)
        b4 = v2.propose()
        logging.info("V2 second block: %s", b4[:16])

        # Phase 3: V1 can propose (V2+V3 = 200 >= 134)
        wait_for_block_visible(v1, b4, t)
        logging.info("Phase 3: V1 proposes (V2=102 + V3=98 = 200 >= 134)")
        v1.deploy_string("@10!(10)", v1_key, phlo_limit=100_000_000)
        b5 = v1.propose()
        logging.info("V1 second block: %s", b5[:16])

        # Phase 4: V3 can propose (V1+V2 = 202 >= 199.98)
        wait_for_block_visible(v3, b5, t)
        wait_for_block_visible(v3, b4, t)
        logging.info("Phase 4: V3 proposes (V1=100 + V2=102 = 202 >= 199.98)")
        v3.deploy_string("@30!(30)", v3_key, phlo_limit=100_000_000)
        b6 = v3.propose()
        logging.info("V3 second block: %s", b6[:16])

        # Phase 5: V2 proposes again
        wait_for_block_visible(v2, b6, t)
        v2.deploy_string("@21!(21)", v2_key, phlo_limit=100_000_000)
        b7 = v2.propose()
        logging.info("V2 third block: %s", b7[:16])

        # Phase 6: V1 can propose (V3+V2 = 200 >= 134)
        wait_for_block_visible(v1, b7, t)
        logging.info("Phase 6: V1 proposes (V3=98 + V2=102 = 200 >= 134)")
        v1.deploy_string("@11!(11)", v1_key, phlo_limit=100_000_000)
        b8 = v1.propose()
        logging.info("V1 third block: %s", b8[:16])

        # Phase 7: V3 can propose (V1+V2 = 202 >= 199.98)
        wait_for_block_visible(v3, b8, t)
        wait_for_block_visible(v3, b7, t)
        logging.info("Phase 7: V3 proposes (V1=100 + V2=102 = 202 >= 199.98)")
        v3.deploy_string("@31!(31)", v3_key, phlo_limit=100_000_000)
        b9 = v3.propose()
        logging.info("V3 third block: %s", b9[:16])

        logging.info("Synchrony constraint test passed -- all phases verified")
    finally:
        shard.destroy()

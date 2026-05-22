"""
Heartbeat Shard Integration Tests

Tests for heartbeat behavior in a multi-validator shard:
  - Heartbeat startup logged on all validators
  - Block advancement via heartbeat on all validators
  - Manual propose during active heartbeat doesn't crash
"""

import logging

import pytest
from f1r3fly.client import F1r3flyClientException

from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import poll_until

pytestmark = pytest.mark.xdist_group("shared")

VALIDATOR_KEYS = [VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID]


def test_heartbeat_creates_blocks_when_idle_shard(shared_shard, timeouts) -> None:
    """Heartbeat creates blocks on all shard validators under multi-validator
    coordination and multi-parent DAG.
    """
    validators = shared_shard.validators

    # All validators should log heartbeat startup
    for node in validators:
        logs = node.logs()
        assert (
            "Heartbeat: Starting with random initial delay" in logs
        ), f"{node.name} should log heartbeat startup message"

    initial_block_numbers = [max(b.blockNumber for b in v.get_blocks(5)) for v in validators]

    def _all_advanced():
        current = [max(b.blockNumber for b in v.get_blocks(5)) for v in validators]
        if all(current[i] >= initial_block_numbers[i] + 2 for i in range(len(validators))):
            return current
        return None

    final_block_numbers = poll_until(
        predicate=_all_advanced,
        timeout=timeouts.finalization * 2,
        interval=5.0,
        description="all validators advance by 2+ blocks from heartbeat",
    )

    logging.info(
        "Heartbeat shard: all validators advanced: %s",
        ", ".join(
            f"{validators[i].name}: {initial_block_numbers[i]}->{final_block_numbers[i]}"
            for i in range(len(validators))
        ),
    )


def test_manual_propose_during_heartbeat_shard(shared_shard, timeouts) -> None:
    """Manual propose during active heartbeat does not crash the node.

    With heartbeat active, a manual propose can have three outcomes:
      - Success (propose won the lock race, included the deploy in a block)
      - "NoNewDeploys" (deploy already included by auto-proposer)
      - "another propose is in progress" (heartbeat holds the propose lock)

    All three are valid. We deploy and propose on each validator via the
    internal gRPC port (ProposeService), then verify all nodes advance
    LFB by 3+ blocks to confirm no crash or stall.
    """
    validators = shared_shard.validators
    all_nodes = shared_shard.all_nodes

    # Record baseline LFB on all nodes
    baseline_lfbs = {}
    for node in all_nodes:
        baseline_lfbs[node.name] = node.last_finalized_block().blockInfo.blockNumber
    logging.info("Baseline LFBs: %s", baseline_lfbs)

    # Deploy and propose on each validator
    for node, key_id in zip(validators, VALIDATOR_KEYS):
        node.deploy_string(
            f'@"heartbeat-propose-test"!("{node.name}")',
            key_id.private_key(),
        )

        try:
            node.propose()
            logging.info("%s: propose succeeded (won lock race)", node.name)
        except F1r3flyClientException as e:
            message = str(e)
            is_no_new_deploys = "NoNewDeploys" in message
            is_contention = "another propose is in progress" in message
            if is_no_new_deploys or is_contention:
                logging.info(
                    "%s: propose returned expected response: %s",
                    node.name,
                    "NoNewDeploys" if is_no_new_deploys else "contention",
                )
            else:
                logging.warning("%s: unexpected propose error: %s", node.name, message)

    # Verify all nodes advance LFB by 3+ blocks — proves no crash or stall
    target_lfbs = {name: lfb + 3 for name, lfb in baseline_lfbs.items()}
    remaining = set(target_lfbs.keys())

    def _all_advanced():
        for node in all_nodes:
            if node.name not in remaining:
                continue
            lfb = node.last_finalized_block().blockInfo.blockNumber
            if lfb >= target_lfbs[node.name]:
                remaining.discard(node.name)
        return True if not remaining else None

    poll_until(
        predicate=_all_advanced,
        timeout=timeouts.finalization * 3,
        interval=5.0,
        description="all nodes LFB advance by 3+ after propose attempt",
    )

    logging.info("Manual propose during heartbeat: all nodes healthy, LFB advancing")

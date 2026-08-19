"""
Network Convergence Tests

Tests that the network recovers after DAG tip divergence caused by:
1. Validator pause -- pausing a container forces other validators to
   produce independent blocks, creating DAG forks that must be merged
   after unpause.
2. FT convergence -- cached fault tolerance for finalized blocks
   reaches FTT on every node and only grows as later finalization
   rounds update cached values (exact 1.0 is not guaranteed).
3. Slow deploy -- a phlo-exhausting deploy (#224) blocks one validator
   while others produce heartbeat blocks, causing divergence (#437).
   Runs last so its trailing phlo-exhaustion replay lines cannot land
   in another test's log-scan window.

The module uses a dedicated shard because pause and phlo-exhaustion
change network state. Fixture teardown prevents these effects from
reaching downstream shared-shard tests.
"""

import logging
import time

import pytest

from ...infra.assertions import assert_deploy_errored
from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import (
    lfb_number,
    poll_until,
    wait_for_deploy_included,
    wait_for_lfb_converged,
)
from ...infra.shard import Shard

pytestmark = [
    pytest.mark.xdist_group("shared"),
    pytest.mark.isolated_shard,
]

VALIDATOR_KEYS = [VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID]

# Phlo-exhausting loop contract. Runs until phlo runs out, blocking the
# proposing validator long enough for other validators to create independent
# blocks via heartbeat, causing DAG tip divergence.
SLOW_LOOP_CONTRACT = """
new stdout(`rho:io:stdout`) in {
  new loop in {
    contract loop(@n) = {
      if (n <= 0) {
        stdout!("done")
      } else {
        loop!(n - 1)
      }
    } |
    loop!(100000)
  }
}
"""


@pytest.fixture(scope="module")
def convergence_shard(provider, timeouts):
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        heartbeat=True,
        include_readonly=True,
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        yield shard
    finally:
        shard.destroy()


def _wait_for_lfb_ft_at_or_above_ftt(nodes, ftt, timeout):
    """Wait until every node's LFB reports FT >= FTT, and report the values.

    FT on a freshly adopted LFB is a convergent witness cache, not an
    at-adoption guarantee: finality can arrive by floor inheritance while the
    local live view is still re-converging, and later finalization rounds only
    raise the cached value. Both recovery tests must poll rather than assert
    instantly — the same contract ``test_ft_convergence`` relies on — so the
    wait lives here once instead of being written out at each call site.
    """

    def _all_lfbs_clear_ftt():
        ft_values = {}
        for node in nodes:
            lfb = node.last_finalized_block()
            ft_values[node.name] = (
                lfb.blockInfo.blockNumber,
                float(lfb.blockInfo.faultTolerance),
            )
        if all(ft >= ftt for _, ft in ft_values.values()):
            return ft_values
        logging.info(
            "FT still below FTT=%.2f on some nodes: %s",
            ftt,
            {k: f"#{n} FT={ft:.2f}" for k, (n, ft) in ft_values.items()},
        )
        return None

    final_fts = poll_until(
        predicate=_all_lfbs_clear_ftt,
        timeout=timeout,
        interval=5.0,
        description=f"post-recovery LFB FT >= FTT={ftt:.2f} on all nodes",
    )
    for name, (number, ft) in final_fts.items():
        logging.info("%s: LFB #%d, FT=%.2f", name, number, ft)
    return final_fts


def _poll_lfb_all_nodes(nodes, target, timeout):
    """Poll until LFB reaches target on all nodes.

    Lower bound only. Each node is dropped from the polling set the first
    time it crosses ``target`` and is never re-read — sound here because LFB
    is monotonic, so "has crossed" stays true. **Do not follow a call to this
    with a spread assertion**: it confirms each node crossed at some point,
    not that they were ever close together at the same instant. Use
    ``wait_for_lfb_converged`` when a spread matters.
    """
    remaining = set(n.name for n in nodes)

    def _check():
        for node in nodes:
            if node.name not in remaining:
                continue
            try:
                if lfb_number(node) >= target:
                    remaining.discard(node.name)
            except Exception:
                pass
        return True if not remaining else None

    poll_until(
        predicate=_check,
        timeout=timeout,
        interval=5.0,
        description=f"LFB >= #{target} on all {len(nodes)} nodes",
    )


@pytest.mark.allow_forbidden_patterns("DAGStorageMissingHash")
def test_network_recovers_from_validator_pause(convergence_shard, node_conf, timeouts) -> None:
    """Pause validator1 for 30s to force DAG tip divergence, then verify
    the network converges and LFB advances on all nodes.

    Before pausing, deploys are sent to all validators to ensure active
    state. While validator1 is paused, other validators create blocks via
    heartbeat. After unpause, the validators exchange tips and must
    propose multi-parent convergence blocks to merge the diverged forks.
    """
    validators = convergence_shard.validators
    all_nodes = convergence_shard.all_nodes

    # Deploy on each validator to create active state before pause
    for node, key_id in zip(validators, VALIDATOR_KEYS):
        node.deploy_string(
            f'@"pre-pause-{node.name}"!(1)',
            key_id.private_key(),
        )
    logging.info("Pre-pause deploys submitted to all validators")

    baseline_lfb = lfb_number(validators[0])
    logging.info("Baseline LFB: block #%d", baseline_lfb)

    logging.info("Pausing validator1 for 30s to force DAG divergence...")
    validators[0].pause()
    time.sleep(30)
    validators[0].unpause()
    logging.info("Validator1 unpaused. Waiting for network convergence...")

    # Deploy on each validator after unpause to stimulate convergence
    for node, key_id in zip(validators, VALIDATOR_KEYS):
        node.deploy_string(
            f'@"post-pause-{node.name}"!(1)',
            key_id.private_key(),
        )
    logging.info("Post-pause deploys submitted to all validators")

    # All nodes (including readonly) must advance LFB
    _poll_lfb_all_nodes(
        all_nodes,
        baseline_lfb + 3,
        timeout=timeouts.finalization * 3,
    )

    _wait_for_lfb_ft_at_or_above_ftt(all_nodes, node_conf.ftt, timeouts.finalization)

    logging.info("Network converged after validator pause (FT >= FTT=%.2f)", node_conf.ftt)


def test_ft_convergence(convergence_shard, node_conf, timeouts) -> None:
    """Verify cached FT reaches FTT on every node and never decreases.

    FT is cached at finalization time and may only grow as later
    finalization rounds update ancestor blocks. Exact FT=1.0 is NOT a
    valid requirement: with FTT=0.10 a node can validly hold a cached
    FT of 0.3333 forever — later finalization does not guarantee every
    node's cache is updated to full-stake agreement. Reaching exactly
    1.0 needs a controlled consensus configuration this shard does not
    pin.

    Runs before ``test_network_converges_after_slow_deploy``: replay of
    that test's errored deploy can log ``ComputationOutOfPhlogistons``
    after the test ends, and running first keeps those lines out of this
    test's per-test log-scan window without an allowance marker.

    Test flow:
    1. Wait for LFB to advance past genesis
    2. Pick a finalized block from V1's LFB ancestor chain
    3. Assert FT >= FTT on V1 (cache works)
    4. Poll all nodes until every one reports FT >= FTT for the block
    5. Re-sample: FT must stay >= FTT and never decrease (monotonicity)
    """
    all_nodes = convergence_shard.all_nodes
    ftt = node_conf.ftt

    # Wait for LFB to advance past genesis so we have finalized blocks
    lfb = poll_until(
        predicate=lambda: _lfb_past_genesis(convergence_shard.validators[0]),
        timeout=timeouts.finalization,
        interval=3.0,
        description="LFB advances past genesis",
    )
    lfb_hash = lfb.blockInfo.blockHash
    lfb_number = lfb.blockInfo.blockNumber
    logging.info("LFB at block #%d on %s", lfb_number, convergence_shard.validators[0].name)

    # Walk to the first non-genesis ancestor — this block was indirectly finalized
    # and will have a conservative FT that should converge upward
    target_block = convergence_shard.validators[0].get_block(lfb_hash)
    parents = list(target_block.blockInfo.parentsHashList)
    target_hash = parents[0] if parents else lfb_hash
    target_number = convergence_shard.validators[0].get_block(target_hash).blockInfo.blockNumber

    logging.info("Tracking FT convergence for block #%d (%s...)", target_number, target_hash[:16])

    # Verify FT >= FTT and isFinalized on reference node
    ref_block = convergence_shard.validators[0].get_block(target_hash)
    ft_ref = float(ref_block.blockInfo.faultTolerance)
    assert ft_ref >= ftt, (
        f"Block #{target_number} has FT={ft_ref} on reference node, expected >= FTT={ftt}"
    )
    assert ref_block.blockInfo.isFinalized is True, (
        f"Block #{target_number} should have isFinalized=True on reference node"
    )
    logging.info("Reference node FT=%.4f (>= FTT=%.2f)", ft_ref, ftt)

    # Poll until every node reports FT >= FTT for the target block —
    # cross-node agreement that the block is finalized. Exact 1.0 is not
    # required (see docstring).
    def all_nodes_ft_finalized():
        ft_values = {}
        for node in all_nodes:
            block = node.get_block(target_hash)
            ft_values[node.name] = float(block.blockInfo.faultTolerance)
        if all(ft >= ftt for ft in ft_values.values()):
            return ft_values
        logging.info("FT values: %s", {k: f"{v:.4f}" for k, v in ft_values.items()})
        return None

    ft_first = poll_until(
        predicate=all_nodes_ft_finalized,
        timeout=timeouts.finalization * 6,
        interval=5.0,
        description=f"all nodes report FT >= FTT={ftt:.2f} for block #{target_number}",
    )
    logging.info(
        "All nodes report FT >= FTT=%.2f: %s",
        ftt,
        {k: f"{v:.4f}" for k, v in ft_first.items()},
    )

    # Monotonicity check: cached FT may only grow — re-sample and verify
    # it never decreased and still clears FTT on every node.
    for node in all_nodes:
        block = node.get_block(target_hash)
        ft_now = float(block.blockInfo.faultTolerance)
        ft_before = ft_first[node.name]
        assert ft_now >= ftt, (
            f"FT for block #{target_number} dropped below FTT on {node.name}: "
            f"was {ft_before:.4f}, now {ft_now:.4f} < {ftt:.2f}"
        )
        assert ft_now >= ft_before - 1e-9, (
            f"FT for block #{target_number} decreased on {node.name}: "
            f"was {ft_before:.4f}, now {ft_now:.4f}"
        )

    logging.info(
        "FT monotonicity verified: block #%d holds FT >= FTT on all %d nodes",
        target_number,
        len(all_nodes),
    )


@pytest.mark.allow_forbidden_patterns("ComputationOutOfPhlogistons")
def test_network_converges_after_slow_deploy(convergence_shard, node_conf, timeouts) -> None:
    """Deploy a phlo-exhausting loop and verify the shard converges.

    The loop contract blocks V1 for ~25s while phlo is exhausted.
    During this time, V2 and V3 create independent heartbeat blocks,
    causing DAG tip divergence. After the deploy completes (errored),
    the network must converge and LFB must advance.

    Reproduces:
    - #224: phlo-exhausting deploy stalls the proposing validator
    - #437: resulting DAG tip divergence causes permanent LFB stall
    """
    validators = convergence_shard.validators
    all_nodes = convergence_shard.all_nodes

    baseline_lfb = lfb_number(validators[0])
    if baseline_lfb == 0:
        logging.info("Waiting for initial LFB advancement...")
        poll_until(
            predicate=lambda: lfb_number(validators[0]) if lfb_number(validators[0]) > 0 else None,
            timeout=timeouts.finalization,
            interval=5.0,
            description="initial LFB > 0",
        )
        baseline_lfb = lfb_number(validators[0])

    logging.info("Baseline LFB: #%d", baseline_lfb)

    # Deploy on V2 and V3 to create active state alongside the slow deploy
    validators[1].deploy_string(
        '@"pre-slow-v2"!(1)',
        VALIDATOR2_ID.private_key(),
    )
    validators[2].deploy_string(
        '@"pre-slow-v3"!(1)',
        VALIDATOR3_ID.private_key(),
    )

    # Submit the slow deploy on V1
    deploy_id = validators[0].deploy_string(
        SLOW_LOOP_CONTRACT,
        VALIDATOR1_ID.private_key(),
        phlo_limit=20_000_000,
        phlo_price=1,
    )
    logging.info("Deployed loop contract on V1, deploy_id=%s", deploy_id[:24])

    # Wait for the deploy to be included. The phlo-exhausting loop takes
    # ~25s to execute, much longer than normal deploy inclusion.
    info = wait_for_deploy_included(validators[0], deploy_id, timeout=timeouts.finalization * 10)
    deploy_block = info.blockNumber

    # Verify the slow deploy is errored (phlo exhausted)
    block_info = validators[0].get_block(info.blockHash)
    assert_deploy_errored(block_info, deploy_id)
    logging.info("Slow deploy errored as expected (phlo exhausted) in block #%d", deploy_block)

    # LFB must advance past the deploy block on ALL nodes
    target_lfb = deploy_block + 3
    logging.info(
        "Waiting for LFB to advance past deploy block #%d (target=#%d) on all nodes...",
        deploy_block,
        target_lfb,
    )

    # Poll the height and spread conditions together. Waiting per-node on a
    # lower bound and then snapshotting the spread measures scheduling luck:
    # nothing stops a fast node running ahead while the slower ones are still
    # being polled, and a lower bound alone cannot tell "still catching up"
    # from "permanently diverged". See wait_for_lfb_converged.
    #
    # Timeout is *5 for the same reason as the consensus-safety call site: the
    # previous helper latched, finishing once each node had crossed target at
    # any past instant, whereas this must observe a simultaneous tight band.
    # max_spread stays at 2 — extend the timeout, never the tolerance.
    final_lfbs = wait_for_lfb_converged(
        all_nodes,
        timeout=timeouts.finalization * 5,
        min_height=target_lfb,
        max_spread=2,
        description=f"LFB >= #{target_lfb} and spread <= 2 after slow deploy",
    )
    spread = max(final_lfbs.values()) - min(final_lfbs.values())
    logging.info("Final LFBs after slow deploy: %s (spread: %d)", final_lfbs, spread)

    _wait_for_lfb_ft_at_or_above_ftt(all_nodes, node_conf.ftt, timeouts.finalization)

    logging.info(
        "Network recovered after slow deploy (LFB spread: %d, FT >= FTT=%.2f)",
        spread,
        node_conf.ftt,
    )


def _lfb_past_genesis(node):
    """Return LFB if it has advanced past genesis, else None."""
    lfb = node.last_finalized_block()
    if lfb.blockInfo.blockNumber > 0:
        return lfb
    return None

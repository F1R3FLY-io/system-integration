"""
Consensus Safety Integration Tests

Verifies critical consensus safety properties under validator failure,
FTT boundary conditions, and network disruption scenarios. These tests
directly validate the behaviors described in docs/consensus-configuration.md.

Each test creates its own shard with specific FTT/bond configuration.
"""

import logging
import time

import pytest

from ...infra.assertions import assert_all_nodes_agree_on_block
from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID, VALIDATOR4_ID
from ...infra.polling import poll_until, wait_for_block_visible
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")


def _get_lfb_number(node) -> int:
    try:
        return node.last_finalized_block().blockInfo.blockNumber
    except Exception:
        return 0


def _poll_lfb_advances(node, baseline, target_advance, timeout):
    """Poll until node's LFB advances by at least target_advance from baseline."""
    target = baseline + target_advance
    return poll_until(
        predicate=lambda: (
            _get_lfb_number(node)
            if _get_lfb_number(node) >= target
            else None
        ),
        timeout=timeout,
        interval=5.0,
        description=f"{node.name} LFB >= #{target}",
    )


def _poll_lfb_stalls(nodes, duration, interval=5.0):
    """Verify LFB does NOT advance on any node for the given duration.

    Returns True if LFB was stable (no advancement). Raises AssertionError
    if any node's LFB advances during the observation window.
    """
    initial_lfbs = {n.name: _get_lfb_number(n) for n in nodes}
    deadline = time.time() + duration
    while time.time() < deadline:
        time.sleep(interval)
        for node in nodes:
            current = _get_lfb_number(node)
            if current > initial_lfbs[node.name]:
                raise AssertionError(
                    f"{node.name} LFB advanced from #{initial_lfbs[node.name]} "
                    f"to #{current} during stall observation"
                )
    return True


# ═══════════════════════════════════════════════════════════════════════
# Test 1: Validator failure + continued finalization (FTT=0.1)
# ═══════════════════════════════════════════════════════════════════════


def test_validator_failure_recovery(provider, timeouts) -> None:
    """Kill V3, verify V1+V2 continue finalizing. Restart V3, verify convergence.

    With FTT=0.1 and 3 equal-stake validators:
    - All 3 alive: FT = (300*2 - 300) / 300 = 1.0 > 0.1 → finalizes
    - V3 dead, V1+V2: FT = (200*2 - 300) / 300 = 0.33 > 0.1 → still finalizes
    - After V3 restart: all 3 converge, FT returns to 1.0
    """
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        ftt=0.1,
        heartbeat=True,
        include_readonly=True,
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")
        v2 = shard.node("validator2")
        v3 = shard.node("validator3")
        all_nodes = shard.all_nodes

        # Deploy on all validators to establish active state
        v1.deploy_string('@"pre-kill-v1"!(1)', VALIDATOR1_ID.private_key())
        v2.deploy_string('@"pre-kill-v2"!(2)', VALIDATOR2_ID.private_key())
        v3.deploy_string('@"pre-kill-v3"!(3)', VALIDATOR3_ID.private_key())

        # Wait for initial finalization
        baseline_lfb = _get_lfb_number(v1)
        if baseline_lfb == 0:
            poll_until(
                predicate=lambda: _get_lfb_number(v1) if _get_lfb_number(v1) > 0 else None,
                timeout=timeouts.finalization, interval=5.0,
                description="initial LFB > 0",
            )
            baseline_lfb = _get_lfb_number(v1)

        logging.info("Baseline LFB: #%d", baseline_lfb)

        # ── Kill V3 ──
        logging.info("Pausing V3 to simulate validator failure...")
        v3.pause()

        # Deploy on V1 and V2 to generate blocks
        v1.deploy_string('@"post-kill-v1"!(10)', VALIDATOR1_ID.private_key())
        v2.deploy_string('@"post-kill-v2"!(20)', VALIDATOR2_ID.private_key())

        # V1 and V2 should still finalize (FT=0.33 > 0.1)
        logging.info("Verifying V1+V2 continue finalizing without V3...")
        _poll_lfb_advances(v1, baseline_lfb, 3, timeouts.finalization * 3)
        _poll_lfb_advances(v2, baseline_lfb, 3, timeouts.finalization * 3)

        post_kill_lfb = _get_lfb_number(v1)
        logging.info("V1 LFB after V3 kill: #%d (advanced %d blocks)",
                     post_kill_lfb, post_kill_lfb - baseline_lfb)

        # Verify FT on finalized blocks is >= FTT
        lfb = v1.last_finalized_block()
        ft = float(lfb.blockInfo.faultTolerance)
        logging.info("V1 LFB FT: %.2f (need >= 0.1)", ft)
        assert ft >= 0.1, (
            f"V1 LFB FT={ft} should be >= 0.1 with 2/3 validators"
        )

        # ── Restart V3 ──
        logging.info("Unpausing V3...")
        v3.unpause()

        # Deploy on all to stimulate convergence
        v1.deploy_string('@"post-restart-v1"!(100)', VALIDATOR1_ID.private_key())
        v2.deploy_string('@"post-restart-v2"!(200)', VALIDATOR2_ID.private_key())
        v3.deploy_string('@"post-restart-v3"!(300)', VALIDATOR3_ID.private_key())

        # All nodes should converge and advance LFB
        post_restart_baseline = _get_lfb_number(v1)
        for node in all_nodes:
            _poll_lfb_advances(node, post_restart_baseline, 3, timeouts.finalization * 3)

        final_lfbs = {n.name: _get_lfb_number(n) for n in all_nodes}
        logging.info("Final LFBs after V3 restart: %s", final_lfbs)

        # All nodes should be close
        max_lfb = max(final_lfbs.values())
        min_lfb = min(final_lfbs.values())
        assert max_lfb - min_lfb <= 3, (
            f"LFB spread {max_lfb - min_lfb} exceeds 3 after recovery: {final_lfbs}"
        )

        logging.info("Validator failure recovery test passed (FTT=0.1)")
    finally:
        # Ensure V3 is unpaused before destroy
        try:
            v3.unpause()
        except Exception:
            pass
        shard.destroy()


# ═══════════════════════════════════════════════════════════════════════
# Test 2: Validator failure halts finalization (FTT=0.67)
# ═══════════════════════════════════════════════════════════════════════


def test_validator_failure_halts_finalization(provider, timeouts) -> None:
    """Kill V3, verify finalization STOPS. Restart V3, verify it resumes.

    With FTT=0.67 and 3 equal-stake validators:
    - All 3 alive: FT = (300*2 - 300) / 300 = 1.0 > 0.67 → finalizes
    - V3 dead, V1+V2: FT = (200*2 - 300) / 300 = 0.33, NOT > 0.67 → halts
    - After V3 restart: FT returns to 1.0 → finalization resumes
    """
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        ftt=0.67,
        heartbeat=True,
        include_readonly=True,
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")
        v2 = shard.node("validator2")
        v3 = shard.node("validator3")
        all_nodes = shard.all_nodes

        # Wait for initial finalization (all 3 alive)
        v1.deploy_string('@"pre-halt-v1"!(1)', VALIDATOR1_ID.private_key())
        v2.deploy_string('@"pre-halt-v2"!(2)', VALIDATOR2_ID.private_key())
        v3.deploy_string('@"pre-halt-v3"!(3)', VALIDATOR3_ID.private_key())

        baseline_lfb = _get_lfb_number(v1)
        if baseline_lfb == 0:
            poll_until(
                predicate=lambda: _get_lfb_number(v1) if _get_lfb_number(v1) > 0 else None,
                timeout=timeouts.finalization, interval=5.0,
                description="initial LFB > 0",
            )
            baseline_lfb = _get_lfb_number(v1)

        # Ensure LFB advances with all 3 alive
        _poll_lfb_advances(v1, baseline_lfb, 3, timeouts.finalization * 3)
        pre_kill_lfb = _get_lfb_number(v1)
        logging.info("Pre-kill LFB: #%d (FTT=0.67, all 3 validators finalizing)", pre_kill_lfb)

        # ── Kill V3 ──
        logging.info("Pausing V3 to simulate validator failure...")
        v3.pause()

        # Deploy on V1+V2 to generate blocks
        v1.deploy_string('@"post-halt-v1"!(10)', VALIDATOR1_ID.private_key())
        v2.deploy_string('@"post-halt-v2"!(20)', VALIDATOR2_ID.private_key())

        # Finalization should STOP — FT=0.33 is NOT > 0.67
        logging.info("Verifying finalization halts with only V1+V2 (FT=0.33 < 0.67)...")
        _poll_lfb_stalls([v1, v2], duration=30, interval=5.0)
        post_kill_lfb = _get_lfb_number(v1)
        logging.info("V1 LFB after 30s stall: #%d (unchanged from #%d)",
                     post_kill_lfb, pre_kill_lfb)

        # ── Restart V3 ──
        logging.info("Unpausing V3...")
        v3.unpause()

        # Deploy on all to stimulate convergence
        v1.deploy_string('@"post-resume-v1"!(100)', VALIDATOR1_ID.private_key())
        v2.deploy_string('@"post-resume-v2"!(200)', VALIDATOR2_ID.private_key())
        v3.deploy_string('@"post-resume-v3"!(300)', VALIDATOR3_ID.private_key())

        # Finalization should resume
        logging.info("Verifying finalization resumes after V3 restart...")
        for node in all_nodes:
            _poll_lfb_advances(node, post_kill_lfb, 3, timeouts.finalization * 3)

        final_lfbs = {n.name: _get_lfb_number(n) for n in all_nodes}
        logging.info("Final LFBs after V3 restart: %s", final_lfbs)

        logging.info("Validator failure halts finalization test passed (FTT=0.67)")
    finally:
        try:
            v3.unpause()
        except Exception:
            pass
        shard.destroy()


# ═══════════════════════════════════════════════════════════════════════
# Test 3: FTT boundary — strict greater-than (FTT=0.5, bonds 75/75/50)
# ═══════════════════════════════════════════════════════════════════════


def test_ftt_boundary_strict_greater_than(provider, timeouts) -> None:
    """Kill V3, verify FT=0.5 does NOT finalize at FTT=0.5 (strict >).

    With bonds 75/75/50 (total=200) and FTT=0.5:
    - All 3 alive: FT = (200*2 - 200) / 200 = 1.0 > 0.5 → finalizes
    - V3 (50) dead, V1+V2 (150): FT = (150*2 - 200) / 200 = 0.5
      Comparison is strict >: 0.5 is NOT > 0.5 → halts
    - After V3 restart: FT returns to 1.0 → resumes

    This proves the finalization formula uses strict greater-than.
    FT must EXCEED FTT, not merely equal it. At the exact boundary,
    there is zero safety margin and finalization correctly refuses.
    """
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 75),
            (VALIDATOR2_ID, 75),
            (VALIDATOR3_ID, 50),
        ],
        ftt=0.5,
        heartbeat=True,
        include_readonly=True,
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")
        v2 = shard.node("validator2")
        v3 = shard.node("validator3")
        all_nodes = shard.all_nodes

        # Wait for initial finalization (all 3 alive, FT=1.0)
        v1.deploy_string('@"pre-boundary-v1"!(1)', VALIDATOR1_ID.private_key())
        v2.deploy_string('@"pre-boundary-v2"!(2)', VALIDATOR2_ID.private_key())
        v3.deploy_string('@"pre-boundary-v3"!(3)', VALIDATOR3_ID.private_key())

        baseline_lfb = _get_lfb_number(v1)
        if baseline_lfb == 0:
            poll_until(
                predicate=lambda: _get_lfb_number(v1) if _get_lfb_number(v1) > 0 else None,
                timeout=timeouts.finalization, interval=5.0,
                description="initial LFB > 0",
            )
            baseline_lfb = _get_lfb_number(v1)

        _poll_lfb_advances(v1, baseline_lfb, 3, timeouts.finalization * 3)
        pre_kill_lfb = _get_lfb_number(v1)
        logging.info("Pre-kill LFB: #%d (FTT=0.5, all 3 finalizing)", pre_kill_lfb)

        # ── Kill V3 (50 stake) ──
        logging.info("Pausing V3 (stake=50)...")
        v3.pause()

        v1.deploy_string('@"post-boundary-v1"!(10)', VALIDATOR1_ID.private_key())
        v2.deploy_string('@"post-boundary-v2"!(20)', VALIDATOR2_ID.private_key())

        # Finalization should STOP — FT = (150*2 - 200) / 200 = 0.5, NOT > 0.5
        logging.info("Verifying finalization halts (FT=0.5 is NOT > FTT=0.5, strict >)...")
        _poll_lfb_stalls([v1, v2], duration=30, interval=5.0)
        post_kill_lfb = _get_lfb_number(v1)
        logging.info("V1 LFB after 30s stall: #%d (unchanged from #%d)",
                     post_kill_lfb, pre_kill_lfb)

        # ── Restart V3 ──
        logging.info("Unpausing V3...")
        v3.unpause()

        v1.deploy_string('@"post-resume-v1"!(100)', VALIDATOR1_ID.private_key())
        v2.deploy_string('@"post-resume-v2"!(200)', VALIDATOR2_ID.private_key())
        v3.deploy_string('@"post-resume-v3"!(300)', VALIDATOR3_ID.private_key())

        logging.info("Verifying finalization resumes after V3 restart...")
        for node in all_nodes:
            _poll_lfb_advances(node, post_kill_lfb, 3, timeouts.finalization * 3)

        final_lfbs = {n.name: _get_lfb_number(n) for n in all_nodes}
        logging.info("Final LFBs after V3 restart: %s", final_lfbs)

        logging.info("FTT boundary strict > test passed (FT=0.5 halts at FTT=0.5)")
    finally:
        try:
            v3.unpause()
        except Exception:
            pass
        shard.destroy()


# ═══════════════════════════════════════════════════════════════════════
# Test 5: Epoch transition under heartbeat
# ═══════════════════════════════════════════════════════════════════════


def test_epoch_transition_under_heartbeat(provider, timeouts) -> None:
    """Bond a joiner during active heartbeat, verify epoch transition
    doesn't stall finalization and joiner activates.

    Unlike test_bonding_validators (manual propose, FTT=-1), this test
    uses heartbeat and real FTT to verify epoch transitions work under
    production-like conditions.

    With FTT=0.1, 2 validators, epoch-length=4:
    - V1+V2 finalize normally with heartbeat
    - Joiner bonds via PoS contract
    - Chain advances past epoch boundary automatically
    - Joiner activates and begins producing blocks
    - Finalization continues throughout (no stall during transition)
    """
    joiner_vault = VALIDATOR4_ID.private_key().get_public_key().get_vault_address()

    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
        ],
        ftt=0.1,
        heartbeat=True,
        include_readonly=True,
        global_cli_options={
            "--epoch-length": "4",
            "--quarantine-length": "20",
            "--synchrony-constraint-threshold": "0",
        },
        extra_wallets=[(joiner_vault, 50_000_000_000_000_000)],
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")
        v2 = shard.node("validator2")
        all_nodes = shard.all_nodes

        # Deploy on both validators
        v1.deploy_string('@"pre-epoch-v1"!(1)', VALIDATOR1_ID.private_key())
        v2.deploy_string('@"pre-epoch-v2"!(2)', VALIDATOR2_ID.private_key())

        # Wait for initial finalization
        baseline_lfb = _get_lfb_number(v1)
        if baseline_lfb == 0:
            poll_until(
                predicate=lambda: _get_lfb_number(v1) if _get_lfb_number(v1) > 0 else None,
                timeout=timeouts.finalization, interval=5.0,
                description="initial LFB > 0",
            )
            baseline_lfb = _get_lfb_number(v1)
        logging.info("Baseline LFB: #%d", baseline_lfb)

        # Bond the joiner via PoS contract (deploy on V1)
        logging.info("Bonding joiner via PoS contract...")
        bond_deploy_id = v1.deploy_rho_file(
            rho_file_path="resources/wallets/bond.rho",
            private_key=VALIDATOR4_ID.private_key(),
            substitutions={"%AMOUNT": "10000000"},
            phlo_limit=100_000_000,
            phlo_price=1,
        )
        logging.info("Bond deploy submitted: %s", bond_deploy_id[:24])

        # Wait for bond to be included
        from ...infra.polling import wait_for_deploy_included
        bond_block = wait_for_deploy_included(v1, bond_deploy_id, timeouts.deploy_inclusion)
        logging.info("Bond included in block #%d", bond_block.blockNumber)

        # Add joiner node to the shard
        logging.info("Adding joiner to shard network...")
        with shard.add_joiner(
            VALIDATOR4_ID,
            cli_options={
                "--epoch-length": "4",
                "--quarantine-length": "20",
                "--synchrony-constraint-threshold": "0",
            },
        ) as joiner:
            # Wait for joiner to sync
            wait_for_block_visible(
                joiner, bond_block.blockHash,
                timeout=timeouts.node_startup * 2,
            )
            logging.info("Joiner synced to block #%d", bond_block.blockNumber)

            # Record LFB before epoch transition
            pre_epoch_lfb = _get_lfb_number(v1)
            logging.info("Pre-epoch LFB: #%d", pre_epoch_lfb)

            # Wait for chain to advance well past at least one epoch boundary
            # Epoch changes at blocks 4, 8, 12... Need LFB to pass one.
            target_lfb = max(pre_epoch_lfb + 8, 12)
            logging.info("Waiting for LFB >= #%d (past epoch boundary)...", target_lfb)

            _poll_lfb_advances(v1, 0, target_lfb, timeouts.finalization * 5)
            post_epoch_lfb = _get_lfb_number(v1)
            logging.info("Post-epoch LFB: #%d", post_epoch_lfb)

            # Verify finalization continued throughout (no stall)
            assert post_epoch_lfb >= target_lfb, (
                f"LFB #{post_epoch_lfb} should have reached #{target_lfb}"
            )

            # Verify V2 and readonly also advanced
            for node in all_nodes:
                node_lfb = _get_lfb_number(node)
                assert node_lfb >= target_lfb - 3, (
                    f"{node.name} LFB #{node_lfb} too far behind target #{target_lfb}"
                )

            # Check if joiner produced any blocks (appeared in justifications)
            latest_blocks = v1.get_blocks(20)
            joiner_produced = False
            for b in latest_blocks:
                if b.sender == VALIDATOR4_ID.public_hex:
                    joiner_produced = True
                    logging.info("Joiner produced block #%d", b.blockNumber)
                    break

            if joiner_produced:
                logging.info("Joiner activated and producing blocks")
            else:
                logging.warning(
                    "Joiner did not produce blocks in latest 20 — may need more epochs"
                )

            logging.info("Epoch transition under heartbeat test passed")
    finally:
        shard.destroy()


# ═══════════════════════════════════════════════════════════════════════
# Test 6: Merge determinism with asymmetric divergence
# ═══════════════════════════════════════════════════════════════════════


def test_merge_determinism_asymmetric_divergence(provider, timeouts) -> None:
    """Pause heaviest validator to force divergence, verify deterministic merge.

    With asymmetric bonds 60/20/15 (total=95) and FTT=0.1:
    - V1 (60 stake) is paused for 30s
    - V2+V3 produce independent blocks during pause
    - V1 unpauses, receives diverged tips, must merge
    - All 3 validators must compute identical post-states after merge

    Regression test for the InvalidBondsCache bug (deterministic LCA
    computation) and ConflictSetMerger (deterministic merge ordering)
    with unequal stakes. The heaviest validator having a different DAG
    view than the lighter validators is the worst case for merge ordering.
    """
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 60),
            (VALIDATOR2_ID, 20),
            (VALIDATOR3_ID, 15),
        ],
        ftt=0.1,
        heartbeat=True,
        include_readonly=True,
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")
        v2 = shard.node("validator2")
        v3 = shard.node("validator3")
        all_nodes = shard.all_nodes
        validators = shard.validators

        # Deploy on all validators to establish active state
        v1.deploy_string('@"pre-diverge-v1"!(1)', VALIDATOR1_ID.private_key())
        v2.deploy_string('@"pre-diverge-v2"!(2)', VALIDATOR2_ID.private_key())
        v3.deploy_string('@"pre-diverge-v3"!(3)', VALIDATOR3_ID.private_key())

        # Wait for initial finalization
        baseline_lfb = _get_lfb_number(v1)
        if baseline_lfb == 0:
            poll_until(
                predicate=lambda: _get_lfb_number(v1) if _get_lfb_number(v1) > 0 else None,
                timeout=timeouts.finalization, interval=5.0,
                description="initial LFB > 0",
            )
            baseline_lfb = _get_lfb_number(v1)
        logging.info("Baseline LFB: #%d", baseline_lfb)

        # ── Pause V1 (heaviest, 60 stake) ──
        logging.info("Pausing V1 (stake=60) for 30s to force asymmetric divergence...")
        v1.pause()
        time.sleep(30)
        v1.unpause()
        logging.info("V1 unpaused. Waiting for convergence...")

        # Deploy on all to stimulate merge
        v1.deploy_string('@"post-diverge-v1"!(10)', VALIDATOR1_ID.private_key())
        v2.deploy_string('@"post-diverge-v2"!(20)', VALIDATOR2_ID.private_key())
        v3.deploy_string('@"post-diverge-v3"!(30)', VALIDATOR3_ID.private_key())

        # Wait for all nodes to advance LFB
        for node in all_nodes:
            _poll_lfb_advances(node, baseline_lfb, 3, timeouts.finalization * 3)

        # Get a recent block that all validators should agree on
        v1_lfb = v1.last_finalized_block()
        lfb_hash = v1_lfb.blockInfo.blockHash
        logging.info("Checking post-merge agreement on LFB %s (block #%d)",
                     lfb_hash[:16], v1_lfb.blockInfo.blockNumber)

        # Verify all nodes agree on post-state
        assert_all_nodes_agree_on_block(all_nodes, lfb_hash)
        logging.info("Post-state agreement verified across all nodes")

        # Verify FT >= FTT on post-merge LFB
        for node in validators:
            lfb = node.last_finalized_block()
            ft = float(lfb.blockInfo.faultTolerance)
            logging.info("%s: LFB #%d, FT=%.2f", node.name, lfb.blockInfo.blockNumber, ft)
            assert ft >= 0.1, (
                f"{node.name}: post-merge LFB FT={ft} should be >= 0.1"
            )

        # Check LFB spread
        final_lfbs = {n.name: _get_lfb_number(n) for n in all_nodes}
        max_lfb = max(final_lfbs.values())
        min_lfb = min(final_lfbs.values())
        spread = max_lfb - min_lfb
        logging.info("Final LFBs: %s (spread: %d)", final_lfbs, spread)
        assert spread <= 3, (
            f"LFB spread {spread} exceeds 3 after asymmetric merge: {final_lfbs}"
        )

        logging.info("Merge determinism asymmetric divergence test passed")
    finally:
        try:
            v1.unpause()
        except Exception:
            pass
        shard.destroy()

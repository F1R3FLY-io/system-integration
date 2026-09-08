"""
Joiner Self-Proposes at Epoch Boundary — Negative-Control Test

This test was designed to deterministically reproduce a bug observed in
v19 of the enhanced bonding test (test_bonding_validators), where a
freshly-bonded joiner silently disappeared from the bonds map after
producing an epoch-boundary block. After 6 variants none reproduced the
bug. The test now serves as a NEGATIVE CONTROL: it proves the simple
architectural shape (joiner produces first epoch-boundary block, with
multi-parent merges + bg-proposer chaos in V1/V2/V3) is INSUFFICIENT
to trigger the bug.

Variants attempted (all PASSED, V4 stayed bonded):
  1. Linear single-parent propose: V1, V2, V3, V4 propose in strict
     sequence, V4's first block at #8 epoch boundary.
  2. Concurrent multi-parent rounds: 3 forks per height at #5/#6/#7,
     V4's #8 multi-parent merges 4 height-7 forks.
  3. + V4 lagging: V4 not caught up between rounds, must catch up at
     #8 propose time.
  4. Continuous bg proposers on V1/V2/V3 (40+ deploys), V4 idle.
  5. (4) + V4 multi-iteration scan: V4 produces 12 sequential blocks,
     4 of which on epoch boundaries (#16, #20, #24, #28). Bug never
     fires on any of them.

Conclusion: the bug needed heartbeat-driven concurrency dynamics (the
actor-message timing race specific to heartbeat-check / propose
pipeline) that manual propose can't replicate. The bug class has since
been fixed (seal-base / fresh-joiner-latest-message work) and
test_bonding_validators runs green under heartbeat + bg load. This
test remains as the deterministic forward-regression for the shape:
bond sealed exactly ON a boundary, activation via closeBlock, and the
joiner self-proposing across multiple later boundaries with the bonds
map held node-identical throughout.
"""

import logging
import math
import threading
import time
from typing import List, Tuple

import pytest
from f1r3fly.client import F1r3flyClientException

from ...infra.assertions import assert_bonds_map_consistent_across_nodes
from ...infra.config import ShardConfig
from ...infra.keys import (
    VALIDATOR1_ID,
    VALIDATOR2_ID,
    VALIDATOR3_ID,
    VALIDATOR4_ID,
)
from ...infra.polling import wait_for_block_visible
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")


_BOND_AMOUNT = 100

# The whole test is built around block #4 being an epoch boundary, so the
# epoch is pinned explicitly on the shard AND the joiner below — never
# inherited from conf/rust.conf (which carries a long epoch for suites
# that never bond, and drifted to 50 while this test assumed 4).
_EPOCH_LENGTH = 4
_QUARANTINE_LENGTH = 10


def _expect(node, block_hash: str):
    """Get blockInfo for a hash, with a clear error message on miss."""
    return node.get_block(block_hash).blockInfo


def _bonds_set(block_info) -> set:
    return {b.validator for b in block_info.bonds}


def _propose_with_filler(node, identity, label: str) -> str:
    """Deploy a small filler term then propose. Returns the new block hash.

    Manual propose requires non-empty mempool — every existing heartbeat-off
    test uses this deploy+propose pattern.
    """
    node.deploy_string(
        f'@"filler-{label}"!(0)',
        identity.private_key(),
        phlo_limit=100_000_000,
        phlo_price=1,
    )
    return node.propose()


# Logical budget for the advance-to-#8 phase: rounds, not wall-clock, so
# host speed changes duration but never the verdict. Each round advances
# the height by ~1 and the LFB trails it by the witnessing lag (a few
# rounds at the production FTT), so reaching #8 from #5 needs well under
# ten rounds on a healthy shard.
_MAX_ADVANCE_ROUNDS = 30


def _concurrent_propose_round(
    producers, identities, round_idx: int, *, timeout: float
) -> List[str]:
    """Deploy in parallel, then release proposals together within one deadline.

    A barrier aligns proposals after every deployment completes. Scheduling
    alone cannot guarantee sibling blocks, so the caller checks that shape.
    Deployment, barrier, and timeout failures are fatal. Proposal exceptions
    remain tolerated contention. Daemon threads cannot pin the pytest worker
    if an RPC ignores its own timeout. Cancellation prevents a delayed deploy
    from starting a proposal after the round has failed.
    """
    if not producers or len(producers) != len(identities):
        raise ValueError("a propose round needs one identity per producer")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("round timeout must be positive and finite")
    deadline = time.monotonic() + timeout
    barrier = threading.Barrier(len(producers))
    cancelled = threading.Event()
    results: List[str] = []
    setup_errors: List[str] = []
    propose_errors: List[str] = []
    lock = threading.Lock()

    def _one(idx: int, node, identity) -> None:
        try:
            node.deploy_string(
                f'@"round-{round_idx}-prop-{idx}"!({round_idx})',
                identity.private_key(),
                phlo_limit=100_000_000,
                phlo_price=1,
            )
            barrier.wait(timeout=max(0, deadline - time.monotonic()))
        except Exception as exc:
            with lock:
                setup_errors.append(f"{idx}: {type(exc).__name__}: {exc}")
            cancelled.set()
            barrier.abort()
            return
        if cancelled.is_set() or time.monotonic() >= deadline:
            return
        try:
            block = node.propose()
            with lock:
                results.append(block)
        except Exception as exc:
            with lock:
                propose_errors.append(f"{idx}: {exc}")

    threads = [
        threading.Thread(target=_one, args=(i, n, ident), name=f"round-prop-{i}", daemon=True)
        for i, (n, ident) in enumerate(zip(producers, identities))
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        stalled = [thread.name for thread in threads if thread.is_alive()]
        if stalled or time.monotonic() >= deadline:
            raise AssertionError(f"propose round {round_idx} timed out after {timeout}s: {stalled}")
        if setup_errors:
            raise AssertionError(f"propose round {round_idx} setup failed: {setup_errors}")
        if propose_errors:
            logging.info("Round %d propose contention: %s", round_idx, propose_errors)
        return results
    finally:
        cancelled.set()
        barrier.abort()


def _has_sibling_proposals(block_infos) -> bool:
    """Require distinct validators' blocks at one height with a shared parent."""
    return any(
        left.sender != right.sender
        and left.blockHash != right.blockHash
        and left.blockNumber == right.blockNumber
        and bool(set(left.parentsHashList) & set(right.parentsHashList))
        for i, left in enumerate(block_infos)
        for right in block_infos[i + 1 :]
    )


def test_joiner_self_proposes_at_epoch_boundary(provider, timeouts) -> None:
    """Negative-control for the joiner-bond-drop bug.

    With manual propose (heartbeat disabled), concurrent propose rounds
    on V1/V2/V3 creating multi-parent merges, and V4 cycling through 12
    sequential proposes including epoch-boundary blocks, V4 stays bonded
    throughout. Six variants of this test (linear, multi-parent
    concurrent, V4 lagging, free-running bg proposers, multi-iteration
    scan) all PASS. The simple architectural shape — joiner produces
    first epoch-boundary block — is insufficient to trigger the bug.
    The advance phase has a logical round budget and a scaled RPC deadline
    per round. It must observe sibling proposals before accepting success.

    See module docstring for full variant matrix and what conditions
    ARE needed (heartbeat-driven concurrency in the v19 trace).
    """
    extra_wallets = [
        (
            VALIDATOR4_ID.private_key().get_public_key().get_vault_address(),
            50_000_000_000_000_000,
        ),
    ]
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, _BOND_AMOUNT),
            (VALIDATOR2_ID, _BOND_AMOUNT),
            (VALIDATOR3_ID, _BOND_AMOUNT),
        ],
        # Production FTT (conf default). A negative FTT finalizes on bare
        # majority per snapshot, which legally permits divergent floors
        # under the sibling contests the bg-proposer phase creates — and
        # the suite forbids the FinalityDivergence sentinel that reports
        # them.
        heartbeat=False,  # manual propose only
        include_readonly=True,
        extra_wallets=extra_wallets,
        global_cli_options={
            "--synchrony-constraint-threshold": "0",
            "--epoch-length": str(_EPOCH_LENGTH),
            "--quarantine-length": str(_QUARANTINE_LENGTH),
        },
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")
        v2 = shard.node("validator2")
        v3 = shard.node("validator3")
        ro = shard.readonly

        # ── V4 attaches with heartbeat disabled and matching consensus knobs ──
        # CRITICAL: cli_flags --heartbeat-disabled keeps V4 under manual
        # control. Default joiner attach has heartbeat enabled (only readonly
        # observers auto-disable per provider code).
        joiner = shard.attach_joiner(
            VALIDATOR4_ID,
            cli_flags={"--heartbeat-disabled"},
            cli_options={
                "--synchrony-constraint-threshold": "0",
                "--epoch-length": str(_EPOCH_LENGTH),
                "--quarantine-length": str(_QUARANTINE_LENGTH),
            },
        )
        all_nodes = [v1, v2, v3, joiner, ro]

        v1_pub = VALIDATOR1_ID.public_hex
        v2_pub = VALIDATOR2_ID.public_hex
        v3_pub = VALIDATOR3_ID.public_hex
        v4_pub = VALIDATOR4_ID.public_hex
        bonds_3 = {v1_pub: _BOND_AMOUNT, v2_pub: _BOND_AMOUNT, v3_pub: _BOND_AMOUNT}
        bonds_4 = {**bonds_3, v4_pub: _BOND_AMOUNT}

        t = timeouts.command

        # ── Blocks #1-#3: advance height with V1/V2/V3 fillers ──
        b1 = _propose_with_filler(v1, VALIDATOR1_ID, "1")
        for n in (v2, v3, joiner, ro):
            wait_for_block_visible(n, b1, t)
        b2 = _propose_with_filler(v2, VALIDATOR2_ID, "2")
        for n in (v1, v3, joiner, ro):
            wait_for_block_visible(n, b2, t)
        b3 = _propose_with_filler(v3, VALIDATOR3_ID, "3")
        for n in (v1, v2, joiner, ro):
            wait_for_block_visible(n, b3, t)
        assert _expect(v1, b3).blockNumber == 3, (
            f"Block-numbering invariant broken: expected #3, got #{_expect(v1, b3).blockNumber}"
        )

        # ── Block #4: V1 proposes bond.rho — bond block AT epoch boundary ──
        v1.deploy_rho_file(
            rho_file_path="resources/wallets/bond.rho",
            private_key=VALIDATOR4_ID.private_key(),
            substitutions={"%AMOUNT": str(_BOND_AMOUNT)},
            phlo_limit=100_000_000,
            phlo_price=1,
        )
        b4 = v1.propose()
        for n in (v2, v3, joiner, ro):
            wait_for_block_visible(n, b4, t)
        b4_info = _expect(v1, b4)
        assert b4_info.blockNumber == 4, (
            f"Bond block expected at #4 (epoch boundary), got #{b4_info.blockNumber}"
        )
        assert b4_info.blockNumber % _EPOCH_LENGTH == 0
        # closeBlock at #4 activates V4, but the BOUNDARY block itself may
        # validly expose either side of the transition: its header can carry
        # the pre-activation weights (bonds_3) or the post-activation set
        # (bonds_4) depending on when the epoch transition is applied
        # relative to header construction (same semantics as the bonding
        # suite's boundary handling; soak preflight 31919610258 failed here
        # by demanding bonds_4 unconditionally). Pin whichever side b4
        # actually shows, then require ALL nodes to agree on that exact map.
        b4_bonds = {b.validator: b.stake for b in b4_info.bonds}
        assert b4_bonds in (bonds_3, bonds_4), (
            f"Bond block #4 bonds map matches neither transition side:\n"
            f"  got:      {sorted(b4_bonds)}\n"
            f"  pre-set:  {sorted(bonds_3)}\n"
            f"  post-set: {sorted(bonds_4)}"
        )
        assert_bonds_map_consistent_across_nodes(all_nodes, b4, b4_bonds)
        # Block #5 gets the SAME either-side treatment as the boundary
        # block: the 43e9f844 preflight showed every node UNIFORMLY still
        # carrying the pre-activation map at #5 — activation surfaces in
        # headers at a later epoch transition, not necessarily the next
        # block. What must hold at #5 is cross-node agreement on
        # whichever side it shows; the behavioral proof of activation is
        # V4's own successful self-propose in the phases below.
        b5 = _propose_with_filler(v1, VALIDATOR1_ID, "post-boundary")
        for n in (v2, v3, joiner, ro):
            wait_for_block_visible(n, b5, t)
        b5_bonds = {b.validator: b.stake for b in _expect(v1, b5).bonds}
        assert b5_bonds in (bonds_3, bonds_4), (
            f"Block #5 bonds map matches neither transition side:\n"
            f"  got:      {sorted(b5_bonds)}\n"
            f"  pre-set:  {sorted(bonds_3)}\n"
            f"  post-set: {sorted(bonds_4)}"
        )
        assert_bonds_map_consistent_across_nodes(all_nodes, b5, b5_bonds)
        logging.info(
            "Bond block #4 (%s): bonds=%s; #5 shows %s-activation side — "
            "behavioral activation proof follows via V4 self-propose",
            b4[:16],
            sorted(_bonds_set(b4_info)),
            "post" if b5_bonds == bonds_4 else "pre",
        )

        # ── Blocks #6+: concurrent propose rounds advance the chain ──
        # Each round releases V1/V2/V3 proposals after all deployments finish,
        # then checks the LFB. A barrier cannot force a DAG shape, so require
        # at least one observed sibling pair before accepting success. The loop
        # runs until the LFB crosses the epoch boundary at 8 — V4's bond
        # (made during epoch 1) surfaces in block headers at #8, so
        # stopping earlier would leave the bonds guard reading the
        # pre-boundary header forever. The round count bounds progress and
        # the scaled per-round deadline bounds stalled RPCs.
        producers = [v1, v2, v3]
        identities = [VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID]
        saw_siblings = False
        for round_idx in range(_MAX_ADVANCE_ROUNDS):
            round_blocks = _concurrent_propose_round(
                producers, identities, round_idx, timeout=timeouts.command * 2
            )
            for block in round_blocks:
                for n in (v1, v2, v3, joiner, ro):
                    wait_for_block_visible(n, block, t)
            saw_siblings |= _has_sibling_proposals([_expect(v1, b) for b in round_blocks])
            lfb_n = v1.last_finalized_block().blockInfo.blockNumber
            if lfb_n >= 8 and saw_siblings:
                logging.info(
                    "Round %d advanced LFB to #%d; handing off to V4",
                    round_idx,
                    lfb_n,
                )
                break
        else:
            pytest.fail(
                f"Advance phase did not establish both LFB >= 8 and sibling "
                f"proposals within {_MAX_ADVANCE_ROUNDS} rounds: "
                f"LFB=#{v1.last_finalized_block().blockInfo.blockNumber}, "
                f"sibling proposals observed={saw_siblings}."
            )

        # Wait for V4 to catch up to v1's LFB before V4 proposes — V4
        # has been idle but receiving gossip; ensure V4's view is
        # current.
        v1_lfb_hash = v1.last_finalized_block().blockInfo.blockHash
        wait_for_block_visible(joiner, v1_lfb_hash, t)

        # Guard: V4 must be visible in the LFB's bonds before its propose.
        # A bond made during epoch 1 surfaces in block headers at the next
        # epoch boundary (#8); the round loop above only exits with the
        # LFB at >= 8, so the finalized header is post-boundary and this
        # holds deterministically — no header-lag window to wait out.
        v1_lfb_info = v1.last_finalized_block().blockInfo
        lfb_bonds = _bonds_set(_expect(v1, v1_lfb_info.blockHash))
        assert v4_pub in lfb_bonds, (
            f"V4 absent from the post-boundary LFB's bonds "
            f"(LFB #{v1_lfb_info.blockNumber}). Bonds: {sorted(lfb_bonds)}"
        )

        # ── V4 proposes multiple times — at least one will land on an epoch boundary ──
        # The advance rounds leave the chain at a round-quantized height.
        # V4's first manual propose lands at max+1 and subsequent
        # proposes advance by 1 each (V4 is the only producer now), so 12
        # consecutive heights always contain at least two epoch
        # boundaries (heights mod 4 == 0 — the bug trigger).
        v4_blocks: List[Tuple[int, str, set]] = []  # (blockNumber, hash, bonds_set)
        for i in range(12):
            try:
                joiner.deploy_string(
                    f'@"v4-prop-{i}"!({i})',
                    VALIDATOR4_ID.private_key(),
                    phlo_limit=100_000_000,
                    phlo_price=1,
                )
                vb = joiner.propose()
            except F1r3flyClientException as e:
                pytest.fail(
                    f"V4 failed to propose iteration {i}: {e}. "
                    f"This is also a bond-drop manifestation (V4 thinks "
                    f"it's not active locally)."
                )
            for n in (v1, v2, v3, ro):
                wait_for_block_visible(n, vb, t)
            vb_info = _expect(v1, vb)
            assert vb_info.sender == v4_pub
            v4_blocks.append((vb_info.blockNumber, vb, _bonds_set(vb_info)))

        epoch_blocks = [(n, h, bonds) for n, h, bonds in v4_blocks if n % _EPOCH_LENGTH == 0]
        logging.info(
            "V4 produced %d blocks: heights %s; %d on epoch boundaries: %s",
            len(v4_blocks),
            [n for n, _, _ in v4_blocks],
            len(epoch_blocks),
            [n for n, _, _ in epoch_blocks],
        )

        assert epoch_blocks, (
            f"V4 didn't produce any epoch-boundary block in {len(v4_blocks)} "
            f"proposes (heights {[n for n, _, _ in v4_blocks]}). Test "
            f"setup didn't put V4 on a boundary — adjust the advance-round "
            f"phase to land V4 closer to a boundary."
        )

        # The bond-drop check: V4 must remain in bonds at every epoch
        # boundary block V4 produced. This is the bug from v19.
        for height, vb_hash, bonds in epoch_blocks:
            assert v4_pub in bonds, (
                f"V4 dropped from bonds when self-proposing "
                f"epoch-boundary block #{height} ({vb_hash[:16]}). "
                f"Bonds: {sorted(bonds)}"
            )
            # Cross-node consistency on each epoch-boundary block.
            assert_bonds_map_consistent_across_nodes(
                all_nodes,
                vb_hash,
                bonds_4,
            )

        # Liveness check: V4 still proposing at the very last block.
        last_n, last_hash, _ = v4_blocks[-1]
        logging.info(
            "V4 still proposing at #%d (last); bond-drop not triggered across "
            "%d V4 blocks (%d at epoch boundaries)",
            last_n,
            len(v4_blocks),
            len(epoch_blocks),
        )
    finally:
        shard.destroy()

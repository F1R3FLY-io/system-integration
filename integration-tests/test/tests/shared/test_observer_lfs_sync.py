"""
Observer LFS-Sync Integration Test

Verifies that a fresh readonly observer can attach to a live, actively
producing shard, run its full Initializing → LFS-sync → Running
transition without consensus or storage errors, and remain consistent
with the existing nodes' view of the chain.

Real-world scenario: an operator brings up a new readonly node against a
running shard. The new node has no local rspace, no DAG, no blocks.
During Initializing it must:

  1. Pull every block from genesis to LFB (the LFS block requester).
  2. Pull the LFB rspace tuple-space chunk-by-chunk (the LFS tuple-space
     requester).
  3. Pull the mergeable-channels store entry per block
     (`MergeableEntryRequest`/`Response`).
  4. Pull the rspace history for every ancestor root reachable within
     `max_parent_depth + depth_buffer` of the LFB (the LFS forward-
     horizon requester) — including pre-state hashes for multi-parent
     merge intermediates that wouldn't otherwise reach the joiner.

While the observer is doing that, the validators keep producing blocks
under heartbeat. The observer must converge to the validators' LFB
within drift tolerance and agree with them on the bonds map, finalized
ancestor chain, and per-block post-state hashes.

Runs against ``shared_shard`` (3 validators + readonly + heartbeat=True).
The shared readonly was attached at genesis and so doesn't exercise the
"attach against live shard" code path; this test attaches a TRANSIENT
observer mid-session via ``add_observer`` (context-managed cleanup) so
the assertion target is the same code an operator would hit in
production.
"""
import logging
import threading
import time
from typing import List, Optional

import pytest

from ...infra.assertions import (
    assert_all_nodes_agree_on_block,
    assert_block_finalized_on_all_nodes,
    assert_bonds_map_consistent_across_nodes,
)
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import (
    all_blocks_visible,
    get_blocks_if_enough,
    poll_until,
    wait_for_block_visible,
)

pytestmark = pytest.mark.xdist_group("shared")

# Drift tolerance between observer's LFB and v1's LFB (in blocks).
# A readonly observer has no proposer back-pressure and ingests via
# gossip; with heartbeat-driven validators producing in parallel,
# steady-state drift is typically 3-7 blocks.
_LFB_DRIFT_TOLERANCE = 10

# Minimum DAG depth on v1 before we attach the observer. This guarantees
# the forward-horizon sync has non-trivial work — without depth the
# horizon collapses to ~genesis and the new code paths emit early.
_MIN_PRE_ATTACH_DEPTH = 12

# Background load cadence per producer. With 3 producers round-robin,
# effective shard-wide cadence is ~3× this. Calibrated to keep the DAG
# advancing during the observer attach window without saturating the
# horizon density (which can stretch sync past timeout).
_BG_LOAD_INTERVAL = 2.5
_BG_LOAD_PHLO_LIMIT = 100_000_000

# How long to keep load running after observer attaches before stopping
# and asserting drift convergence. Enough to give the observer a chance
# to cross from Initializing through Running and ingest several gossip
# rounds.
_OBSERVER_LOAD_WINDOW_SEC = 20.0


class _BackgroundLoad:
    """Round-robin deploy generator — drives the chain forward during
    observer attach so the LFS sync chases a moving LFB.

    Local copy of the pattern used by ``test_bonding_validators._BackgroundLoad``
    rather than a shared infra import: each test calibrates cadence and
    failure tolerance differently. Keeping the class local makes those
    knobs explicit at the call site.
    """

    def __init__(
        self,
        producers: List,
        identities: List,
        interval: float = _BG_LOAD_INTERVAL,
    ) -> None:
        if len(producers) != len(identities):
            raise ValueError("producers and identities must be same length")
        self._producers = producers
        self._identities = identities
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._counter = 0
        self._errors = 0

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("BackgroundLoad already started")
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="obs-lfs-bg-load",
        )
        self._thread.start()

    def stop(self, join_timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=join_timeout)
        logging.info(
            "Background load stopped: %d deploys sent, %d errors",
            self._counter, self._errors,
        )
        self._thread = None

    @property
    def deploy_count(self) -> int:
        return self._counter

    def _run(self) -> None:
        while not self._stop.is_set():
            idx = self._counter % len(self._producers)
            node = self._producers[idx]
            identity = self._identities[idx]
            try:
                node.deploy_string(
                    f'@"obs-lfs-bg-{self._counter}"!({self._counter})',
                    identity.private_key(),
                    phlo_limit=_BG_LOAD_PHLO_LIMIT,
                    phlo_price=1,
                )
            except Exception as e:
                self._errors += 1
                logging.warning(
                    "Background load deploy %d failed on %s: %s",
                    self._counter, node.name, e,
                )
            self._counter += 1
            self._stop.wait(self._interval)


def _walk_finalized_chain(node, lfb_hash: str, max_blocks: int = 20) -> List[str]:
    """Walk back from `lfb_hash` along the main parent chain.

    Returns up to `max_blocks` finalized block hashes (most-recent first).
    Used to verify deeper cross-node agreement than just the LFB.
    """
    hashes: List[str] = []
    cur = lfb_hash
    while cur and len(hashes) < max_blocks:
        hashes.append(cur)
        block = node.get_block(cur)
        parents = list(block.blockInfo.parentsHashList)
        cur = parents[0] if parents else None
    return hashes


def test_observer_lfs_sync_against_active_shard(shared_shard, timeouts) -> None:
    """A fresh observer LFS-syncs cleanly against an actively producing shard.

    1. Wait until the shard's DAG has real depth (≥ _MIN_PRE_ATTACH_DEPTH
       blocks) so the forward-horizon sync has non-trivial work.
    2. Start background load on V1/V2/V3 to keep the chain advancing.
    3. Attach a transient observer; observer runs full LFS-sync against
       a moving LFB.
    4. Wait for observer's LFB to converge within drift tolerance of v1.
    5. Stop background load and let the chain settle.
    6. Assert robust cross-node agreement on:
       - the observer's LFB block (visible + finalized everywhere)
       - the bonds map at the observer's LFB
       - per-block post-state hashes for the last several finalized
         ancestor blocks (deep agreement, not just LFB-tip)
       - the DAG had multi-parent merges in the depth window the
         observer had to sync (proves the test exercised the real-world
         code path, not a degenerate single-parent chain)
    7. Re-assert drift remains within tolerance after a settle window
       (catches "synced then stalled" regressions).

    The autouse log scanner in ``conftest.check_node_logs_after_test``
    fails this test if the observer (or any other node) emitted
    ``DAGStorageMissingHash``, ``RootRepositoryDivergence``,
    ``KvStoreError``, ``UnknownRootError``, or any other forbidden
    pattern during sync.
    """
    v1 = shared_shard.node("validator1")
    v2 = shared_shard.node("validator2")
    v3 = shared_shard.node("validator3")
    validators = [v1, v2, v3]
    keys = [VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID]

    # ── 1. Pre-attach DAG depth ────────────────────────────────────────
    # The shared shard has been running with heartbeat since session
    # startup, so the DAG should already have depth. Poll to be sure
    # before we start measuring.
    poll_until(
        predicate=lambda: get_blocks_if_enough(v1, _MIN_PRE_ATTACH_DEPTH),
        timeout=timeouts.finalization * 4,
        interval=3.0,
        description=(
            f"v1 accumulates ≥ {_MIN_PRE_ATTACH_DEPTH} blocks pre-attach"
        ),
    )
    pre_attach_lfb_n = v1.last_finalized_block().blockInfo.blockNumber
    logging.info(
        "Pre-attach: v1 LFB at #%d, %d+ blocks in DAG",
        pre_attach_lfb_n, _MIN_PRE_ATTACH_DEPTH,
    )

    # ── 2. Background load drives DAG forward during attach ───────────
    bg = _BackgroundLoad(validators, keys)
    bg.start()
    try:
        # ── 3. Attach transient observer ──────────────────────────────
        # ``add_observer`` is the context-managed (transient) variant —
        # observer is removed and its volume cleaned up on exit, so its
        # post-attach errors (if any) are still visible to the autouse
        # log scanner that runs at test end.
        with shared_shard.add_observer() as observer:
            logging.info(
                "Observer %s attached during bg load (~%d deploys so far)",
                observer.name, bg.deploy_count,
            )

            # ── 4. Wait for observer to catch up ──────────────────────
            def _observer_caught_up() -> bool:
                v1_n = v1.last_finalized_block().blockInfo.blockNumber
                obs_n = observer.last_finalized_block().blockInfo.blockNumber
                return abs(v1_n - obs_n) <= _LFB_DRIFT_TOLERANCE

            poll_until(
                _observer_caught_up,
                timeout=timeouts.finalization * 6,
                interval=3.0,
                description=(
                    f"observer LFB within {_LFB_DRIFT_TOLERANCE} blocks "
                    f"of v1 LFB"
                ),
            )

            # Let load continue briefly so observer has to ingest via
            # gossip after sync — confirms gossip path works post-LFS,
            # not just the bulk sync.
            time.sleep(_OBSERVER_LOAD_WINDOW_SEC)

            # ── 5. Stop load + settle ─────────────────────────────────
            bg.stop()

            # Allow the chain to quiesce so observer can fully catch up
            # (validators aren't producing anymore once load stops + no
            # pending mempool, but heartbeat may emit one or two more
            # blocks before going idle).
            def _drift_within_tolerance() -> bool:
                o = observer.last_finalized_block().blockInfo.blockNumber
                v = v1.last_finalized_block().blockInfo.blockNumber
                return abs(v - o) <= _LFB_DRIFT_TOLERANCE

            poll_until(
                _drift_within_tolerance,
                timeout=timeouts.finalization * 4,
                interval=3.0,
                description=(
                    f"observer LFB drift converges to within "
                    f"{_LFB_DRIFT_TOLERANCE} blocks of v1 (post-settle)"
                ),
            )

            # ── 6. Robust cross-node assertions ───────────────────────
            observer_lfb = observer.last_finalized_block().blockInfo
            observer_bonds = {
                b.validator: b.stake for b in observer_lfb.bonds
            }
            assert len(observer_bonds) == len(validators), (
                f"Observer LFB #{observer_lfb.blockNumber} bonds map has "
                f"{len(observer_bonds)} entries, expected {len(validators)}: "
                f"{sorted(observer_bonds)}"
            )
            for ident in keys:
                assert ident.public_hex in observer_bonds, (
                    f"Observer LFB missing {ident.name} from bonds map: "
                    f"{sorted(observer_bonds)}"
                )

            # v1 must hold observer's LFB block too — covers cross-node
            # block propagation, not just LFB advancement.
            wait_for_block_visible(
                v1, observer_lfb.blockHash,
                timeout=timeouts.finalization * 2,
            )
            assert_block_finalized_on_all_nodes(
                [v1, observer], observer_lfb.blockHash,
                timeout=timeouts.finalization * 3,
            )
            assert_bonds_map_consistent_across_nodes(
                [v1, v2, v3, observer],
                observer_lfb.blockHash,
                observer_bonds,
            )

            # Deep agreement: walk back several blocks along observer's
            # finalized ancestor chain and verify every validator + the
            # observer agree on each one's post-state. Catches latent
            # storage/replay divergence that wouldn't surface from
            # tip-only checks.
            ancestor_chain = _walk_finalized_chain(
                observer, observer_lfb.blockHash, max_blocks=10,
            )
            assert len(ancestor_chain) >= 5, (
                f"Observer ancestor chain only {len(ancestor_chain)} "
                f"blocks deep; need ≥ 5 for deep agreement check"
            )
            poll_until(
                predicate=lambda: all_blocks_visible(
                    [v1, v2, v3, observer], ancestor_chain,
                ),
                timeout=timeouts.finalization * 2,
                interval=3.0,
                description=(
                    f"observer's last {len(ancestor_chain)} finalized "
                    f"blocks visible on all validators"
                ),
            )
            for block_hash in ancestor_chain:
                assert_all_nodes_agree_on_block(
                    [v1, v2, v3, observer], block_hash,
                )

            # Multi-parent existence check: the test only exercises the
            # forward-horizon's pre-state inclusion path if the synced
            # window had multi-parent merges. With heartbeat=True + 3
            # validators + bg load this is reliably satisfied; assert it
            # so a future degenerate config (e.g. single-parent shard)
            # would loudly fail rather than silently turn into a thinner
            # test.
            recent_blocks = v1.get_blocks(_MIN_PRE_ATTACH_DEPTH * 2)
            multi_parent_count = sum(
                1 for b in recent_blocks if len(b.parentsHashList) > 1
            )
            assert multi_parent_count > 0, (
                f"No multi-parent blocks in last {len(recent_blocks)} "
                f"blocks — observer didn't exercise pre-state inclusion "
                f"path; bg-load cadence or heartbeat config has "
                f"degenerated"
            )

            logging.info(
                "Observer %s LFS-synced: LFB #%d (v1 #%d, drift %d), "
                "bonds=%d, ancestor-chain-agreement=%d blocks, "
                "multi-parent blocks=%d/%d",
                observer.name,
                observer_lfb.blockNumber,
                v1.last_finalized_block().blockInfo.blockNumber,
                v1.last_finalized_block().blockInfo.blockNumber
                - observer_lfb.blockNumber,
                len(observer_bonds),
                len(ancestor_chain),
                multi_parent_count, len(recent_blocks),
            )
    finally:
        bg.stop()

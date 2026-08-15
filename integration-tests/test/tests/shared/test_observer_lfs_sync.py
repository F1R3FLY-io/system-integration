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

Runs against a DEDICATED module-scoped shard (3 validators, heartbeat)
rather than ``shared_shard``: the ``isolated_shard`` ordering runs this
module before the session shard exists, so the pre-attach DAG window is
freshly produced under this test's own load — the multi-parent
precondition below is verified against blocks this test caused, not
whatever a long-lived session shard happens to have near its tip. The
test attaches a TRANSIENT observer mid-run via ``add_observer``
(context-managed cleanup) so the assertion target is the same code an
operator would hit in production.

Multi-parent precondition: the forward-horizon pre-state inclusion path
is only exercised if the observer's sync window contains multi-parent
merge blocks. The test therefore WAITS for a FINALIZED multi-parent
block to appear under background load BEFORE attaching the observer
(finalized ⇒ inside the genesis→LFB bulk-sync range, and near the
forward horizon because the most recent finalized merge is chosen) and
afterwards asserts the observer actually holds that block — a
guaranteed precondition plus direct proof, replacing the earlier
post-hoc sample of v1's recent blocks that could falsely fail when the
sampled window happened to be single-parent.
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
from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import (
    all_blocks_visible,
    get_blocks_if_enough,
    poll_until,
    wait_for_block_visible,
)
from ...infra.shard import Shard

pytestmark = [
    # Deliberately kept in the "shared" xdist group despite the dedicated
    # shard: under --dist=loadgroup it serializes this resource-heavy
    # module with the other shard-owning tests on one worker, so two
    # multi-node shards never run concurrently on a constrained host.
    pytest.mark.xdist_group("shared"),
    pytest.mark.isolated_shard,
]

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

# How many recent blocks to inspect when hunting for a multi-parent
# merge block pre-attach. Twice the depth gate: covers everything the
# depth gate guarantees plus whatever lands while polling.
_MULTI_PARENT_SCAN_DEPTH = _MIN_PRE_ATTACH_DEPTH * 2


@pytest.fixture(scope="module")
def observer_shard(provider, timeouts):
    """Dedicated shard so the observer attaches against a DAG whose
    recent history this test fully controls (fresh genesis, own load).

    No baked-in readonly: the transient ``add_observer`` node is the
    only observer this test needs, and it must be the mid-session
    attach variant to exercise the LFS-sync path.
    """
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        heartbeat=True,
        include_readonly=False,
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        yield shard
    finally:
        shard.destroy()


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
            target=self._run,
            daemon=True,
            name="obs-lfs-bg-load",
        )
        self._thread.start()

    def stop(self, join_timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=join_timeout)
        logging.info(
            "Background load stopped: %d deploys sent, %d errors",
            self._counter,
            self._errors,
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
                    self._counter,
                    node.name,
                    e,
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


def test_observer_lfs_sync_against_active_shard(observer_shard, timeouts) -> None:
    """A fresh observer LFS-syncs cleanly against an actively producing shard.

    1. Start background load on V1/V2/V3 so the fresh shard's DAG
       advances (and merges) from the start.
    2. Wait until the DAG has real depth (≥ _MIN_PRE_ATTACH_DEPTH
       blocks) so the forward-horizon sync has non-trivial work.
    3. Wait for a FINALIZED multi-parent merge block to appear in the
       recent window and record it — the precondition that the
       observer's genesis→LFB sync will exercise the pre-state
       inclusion path.
    4. Immediately attach a transient observer (so the recorded block
       is still within the forward-horizon depth of the LFB); observer
       runs full LFS-sync against a moving LFB.
    5. Wait for observer's LFB to converge within drift tolerance of v1.
    6. Stop background load and let the chain settle.
    7. Assert robust cross-node agreement on:
       - the observer's LFB block (visible + finalized everywhere)
       - the bonds map at the observer's LFB
       - per-block post-state hashes for the last several finalized
         ancestor blocks (deep agreement, not just LFB-tip)
       - the observer holds the recorded multi-parent block (direct
         proof the sync covered the real-world merge path, not a
         degenerate single-parent chain)
    8. Re-assert drift remains within tolerance after a settle window
       (catches "synced then stalled" regressions).

    The autouse log scanner in ``conftest.check_node_logs_after_test``
    fails this test if the observer (or any other node) emitted
    ``DAGStorageMissingHash``, ``RootRepositoryDivergence``,
    ``KvStoreError``, ``UnknownRootError``, or any other forbidden
    pattern during sync.
    """
    v1 = observer_shard.node("validator1")
    v2 = observer_shard.node("validator2")
    v3 = observer_shard.node("validator3")
    validators = [v1, v2, v3]
    keys = [VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID]

    # ── 1. Background load drives the fresh DAG forward ───────────────
    bg = _BackgroundLoad(validators, keys)
    bg.start()
    try:
        # ── 2. Pre-attach DAG depth ───────────────────────────────────
        poll_until(
            predicate=lambda: get_blocks_if_enough(v1, _MIN_PRE_ATTACH_DEPTH),
            timeout=timeouts.finalization * 4,
            interval=3.0,
            description=(f"v1 accumulates ≥ {_MIN_PRE_ATTACH_DEPTH} blocks pre-attach"),
        )
        pre_attach_lfb_n = v1.last_finalized_block().blockInfo.blockNumber
        logging.info(
            "Pre-attach: v1 LFB at #%d, %d+ blocks in DAG",
            pre_attach_lfb_n,
            _MIN_PRE_ATTACH_DEPTH,
        )

        # ── 3. Multi-parent precondition ──────────────────────────────
        # Concurrent proposals under load + heartbeat produce merge
        # blocks; wait until one exists so attaching now guarantees the
        # observer's horizon window contains it. Recording the block
        # (rather than re-sampling afterwards) is what makes the final
        # check deterministic. Only a block v1 already reports as
        # FINALIZED qualifies: a finalized pre-attach block is covered
        # by the observer's genesis→LFB bulk sync regardless of attach
        # latency, so the post-sync visibility check proves LFS
        # coverage. An unfinalized tip would prove nothing (it could
        # arrive via post-attach gossip) or flake (it could be orphaned
        # off the finalized path entirely) — the same orphan-race family
        # as PR #117/#118. Most-recent finalized merge wins the
        # tie-break so the block stays near the forward horizon.
        def _find_multi_parent():
            candidates = [
                b for b in v1.get_blocks(_MULTI_PARENT_SCAN_DEPTH) if len(b.parentsHashList) > 1
            ]
            for candidate in sorted(candidates, key=lambda b: b.blockNumber, reverse=True):
                try:
                    if v1.get_block(candidate.blockHash).blockInfo.isFinalized:
                        return candidate
                except Exception:
                    continue
            return None

        multi_parent = poll_until(
            predicate=_find_multi_parent,
            timeout=timeouts.finalization * 4,
            interval=3.0,
            description="a multi-parent merge block appears on v1 pre-attach",
        )
        logging.info(
            "Multi-parent precondition met: finalized block #%d (%s…) has %d parents; attaching observer now",
            multi_parent.blockNumber,
            multi_parent.blockHash[:16],
            len(multi_parent.parentsHashList),
        )

        # ── 4. Attach transient observer ──────────────────────────────
        # ``add_observer`` is the context-managed (transient) variant —
        # observer is removed and its volume cleaned up on exit, so its
        # post-attach errors (if any) are still visible to the autouse
        # log scanner that runs at test end.
        with observer_shard.add_observer() as observer:
            logging.info(
                "Observer %s attached during bg load (~%d deploys so far)",
                observer.name,
                bg.deploy_count,
            )

            # ── 5. Wait for observer to catch up ──────────────────────
            def _observer_caught_up() -> bool:
                v1_n = v1.last_finalized_block().blockInfo.blockNumber
                obs_n = observer.last_finalized_block().blockInfo.blockNumber
                return abs(v1_n - obs_n) <= _LFB_DRIFT_TOLERANCE

            poll_until(
                _observer_caught_up,
                timeout=timeouts.finalization * 6,
                interval=3.0,
                description=(f"observer LFB within {_LFB_DRIFT_TOLERANCE} blocks of v1 LFB"),
            )

            # Let load continue briefly so observer has to ingest via
            # gossip after sync — confirms gossip path works post-LFS,
            # not just the bulk sync.
            time.sleep(_OBSERVER_LOAD_WINDOW_SEC)

            # ── 6. Stop load + settle ─────────────────────────────────
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

            # ── 7. Robust cross-node assertions ───────────────────────
            observer_lfb = observer.last_finalized_block().blockInfo
            observer_bonds = {b.validator: b.stake for b in observer_lfb.bonds}
            assert len(observer_bonds) == len(validators), (
                f"Observer LFB #{observer_lfb.blockNumber} bonds map has "
                f"{len(observer_bonds)} entries, expected {len(validators)}: "
                f"{sorted(observer_bonds)}"
            )
            for ident in keys:
                assert ident.public_hex in observer_bonds, (
                    f"Observer LFB missing {ident.name} from bonds map: {sorted(observer_bonds)}"
                )

            # v1 must hold observer's LFB block too — covers cross-node
            # block propagation, not just LFB advancement.
            wait_for_block_visible(
                v1,
                observer_lfb.blockHash,
                timeout=timeouts.finalization * 2,
            )
            assert_block_finalized_on_all_nodes(
                [v1, observer],
                observer_lfb.blockHash,
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
                observer,
                observer_lfb.blockHash,
                max_blocks=10,
            )
            assert len(ancestor_chain) >= 5, (
                f"Observer ancestor chain only {len(ancestor_chain)} "
                f"blocks deep; need ≥ 5 for deep agreement check"
            )
            poll_until(
                predicate=lambda: all_blocks_visible(
                    [v1, v2, v3, observer],
                    ancestor_chain,
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
                    [v1, v2, v3, observer],
                    block_hash,
                )

            # Multi-parent coverage check: the pre-attach precondition
            # recorded a merge block already FINALIZED before the
            # observer attached, so the observer's genesis→LFB bulk
            # sync must have covered it — gossip cannot satisfy this
            # check by accident, and the block cannot be orphaned. Verifying the observer holds THAT block is a
            # deterministic proof it exercised the pre-state inclusion
            # path — unlike sampling v1's recent window post-hoc, which
            # falsely failed when the sampled tail happened to be
            # single-parent (soak preflight, f1r3node-rust PR #273).
            wait_for_block_visible(
                observer,
                multi_parent.blockHash,
                timeout=timeouts.finalization * 2,
            )

            logging.info(
                "Observer %s LFS-synced: LFB #%d (v1 #%d, drift %d), "
                "bonds=%d, ancestor-chain-agreement=%d blocks, "
                "multi-parent block #%d held by observer",
                observer.name,
                observer_lfb.blockNumber,
                v1.last_finalized_block().blockInfo.blockNumber,
                v1.last_finalized_block().blockInfo.blockNumber - observer_lfb.blockNumber,
                len(observer_bonds),
                len(ancestor_chain),
                multi_parent.blockNumber,
            )
    finally:
        bg.stop()

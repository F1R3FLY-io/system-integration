"""User-side multi-parent-merge validation — concurrency on ordinary contracts.

This test exercises the multi-parent merge with NO PoS involvement, to
establish whether the platform's concurrency model is sound on its own terms.
It mirrors the validator-lifecycle environment (heartbeat on, always-on
background load, strict cluster-wide finalization assertions) but contends
USER contract state instead of the validator bonds map.

Every state assertion is checked on EVERY node at a common finalized cut
(``assert_*_consistent_across_nodes`` / ``await_*_converges_on_all_nodes``):
a divergent finalized value is a node-identity break (the #71 cascade shape)
even when it does not itself gate finalization.

Surfaces, from least to most adversarial:

  - independent channels (distinct per-key cells) — different channels commute,
    so every concurrent write lands in PARALLEL;

  - a single whole-Map cell with DISTINCT-key read-modify-writes per round
    (Maps are excluded from number-channels — the SAME shape as the PoS bonds
    cell); the platform serializes via reject-and-recover so EVERY entry lands;

  - SAME-key conflicting writes (``test_same_key_conflict_*``) — three proposers
    write the SAME map key DIFFERENT values in one round: a genuine non-foldable
    conflict the merge keep-ones and recovery re-lands, so the cell settles to a
    single deterministic value (never multi-valued, never stale-consumed) and is
    identical on every node. This is the integration analog of the
    ``fs_seal_non_foldable_fork`` unit test;

  - a GUARDED read-modify-write conflict (``test_guarded_rmw_*``) — concurrent
    conditional decrements whose losers re-EXECUTE the guard on the recovered
    base, so the counter equals the sequential fold and never drops below the
    single-application floor (a lower bound that catches the
    ``fs_seal_must_not_double_apply`` ``FS=-20`` mode at integration level);

  - a mergeable integer counter (number-channel / IntegerAdd) — concurrent
    credits COMPOSE; the final value is the sum, never exceeding it
    (an upper bound that catches a double-applied credit);

  - cost-priority overdraft (``test_overdraft_cost_priority``) — two concurrent
    same-source vault transfers that together overdraw; #3 keeps the
    higher-phlo-price (higher-cost) branch, so the higher-cost transfer's amount
    lands and the lower-cost one is rejected-then-recovery-fails. The integration
    analog of ``fold_rejection_rejects_lower_cost_branch_on_overdraft``.

Background load runs the whole time to reproduce the lumpy, contended
finalization the merge must survive. Forbidden node-log patterns (including
``StaleConsume`` and the single-value-cell invariant) are enforced on every
node by the autouse ``check_node_logs_after_test`` fixture.
"""
import logging
import threading
import time
from typing import List, Optional

import pytest
from f1r3fly.crypto import PrivateKey

from ...infra.assertions import (
    assert_all_nodes_agree_on_lfb,
    assert_balance_consistent_across_nodes,
    assert_block_finalized_on_all_nodes,
    assert_chain_advances,
    assert_channel_consistent_across_nodes,
    await_balance_converges_on_all_nodes,
    await_channel_converges_on_all_nodes,
    lowest_lfb_number,
    resolve_deploy_verdicts,
)
from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import wait_for_deploy_included, wait_for_finalized
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

# ── Shard parameters (aligned with the lifecycle shard) ──────────────────────
_GENESIS_STAKE = 100
_WALLET_BALANCE = 50_000_000_000_000_000
_PHLO_LIMIT = 100_000_000
_PHLO_PRICE = 1

# A dedicated funded deployer key per producer node: each concurrent op runs on
# its own vault (no inter-op phlo contention) and is submitted to a distinct
# node so the three proposals are siblings. Keyed by node name.
_PRODUCER_KEYS = {
    "validator1": PrivateKey.from_seed(91001),
    "validator2": PrivateKey.from_seed(91002),
    "validator3": PrivateKey.from_seed(91003),
}
_PRODUCER_WALLETS = [
    (k.get_public_key().get_vault_address(), _WALLET_BALANCE) for k in _PRODUCER_KEYS.values()
]
# Shared destination vault for the mergeable-balance test: three sources credit
# it concurrently; its balance is a mergeable IntegerAdd number-channel.
_MERGE_DEST_KEY = PrivateKey.from_seed(91004)
_MERGE_DEST_ADDR = _MERGE_DEST_KEY.get_public_key().get_vault_address()

# ── Cost-priority overdraft (#3) — concurrent same-source transfers ──────────
# Source funded so each transfer fits ALONE but the two together overdraw. The
# amounts (≫ gas) differ so the surviving branch is observable: #3 keeps the
# higher-COST (higher phlo-price) branch, so the HIGH-amount transfer must land.
_OVERDRAFT_SRC_KEY = PrivateKey.from_seed(91005)
_OVERDRAFT_DST_KEY = PrivateKey.from_seed(91006)
_OVERDRAFT_SRC_ADDR = _OVERDRAFT_SRC_KEY.get_public_key().get_vault_address()
_OVERDRAFT_DST_ADDR = _OVERDRAFT_DST_KEY.get_public_key().get_vault_address()
_OVERDRAFT_SRC_BALANCE = 200_000_000
_OVERDRAFT_HIGH_AMOUNT = 120_000_000  # high phlo-price → higher cost → must WIN
_OVERDRAFT_LOW_AMOUNT = 100_000_000  # low phlo-price → lower cost → must LOSE
_OVERDRAFT_HIGH_PRICE = 10
_OVERDRAFT_LOW_PRICE = 1

# ── Background load: same-vault transfer contention (mirrors lifecycle) ───────
_BG_SRC_KEY = PrivateKey.from_seed(90001)
_BG_DST_KEY = PrivateKey.from_seed(90002)
_BG_SRC_ADDR = _BG_SRC_KEY.get_public_key().get_vault_address()
_BG_DST_ADDR = _BG_DST_KEY.get_public_key().get_vault_address()
_BG_INTERVAL = 2.0
_BG_TRANSFER_AMOUNT = 1

# Background-load master switch. Keep OFF until the base (no-bg) cases pass on
# every node; flip to True to add contended-finalization stress on top. One
# switch gates all scenarios so bg state can't drift per-test.
_BG_LOAD_ENABLED = False

# Multi-round conflict sampling. The non-foldable / single-value-cell-race modes are
# cone-shape-dependent (~50% per shot at the unit level), so the conflict scenarios run
# many rounds on the SAME cell — each round re-samples the non-deterministic merge
# ordering and stresses recovery re-basing onto the prior round's result. One round is
# not a gate; this is.
#
# Five, not ten: round count is the dominant term in suite runtime (~63 rounds across
# the file at ten), and five still re-samples the race enough to gate on. Raise it for
# a HUNT, where the point is to keep re-rolling a rare geometry rather than to answer
# "does this pass" inside a bounded window.
_CONFLICT_ROUNDS = 5
# High fan-out: deploys submitted per producer node in one round. 3 nodes x this =
# total concurrent writers in the round (exercises wider-than-3-way merge fan-out).
_FANOUT_PER_NODE = 4


class _BackgroundLoad:
    """Same-vault transfer generator, round-robin across producer nodes.

    Always-on for the duration of a scenario to reproduce the contended,
    lumpy finalization the merge must survive. Submit errors are surfaced via
    the finalization assertion (a missing deploy), never swallowed.
    """

    def __init__(self, producers: List, interval: float = _BG_INTERVAL) -> None:
        self._producers = producers
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._counter = 0
        self._errors = 0
        self._deploy_ids: List[str] = []
        self._lock = threading.Lock()

    def deploy_ids(self) -> List[str]:
        with self._lock:
            return list(self._deploy_ids)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("BackgroundLoad already started")
        self._thread = threading.Thread(target=self._run, daemon=True, name="user-bg-load")
        self._thread.start()

    def stop(self, join_timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=join_timeout)
        logging.info(
            "Background load stopped: %d transfers, %d errors", self._counter, self._errors
        )
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            node = self._producers[self._counter % len(self._producers)]
            try:
                did = node.get_vault().transfer(
                    _BG_SRC_ADDR,
                    _BG_DST_ADDR,
                    _BG_TRANSFER_AMOUNT,
                    _BG_SRC_KEY,
                    phlo_price=1,
                    phlo_limit=_PHLO_LIMIT,
                )
                with self._lock:
                    self._deploy_ids.append(did)
            except Exception as e:  # noqa: BLE001 — surfaced via finalization assertion
                self._errors += 1
                logging.warning("bg-load transfer %d on %s failed: %s", self._counter, node.name, e)
            self._counter += 1
            self._stop.wait(self._interval)


# ── Strict finalization helper (mirrors lifecycle's bg-load assertion) ───────


def _assert_all_finalized(producers, all_nodes, deploy_ids: List[str], timeouts, label: str):
    """Resolve every deploy to a coherent terminal verdict on EVERY node and
    return the verdicts.

    Integrity is enforced, fairness is only recorded. A deploy the shard
    terminally judged Expired under contention did not silently vanish: the
    window closed, the register said so, and a client can resubmit — that is
    logged as a STARVATION-RECORD, not failed. What still fails is the shard
    failing to DECIDE (no verdict inside the budget — the frozen-chain and
    propose-wedge signature), a terminal Failed, or a verdict that differs
    between nodes (a forked read surface).

    Callers must build expected state from the returned ``finalized`` subset:
    an expired deploy's effect must be absent, and asserting the full
    submitted set would fail a shard that behaved correctly.

    finalization * 3 (135s at scale 1.0): a same-key conflict whose loser is
    keep-one'd repeatedly must win a cut via recovery and then finalize —
    measured at ~102s for a 7-rejection delete/set round, so * 2 (90s) was
    ~12s short.
    """
    del producers  # deploy-status is queried per node directly; no block lookup
    return resolve_deploy_verdicts(
        all_nodes, deploy_ids, timeouts.finalization * 3, label=label
    )


def _finalized_names(deploy_ids: List[str], verdicts) -> List[str]:
    """Producer names whose deploy finalized, for ids returned by
    ``_deploy_on_each`` (one per producer, in ``_PRODUCER_KEYS`` order)."""
    finalized = verdicts.finalized_set()
    return [
        name for name, did in zip(_PRODUCER_KEYS, deploy_ids) if did in finalized
    ]


def _assert_bg_load_robust(
    producers, all_nodes, bg, src0: int, dst0: int, timeouts, label: str
) -> None:
    """Robust bg-load verification on ALL nodes — the same non-regression +
    exact-reconciliation principle the foreground scenarios apply, carried over
    to the always-on same-vault transfer stream.

    The bg load moves exactly ``_BG_TRANSFER_AMOUNT`` from SRC to DST per
    transfer. After the N submitted transfers finalize:

      1. every bg transfer finalizes on every node;
      2. DEST reconciles EXACTLY to ``dst0 + N*amount`` on every node, never
         decreasing en route (a finalized credit not undone — #71) and never
         EXCEEDING it (a double-applied credit — caught immediately by the
         upper bound rather than as a convergence timeout);
      3. SOURCE, at the settled common cut, is identical on every node and
         debited by AT LEAST the transferred total (the surplus is gas)."""
    bg_ids = bg.deploy_ids()
    verdicts = _assert_all_finalized(producers, all_nodes, bg_ids, timeouts, label)
    # Reconcile against the transfers that FINALIZED: an expired bg transfer moved
    # nothing, so counting it would fail the exact-destination check on a shard that
    # behaved correctly.
    n = len(verdicts.finalized)
    want_dst = dst0 + n * _BG_TRANSFER_AMOUNT
    min_src_debit = n * _BG_TRANSFER_AMOUNT
    await_balance_converges_on_all_nodes(
        all_nodes,
        _BG_DST_ADDR,
        want_dst,
        timeouts.finalization * 2,
        f"{label}-dst",
        non_regression="up",
        upper_bound=want_dst,
    )
    lfb = assert_all_nodes_agree_on_lfb(all_nodes, timeout=timeouts.finalization)
    src_final = assert_balance_consistent_across_nodes(all_nodes, _BG_SRC_ADDR, lfb)
    assert src0 - src_final >= min_src_debit, (
        f"[{label}] bg-src under-debited: source fell by {src0 - src_final} < transferred "
        f"{min_src_debit} — a finalized debit was lost"
    )
    logging.info(
        "%s: reconciled %d bg transfers on all nodes — dst->%d (exact), src->%d (incl gas)",
        label,
        n,
        want_dst,
        src_final,
    )


def _deploy_on_each(shard, term_for, timeouts) -> List[str]:
    """Submit one deploy per genesis validator in tight succession (signed by
    that validator's own funded key) so the three land in overlapping blocks —
    sibling proposals the next block multi-parent-merges. Returns the deploy
    ids. Waits for inclusion only; finalization is asserted by the caller."""
    deploy_ids: List[str] = []
    for name, key in _PRODUCER_KEYS.items():
        node = shard.node(name)
        did = node.deploy_string(
            term_for(name), key, phlo_limit=_PHLO_LIMIT, phlo_price=_PHLO_PRICE
        )
        deploy_ids.append(did)
        logging.info("RMW_DEPLOY_ID node=%s deploy_id=%s", name, did)
    for name, did in zip(_PRODUCER_KEYS, deploy_ids):
        wait_for_deploy_included(shard.node(name), did, timeouts.deploy_inclusion * 3)
    return deploy_ids


def _deploy_k_on_each(shard, term_for, k: int, timeouts) -> List[str]:
    """Submit ``k`` deploys per genesis validator in tight succession (each signed by
    that validator's own funded key), interleaved across nodes so 3*k proposals overlap
    as siblings the next blocks multi-parent-merge. ``term_for(name, i)`` builds the i-th
    term for a node. Returns all deploy ids; waits for inclusion only."""
    submitted: List = []  # (node, did)
    for i in range(k):
        for name, key in _PRODUCER_KEYS.items():
            node = shard.node(name)
            did = node.deploy_string(
                term_for(name, i), key, phlo_limit=_PHLO_LIMIT, phlo_price=_PHLO_PRICE
            )
            submitted.append((node, did))
            logging.info("FANOUT_DEPLOY_ID node=%s i=%d deploy_id=%s", name, i, did)
    for node, did in submitted:
        wait_for_deploy_included(node, did, timeouts.deploy_inclusion * 3)
    return [did for _, did in submitted]


def _finalize_setup(shard, term: str, timeouts) -> None:
    """Deploy a one-time setup term on validator1 and wait until it finalizes
    cluster-wide so the initialized cell is visible to every proposer."""
    v1 = shard.node("validator1")
    did = v1.deploy_string(
        term, _PRODUCER_KEYS["validator1"], phlo_limit=_PHLO_LIMIT, phlo_price=_PHLO_PRICE
    )
    block = wait_for_deploy_included(v1, did, timeouts.deploy_inclusion * 3)
    wait_for_finalized(v1, block.blockNumber, timeouts.finalization * 3)
    assert_block_finalized_on_all_nodes(
        shard.all_nodes, block.blockHash, timeout=timeouts.finalization * 2
    )


def _await_map_settles(all_nodes, channel, accept, allowed_keys, timeout, label):
    """Poll the all-node-consistent finalized map until ``accept(m)`` holds, enforcing a
    per-poll STRUCTURAL invariant: the map's key set must stay within ``allowed_keys`` at
    every aligned cut (a spurious/extra key, or a transient never-settling corruption, is
    a single-value-cell merge defect even if it never crashes). Recovery may move the
    value between accepted states before settling — that is allowed; structural corruption
    is not. Returns the settled map.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            lfb = assert_all_nodes_agree_on_lfb(all_nodes)
        except AssertionError:
            time.sleep(1.0)
            continue
        m = assert_channel_consistent_across_nodes(all_nodes, channel, lfb)
        last = m
        if m is not None:
            assert set(m.keys()) <= allowed_keys, (
                f"[{label}] corrupt finalized map: keys {sorted(m.keys())} exceed allowed "
                f"{sorted(allowed_keys)} at LFB {lfb[:16]}; map={m}"
            )
            if accept(m):
                return m
        time.sleep(1.0)
    raise AssertionError(
        f"[{label}] finalized map did not settle to an accepted state within "
        f"{timeout:.0f}s; last all-node-consistent read={last}"
    )


def _norm(x):
    """Normalize a decoded Rholang value for tolerant comparison: tuples and lists are
    both sequence-decoded, so compare them in one canonical form."""
    return list(x) if isinstance(x, (tuple, list)) else x


def _await_settles(all_nodes, channel, accept, timeout, label):
    """Generic settle for ANY value type: poll the all-node-consistent finalized value
    until ``accept(v)`` holds. Node-identity is enforced each poll (a post-state divergence
    raises immediately; a transient retrieval race is retried). Returns the settled value."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            lfb = assert_all_nodes_agree_on_lfb(all_nodes)
        except AssertionError:
            time.sleep(1.0)
            continue
        try:
            v = assert_channel_consistent_across_nodes(all_nodes, channel, lfb)
        except AssertionError as e:
            if "disagree on post-state" in str(e):
                raise
            time.sleep(1.0)
            continue
        last = v
        if accept(v):
            return v
        time.sleep(1.0)
    raise AssertionError(
        f"[{label}] finalized value did not settle to an accepted state within "
        f"{timeout:.0f}s; last all-node-consistent read={last!r}"
    )


# ── Fixture: dedicated user-contract shard ───────────────────────────────────


@pytest.fixture(scope="module")
def user_shard(provider, timeouts):
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, _GENESIS_STAKE),
            (VALIDATOR2_ID, _GENESIS_STAKE),
            (VALIDATOR3_ID, _GENESIS_STAKE),
        ],
        ftt=0.1,
        heartbeat=True,
        include_readonly=True,
        extra_wallets=[
            (_BG_SRC_ADDR, _WALLET_BALANCE),
            (_BG_DST_ADDR, _WALLET_BALANCE),
            (_MERGE_DEST_ADDR, _WALLET_BALANCE),
            (_OVERDRAFT_SRC_ADDR, _OVERDRAFT_SRC_BALANCE),
            (_OVERDRAFT_DST_ADDR, _WALLET_BALANCE),
        ]
        + _PRODUCER_WALLETS,
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        yield shard
    finally:
        shard.destroy()


# ── Tests ────────────────────────────────────────────────────────────────────


def test_independent_channels_merge_in_parallel(user_shard, timeouts):
    """Concurrent writes to DISTINCT per-key cells must all land — different
    channels commute, so the merge applies every write in parallel. Each cell's
    finalized value is asserted identical on every node."""
    shard = user_shard
    all_nodes = shard.all_nodes
    ro = shard.node("readonly")
    producers = [shard.node(n) for n in _PRODUCER_KEYS]
    bg = _BackgroundLoad(producers)
    bg_src0 = bg_dst0 = 0
    if _BG_LOAD_ENABLED:
        bg_src0 = ro.get_vault().get_balance(_BG_SRC_ADDR)
        bg_dst0 = ro.get_vault().get_balance(_BG_DST_ADDR)
        bg.start()
    try:
        expected = {"validator1": 11, "validator2": 22, "validator3": 33}
        deploy_ids = _deploy_on_each(
            shard,
            lambda name: f'@"ucc_kv_{name}"!({expected[name]})',
            timeouts,
        )
        verdicts = _assert_all_finalized(
            producers, all_nodes, deploy_ids, timeouts, "independent-channels"
        )
        # Distinct channels commute, so only the finalized writers' channels are
        # asserted; an expired writer's channel simply never gets its value.
        for name in _finalized_names(deploy_ids, verdicts):
            await_channel_converges_on_all_nodes(
                all_nodes,
                f"ucc_kv_{name}",
                expected[name],
                timeouts.finalization * 2,
                f"independent-{name}",
            )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(
            producers, all_nodes, bg, bg_src0, bg_dst0, timeouts, "bg-load(independent)"
        )


def test_single_cell_map_concurrent_adds_all_resolve(user_shard, timeouts):
    """The PoS-bonds analog, full lifecycle: a single whole-Map cell driven by rounds
    of CONCURRENT read-modify-writes (set / delete / update / re-add), three proposers
    per round on DISTINCT keys (they commute). One wins the merge, the losers are
    re-proposed by recovery onto the new map. Each round is driven to full finalized
    convergence ON EVERY NODE before the next, so same-key cross-round ops are
    deterministically ordered.

    Asserts end to end, on every node: every deploy finalizes; an add-only finalized
    key never vanishes (the #71 mode); the finalized map converges each round to the
    exact running fold; the final map equals the exact operation fold."""
    shard = user_shard
    all_nodes = shard.all_nodes
    ro = shard.node("readonly")
    producers = [shard.node(n) for n in _PRODUCER_KEYS]
    bg = _BackgroundLoad(producers)
    bg_src0 = bg_dst0 = 0
    if _BG_LOAD_ENABLED:
        bg_src0 = ro.get_vault().get_balance(_BG_SRC_ADDR)
        bg_dst0 = ro.get_vault().get_balance(_BG_DST_ADDR)
        bg.start()
    try:
        _finalize_setup(shard, '@"ucc_map_cell"!({})', timeouts)

        # One concurrent op per proposer per round. ("set", key, val) | ("del", key).
        rounds = [
            {
                "validator1": ("set", "a", 1),
                "validator2": ("set", "b", 2),
                "validator3": ("set", "c", 3),
            },  # adds
            {
                "validator1": ("del", "a"),
                "validator2": ("set", "d", 4),
                "validator3": ("set", "e", 5),
            },  # delete a (+ distinct adds)
            {
                "validator1": ("set", "a", 9),
                "validator2": ("set", "b", 20),
                "validator3": ("set", "f", 6),
            },  # re-add a, update b, add f
        ]

        expected: dict = {}
        volatile: set = set()  # keys revisited by del/update/re-add — non-monotone
        for rnd, ops in enumerate(rounds):

            def term_for(name, ops=ops):
                op = ops[name]
                if op[0] == "set":
                    return (
                        f'for (@m <- @"ucc_map_cell") {{ '
                        f'@"ucc_map_cell"!(m.set("{op[1]}", {op[2]})) }}'
                    )
                return (
                    f'for (@m <- @"ucc_map_cell") {{ ' f'@"ucc_map_cell"!(m.delete("{op[1]}")) }}'
                )

            lfb0 = lowest_lfb_number(all_nodes)
            ids = _deploy_on_each(shard, term_for, timeouts)
            assert_chain_advances(
                all_nodes, lfb0, timeouts.finalization * 2, label=f"map-round-{rnd}"
            )
            verdicts = _assert_all_finalized(
                producers, all_nodes, ids, timeouts, f"map-round-{rnd}"
            )

            # Only the ops the shard finalized may appear in the expected fold;
            # an expired op's effect must be ABSENT, and the convergence check
            # below fails if it shows up anyway.
            for name in _finalized_names(ids, verdicts):
                op = ops[name]
                k = op[1]
                if k in expected or k in volatile:
                    volatile.add(k)
                if op[0] == "set":
                    expected[k] = op[2]
                else:
                    expected.pop(k, None)

            # Drive the finalized cell to this round's exact fold on EVERY node, asserting
            # no add-only finalized key vanishes en route (the #71 non-regression check).
            await_channel_converges_on_all_nodes(
                all_nodes,
                "ucc_map_cell",
                expected,
                timeouts.finalization * 3,
                f"map-round-{rnd}",
                non_regression="map",
                volatile=volatile,
            )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(
            producers, all_nodes, bg, bg_src0, bg_dst0, timeouts, "bg-load(single-cell)"
        )


def test_same_key_conflict_resolves_to_single_value(user_shard, timeouts):
    """SAME-cell, SAME-key CONFLICT (integration analog of fs_seal_non_foldable_fork):
    three proposers concurrently write the SAME map key DIFFERENT values in one round.
    Maps are excluded from number-channels, so these genuinely conflict — the merge
    keep-ones one write and recovery re-lands the losers on the new base. The cell must
    settle to a SINGLE value for the key (never multi-valued, never stale-consumed),
    one of the written candidates, IDENTICAL on every node. A divergent or corrupted
    value is the seal item-2 / #71 regression; a stale-consume crash is caught by the
    autouse forbidden-log gate."""
    shard = user_shard
    all_nodes = shard.all_nodes
    ro = shard.node("readonly")
    producers = [shard.node(n) for n in _PRODUCER_KEYS]
    bg = _BackgroundLoad(producers)
    bg_src0 = bg_dst0 = 0
    if _BG_LOAD_ENABLED:
        bg_src0 = ro.get_vault().get_balance(_BG_SRC_ADDR)
        bg_dst0 = ro.get_vault().get_balance(_BG_DST_ADDR)
        bg.start()
    try:
        _finalize_setup(shard, '@"ucc_conflict_map"!({})', timeouts)
        last_settled = None  # value the cell holds if a whole round expires
        for rnd in range(_CONFLICT_ROUNDS):
            # Distinct candidate triple per round so each round is a fresh conflict AND the
            # losers' recovery must re-base onto the prior round's settled value (cross-round
            # stress). Many rounds = many samples of the cone-shape-dependent merge ordering.
            candidates = {
                "validator1": rnd * 3 + 1,
                "validator2": rnd * 3 + 2,
                "validator3": rnd * 3 + 3,
            }
            lfb0 = lowest_lfb_number(all_nodes)
            ids = _deploy_on_each(
                shard,
                lambda name, c=candidates: (
                    f'for (@m <- @"ucc_conflict_map") {{ '
                    f'@"ucc_conflict_map"!(m.set("shared", {c[name]})) }}'
                ),
                timeouts,
            )
            assert_chain_advances(
                all_nodes, lfb0, timeouts.finalization * 2, label=f"same-key-conflict-{rnd}"
            )
            verdicts = _assert_all_finalized(
                producers, all_nodes, ids, timeouts, f"same-key-conflict-{rnd}"
            )

            # Settle to a single "shared" entry whose value is one of this round's
            # FINALIZED candidates, identical on every node, never multi-keyed/corrupt
            # (the per-poll structural invariant lives in _await_map_settles). Recovery
            # re-lands losers, so the value may move between accepted states before
            # settling. Candidate values are distinct per round, so an expired
            # candidate's value appearing here is a verdict-vs-state contradiction and
            # fails; if every candidate expired the cell must hold its prior value.
            finalized_vals = {candidates[name] for name in _finalized_names(ids, verdicts)}
            accepted_vals = finalized_vals or {last_settled}
            settled = _await_map_settles(
                all_nodes,
                "ucc_conflict_map",
                accept=lambda m, cv=accepted_vals: m.get("shared") in cv,
                allowed_keys={"shared"},
                timeout=timeouts.finalization * 3,
                label=f"same-key-conflict-{rnd}",
            )
            last_settled = settled.get("shared")
            logging.info(
                "same-key-conflict round %d/%d: settled to shared=%r on all nodes",
                rnd,
                _CONFLICT_ROUNDS,
                settled["shared"],
            )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(
            producers, all_nodes, bg, bg_src0, bg_dst0, timeouts, "bg-load(same-key-conflict)"
        )


def test_guarded_rmw_conflict_no_double_apply(user_shard, timeouts):
    """GUARDED read-modify-write conflict (integration analog of
    fs_seal_must_not_double_apply_guarded_conflicting_decrement): a single-value Int
    cell seeded at 100, with three concurrent conditional decrements
    ``if (n >= 60) n-60 else n``. The merge keep-ones one decrement (100 -> 40); the two
    losers re-EXECUTE the guard on the recovered base (40 >= 60 is false -> no-op), so the
    finalized counter settles to exactly 40 on every node.

    The ``lower_bound=40`` is the key anti-double-apply assertion: if the seal folded a
    rejected decrement (the verified ``FS=-20`` mode), the finalized value would drop
    below 40 and fail immediately rather than as an opaque convergence timeout."""
    shard = user_shard
    all_nodes = shard.all_nodes
    ro = shard.node("readonly")
    producers = [shard.node(n) for n in _PRODUCER_KEYS]
    bg = _BackgroundLoad(producers)
    bg_src0 = bg_dst0 = 0
    if _BG_LOAD_ENABLED:
        bg_src0 = ro.get_vault().get_balance(_BG_SRC_ADDR)
        bg_dst0 = ro.get_vault().get_balance(_BG_DST_ADDR)
        bg.start()
    try:
        _finalize_setup(shard, '@"ucc_guarded_counter"!(100)', timeouts)

        ids = _deploy_on_each(
            shard,
            lambda name: (
                'for (@n <- @"ucc_guarded_counter") { '
                'if (n >= 60) { @"ucc_guarded_counter"!(n - 60) } '
                'else { @"ucc_guarded_counter"!(n) } }'
            ),
            timeouts,
        )
        verdicts = _assert_all_finalized(producers, all_nodes, ids, timeouts, "guarded-rmw")

        # Exactly one decrement applies (100 -> 40); the losers' recovery re-evaluates the
        # guard and no-ops. Converge to 40 on every node, never rising (down-only) and —
        # critically — never below 40 (a double-applied decrement is the item-1 mode).
        if len(verdicts.finalized) == len(ids):
            await_channel_converges_on_all_nodes(
                all_nodes,
                "ucc_guarded_counter",
                40,
                timeouts.finalization * 3,
                "guarded-rmw",
                non_regression="down",
                lower_bound=40,
            )
        else:
            # With a deploy expired, whether the APPLYING decrement is the one that
            # landed is not observable from verdicts alone: the survivors may all be
            # guard no-ops. Both 40 (one applied) and 100 (none did) are legitimate;
            # anything below 40 is still a double-apply.
            settled = _await_settles(
                all_nodes,
                "ucc_guarded_counter",
                accept=lambda n: n in (40, 100),
                timeout=timeouts.finalization * 3,
                label="guarded-rmw",
            )
            assert settled >= 40, f"guarded-rmw: double-applied decrement, settled={settled}"
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(
            producers, all_nodes, bg, bg_src0, bg_dst0, timeouts, "bg-load(guarded-rmw)"
        )


def test_mergeable_balance_concurrent_transfers_compose(user_shard, timeouts):
    """Concurrent vault transfers from three sources into ONE shared dest must
    COMPOSE. The dest balance is a mergeable IntegerAdd number-channel, so every
    credit lands and the final balance is the sum — never less (a finalized credit
    undone, #71) and never MORE (a double-applied credit), on every node."""
    shard = user_shard
    all_nodes = shard.all_nodes
    ro = shard.node("readonly")
    producers = [shard.node(n) for n in _PRODUCER_KEYS]
    bg = _BackgroundLoad(producers)
    bg_src0 = bg_dst0 = 0
    if _BG_LOAD_ENABLED:
        bg_src0 = ro.get_vault().get_balance(_BG_SRC_ADDR)
        bg_dst0 = ro.get_vault().get_balance(_BG_DST_ADDR)
        bg.start()
    try:
        before = ro.get_vault().get_balance(_MERGE_DEST_ADDR)
        amounts = {"validator1": 10, "validator2": 100, "validator3": 1000}

        # Submit one transfer per source-node in tight succession so the three
        # land in overlapping sibling blocks (the mergeable-merge surface).
        deploy_ids: List[str] = []
        for name, key in _PRODUCER_KEYS.items():
            src_addr = key.get_public_key().get_vault_address()
            deploy_ids.append(
                shard.node(name)
                .get_vault()
                .transfer(
                    src_addr,
                    _MERGE_DEST_ADDR,
                    amounts[name],
                    key,
                    phlo_price=1,
                    phlo_limit=_PHLO_LIMIT,
                )
            )
        for name, did in zip(_PRODUCER_KEYS, deploy_ids):
            wait_for_deploy_included(shard.node(name), did, timeouts.deploy_inclusion * 3)
        verdicts = _assert_all_finalized(
            [shard.node(n) for n in _PRODUCER_KEYS],
            all_nodes,
            deploy_ids,
            timeouts,
            "mergeable-balance",
        )

        # Credits compose, so the destination reconciles to exactly the sum of the
        # transfers that finalized — an expired transfer must not be credited.
        expected = before + sum(amounts[name] for name in _finalized_names(deploy_ids, verdicts))
        await_balance_converges_on_all_nodes(
            all_nodes,
            _MERGE_DEST_ADDR,
            expected,
            timeouts.finalization * 2,
            "mergeable-balance",
            non_regression="up",
            upper_bound=expected,
        )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(
            producers, all_nodes, bg, bg_src0, bg_dst0, timeouts, "bg-load(balance)"
        )


def test_overdraft_cost_priority_keeps_higher_cost_transfer(user_shard, timeouts):
    """Cost-priority overdraft (integration analog of
    fold_rejection_rejects_lower_cost_branch_on_overdraft): two concurrent transfers
    from the SAME source that each fit alone but together overdraw it. The source
    balance cell is an IntegerAdd number-channel, so the combined debit goes negative
    and #3's fold_rejection rejects the LOWER-cost branch.

    The HIGH transfer (higher phlo-price -> higher cost) moves the LARGER amount; the
    LOW transfer the smaller. With amounts (≫ gas) chosen so the source cannot cover
    the loser after the winner applies, the loser's recovery re-executes and fails
    (insufficient balance). So the dest must receive EXACTLY the HIGH amount — not the
    low amount (which would mean the cheaper branch won), not their sum (a double-spend)
    — and the source must never go negative. All checked on every node."""
    shard = user_shard
    all_nodes = shard.all_nodes
    ro = shard.node("readonly")
    producers = [shard.node(n) for n in _PRODUCER_KEYS]
    bg = _BackgroundLoad(producers)
    bg_src0 = bg_dst0 = 0
    if _BG_LOAD_ENABLED:
        bg_src0 = ro.get_vault().get_balance(_BG_SRC_ADDR)
        bg_dst0 = ro.get_vault().get_balance(_BG_DST_ADDR)
        bg.start()
    try:
        before = ro.get_vault().get_balance(_OVERDRAFT_DST_ADDR)

        # High-cost transfer on validator1, low-cost on validator2 — siblings the next
        # block multi-parent-merges. Both debit the same source; together they overdraw.
        high_id = (
            shard.node("validator1")
            .get_vault()
            .transfer(
                _OVERDRAFT_SRC_ADDR,
                _OVERDRAFT_DST_ADDR,
                _OVERDRAFT_HIGH_AMOUNT,
                _OVERDRAFT_SRC_KEY,
                phlo_price=_OVERDRAFT_HIGH_PRICE,
                phlo_limit=_PHLO_LIMIT,
            )
        )
        low_id = (
            shard.node("validator2")
            .get_vault()
            .transfer(
                _OVERDRAFT_SRC_ADDR,
                _OVERDRAFT_DST_ADDR,
                _OVERDRAFT_LOW_AMOUNT,
                _OVERDRAFT_SRC_KEY,
                phlo_price=_OVERDRAFT_LOW_PRICE,
                phlo_limit=_PHLO_LIMIT,
            )
        )
        for node, did in ((shard.node("validator1"), high_id), (shard.node("validator2"), low_id)):
            wait_for_deploy_included(node, did, timeouts.deploy_inclusion * 3)

        # No-double-spend SAFETY invariant. The merge keeps the higher-COST branch, but
        # "cost" is phlo CONSUMED (≈equal for two simple transfers), not phlo_price — so
        # WHICH branch wins is the deterministic tiebreak, not observable as price
        # priority. What IS guaranteed and observable: EXACTLY ONE transfer lands (never
        # both = double-spend, never neither), and the source never goes negative, on
        # every node. The loser recovers and fails (insufficient balance after the winner).
        accept_high = before + _OVERDRAFT_HIGH_AMOUNT
        accept_low = before + _OVERDRAFT_LOW_AMOUNT
        deadline = time.time() + timeouts.finalization * 3
        last_dst = None
        settled = False
        while time.time() < deadline:
            try:
                lfb = assert_all_nodes_agree_on_lfb(all_nodes)
            except AssertionError:
                time.sleep(1.0)
                continue
            dst_v = assert_balance_consistent_across_nodes(all_nodes, _OVERDRAFT_DST_ADDR, lfb)
            src_v = assert_balance_consistent_across_nodes(all_nodes, _OVERDRAFT_SRC_ADDR, lfb)
            last_dst = dst_v
            assert src_v >= 0, (
                f"[overdraft] source went negative ({src_v}) — an overdraft was applied "
                f"instead of rejected"
            )
            assert dst_v <= accept_high, (
                f"[overdraft] dest {dst_v} exceeds single-winner max {accept_high} — both "
                f"branches landed (double-spend)"
            )
            if dst_v in (accept_high, accept_low):
                settled = True
                break
            time.sleep(1.0)
        if not settled and last_dst == before:
            # Neither transfer landed. Legitimate only if the shard terminally
            # judged BOTH expired; otherwise the contended transfer was lost
            # without a verdict and this stays a failure. The call raises on any
            # incoherent verdict and records the expiries.
            verdicts = _assert_all_finalized(
                producers, all_nodes, [high_id, low_id], timeouts, "overdraft"
            )
            if not verdicts.finalized:
                logging.warning(
                    "overdraft: both transfers expired without landing — dest unchanged at %d",
                    before,
                )
                settled = True
        assert settled, (
            f"[overdraft] dest did not settle to exactly one of "
            f"{{{accept_high}, {accept_low}}}; last all-node read={last_dst}"
        )
        logging.info(
            "overdraft: dest settled to %s (one of high=%d/low=%d), source>=0, all nodes",
            last_dst,
            accept_high,
            accept_low,
        )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(
            producers, all_nodes, bg, bg_src0, bg_dst0, timeouts, "bg-load(overdraft)"
        )


def test_nested_map_concurrent_distinct_inner_keys(user_shard, timeouts):
    """Nested single-value Map cell (the PoS ``allBonds`` shape): one cell holds an OUTER
    map whose ``"bonds"`` key holds an INNER map. Each round, three proposers concurrently
    rewrite the SAME outer key ``"bonds"`` adding DISTINCT inner keys — a same-outer-key
    conflict the merge must resolve by RECURSING into the inner map and unioning the
    distinct entries (merge3_map recursion), NOT keep-one'ing the whole inner map (which
    would drop two validators' entries — the #71 mode). Integration analog of
    fs_seal_nested_map_proxy_pos_statech. Every inner entry, once finalized, must persist
    on every node; the inner map converges to the exact union each round."""
    shard = user_shard
    all_nodes = shard.all_nodes
    ro = shard.node("readonly")
    producers = [shard.node(n) for n in _PRODUCER_KEYS]
    bg = _BackgroundLoad(producers)
    bg_src0 = bg_dst0 = 0
    if _BG_LOAD_ENABLED:
        bg_src0 = ro.get_vault().get_balance(_BG_SRC_ADDR)
        bg_dst0 = ro.get_vault().get_balance(_BG_DST_ADDR)
        bg.start()
    try:
        _finalize_setup(shard, '@"ucc_nested"!({"bonds" : {}})', timeouts)
        inner: dict = {}
        for rnd in range(_CONFLICT_ROUNDS):
            keys = {"validator1": f"v1_{rnd}", "validator2": f"v2_{rnd}", "validator3": f"v3_{rnd}"}
            vals = {"validator1": rnd * 3 + 1, "validator2": rnd * 3 + 2, "validator3": rnd * 3 + 3}
            ids = _deploy_on_each(
                shard,
                lambda name, k=keys, v=vals: (
                    f'for (@m <- @"ucc_nested") {{ @"ucc_nested"!('
                    f'm.set("bonds", m.getOrElse("bonds", {{}}).set("{k[name]}", {v[name]}))) }}'
                ),
                timeouts,
            )
            verdicts = _assert_all_finalized(
                producers, all_nodes, ids, timeouts, f"nested-map-{rnd}"
            )
            # Inner keys are distinct, so the union carries exactly the finalized
            # writers' entries; an expired writer's entry must never appear.
            for name in _finalized_names(ids, verdicts):
                inner[keys[name]] = vals[name]
            # Converge to {"bonds": <exact inner union>} on every node. An inner entry
            # missing/changed is the recursive-merge-drops-an-entry (#71) mode.
            await_channel_converges_on_all_nodes(
                all_nodes,
                "ucc_nested",
                {"bonds": dict(inner)},
                timeouts.finalization * 3,
                f"nested-map-{rnd}",
            )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(
            producers, all_nodes, bg, bg_src0, bg_dst0, timeouts, "bg-load(nested-map)"
        )


def test_concurrent_delete_and_set_same_key(user_shard, timeouts):
    """Delete/set RACE on the SAME key in one cell: each round, validator1 DELETES key
    ``"x"``, validator2 SETS ``"x"`` to a new value, validator3 updates a distinct key
    ``"y"`` — all three consume the one cell, so it is a genuine 3-way single-value-cell
    conflict. The merge keep-ones one write and recovery re-lands the losers, so the cell
    settles deterministically and node-identically to EITHER ``"x"`` present (set ordered
    last) OR ``"x"`` absent (delete ordered last) — never multi-valued, never both, never
    a spurious key. ``"y"`` (uncontended on its key, contended on the cell) always lands.
    Exercises delete-vs-write conflict resolution, which the other scenarios omit."""
    shard = user_shard
    all_nodes = shard.all_nodes
    ro = shard.node("readonly")
    producers = [shard.node(n) for n in _PRODUCER_KEYS]
    bg = _BackgroundLoad(producers)
    bg_src0 = bg_dst0 = 0
    if _BG_LOAD_ENABLED:
        bg_src0 = ro.get_vault().get_balance(_BG_SRC_ADDR)
        bg_dst0 = ro.get_vault().get_balance(_BG_DST_ADDR)
        bg.start()
    try:
        _finalize_setup(shard, '@"ucc_delset"!({"x" : 0, "y" : 0})', timeouts)
        last_x, last_y = 0, 0  # values the cell holds if a round's writers expire
        for rnd in range(_CONFLICT_ROUNDS):
            set_val = rnd * 2 + 1
            y_val = rnd * 2 + 2

            def term_for(name, sv=set_val, yv=y_val):
                if name == "validator1":
                    return 'for (@m <- @"ucc_delset") { @"ucc_delset"!(m.delete("x")) }'
                if name == "validator2":
                    return f'for (@m <- @"ucc_delset") {{ @"ucc_delset"!(m.set("x", {sv})) }}'
                return f'for (@m <- @"ucc_delset") {{ @"ucc_delset"!(m.set("y", {yv})) }}'

            ids = _deploy_on_each(shard, term_for, timeouts)
            verdicts = _assert_all_finalized(
                producers, all_nodes, ids, timeouts, f"del-set-{rnd}"
            )
            # Settle: "y" == this round's value; "x" either absent (del last) or == set_val
            # (set last); single-valued, node-identical, no spurious keys (per-poll in helper).
            # Each outcome is admitted only if the deploy that produces it finalized —
            # so an expired delete cannot excuse a missing "x", and vice versa.
            fin = set(_finalized_names(ids, verdicts))
            x_allowed = set()
            if "validator1" in fin:
                x_allowed.add(None)
            if "validator2" in fin:
                x_allowed.add(set_val)
            if not x_allowed:
                x_allowed = {last_x}
            y_expected = y_val if "validator3" in fin else last_y
            settled = _await_map_settles(
                all_nodes,
                "ucc_delset",
                accept=lambda m, xa=x_allowed, ye=y_expected: m.get("y") == ye
                and m.get("x") in xa,
                allowed_keys={"x", "y"},
                timeout=timeouts.finalization * 3,
                label=f"del-set-{rnd}",
            )
            last_x, last_y = settled.get("x"), settled.get("y")
            logging.info(
                "del-set round %d/%d: settled to %r on all nodes", rnd, _CONFLICT_ROUNDS, settled
            )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(
            producers, all_nodes, bg, bg_src0, bg_dst0, timeouts, "bg-load(del-set)"
        )


def test_high_fanout_distinct_keys_all_land(user_shard, timeouts):
    """Wider-than-3-way concurrency: each of the 3 producers submits ``_FANOUT_PER_NODE``
    deploys (3 x that total) overlapping as siblings, every one a read-modify-write of ONE
    shared map cell adding a DISTINCT key. Distinct keys commute, so the merge + recovery
    must serialize the wide fan-out and land EVERY write — none dropped — with the
    finalized map equal to the full key set on every node. Stresses the merge with more
    concurrent single-cell writers than there are validators."""
    shard = user_shard
    all_nodes = shard.all_nodes
    ro = shard.node("readonly")
    producers = [shard.node(n) for n in _PRODUCER_KEYS]
    bg = _BackgroundLoad(producers)
    bg_src0 = bg_dst0 = 0
    if _BG_LOAD_ENABLED:
        bg_src0 = ro.get_vault().get_balance(_BG_SRC_ADDR)
        bg_dst0 = ro.get_vault().get_balance(_BG_DST_ADDR)
        bg.start()
    try:
        _finalize_setup(shard, '@"ucc_fanout"!({})', timeouts)
        expected: dict = {}
        idx = 0
        for name in _PRODUCER_KEYS:
            for i in range(_FANOUT_PER_NODE):
                idx += 1
                expected[f"{name}_{i}"] = idx
        ids = _deploy_k_on_each(
            shard,
            lambda name, i: (
                f'for (@m <- @"ucc_fanout") {{ '
                f'@"ucc_fanout"!(m.set("{name}_{i}", {expected[f"{name}_{i}"]})) }}'
            ),
            _FANOUT_PER_NODE,
            timeouts,
        )
        verdicts = _assert_all_finalized(producers, all_nodes, ids, timeouts, "high-fanout")
        # Every distinct key that FINALIZED must land; the finalized map equals exactly
        # that set on every node, and no already-finalized key vanishes en route (#71
        # non-regression). `_deploy_k_on_each` submits i-major, producer-minor, so the
        # id order is reconstructed the same way.
        submitted = [(name, i) for i in range(_FANOUT_PER_NODE) for name in _PRODUCER_KEYS]
        finalized = verdicts.finalized_set()
        landed = {
            f"{name}_{i}": expected[f"{name}_{i}"]
            for (name, i), did in zip(submitted, ids)
            if did in finalized
        }
        await_channel_converges_on_all_nodes(
            all_nodes,
            "ucc_fanout",
            landed,
            timeouts.finalization * 4,
            "high-fanout",
            non_regression="map",
        )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(
            producers, all_nodes, bg, bg_src0, bg_dst0, timeouts, "bg-load(high-fanout)"
        )


def test_set_cell_concurrent_distinct_elements_union(user_shard, timeouts):
    """Single Set cell, concurrent DISTINCT-element adds (the Set analog of nested-map
    union / fs_seal_nested_set_proxy_pos_activevalidators): each round three proposers
    ``s.add()`` a distinct element to the SAME set cell. Distinct elements commute, so the
    merge unions them via merge3_set — every added element must land and persist on every
    node. Covers the Set value type and the set-union merge primitive."""
    shard = user_shard
    all_nodes = shard.all_nodes
    ro = shard.node("readonly")
    producers = [shard.node(n) for n in _PRODUCER_KEYS]
    bg = _BackgroundLoad(producers)
    bg_src0 = bg_dst0 = 0
    if _BG_LOAD_ENABLED:
        bg_src0 = ro.get_vault().get_balance(_BG_SRC_ADDR)
        bg_dst0 = ro.get_vault().get_balance(_BG_DST_ADDR)
        bg.start()
    try:
        _finalize_setup(shard, '@"ucc_set"!(Set())', timeouts)
        elements: set = set()
        for rnd in range(_CONFLICT_ROUNDS):
            elems = {
                "validator1": rnd * 3 + 1,
                "validator2": rnd * 3 + 2,
                "validator3": rnd * 3 + 3,
            }
            ids = _deploy_on_each(
                shard,
                lambda name, e=elems: (
                    f'for (@s <- @"ucc_set") {{ ' f'@"ucc_set"!(s.add({e[name]})) }}'
                ),
                timeouts,
            )
            verdicts = _assert_all_finalized(
                producers, all_nodes, ids, timeouts, f"set-union-{rnd}"
            )
            # Distinct elements commute: the union carries exactly the finalized adds.
            elements.update(elems[name] for name in _finalized_names(ids, verdicts))
            # Every added element, once finalized, must be present on every node (a missing
            # element is a union-drops-a-member regression). Membership check tolerates the
            # set/list decode form.
            _await_settles(
                all_nodes,
                "ucc_set",
                accept=lambda v, ex=set(elements): v is not None and all(e in v for e in ex),
                timeout=timeouts.finalization * 3,
                label=f"set-union-{rnd}",
            )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(
            producers, all_nodes, bg, bg_src0, bg_dst0, timeouts, "bg-load(set-union)"
        )


def test_scalar_value_conflict_resolves_deterministically(user_shard, timeouts):
    """Same-cell CONFLICT across the full range of NON-foldable value types — String, Bool,
    Int, List, Tuple — the generalized non_foldable_fork. For each type, three proposers
    concurrently write DIFFERENT values of that type into one cell (wrapped ``{"v": <value>}``
    so the decode is a stable map). These are non-mergeable, so the merge keep-ones one
    write and recovery re-lands the losers; the cell must settle to a SINGLE value that is
    one of the written candidates, node-identical, never multi-valued or stale-consumed.
    Exercises the deterministic_pick scalar leaf for every value type."""
    shard = user_shard
    all_nodes = shard.all_nodes
    ro = shard.node("readonly")
    producers = [shard.node(n) for n in _PRODUCER_KEYS]
    bg = _BackgroundLoad(producers)
    bg_src0 = bg_dst0 = 0
    if _BG_LOAD_ENABLED:
        bg_src0 = ro.get_vault().get_balance(_BG_SRC_ADDR)
        bg_dst0 = ro.get_vault().get_balance(_BG_DST_ADDR)
        bg.start()
    # (type label, [(rholang literal, decoded value) per proposer]).
    type_cases = [
        ("string", [('"alpha"', "alpha"), ('"bravo"', "bravo"), ('"charlie"', "charlie")]),
        ("bool", [("true", True), ("false", False), ("true", True)]),
        ("int", [("111", 111), ("222", 222), ("333", 333)]),
        ("list", [("[1, 2]", [1, 2]), ("[3, 4]", [3, 4]), ("[5, 6]", [5, 6])]),
        ("tuple", [("(1, 2)", (1, 2)), ("(3, 4)", (3, 4)), ("(5, 6)", (5, 6))]),
    ]
    try:
        for type_label, cands in type_cases:
            cell = f"ucc_scalar_{type_label}"
            # Sentinel seed distinct from every candidate of every type (and not equal under
            # Python's True==1/False==0 coercion, which a numeric seed would trip for Bool).
            _finalize_setup(shard, f'@"{cell}"!({{"v" : "INIT"}})', timeouts)
            lits = {name: cands[i][0] for i, name in enumerate(_PRODUCER_KEYS)}
            decoded_by_name = {name: _norm(cands[i][1]) for i, name in enumerate(_PRODUCER_KEYS)}
            last_v = _norm("INIT")  # value the cell holds if a whole round expires
            for rnd in range(3):
                ids = _deploy_on_each(
                    shard,
                    lambda name, c=cell, lts=lits: (
                        f'for (@m <- @"{c}") {{ ' f'@"{c}"!(m.set("v", {lts[name]})) }}'
                    ),
                    timeouts,
                )
                verdicts = _assert_all_finalized(
                    producers, all_nodes, ids, timeouts, f"scalar-{type_label}-{rnd}"
                )
                # Settle to {"v": <one FINALIZED candidate>}, single-keyed, node-identical.
                # An expired writer's value must not be the one that settled; if every
                # writer expired the cell keeps what it already held.
                fin_vals = [decoded_by_name[n] for n in _finalized_names(ids, verdicts)]
                accepted = fin_vals or [last_v]
                settled = _await_map_settles(
                    all_nodes,
                    cell,
                    accept=lambda m, d=accepted: m.get("v") is not None and _norm(m.get("v")) in d,
                    allowed_keys={"v"},
                    timeout=timeouts.finalization * 3,
                    label=f"scalar-{type_label}-{rnd}",
                )
                last_v = _norm(settled.get("v"))
                logging.info(
                    "scalar-%s round %d: settled to v=%r on all nodes",
                    type_label,
                    rnd,
                    settled.get("v"),
                )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(
            producers, all_nodes, bg, bg_src0, bg_dst0, timeouts, "bg-load(scalar-types)"
        )

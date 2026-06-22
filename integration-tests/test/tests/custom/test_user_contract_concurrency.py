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

from ...infra.assertions import (
    assert_all_nodes_agree_on_lfb,
    assert_balance_consistent_across_nodes,
    assert_block_finalized_on_all_nodes,
    assert_channel_consistent_across_nodes,
    await_balance_converges_on_all_nodes,
    await_channel_converges_on_all_nodes,
)
from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import wait_for_deploy_included, wait_for_finalized
from ...infra.shard import Shard
from f1r3fly.crypto import PrivateKey

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
_OVERDRAFT_HIGH_AMOUNT = 120_000_000   # high phlo-price → higher cost → must WIN
_OVERDRAFT_LOW_AMOUNT = 100_000_000    # low phlo-price → lower cost → must LOSE
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
_BG_LOAD_ENABLED = True


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
        logging.info("Background load stopped: %d transfers, %d errors", self._counter, self._errors)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            node = self._producers[self._counter % len(self._producers)]
            try:
                did = node.get_vault().transfer(
                    _BG_SRC_ADDR, _BG_DST_ADDR, _BG_TRANSFER_AMOUNT, _BG_SRC_KEY,
                    phlo_price=1, phlo_limit=_PHLO_LIMIT,
                )
                with self._lock:
                    self._deploy_ids.append(did)
            except Exception as e:  # noqa: BLE001 — surfaced via finalization assertion
                self._errors += 1
                logging.warning("bg-load transfer %d on %s failed: %s", self._counter, node.name, e)
            self._counter += 1
            self._stop.wait(self._interval)


# ── Strict finalization helper (mirrors lifecycle's bg-load assertion) ───────

def _assert_all_finalized(producers, all_nodes, deploy_ids: List[str], timeouts,
                          label: str) -> None:
    """Every deploy in ``deploy_ids`` must land in a block that finalizes on
    EVERY node. A deploy that lands in a losing-fork block and is never merged
    into a finalized descendant is silently dropped work — zero tolerance."""
    missing: List[str] = []
    unfinalized: List = []
    for sig in deploy_ids:
        light_block = None
        for node in producers:
            try:
                light_block = node.find_deploy(sig)
                if light_block is not None:
                    break
            except Exception:  # noqa: BLE001
                continue
        if light_block is None:
            missing.append(sig)
            continue
        try:
            assert_block_finalized_on_all_nodes(all_nodes, light_block.blockHash,
                                                timeout=timeouts.finalization * 2)
        except Exception:  # noqa: BLE001
            unfinalized.append((sig, light_block.blockNumber))
    assert not missing and not unfinalized, (
        f"[{label}] {len(missing)} deploys never included, {len(unfinalized)} included "
        f"but not finalized on all nodes (of {len(deploy_ids)}). "
        f"missing(3)={[s[:16] for s in missing[:3]]} "
        f"unfinalized(3)={[(s[:16], n) for s, n in unfinalized[:3]]}"
    )


def _assert_bg_load_robust(producers, all_nodes, bg, src0: int, dst0: int,
                           timeouts, label: str) -> None:
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
    _assert_all_finalized(producers, all_nodes, bg_ids, timeouts, label)
    n = len(bg_ids)
    want_dst = dst0 + n * _BG_TRANSFER_AMOUNT
    min_src_debit = n * _BG_TRANSFER_AMOUNT
    await_balance_converges_on_all_nodes(
        all_nodes, _BG_DST_ADDR, want_dst, timeouts.finalization * 2, f"{label}-dst",
        non_regression="up", upper_bound=want_dst,
    )
    lfb = assert_all_nodes_agree_on_lfb(all_nodes, timeout=timeouts.finalization)
    src_final = assert_balance_consistent_across_nodes(all_nodes, _BG_SRC_ADDR, lfb)
    assert src0 - src_final >= min_src_debit, (
        f"[{label}] bg-src under-debited: source fell by {src0 - src_final} < transferred "
        f"{min_src_debit} — a finalized debit was lost"
    )
    logging.info("%s: reconciled %d bg transfers on all nodes — dst->%d (exact), src->%d (incl gas)",
                 label, n, want_dst, src_final)


def _deploy_on_each(shard, term_for, timeouts) -> List[str]:
    """Submit one deploy per genesis validator in tight succession (signed by
    that validator's own funded key) so the three land in overlapping blocks —
    sibling proposals the next block multi-parent-merges. Returns the deploy
    ids. Waits for inclusion only; finalization is asserted by the caller."""
    deploy_ids: List[str] = []
    for name, key in _PRODUCER_KEYS.items():
        node = shard.node(name)
        did = node.deploy_string(term_for(name), key,
                                 phlo_limit=_PHLO_LIMIT, phlo_price=_PHLO_PRICE)
        deploy_ids.append(did)
        logging.info("RMW_DEPLOY_ID node=%s deploy_id=%s", name, did)
    for name, did in zip(_PRODUCER_KEYS, deploy_ids):
        wait_for_deploy_included(shard.node(name), did, timeouts.deploy_inclusion * 3)
    return deploy_ids


def _finalize_setup(shard, term: str, timeouts) -> None:
    """Deploy a one-time setup term on validator1 and wait until it finalizes
    cluster-wide so the initialized cell is visible to every proposer."""
    v1 = shard.node("validator1")
    did = v1.deploy_string(term, _PRODUCER_KEYS["validator1"],
                           phlo_limit=_PHLO_LIMIT, phlo_price=_PHLO_PRICE)
    block = wait_for_deploy_included(v1, did, timeouts.deploy_inclusion * 3)
    wait_for_finalized(v1, block.blockNumber, timeouts.finalization * 3)
    assert_block_finalized_on_all_nodes(shard.all_nodes, block.blockHash,
                                        timeout=timeouts.finalization * 2)


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
        ] + _PRODUCER_WALLETS,
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
        _assert_all_finalized(producers, all_nodes, deploy_ids, timeouts, "independent-channels")
        for name, val in expected.items():
            await_channel_converges_on_all_nodes(
                all_nodes, f"ucc_kv_{name}", val, timeouts.finalization * 2,
                f"independent-{name}",
            )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(producers, all_nodes, bg, bg_src0, bg_dst0,
                               timeouts, "bg-load(independent)")


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
            {"validator1": ("set", "a", 1), "validator2": ("set", "b", 2),
             "validator3": ("set", "c", 3)},                       # adds
            {"validator1": ("del", "a"), "validator2": ("set", "d", 4),
             "validator3": ("set", "e", 5)},                       # delete a (+ distinct adds)
            {"validator1": ("set", "a", 9), "validator2": ("set", "b", 20),
             "validator3": ("set", "f", 6)},                       # re-add a, update b, add f
        ]

        expected: dict = {}
        volatile: set = set()           # keys revisited by del/update/re-add — non-monotone
        for rnd, ops in enumerate(rounds):
            def term_for(name, ops=ops):
                op = ops[name]
                if op[0] == "set":
                    return (f'for (@m <- @"ucc_map_cell") {{ '
                            f'@"ucc_map_cell"!(m.set("{op[1]}", {op[2]})) }}')
                return (f'for (@m <- @"ucc_map_cell") {{ '
                        f'@"ucc_map_cell"!(m.delete("{op[1]}")) }}')

            ids = _deploy_on_each(shard, term_for, timeouts)
            _assert_all_finalized(producers, all_nodes, ids, timeouts, f"map-round-{rnd}")

            for op in ops.values():
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
                all_nodes, "ucc_map_cell", expected, timeouts.finalization * 3,
                f"map-round-{rnd}", non_regression="map", volatile=volatile,
            )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(producers, all_nodes, bg, bg_src0, bg_dst0,
                               timeouts, "bg-load(single-cell)")


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
        candidates = {"validator1": 1, "validator2": 2, "validator3": 3}

        ids = _deploy_on_each(
            shard,
            lambda name: (f'for (@m <- @"ucc_conflict_map") {{ '
                          f'@"ucc_conflict_map"!(m.set("shared", {candidates[name]})) }}'),
            timeouts,
        )
        _assert_all_finalized(producers, all_nodes, ids, timeouts, "same-key-conflict")

        # Poll the finalized cell on EVERY node (at the common LFB) until it settles to a
        # single "shared" entry whose value is one of the written candidates and is
        # identical across nodes. Recovery re-lands the losers, so the running value may
        # change between candidates before settling — but it must never be multi-valued,
        # divergent, or missing.
        candidate_vals = set(candidates.values())
        deadline = time.time() + timeouts.finalization * 3
        last = None
        settled = False
        while time.time() < deadline:
            try:
                lfb = assert_all_nodes_agree_on_lfb(all_nodes)
            except AssertionError:
                time.sleep(1.0)
                continue
            m = assert_channel_consistent_across_nodes(all_nodes, "ucc_conflict_map", lfb)
            last = m
            if m and set(m.keys()) == {"shared"} and m["shared"] in candidate_vals:
                settled = True
                break
            time.sleep(1.0)
        assert settled, (
            f"[same-key-conflict] cell did not settle to a single shared value in "
            f"{candidate_vals}; last all-node-consistent read={last}"
        )
        logging.info("same-key-conflict: settled to shared=%r on all nodes", last["shared"])
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(producers, all_nodes, bg, bg_src0, bg_dst0,
                               timeouts, "bg-load(same-key-conflict)")


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
            lambda name: ('for (@n <- @"ucc_guarded_counter") { '
                          'if (n >= 60) { @"ucc_guarded_counter"!(n - 60) } '
                          'else { @"ucc_guarded_counter"!(n) } }'),
            timeouts,
        )
        _assert_all_finalized(producers, all_nodes, ids, timeouts, "guarded-rmw")

        # Exactly one decrement applies (100 -> 40); the losers' recovery re-evaluates the
        # guard and no-ops. Converge to 40 on every node, never rising (down-only) and —
        # critically — never below 40 (a double-applied decrement is the item-1 mode).
        await_channel_converges_on_all_nodes(
            all_nodes, "ucc_guarded_counter", 40, timeouts.finalization * 3,
            "guarded-rmw", non_regression="down", lower_bound=40,
        )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(producers, all_nodes, bg, bg_src0, bg_dst0,
                               timeouts, "bg-load(guarded-rmw)")


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
            deploy_ids.append(shard.node(name).get_vault().transfer(
                src_addr, _MERGE_DEST_ADDR, amounts[name], key,
                phlo_price=1, phlo_limit=_PHLO_LIMIT))
        for name, did in zip(_PRODUCER_KEYS, deploy_ids):
            wait_for_deploy_included(shard.node(name), did, timeouts.deploy_inclusion * 3)
        _assert_all_finalized([shard.node(n) for n in _PRODUCER_KEYS], all_nodes,
                              deploy_ids, timeouts, "mergeable-balance")

        expected = before + sum(amounts.values())
        await_balance_converges_on_all_nodes(
            all_nodes, _MERGE_DEST_ADDR, expected, timeouts.finalization * 2,
            "mergeable-balance", non_regression="up", upper_bound=expected,
        )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(producers, all_nodes, bg, bg_src0, bg_dst0,
                               timeouts, "bg-load(balance)")


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
        high_id = shard.node("validator1").get_vault().transfer(
            _OVERDRAFT_SRC_ADDR, _OVERDRAFT_DST_ADDR, _OVERDRAFT_HIGH_AMOUNT,
            _OVERDRAFT_SRC_KEY, phlo_price=_OVERDRAFT_HIGH_PRICE, phlo_limit=_PHLO_LIMIT)
        low_id = shard.node("validator2").get_vault().transfer(
            _OVERDRAFT_SRC_ADDR, _OVERDRAFT_DST_ADDR, _OVERDRAFT_LOW_AMOUNT,
            _OVERDRAFT_SRC_KEY, phlo_price=_OVERDRAFT_LOW_PRICE, phlo_limit=_PHLO_LIMIT)
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
        assert settled, (
            f"[overdraft] dest did not settle to exactly one of "
            f"{{{accept_high}, {accept_low}}}; last all-node read={last_dst}"
        )
        logging.info("overdraft: dest settled to %s (one of high=%d/low=%d), source>=0, all nodes",
                     last_dst, accept_high, accept_low)
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(producers, all_nodes, bg, bg_src0, bg_dst0,
                               timeouts, "bg-load(overdraft)")

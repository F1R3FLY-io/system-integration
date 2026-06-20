"""User-side multi-parent-merge validation — concurrency on ordinary contracts.

This test exercises the multi-parent merge with NO PoS involvement, to
establish whether the platform's concurrency model is sound on its own terms.
It mirrors the validator-lifecycle environment (heartbeat on, always-on
background load, strict cluster-wide finalization assertions) but contends
USER contract state instead of the validator bonds map.

Three merge surfaces, each from a different proposer concurrently under load:

  - independent channels (distinct per-key cells) — must merge in PARALLEL;
    every write lands. This is the structure a per-validator-channel PoS
    rewrite would use, so it is direct evidence that rewrite is sound.

  - a single whole-Map cell with read-modify-write (Maps are excluded from
    number-channels by design — the SAME shape as the PoS bonds cell). The
    concurrent writes genuinely conflict; the platform must serialize them via
    reject-and-recover so EVERY entry lands. Silent loss here is a merge bug;
    convergence here proves the bonds failure is specific to the every-block
    close-block contention, not the merge.

  - a mergeable integer counter (number-channel / IntegerAdd) — concurrent
    increments must COMPOSE; the final value is the sum.

Strict throughout: every user deploy must finalize on every node, and the
final canonical state must reflect EVERY operation. Background load runs the
whole time to reproduce the lumpy, contended finalization the merge must
survive.
"""
import logging
import threading
import time
from typing import List, Optional

import pytest

from ...infra.assertions import assert_block_finalized_on_all_nodes
from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import wait_for_deploy_included, wait_for_finalized
from ...infra.shard import Shard
from f1r3fly.crypto import PrivateKey
from f1r3fly.par import par_as_int, par_as_map

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
        ] + _PRODUCER_WALLETS,
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        yield shard
    finally:
        shard.destroy()


# ── Read helpers (exploratory deploy on the readonly observer) ───────────────

def _read_map(ro, cell_channel: str, block_hash: str = "") -> dict:
    """Peek the current Map held on ``cell_channel`` (non-consuming `<<-`)."""
    code = f'new return in {{ for (@m <<- @"{cell_channel}") {{ return!(m) }} }}'
    result = ro.exploratory_deploy(code, block_hash)
    return par_as_map(result[0])


def _read_int(ro, channel: str, block_hash: str = "") -> int:
    code = f'new return in {{ for (@n <<- @"{channel}") {{ return!(n) }} }}'
    result = ro.exploratory_deploy(code, block_hash)
    return par_as_int(result[0])


def _await_map_monotone(ro, cell: str, expected: dict, timeout: float,
                        label: str, interval: float = 1.0,
                        volatile=frozenset()) -> None:
    """Poll the FINALIZED map (`FS(LFB)` via exploratory read) until it == expected,
    asserting NON-REGRESSION the whole way: an add-only key, once observed in a
    finalized read, must never vanish or change in a later finalized read (a dropped
    entry is the #71 finalized-state regression — which a converge-only check would
    miss if it self-heals before the final read). Keys in ``volatile`` (touched by a
    delete / update / re-add) are EXEMPT from the persistence+value check: they are
    intentionally non-monotone, and their correctness is verified by ``expected``."""
    high_water: dict = {}
    last: dict = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cur = _read_map(ro, cell)
        except Exception:  # noqa: BLE001 — transient read during contention
            time.sleep(interval)
            continue
        for k, v in high_water.items():
            if k in volatile:
                continue
            assert cur.get(k) == v, (
                f"[{label}] finalized-state REGRESSION: {k}={v} was finalized then "
                f"vanished/changed (now {cur.get(k)!r}); full finalized map {cur}"
            )
        for k, v in cur.items():
            if k not in volatile:
                high_water[k] = v
        last = cur
        if cur == expected:
            return
        time.sleep(interval)
    raise AssertionError(
        f"[{label}] finalized map did not converge to {expected} within {timeout:.0f}s; "
        f"last={last}, high-water={high_water}"
    )


def _await_balance_monotone(ro, addr: str, expected: int, timeout: float,
                            label: str, interval: float = 1.0) -> None:
    """Poll the FINALIZED dest balance until it == expected, asserting it NEVER
    DECREASES en route — a finalized credit must not be undone (#71 for a number
    cell). Convergence alone would miss a transient drop that self-heals."""
    high_water = -1
    last: Optional[int] = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cur = ro.get_vault().get_balance(addr)
        except Exception:  # noqa: BLE001
            time.sleep(interval)
            continue
        assert cur >= high_water, (
            f"[{label}] finalized-balance REGRESSION: dropped from {high_water} to {cur}"
        )
        high_water = max(high_water, cur)
        last = cur
        if cur == expected:
            return
        time.sleep(interval)
    raise AssertionError(
        f"[{label}] balance did not reach {expected} within {timeout:.0f}s; last={last}"
    )


def _assert_bg_load_robust(producers, all_nodes, ro, bg, src0: int, dst0: int,
                           timeouts, label: str) -> None:
    """Robust bg-load verification — the same non-regression + exact-reconciliation
    principle the foreground scenarios apply, carried over to the always-on same-vault
    transfer stream.

    The bg load moves exactly ``_BG_TRANSFER_AMOUNT`` from ``_BG_SRC`` to ``_BG_DST`` per
    transfer. The SOURCE vault also pays gas (it signs the deploy), so its drop is
    ``N*amount`` PLUS non-deterministic gas — no exact target. The DEST receives only the
    transfer amount, so its target IS exact. After the N submitted transfers finalize:

      1. every bg transfer finalizes on every node (inclusion + cluster finalization);
      2. DEST reconciles EXACTLY to ``dst0 + N*amount`` and never decreases en route —
         every finalized credit lands exactly once: a drop is the #71 mode, a
         double-apply overshoots the exact target and is caught as a timeout. This is the
         strong check — dest is a contended IntegerAdd number cell, so exact convergence
         is direct evidence the merge/seal compose concurrent credits losslessly;
      3. SOURCE never increases en route (a finalized debit not undone) and ends debited
         by AT LEAST the transferred total (``src0 - src_final >= N*amount``; the surplus
         is gas).

    Reading finalized balances on the readonly observer is sufficient for cluster
    agreement: every bg block is asserted finalized on ALL nodes, and a block's state is
    a function of its finalized cone, so a divergent finalized balance could not have
    finalized the same blocks cluster-wide."""
    bg_ids = bg.deploy_ids()
    _assert_all_finalized(producers, all_nodes, bg_ids, timeouts, label)
    n = len(bg_ids)
    want_dst = dst0 + n * _BG_TRANSFER_AMOUNT
    min_src_debit = n * _BG_TRANSFER_AMOUNT
    dst_water = -1
    src_water: Optional[int] = None
    last_dst = last_src = None
    deadline = time.time() + timeouts.finalization * 2
    while time.time() < deadline:
        try:
            cur_dst = ro.get_vault().get_balance(_BG_DST_ADDR)
            cur_src = ro.get_vault().get_balance(_BG_SRC_ADDR)
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
            continue
        assert cur_dst >= dst_water, (
            f"[{label}] bg-dst REGRESSION: finalized credit dropped from {dst_water} to {cur_dst}"
        )
        if src_water is not None:
            assert cur_src <= src_water, (
                f"[{label}] bg-src REGRESSION: finalized debit undone, rose from {src_water} to {cur_src}"
            )
        dst_water = max(dst_water, cur_dst)
        src_water = cur_src if src_water is None else min(src_water, cur_src)
        last_dst, last_src = cur_dst, cur_src
        if cur_dst == want_dst:
            assert src0 - cur_src >= min_src_debit, (
                f"[{label}] bg-src under-debited: source fell by {src0 - cur_src} < transferred "
                f"{min_src_debit} — a finalized debit was lost"
            )
            logging.info("%s: reconciled %d bg transfers — dst %d->%d (exact), src %d->%d (incl gas)",
                         label, n, dst0, cur_dst, src0, cur_src)
            return
        time.sleep(1.0)
    raise AssertionError(
        f"[{label}] bg-dst did not reach exact credit {want_dst} within "
        f"{timeouts.finalization * 2:.0f}s; last dst={last_dst} (want {want_dst}), src={last_src}"
    )


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


# ── Tests ────────────────────────────────────────────────────────────────────

def test_independent_channels_merge_in_parallel(user_shard, timeouts):
    """Concurrent writes to DISTINCT per-key cells must all land — different
    channels commute, so the merge applies every write in parallel."""
    shard = user_shard
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
        _assert_all_finalized(producers, shard.all_nodes,
                              deploy_ids, timeouts, "independent-channels")
        for name, val in expected.items():
            got = _read_int(ro, f"ucc_kv_{name}")
            assert got == val, f"key ucc_kv_{name}: expected {val}, read {got}"
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(producers, shard.all_nodes, ro, bg, bg_src0, bg_dst0,
                               timeouts, "bg-load(independent)")


def test_single_cell_map_concurrent_adds_all_resolve(user_shard, timeouts):
    """The PoS-bonds analog, full lifecycle: a single whole-Map cell driven by rounds
    of CONCURRENT read-modify-writes (set / delete / update / re-add), three proposers
    per round. Maps are excluded from number-channels, so concurrent writes genuinely
    conflict — one wins the merge, the losers are re-proposed by recovery onto the new
    map. Each round is driven to full finalized convergence before the next, so
    same-key cross-round ops are deterministically ordered.

    Asserts end to end:
      - every deploy finalizes on every node,
      - NON-REGRESSION: an add-only finalized key never vanishes (the #71 mode),
      - the finalized map converges each round to the exact running fold,
      - the final finalized map equals the exact operation fold (adds − deletes,
        latest value for updates).
    A missing/extra entry is silently dropped/duplicated work — the bonds failure
    mode. Within-round ops use DISTINCT keys (they commute); cross-round same-key ops
    (delete-a then re-add-a; update-b) are ordered by the per-round convergence."""
    shard = user_shard
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
            _assert_all_finalized(producers, shard.all_nodes, ids, timeouts,
                                  f"map-round-{rnd}")

            for op in ops.values():
                k = op[1]
                if k in expected or k in volatile:
                    volatile.add(k)
                if op[0] == "set":
                    expected[k] = op[2]
                else:
                    expected.pop(k, None)

            # Drive the finalized cell to this round's exact fold, asserting no
            # add-only finalized key vanishes en route (the #71 non-regression check).
            _await_map_monotone(ro, "ucc_map_cell", expected, timeouts.finalization * 3,
                                f"map-round-{rnd}", volatile=volatile)

        final_map = _read_map(ro, "ucc_map_cell")
        assert final_map == expected, (
            f"final finalized map != exact operation fold: expected {expected}, got {final_map}"
        )
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(producers, shard.all_nodes, ro, bg, bg_src0, bg_dst0,
                               timeouts, "bg-load(single-cell)")


def test_mergeable_balance_concurrent_transfers_compose(user_shard, timeouts):
    """Concurrent vault transfers from three sources into ONE shared dest must
    COMPOSE. The dest balance is a mergeable IntegerAdd number-channel (the only
    genuinely-mergeable user-reachable surface — a plain user channel is NOT
    in the mergeable_tags registry and would single-cell-conflict instead), so
    every credit lands and the final balance is the sum, none lost."""
    shard = user_shard
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
        _assert_all_finalized([shard.node(n) for n in _PRODUCER_KEYS], shard.all_nodes,
                              deploy_ids, timeouts, "mergeable-balance")

        expected = before + sum(amounts.values())

        # Compose to the sum AND assert non-regression: the finalized balance must
        # never decrease en route (a finalized credit must not be undone — #71).
        _await_balance_monotone(ro, _MERGE_DEST_ADDR, expected, timeouts.finalization * 2,
                                "mergeable-balance")
    finally:
        bg.stop()
    if _BG_LOAD_ENABLED:
        _assert_bg_load_robust(producers, shard.all_nodes, ro, bg, bg_src0, bg_dst0,
                               timeouts, "bg-load(balance)")

"""The leaky bucket on one live shard, through a validator set that changes.

Five validators with stakes 500, 400, 300, 200 and 100 run under the liveness
profile (a fast drain and short epochs), with a readonly observer and
background deploy load throughout. One scenario runs in phases that overlap on
purpose: a validator flaps short of ejection and refills each time; then a
validator joins and another leaves while a third goes silent and is
ejected; the ejected validator's first rejoin is refused and its second
reinstates it while a fourth validator goes silent; that one is slashed when
its grace ends.

Each step is detected from the honest validators' liveness reports
(``GET /api/validator-liveness`` on the admin port) and its outcome is checked
against the PoS contract, read on the observer at the same finalized block.
"""

import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

import pytest
from f1r3fly.crypto import PrivateKey
from f1r3fly.par import par_as_string

from ...infra.assertions import (
    assert_bonds_map_consistent_across_nodes,
    assert_deploy_block_finalized_on_all_nodes,
    collect_forensics,
    common_finalized_anchor,
    finalized_deploy_cost,
)
from ...infra.background_load import (
    BackgroundLoad,
    assert_vault_transfers_reconciled,
    vault_transfers,
)
from ...infra.config import (
    LIVENESS_PROFILE_CLI_OPTIONS,
    LIVENESS_PROFILE_EPOCH_LENGTH,
    ShardConfig,
)
from ...infra.keys import (
    VALIDATOR1_ID,
    VALIDATOR2_ID,
    VALIDATOR3_ID,
    VALIDATOR4_ID,
    VALIDATOR5_ID,
    VALIDATOR6_ID,
)
from ...infra.polling import poll_until, wait_for_block_visible
from ...infra.shard import Shard

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.xdist_group("custom")

_GENESIS = (
    (VALIDATOR1_ID, 500),
    (VALIDATOR2_ID, 400),
    (VALIDATOR3_ID, 300),
    (VALIDATOR4_ID, 200),
    (VALIDATOR5_ID, 100),
)
_STAKE = {identity.public_hex: stake for identity, stake in _GENESIS}
_JOINER_STAKE = 100

_V1, _V2, _LEAVER, _A, _B = (identity.public_hex for identity, _ in _GENESIS)
_JOINER = VALIDATOR6_ID.public_hex
_NAME = {
    identity.public_hex: identity.name for identity in (*(i for i, _ in _GENESIS), VALIDATOR6_ID)
}

# Well below the shortfall of any drained bucket (at least a tenth of A's 200),
# so every voter refuses it.
_SHORT_TOPUP = 1
# Bucket levels and shortfalls are reported in `1 / _PPM` of a bond unit.
_PPM = 1_000_000
# The whole stake covers any shortfall, so every voter backs it.
_FULL_TOPUP = _STAKE[_A]

# Heights the shard runs at mixed stakes before anything changes.
_HOLD_HEIGHTS = 30
# A is silenced late in an epoch and the join and leave are submitted early in
# the next, so they take effect a whole epoch later: the ejection claim
# against A, which opens 10 to 16 heights after the silence, opens while the
# leaver is still seated.
_SILENCE_OFFSET = range(17, 19)
_EPOCH_OFFSET = range(1, 5)
# Budget for any one step, in blocks: the slowest step waits out two epochs of
# grace.
_STEP_BLOCKS = 200
_FUNDED = 50_000_000_000_000_000

# A is paused until it has used these fractions of its headroom above the
# threshold, then resumed, and must refill without any reporter acting on it.
_FLAP_DEPTHS = (0.5, 0.95)
# Then it is paused until a reporter sees it drained, this many times.
_DIPS = 3
# A dip is resumed at most this many blocks below the threshold, half the
# observation window, or the poll was too slow to test the boundary.
_DIP_RESUME_LIMIT = 5
# A flap's pause ends within about a block of its trigger only at this poll.
_FLAP_POLL = 0.25
# Windows shorter than this many blocks are not measured for earnings.
_EARNINGS_MIN_BLOCKS = 20
# The most heights one block listing returns (the node's max-blocks-limit).
_MAX_BLOCKS_DEPTH = 100
_REWARD_PPM = int(LIVENESS_PROFILE_CLI_OPTIONS["--leaky-bucket-reward-rate-ppm"])

_LOAD_KEY = PrivateKey.from_seed(71001)
_LOAD_DESTINATION = PrivateKey.from_seed(71002).get_public_key().get_vault_address()
_LOAD_AMOUNT = 1


class _ViolationError(AssertionError):
    """A property that must hold on every poll does not. ``poll_until``
    retries every exception, so a probe hands this back as its result to end
    the poll at once."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _ViolationError(message)


def _vault(identity) -> str:
    return identity.private_key().get_public_key().get_vault_address()


def _bucket(report: dict, validator: str) -> Optional[dict]:
    return next((b for b in report["localView"]["buckets"] if b["validator"] == validator), None)


def _stakes(entries: List[dict]) -> Dict[str, int]:
    return {e["validator"]: int(e["stake"]) for e in entries if int(e["stake"]) > 0}


def _headroom_used(bucket: dict) -> float:
    full = int(bucket["capacity"]) * _PPM
    return (full - int(bucket["level"])) / (full - int(bucket["threshold"]))


def _full(bucket: Optional[dict]) -> bool:
    return bucket is not None and int(bucket["level"]) == int(bucket["capacity"]) * _PPM


def _claims(report: dict, family: str, target: str) -> List[dict]:
    return [
        c
        for c in report["finalizedState"]["claims"]
        if c["family"] == family and c["target"] == target
    ]


@dataclass
class _Run:
    """Polls the reporters' liveness reports. On every poll no reporter may
    see a producer drained, or any protected validator offered, claimed or
    ejected. Logs when each step is first seen."""

    shard: Shard
    timeouts: object
    reporters: List[str]
    producers: Set[str]
    protected: Set[str]
    paused: Set[str] = field(default_factory=set)
    started: float = field(default_factory=time.monotonic)
    steps: Dict[str, tuple] = field(default_factory=dict)
    tightest: Dict[str, float] = field(default_factory=dict)
    samples: List[dict] = field(default_factory=list)
    earnings: Dict[str, List[dict]] = field(default_factory=dict)

    @property
    def observer(self):
        return self.shard.readonly

    def live(self) -> list:
        paused = [self.shard.node(name) for name in self.paused]
        return [n for n in self.shard.all_nodes if all(n is not p for p in paused)]

    def pause(self, name: str) -> None:
        self.shard.node(name).pause()
        self.paused.add(name)

    def unpause(self, name: str) -> None:
        self.shard.node(name).unpause()
        self.paused.discard(name)

    def reports(self) -> List[dict]:
        reports = [self.shard.node(name).validator_liveness() for name in self.reporters]
        for name, report in zip(self.reporters, reports):
            offers = set((report["decision"] or {}).get("ejectionOffers", []))
            claimed = {c["target"] for c in report["localView"]["openEjectionClaims"]} | {
                c["target"] for c in report["finalizedState"]["claims"] if c["family"] == "eject"
            }
            ejected = set(report["finalizedState"]["ejected"])
            for key in self.protected:
                who = _NAME[key]
                _require(key not in offers, f"{name} offers {who} for ejection")
                _require(key not in claimed, f"{name} sees an ejection claim against {who}")
                _require(key not in ejected, f"{name} sees {who} ejected")
            for key in self.producers:
                bucket = _bucket(report, key)
                if bucket is None:
                    continue
                _require(
                    not bucket["drained"], f"{name} sees producer {_NAME[key]} drained: {bucket}"
                )
                margin = int(bucket["level"]) / int(bucket["threshold"])
                self.tightest[key] = min(self.tightest.get(key, margin), margin)
        return reports

    def step(
        self,
        name: str,
        seen: Callable[[List[dict]], bool],
        interval: Optional[float] = None,
    ) -> List[dict]:
        def probe():
            try:
                current = self.reports()
                return current if seen(current) else None
            except _ViolationError as violation:
                return violation

        current = poll_until(
            probe,
            timeout=self.timeouts.blocks(_STEP_BLOCKS),
            interval=interval or self.timeouts.poll_interval,
            description=f"liveness step {name}",
        )
        if isinstance(current, _ViolationError):
            raise current
        tip = max(r["localView"]["tipHeight"] for r in current)
        self.steps[name] = (round(time.monotonic() - self.started, 1), tip)
        logger.info("liveness step %s at %ss, tip %s", name, *self.steps[name])
        self.measure_earnings()
        return current

    def measure_earnings(self) -> None:
        """Records what each producer earned since the last step, as
        validator 1 sees it: ``e`` is its credit over its drain and holds it
        at 1 or more, ``p`` its blocks over its share of the blocks, and
        ``q`` its blocks' mean reward factor."""
        node = self.shard.node("validator1")
        report = node.validator_liveness()
        view = report["localView"]
        sample = {
            "tip": view["tipHeight"],
            "producers": set(self.producers),
            "members": len(view["tipCommittee"]),
            "buckets": {b["validator"]: b for b in view["buckets"]},
        }
        previous = self.samples[-1] if self.samples else None
        self.samples.append(sample)
        if previous is None:
            return
        heights = sample["tip"] - previous["tip"]
        if heights > _MAX_BLOCKS_DEPTH - 5:
            return
        blocks = [
            b
            for b in node.get_blocks(depth=heights + 5)
            if previous["tip"] < b.blockNumber <= sample["tip"]
        ]
        if len(blocks) < _EARNINGS_MIN_BLOCKS:
            return
        drain_ppm = report["params"]["lambdaPpm"]
        for key in previous["producers"] & sample["producers"]:
            before, after = previous["buckets"].get(key), sample["buckets"].get(key)
            if before is None or after is None or before["capacity"] != after["capacity"]:
                continue
            credited = (
                int(after["level"])
                + int(after["overflow"])
                - int(before["level"])
                - int(before["overflow"])
            )
            drained = int(after["capacity"]) * drain_ppm * len(blocks)
            e = 1 + credited / drained
            own = sum(1 for b in blocks if b.sender.lower() == key.lower())
            p = own * sample["members"] / len(blocks)
            q = e * drain_ppm / (p * _REWARD_PPM) if own else None
            self.earnings.setdefault(key, []).append(
                {"tip": sample["tip"], "blocks": len(blocks), "e": e, "p": p, "q": q}
            )

    def every(
        self, name: str, seen: Callable[[dict], bool], interval: Optional[float] = None
    ) -> List[dict]:
        return self.step(name, lambda rs: all(seen(r) for r in rs), interval)

    def caught_up(self, step: str, node_name: str) -> List[dict]:
        """Waits until ``node_name``'s tip is within 2 heights of every
        reporter's."""

        def seen(reports: List[dict]) -> bool:
            tip = self.shard.node(node_name).validator_liveness()["localView"]["tipHeight"]
            return tip >= min(r["localView"]["tipHeight"] for r in reports) - 2

        return self.step(step, seen)

    def contract(self, report: dict) -> dict:
        """The PoS state at ``report``'s finalized block, read on the observer,
        after checking it agrees with what the report shows."""
        state = report["finalizedState"]
        block_hash = state["blockHash"]
        wait_for_block_visible(self.observer, block_hash, timeout=self.timeouts.finalization * 3)
        pos = self.observer.pos
        read = {
            "bonds": {k: v for k, v in pos.get_bonds(block_hash).items() if v > 0},
            "active": pos.get_active_validators(block_hash),
            "ejected": pos.get_ejected_validators(block_hash),
            "pendingRejoins": pos.get_pending_rejoins(block_hash),
            "ejectionEvidence": pos.get_ejection_evidence(block_hash),
            "freezes": pos.get_freeze_history(block_hash),
            "withdrawers": pos.get_withdrawers(block_hash),
        }
        assert read["bonds"] == _stakes(state["bonds"]), (
            f"bonds at {block_hash[:12]}: contract {read['bonds']}, report {state['bonds']}"
        )
        assert read["active"] == set(state["activeValidators"]), (
            f"active set at {block_hash[:12]}: contract {read['active']}, "
            f"report {state['activeValidators']}"
        )
        assert set(read["ejected"]) == set(state["ejected"]), (
            f"ejected at {block_hash[:12]}: contract {set(read['ejected'])}, "
            f"report {state['ejected']}"
        )
        return read

    def balance(self, address: str, report: dict) -> int:
        block_hash = report["finalizedState"]["blockHash"]
        wait_for_block_visible(self.observer, block_hash, timeout=self.timeouts.finalization * 3)
        return self.observer.get_vault().get_balance(address, block_hash)


def _flap(run: _Run, name: str, depth: Optional[float]) -> None:
    """Pauses A until the deepest reporter sees it ``depth`` of the way from
    full to the threshold, or, at ``None``, until one sees it drained. Then
    resumes it and waits until every reporter sees it full. A stays
    protected throughout, so no reporter may offer, claim or eject it."""
    run.producers.discard(_A)
    run.pause("validator4")

    def reached(reports: List[dict]) -> bool:
        buckets = [_bucket(r, _A) for r in reports]
        if depth is None:
            return any(b["drained"] for b in buckets)
        return max(_headroom_used(b) for b in buckets) >= depth

    paused = run.step(f"{name}_paused", reached, interval=_FLAP_POLL)
    run.unpause("validator4")
    if depth is None:
        below = max(_bucket(r, _A)["blocksBelowThreshold"] for r in paused)
        assert below <= _DIP_RESUME_LIMIT, (
            f"A resumed {below} blocks below the threshold: the test polled too slowly "
            "to resume it well inside the observation window"
        )
    run.every(f"{name}_full", lambda r: _full(_bucket(r, _A)), interval=_FLAP_POLL)
    run.producers.add(_A)


def _rejoin(run: _Run, topup: int) -> int:
    """A's rejoin with ``topup``, signed by A and submitted through
    validator 1. Returns the phlo it cost A."""
    v1 = run.shard.node("validator1")
    deploy_id = v1.pos.rejoin_from_ejection(VALIDATOR4_ID.private_key(), topup)
    block_hash = assert_deploy_block_finalized_on_all_nodes(
        v1, deploy_id, run.live(), run.timeouts.finalization * 3
    )
    result = v1.pos.read_result(deploy_id, block_hash)
    assert result.success, f"the rejoin with topup {topup} did not open: {result}"
    return finalized_deploy_cost(v1, deploy_id, block_hash)


@pytest.fixture
def liveness_shard(provider, timeouts):
    config = ShardConfig(
        bonds=list(_GENESIS),
        heartbeat=True,
        include_readonly=True,
        extra_wallets=[
            (_vault(VALIDATOR4_ID), _FUNDED),
            (_vault(VALIDATOR5_ID), _FUNDED),
            (_vault(VALIDATOR6_ID), _FUNDED),
            (_LOAD_KEY.get_public_key().get_vault_address(), _FUNDED),
            (_LOAD_DESTINATION, _FUNDED),
        ],
        global_cli_options=dict(LIVENESS_PROFILE_CLI_OPTIONS),
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        yield shard
    finally:
        shard.destroy()


@pytest.mark.timeout(3600)
def test_ejection_rejoin_and_slashing_while_validators_join_and_leave(
    liveness_shard, timeouts
) -> None:
    shard = liveness_shard
    run = _Run(
        shard,
        timeouts,
        reporters=["validator1", "validator2", "validator5"],
        producers={_V1, _V2, _LEAVER, _A, _B},
        protected={_V1, _V2, _LEAVER, _A, _B},
    )
    load = BackgroundLoad(
        [shard.node("validator1"), shard.node("validator2")],
        vault_transfers(_LOAD_KEY, _LOAD_DESTINATION, _LOAD_AMOUNT),
        name="liveness-load",
    )
    start = run.reports()[0]
    load_source = _LOAD_KEY.get_public_key().get_vault_address()
    source_before = run.balance(load_source, start)
    destination_before = run.balance(_LOAD_DESTINATION, start)
    load.start()
    try:
        _scenario(run)
    except Exception as exc:
        try:
            forensics = collect_forensics(run.live(), label="validator-liveness")
        except Exception as probe_exc:  # noqa: BLE001 — never mask the real failure
            forensics = f"(forensics unavailable: {type(probe_exc).__name__}: {probe_exc})"
        raise AssertionError(f"{exc}\n{forensics}") from exc
    finally:
        load.stop()

    live = run.live()
    assert not load.errors, f"background load submissions failed: {load.errors[:5]}"
    assert_vault_transfers_reconciled(
        load,
        live,
        _LOAD_KEY,
        _LOAD_DESTINATION,
        _LOAD_AMOUNT,
        source_before,
        destination_before,
        timeouts,
        "liveness-load",
    )
    anchor = common_finalized_anchor(live, timeouts.finalization * 3)
    assert_bonds_map_consistent_across_nodes(
        live,
        anchor,
        {_V1: _STAKE[_V1], _V2: _STAKE[_V2], _A: _STAKE[_A], _JOINER: _JOINER_STAKE},
        timeout=timeouts.finalization,
    )
    logger.info("liveness steps (seconds, tip height): %s", run.steps)
    logger.info(
        "tightest level as a multiple of the threshold: %s",
        {_NAME[k]: round(v, 3) for k, v in run.tightest.items()},
    )
    for key, windows in run.earnings.items():
        qs = [w["q"] for w in windows if w["q"] is not None]
        logger.info(
            "earnings %s over %d windows (%d blocks): e min %.2f median %.2f, "
            "p min %.2f median %.2f, q min %s median %s",
            _NAME[key],
            len(windows),
            sum(w["blocks"] for w in windows),
            min(w["e"] for w in windows),
            statistics.median(w["e"] for w in windows),
            min(w["p"] for w in windows),
            statistics.median(w["p"] for w in windows),
            f"{min(qs):.2f}" if qs else "-",
            f"{statistics.median(qs):.2f}" if qs else "-",
        )


def _scenario(run: _Run) -> None:
    shard = run.shard
    v1 = shard.node("validator1")
    epoch = LIVENESS_PROFILE_EPOCH_LENGTH

    # ── Hold at mixed stakes ────────────────────────────────────────────
    run.every("held", lambda r: r["localView"]["tipHeight"] >= _HOLD_HEIGHTS)
    assert set(run.tightest) == run.producers, "every producer's bucket is reported"

    # ── A flaps short of ejection ───────────────────────────────────────
    for i, depth in enumerate(_FLAP_DEPTHS, start=1):
        _flap(run, f"a_flap{i}", depth)
    for i in range(1, _DIPS + 1):
        _flap(run, f"a_dip{i}", None)

    # ── Join and leave, with A silenced before the boundary that applies them ──
    lfb = v1.last_finalized_block()
    joiner = shard.attach_joiner(VALIDATOR6_ID)
    wait_for_block_visible(joiner, lfb.blockInfo.blockHash, timeout=run.timeouts.finalization * 3)
    run.caught_up("joiner_synced", "validator6")
    run.every("epoch_end", lambda r: r["localView"]["tipHeight"] % epoch in _SILENCE_OFFSET)
    run.pause("validator4")
    run.producers.discard(_A)
    run.protected.discard(_A)
    run.every("epoch_start", lambda r: r["localView"]["tipHeight"] % epoch in _EPOCH_OFFSET)
    v1.pos.bond(VALIDATOR6_ID.private_key(), _JOINER_STAKE)
    shard.node("validator3").pos.withdraw(VALIDATOR3_ID.private_key())

    run.every("a_drained", lambda r: bool((_bucket(r, _A) or {}).get("drained")))
    opened = run.step("a_claim_open", lambda rs: any(_claims(r, "eject", _A) for r in rs))
    claim = next(c for r in opened for c in _claims(r, "eject", _A))
    assert _LEAVER in _stakes(claim["electorate"]), (
        f"the ejection claim against A opened after the leaver left: {claim}"
    )

    ejected = run.every("a_ejected", lambda r: _A in r["finalizedState"]["ejected"])
    grace_ends = {
        g["graceEnd"]
        for r in ejected
        for g in r["finalizedState"]["graceEnds"]
        if g["validator"] == _A
    }
    assert len(grace_ends) == 1, f"honest nodes disagree on A's grace end: {grace_ends}"
    record = run.contract(ejected[0])["ejected"][_A]
    assert record["graceEnd"] in grace_ends, f"contract grace end {record}, reports {grace_ends}"
    assert record["ejectionSnapshot"].get(_LEAVER) == _STAKE[_LEAVER], (
        f"A's ejection electorate holds the leaver: {record['ejectionSnapshot']}"
    )
    run.unpause("validator4")

    joined = run.every(
        "joined",
        lambda r: (
            _stakes(r["finalizedState"]["bonds"]).get(_JOINER) == _JOINER_STAKE
            and _JOINER in r["finalizedState"]["activeValidators"]
        ),
    )
    assert _JOINER in run.contract(joined[0])["active"]
    run.producers.add(_JOINER)
    run.protected.add(_JOINER)

    left = run.every(
        "left",
        lambda r: (
            _LEAVER not in _stakes(r["finalizedState"]["bonds"])
            and _LEAVER in _stakes(r["finalizedState"]["withdrawers"])
        ),
    )
    assert _LEAVER in run.contract(left[0])["withdrawers"]
    run.producers.discard(_LEAVER)

    # ── A's short rejoin is refused ─────────────────────────────────────
    a_vault = _vault(VALIDATOR4_ID)
    a_before = run.balance(a_vault, left[0])
    for report in run.reports():
        shortfall = next(
            int(s["amount"]) for s in report["decision"]["shortfall"] if s["validator"] == _A
        )
        assert shortfall > _SHORT_TOPUP * _PPM, (
            f"a reporter's view leaves A no shortfall for the short topup to miss: {shortfall}"
        )
    short_cost = _rejoin(run, _SHORT_TOPUP)

    def a_refused(report: dict) -> bool:
        _require(
            _A in report["finalizedState"]["ejected"] or bool(_claims(report, "rejoin", _A)),
            "A was reinstated by a rejoin whose topup is below its shortfall",
        )
        return not _claims(report, "rejoin", _A)

    refused = run.every("a_rejoin_refused", a_refused)
    state = run.contract(refused[0])
    assert _A not in state["pendingRejoins"], state["pendingRejoins"]
    assert all(f["to"] is None for f in state["freezes"][_A].values()), (
        f"a refused rejoin leaves A's freeze open: {state['freezes'][_A]}"
    )
    assert a_before - run.balance(a_vault, refused[0]) == short_cost, (
        "a refused rejoin takes only the deploy's phlo from A"
    )

    # ── B goes silent while A's full rejoin is decided ──────────────────
    run.caught_up("a_caught_up", "validator4")
    run.pause("validator5")
    run.reporters.remove("validator5")
    run.producers.discard(_B)
    run.protected.discard(_B)
    full_cost = _rejoin(run, _FULL_TOPUP)

    def a_reinstated(report: dict) -> bool:
        _require(
            _A in _stakes(report["finalizedState"]["bonds"]),
            "A was slashed although every remaining voter could back its paid rejoin",
        )
        return _A not in report["finalizedState"]["ejected"]

    reinstated = run.every("a_reinstated", a_reinstated)
    state = run.contract(reinstated[0])
    closed = [f for f in state["freezes"][_A].values() if f["to"] is not None]
    assert [f["topup"] for f in closed] == [_FULL_TOPUP], (
        f"the full rejoin closes A's freeze with its topup: {state['freezes'][_A]}"
    )
    assert state["bonds"].get(_A) == _STAKE[_A], "a rejoin topup does not change A's bond"
    assert a_before - run.balance(a_vault, reinstated[0]) == short_cost + full_cost + _FULL_TOPUP, (
        "A pays both deploys' phlo and the full topup, nothing more"
    )
    run.protected.add(_A)

    # ── B is ejected, then slashed when its grace ends ──────────────────
    b_ejected = run.every("b_ejected", lambda r: _B in r["finalizedState"]["ejected"])
    coop = par_as_string(run.observer.pos.get_coop_vault()[1])
    coop_before = run.balance(coop, b_ejected[0])
    slashed = run.every("b_slashed", lambda r: _B not in _stakes(r["finalizedState"]["bonds"]))
    state = run.contract(slashed[0])
    assert _B not in state["bonds"], f"B keeps stake after its grace: {state['bonds']}"
    assert run.balance(coop, slashed[0]) - coop_before == _STAKE[_B], (
        "the coop vault receives exactly B's stake"
    )

    # ── A holds once it produces again ──────────────────────────────────
    past = slashed[0]["finalizedState"]["blockNumber"] + epoch
    final = run.every("a_holds", lambda r: r["finalizedState"]["blockNumber"] >= past)
    for report in final:
        bucket = _bucket(report, _A)
        assert bucket is not None and not bucket["drained"], (
            f"reinstated A, producing again, is drained: {bucket}"
        )

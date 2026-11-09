"""A validator that stops proposing is ejected through the leaky bucket.

Runs a four-validator shard under the liveness profile (a fast drain and short
epochs) and silences one validator. The tests follow each step through the
honest validators' liveness reports (``GET /api/validator-liveness`` on the
admin port): the silent validator's bucket drains, it becomes ejectable, a
claim to eject it opens and passes, and it is ejected. Left alone, it is
slashed when its grace ends; rejoining with a topup within grace reinstates it.
The validators that keep producing never drain.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import pytest

from ...infra.config import LIVENESS_PROFILE_CLI_OPTIONS, ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID, VALIDATOR4_ID
from ...infra.polling import poll_until
from ...infra.shard import Shard

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.xdist_group("custom")

_STAKE = 100
_PRODUCERS = ("validator1", "validator2", "validator3")
_SILENT = VALIDATOR4_ID.public_hex
# The scenario takes about a hundred blocks at the profile's parameters: the
# drain and the observation window, the claim's votes and finality, and two
# epochs of grace.
_BLOCKS_BUDGET = 200
# The default genesis wallets fund validators 1-3 only; the silent validator
# pays for its rejoin deploy and topup.
_SILENT_BALANCE = 50_000_000_000_000_000

_REJOIN = """
new rl(`rho:registry:lookup`), poSCh, deployerId(`rho:system:deployerId`), ret in {{
  rl!(`rho:system:pos`, *poSCh) |
  for (@(_, PoS) <- poSCh) {{
    @PoS!("rejoinFromEjection", *deployerId, {topup}, *ret)
  }}
}}
"""


def _profile_shard(provider, timeouts, stakes: List[int]):
    """A shard of validators 1-4 at `stakes`, heartbeat on, under the liveness
    profile, with validator 4's vault funded."""
    identities = (VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID, VALIDATOR4_ID)
    config = ShardConfig(
        bonds=list(zip(identities, stakes)),
        heartbeat=True,
        extra_wallets=[
            (VALIDATOR4_ID.private_key().get_public_key().get_vault_address(), _SILENT_BALANCE)
        ],
        global_cli_options=dict(LIVENESS_PROFILE_CLI_OPTIONS),
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        yield shard
    finally:
        shard.destroy()


@pytest.fixture
def liveness_shard(provider, timeouts):
    yield from _profile_shard(provider, timeouts, [_STAKE] * 4)


@pytest.fixture
def mixed_shard(provider, timeouts):
    yield from _profile_shard(provider, timeouts, [100, 200, 300, 400])


def _bucket(report: dict, validator: str) -> Optional[dict]:
    return next((b for b in report["localView"]["buckets"] if b["validator"] == validator), None)


def _stake(state: dict, validator: str) -> Optional[int]:
    return next((b["stake"] for b in state["bonds"] if b["validator"] == validator), None)


@dataclass
class _Scenario:
    """Polls the producers' liveness reports, holding on every poll that no
    producer is drained, and logs when each step is first seen as (seconds
    since silencing, tip height)."""

    shard: Shard
    timeouts: object
    producer_names: tuple = _PRODUCERS
    started: float = field(default_factory=time.monotonic)
    steps: Dict[str, tuple] = field(default_factory=dict)

    @property
    def producers(self):
        return [self.shard.node(name) for name in self.producer_names]

    def reports(self) -> List[dict]:
        producers = self.producers
        keys = {node.identity.public_hex for node in producers}
        reports = [node.validator_liveness() for node in producers]
        for node, report in zip(producers, reports):
            for key in keys:
                bucket = _bucket(report, key)
                assert bucket is None or not bucket["drained"], (
                    f"{node.name} sees producer {key[:12]} drained: {bucket}"
                )
        return reports

    def step(self, name: str, seen: Callable[[List[dict]], bool]) -> List[dict]:
        def probe() -> Optional[List[dict]]:
            current = self.reports()
            return current if seen(current) else None

        current = poll_until(
            probe,
            timeout=self.timeouts.blocks(_BLOCKS_BUDGET),
            interval=self.timeouts.poll_interval,
            description=f"liveness step {name}",
        )
        if name not in self.steps:
            tip = max(r["localView"]["tipHeight"] for r in current)
            self.steps[name] = (round(time.monotonic() - self.started, 1), tip)
            logger.info("liveness step %s at %ss, tip %s", name, *self.steps[name])
        return current

    def eject_silent(self) -> int:
        """Silences validator 4 and waits until every producer's finalized
        state shows it ejected. Returns the end of its grace."""
        self.shard.node("validator4").pause()
        self.started = time.monotonic()
        self.step(
            "drained",
            lambda rs: all((_bucket(r, _SILENT) or {}).get("drained") for r in rs),
        )
        self.step(
            "offered",
            lambda rs: any(_SILENT in (r["decision"] or {}).get("ejectionOffers", []) for r in rs),
        )
        self.step(
            "claim_open",
            lambda rs: (
                any(
                    c["family"] == "eject" and c["target"] == _SILENT
                    for r in rs
                    for c in r["finalizedState"]["claims"]
                )
                or any(
                    c["target"] == _SILENT for r in rs for c in r["localView"]["openEjectionClaims"]
                )
            ),
        )
        ejected = self.step(
            "ejected",
            lambda rs: all(_SILENT in r["finalizedState"]["ejected"] for r in rs),
        )
        grace_ends = {
            g["graceEnd"]
            for r in ejected
            for g in r["finalizedState"]["graceEnds"]
            if g["validator"] == _SILENT
        }
        assert len(grace_ends) == 1, f"honest nodes disagree on the grace end: {grace_ends}"
        return grace_ends.pop()


def test_a_silent_validator_is_ejected_and_slashed_when_its_grace_ends(
    liveness_shard, timeouts
) -> None:
    scenario = _Scenario(liveness_shard, timeouts)
    scenario.eject_silent()
    scenario.step(
        "slashed",
        lambda rs: all(_stake(r["finalizedState"], _SILENT) == 0 for r in rs),
    )
    logger.info("liveness steps (seconds, tip height): %s", scenario.steps)


def test_an_ejected_validator_that_rejoins_within_grace_is_reinstated(
    liveness_shard, timeouts
) -> None:
    scenario = _Scenario(liveness_shard, timeouts)
    grace_end = scenario.eject_silent()

    # A topup of the whole stake covers any shortfall, so every voter backs it.
    liveness_shard.node("validator1").deploy_string(
        _REJOIN.format(topup=_STAKE), VALIDATOR4_ID.private_key()
    )

    def reinstated(rs: List[dict]) -> bool:
        for r in rs:
            state = r["finalizedState"]
            freezes = next(
                (f["intervals"] for f in state["freezes"] if f["validator"] == _SILENT), []
            )
            if _SILENT in state["ejected"] or not any(
                interval["rejoinedAt"] is not None for interval in freezes
            ):
                return False
        return True

    scenario.step("reinstated", reinstated)
    liveness_shard.node("validator4").unpause()

    scenario.step(
        "past_grace",
        lambda rs: all(r["finalizedState"]["blockNumber"] > grace_end + 10 for r in rs),
    )
    for report in scenario.reports():
        assert (_stake(report["finalizedState"], _SILENT) or 0) > 0, (
            "a reinstated validator keeps its stake"
        )
        bucket = _bucket(report, _SILENT)
        assert bucket is not None and not bucket["drained"], (
            f"a reinstated validator that produces again is not drained: {bucket}"
        )
    logger.info("liveness steps (seconds, tip height): %s", scenario.steps)


# Heights a holding run covers: several times the drain and observation window
# of the profile, so a validator that slipped would have been ejected.
_HOLDING_HEIGHTS = 100


def test_validators_with_mixed_bonds_that_keep_producing_are_never_ejected(
    mixed_shard, timeouts
) -> None:
    names = _PRODUCERS + ("validator4",)
    scenario = _Scenario(mixed_shard, timeouts, producer_names=names)
    keys = [mixed_shard.node(name).identity.public_hex for name in names]
    tightest: Dict[str, float] = {}

    def held(rs: List[dict]) -> bool:
        for report in rs:
            decision = report["decision"] or {}
            assert not decision.get("ejectionOffers"), f"ejection offered: {decision}"
            assert not report["localView"]["openEjectionClaims"], report["localView"]
            state = report["finalizedState"]
            assert not state["ejected"], f"ejected: {state['ejected']}"
            assert not [c for c in state["claims"] if c["family"] == "eject"], state["claims"]
            for key in keys:
                bucket = _bucket(report, key)
                if bucket is not None:
                    margin = int(bucket["level"]) / int(bucket["threshold"])
                    tightest[key] = min(tightest.get(key, margin), margin)
        return all(r["localView"]["tipHeight"] >= _HOLDING_HEIGHTS for r in rs)

    scenario.step("held", held)
    logger.info(
        "tightest level as a multiple of the threshold, by stake: %s",
        {stake: round(tightest[key], 3) for stake, key in zip((100, 200, 300, 400), keys)},
    )
    assert set(tightest) == set(keys), "every validator's bucket was reported"

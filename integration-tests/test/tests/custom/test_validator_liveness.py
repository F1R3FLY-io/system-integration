"""A validator that stops proposing is ejected through the leaky bucket.

Runs a four-validator shard under the liveness profile (a fast drain and short
epochs) and silences one validator. The test follows each step through the
honest validators' liveness reports (``GET /api/validator-liveness`` on the
admin port): the silent validator's bucket drains, it becomes ejectable, a
claim to eject it opens and passes, it is ejected, and when its grace ends it
is slashed. The validators that keep producing never drain.
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


@pytest.fixture(scope="module")
def liveness_shard(provider, timeouts):
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, _STAKE),
            (VALIDATOR2_ID, _STAKE),
            (VALIDATOR3_ID, _STAKE),
            (VALIDATOR4_ID, _STAKE),
        ],
        heartbeat=True,
        global_cli_options=dict(LIVENESS_PROFILE_CLI_OPTIONS),
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        yield shard
    finally:
        shard.destroy()


@dataclass
class _Progress:
    """When each step was first seen, as (seconds since silencing, tip height)."""

    started: float = field(default_factory=time.monotonic)
    steps: Dict[str, tuple] = field(default_factory=dict)

    def mark(self, step: str, tip_height: int) -> None:
        if step not in self.steps:
            self.steps[step] = (round(time.monotonic() - self.started, 1), tip_height)
            logger.info("liveness step %s at %ss, tip %s", step, *self.steps[step])


def _bucket(report: dict, validator: str) -> Optional[dict]:
    return next((b for b in report["localView"]["buckets"] if b["validator"] == validator), None)


def _stake(state: dict, validator: str) -> Optional[int]:
    return next((b["stake"] for b in state["bonds"] if b["validator"] == validator), None)


def test_a_silent_validator_is_ejected_and_slashed_when_its_grace_ends(
    liveness_shard, timeouts
) -> None:
    producers = [liveness_shard.node(name) for name in _PRODUCERS]
    producer_keys = {node.identity.public_hex for node in producers}
    liveness_shard.node("validator4").pause()
    progress = _Progress()

    def reports() -> List[dict]:
        reports = [node.validator_liveness() for node in producers]
        for node, report in zip(producers, reports):
            for key in producer_keys:
                bucket = _bucket(report, key)
                assert bucket is None or not bucket["drained"], (
                    f"{node.name} sees producer {key[:12]} drained: {bucket}"
                )
        return reports

    def step(name: str, seen: Callable[[List[dict]], bool]) -> List[dict]:
        def probe() -> Optional[List[dict]]:
            current = reports()
            return current if seen(current) else None

        current = poll_until(
            probe,
            timeout=timeouts.blocks(_BLOCKS_BUDGET),
            interval=timeouts.poll_interval,
            description=f"liveness step {name}",
        )
        progress.mark(name, max(r["localView"]["tipHeight"] for r in current))
        return current

    step(
        "drained",
        lambda rs: all((_bucket(r, _SILENT) or {}).get("drained") for r in rs),
    )
    step(
        "offered",
        lambda rs: any(_SILENT in (r["decision"] or {}).get("ejectionOffers", []) for r in rs),
    )
    step(
        "claim_open",
        lambda rs: (
            any(
                c["family"] == "eject" and c["target"] == _SILENT
                for r in rs
                for c in r["finalizedState"]["claims"]
            )
            or any(c["target"] == _SILENT for r in rs for c in r["localView"]["openEjectionClaims"])
        ),
    )
    ejected = step(
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
    step(
        "slashed",
        lambda rs: all(_stake(r["finalizedState"], _SILENT) == 0 for r in rs),
    )

    logger.info("liveness steps (seconds, tip height): %s", progress.steps)

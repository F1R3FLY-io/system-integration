"""Load-run verdicts: a step fails only when the shard stalls or
breaks; growing depths are pressure, and expiries are outcomes, not failures."""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

load_run = importlib.import_module("test.infra.load_run")


def series(end, half_rise=0.0):
    return {"end": end, "max": end, "slope_per_min": 0.0, "half_rise_per_min": half_rise}


def health(advanced=40, spread=series(1), depth=series(3), backlog=series(4)):
    return {
        "finality": {"advanced": advanced, "secs_per_height": 5.0, "samples": 30},
        "lfb_spread": spread,
        "tip_depth": depth,
        "ingest_backlog": backlog,
        "queued_deploys": series(500, half_rise=40.0),
    }


def stats(offered=6.0, refused=0, errors=()):
    return SimpleNamespace(
        offered_rate=offered, max_start_lag=0.1, errors=list(errors), refused=refused
    )


def test_refusals_and_a_growing_queue_are_not_failures():
    verdict = load_run.judge_step(6.0, stats(refused=400), health())
    assert verdict["ok"] and not verdict["pressure"], verdict


def test_a_stalled_finality_fails_the_step():
    verdict = load_run.judge_step(6.0, stats(), health(advanced=0))
    assert not verdict["ok"]
    assert any("finality stalled" in r for r in verdict["failures"])


def test_a_step_without_health_samples_cannot_pass():
    unmeasured = health()
    unmeasured["finality"] = {"advanced": None, "secs_per_height": None, "samples": 0}
    verdict = load_run.judge_step(6.0, stats(), unmeasured)
    assert not verdict["ok"]
    assert any("unmeasured" in r for r in verdict["failures"])


def test_the_sampler_collects_from_a_live_shard():
    class Node:
        name = "n"

        def __init__(self):
            self.lfb = 100

        def api_get(self, path, timeout):
            self.lfb += 1
            return {"lastFinalizedBlockNumber": self.lfb}

        def get_blocks(self, depth):
            sampler._stop.set()
            return [SimpleNamespace(blockNumber=self.lfb + 2)]

        def http_get(self, path, timeout):
            return SimpleNamespace(
                json=lambda: {"ready": 3, "waiting": 0, "submitted": 2, "in_block": 1}
            )

    node = Node()
    sampler = load_run.HealthSampler([node], node, [node])
    sampler._run()
    window = sampler.window(0, float("inf"))
    assert window["lfb_spread"]["samples"] == 1
    assert window["tip_depth"]["samples"] == 1
    assert sampler._queued_deploys() == 6


def test_a_growing_backlog_is_pressure_not_failure():
    verdict = load_run.judge_step(6.0, stats(), health(backlog=series(49, half_rise=6.0)))
    assert verdict["ok"]
    assert any("ingest_backlog growing" in p for p in verdict["pressure"])


def test_a_submitter_behind_its_rate_makes_the_step_invalid_not_a_stall():
    verdict = load_run.judge_step(6.0, stats(offered=4.0), health())
    assert not verdict["valid"]
    assert verdict["ok"], "the load generator falling behind says nothing about the shard"
    assert verdict["invalid"].startswith("load generator fell behind")


@pytest.mark.parametrize(
    "outcomes, failing",
    [
        ({"expired": 40, "failed": 3, "unsettled": 0}, False),
        ({"expired": 0, "failed": 0, "unsettled": 1}, True),
    ],
)
def test_only_a_deploy_without_an_outcome_fails_the_run(outcomes, failing):
    settlement = {"accepted": 1000, "finalized": 1000 - sum(outcomes.values()), **outcomes}
    assert bool(load_run.settlement_failures(settlement)) == failing


def test_convergence_gets_a_window_at_the_slowest_pace():
    rows = [
        {"health": {"finality": {"secs_per_height": 4.0}}},
        {"health": {"finality": {"secs_per_height": 6.5}}},
        {"health": {"finality": {"secs_per_height": None}}},
    ]
    assert load_run.window_seconds(rows, deploy_lifespan=300, max_parent_depth=25) == 325 * 6.5


def test_an_untimed_ramp_cannot_size_its_convergence():
    with pytest.raises(AssertionError):
        load_run.window_seconds([{"health": {"finality": {"secs_per_height": None}}}], 300, 25)


class FakeDrain:
    """A tracker whose deploys settle at ``settles_at`` seconds (never when
    None), and a shard whose finalized height reads ``heights`` in turn, the
    last one repeating; time passes only while the drain waits."""

    def __init__(self, heights, settles_at=None):
        self.now = 0.0
        self.heights = list(heights)
        self.settles_at = settles_at

    def clock(self):
        return self.now

    def settled(self, timeout):
        if self.settles_at is not None and self.now + timeout >= self.settles_at:
            self.now = max(self.now, self.settles_at)
            return True
        self.now += timeout
        return False

    def finalized_height(self):
        return self.heights.pop(0) if len(self.heights) > 1 else self.heights[0]


def drain(fake, target=400):
    return load_run.drain(
        fake.settled, fake.finalized_height, target, stall_seconds=300, clock=fake.clock
    )


def test_the_drain_ends_once_every_deploy_has_an_outcome():
    fake = FakeDrain(range(100, 1000), settles_at=40)
    assert drain(fake) == (True, None)
    assert fake.now == 40


def test_the_drain_outlasts_slow_heights_until_every_window_has_closed():
    fake = FakeDrain(range(100, 1000), settles_at=None)
    settled, stall = drain(fake)
    assert (settled, stall) == (False, None)
    assert fake.now > 1000, "a time budget would have given up long before the windows closed"


def test_finality_that_stops_during_the_drain_is_a_stall():
    fake = FakeDrain([100, 120, 130], settles_at=None)
    settled, stall = drain(fake)
    assert not settled
    assert "130" in stall


def test_a_clients_wait_runs_from_its_first_attempt_to_finalization():
    def result(first_attempt, submitted, finalized_after):
        record = SimpleNamespace(submit_time=submitted, first_attempt_time=first_attempt)
        return SimpleNamespace(record=record, finalization_time=finalized_after)

    results = [
        result(100.0, 100.0, 30.0),  # accepted first time, finalized at 130
        result(100.0, 400.0, 30.0),  # refused, accepted at 400, finalized at 430
        result(100.0, 100.0, None),  # never finalized
    ]
    assert load_run.client_seconds(results) == [30.0, 330.0]


def test_a_steps_finalized_rate_counts_what_finalized_while_it_ran():
    def result(phase, submitted, finalized_after):
        record = SimpleNamespace(phase=phase, submit_time=submitted)
        return SimpleNamespace(record=record, finalization_time=finalized_after)

    results = [
        result("mix@8", 100.0, 50.0),  # finalized at 150, inside the step
        result("mix@8", 100.0, 250.0),  # finalized at 350, after it
        result("mix@4", 10.0, 120.0),  # an earlier step's deploy finalized at 130
        result("mix@8", 190.0, None),  # never finalized
    ]
    assert load_run.finalized_between(results, 120.0, 220.0) == 2

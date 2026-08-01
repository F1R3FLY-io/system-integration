"""The minimum-attempt floor on `poll_until`.

The upstream `f1r3fly.polling.poll_until` checks its deadline only at the top of
the loop, then sleeps unconditionally. A single probe slower than the whole
budget therefore produces exactly one attempt — a poll loop that never polls:

    TimeoutError: deploy 304402200c11aca2 inclusion on ...validator1:
    timed out after 10s (1 attempts)

That is the real cause of the `test_dag_correctness` failure on f1r3node-rust
PR #178 (run 30672935310), diagnosed by claude-session-9f68c6fa. It is a whole
class of bug rather than one bad number: any timeout on the same order as a
probe's latency degrades identically, and the failure message cannot be
distinguished from a genuine absence of the condition.

These tests pin the floor, and — more importantly — that the floor does not turn
a bounded wait into an unbounded one.
"""

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra.polling import MIN_POLL_ATTEMPTS, poll_until  # noqa: E402


def test_slow_probe_still_gets_the_floor():
    """The regression: one probe longer than the whole budget.

    Budget 1s, probe 1.2s. Upstream would try once and give up. The condition
    here never becomes true, so this measures attempts, not luck.
    """
    attempts = []

    def slow_and_failing():
        attempts.append(time.monotonic())
        time.sleep(1.2)
        return None

    with pytest.raises(TimeoutError) as exc:
        poll_until(slow_and_failing, timeout=1, interval=0, description="slow probe")

    assert len(attempts) >= MIN_POLL_ATTEMPTS, (
        f"a probe slower than the budget got {len(attempts)} attempt(s); "
        f"the floor should guarantee {MIN_POLL_ATTEMPTS}"
    )
    assert f"({len(attempts)} attempts)" in str(exc.value)


def test_fast_probe_is_not_forced_to_the_floor_when_it_succeeds():
    """Success on the first try must return immediately.

    A floor that made every caller wait for N attempts would be a latency
    regression across all 51 `deploy_inclusion` call sites.
    """
    calls = []

    def succeeds_immediately():
        calls.append(1)
        return "found"

    started = time.monotonic()
    result = poll_until(succeeds_immediately, timeout=30, interval=5)

    assert result == "found"
    assert len(calls) == 1, "a satisfied predicate must not be re-polled"
    assert time.monotonic() - started < 1, "returned late despite immediate success"


def test_success_on_a_later_attempt_returns_that_value():
    """The ordinary polling path still works."""
    state = {"n": 0}

    def truthy_on_third():
        state["n"] += 1
        return "ready" if state["n"] >= 3 else None

    assert poll_until(truthy_on_third, timeout=30, interval=0) == "ready"
    assert state["n"] == 3


def test_floor_does_not_make_the_wait_unbounded():
    """The floor must not turn a bounded wait into an open-ended one.

    Once `min_attempts` is satisfied and the deadline has passed, the loop has
    to stop — otherwise a permanently-false condition would poll forever. With
    a fast probe and an already-expired budget, we should see exactly the floor.
    """
    calls = []

    def always_false():
        calls.append(1)
        return None

    with pytest.raises(TimeoutError):
        poll_until(always_false, timeout=0, interval=0, description="never true")

    assert len(calls) == MIN_POLL_ATTEMPTS, (
        f"expected exactly the floor ({MIN_POLL_ATTEMPTS}) attempts once the "
        f"deadline had passed, got {len(calls)}"
    )


def test_exceptions_are_retried_and_the_last_one_is_reported():
    """A raising predicate must not escape — the client contract, preserved."""
    calls = []

    def raises():
        calls.append(1)
        raise RuntimeError(f"probe failure {len(calls)}")

    with pytest.raises(TimeoutError) as exc:
        poll_until(raises, timeout=0, interval=0, description="raising probe")

    assert len(calls) == MIN_POLL_ATTEMPTS
    assert "probe failure" in str(exc.value), "last error must reach the message"


def test_min_attempts_is_caller_overridable():
    """A caller that genuinely wants one shot can still ask for it."""
    calls = []

    def always_false():
        calls.append(1)
        return None

    with pytest.raises(TimeoutError):
        poll_until(always_false, timeout=0, interval=0, min_attempts=1)

    assert len(calls) == 1


def test_deploy_inclusion_is_not_an_outlier_among_timeouts():
    """The calibration half of the fix.

    `deploy_inclusion` gates on block production, so its floor is heartbeat
    cadence rather than network latency. At 10s it was 3x smaller than the next
    smallest timeout, which is what put it within reach of one probe's latency.
    """
    from test.infra.config import TimeoutConfig  # noqa: PLC0415

    cfg = TimeoutConfig()
    siblings = [cfg.finalization, cfg.command, cfg.port_release, cfg.epoch_transition]

    assert cfg.deploy_inclusion >= min(siblings), (
        f"deploy_inclusion={cfg.deploy_inclusion} is below every sibling "
        f"timeout {sorted(siblings)}; it gates on block production and should "
        f"not be the smallest budget in the config"
    )

"""How the load module treats a deploy queue's refusal: recognized only when
the queue names itself, retried after the wait it asks for, and counted
apart from errors."""

import importlib
import sys
import time
from pathlib import Path

import grpc
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

load = importlib.import_module("test.infra.load")


class FakeRpcError(grpc.RpcError):
    def __init__(self, code, details, metadata=()):
        self._code = code
        self._details = details
        self._metadata = metadata

    def code(self):
        return self._code

    def details(self):
        return self._details

    def trailing_metadata(self):
        return self._metadata


FULL = FakeRpcError(
    grpc.StatusCode.RESOURCE_EXHAUSTED,
    "deploy queue is full: the deploys ahead of this one would not land; retry in 7s",
    (("retry-after", "7"),),
)


def test_a_queue_refusal_carries_its_wait():
    refusal = load.queue_refusal(FULL)
    assert isinstance(refusal, load.QueueRefusedError)
    assert refusal.retry_after == 7.0


def test_a_refusal_without_a_hint_waits_the_default():
    refusal = load.queue_refusal(
        FakeRpcError(grpc.StatusCode.UNAVAILABLE, "deploy queue is not connected to its node")
    )
    assert refusal.retry_after == load.DEFAULT_REFUSAL_WAIT_SECONDS


@pytest.mark.parametrize(
    "error",
    [
        FakeRpcError(grpc.StatusCode.UNAVAILABLE, "failed to connect to all addresses"),
        FakeRpcError(grpc.StatusCode.INTERNAL, "deploy queue store failed"),
        RuntimeError("deploy queue is full"),
    ],
)
def test_other_failures_are_not_refusals(error):
    assert load.queue_refusal(error) is None


class RefusingNode:
    name = "validator1"

    def __init__(self, refusals):
        self.refusals = refusals
        self.calls = 0

    def deploy_string(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.refusals:
            raise FULL
        return "deploy-id"


class FixedVabn:
    def get(self):
        return 10

    def advance_to(self, height):
        return height - 1


def test_a_refused_deploy_is_resubmitted_after_its_wait(monkeypatch):
    waits = []
    monkeypatch.setattr(load.time, "sleep", waits.append)
    node = RefusingNode(refusals=2)
    assert load.submit_deploy(node, None, "Nil", 1000, FixedVabn()) == "deploy-id"
    assert node.calls == 3
    assert waits == [7.0, 7.0]


def test_without_retries_the_refusal_is_raised_at_once(monkeypatch):
    monkeypatch.setattr(load.time, "sleep", lambda _: pytest.fail("must not wait"))
    with pytest.raises(load.QueueRefusedError):
        load.submit_deploy(
            RefusingNode(refusals=1), None, "Nil", 1000, FixedVabn(), retry_refusals=False
        )


def test_patience_runs_out(monkeypatch):
    monkeypatch.setattr(load.time, "sleep", lambda _: None)
    monkeypatch.setattr(load, "REFUSAL_PATIENCE_SECONDS", 10.0)
    with pytest.raises(load.QueueRefusedError):
        load.submit_deploy(RefusingNode(refusals=5), None, "Nil", 1000, FixedVabn())


class ScriptedSubmit:
    """Refuses the first ``refusals`` resubmissions, then accepts."""

    def __init__(self, refusals, retry_after):
        self.refusals = refusals
        self.retry_after = retry_after
        self.calls = 0

    def __call__(self, node, key, term, phlo_limit, vabn, retry_refusals):
        assert retry_refusals is False, "a resubmission must not block on its own retries"
        self.calls += 1
        if self.calls <= self.refusals:
            raise load.QueueRefusedError("deploy queue is full", self.retry_after)
        return "deploy-id"


def light_deploy():
    return load.PlannedDeploy(RefusingNode(0), None, "Nil", 1000, "light")


def resubmit(submit, records, first_attempt=None):
    resubmitter = load.Resubmitter(2, FixedVabn(), records.append, submit=submit)
    first_attempt = time.time() if first_attempt is None else first_attempt
    resubmitter.refused(7, light_deploy(), "mix@8", 0.01, first_attempt)
    assert resubmitter.wait_idle(5)
    resubmitter.close()
    return resubmitter.outcomes["mix@8"]


def test_a_refused_deploy_is_resubmitted_after_its_wait_and_recorded_once_accepted():
    records = []
    first_attempt = time.time() - 30
    outcomes = resubmit(ScriptedSubmit(refusals=1, retry_after=0.01), records, first_attempt)
    assert [(r.deploy_id, r.index, r.phase, r.refusals) for r in records] == [
        ("deploy-id", 7, "mix@8", 2)
    ]
    assert records[0].first_attempt == first_attempt
    assert records[0].submit_time > first_attempt
    assert outcomes == {"accepted": 1, "abandoned": 0, "error": 0, "attempts": 2}


def test_a_deploy_refused_past_the_clients_patience_is_abandoned(monkeypatch):
    monkeypatch.setattr(load, "REFUSAL_PATIENCE_SECONDS", 0.05)
    records = []
    outcomes = resubmit(ScriptedSubmit(refusals=10**6, retry_after=0.02), records)
    assert records == []
    assert outcomes["abandoned"] == 1 and outcomes["accepted"] == 0


def test_a_first_refusal_waiting_past_the_clients_patience_is_abandoned_at_once(monkeypatch):
    monkeypatch.setattr(load, "REFUSAL_PATIENCE_SECONDS", 0.05)
    submit = ScriptedSubmit(refusals=0, retry_after=0)
    resubmitter = load.Resubmitter(2, FixedVabn(), [].append, submit=submit)
    resubmitter.refused(7, light_deploy(), "mix@8", 3600, time.time())
    assert resubmitter.wait_idle(1)
    resubmitter.close()
    assert submit.calls == 0
    assert resubmitter.outcomes["mix@8"] == {
        "accepted": 0,
        "abandoned": 1,
        "error": 0,
        "attempts": 0,
    }


def test_closing_abandons_deploys_still_waiting_to_be_resubmitted():
    submit = ScriptedSubmit(refusals=0, retry_after=0)
    resubmitter = load.Resubmitter(2, FixedVabn(), [].append, submit=submit)
    resubmitter.refused(7, light_deploy(), "mix@8", 60, time.time())
    resubmitter.close()
    assert submit.calls == 0
    assert resubmitter.outcomes["mix@8"]["abandoned"] == 1
    assert resubmitter.wait_idle(0)


def test_refusals_count_apart_from_errors_and_into_the_offered_rate():
    stats = load.SubmitStats(target_rate=2.0, duration=10.0, submitted=12, refused=8)
    assert stats.achieved_rate == pytest.approx(1.2)
    assert stats.offered_rate == pytest.approx(2.0)
    assert stats.errors == []

"""Retry policy for signed load and capacity setup deploys."""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest
from f1r3fly import util as sdk_util
from f1r3fly.client import DeployAdmissionRefused, DeploySubmissionUnknown
from f1r3fly.pb.CasperMessage_pb2 import DeployDataProto

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

load = importlib.import_module("test.infra.load")
workloads = importlib.import_module("test.infra.workloads")


class ScriptedNode:
    name = "validator1"

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.sent = []

    def send_deploy(self, deploy):
        self.sent.append(DeployDataProto.FromString(deploy.SerializeToString()))
        response = next(self.responses, None)
        if response is not None:
            response(deploy)
        return deploy.sig.hex()


class FixedVabn:
    def __init__(self):
        self.refreshes = []
        self.advances = []

    def get(self, *, refreshed_after=None):
        if refreshed_after is not None:
            self.refreshes.append(refreshed_after)
            return 20
        return 10

    def advance_to(self, height):
        self.advances.append(height)
        return height - 1


def refuse(cause, retry_after=7.0, deploy_id=None, code="deploy_queue_full"):
    def reject(deploy):
        raise DeployAdmissionRefused(
            deploy_id=deploy_id or deploy.sig.hex(),
            code=code,
            message="queue refused this deploy",
            retryable=cause == "deployer_share" or code == "node_unavailable",
            retry_after=retry_after,
            congestion=cause,
        )

    return reject


def expire(deploy_id=None):
    def reject(deploy):
        raise DeployAdmissionRefused(
            deploy_id=deploy_id or deploy.sig.hex(),
            code="deploy_expired",
            message=(
                "deploy_expired: validAfterBlockNumber 10 has expired at block 70 "
                "with deploy lifespan 50."
            ),
        )

    return reject


@pytest.fixture
def retry_clock(monkeypatch):
    class Clock:
        now = 0.0

        def __init__(self):
            self.waits = []

        def time(self):
            return self.now

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.waits.append(seconds)
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(load, "time", clock)
    return clock


@pytest.mark.parametrize(
    "cause, code",
    [
        ("deployer_share", "deploy_queue_full"),
        (None, "node_unavailable"),
    ],
)
def test_same_deploy_is_sent_after_queue_wait(cause, code, retry_clock):
    node = ScriptedNode(refuse(cause, code=code), refuse(cause, code=code))
    vabn = FixedVabn()
    deploy_id = load.submit_deploy(node, load.sender_key(0), "Nil", 1000, vabn)
    assert len(node.sent) == 3
    assert len({deploy.SerializeToString() for deploy in node.sent}) == 1
    assert deploy_id == node.sent[0].sig.hex()
    assert retry_clock.waits == [7.0, 7.0]
    assert not vabn.refreshes


@pytest.mark.parametrize("cause", ["validator", "validator_idle", "shard", "storage"])
def test_capacity_refusal_refreshes_height_and_signs_again(cause, retry_clock):
    node = ScriptedNode(refuse(cause))
    vabn = FixedVabn()
    deploy_id = load.submit_deploy(node, load.sender_key(0), "Nil", 1000, vabn)
    first, second = node.sent
    assert retry_clock.waits == [7.0]
    assert len(vabn.refreshes) == 1
    assert (first.validAfterBlockNumber, second.validAfterBlockNumber) == (10, 20)
    assert deploy_id == second.sig.hex()
    assert first.sig != second.sig


def test_capacity_refusal_signs_new_id_at_same_tip_and_millisecond(monkeypatch, retry_clock):
    class UnchangedTip(FixedVabn):
        def get(self, *, refreshed_after=None):
            super().get(refreshed_after=refreshed_after)
            return 10

    monkeypatch.setattr(sdk_util, "time", SimpleNamespace(time_ns=lambda: 123_000_000))
    node = ScriptedNode(refuse("storage", retry_after=0.0))
    deploy_id = load.submit_deploy(node, load.sender_key(410), "Nil", 1000, UnchangedTip())
    first, second = node.sent
    assert retry_clock.waits == [0.0]
    assert first.validAfterBlockNumber == second.validAfterBlockNumber == 10
    assert second.timestamp > first.timestamp
    assert first.sig != second.sig
    assert deploy_id == second.sig.hex()


@pytest.mark.parametrize("cause", [None, "future_cause"])
def test_queue_refusal_with_no_recognized_cause_is_not_retried(cause, retry_clock):
    node = ScriptedNode(refuse(cause))
    with pytest.raises(load.QueueRefusedError) as caught:
        load.submit_deploy(node, load.sender_key(0), "Nil", 1000, FixedVabn())
    assert isinstance(caught.value.__cause__, DeployAdmissionRefused)
    assert len(node.sent) == 1
    assert not retry_clock.waits


@pytest.mark.parametrize(
    "response",
    [
        refuse("validator", deploy_id="other"),
        expire(deploy_id="other"),
    ],
)
def test_refusal_for_a_different_deploy_is_not_retried(response, retry_clock):
    node = ScriptedNode(response)
    with pytest.raises(DeployAdmissionRefused):
        load.submit_deploy(node, load.sender_key(0), "Nil", 1000, FixedVabn())
    assert len(node.sent) == 1
    assert not retry_clock.waits


@pytest.mark.parametrize("error_type", [DeploySubmissionUnknown, grpc.RpcError, RuntimeError])
def test_expiration_text_in_ambiguous_failure_cannot_authorize_resigning(error_type, retry_clock):
    def fail(deploy):
        message = (
            "deploy_expired: validAfterBlockNumber 10 has expired at block 70 "
            "with deploy lifespan 50"
        )
        if error_type is DeploySubmissionUnknown:
            raise error_type(deploy.sig.hex(), message)
        raise error_type(message)

    node = ScriptedNode(fail)
    with pytest.raises(error_type):
        load.submit_deploy(node, load.sender_key(0), "Nil", 1000, FixedVabn())
    assert len(node.sent) == 1
    assert not retry_clock.waits


def test_expiry_then_share_refusal_retries_the_replacement_bytes(retry_clock):
    node = ScriptedNode(expire(), refuse("deployer_share"))
    vabn = FixedVabn()
    deploy_id = load.submit_deploy(node, load.sender_key(0), "Nil", 1000, vabn)
    expired, refused, accepted = node.sent
    assert vabn.advances == [70]
    assert expired.validAfterBlockNumber == 10
    assert refused.validAfterBlockNumber == 69
    assert expired.sig != refused.sig
    assert refused.SerializeToString() == accepted.SerializeToString()
    assert deploy_id == accepted.sig.hex()
    assert retry_clock.waits == [7.0]


def test_sync_patience_runs_out_without_an_extra_attempt(monkeypatch, retry_clock):
    monkeypatch.setattr(load, "REFUSAL_PATIENCE_SECONDS", 10.0)
    node = ScriptedNode(refuse("deployer_share"), refuse("deployer_share"))
    with pytest.raises(load.QueueRefusedError):
        load.submit_deploy(node, load.sender_key(0), "Nil", 1000, FixedVabn())
    assert len(node.sent) == 2
    assert retry_clock.waits == [7.0]


def test_workload_setup_uses_original_bytes_for_deployer_share(monkeypatch, retry_clock):
    node = ScriptedNode(refuse("deployer_share"))
    finalized = []
    monkeypatch.setattr(
        workloads,
        "wait_for_deploy_finalized",
        lambda node, deploy_id, timeout: finalized.append((node.name, deploy_id, timeout)),
    )
    setup = workloads.SetupContext(node, load.sender_key(0), [node], 30, FixedVabn())
    (deploy_id,) = setup.deploy_all_and_wait(["Nil"], 1000)
    assert node.sent[0].SerializeToString() == node.sent[1].SerializeToString()
    assert deploy_id == node.sent[1].sig.hex()
    assert finalized == [("validator1", deploy_id, 30)]
    assert retry_clock.waits == [7.0]


def run_open_loop(node, vabn):
    records = []
    resubmitter = load.Resubmitter(2, vabn, records.append)
    try:
        stats = load.OpenLoopSubmitter(2).run(
            rate=1,
            duration=1,
            first_index=7,
            plan=lambda index: load.PlannedDeploy(
                node, load.sender_key(index), "Nil", 1000, "light"
            ),
            vabn=vabn,
            phase="mix@8",
            on_record=records.append,
            retry_refusals=False,
            on_refused=resubmitter.refused,
        )
        assert resubmitter.wait_idle(5)
    finally:
        resubmitter.close()
    return stats, records, resubmitter.outcomes.get("mix@8", {})


def test_open_loop_carries_signed_bytes_until_cause_requires_resigning():
    node = ScriptedNode(refuse("deployer_share", 0.01), refuse("storage", 0.01))
    stats, records, outcomes = run_open_loop(node, FixedVabn())
    first, second, third = node.sent
    assert first.SerializeToString() == second.SerializeToString()
    assert (second.validAfterBlockNumber, third.validAfterBlockNumber) == (10, 20)
    assert third.sig != second.sig
    assert stats.refused == 1
    assert [(r.deploy_id, r.index, r.phase, r.refusals) for r in records] == [
        (third.sig.hex(), 7, "mix@8", 2)
    ]
    assert records[0].submit_time - records[0].first_attempt >= 0.02
    assert outcomes == {"accepted": 1, "abandoned": 0, "error": 0, "attempts": 2}


@pytest.mark.parametrize("cause, retry_after", [(None, 0.01), ("deployer_share", 7.0)])
def test_open_loop_abandons_unknown_cause_or_expired_patience(monkeypatch, cause, retry_after):
    monkeypatch.setattr(load, "REFUSAL_PATIENCE_SECONDS", 1.0)
    node = ScriptedNode(refuse(cause, retry_after))
    stats, records, outcomes = run_open_loop(node, FixedVabn())
    assert stats.refused == 1
    assert len(node.sent) == 1
    assert records == []
    assert outcomes == {"accepted": 0, "abandoned": 1, "error": 0, "attempts": 0}


def test_open_loop_never_queues_an_ambiguous_submit():
    def fail(deploy):
        raise DeploySubmissionUnknown(deploy.sig.hex(), "no RPC response")

    node = ScriptedNode(fail)
    stats, records, outcomes = run_open_loop(node, FixedVabn())
    assert stats.refused == 0
    assert len(stats.errors) == 1
    assert node.sent[0].sig.hex() in stats.errors[0]
    assert len(node.sent) == 1
    assert not records
    assert not outcomes


def test_refusals_count_apart_from_errors_and_into_the_offered_rate():
    stats = load.SubmitStats(target_rate=2.0, duration=10.0, submitted=12, refused=8)
    assert stats.achieved_rate == pytest.approx(1.2)
    assert stats.offered_rate == pytest.approx(2.0)
    assert stats.errors == []

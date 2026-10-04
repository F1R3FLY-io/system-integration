import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from f1r3fly.pb.DeployServiceCommon_pb2 import DeployFinalizationStateProto  # noqa: E402
from test.infra import assertions  # noqa: E402
from test.infra.config import TimeoutConfig  # noqa: E402
from test.infra.timeouts import TimeoutHierarchy  # noqa: E402
from test.tests.custom import test_validator_lifecycle as lifecycle  # noqa: E402

TIMING = json.loads(
    (Path(__file__).parent / "fixtures" / "validator-lifecycle-settlement.json").read_text()
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Node:
    def __init__(self, replay, name, index):
        self.replay = replay
        self.name = name
        self.index = index

    def last_finalized_block(self):
        return SimpleNamespace(
            blockInfo=SimpleNamespace(blockNumber=int(self.replay.clock.now / 10))
        )

    def deploy_status(self, signature):
        replay = self.replay
        replay.events.append(("poll", self.name, signature, replay.clock.now))
        available_at, states = replay.records[signature]
        state = states[self.index] if replay.clock.now >= available_at else "Pending"
        return SimpleNamespace(
            state=DeployFinalizationStateProto.Value(f"DEPLOY_STATE_{state.upper()}")
        )


class Replay:
    def __init__(self):
        self.clock = Clock()
        self.nodes = [Node(self, name, i) for i, name in enumerate(TIMING["nodes"])]
        self.records = {}
        self.events = []
        self.budgets = []

    def submitter(self, name, outcomes=("Finalized",), delay=0):
        outcomes = iter(outcomes)

        def submit():
            outcome = next(outcomes)
            states = [outcome] * len(self.nodes) if isinstance(outcome, str) else outcome
            signature = f"{len(self.records) + 1:016x}" * 4
            self.records[signature] = (self.clock.now + delay, states)
            self.events.append(("submit", name, signature, self.clock.now))
            return signature

        return submit

    def settle(self, submits, timeouts=None, **kwargs):
        return lifecycle._submit_pos_until_effective(
            self.nodes,
            submits,
            timeouts or TimeoutHierarchy(TimeoutConfig()),
            label="settlement replay",
            **kwargs,
        )

    @property
    def submissions(self):
        return [(name, sig) for action, name, sig, _ in self.events if action == "submit"]


@pytest.fixture
def replay(monkeypatch):
    replay = Replay()
    monkeypatch.setattr(assertions, "time", replay.clock)
    monkeypatch.setattr(assertions, "collect_forensics", lambda *args, **kwargs: "")
    monkeypatch.setattr(assertions, "_classify", lambda *args: {})

    def resolve(nodes, signatures, timeout, *, label):
        replay.budgets.append(timeout)
        return assertions.resolve_deploy_verdicts(nodes, signatures, timeout, label=label)

    monkeypatch.setattr(lifecycle, "resolve_deploy_verdicts", resolve)
    return replay


def test_recorded_finalization_fits_combined_budget(replay):
    result = replay.settle(
        {"bond": replay.submitter("bond", delay=TIMING["finalized_after_seconds"])}
    )

    assert result == dict(replay.submissions)
    assert replay.budgets == [TIMING["combined_budget_seconds"]]
    assert TIMING["previous_budget_seconds"] < TIMING["finalized_after_seconds"]
    assert TIMING["finalized_after_seconds"] <= replay.clock.now <= replay.budgets[0]
    terminal_polls = {
        name
        for action, name, _, when in replay.events
        if action == "poll" and when >= TIMING["finalized_after_seconds"]
    }
    assert terminal_polls == set(TIMING["nodes"])


@pytest.mark.parametrize(
    "inclusion,finalization,scale,budget", [(30, 45, 2.0, 450), (41, 53, 1.5, 420)]
)
def test_scaled_custom_budget_accepts_later_finalization(
    replay, inclusion, finalization, scale, budget
):
    timeouts = TimeoutHierarchy(
        TimeoutConfig(deploy_inclusion=inclusion, finalization=finalization, scale=scale)
    )

    result = replay.settle({"bond": replay.submitter("bond", delay=300)}, timeouts)

    assert result == dict(replay.submissions)
    assert replay.budgets == [budget]
    assert 300 <= replay.clock.now <= budget


@pytest.mark.parametrize("pending_nodes", [1, 8])
def test_missing_verdict_fails_with_one_shared_deadline_and_no_retry(replay, pending_nodes):
    states = ["Finalized"] * (8 - pending_nodes) + ["Pending"] * pending_nodes

    with pytest.raises(AssertionError, match="no terminal verdict"):
        replay.settle({"bond": replay.submitter("bond", outcomes=[states])})

    assert len(replay.submissions) == 1
    assert replay.clock.now == replay.budgets[0]
    assert {name for action, name, _, _ in replay.events if action == "poll"} == set(
        TIMING["nodes"]
    )


@pytest.mark.parametrize(
    "states,message",
    [
        (["Failed"] * 8, "terminal Failed"),
        (["Finalized"] * 7 + ["Failed"], "terminal Failed"),
        (["Finalized"] * 7 + ["Expired"], "verdict differs across nodes"),
    ],
)
def test_failure_or_disagreement_is_fatal_without_retry(replay, states, message):
    with pytest.raises(AssertionError, match=message):
        replay.settle({"bond": replay.submitter("bond", outcomes=[states])})

    assert len(replay.submissions) == 1


def test_all_mutations_are_submitted_before_verdict_resolution(replay):
    result = replay.settle(
        {"bond": replay.submitter("bond"), "withdraw": replay.submitter("withdraw")}
    )

    assert result == dict(replay.submissions)
    assert [(action, name) for action, name, _, _ in replay.events[:2]] == [
        ("submit", "bond"),
        ("submit", "withdraw"),
    ]
    assert replay.events[2][0] == "poll"


def test_only_expired_mutations_are_resubmitted_with_fresh_signatures(replay):
    result = replay.settle(
        {
            "bond": replay.submitter("bond"),
            "withdraw": replay.submitter("withdraw", outcomes=["Expired", "Finalized"]),
        }
    )

    assert [name for name, _ in replay.submissions] == ["bond", "withdraw", "withdraw"]
    signatures = [sig for _, sig in replay.submissions]
    assert len(set(signatures)) == 3
    assert result == {"bond": signatures[0], "withdraw": signatures[2]}


def test_three_expired_attempts_fail(replay):
    with pytest.raises(AssertionError, match="never took effect in 3 attempts"):
        replay.settle({"bond": replay.submitter("bond", outcomes=["Expired"] * 3)})

    assert [name for name, _ in replay.submissions] == ["bond"] * 3
    assert len({sig for _, sig in replay.submissions}) == 3

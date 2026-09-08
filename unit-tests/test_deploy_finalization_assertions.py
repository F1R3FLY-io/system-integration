"""PR #137 / #138 review: contention tolerance must not hide node disagreement,
missing evidence, or contradictory evidence; finalization polls share one deadline."""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra import assertions, log_events, polling  # noqa: E402

GOOD = "a" * 64
LOST = "b" * 64


def verdict(sig=LOST, state="Expired", rejections: object = 2):
    return json.dumps(
        {
            "message": "deploy lifecycle terminal verdict written",
            "sig": sig,
            "state": state,
            "rejection_count": rejections,
        }
    )


class Node:
    def __init__(self, name, state="Expired", lines=None, log_error=None):
        self.name = name
        self.state = state
        self.lines = [verdict(state=state)] if lines is None else lines
        self.log_error = log_error
        self.scans = 0
        self.polls = []

    def iter_log_lines(self):
        self.scans += 1
        yield from self.lines
        if self.log_error:
            raise OSError(self.log_error)


@pytest.fixture(autouse=True)
def fake_poll(monkeypatch):
    def wait(node, sig, timeout):
        node.polls.append(sig)
        if sig != GOOD and node.state != "Finalized":
            raise TimeoutError(node.state)

    monkeypatch.setattr(polling, "wait_for_deploy_finalized", wait)


def check(nodes, ids=None, floor: int | None = 1):
    return assertions.assert_all_deploys_finalized_on_all_nodes(
        nodes, [GOOD, LOST] if ids is None else ids, 1, contention_floor=floor
    )


def test_strict_success_returns_ids_without_scanning_logs():
    nodes = [Node("a", "Finalized"), Node("b", "Finalized")]
    assert check(nodes, floor=None) == [GOOD, LOST]
    assert all(node.scans == 0 for node in nodes)


@pytest.mark.parametrize("states", [("Finalized", "Expired"), ("Expired", "Finalized")])
def test_mixed_finalization_is_fatal_in_either_node_order(states):
    nodes = [Node("a", states[0]), Node("b", states[1]), Node("c", "Expired")]
    with pytest.raises(AssertionError, match="divergence"):
        check(nodes)
    assert all(LOST in node.polls for node in nodes), "must poll every node, even after failure"


def test_consistent_contention_is_tolerated_and_scans_once(caplog):
    nodes = [Node("a"), Node("b")]
    assert check(nodes) == [GOOD]
    assert [node.scans for node in nodes] == [1, 1]
    assert "1 of 2" in caplog.text
    assert "50.0%" in caplog.text


@pytest.mark.parametrize(
    "lines",
    [
        [verdict(state="Failed")],
        [verdict(rejections=0)],
        [verdict(), f"quarantined_toxic_rejected_buffer=true {LOST}"],
        [verdict(state="Finalized")],
        [verdict(rejections="two")],
        [verdict(rejections=True)],
        [verdict(rejections=-1)],
        [verdict(), verdict(state="Finalized")],
        [verdict(), verdict(state="Finalized")[:-1]],
    ],
)
def test_one_nodes_evidence_cannot_override_another_nodes_loss(lines):
    nodes = [Node("a", lines=lines), Node("b")]
    with pytest.raises(AssertionError):
        check(nodes)


@pytest.mark.parametrize("counts", [(0, 2), (2, 0), (2, 3)])
def test_same_state_verdicts_with_different_rejection_counts_fail_closed(counts):
    """A repeated Expired verdict with a different count is contradictory, in either order."""
    lines = [verdict(rejections=counts[0]), verdict(rejections=counts[1])]
    facts = log_events.collect_deploy_loss_facts([Node("a", lines=lines)], [LOST])
    assert facts[(LOST[:16], "a")]["diagnostics_error"] == "conflicting terminal verdicts"
    with pytest.raises(AssertionError, match="conflicting terminal verdicts"):
        check([Node("a", lines=lines), Node("b")])


def test_identical_repeated_verdict_is_not_a_conflict():
    facts = log_events.collect_deploy_loss_facts([Node("a", lines=[verdict(), verdict()])], [LOST])
    assert "diagnostics_error" not in facts[(LOST[:16], "a")]
    assert facts[(LOST[:16], "a")]["rejection_count"] == 2


def test_every_node_is_polled_concurrently_against_one_deadline(monkeypatch):
    """Serial per-node polling would never release the barrier and would fail."""
    nodes = [Node("a", "Finalized"), Node("b", "Finalized"), Node("c", "Finalized")]
    rendezvous = threading.Barrier(len(nodes), timeout=2.0)
    budgets = []

    def wait(node, sig, timeout):
        node.polls.append(sig)
        budgets.append(timeout)
        rendezvous.wait()

    monkeypatch.setattr(polling, "wait_for_deploy_finalized", wait)
    assert check(nodes, ids=[LOST], floor=None) == [LOST]
    assert budgets == [1, 1, 1], "every node gets the full window, not a shrinking remainder"


def test_lagging_node_inside_the_window_is_not_divergence(monkeypatch):
    nodes = [Node("a", "Finalized"), Node("b", "Finalized")]
    started = time.monotonic()

    def wait(node, sig, timeout):
        node.polls.append(sig)
        if node.name == "b":
            time.sleep(0.3)

    monkeypatch.setattr(polling, "wait_for_deploy_finalized", wait)
    assert check(nodes, ids=[LOST], floor=None) == [LOST]
    assert time.monotonic() - started < 1.0


def test_stalled_poll_is_recorded_as_a_loss_not_a_hang(monkeypatch):
    """A poll that ignores its own timeout cannot pin the test past the deadline."""
    release = threading.Event()

    def wait(node, sig, timeout):
        node.polls.append(sig)
        if node.name == "b":
            release.wait(30)

    monkeypatch.setattr(polling, "wait_for_deploy_finalized", wait)
    started = time.monotonic()
    try:
        with pytest.raises(AssertionError, match="divergence.*not finalized on \\['b'\\]"):
            check([Node("a", "Finalized"), Node("b", "Finalized")], ids=[LOST], floor=None)
        assert time.monotonic() - started < 5.0
    finally:
        release.set()


@pytest.mark.parametrize("lines", [[], [verdict()]])
def test_log_read_failure_is_explicit_even_after_partial_evidence(lines):
    nodes = [Node("a", lines=lines, log_error="log stream unavailable"), Node("b")]
    with pytest.raises(AssertionError, match="diagnostics unavailable"):
        check(nodes)


def test_missing_verdict_is_not_reported_as_proven_starvation():
    with pytest.raises(AssertionError, match="diagnostics unavailable"):
        check([Node("a", lines=[])])


def test_collector_exception_is_explicit(monkeypatch):
    def unavailable(*args):
        raise OSError("log transport")

    monkeypatch.setattr(log_events, "collect_deploy_loss_facts", unavailable)
    with pytest.raises(AssertionError, match="diagnostics unavailable.*OSError"):
        check([Node("a")])


def test_floor_and_strict_mode_still_fail_for_legal_losses():
    with pytest.raises(AssertionError, match="floor is 2"):
        check([Node("a")], floor=2)
    with pytest.raises(AssertionError, match="did not finalize"):
        check([Node("a")], floor=None)


def test_ambiguous_signature_prefix_is_rejected_before_polling():
    nodes = [Node("a")]
    with pytest.raises(ValueError, match="prefix"):
        check(nodes, ids=["a" * 16 + "1" * 48, "a" * 16 + "2" * 48])
    assert not nodes[0].polls


def test_collector_keeps_per_node_facts_and_matches_the_sig_field():
    unrelated = json.loads(verdict("c" * 64, "Finalized"))
    unrelated["other"] = LOST
    nodes = [Node("a", lines=[verdict(), json.dumps(unrelated)]), Node("b", "Failed")]
    facts = log_events.collect_deploy_loss_facts(nodes, [LOST])
    assert facts[(LOST[:16], "a")]["state"] == "Expired"
    assert facts[(LOST[:16], "b")]["state"] == "Failed"


def test_short_log_signatures_are_supported():
    facts = log_events.collect_deploy_loss_facts([Node("a", lines=[verdict(LOST[:16])])], [LOST])
    assert facts[(LOST[:16], "a")]["rejection_count"] == 2


def test_legacy_classifier_reports_both_nodes_without_collapsing_verdicts():
    causes = log_events.classify_deploy_losses([Node("a"), Node("b", "Failed")], [LOST])
    assert "a:" in causes[LOST[:16]]
    assert "b:" in causes[LOST[:16]]
    assert "Failed" in causes[LOST[:16]]


def test_contention_checks_survive_python_optimization():
    namespace = dict(vars(assertions))
    source = Path(assertions.__file__).read_text()
    exec(compile(source, assertions.__file__, "exec", optimize=2), namespace)
    fn = namespace["assert_all_deploys_finalized_on_all_nodes"]
    with pytest.raises(AssertionError):
        fn([Node("a", lines=[verdict(rejections=0)])], [GOOD, LOST], 1, contention_floor=1)
    with pytest.raises(AssertionError, match="floor is 2"):
        fn([Node("a")], [GOOD, LOST], 1, contention_floor=2)

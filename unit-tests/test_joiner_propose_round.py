"""Bounded proposal rounds must align deployment and verify the claimed fork shape."""

import importlib
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))
rounds = importlib.import_module("test.tests.custom.test_joiner_self_proposes_at_epoch_boundary")
IDENTITY = SimpleNamespace(private_key=lambda: "test-key")


def test_all_deployments_finish_before_any_proposal():
    deployed = set()
    lock = threading.Lock()

    def node(index):
        def deploy(*args, **kwargs):
            if index == 0:
                time.sleep(0.03)
            with lock:
                deployed.add(index)

        def propose():
            with lock:
                assert len(deployed) == 3, "proposal started before every deployment finished"
            return str(index)

        return SimpleNamespace(deploy_string=deploy, propose=propose)

    result = rounds._concurrent_propose_round(
        [node(i) for i in range(3)], [IDENTITY] * 3, 0, timeout=1
    )
    assert set(result) == {"0", "1", "2"}


@pytest.mark.parametrize("stalled_stage", ["deploy", "propose"])
def test_stalled_rpc_cannot_pin_the_round_or_worker(stalled_stage):
    release = threading.Event()
    finished = threading.Event()
    workers = []
    proposed = []

    def stall():
        workers.append(threading.current_thread())
        release.wait(2)
        finished.set()

    def deploy(*args, **kwargs):
        if stalled_stage == "deploy":
            stall()

    def propose():
        proposed.append(True)
        if stalled_stage == "propose":
            stall()
        return "block"

    node = SimpleNamespace(deploy_string=deploy, propose=propose)
    started = time.monotonic()
    try:
        with pytest.raises(AssertionError, match="round.*timed out"):
            rounds._concurrent_propose_round([node], [IDENTITY], 2, timeout=0.1)
        assert time.monotonic() - started < 0.8
        assert workers and all(worker.daemon for worker in workers)
    finally:
        release.set()
        if workers:
            assert finished.wait(1)
            for worker in workers:
                worker.join(timeout=1)
    if stalled_stage == "deploy":
        assert not proposed, "an abandoned deployment must not propose after the round fails"


def test_deploy_failure_aborts_barrier_without_proposing():
    proposed = []

    def fail(*args, **kwargs):
        raise OSError("deploy failed")

    bad = SimpleNamespace(deploy_string=fail, propose=lambda: proposed.append(True))
    good = SimpleNamespace(deploy_string=lambda *a, **k: None, propose=bad.propose)
    with pytest.raises(AssertionError, match="deploy failed"):
        rounds._concurrent_propose_round([bad, good], [IDENTITY] * 2, 3, timeout=1)
    assert not proposed


def test_propose_contention_still_returns_successful_blocks():
    def fail():
        raise RuntimeError("competing proposal")

    bad = SimpleNamespace(deploy_string=lambda *a, **k: None, propose=fail)
    good = SimpleNamespace(deploy_string=bad.deploy_string, propose=lambda: "winner")
    assert rounds._concurrent_propose_round([bad, good], [IDENTITY] * 2, 3, timeout=1) == ["winner"]


def test_all_thread_joins_share_one_deadline(monkeypatch):
    elapsed = 0.0
    budgets = []

    class StalledThread:
        def __init__(self, *, target, args, name, daemon):
            self.name = name
            assert daemon

        def start(self):
            pass

        def join(self, *, timeout):
            nonlocal elapsed
            budgets.append(timeout)
            elapsed += timeout

        def is_alive(self):
            return True

    monkeypatch.setattr(rounds, "time", SimpleNamespace(monotonic=lambda: elapsed))
    monkeypatch.setattr(rounds.threading, "Thread", StalledThread)
    with pytest.raises(AssertionError, match="round.*timed out"):
        rounds._concurrent_propose_round([None] * 3, [IDENTITY] * 3, 0, timeout=0.25)
    assert budgets == [0.25, 0, 0], "per-thread timeouts must not multiply the round budget"


def block(sender, block_hash, height=6, parents=("parent",)):
    return SimpleNamespace(
        sender=sender, blockHash=block_hash, blockNumber=height, parentsHashList=list(parents)
    )


def test_sibling_evidence_requires_distinct_senders_hashes_and_shared_parents():
    a = block("a", "a6")
    assert rounds._has_sibling_proposals([a, block("b", "b6")])
    assert not rounds._has_sibling_proposals([a])
    assert not rounds._has_sibling_proposals([a, a])
    assert not rounds._has_sibling_proposals([a, block("a", "other-a6")])
    assert not rounds._has_sibling_proposals([a, block("b", "a6")])
    assert not rounds._has_sibling_proposals([a, block("b", "b7", height=7)])
    assert not rounds._has_sibling_proposals([a, block("b", "b6", parents=("different",))])

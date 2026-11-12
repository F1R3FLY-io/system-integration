import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration-tests"))
observer_retry = importlib.import_module("test.tests.custom.test_observer_missing_block_retry")


def run_observer_scenario(monkeypatch, evidence, *, restored_state="state"):
    shard = MagicMock()
    source = shard.node.return_value
    shard.all_nodes = [source]
    target = SimpleNamespace(blockNumber=5, blockHash="block", postStateHash="state")
    source.last_finalized_block.side_effect = [
        SimpleNamespace(blockInfo=SimpleNamespace(blockNumber=0)),
        SimpleNamespace(blockInfo=target),
    ]
    observer = shard.add_observer.return_value.__enter__.return_value
    observer.logs.return_value = (
        "request_approved_state: start\nLFS Block Requester stream initialized\n" + evidence
    )
    observer.get_block.return_value = SimpleNamespace(
        blockInfo=SimpleNamespace(postStateHash=restored_state)
    )
    timeouts = SimpleNamespace(
        command=1, finalization=1, node_startup=1, deploy_inclusion=1, custom=lambda value: value
    )
    monkeypatch.setattr(observer_retry, "propose_until_included", lambda *args, **kwargs: "block")
    for name in ("wait_for_block_visible", "wait_for_lfb_at_least", "wait_for_node_running"):
        monkeypatch.setattr(observer_retry, name, MagicMock())

    def poll(predicate, *, description, **kwargs):
        result = predicate()
        if not result:
            raise TimeoutError(description)
        return result

    monkeypatch.setattr(observer_retry, "poll_until", poll)
    try:
        observer_retry.test_observer_retries_missing_block_after_peer_returns(shard, timeouts)
    finally:
        source.pause.assert_called_once()
        source.unpause.assert_called_once()
    observer_retry.wait_for_node_running.assert_called_once()
    observer_retry.wait_for_lfb_at_least.assert_any_call(observer, 5, timeout=1)
    observer_retry.wait_for_block_visible.assert_any_call(observer, "block", timeout=3)
    observer.get_block.assert_called_once_with("block")


@pytest.mark.parametrize(
    "evidence",
    [
        '{"message":"floor-cache request failed; retrying","attempt":1}\n'
        '{"message":"floor-cache request failed; retrying","attempt":2}',
        '{"message":"floor-cache request timed out; retrying","attempt":1}',
        "No responses for 2s. Resending requests.",
        "floor-cache channel full or closed",
        "Proceeding without the shipped floor cache",
        "Making a transition to Running",
    ],
)
def test_observer_accepts_retry_or_progress_and_checks_recovered_state(monkeypatch, evidence):
    run_observer_scenario(monkeypatch, evidence)


@pytest.mark.parametrize(
    "evidence",
    [
        "",
        "Casper engine present but Casper not initialized yet",
        "Request timeout: Protocol error. Request timed out after 2000ms",
        "LFS Block Requester completed successfully - all required blocks downloaded",
        "Heartbeat failed; retaining connection",
    ],
)
def test_observer_rejects_logs_without_restore_retry_or_progress(monkeypatch, evidence):
    with pytest.raises(TimeoutError, match="observer retries, degrades loudly, or completes"):
        run_observer_scenario(monkeypatch, evidence)


def test_retry_evidence_does_not_hide_divergent_recovered_state(monkeypatch):
    with pytest.raises(AssertionError):
        run_observer_scenario(
            monkeypatch, "floor-cache request failed; retrying", restored_state="different-state"
        )

"""The scaled ceremony observation window must retain short status probes."""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))
metadata = importlib.import_module("test.tests.standalone.test_token_metadata")


@pytest.mark.parametrize("ready", [False, True])
def test_status_probe_budget_and_ceremony_verdict(monkeypatch, ready):
    elapsed = 0.0
    budgets = []
    destroyed = []

    def clock():
        return elapsed

    def sleep(duration):
        nonlocal elapsed
        elapsed += duration

    def get(url, *, timeout):
        budgets.append(timeout)
        sleep(timeout)
        return SimpleNamespace(status_code=200, json=lambda: {"isReady": ready})

    handle = SimpleNamespace(
        name="validator",
        grpc_host="localhost",
        ports=SimpleNamespace(http=1),
        exit_code=lambda: None,
        logs=lambda: "Mismatch in genesis token",
    )
    provider = SimpleNamespace(
        create_shard=lambda *a, **k: [handle] * 4,
        destroy_shard=lambda handles: destroyed.append(True),
    )
    # Only the test function's local import sees this clock. Do not replace
    # time functions used by pytest, logging, or background threads.
    monkeypatch.setitem(
        sys.modules, "time", SimpleNamespace(time=clock, monotonic=clock, sleep=sleep)
    )
    monkeypatch.setattr(requests, "get", get)
    timeouts = SimpleNamespace(node_startup=10, custom=lambda base: base * 100)
    if ready:
        with pytest.raises(AssertionError, match="reached Running"):
            metadata.test_genesis_validator_with_wrong_token_blocks_ceremony(provider, timeouts)
    else:
        metadata.test_genesis_validator_with_wrong_token_blocks_ceremony(provider, timeouts)
        assert len(budgets) == 2, "a scaled request must not consume the observation window"
    assert budgets and all(0 < budget <= 3 for budget in budgets)
    assert elapsed <= timeouts.node_startup
    assert destroyed == [True]

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.tests.shared import test_cost_accounting  # noqa: E402


class _Node:
    def __init__(self, name, occurrence_hash, state_hash=b"state"):
        self.name = name
        self.occurrence_hash = occurrence_hash
        self.state_hash = state_hash

    def last_finalized_block(self):
        return SimpleNamespace(
            blockInfo=SimpleNamespace(blockHash=self.state_hash.hex(), blockNumber=7)
        )


def _shard(*nodes):
    return SimpleNamespace(all_nodes=nodes, readonly=nodes[-1])


def _status(node, deploy_id, timeout, *, absolute_timeout):
    assert absolute_timeout == 135
    return SimpleNamespace(latestBlockHash=node.occurrence_hash)


def test_finalized_deploy_requires_one_canonical_block_on_every_node(monkeypatch):
    boot = _Node("boot", b"block-a")
    readonly = _Node("readonly", b"block-b")
    monkeypatch.setattr(test_cost_accounting, "wait_for_deploy_finalized", _status)

    with pytest.raises(AssertionError, match="canonical deploy block") as error:
        test_cost_accounting._finalized_on_every_node(
            _shard(boot, readonly),
            "deploy-id",
            45,
            135,
        )

    assert "boot" in str(error.value)
    assert "readonly" in str(error.value)


def test_finalized_deploy_checks_canonical_and_query_states_on_every_node(monkeypatch):
    occurrence_hash = b"canonical-block"
    boot = _Node("boot", occurrence_hash)
    readonly = _Node("readonly", occurrence_hash)
    calls = []
    monkeypatch.setattr(test_cost_accounting, "wait_for_deploy_finalized", _status)
    monkeypatch.setattr(
        test_cost_accounting,
        "assert_all_nodes_agree_on_block",
        lambda nodes, block_hash, timeout: calls.append(
            (tuple(node.name for node in nodes), block_hash, timeout)
        ),
    )

    state_hash, returned_occurrence_hash = test_cost_accounting._finalized_on_every_node(
        _shard(boot, readonly),
        "deploy-id",
        45,
        135,
    )

    assert state_hash == b"state".hex()
    assert returned_occurrence_hash == occurrence_hash.hex()
    assert calls == [
        (("boot", "readonly"), occurrence_hash.hex(), 45),
        (("boot", "readonly"), b"state".hex(), 45),
    ]

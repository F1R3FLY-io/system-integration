"""The remote provider adopts an inventory's shard and never changes it.

Pins, without a node: inventory validation (roles, names, ports, key-file
permissions), adoption's session-id and network-id checks, the refusal of
every operation that would change a remote node, Shard keying for several
observers and for validator10, and test_load's submitter and phase helpers.
"""

import importlib
import sys
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

inventory_mod = importlib.import_module("test.infra.inventory")
remote = importlib.import_module("test.infra.providers.remote")
shard_mod = importlib.import_module("test.infra.shard")
types_mod = importlib.import_module("test.infra.types")
load = importlib.import_module("test.tests.custom.test_load")

ShardInventory = inventory_mod.ShardInventory
InventoryError = inventory_mod.InventoryError
NodeRole = types_mod.NodeRole

KEY_HEX = "a" * 64


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "dev1.hex"
    path.write_text(KEY_HEX + "\n")
    path.chmod(0o600)
    return path


def _doc(key_file, **overrides):
    doc = {
        "shard": "testnet01",
        "network_id": "f1r3fly-testnet01",
        "nodes": [
            {"name": "boot", "role": "boot", "host": "192.0.2.10"},
            {"name": "validator1", "role": "validator", "host": "192.0.2.11"},
            {"name": "validator10", "role": "validator", "host": "192.0.2.12"},
            {"name": "validator2", "role": "validator", "host": "192.0.2.13"},
            {"name": "observer1", "role": "readonly", "host": "192.0.2.20"},
            {"name": "observer2", "role": "readonly", "host": "192.0.2.21", "port_base": 41400},
        ],
        "deployers": [{"name": "dev1", "private_key_file": str(key_file)}],
    }
    doc.update(overrides)
    return doc


def test_parses_a_valid_inventory(key_file):
    inv = ShardInventory.parse(_doc(key_file))
    assert inv.shard == "testnet01"
    assert [n.name for n in inv.validators] == ["validator1", "validator10", "validator2"]
    observer2 = next(n for n in inv.nodes if n.name == "observer2")
    assert observer2.role == NodeRole.READONLY
    assert (observer2.ports.grpc_ext, observer2.ports.http) == (41401, 41403)
    boot = next(n for n in inv.nodes if n.name == "boot")
    assert (boot.ports.grpc_ext, boot.ports.http) == (40401, 40403)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"nodes": [{"name": "observer1", "role": "readonly", "host": "192.0.2.20"}]},
            "at least one node must have role 'validator'",
        ),
        (
            {
                "nodes": [
                    {"name": "v1", "role": "validator", "host": "192.0.2.11"},
                    {"name": "v1", "role": "validator", "host": "192.0.2.12"},
                ]
            },
            "duplicate node names",
        ),
    ],
)
def test_rejects_invalid_inventories(key_file, overrides, message):
    with pytest.raises(InventoryError, match=message):
        ShardInventory.parse(_doc(key_file, **overrides))


def test_rejects_a_key_file_readable_by_others(key_file):
    key_file.chmod(0o644)
    with pytest.raises(InventoryError, match="accessible by group or others") as exc:
        ShardInventory.parse(_doc(key_file))
    assert KEY_HEX not in str(exc.value)


def _provider(key_file, monkeypatch, statuses):
    def fake_status(host, http_port):
        status = statuses.get(host, {"networkId": "f1r3fly-testnet01"})
        if isinstance(status, Exception):
            raise status
        return status

    monkeypatch.setattr(remote, "read_status", fake_status)
    return remote.RemoteProvider(ShardInventory.parse(_doc(key_file)))


def test_adoption_requires_the_inventory_shard(key_file, monkeypatch):
    provider = _provider(key_file, monkeypatch, {})
    with pytest.raises(remote.RemoteShardError, match="does not name the inventory's shard"):
        provider.adopt_session("shard02")
    assert provider.active_handles == []


def test_adoption_reports_every_wrong_or_unreachable_node(key_file, monkeypatch):
    provider = _provider(
        key_file,
        monkeypatch,
        {
            "192.0.2.11": {"networkId": "f1r3fly-shard02"},
            "192.0.2.20": requests.ConnectionError("refused"),
        },
    )
    with pytest.raises(remote.RemoteShardError) as exc:
        provider.adopt_session("testnet01")
    message = str(exc.value)
    assert "validator1 (192.0.2.11): network id 'f1r3fly-shard02'" in message
    assert "observer1 (192.0.2.20): unreachable" in message
    assert provider.active_handles == []


@pytest.mark.parametrize(
    "operation",
    ["restart", "pause", "unpause", "stop", "remove", "exit_code", "resource_usage", "logs"],
)
def test_handles_refuse_to_change_or_inspect_the_node(key_file, monkeypatch, operation):
    handle = _provider(key_file, monkeypatch, {}).adopt_session("testnet01")[0]
    with pytest.raises(remote.RemoteShardError, match="does not control nodes"):
        getattr(handle, operation)()


def test_provider_refuses_to_build_or_destroy(key_file, monkeypatch):
    provider = _provider(key_file, monkeypatch, {})
    handles = provider.adopt_session("testnet01")
    with pytest.raises(remote.RemoteShardError, match="--session-id testnet01"):
        provider.create_shard(None)
    with pytest.raises(remote.RemoteShardError):
        provider.destroy_shard(handles)
    with pytest.raises(remote.RemoteShardError):
        provider.add_node("testnet01", None, handles[0])
    assert provider.keep_running and not provider.node_logs_available


def test_shard_keys_every_observer_and_orders_validators_numerically(key_file, monkeypatch):
    provider = _provider(key_file, monkeypatch, {})
    shard = shard_mod.Shard.from_handles(provider, provider.adopt_session("testnet01"), None, None)
    assert list(shard.named_validators) == ["validator1", "validator2", "validator10"]
    assert shard.node("observer1").role == NodeRole.READONLY
    assert shard.node("observer2").role == NodeRole.READONLY
    assert len(shard.all_nodes) == 6


def test_phases_apply_the_sustained_overrides(monkeypatch):
    monkeypatch.setenv("LOAD_TEST_SUSTAINED_RATE", "8")
    monkeypatch.setenv("LOAD_TEST_SUSTAINED_DURATION", "3600")
    *others, sustained = load._phases()
    assert (sustained.name, sustained.rate, sustained.seconds) == ("sustained", 8.0, 3600.0)
    assert others == load.PHASES[:-1]


def test_remote_runs_only_tests_marked_remote_shard():
    conftest = importlib.import_module("test.conftest")

    class Config:
        stash = {conftest._NODE_CAPABILITIES: frozenset()}

        @staticmethod
        def getoption(name):
            return {"--provider": "remote", "--run-all-node-capability-tests": False}[name]

    class Item:
        def __init__(self, *markers):
            self.nodeid = "test_x.py::test_x"
            self.markers = [pytest.Mark(name, (), {}) for name in markers]

        def iter_markers(self, name=None):
            return (m for m in self.markers if name is None or m.name == name)

        def add_marker(self, marker):
            self.markers.append(marker.mark)

    marked, unmarked = Item("remote_shard"), Item()
    conftest.pytest_collection_modifyitems(Config, [marked, unmarked])
    assert not tuple(marked.iter_markers("skip"))
    (skip,) = tuple(unmarked.iter_markers("skip"))
    assert "remote_shard" in skip.kwargs["reason"]

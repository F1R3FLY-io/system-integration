import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra.config import ShardConfig  # noqa: E402
from test.infra.keys import BOOTSTRAP_NODE_ID  # noqa: E402
from test.infra.providers.base import activate_handles_then_wait  # noqa: E402
from test.infra.providers.docker import DockerProvider  # noqa: E402
from test.infra.providers.subprocess import SubprocessProvider  # noqa: E402
from test.infra.shard import Shard  # noqa: E402
from test.infra.types import NodeRole, PortMapping, ValidatorIdentity  # noqa: E402


class FakeHandle:
    def __init__(self, active_handles, error=None):
        self.name = "node"
        self.grpc_host = "127.0.0.1"
        self.ports = type("Ports", (), {"http": 40403})()
        self.active_handles = active_handles
        self.archived = False
        self.error = error or RuntimeError("readiness failed")

    def logs(self):
        return "startup failed"

    def is_running(self):
        assert self in self.active_handles
        raise self.error

    def archive_log(self, _destination):
        self.archived = True


def test_startup_failure_is_tracked_archived_and_cleaned(tmp_path):
    active_handles = []
    handle = FakeHandle(active_handles)
    cleanup_calls = []

    def cleanup():
        cleanup_calls.append(handle)
        active_handles.remove(handle)

    with pytest.raises(RuntimeError, match="readiness failed"):
        activate_handles_then_wait(
            active_handles,
            [handle],
            tmp_path,
            timeout=1,
            wait_running=True,
            cleanup_on_failure=cleanup,
        )

    assert handle.archived
    assert cleanup_calls == [handle]
    assert active_handles == []


def test_deferred_readiness_still_registers_the_resource(tmp_path):
    active_handles = []
    handle = FakeHandle(active_handles)

    activate_handles_then_wait(
        active_handles,
        [handle],
        tmp_path,
        timeout=0,
        wait_running=False,
        cleanup_on_failure=lambda: pytest.fail("cleanup must not run"),
    )

    assert active_handles == [handle]


def test_startup_interrupt_is_archived_and_rolled_back(tmp_path):
    active_handles = []
    handle = FakeHandle(active_handles, KeyboardInterrupt())

    def cleanup():
        active_handles.remove(handle)

    with pytest.raises(KeyboardInterrupt):
        activate_handles_then_wait(
            active_handles,
            [handle],
            tmp_path,
            timeout=1,
            wait_running=True,
            cleanup_on_failure=cleanup,
        )

    assert handle.archived
    assert active_handles == []


def test_subprocess_shard_bootstrap_is_scoped_to_its_allocated_ports(tmp_path):
    provider = object.__new__(SubprocessProvider)
    provider._session_id = "isolation"
    provider._session_root = tmp_path
    provider._active_handles = []
    provider._shard_counter = 0
    provider._timeouts = SimpleNamespace(node_startup=1)
    provider._paths = SimpleNamespace(
        certs_dir=str(tmp_path / "certs"),
        integration_tests=str(tmp_path),
    )

    allocated = iter([PortMapping.from_base(41700), PortMapping.from_base(41706)])
    provider._ports = SimpleNamespace(allocate=lambda: next(allocated))
    provider._write_genesis = lambda _config, target: target

    spawn_calls = []

    def spawn(**kwargs):
        spawn_calls.append(kwargs)
        return SimpleNamespace(ports=kwargs["ports"], name=kwargs["role_key"])

    provider._spawn = spawn
    identity = ValidatorIdentity(
        name="validator",
        private_hex="11" * 32,
        public_hex="04" + "22" * 64,
    )

    handles = provider.create_shard(
        ShardConfig(bonds=[(identity, 1)], heartbeat=False),
        wait_running=False,
    )

    expected = f"--bootstrap=rnode://{BOOTSTRAP_NODE_ID}@127.0.0.1?protocol=41700&discovery=41704"
    boot_bootstrap_args = [
        arg for arg in spawn_calls[0]["cli_args"] if arg.startswith("--bootstrap=")
    ]
    validator_bootstrap_args = [
        arg for arg in spawn_calls[1]["cli_args"] if arg.startswith("--bootstrap=")
    ]

    assert boot_bootstrap_args == [expected]
    assert validator_bootstrap_args == [expected]
    assert "protocol=40400" not in expected
    assert provider._active_handles == handles


def test_subprocess_shard_propagates_ceremony_threshold_to_readonly(tmp_path):
    provider = object.__new__(SubprocessProvider)
    provider._session_id = "threshold"
    provider._session_root = tmp_path
    provider._active_handles = []
    provider._shard_counter = 0
    provider._timeouts = SimpleNamespace(node_startup=1)
    provider._paths = SimpleNamespace(
        certs_dir=str(tmp_path / "certs"),
        integration_tests=str(tmp_path),
    )
    allocated = iter(PortMapping.from_base(41800 + offset) for offset in (0, 6, 12, 18))
    provider._ports = SimpleNamespace(allocate=lambda: next(allocated))
    provider._write_genesis = lambda _config, target: target
    spawn_calls = []

    def spawn(**kwargs):
        spawn_calls.append(kwargs)
        return SimpleNamespace(ports=kwargs["ports"], name=kwargs["role_key"])

    provider._spawn = spawn
    identities = [
        ValidatorIdentity(
            name=f"validator{index}",
            private_hex=f"{index + 1:02x}" * 32,
            public_hex="04" + f"{index + 2:02x}" * 64,
        )
        for index in range(2)
    ]

    provider.create_shard(
        ShardConfig(
            bonds=[(identity, 1) for identity in identities],
            required_signatures=1,
            heartbeat=False,
            include_readonly=True,
        ),
        wait_running=False,
    )

    assert ["--required-signatures=1" in call["cli_args"] for call in spawn_calls] == [
        True,
        True,
        True,
        True,
    ]


@pytest.mark.parametrize("method_name", ["add_joiner", "add_observer"])
def test_attached_nodes_inherit_the_shard_ceremony_threshold(method_name):
    captured = []
    bootstrap = SimpleNamespace(
        role=NodeRole.BOOTSTRAP,
        name="rnode.test.threshold.boot",
        network_name="threshold-network",
        identity=None,
    )

    class Provider:
        def add_node(self, **kwargs):
            captured.append(kwargs["node_config"])
            return SimpleNamespace(
                role=kwargs["node_config"].role,
                name=f"rnode.test.threshold.{method_name}",
                network_name="threshold-network",
                identity=kwargs["node_config"].identity,
            )

        def remove_node(self, _handle):
            return None

    identity = ValidatorIdentity(
        name="joiner",
        private_hex="11" * 32,
        public_hex="04" + "22" * 64,
    )
    shard = Shard(
        provider=Provider(),
        handles=[bootstrap],
        config=ShardConfig(
            bonds=[(identity, 1), (identity, 1)],
            required_signatures=1,
        ),
        timeouts=SimpleNamespace(),
    )
    context = (
        shard.add_joiner(identity, wait_running=False)
        if method_name == "add_joiner"
        else shard.add_observer(wait_running=False)
    )

    with context:
        pass

    assert captured[0].cli_options["--required-signatures"] == "1"


def test_subprocess_shards_keep_distinct_genesis_directories(tmp_path):
    provider = object.__new__(SubprocessProvider)
    provider._session_id = "isolation"
    provider._session_root = tmp_path
    provider._active_handles = []
    provider._shard_counter = 0
    provider._timeouts = SimpleNamespace(node_startup=1)
    provider._paths = SimpleNamespace(
        certs_dir=str(tmp_path / "certs"),
        integration_tests=str(tmp_path),
    )
    allocated = iter([PortMapping.from_base(12000), PortMapping.from_base(12006)])
    provider._ports = SimpleNamespace(allocate=lambda: next(allocated))
    genesis_directories = []

    def write_genesis(_config, target):
        genesis_directories.append(target)
        return target

    provider._write_genesis = write_genesis
    spawn_calls = []

    def spawn(**kwargs):
        spawn_calls.append(kwargs)
        return SimpleNamespace(ports=kwargs["ports"], name=kwargs["role_key"])

    provider._spawn = spawn
    config = ShardConfig(bonds=[], heartbeat=False, include_readonly=False)

    provider.create_shard(config, wait_running=False)
    provider.create_shard(config, wait_running=False)

    assert genesis_directories == [tmp_path / "genesis-1", tmp_path / "genesis-2"]
    assert [call["config_file"] for call in spawn_calls] == [
        tmp_path / "genesis-1" / "rnode.conf",
        tmp_path / "genesis-2" / "rnode.conf",
    ]
    assert [call["data_subdir"] for call in spawn_calls] == [
        Path("shard1") / "boot",
        Path("shard2") / "boot",
    ]
    assert [call["node_name_key"] for call in spawn_calls] == [
        "shard1.boot",
        "shard2.boot",
    ]


def test_subprocess_adopts_a_namespaced_shard(monkeypatch, tmp_path):
    provider = object.__new__(SubprocessProvider)
    provider._session_id = "replacement"
    provider._paths = SimpleNamespace(integration_tests=str(tmp_path))

    session_dir = tmp_path / ".subprocess-data" / "kept"
    shard_root = session_dir / "shard1"
    for role in ("boot", "validator1", "readonly"):
        (shard_root / role).mkdir(parents=True)

    monkeypatch.setattr(
        "test.infra.providers.subprocess._find_pid_for_data_dir",
        lambda path: {"boot": 101, "validator1": 102, "readonly": 103}.get(path.name),
    )
    monkeypatch.setattr(
        "test.infra.providers.subprocess._ports_from_cmdline",
        lambda pid: PortMapping.from_base(12000 + (pid - 101) * 6),
    )

    handles = provider.adopt_session("kept")

    assert [handle.data_dir for handle in handles] == [
        shard_root / "boot",
        shard_root / "validator1",
        shard_root / "readonly",
    ]
    assert [handle.name for handle in handles] == [
        "rnode.test.kept.shard1.boot",
        "rnode.test.kept.shard1.validator1",
        "rnode.test.kept.shard1.readonly",
    ]
    assert provider._session_id == "kept"
    assert provider._session_root == session_dir


def test_subprocess_rejects_ambiguous_multi_shard_adoption(monkeypatch, tmp_path):
    provider = object.__new__(SubprocessProvider)
    provider._session_id = "replacement"
    provider._paths = SimpleNamespace(integration_tests=str(tmp_path))

    session_dir = tmp_path / ".subprocess-data" / "kept"
    for shard in ("shard1", "shard2"):
        (session_dir / shard / "boot").mkdir(parents=True)

    monkeypatch.setattr(
        "test.infra.providers.subprocess._find_pid_for_data_dir",
        lambda _path: 101,
    )

    with pytest.raises(ValueError, match="multiple live shards: shard1, shard2"):
        provider.adopt_session("kept")


class _RemovableHandle:
    def __init__(self):
        self.name = "rnode.test.session.standalone1"
        self.network_name = "f1r3fly-test-session-standalone1"
        self.ports = object()
        self.archived = False
        self.removed = False

    def archive_log(self, _destination):
        self.archived = True

    def remove(self):
        self.removed = True


def test_docker_failed_startup_force_cleanup_overrides_keep_running(monkeypatch, tmp_path):
    provider = object.__new__(DockerProvider)
    handle = _RemovableHandle()
    provider._active_handles = [handle]
    provider._session_id = "session"
    provider._paths = SimpleNamespace(integration_tests=str(tmp_path))
    provider._registry = SimpleNamespace(keep_running=True, keep_on_failure=False)
    provider._ports = SimpleNamespace(release=lambda _ports: None)
    docker_calls = []
    monkeypatch.setattr(
        "test.infra.providers.docker._docker",
        lambda *args, **kwargs: docker_calls.append(args),
    )

    provider.destroy_standalone(handle)
    assert provider._active_handles == [handle]
    assert not handle.removed

    provider.destroy_standalone(handle, force=True)
    assert provider._active_handles == []
    assert handle.archived
    assert handle.removed
    assert docker_calls == [
        ("volume", "rm", "-f", "test-session-standalone1-data"),
        ("network", "rm", handle.network_name),
    ]


def test_subprocess_failed_startup_force_cleanup_overrides_keep_running(tmp_path):
    provider = object.__new__(SubprocessProvider)
    handle = _RemovableHandle()
    provider._active_handles = [handle]
    provider._session_id = "session"
    provider._paths = SimpleNamespace(integration_tests=str(tmp_path))
    provider._keep_running = True
    provider._ports = SimpleNamespace(release=lambda _ports: None)

    provider.destroy_standalone(handle)
    assert provider._active_handles == [handle]
    assert not handle.removed

    provider.destroy_standalone(handle, force=True)
    assert provider._active_handles == []
    assert handle.archived
    assert handle.removed

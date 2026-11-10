"""A shard with ``deploy_queue`` puts a deploy queue in front of each validator.

The queue serves the validator's client ports (gRPC external and HTTP), so a
test reaches the validator through it with the ports the handle advertises.
The validator moves its own client ports elsewhere and keeps the rest.
"""

import dataclasses
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra.compose import DEPLOY_QUEUE_ENTRYPOINT, generate_compose  # noqa: E402
from test.infra.config import ResourcePaths, ShardConfig, TimeoutConfig  # noqa: E402
from test.infra.keys import VALIDATOR1_ID, VALIDATOR2_ID  # noqa: E402
from test.infra.ports import PortAllocator  # noqa: E402
from test.infra.providers import docker as docker_mod  # noqa: E402
from test.infra.providers import subprocess as subprocess_mod  # noqa: E402
from test.infra.timeouts import TimeoutHierarchy  # noqa: E402
from test.infra.types import PortMapping  # noqa: E402

_BONDS = [(VALIDATOR1_ID, 100), (VALIDATOR2_ID, 100)]


def _queued_compose_services(tmp_path) -> dict:
    registry = SimpleNamespace(
        register_tempfile=lambda p: None,
        register_network=lambda n: None,
        register_container=lambda n: None,
        register_volume=lambda n: None,
    )
    ports = {
        role: PortMapping.from_base(41000 + 10 * i)
        for i, role in enumerate(["boot", "validator1", "validator2"])
    }
    path = generate_compose(
        config=ShardConfig(bonds=_BONDS, deploy_queue=True),
        genesis_dir=str(tmp_path),
        port_assignments=ports,
        scope="unit-s1",
        paths=ResourcePaths.resolve(),
        registry=registry,
    )
    try:
        return yaml.safe_load(Path(path).read_text())["services"]
    finally:
        Path(path).unlink()


def _published(service: dict) -> dict:
    """Container port -> host port."""
    return {int(p.split(":")[1]): int(p.split(":")[0]) for p in service.get("ports", [])}


def test_compose_queue_takes_each_validators_client_ports(tmp_path):
    services = _queued_compose_services(tmp_path)

    assert set(services) == {"boot", "validator1", "validator2", "deploy-queue1", "deploy-queue2"}
    for slot in (1, 2):
        validator = services[f"validator{slot}"]
        queue = services[f"deploy-queue{slot}"]
        base = 41000 + 10 * slot
        assert _published(validator) == {
            40400: base,
            40402: base + 2,
            40404: base + 4,
            40405: base + 5,
        }
        assert _published(queue) == {40401: base + 1, 40403: base + 3}
        assert queue["entrypoint"] == [DEPLOY_QUEUE_ENTRYPOINT]
        node = f"rnode.test.unit-s1.validator{slot}"
        assert f"--node-grpc=http://{node}:40401" in queue["command"]
        assert f"--node-http=http://{node}:40403" in queue["command"]
        assert queue["depends_on"] == [f"validator{slot}"]


class _ExitedProcess:
    """A Popen stand-in that has already exited, so teardown sends no signal."""

    pid = 0

    def __init__(self, cmd, **kwargs):
        self.args = cmd

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _provider(tmp_path, monkeypatch):
    for name in ("node", "deploy-queue"):
        binary = tmp_path / name
        binary.write_text("")
        binary.chmod(0o755)
    monkeypatch.setenv("F1R3FLY_NODE_BINARY", str(tmp_path / "node"))
    monkeypatch.setenv("F1R3FLY_DEPLOY_QUEUE_BINARY", str(tmp_path / "deploy-queue"))
    launched = []

    def popen(cmd, **kwargs):
        launched.append(cmd)
        return _ExitedProcess(cmd, **kwargs)

    monkeypatch.setattr(subprocess_mod.subprocess, "Popen", popen)
    paths = dataclasses.replace(ResourcePaths.resolve(), integration_tests=str(tmp_path))
    allocator = PortAllocator(base=45000, ceiling=45600)
    provider = subprocess_mod.SubprocessProvider(
        port_allocator=allocator,
        session_id="unit",
        keep_running=False,
        timeouts=TimeoutHierarchy(TimeoutConfig()),
        paths=paths,
    )
    return provider, allocator, launched


def _flag(cmd, name: str) -> str:
    if name in cmd:
        return cmd[cmd.index(name) + 1]
    return next(a.split("=", 1)[1] for a in cmd if a.startswith(f"{name}="))


def test_subprocess_queue_serves_the_ports_the_validator_advertises(tmp_path, monkeypatch):
    provider, allocator, launched = _provider(tmp_path, monkeypatch)

    handles = provider.create_shard(
        ShardConfig(bonds=_BONDS, deploy_queue=True), wait_running=False
    )

    validators = [h for h in handles if h.name.endswith(("validator1", "validator2"))]
    nodes = {_flag(c, "--data-dir").rsplit("/", 1)[-1]: c for c in launched if "run" in c}
    queues = {_flag(c, "--data-dir").rsplit("/", 1)[-1]: c for c in launched if "run" not in c}
    assert set(queues) == {"deploy-queue1", "deploy-queue2"}
    for slot, handle in enumerate(validators, start=1):
        node, queue = nodes[f"validator{slot}"], queues[f"deploy-queue{slot}"]
        node_grpc = int(_flag(node, "--api-port-grpc-external"))
        node_http = int(_flag(node, "--api-port-http"))
        assert (node_grpc, node_http) != (handle.ports.grpc_ext, handle.ports.http)
        assert int(_flag(node, "--protocol-port")) == handle.ports.protocol
        assert _flag(queue, "--grpc-listen") == f"127.0.0.1:{handle.ports.grpc_ext}"
        assert _flag(queue, "--http-listen") == f"127.0.0.1:{handle.ports.http}"
        assert _flag(queue, "--node-grpc") == f"http://127.0.0.1:{node_grpc}"
        assert _flag(queue, "--node-http") == f"http://127.0.0.1:{node_http}"

    in_use = len(allocator._in_use)
    provider.destroy_shard(handles)
    assert in_use == len(handles) + 2
    assert allocator._in_use == set()


def test_adopted_validator_takes_client_ports_from_its_running_queue(tmp_path):
    queue_dir = tmp_path / "deploy-queue1"
    queue_dir.mkdir()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            f"--data-dir={queue_dir}",
            "--grpc-listen=127.0.0.1:45101",
            "--http-listen=127.0.0.1:45103",
        ]
    )
    try:
        ports = subprocess_mod._with_queue_client_ports(PortMapping.from_base(45200), queue_dir)
    finally:
        proc.kill()
        proc.wait()

    assert ports == dataclasses.replace(PortMapping.from_base(45200), grpc_ext=45101, http=45103)


def test_adopted_docker_validator_reads_client_ports_from_its_queue(monkeypatch):
    published = {
        "v": {40400: 1, 40401: 2, 40402: 3, 40403: 4, 40404: 5, 40405: 6},
        "q": {40401: 12, 40403: 14},
    }

    def fake_docker(*args, **kwargs):
        _, container, port = args
        host = published[container].get(int(port.split("/")[0]))
        return SimpleNamespace(stdout=f"0.0.0.0:{host}\n" if host else "", returncode=0)

    monkeypatch.setattr(docker_mod, "_docker", fake_docker)

    assert docker_mod._inspect_port_mapping("v", client_container="q") == PortMapping(
        protocol=1, grpc_ext=12, grpc_int=3, http=14, discovery=5, admin=6
    )
    assert docker_mod._inspect_port_mapping("v").grpc_ext == 2

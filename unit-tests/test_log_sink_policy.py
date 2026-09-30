"""One node log sink per deployment (f1r3node-rust TASK-020-3).

`sink = "both"` wrote every node log line twice — once to the container's
json-file buffer and once to the rotating file sink — doubling disk use on a
soak run. Each deployment now selects one sink, matched to its reader:

- Compose deployments (compose/*.yml, smoke-test, `shardctl wait` / `logs`)
  read `docker logs`, so conf/rust.conf and conf/standalone-dev.conf select
  `stdout`, bounded by the json-file cap (100m x 3).
- The integration harness reads the rotated file sink (DockerNodeHandle.logs
  / archive_log), so every node it launches gets `--log-sink=file` as a root
  argument. The node CLI rejects the flag after `run`, so position matters.
- `both` is a development override only, never a checked-in default.
"""

import ast
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra import compose as compose_mod  # noqa: E402
from test.infra.compose import NODE_LOG_SINK_ARGS, generate_compose  # noqa: E402
from test.infra.config import ResourcePaths, ShardConfig  # noqa: E402
from test.infra.keys import VALIDATOR1_ID, VALIDATOR2_ID  # noqa: E402
from test.infra.providers import docker as docker_mod  # noqa: E402
from test.infra.providers.docker import DockerNodeHandle  # noqa: E402
from test.infra.types import NodeRole, PortMapping  # noqa: E402

COMPOSE_DIR = REPO_ROOT / "compose"
DEPLOYMENT_CONFS = ["conf/rust.conf", "conf/standalone-dev.conf"]


def _logging_sink(conf_text: str) -> str:
    """The `sink` value inside the top-level `logging { ... }` block."""
    m = re.search(r"^logging\s*\{(.*?)^\}", conf_text, re.S | re.M)
    assert m, "no top-level logging block"
    sinks = re.findall(r'^\s*sink\s*=\s*"(\w+)"', m.group(1), re.M)
    assert len(sinks) == 1, f"expected one sink setting, found {sinks}"
    return sinks[0]


def _node_services():
    """(file, name, service) for every service running the node image."""
    out = []
    for path in sorted(COMPOSE_DIR.glob("*.yml")):
        services = (yaml.safe_load(path.read_text()) or {}).get("services") or {}
        for name, svc in services.items():
            if "f1r3fly-rust" in str(svc.get("image", "")):
                out.append((path.name, name, svc))
    return out


def _argv(value) -> list:
    """Compose `command` / `entrypoint` as argv: a string is shell-split."""
    if not value:
        return []
    return shlex.split(value) if isinstance(value, str) else [str(a) for a in value]


def _sink_args(command) -> list:
    return [a for a in _argv(command) if a.startswith("--log-sink")]


# ── Deployment defaults ─────────────────────────────────────────────────────


@pytest.mark.parametrize("conf", DEPLOYMENT_CONFS)
def test_deployment_conf_selects_stdout(conf):
    assert _logging_sink((REPO_ROOT / conf).read_text()) == "stdout"


def test_node_compose_services_are_all_found():
    """Guard the discovery helper: five variants, eleven node services."""
    services = _node_services()
    assert len({f for f, _, _ in services}) == 5
    assert len(services) == 11


@pytest.mark.parametrize(
    "compose_file,service,svc",
    _node_services(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_node_compose_service_keeps_json_file_cap(compose_file, service, svc):
    assert svc.get("logging") == {
        "driver": "json-file",
        "options": {"max-size": "100m", "max-file": "3"},
    }


@pytest.mark.parametrize(
    "compose_file,service,svc",
    _node_services(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_node_compose_service_takes_its_sink_from_the_conf(compose_file, service, svc):
    """Compose deployments get their sink from the mounted conf (stdout).

    Any `--log-sink` in `command` or `entrypoint` would override it, whether
    to `both` (twice the disk) or `file` (`docker logs`, `shardctl wait` and
    the smoke-test lose their reader).
    """
    for key in ("command", "entrypoint"):
        assert _sink_args(svc.get(key)) == [], f"{compose_file}:{service} {key}"


@pytest.mark.parametrize(
    "value",
    [
        "--log-sink=both run --host=x",
        ["--log-sink", "file", "run"],
        "run --log-sink both",
    ],
)
def test_sink_override_is_found_in_either_command_form(value):
    """The Compose guard sees an override in string and list form alike."""
    assert _sink_args(value) != []


# ── Integration harness ─────────────────────────────────────────────────────


def test_harness_sink_is_file_only():
    assert NODE_LOG_SINK_ARGS == ["--log-sink=file"]


def test_generated_shard_puts_file_sink_before_run(tmp_path):
    """Every node the generated test shard starts logs to the file sink only."""
    registry = SimpleNamespace(
        register_tempfile=lambda p: None,
        register_network=lambda n: None,
        register_container=lambda n: None,
        register_volume=lambda n: None,
    )
    ports = PortMapping(40400, 40401, 40402, 40403, 40404, 40405)
    config = ShardConfig(bonds=[(VALIDATOR1_ID, 100), (VALIDATOR2_ID, 100)], include_readonly=True)
    path = generate_compose(
        config=config,
        genesis_dir=str(tmp_path),
        port_assignments={
            "boot": ports,
            "validator1": ports,
            "validator2": ports,
            "readonly": ports,
        },
        scope="unit-s1",
        paths=ResourcePaths.resolve(),
        registry=registry,
    )
    try:
        services = yaml.safe_load(Path(path).read_text())["services"]
    finally:
        Path(path).unlink()

    assert set(services) == {"boot", "validator1", "validator2", "readonly"}
    for name, svc in services.items():
        command = svc["command"]
        assert _sink_args(command) == NODE_LOG_SINK_ARGS, name
        assert command.index(NODE_LOG_SINK_ARGS[0]) < command.index("run"), name


def test_every_docker_run_launch_puts_file_sink_before_the_node_command():
    """Each `docker run` argv in the provider follows `image` with the sink.

    The standalone, recreate, and add_node paths build argv by hand rather
    than through generate_compose. A new launch path that forgets the flag
    would silently fall back to the conf's stdout sink, and the log scans
    would read the capped container tail again.
    """
    tree = ast.parse(Path(docker_mod.__file__).read_text())
    launches = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        elts = node.elts
        for i, elt in enumerate(elts):
            if isinstance(elt, ast.Name) and elt.id == "image":
                launches += 1
                nxt = elts[i + 1] if i + 1 < len(elts) else None
                assert (
                    isinstance(nxt, ast.Starred)
                    and isinstance(nxt.value, ast.Name)
                    and nxt.value.id == "NODE_LOG_SINK_ARGS"
                ), f"docker.py:{elt.lineno}: `image` not followed by *NODE_LOG_SINK_ARGS"
    assert launches == 3, f"expected 3 docker run launch sites, found {launches}"


def test_compose_module_exports_the_constant_docker_uses():
    assert docker_mod.NODE_LOG_SINK_ARGS is compose_mod.NODE_LOG_SINK_ARGS


# ── Log-reader contract ─────────────────────────────────────────────────────


def _handle():
    return DockerNodeHandle(
        name="rnode.test.unit.boot",
        ports=PortMapping(1, 2, 3, 4, 5, 6),
        network="net",
        role=NodeRole.BOOTSTRAP,
    )


def _fake_docker(monkeypatch, files: list, file_text: str, stdout_text: str):
    calls = []

    def fake(*args, **_kw):
        calls.append(args)
        if args[:1] == ("exec",) and "ls -1t" in args[-1]:
            listing = "\n".join(files)
            return subprocess.CompletedProcess(args, 0 if files else 1, listing, "")
        if args[:1] == ("exec",) and args[2] == "cat":
            return subprocess.CompletedProcess(args, 0, file_text, "")
        if args[:1] == ("logs",):
            return subprocess.CompletedProcess(args, 0, stdout_text, "")
        raise AssertionError(f"unexpected docker call {args}")

    monkeypatch.setattr(docker_mod, "_docker", fake)
    return calls


def test_file_sink_logs_are_read_from_rotated_files(monkeypatch):
    """sink=file: logs() concatenates node.log* oldest-first, skips .gz, no docker logs."""
    calls = _fake_docker(
        monkeypatch,
        files=[
            "/var/lib/rnode/logs/node.log.2026-09-30-21",
            "/var/lib/rnode/logs/node.log.2026-09-30-20",
            "/var/lib/rnode/logs/node.log.2026-09-30-19.gz",
        ],
        file_text="from-file\n",
        stdout_text="from-stdout\n",
    )
    assert _handle().logs() == "from-file\n"
    cat = next(c for c in calls if c[:1] == ("exec",) and c[2] == "cat")
    assert cat[3:] == (
        "/var/lib/rnode/logs/node.log.2026-09-30-20",
        "/var/lib/rnode/logs/node.log.2026-09-30-21",
    )
    assert not any(c[:1] == ("logs",) for c in calls)


def test_stdout_sink_logs_fall_back_to_docker_logs(monkeypatch):
    """sink=stdout (or a crash before the file sink opens): read `docker logs`."""
    calls = _fake_docker(monkeypatch, files=[], file_text="", stdout_text="from-stdout\n")
    assert _handle().logs() == "from-stdout\n"
    assert any(c[:1] == ("logs",) for c in calls)

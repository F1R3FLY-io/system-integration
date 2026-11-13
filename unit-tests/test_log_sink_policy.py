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
import importlib
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
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
from test.infra.log_events import record_timestamp  # noqa: E402
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
        if args[:1] == ("inspect",):
            return subprocess.CompletedProcess(args, 0, "0|running", "")
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


def _now() -> str:
    """The current time as the node and Docker write it, then let the clock move.

    The scan window opens one microsecond before its test starts, so a line
    logged before a test begins must carry an earlier time than that start.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    time.sleep(0.001)
    return stamp


def _record(level: str, message: str) -> str:
    return f'{{"timestamp":"{_now()}","level":"{level}","message":"{message}"}}\n'


class _RotatingFileSink:
    """Model the bounded writer's rename/reopen between Docker's ls and cat."""

    live = "/var/lib/rnode/logs/node.log"
    retired = live + ".00000000000000000000"

    def __init__(self, monkeypatch):
        self.files = {self.live: _record("INFO", "startup")}
        self.before_cat = None
        self.cat_status = 0
        self.fallback_calls = 0
        self.running = True
        self.copy_status = 0
        self.stdout = "INFO stdout startup only\n"
        self.stderr = ""
        self.stream_written = _now()
        monkeypatch.setattr(docker_mod, "_docker", self.docker)

    def rotate(self, new_text):
        self.files[self.retired] = self.files.pop(self.live)
        self.files[self.live] = new_text

    def crash(self, stderr):
        self.stderr = stderr
        self.stream_written = _now()

    def stream(self, text, stamped):
        if not stamped:
            return text
        return "".join(f"{self.stream_written} {line}" for line in text.splitlines(True))

    def docker(self, *args, **_kw):
        if args[:1] == ("inspect",):
            status = "running" if self.running else "exited"
            return subprocess.CompletedProcess(args, 0, f"0|{status}", "")
        if args[:1] == ("exec",) and not self.running:
            return subprocess.CompletedProcess(args, 1, "", "container is not running")
        if args[:1] == ("cp",):
            if self.copy_status:
                return subprocess.CompletedProcess(args, self.copy_status, "", "permission denied")
            if not self.files:
                missing = f"Error response from daemon: Could not find the file {args[1]}"
                return subprocess.CompletedProcess(args, 1, "", missing)
            for name, text in self.files.items():
                (Path(args[2]) / Path(name).name).write_text(text)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:1] == ("exec",) and "ls -1t" in args[-1]:
            return subprocess.CompletedProcess(args, 0, "\n".join(reversed(self.files)), "")
        if args[:1] == ("exec",) and args[2] == "cat":
            if self.before_cat:
                action, self.before_cat = self.before_cat, None
                action()
            text = "".join(self.files.get(p, "") for p in args[3:])
            status = self.cat_status or int(any(p not in self.files for p in args[3:]))
            return subprocess.CompletedProcess(args, status, text, "")
        if args[:1] == ("logs",):
            self.fallback_calls += 1
            stamped = "--timestamps" in args
            return subprocess.CompletedProcess(
                args, 0, self.stream(self.stdout, stamped), self.stream(self.stderr, stamped)
            )
        raise AssertionError(f"unexpected docker call {args}")


@pytest.mark.parametrize("new_warning", [False, True])
@pytest.mark.parametrize("fresh_text", ["", "INFO after hourly rotation\n"])
def test_per_test_scan_does_not_replay_allowed_warning_after_first_rotation(
    monkeypatch, new_warning, fresh_text
):
    """A rotation racing a log read must not replay an earlier test's lines.

    Drive the actual autouse fixture through an allowing test and a strict
    test. The first rotation races the strict test's teardown read; cat exits
    zero even though the earlier log is now at a path missing from its argv.
    """
    conftest = importlib.import_module("test.conftest")
    monkeypatch.setattr(conftest, "_judged_through", {})
    monkeypatch.setattr(conftest, "_last_scan_allowances", {})
    sink = _RotatingFileSink(monkeypatch)
    handle = _handle()
    provider = SimpleNamespace(active_handles=[handle], node_logs_available=True)

    def begin_test(*allowances):
        markers = [SimpleNamespace(args=allowances)] if allowances else []
        request = SimpleNamespace(node=SimpleNamespace(iter_markers=lambda _name: markers))
        scan = conftest.check_node_logs_after_test.__wrapped__(request, provider)
        next(scan)
        return scan

    earlier = begin_test("ComputationOutOfPhlogistons")
    sink.files[sink.live] += _record("WARN", "old deploy: Computation ran out of phlogistons")
    with pytest.raises(StopIteration):
        next(earlier)

    later = begin_test()
    sink.before_cat = lambda: sink.rotate(fresh_text)
    last = _record("INFO", "strict test deploy")
    sink.files[sink.live] += last
    if new_warning:
        last = _record("WARN", "new deploy: Computation ran out of phlogistons")
        sink.files[sink.live] += last
        with pytest.raises(pytest.fail.Exception) as failure:
            next(later)
        assert "new deploy" in str(failure.value)
        assert "old deploy" not in str(failure.value)
        assert "1 total" in str(failure.value)
    else:
        with pytest.raises(StopIteration):
            next(later)
    assert sink.fallback_calls == 0
    assert conftest._last_scan_allowances[handle.name] == frozenset()
    assert conftest._judged_through[handle.name] == record_timestamp(last)


def test_empty_live_file_does_not_change_log_source(monkeypatch):
    sink = _RotatingFileSink(monkeypatch)
    sink.files[sink.live] = ""
    assert _handle().logs() == ""
    assert sink.fallback_calls == 0


def test_file_read_failure_does_not_change_log_source(monkeypatch):
    sink = _RotatingFileSink(monkeypatch)
    handle = _handle()
    expected = handle.logs()
    sink.cat_status = 1
    with pytest.raises(RuntimeError, match="stable log inventory"):
        handle.logs()
    sink.cat_status = 0
    assert handle.logs() == expected
    assert sink.fallback_calls == 0


def test_missing_inventory_after_file_sink_opened_does_not_change_source(monkeypatch):
    sink = _RotatingFileSink(monkeypatch)
    handle = _handle()
    handle.logs()
    sink.files.clear()
    with pytest.raises(RuntimeError, match="stable log inventory"):
        handle.logs()
    assert sink.fallback_calls == 0


def test_live_file_precedes_rotated_files_even_when_mtimes_tie(monkeypatch):
    sink = _RotatingFileSink(monkeypatch)
    # A tied ls -t can put the numbered predecessor before the live file.
    sink.files[sink.retired] = "INFO old\n"
    assert _handle().logs() == "INFO old\n" + sink.files[sink.live]


@pytest.fixture
def log_scan(monkeypatch):
    conftest = importlib.import_module("test.conftest")
    monkeypatch.setattr(conftest, "_judged_through", {})
    monkeypatch.setattr(conftest, "_last_scan_allowances", {})

    def begin(provider, *allowances):
        markers = [SimpleNamespace(args=allowances)] if allowances else []
        request = SimpleNamespace(node=SimpleNamespace(iter_markers=lambda _name: markers))
        scan = conftest.check_node_logs_after_test.__wrapped__(request, provider)
        next(scan)
        return scan

    return begin


def _scan_provider():
    # Keep resources after snapshotting so the real retirement paths need no
    # Docker cleanup or archive I/O in these focused tests.
    return docker_mod.DockerProvider(
        port_allocator=None,
        registry=SimpleNamespace(session_id="unit", keep_running=True),
        timeouts=None,
        paths=SimpleNamespace(),
    )


@pytest.mark.parametrize("retire", [None, "remove_node", "destroy_shard"])
@pytest.mark.parametrize("new_node", [False, True])
def test_stopped_container_panic_fails_active_and_retired_scans(
    monkeypatch, log_scan, retire, new_node
):
    sink = _RotatingFileSink(monkeypatch)
    handle = _handle()
    provider = _scan_provider()
    if not new_node:
        provider._active_handles.append(handle)
        earlier = log_scan(provider, "ComputationOutOfPhlogistons")
        sink.files[sink.live] += _record("WARN", "old deploy: Computation ran out of phlogistons")
        with pytest.raises(StopIteration):
            next(earlier)
        sink.rotate(_record("INFO", "next hour"))

    strict = log_scan(provider)
    if new_node:
        provider._active_handles.append(handle)
    handle.logs()  # Observe the file sink before the process exits.
    sink.files[sink.live] += _record("INFO", "last file event")
    sink.crash("thread 'main' panicked at casper/src/lib.rs\n")
    sink.running = False
    if retire == "remove_node":
        provider.remove_node(handle)
    elif retire == "destroy_shard":
        provider.destroy_shard([handle])
    with pytest.raises(pytest.fail.Exception) as failure:
        next(strict)
    assert "[Panic]" in str(failure.value)
    assert "old deploy" not in str(failure.value)
    assert "1 total" in str(failure.value)
    assert sink.fallback_calls == 1
    assert provider.retired_log_snapshots == []


@pytest.mark.parametrize("retire", [None, "remove_node", "destroy_shard"])
def test_unreadable_stopped_logs_fail_scanning(monkeypatch, log_scan, retire):
    sink = _RotatingFileSink(monkeypatch)
    handle = _handle()
    provider = _scan_provider()
    provider._active_handles.append(handle)
    strict = log_scan(provider)
    sink.running = False
    sink.copy_status = 1
    if retire == "remove_node":
        provider.remove_node(handle)
    elif retire == "destroy_shard":
        provider.destroy_shard([handle])
    with pytest.raises(pytest.fail.Exception, match="Log scan failed") as failure:
        next(strict)
    assert handle.name in str(failure.value)
    assert provider.retired_log_snapshots == []


def test_a_node_stopped_before_any_read_keeps_its_file_logs(monkeypatch, log_scan):
    sink = _RotatingFileSink(monkeypatch)
    provider = _scan_provider()
    handle = _handle()
    provider._active_handles.append(handle)
    strict = log_scan(provider)
    sink.files[sink.live] += _record("WARN", "file deploy: Computation ran out of phlogistons")
    sink.running = False
    with pytest.raises(pytest.fail.Exception) as failure:
        next(strict)
    assert "file deploy" in str(failure.value)


def test_a_node_that_exits_before_opening_its_file_sink_yields_its_stdout(monkeypatch):
    sink = _RotatingFileSink(monkeypatch)
    sink.files.clear()
    sink.crash("config error: unknown key\n")
    sink.running = False
    assert _handle().logs().endswith("config error: unknown key\n")


def test_unreadable_logs_at_setup_do_not_open_the_window_early(monkeypatch, log_scan):
    sink = _RotatingFileSink(monkeypatch)
    provider = _scan_provider()
    handle = _handle()
    provider._active_handles.append(handle)
    earlier = log_scan(provider, "ComputationOutOfPhlogistons")
    sink.files[sink.live] += _record("WARN", "old deploy: Computation ran out of phlogistons")
    with pytest.raises(StopIteration):
        next(earlier)
    sink.cat_status = 1
    strict = log_scan(provider)
    sink.cat_status = 0
    sink.files[sink.live] += _record("WARN", "new deploy: Computation ran out of phlogistons")
    with pytest.raises(pytest.fail.Exception) as failure:
        next(strict)
    assert "new deploy" in str(failure.value)
    assert "old deploy" not in str(failure.value)


def test_stopped_logs_preserve_file_prefix_and_tail(monkeypatch):
    sink = _RotatingFileSink(monkeypatch)
    handle = _handle()
    prefix = handle.logs()
    sink.rotate(_record("INFO", "newest file"))
    prefix += sink.files[sink.live]
    panic = "thread 'main' panicked at casper/src/lib.rs\n"
    sink.crash(panic)
    sink.running = False
    stamp = sink.stream_written
    assert handle.logs() == f"{prefix}{stamp} INFO stdout startup only\n{stamp} {panic}"
    assert handle.logs(tail=1) == f"{stamp} {panic}".rstrip("\n")
    assert record_timestamp(handle.logs(tail=1)) is not None

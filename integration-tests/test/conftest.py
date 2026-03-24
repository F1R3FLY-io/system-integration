import dataclasses
import os
import shutil
import socket
import subprocess
import tempfile
import time
import logging
from contextlib import contextmanager
from random import Random
from typing import (
    Dict,
    Generator,
    List,
    Optional,
    Set,
    Tuple,
)

import pytest
import yaml
from _pytest.terminal import TerminalReporter
from _pytest.reports import TestReport
from _pytest.config.argparsing import Parser
from f1r3fly.crypto import PrivateKey
import docker as docker_py
from docker.client import DockerClient

from .common import (
    CommandLineOptions,
    TestingContext,
)
from .rnode import (
    DEFAULT_IMAGE,
    Node,
    COMPOSE_PORT_MAPPINGS,
)


# Silence unwanted noise in logs produced at the DEBUG level
logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
logging.getLogger('connectionpool.py').setLevel(logging.WARNING)
logging.getLogger('docker.utils.config').setLevel(logging.WARNING)
logging.getLogger('docker.auth').setLevel(logging.WARNING)

def _docker_compose_bin():
    """Return the docker compose binary as a list.

    Tries 'docker compose' (v2 plugin) first, falls back to 'docker-compose'.
    """
    try:
        subprocess.run(["docker", "compose", "version"],
                       capture_output=True, check=True)
        return ["docker", "compose"]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ["docker-compose"]


# Container names matching the docker-compose defaults
BOOTSTRAP_CONTAINER = 'rnode.bootstrap'
VALIDATOR1_CONTAINER = 'rnode.validator1'
VALIDATOR2_CONTAINER = 'rnode.validator2'
VALIDATOR3_CONTAINER = 'rnode.validator3'
READONLY_CONTAINER = 'rnode.readonly'

ALL_CONTAINERS = [
    BOOTSTRAP_CONTAINER,
    VALIDATOR1_CONTAINER,
    VALIDATOR2_CONTAINER,
    VALIDATOR3_CONTAINER,
    READONLY_CONTAINER,
]

VALIDATOR_CONTAINERS = [
    VALIDATOR1_CONTAINER,
    VALIDATOR2_CONTAINER,
    VALIDATOR3_CONTAINER,
]

# Compose project and network names -- distinct project names prevent
# `docker-compose down --remove-orphans` from killing containers managed
# by the other project.
SHARD_PROJECT_NAME = 'f1r3fly-shard'
STANDALONE_PROJECT_NAME = 'f1r3fly-standalone'
COMPOSE_NETWORK = 'f1r3fly-test-shard'
STANDALONE_NETWORK = 'f1r3fly-test-standalone'

# Standalone node
STANDALONE_CONTAINER = 'rnode.standalone'

# Keys from .env.node -- matching genesis/bonds.txt and genesis/wallets.txt
BOOTSTRAP_KEY = PrivateKey.from_hex("5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657")
VALIDATOR1_KEY = PrivateKey.from_hex("357cdc4201a5650830e0bc5a03299a30038d9934ba4c7ab73ec164ad82471ff9")
VALIDATOR2_KEY = PrivateKey.from_hex("2c02138097d019d263c1d5383fcaddb1ba6416a0f4e64e3a617fe3af45b7851d")
VALIDATOR3_KEY = PrivateKey.from_hex("b67533f1f99c0ecaedb7d829e430b1c0e605bda10f339f65d5567cb5bd77cbcb")

# Genesis wallet balances from genesis/wallets.txt (vault addresses derived from the keys above)
# BOOTSTRAP  = 1111AtahZeefej... = 50000000000000000
# VALIDATOR1 = 111127RX5Zgi...   = 50000000000000000
# VALIDATOR2 = 111129p33f7v...   = 50000000000000000
# VALIDATOR3 = 1111LAd2PWah...   = 500000000000000000
BOOTSTRAP_GENESIS_BALANCE = 50000000000000000
VALIDATOR1_GENESIS_BALANCE = 50000000000000000
VALIDATOR2_GENESIS_BALANCE = 50000000000000000
VALIDATOR3_GENESIS_BALANCE = 500000000000000000

# Legacy aliases used by older tests
STANDALONE_KEY = PrivateKey.from_hex("ff2ba092524bafdbc85fa0c7eddb2b41c69bc9bf066a4711a8a16f749199e5be")

# Standalone node private key (matches .env.node STANDALONE_PRIVATE_KEY and standalone genesis)
STANDALONE_PRIVATE_KEY = "5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"


def pytest_sessionstart(session) -> None:
    """Force-remove all known test containers and networks at session start.

    Ensures a clean Docker environment regardless of what a previous test
    session, CI job, or manual run left behind. This is the primary defense
    against resource exhaustion and port conflicts caused by unclean shutdowns
    (e.g. pytest-timeout SIGALRM killing a test mid-lifecycle, OOM kills,
    CI job cancellation).

    Uses 'docker rm -f' which is a no-op for non-existent containers.
    """
    all_containers = (
        ALL_CONTAINERS
        + [STANDALONE_CONTAINER]
        + [
            CUSTOM_BOOT_CONTAINER,
            "rnode.custom.validator1",
            "rnode.custom.validator2",
            "rnode.custom.validator3",
            CUSTOM_JOINER_CONTAINER,
        ]
    )
    removed = []
    for name in all_containers:
        result = subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0 and b"No such container" not in result.stderr:
            removed.append(name)

    all_networks = [COMPOSE_NETWORK, STANDALONE_NETWORK, CUSTOM_NETWORK]
    for net in all_networks:
        subprocess.run(
            ["docker", "network", "rm", net],
            capture_output=True,
            check=False,
        )

    if removed:
        logging.warning(
            "Session start: removed leftover containers from previous run: %s",
            ", ".join(removed),
        )


def _get_tests_dir() -> str:
    """Return the absolute path to the integration-tests directory."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_rust_node() -> bool:
    """Check if the active node image is the Rust implementation."""
    image = os.environ.get("DEFAULT_IMAGE", "")
    return "rust" in image.lower()


def _get_compose_file() -> str:
    """Determine which compose file to use based on DEFAULT_IMAGE env var."""
    if _is_rust_node():
        return "docker-compose.rust.yml"
    return "docker-compose.scala.yml"


def _compose_cmd(*args: str) -> List[str]:
    """Build a docker compose command with the correct env and compose file."""
    tests_dir = _get_tests_dir()
    repo_root = os.path.dirname(tests_dir)
    compose_file = _get_compose_file()
    return [
        *_docker_compose_bin(),
        "--project-name", SHARD_PROJECT_NAME,
        "--env-file", os.path.join(repo_root, ".env.node"),
        "-f", os.path.join(tests_dir, compose_file),
        *args,
    ]


def _compose_up() -> None:
    """Bring up the test shard via docker-compose."""
    tests_dir = _get_tests_dir()
    compose_file = _get_compose_file()
    logging.info("Starting test shard via %s ...", compose_file)
    result = subprocess.run(
        _compose_cmd("up", "-d"),
        cwd=tests_dir,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        logging.error(
            "docker-compose up failed (exit %d):\nstdout: %s\nstderr: %s",
            result.returncode, stdout, stderr,
        )
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr,
        )


def _compose_down() -> None:
    """Tear down the test shard via docker-compose."""
    tests_dir = _get_tests_dir()
    logging.info("Stopping test shard via docker-compose...")
    subprocess.run(
        _compose_cmd("down", "--volumes", "--remove-orphans"),
        cwd=tests_dir,
        check=False,
    )


def _get_standalone_compose_file() -> str:
    """Determine which standalone compose file to use based on DEFAULT_IMAGE env var."""
    if _is_rust_node():
        return "docker-compose.standalone-rust.yml"
    return "docker-compose.standalone-scala.yml"


def _standalone_compose_cmd(*args: str, override_file: Optional[str] = None) -> List[str]:
    """Build a docker-compose command for the standalone node."""
    tests_dir = _get_tests_dir()
    repo_root = os.path.dirname(tests_dir)
    compose_file = _get_standalone_compose_file()
    cmd = [
        *_docker_compose_bin(),
        "--project-name", STANDALONE_PROJECT_NAME,
        "--env-file", os.path.join(repo_root, ".env.node"),
        "-f", os.path.join(tests_dir, compose_file),
    ]
    if override_file:
        cmd.extend(["-f", override_file])
    cmd.extend(args)
    return cmd


def _standalone_compose_up(override_file: Optional[str] = None) -> None:
    """Bring up the standalone node via docker-compose."""
    tests_dir = _get_tests_dir()
    compose_file = _get_standalone_compose_file()
    logging.info("Starting standalone node via %s ...", compose_file)
    result = subprocess.run(
        _standalone_compose_cmd("up", "-d", override_file=override_file),
        cwd=tests_dir,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        logging.error(
            "docker-compose up (standalone) failed (exit %d):\nstdout: %s\nstderr: %s",
            result.returncode, stdout, stderr,
        )
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr,
        )


def _standalone_compose_down(override_file: Optional[str] = None) -> None:
    """Tear down the standalone node via docker-compose."""
    logging.info("Stopping standalone node via docker-compose...")
    tests_dir = _get_tests_dir()
    subprocess.run(
        _standalone_compose_cmd("down", "--volumes", "--remove-orphans",
                                override_file=override_file),
        cwd=tests_dir,
        check=False,
    )


def _build_standalone_override(
    cli_flags: Optional[Set[str]] = None,
    cli_options: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Generate a temporary compose override file for standalone CLI overrides.

    Returns the path to the temp file, or None if no overrides are needed.
    The caller is responsible for deleting the temp file when done.
    """
    if not cli_flags and not cli_options:
        return None

    # Read the base compose file to extract the existing command
    tests_dir = _get_tests_dir()
    compose_file = _get_standalone_compose_file()
    compose_path = os.path.join(tests_dir, compose_file)
    with open(compose_path, 'r') as f:
        base_compose = yaml.safe_load(f)

    base_command = base_compose['services']['standalone']['command']

    # Build extended command with CLI overrides
    extended_command = list(base_command)
    if cli_flags:
        for flag in sorted(cli_flags):
            extended_command.append(flag)
    if cli_options:
        for key, value in sorted(cli_options.items()):
            extended_command.append(f"{key}={value}")

    # Generate override YAML
    override = {
        'services': {
            'standalone': {
                'command': extended_command,
            }
        }
    }

    fd, path = tempfile.mkstemp(prefix='standalone-override-', suffix='.yml')
    with os.fdopen(fd, 'w') as f:
        yaml.dump(override, f, default_flow_style=False)

    logging.info("Generated standalone compose override: %s", path)
    logging.debug("Override command: %s", extended_command)
    return path


@contextmanager
def start_standalone_node(
    docker_client: DockerClient,
    command_line_options: CommandLineOptions,
    cli_flags: Optional[Set[str]] = None,
    cli_options: Optional[Dict[str, str]] = None,
) -> Generator[Node, None, None]:
    """Start a standalone node with optional CLI overrides.

    Uses docker-compose with the standalone compose file and an optional
    override file for custom CLI flags. Each invocation gets a clean node.

    Args:
        docker_client: Docker client for container interaction.
        command_line_options: Test session command-line options (for timeouts).
        cli_flags: Set of CLI flags to add (e.g. {"--heartbeat-enabled"}).
        cli_options: Dict of CLI options with values (e.g. {"--max-number-of-parents": "1"}).

    Yields:
        A Node wrapper connected to the standalone node.
    """
    override_file = _build_standalone_override(cli_flags, cli_options)

    try:
        # Clean state (down --volumes removes named volumes)
        _standalone_compose_down(override_file=override_file)

        # Wait for standalone ports to be released (kernel TIME_WAIT
        # from a previous test session's standalone container).
        _wait_for_standalone_ports_free()

        # Start with one retry on transient Docker errors
        try:
            _standalone_compose_up(override_file=override_file)
        except subprocess.CalledProcessError:
            logging.warning("Standalone compose up failed, cleaning up and retrying...")
            _standalone_compose_down(override_file=override_file)
            time.sleep(5)
            _wait_for_standalone_ports_free()
            _standalone_compose_up(override_file=override_file)

        # Wait for Running state
        timeout = command_line_options.node_startup_timeout
        _wait_for_running_state(docker_client, STANDALONE_CONTAINER, timeout)

        # Create Node wrapper
        node = _make_standalone_node(docker_client, command_line_options.command_timeout)
        try:
            yield node
        finally:
            node.stop_logging()

    finally:
        # Tear down (down --volumes removes named volumes)
        _standalone_compose_down(override_file=override_file)
        _wait_for_standalone_ports_free()

        # Clean up temp override file
        if override_file and os.path.exists(override_file):
            os.unlink(override_file)


def _make_standalone_node(docker_client: DockerClient, command_timeout: int) -> Node:
    """Create a Node wrapper for the standalone container."""
    container = docker_client.containers.get(STANDALONE_CONTAINER)
    networks = container.attrs.get('NetworkSettings', {}).get('Networks', {})
    network = ''
    for net_name in networks:
        if 'standalone' in net_name or 'f1r3fly' in net_name:
            network = net_name
            break
    if not network and networks:
        network = next(iter(networks))

    return Node.from_container_name(
        docker_client=docker_client,
        container_name=STANDALONE_CONTAINER,
        command_timeout=command_timeout,
        network=network,
    )


def _wait_for_running_state(docker_client: DockerClient, container_name: str,
                             timeout: int,
                             data_volume: Optional[str] = None) -> None:
    """Wait for a node container to reach the Running state.

    Polls container logs every 5 seconds for the Running state marker. Also
    checks the container's Docker status on each poll: if the container has
    exited or died, it is restarted once via ``docker restart`` rather than
    waiting the full timeout doing nothing. If the container crashes again
    after restart, fails immediately with diagnostic output.

    When ``data_volume`` is provided (a Docker named volume), its contents
    are cleaned before restart so that partial genesis or DAG state from the
    crashed run does not prevent the new JVM from completing startup.

    This catches scenarios where the JVM crashes during genesis or startup
    (e.g. OOM, config error, BindException) and saves up to 10 minutes of
    wasted CI time per container compared to blindly polling until timeout.
    """
    container = docker_client.containers.get(container_name)
    running_marker = 'Making a transition to Running state.'
    start = time.time()
    restart_attempted = False

    while time.time() - start < timeout:
        # Refresh container state from Docker daemon
        container.reload()

        # Early crash detection: if the container has exited, either restart
        # it once or fail immediately if we already tried.
        if container.status in ('exited', 'dead'):
            exit_code = container.attrs.get('State', {}).get('ExitCode', '?')
            log_tail = container.logs(tail=30).decode('utf-8', errors='replace').strip()

            if not restart_attempted:
                logging.warning(
                    "Container %s crashed (status=%s, exit_code=%s), "
                    "attempting restart (%.1fs elapsed). Last 30 log lines:\n%s",
                    container_name, container.status, exit_code,
                    time.time() - start, log_tail,
                )
                # Clean the data volume before restart. A partially-written
                # genesis or DAG state from the crashed run can prevent the
                # JVM from completing startup on the second attempt.
                if data_volume:
                    logging.info("Cleaning data volume before restart: %s", data_volume)
                    subprocess.run(
                        ["docker", "run", "--rm", "-v", f"{data_volume}:/data",
                         "alpine", "sh", "-c", "rm -rf /data/*"],
                        check=False,
                    )
                container.restart()
                restart_attempted = True
                time.sleep(10)  # Give JVM time to begin startup
                continue
            else:
                raise RuntimeError(
                    f"Container {container_name} crashed again after restart "
                    f"(status={container.status}, exit_code={exit_code}). "
                    f"Last 30 log lines:\n{log_tail}"
                )

        # Check for the Running state marker in container logs
        logs = container.logs().decode('utf-8')
        if running_marker in logs:
            elapsed = time.time() - start
            restarted = " (after restart)" if restart_attempted else ""
            logging.info("Container %s reached Running state (%.1fs%s)",
                         container_name, elapsed, restarted)
            return

        time.sleep(5)

    # Timeout: collect diagnostics before raising
    container.reload()
    log_tail = container.logs(tail=30).decode('utf-8', errors='replace').strip()
    raise TimeoutError(
        f"Container {container_name} did not reach Running state within {timeout}s "
        f"(status={container.status}). Last 30 log lines:\n{log_tail}"
    )


def assert_containers_running(docker_client: DockerClient,
                               container_names: List[str]) -> None:
    """Assert that all named containers are in a running state.

    Raises AssertionError with details about any containers that are not running,
    including exit codes and tail of logs for crashed containers.
    """
    failures = []
    for name in container_names:
        try:
            container = docker_client.containers.get(name)
        except docker_py.errors.NotFound:
            failures.append(f"{name}: container not found")
            continue

        status = container.status
        if status != 'running':
            exit_code = container.attrs.get('State', {}).get('ExitCode', 'unknown')
            log_tail = container.logs(tail=20).decode('utf-8', errors='replace')
            failures.append(
                f"{name}: status={status}, exit_code={exit_code}\n"
                f"  Last 20 log lines:\n{log_tail}"
            )

    if failures:
        detail = "\n".join(failures)
        raise AssertionError(f"Expected all containers running, but:\n{detail}")


# ── Shard health guard ──────────────────────────────────────────────
# ---------------------------------------------------------------------------
# Test ordering: enforce the sequence from pyproject.toml [tool.pytest.ini_options]
# python_files so that test_consensus_health.py always runs last.
# Pytest collects files alphabetically by default; this hook re-sorts to
# match the explicit ordering in pyproject.toml (the single source of truth).
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items: list) -> None:
    """Re-order collected test items to match the python_files list in pyproject.toml.

    The python_files list in pyproject.toml is the single source of truth for
    test execution order. Tests from files listed earlier run first. Tests from
    files not in the list are appended at the end. Within a single file, the
    original collection order is preserved.

    After reordering, identifies the last shard test so the
    pytest_runtest_teardown hook can tear down the shard environment as soon
    as it finishes, freeing resources for the custom shard tests that follow.
    """
    file_order = config.getini("python_files")
    if not file_order:
        return
    file_rank = {name: idx for idx, name in enumerate(file_order)}
    default_rank = len(file_order)

    def sort_key(item):
        filename = os.path.basename(str(item.fspath))
        return file_rank.get(filename, default_rank)

    items[:] = sorted(items, key=sort_key)

    # Find the last shard test in the ordered sequence.
    global _last_shard_test_nodeid
    for item in reversed(items):
        marker = item.get_closest_marker('xdist_group')
        if marker and marker.args and marker.args[0] == 'shard':
            _last_shard_test_nodeid = item.nodeid
            break


# Track whether the shard has been started so the health check only
# runs when shard containers are expected to exist.
_shard_started = False

# Nodeid of the last shard test in the ordered collection. Set during
# pytest_collection_modifyitems and used by pytest_runtest_teardown to
# tear down the shard as soon as the last shard test completes (freeing
# resources before custom shard tests begin).
_last_shard_test_nodeid: Optional[str] = None


def pytest_runtest_setup(item) -> None:
    """Pre-test setup hook: shard health check and custom container cleanup.

    Runs before every test. Performs two tasks:

    1. If the session-scoped shard is active, verify all containers are still
       running. If any have crashed, abort the suite immediately rather than
       letting subsequent tests hang on dead gRPC connections.

    2. Before any custom-group test, force-remove leftover custom shard
       containers. When pytest-timeout kills a custom shard test (SIGALRM),
       the context manager's finally block may not execute cleanly, leaving
       containers running and ports bound. This cleanup ensures the next
       custom test starts with a clean slate.
    """
    # --- Custom shard pre-cleanup ---
    # Detect custom-group tests by their xdist_group marker.
    markers = list(item.iter_markers("xdist_group"))
    is_custom = any(m.args and m.args[0] == "custom" for m in markers)
    if is_custom:
        _force_cleanup_custom_containers()

    # --- Shard health check ---
    if not _shard_started:
        return

    client = docker_py.from_env()
    crashed = []
    for name in ALL_CONTAINERS:
        try:
            container = client.containers.get(name)
            if container.status != 'running':
                exit_code = container.attrs.get('State', {}).get('ExitCode', '?')
                log_tail = container.logs(tail=5).decode('utf-8', errors='replace').strip()
                crashed.append(f"{name}: status={container.status}, exit_code={exit_code}\n  {log_tail}")
        except docker_py.errors.NotFound:
            crashed.append(f"{name}: container not found")

    if crashed:
        detail = "\n".join(crashed)
        pytest.exit(
            f"ABORTING: Shard node(s) crashed -- remaining tests cannot proceed.\n{detail}",
            returncode=1,
        )
    client.close()


def pytest_runtest_teardown(item, nextitem) -> None:
    """Tear down the shard environment after the last shard test completes.

    In sequential mode the shard (5 containers) stays alive for the entire
    session by default. Custom shard tests that follow don't need it and
    benefit from having all machine resources available. This hook tears
    down the shard as soon as the last shard test finishes, as identified
    during collection by pytest_collection_modifyitems.

    Under parallel execution (xdist), each worker only runs one group so
    this hook is effectively a no-op -- the session fixture handles teardown.
    """
    global _shard_started
    if not _shard_started:
        return
    if _last_shard_test_nodeid is None:
        return
    if item.nodeid != _last_shard_test_nodeid:
        return

    logging.info("Last shard test completed -- tearing down shard environment.")
    _compose_down()
    _shard_started = False


def pytest_addoption(parser: Parser) -> None:
    parser.addoption("--startup-timeout", type=int, action="store", default=60 * 8,
                     help="timeout in seconds for starting a node")
    parser.addoption("--converge-timeout", type=int, action="store", default=60 * 5,
                     help="timeout in seconds for network converge")
    parser.addoption("--receive-timeout", type=int, action="store", default=30,
                     help="timeout in seconds for receiving a single block")
    parser.addoption("--command-timeout", type=int, action="store", default=60 * 3,
                     help="timeout in seconds for executing a rnode call")
    parser.addoption("--random-seed", type=int, action="store", default=None,
                     help="seed for the random numbers generator used in integration tests")
    parser.addoption("--timeout-scale", type=float, action="store", default=1.0,
                     help="multiplier for hardcoded polling timeouts (e.g. 3.0 for CI)")
    parser.addoption("--skip-setup", action="store_true", default=False,
                     help="skip shard setup (assume already running)")


def pytest_terminal_summary(terminalreporter: TerminalReporter) -> None:
    tr = terminalreporter
    dlist: Dict[str, List[TestReport]] = {}
    for replist in tr.stats.values():
        for rep in replist:
            if hasattr(rep, "duration"):
                if rep.nodeid not in dlist:
                    dlist[rep.nodeid] = []
                dlist[rep.nodeid].append(rep)
    if not dlist:
        return
    tr.write_sep("=", "test durations")

    for nodeid, reps in dlist.items():
        total_second = sum(rep.duration for rep in reps)
        detail_duration = ",".join([f"{rep.when:<8} {rep.duration:8.2f}s" for rep in reps])
        tr.write_line(f"Total: {total_second:8.2f}s, {detail_duration}    {nodeid}")


@pytest.fixture(scope='session')
def command_line_options(request) -> Generator[CommandLineOptions, None, None]:
    startup_timeout = int(request.config.getoption("--startup-timeout"))
    converge_timeout = int(request.config.getoption("--converge-timeout"))
    receive_timeout = int(request.config.getoption("--receive-timeout"))
    command_timeout = int(request.config.getoption("--command-timeout"))
    random_seed = request.config.getoption("--random-seed")
    timeout_scale = float(request.config.getoption("--timeout-scale"))

    yield CommandLineOptions(
        node_startup_timeout=startup_timeout,
        network_converge_timeout=converge_timeout,
        receive_timeout=receive_timeout,
        command_timeout=command_timeout,
        random_seed=random_seed,
        timeout_scale=timeout_scale,
    )


@pytest.fixture(scope='session')
def docker_client() -> Generator[DockerClient, None, None]:
    client = docker_py.from_env()
    try:
        yield client
    finally:
        pass  # Don't prune -- compose manages lifecycle


@pytest.fixture(scope='session')
def random_generator(command_line_options: CommandLineOptions) -> Generator[Random, None, None]:
    random_seed = time.time() if command_line_options.random_seed is None else command_line_options.random_seed
    logging.critical("Using tests random number generator seed: %d", random_seed)
    yield Random(random_seed)


@pytest.fixture(scope='session')
def testing_context(command_line_options: CommandLineOptions,
                    random_generator: Random,
                    docker_client: DockerClient) -> Generator[TestingContext, None, None]:
    """Session-scoped testing context for compose-based tests."""
    yield TestingContext(
        node_startup_timeout=command_line_options.node_startup_timeout,
        network_converge_timeout=command_line_options.network_converge_timeout,
        receive_timeout=command_line_options.receive_timeout,
        command_timeout=command_line_options.command_timeout,
        docker=docker_client,
        random_generator=random_generator,
        timeout_scale=command_line_options.timeout_scale,
    )


@pytest.fixture(scope='session')
def shard(request, command_line_options: CommandLineOptions,
          docker_client: DockerClient) -> Generator[None, None, None]:
    """Session-scoped fixture that brings up the test shard and tears it down.

    Starts the full shard topology (boot + 3 validators + readonly) using
    docker-compose and waits for all nodes to reach the Running state.

    Selects docker-compose.scala.yml or docker-compose.rust.yml based on
    the DEFAULT_IMAGE environment variable.

    Use --skip-setup to skip compose up/down (when shard is already running).
    """
    skip_setup = request.config.getoption("--skip-setup")
    timeout = command_line_options.node_startup_timeout

    if not skip_setup:
        # down --volumes removes named volumes for clean state
        _compose_down()
        try:
            _compose_up()
        except subprocess.CalledProcessError:
            logging.warning("Shard compose up failed, cleaning up and retrying...")
            _compose_down()
            time.sleep(5)
            _compose_up()

    try:
        logging.info("Waiting for all nodes to reach Running state (timeout=%ds)...", timeout)
        for container_name in ALL_CONTAINERS:
            _wait_for_running_state(docker_client, container_name, timeout)
        logging.info("All nodes are in Running state.")

        global _shard_started
        _shard_started = True

        yield

    finally:
        # Always tear down when not skipping setup, even if the shard never
        # reached Running state. Previously, _compose_down was only called
        # when _shard_started was True, which meant partially started shards
        # (e.g. _compose_up succeeded but _wait_for_running_state timed out)
        # were never cleaned up, leaving containers running and consuming
        # resources for all subsequent tests.
        if not skip_setup:
            _compose_down()
            _shard_started = False


def _make_node(docker_client: DockerClient, container_name: str,
               command_timeout: int) -> Node:
    """Create a Node wrapper for a compose-managed container."""
    container = docker_client.containers.get(container_name)
    networks = container.attrs.get('NetworkSettings', {}).get('Networks', {})
    network = ''
    for net_name in networks:
        if 'test-shard' in net_name or 'f1r3fly' in net_name:
            network = net_name
            break
    if not network and networks:
        network = next(iter(networks))

    return Node.from_container_name(
        docker_client=docker_client,
        container_name=container_name,
        command_timeout=command_timeout,
        network=network,
    )


@pytest.fixture(scope='session')
def bootstrap_node(shard, docker_client: DockerClient,
                   command_line_options: CommandLineOptions) -> Generator[Node, None, None]:
    """The bootstrap (ceremony master) node. Not a validator."""
    node = _make_node(docker_client, BOOTSTRAP_CONTAINER,
                      command_line_options.command_timeout)
    yield node
    node.stop_logging()


@pytest.fixture(scope='session')
def validator1_node(shard, docker_client: DockerClient,
                    command_line_options: CommandLineOptions) -> Generator[Node, None, None]:
    """Validator 1 node."""
    node = _make_node(docker_client, VALIDATOR1_CONTAINER,
                      command_line_options.command_timeout)
    yield node
    node.stop_logging()


@pytest.fixture(scope='session')
def validator2_node(shard, docker_client: DockerClient,
                    command_line_options: CommandLineOptions) -> Generator[Node, None, None]:
    """Validator 2 node."""
    node = _make_node(docker_client, VALIDATOR2_CONTAINER,
                      command_line_options.command_timeout)
    yield node
    node.stop_logging()


@pytest.fixture(scope='session')
def validator3_node(shard, docker_client: DockerClient,
                    command_line_options: CommandLineOptions) -> Generator[Node, None, None]:
    """Validator 3 node."""
    node = _make_node(docker_client, VALIDATOR3_CONTAINER,
                      command_line_options.command_timeout)
    yield node
    node.stop_logging()


@pytest.fixture(scope='session')
def readonly_node(shard, docker_client: DockerClient,
                  command_line_options: CommandLineOptions) -> Generator[Node, None, None]:
    """The read-only (observer) node."""
    node = _make_node(docker_client, READONLY_CONTAINER,
                      command_line_options.command_timeout)
    yield node
    node.stop_logging()


@pytest.fixture(scope='session')
def validator_nodes(validator1_node: Node, validator2_node: Node,
                    validator3_node: Node) -> List[Node]:
    """All three validator nodes as a list."""
    return [validator1_node, validator2_node, validator3_node]


@pytest.fixture(scope='session')
def all_nodes(bootstrap_node: Node, validator1_node: Node,
              validator2_node: Node, validator3_node: Node,
              readonly_node: Node) -> List[Node]:
    """All five nodes as a list."""
    return [bootstrap_node, validator1_node, validator2_node,
            validator3_node, readonly_node]


# ── Custom Shard Infrastructure ─────────────────────────────────────
# Per-test shard with custom bond weights, FTT, and CLI overrides.
# Used by Phase 6 tests (asymmetric bonds, synchrony constraints,
# bonding/unbonding, state trimming, slashing).

CUSTOM_PROJECT_NAME = 'f1r3fly-custom'
CUSTOM_NETWORK = 'f1r3fly-test-custom'
CUSTOM_BOOT_CONTAINER = 'rnode.custom.boot'
CUSTOM_JOINER_CONTAINER = 'rnode.custom.joiner'

BOOTSTRAP_NODE_ID = '1e780e5dfbe0a3d9470a2b414f502d59402e09c2'
BOOTSTRAP_PRIVATE_KEY_HEX = '5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657'

# Port base offsets for custom shard nodes (host ports).
# Each node uses 6 consecutive ports: protocol, ext-gRPC, int-gRPC,
# HTTP, discovery, admin-HTTP.
_CUSTOM_PORT_BASES = {
    'boot': 40500,
    'validator1': 40510,
    'validator2': 40520,
    'validator3': 40530,
    'joiner': 40540,
}


@dataclasses.dataclass
class ValidatorIdentity:
    """Complete validator identity for custom shard configuration.

    Bundles a PrivateKey (for signing deploys in tests) with the raw hex
    representations needed for genesis files and docker-compose commands.
    """
    key: PrivateKey
    private_hex: str
    public_hex: str


# Pre-defined validator identities matching .env.node credentials.
VALIDATOR1_ID = ValidatorIdentity(
    key=VALIDATOR1_KEY,
    private_hex="357cdc4201a5650830e0bc5a03299a30038d9934ba4c7ab73ec164ad82471ff9",
    public_hex="04fa70d7be5eb750e0915c0f6d19e7085d18bb1c22d030feb2a877ca2cd226d04438aa819359c56c720142fbc66e9da03a5ab960a3d8b75363a226b7c800f60420",
)
VALIDATOR2_ID = ValidatorIdentity(
    key=VALIDATOR2_KEY,
    private_hex="2c02138097d019d263c1d5383fcaddb1ba6416a0f4e64e3a617fe3af45b7851d",
    public_hex="04837a4cff833e3157e3135d7b40b8e1f33c6e6b5a4342b9fc784230ca4c4f9d356f258debef56ad4984726d6ab3e7709e1632ef079b4bcd653db00b68b2df065f",
)
VALIDATOR3_ID = ValidatorIdentity(
    key=VALIDATOR3_KEY,
    private_hex="b67533f1f99c0ecaedb7d829e430b1c0e605bda10f339f65d5567cb5bd77cbcb",
    public_hex="0457febafcc25dd34ca5e5c025cd445f60e5ea6918931a54eb8c3a204f51760248090b0c757c2bdad7b8c4dca757e109f8ef64737d90712724c8216c94b4ae661c",
)

VALIDATOR4_KEY = PrivateKey.from_hex("5ff3514bf79a7d18e8dd974c699678ba63b7762ce8d78c532346e52f0ad219cd")
VALIDATOR4_ID = ValidatorIdentity(
    key=VALIDATOR4_KEY,
    private_hex="5ff3514bf79a7d18e8dd974c699678ba63b7762ce8d78c532346e52f0ad219cd",
    public_hex="04d26c6103d7269773b943d7a9c456f9eb227e0d8b1fe30bccee4fca963f4446e3385d99f6386317f2c1ad36b9e6b0d5f97bb0a0041f05781c60a5ebca124a251d",
)


@dataclasses.dataclass
class CustomShard:
    """Handle to a running custom shard, returned by start_custom_shard.

    Provides access to nodes, network details, and genesis configuration
    needed by add_peer_to_shard to attach joiner nodes mid-test.
    """
    nodes: Dict[str, Node]
    network_name: str
    genesis_dir: str
    bootstrap_host: str


def _generate_custom_genesis(
    bonds: List[Tuple[ValidatorIdentity, int]],
    extra_wallets: Optional[List[Tuple[str, int]]] = None,
) -> str:
    """Write custom genesis files (bonds.txt, wallets.txt) to a temp directory.

    bonds.txt is generated from the provided validator identities and stakes.
    wallets.txt is copied from the default genesis (all standard keys have
    sufficient tokens for test deploys).  Any entries in *extra_wallets* are
    appended so that additional keys have tokens at genesis.

    Args:
        bonds: Validator identities and their bond stakes.
        extra_wallets: Optional list of ``(vault_address, balance)`` tuples to
            append to the genesis wallets file.  Use
            ``PrivateKey.get_public_key().get_vault_address()`` to compute the
            vault address for a given key.

    Returns the absolute path to the temporary genesis directory.
    """
    genesis_dir = tempfile.mkdtemp(prefix='custom-genesis-')

    bonds_path = os.path.join(genesis_dir, 'bonds.txt')
    with open(bonds_path, 'w') as f:
        for identity, stake in bonds:
            f.write(f"{identity.public_hex} {stake}\n")

    tests_dir = _get_tests_dir()
    default_wallets = os.path.join(tests_dir, 'genesis', 'wallets.txt')
    wallets_path = os.path.join(genesis_dir, 'wallets.txt')
    shutil.copy2(default_wallets, wallets_path)

    if extra_wallets:
        with open(wallets_path, 'a') as f:
            for vault_addr, balance in extra_wallets:
                f.write(f"{vault_addr},{balance}\n")

    logging.info("Generated custom genesis in %s (bonds: %s, extra_wallets: %d)",
                 genesis_dir,
                 ", ".join(f"{v.public_hex[:8]}...={s}" for v, s in bonds),
                 len(extra_wallets or []))
    return genesis_dir


def _generate_custom_compose(
    tests_dir: str,
    bonds: List[Tuple[ValidatorIdentity, int]],
    genesis_dir: str,
    ftt: float = 0.99,
    required_signatures: Optional[int] = None,
    global_cli_options: Optional[Dict[str, str]] = None,
    per_node_cli_options: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    """Generate a complete docker-compose YAML file for a custom shard.

    Creates a compose file with a bootstrap node and N validators (1-3),
    using the provided genesis files and CLI overrides. Data is stored in
    Docker named volumes (boot-data, validator{N}-data); config and genesis
    files are bind-mounted on top.

    Config overrides are passed as CLI flags after ``run`` (not JVM -D
    properties), because the node's Configuration.scala does not include
    system properties in its config resolution chain.

    Returns the path to the generated compose YAML file.
    """
    if len(bonds) < 1 or len(bonds) > 3:
        raise ValueError(f"Custom shard bonds must have 1-3 entries, got {len(bonds)}")

    if required_signatures is None:
        required_signatures = max(0, len(bonds) - 1)

    image = DEFAULT_IMAGE
    bootstrap_url = (
        f"rnode://{BOOTSTRAP_NODE_ID}@{CUSTOM_BOOT_CONTAINER}"
        f"?protocol=40400&discovery=40404"
    )

    repo_root = os.path.dirname(tests_dir)
    abs_conf = os.path.join(repo_root, 'conf')
    abs_local_conf = os.path.join(tests_dir, 'conf')
    abs_certs = os.path.join(tests_dir, 'certs')

    global_opts = global_cli_options or {}
    per_node_opts = per_node_cli_options or {}

    def _extra_cli(node_key: str) -> List[str]:
        """Merge global and per-node CLI options into a flag list.

        Per-node options override global options for the same key.
        Options with empty string values are treated as presence-only flags
        (e.g. ``--heartbeat-disabled``). Options with non-empty values are
        formatted as ``--key=value``.
        """
        merged = dict(global_opts)
        merged.update(per_node_opts.get(node_key, {}))
        flags: List[str] = []
        for k, v in sorted(merged.items()):
            flags.append(k if v == "" else f"{k}={v}")
        return flags

    # ── JVM memory tuning for CI (Scala only) ──
    # The CI job sets _JAVA_OPTIONS=-XX:MaxRAMPercentage=35.0 globally, so
    # each JVM claims 35% of host RAM.  With 4 containers on a 7GB runner
    # that's 140% -- causing GC thrashing and startup timeouts.
    #
    # Override _JAVA_OPTIONS per container with a percentage that lets all
    # containers fit within ~90% of host RAM (leaving ~10% for the OS).
    # On local machines with plenty of RAM this is harmless -- the JVM just
    # gets a proportionally larger (but still bounded) heap.
    rust = _is_rust_node()
    if rust:
        java_options = None
    else:
        total_containers = len(bonds) + 1  # boot + validators
        max_ram_pct = 90.0 / total_containers
        java_options = (
            f'-XX:MaxRAMPercentage={max_ram_pct:.1f} -XX:MaxDirectMemorySize=128M'
        )
        logging.info(
            "Custom shard JVM tuning: %d containers, MaxRAMPercentage=%.1f%%",
            total_containers, max_ram_pct,
        )

    # F1R3_* runtime tuning for Rust nodes (must match compose/f1r3node-rust.yml).
    # These env vars are read via OnceLock on first use; Scala ignores them.
    rust_env = [
        # NOTE: F1R3_SYNCHRONY_CONSTRAINT_THRESHOLD intentionally omitted here.
        # It overrides per-validator CLI thresholds at runtime, breaking
        # test_synchrony_constraint which sets different thresholds per node.
        # The static compose (docker-compose.rust.yml) sets it via x-rnode anchor.
        'F1R3_SYNCHRONY_RECOVERY_STALL_WINDOW_SECONDS=60',
        'F1R3_SYNCHRONY_RECOVERY_COOLDOWN_SECONDS=20',
        'F1R3_SYNCHRONY_RECOVERY_MAX_BYPASSES=2',
        'F1R3_SYNCHRONY_FINALIZED_BASELINE_ENABLED=1',
        'F1R3_SYNCHRONY_FINALIZED_BASELINE_MAX_DISTANCE=2048',
        'F1R3_HEARTBEAT_FRONTIER_CHASE_MAX_LAG=0',
        'F1R3_HEARTBEAT_SELF_PROPOSE_COOLDOWN_MS=15000',
        'F1R3_FINALIZER_WORK_BUDGET_MS=8000',
        'F1R3_FINALIZER_CATCHUP_WORK_BUDGET_MS=8000',
        'F1R3_FINALIZER_STEP_TIMEOUT_MS=1000',
        'F1R3_FINALIZER_CATCHUP_STEP_TIMEOUT_MS=1000',
        'F1R3_FINALIZER_MAX_CLIQUE_CANDIDATES=128',
        'F1R3_FINALIZER_CANDIDATE_RANKING=recency_stake',
        'F1R3_BLOCK_RETRIEVER_PEER_REQUERY_COOLDOWN_MS=500',
        'F1R3_BLOCK_RETRIEVER_BROADCAST_ONLY_COOLDOWN_MS=500',
        'F1R3_BLOCK_RETRIEVER_DEPENDENCY_RECOVERY_COOLDOWN_MS=500',
        'F1R3_BLOCK_RETRIEVER_STALE_REQUEST_LIFETIME_MULTIPLIER=6',
        'F1R3_BLOCK_RETRIEVER_MAX_RETRIES_PER_HASH=32',
        'F1R3_BLOCK_RETRIEVER_KNOWN_PEER_REQUERY_SOFT_LIMIT=8',
        'F1R3_BLOCK_RETRIEVER_MIN_REREQUEST_INTERVAL_MS=500',
        'F1R3_BLOCK_RETRIEVER_RETRY_BUDGET_QUARANTINE_MS=10000',
        'F1R3_BLOCK_RETRIEVER_DEDUP_QUERIED_PEERS=0',
        'F1R3_MAX_BLOCKS_IN_PROCESSING=2048',
        'F1R3_MAX_USER_DEPLOYS_PER_BLOCK=32',
    ] if rust else []

    services: Dict = {}

    # ── Bootstrap node ──
    boot_host = CUSTOM_BOOT_CONTAINER
    boot_base = _CUSTOM_PORT_BASES['boot']
    boot_command = ([] if rust else [
        "-Dlogback.configurationFile=/var/lib/rnode/logback.xml",
    ]) + [
        "run",
        f"--host={boot_host}",
        f"--bootstrap={bootstrap_url}",
        "--allow-private-addresses",
        f"--validator-private-key={BOOTSTRAP_PRIVATE_KEY_HEX}",
        f"--fault-tolerance-threshold={ftt}",
        f"--required-signatures={required_signatures}",
        "--approve-duration=180seconds",
        "--ceremony-master-mode",
    ] + ([] if rust else [
        "--disable-mergeable-channel-gc",
    ]) + _extra_cli("boot")

    services['boot'] = {
        'image': image,
        'pull_policy': 'never',
        'user': 'root',
        'restart': 'no',
        'container_name': boot_host,
        'networks': [CUSTOM_NETWORK],
        'command': boot_command,
        'ports': [f"{boot_base + p}:4040{p}" for p in range(6)],
        'volumes': [
            'boot-data:/var/lib/rnode',
            f"{abs_conf}/default.conf:/var/lib/rnode/rnode.conf",
            f"{genesis_dir}/wallets.txt:/var/lib/rnode/genesis/wallets.txt",
            f"{genesis_dir}/bonds.txt:/var/lib/rnode/genesis/bonds.txt",
        ] + ([] if rust else [
            f"{abs_local_conf}/logback.xml:/var/lib/rnode/logback.xml",
        ]) + [
            f"{abs_certs}/bootstrap/node.certificate.pem:/var/lib/rnode/node.certificate.pem:ro",
            f"{abs_certs}/bootstrap/node.key.pem:/var/lib/rnode/node.key.pem:ro",
        ],
        'environment': [
            'OPENAI_ENABLED=false',
        ] + rust_env + ([] if rust else [
            f'_JAVA_OPTIONS={java_options}',
        ]),
    }

    # ── Validator nodes ──
    for idx, (identity, _stake) in enumerate(bonds):
        slot = idx + 1
        node_key = f"validator{slot}"
        host = f"rnode.custom.{node_key}"
        cert_dir = f"validator{slot}"
        base_port = _CUSTOM_PORT_BASES[node_key]

        validator_command = ([] if rust else [
            "-Dlogback.configurationFile=/var/lib/rnode/logback.xml",
        ]) + [
            "run",
            f"--host={host}",
            "--allow-private-addresses",
            f"--bootstrap={bootstrap_url}",
            f"--validator-public-key={identity.public_hex}",
            f"--validator-private-key={identity.private_hex}",
            "--genesis-validator",
            f"--fault-tolerance-threshold={ftt}",
            f"--required-signatures={required_signatures}",
        ] + ([] if rust else [
            "--disable-mergeable-channel-gc",
        ]) + _extra_cli(node_key)

        services[node_key] = {
            'image': image,
            'pull_policy': 'never',
            'user': 'root',
            'restart': 'no',
            'container_name': host,
            'depends_on': ['boot'],
            'networks': [CUSTOM_NETWORK],
            'command': validator_command,
            'ports': [f"{base_port + p}:4040{p}" for p in range(6)],
            'volumes': [
                f'{node_key}-data:/var/lib/rnode',
                f"{abs_conf}/default.conf:/var/lib/rnode/rnode.conf",
                f"{genesis_dir}/wallets.txt:/var/lib/rnode/genesis/wallets.txt",
                f"{genesis_dir}/bonds.txt:/var/lib/rnode/genesis/bonds.txt",
            ] + ([] if rust else [
                f"{abs_local_conf}/logback.xml:/var/lib/rnode/logback.xml",
            ]) + [
                f"{abs_certs}/{cert_dir}/node.certificate.pem:/var/lib/rnode/node.certificate.pem:ro",
                f"{abs_certs}/{cert_dir}/node.key.pem:/var/lib/rnode/node.key.pem:ro",
            ],
            'environment': [
                'OPENAI_ENABLED=false',
            ] + rust_env + ([] if rust else [
                f'_JAVA_OPTIONS={java_options}',
            ]),
        }

    volume_names = ['boot-data'] + [f'validator{i+1}-data' for i in range(len(bonds))]
    compose = {
        'services': services,
        'volumes': {name: None for name in volume_names},
        'networks': {
            CUSTOM_NETWORK: {
                'name': CUSTOM_NETWORK,
                'driver': 'bridge',
            }
        },
    }

    fd, compose_path = tempfile.mkstemp(prefix='custom-shard-', suffix='.yml')
    with os.fdopen(fd, 'w') as f:
        yaml.dump(compose, f, default_flow_style=False, sort_keys=False)

    logging.info("Generated custom shard compose: %s", compose_path)
    return compose_path


def _custom_compose_cmd(compose_file: str, *args: str) -> List[str]:
    """Build a docker-compose command for the custom shard."""
    tests_dir = _get_tests_dir()
    repo_root = os.path.dirname(tests_dir)
    return [
        *_docker_compose_bin(),
        "--project-name", CUSTOM_PROJECT_NAME,
        "--env-file", os.path.join(repo_root, ".env.node"),
        "-f", compose_file,
        *args,
    ]


def _custom_compose_up(compose_file: str, *services: str) -> None:
    """Bring up custom shard services via docker-compose.

    When *services* is empty, starts all services defined in the compose
    file (``docker-compose up -d``).  When specific service names are
    given, only those services are started (``docker-compose up -d <svc>``).
    """
    tests_dir = _get_tests_dir()
    label = f"services: {', '.join(services)}" if services else "(all)"
    logging.info("Starting custom shard %s ...", label)
    result = subprocess.run(
        _custom_compose_cmd(compose_file, "up", "-d", *services),
        cwd=tests_dir,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        logging.error(
            "docker-compose up (custom) failed (exit %d):\nstdout: %s\nstderr: %s",
            result.returncode, stdout, stderr,
        )
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr,
        )


def _force_cleanup_custom_containers() -> None:
    """Force-remove all custom shard containers and network by name.

    docker-compose down only removes containers it knows about from the
    compose file's project context.  Leftover containers from a previous
    test or CI run (started from a different compose file) are invisible
    to it.  This function removes them by their fixed container names,
    ensuring ports are released before the next custom shard starts.
    """
    all_custom = [
        CUSTOM_BOOT_CONTAINER,
        "rnode.custom.validator1",
        "rnode.custom.validator2",
        "rnode.custom.validator3",
        CUSTOM_JOINER_CONTAINER,
    ]
    for name in all_custom:
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True,
            check=False,
        )
    subprocess.run(
        ["docker", "network", "rm", CUSTOM_NETWORK],
        capture_output=True,
        check=False,
    )


def _describe_port_owner(port: int) -> str:
    """Best-effort diagnostic information about what owns a port."""
    # Prefer ss (usually present). Fall back to lsof if available.
    commands = [
        ["ss", "-ltnp", f"sport = :{port}"],
        ["lsof", "-nP", "-i", f"TCP:{port}"],
    ]
    outputs: List[str] = []
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            continue
        if result.stdout.strip():
            outputs.append(
                f"$ {' '.join(cmd)}\n{result.stdout.strip()}"
            )
        if result.stderr.strip():
            outputs.append(
                f"$ {' '.join(cmd)} (stderr)\n{result.stderr.strip()}"
            )
    return "\n\n".join(outputs).strip()


def _wait_for_port_free(port: int, timeout: float = 15.0) -> None:
    """Wait until a TCP port is available for binding.

    After stopping containers, the kernel may hold ports in TIME_WAIT
    state for a few seconds.  This function polls until the port is free
    or raises RuntimeError after the timeout.
    """
    deadline = time.time() + timeout
    retry_cleanup_done = False
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return
            except OSError:
                # One extra cleanup pass can clear lingering docker-proxy
                # listeners from aborted/overlapping custom shard teardowns.
                if not retry_cleanup_done and time.time() >= (deadline - timeout / 2):
                    _force_cleanup_custom_containers()
                    retry_cleanup_done = True
                time.sleep(1)
    owner = _describe_port_owner(port)
    details = f"\nPort owner diagnostics:\n{owner}" if owner else ""
    raise RuntimeError(
        f"Port {port} still in use after {timeout}s -- "
        f"leftover container or TIME_WAIT?{details}"
    )


def _wait_for_port_range_free(base: int, count: int = 6, timeout: float = 60.0) -> None:
    """Wait for a range of consecutive ports to be free.

    Each container binds 6 ports (protocol, ext-gRPC, int-gRPC, HTTP,
    discovery, admin-HTTP). The kernel releases each socket's TIME_WAIT
    independently, so checking only the base port is insufficient.
    """
    for offset in range(count):
        _wait_for_port_free(base + offset, timeout=timeout)


def _wait_for_custom_ports_free(num_validators: int, timeout: float = 60.0) -> None:
    """Wait for all custom shard host ports to be free.

    Previous custom shard teardown leaves kernel sockets in TIME_WAIT.
    """
    node_keys = ['boot'] + [f'validator{i + 1}' for i in range(num_validators)]
    for key in node_keys:
        base = _CUSTOM_PORT_BASES[key]
        _wait_for_port_range_free(base, timeout=timeout)


def _wait_for_standalone_ports_free(timeout: float = 60.0) -> None:
    """Wait for all standalone node host ports to be free.

    Previous standalone teardown leaves kernel sockets in TIME_WAIT.
    """
    _wait_for_port_range_free(40460, timeout=timeout)


def _wait_for_port_listening(host: str, port: int, timeout: float = 120.0) -> None:
    """Wait until a TCP port is accepting connections.

    Used to detect when a container's network layer is ready before
    starting dependent containers.  This avoids starting all JVM
    containers simultaneously, which causes memory pressure spikes on
    resource-constrained CI runners.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            try:
                s.connect((host, port))
                return
            except (ConnectionRefusedError, OSError):
                time.sleep(2)
    raise RuntimeError(
        f"Port {host}:{port} not listening after {timeout}s"
    )


def _custom_compose_down(compose_file: str) -> None:
    """Tear down the custom shard via docker-compose."""
    tests_dir = _get_tests_dir()
    logging.info("Stopping custom shard ...")
    subprocess.run(
        _custom_compose_cmd(compose_file, "down", "--volumes", "--remove-orphans"),
        cwd=tests_dir,
        check=False,
    )


def _make_custom_node(docker_client: DockerClient, container_name: str,
                      command_timeout: int) -> Node:
    """Create a Node wrapper for a custom shard container."""
    return Node.from_container_name(
        docker_client=docker_client,
        container_name=container_name,
        command_timeout=command_timeout,
        network=CUSTOM_NETWORK,
    )


def _remove_container_if_exists(docker_client: DockerClient, name: str) -> None:
    """Remove a container by name if it exists, ignoring errors."""
    try:
        container = docker_client.containers.get(name)
        try:
            container.stop(timeout=5)
        except Exception:
            pass
        container.remove(force=True)
    except docker_py.errors.NotFound:
        pass
    except Exception:
        pass


@contextmanager
def start_custom_shard(
    docker_client: DockerClient,
    command_line_options: CommandLineOptions,
    *,
    bonds: List[Tuple[ValidatorIdentity, int]],
    ftt: float = 0.99,
    required_signatures: Optional[int] = None,
    heartbeat: bool = True,
    extra_wallets: Optional[List[Tuple[str, int]]] = None,
    global_cli_options: Optional[Dict[str, str]] = None,
    per_node_cli_options: Optional[Dict[str, Dict[str, str]]] = None,
) -> Generator[CustomShard, None, None]:
    """Start a fully customizable shard for a single test.

    Creates a fresh shard with custom bond weights, fault tolerance threshold,
    and per-node CLI overrides. Handles full lifecycle: genesis generation,
    compose up, wait for Running state, Node wrapper creation, and teardown.

    The shard uses port range 40500-40535 (bootstrap + up to 3 validators),
    a dedicated Docker network (f1r3fly-test-custom), and the same TLS
    certificates as the session-scoped shard. Container names are prefixed
    with ``rnode.custom.`` to avoid conflicts.

    All config overrides are passed as CLI flags after ``run`` (not JVM -D
    system properties), because the node's Configuration.scala does not
    include system properties in its resolution chain.

    Args:
        docker_client: Docker client for container interaction.
        command_line_options: Test session command-line options (for timeouts).
        bonds: List of (ValidatorIdentity, stake) tuples, one per validator
            (max 3). The first entry maps to validator1 TLS cert slot, etc.
        ftt: Fault tolerance threshold (casper.fault-tolerance-threshold).
        required_signatures: Genesis ceremony required signatures.
            Defaults to len(bonds) - 1.
        heartbeat: Whether to enable heartbeat on validator nodes.
            When False, passes ``--heartbeat-enabled=false`` to all validators
            (requires the node's opt[Boolean] heartbeat flag).
        extra_wallets: Optional list of ``(vault_address, balance)`` tuples to
            add to the genesis wallets file.  Use this to fund keys that are
            not among the default genesis wallets (e.g. a joiner that needs
            tokens to bond).
        global_cli_options: CLI options applied to all nodes
            (e.g. {"--synchrony-constraint-threshold": "0.5"}).
        per_node_cli_options: Per-node CLI overrides, keyed by
            "boot", "validator1", "validator2", "validator3".

    Yields:
        CustomShard with node handles ("boot", "validator1", ...) and
        shard metadata.

    Example::

        with start_custom_shard(
            docker_client, command_line_options,
            bonds=[(VALIDATOR1_ID, 60), (VALIDATOR2_ID, 20), (VALIDATOR3_ID, 15)],
            ftt=0.5,
            heartbeat=False,
        ) as shard:
            shard.nodes["validator1"].deploy_string(...)
    """
    tests_dir = _get_tests_dir()
    timeout = command_line_options.node_startup_timeout

    container_names = [CUSTOM_BOOT_CONTAINER]
    for idx in range(len(bonds)):
        container_names.append(f"rnode.custom.validator{idx + 1}")

    genesis_dir: Optional[str] = None
    compose_file: Optional[str] = None
    nodes: Dict[str, Node] = {}

    try:
        genesis_dir = _generate_custom_genesis(bonds, extra_wallets=extra_wallets)

        # Inject heartbeat flag into global CLI options if disabled.
        # The node's --heartbeat-disabled is a dedicated Flag that sets
        # casper.heartbeat.enabled = false in the config. We use a flag
        # (not opt[Boolean]) because Scallop's opt[Boolean] can interfere
        # with HOCON defaults when the option is not provided on the CLI.
        effective_global_opts = dict(global_cli_options or {})
        if not heartbeat:
            effective_global_opts.setdefault("--heartbeat-disabled", "")

        compose_file = _generate_custom_compose(
            tests_dir, bonds, genesis_dir,
            ftt=ftt,
            required_signatures=required_signatures,
            global_cli_options=effective_global_opts,
            per_node_cli_options=per_node_cli_options,
        )

        # Clean any leftover state from a previous run.
        # Force-remove containers by name first -- docker-compose down only
        # removes containers from its own project context and misses
        # leftovers from a previous test or CI job.
        _force_cleanup_custom_containers()
        _custom_compose_down(compose_file)

        # Wait for all custom shard ports to be released (kernel TIME_WAIT).
        # Checking only boot is insufficient -- validator ports may still be
        # held from the previous test's shard, causing BindException on startup.
        # Also wait for the joiner port range: add_peer_to_shard teardown stops
        # the joiner container but doesn't wait for its ports, so a previous
        # test's joiner may still hold 40540-40545 in TIME_WAIT.
        _wait_for_custom_ports_free(len(bonds))
        _wait_for_port_range_free(_CUSTOM_PORT_BASES['joiner'])

        # ── Staggered startup: boot first, then validators ──
        # Starting all JVM containers simultaneously creates a memory
        # pressure spike (~2 GB per JVM for heap + metaspace + stacks).
        # On a 7 GB CI runner with 4 containers, that's ~8 GB simultaneous
        # demand.  Starting boot first and waiting for its HTTP port lets
        # the JVM complete class loading and network initialization before
        # validators compete for memory.

        def _do_staggered_startup() -> None:
            # Phase 1: start boot alone
            _custom_compose_up(compose_file, 'boot')
            boot_http_port = _CUSTOM_PORT_BASES['boot'] + 3  # HTTP API port
            logging.info(
                "Waiting for boot HTTP port %d to accept connections...",
                boot_http_port,
            )
            _wait_for_port_listening('localhost', boot_http_port, timeout=120)
            logging.info("Boot HTTP port is listening, starting validators...")
            # Phase 2: start validators (boot is already running)
            _custom_compose_up(compose_file)

        try:
            _do_staggered_startup()
        except (subprocess.CalledProcessError, RuntimeError):
            logging.warning("Custom shard startup failed, cleaning up and retrying...")
            _force_cleanup_custom_containers()
            _custom_compose_down(compose_file)
            _wait_for_custom_ports_free(len(bonds))
            _wait_for_port_range_free(_CUSTOM_PORT_BASES['joiner'])
            _do_staggered_startup()

        logging.info(
            "Waiting for custom shard nodes to reach Running state (timeout=%ds)...",
            timeout,
        )
        for i, name in enumerate(container_names):
            volume_name = 'boot-data' if i == 0 else f'validator{i}-data'
            _wait_for_running_state(docker_client, name, timeout,
                                    data_volume=volume_name)
        logging.info("All custom shard nodes are in Running state.")

        for i, name in enumerate(container_names):
            key = "boot" if i == 0 else f"validator{i}"
            nodes[key] = _make_custom_node(
                docker_client, name, command_line_options.command_timeout,
            )

        yield CustomShard(
            nodes=nodes,
            network_name=CUSTOM_NETWORK,
            genesis_dir=genesis_dir,
            bootstrap_host=CUSTOM_BOOT_CONTAINER,
        )

    finally:
        for node in nodes.values():
            node.stop_logging()
        if compose_file:
            _custom_compose_down(compose_file)
        _wait_for_custom_ports_free(len(bonds))
        _wait_for_port_range_free(_CUSTOM_PORT_BASES['joiner'])
        if genesis_dir and os.path.exists(genesis_dir):
            shutil.rmtree(genesis_dir, ignore_errors=True)
        if compose_file and os.path.exists(compose_file):
            os.unlink(compose_file)


@contextmanager
def add_peer_to_shard(
    docker_client: DockerClient,
    command_line_options: CommandLineOptions,
    shard: CustomShard,
    joiner_identity: ValidatorIdentity,
    *,
    cli_options: Optional[Dict[str, str]] = None,
) -> Generator[Node, None, None]:
    """Dynamically add a joiner node to a running custom shard.

    Starts a new node container via Docker SDK (not compose), connects it
    to the shard's Docker network, and waits for it to reach Running state.
    The joiner uses auto-generated TLS certificates (no pre-existing cert
    required) and catches up from the bootstrap node.

    On teardown, the joiner container is stopped and removed, and its
    data volume is cleaned up.

    Args:
        docker_client: Docker client for container interaction.
        command_line_options: Test session command-line options (for timeouts).
        shard: The running custom shard to join (from start_custom_shard).
        joiner_identity: The joiner's validator identity (for block signing
            after bonding).
        cli_options: Additional CLI options for the joiner node.

    Yields:
        A Node wrapper connected to the joiner.

    Example::

        with start_custom_shard(...) as shard:
            with add_peer_to_shard(
                docker_client, command_line_options,
                shard, VALIDATOR4_ID,
            ) as joiner:
                joiner.deploy_string(...)
    """
    tests_dir = _get_tests_dir()
    repo_root = os.path.dirname(tests_dir)
    abs_conf = os.path.join(repo_root, 'conf')
    abs_local_conf = os.path.join(tests_dir, 'conf')
    timeout = command_line_options.node_startup_timeout

    container_name = CUSTOM_JOINER_CONTAINER
    joiner_volume = 'joiner-data'

    bootstrap_url = (
        f"rnode://{BOOTSTRAP_NODE_ID}@{shard.bootstrap_host}"
        f"?protocol=40400&discovery=40404"
    )

    # The joiner uses default.conf (genesis-validator-mode = false,
    # ceremony-master-mode = false by default). Role-specific behavior
    # is controlled via CLI flags — joiner doesn't get --genesis-validator
    # or --ceremony-master-mode.
    rust = _is_rust_node()
    command = ([] if rust else [
        "-Dlogback.configurationFile=/var/lib/rnode/logback.xml",
    ]) + [
        "run",
        f"--host={container_name}",
        "--allow-private-addresses",
        f"--bootstrap={bootstrap_url}",
        f"--validator-public-key={joiner_identity.public_hex}",
        f"--validator-private-key={joiner_identity.private_hex}",
        # The HOCON default (2) can exceed the actual bond count in
        # small custom shards, causing a silent crash.  Set to 0 to be safe.
        "--required-signatures=0",
    ] + ([] if rust else [
        "--disable-mergeable-channel-gc",
    ])
    if cli_options:
        for k, v in sorted(cli_options.items()):
            if v:
                command.append(f"{k}={v}")
            else:
                command.append(k)

    base_port = _CUSTOM_PORT_BASES['joiner']
    ports = {f"4040{p}/tcp": base_port + p for p in range(6)}

    volumes = {
        joiner_volume: {
            'bind': '/var/lib/rnode/', 'mode': 'rw',
        },
        os.path.join(abs_conf, 'default.conf'): {
            'bind': '/var/lib/rnode/rnode.conf', 'mode': 'ro',
        },
        os.path.join(shard.genesis_dir, 'bonds.txt'): {
            'bind': '/var/lib/rnode/genesis/bonds.txt', 'mode': 'ro',
        },
        os.path.join(shard.genesis_dir, 'wallets.txt'): {
            'bind': '/var/lib/rnode/genesis/wallets.txt', 'mode': 'ro',
        },
    }
    if not rust:
        volumes[os.path.join(abs_local_conf, 'logback.xml')] = {
            'bind': '/var/lib/rnode/logback.xml', 'mode': 'ro',
        }

    container = None
    node: Optional[Node] = None

    try:
        _remove_container_if_exists(docker_client, container_name)

        # Wait for joiner ports to be released (kernel TIME_WAIT from a
        # previous test's joiner container that was recently stopped).
        _wait_for_port_range_free(_CUSTOM_PORT_BASES['joiner'])

        # Match the custom shard's JVM tuning so the joiner doesn't
        # claim more RAM than its share (see _generate_custom_compose).
        joiner_env = ['OPENAI_ENABLED=false']
        if not rust:
            joiner_total = len(shard.nodes) + 1  # existing nodes + joiner
            joiner_ram_pct = 90.0 / joiner_total
            joiner_java_opts = (
                f'-XX:MaxRAMPercentage={joiner_ram_pct:.1f}'
                f' -XX:MaxDirectMemorySize=128M'
            )
            joiner_env.append(f'_JAVA_OPTIONS={joiner_java_opts}')

        logging.info("Starting joiner %s on network %s ...",
                     container_name, shard.network_name)
        container = docker_client.containers.run(
            image=DEFAULT_IMAGE,
            name=container_name,
            user='root',
            network=shard.network_name,
            command=command,
            ports=ports,
            volumes=volumes,
            environment=joiner_env,
            pull=False,
            detach=True,
        )

        _wait_for_running_state(docker_client, container_name, timeout)

        node = _make_custom_node(
            docker_client, container_name, command_line_options.command_timeout,
        )
        yield node

    finally:
        if node:
            node.stop_logging()
        if container:
            try:
                container.stop(timeout=10)
            except Exception:
                pass
            try:
                container.remove(force=True)
            except Exception:
                pass
        try:
            docker_client.volumes.get(joiner_volume).remove()
        except Exception:
            pass
        _wait_for_port_range_free(_CUSTOM_PORT_BASES['joiner'])

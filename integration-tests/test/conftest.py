"""Integration test fixtures.

This file contains ONLY pytest hooks and fixture definitions.
All infrastructure logic lives in infra/ modules.
"""
from __future__ import annotations

import uuid

import pytest

import logging

from .infra.cleanup import CleanupRegistry
from .infra.config import NodeConf, ResourcePaths, ShardConfig, TimeoutConfig
from .infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from .infra.node import Node
from .infra.ports import PortAllocator
from .infra.providers.docker import DockerProvider
from .infra.shard import Shard
from .infra.timeouts import TimeoutHierarchy


# ── Hooks ────────────────────────────────────────────────────────────


def pytest_addoption(parser):
    group = parser.getgroup("f1r3fly", "F1R3FLY test framework options")
    group.addoption(
        "--startup-timeout", type=int, default=90,
        help="Max seconds for a node to reach Running state",
    )
    group.addoption(
        "--timeout-scale", type=float, default=1.0,
        help="Multiplier for all timeouts (CI: 1.5 for slow runners)",
    )
    group.addoption(
        "--skip-setup", action="store_true", default=False,
        help="Skip shard creation (assume already running). Requires --session-id.",
    )
    group.addoption(
        "--session-id", action="store", default=None,
        help="Session ID of an existing shard to adopt "
             "(for --skip-setup; printed by --keep-running runs)",
    )
    group.addoption(
        "--keep-running", action="store_true", default=False,
        help="Don't tear down shard after tests",
    )
    group.addoption(
        "--monitor", action="store_true", default=False,
        help="Enable resource monitoring (logs peak memory/CPU per container)",
    )


def pytest_configure(config):
    """Validate option combinations before any test runs."""
    if config.getoption("--skip-setup") and not config.getoption("--session-id"):
        raise pytest.UsageError(
            "--skip-setup requires --session-id <id>. "
            "The session ID is printed by a prior `shardctl test --keep-running` run."
        )


def pytest_sessionstart(session):
    """Clean up stale resources from crashed sessions."""
    CleanupRegistry.cleanup_stale_sessions()


def pytest_sessionfinish(session, exitstatus):
    """Belt-and-suspenders cleanup."""
    CleanupRegistry.cleanup_stale_sessions()


# ── Session-scoped fixtures ──────────────────────────────────────────


@pytest.fixture(scope="session")
def session_id() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="session")
def timeout_config(request) -> TimeoutConfig:
    return TimeoutConfig(
        node_startup=request.config.getoption("--startup-timeout"),
        scale=request.config.getoption("--timeout-scale"),
    )


@pytest.fixture(scope="session")
def timeouts(timeout_config) -> TimeoutHierarchy:
    return TimeoutHierarchy(timeout_config)


@pytest.fixture(scope="session")
def port_allocator(request) -> PortAllocator:
    worker_id = getattr(request.config, "workerinput", {}).get("workerid", "")
    return PortAllocator(worker_id=worker_id)


@pytest.fixture(scope="session")
def cleanup_registry(request, session_id) -> CleanupRegistry:
    keep = request.config.getoption("--keep-running")
    registry = CleanupRegistry(session_id, keep_running=keep)
    if keep:
        # Make the session_id visible so the user can reuse it on the next
        # invocation via `--skip-setup --session-id <id>`.
        logging.warning(
            "Session %s started with --keep-running. "
            "To reuse this shard: `pytest --skip-setup --session-id %s`",
            session_id, session_id,
        )
    yield registry
    registry.cleanup_all()


@pytest.fixture(scope="session")
def node_conf() -> NodeConf:
    """Effective node configuration parsed from defaults.conf + rust.conf."""
    return NodeConf.resolve()


@pytest.fixture(scope="session")
def resource_paths() -> ResourcePaths:
    return ResourcePaths.resolve()


@pytest.fixture(scope="session")
def provider(
    port_allocator, cleanup_registry, timeouts, resource_paths
) -> DockerProvider:
    return DockerProvider(
        port_allocator=port_allocator,
        registry=cleanup_registry,
        timeouts=timeouts,
        paths=resource_paths,
    )


# ── Shared shard fixtures ───────────────────────────────────────────


@pytest.fixture(scope="session")
def shared_shard(request, provider, timeouts) -> Shard:
    """Session-scoped 3-validator shard (boot + v1 + v2 + v3).

    Used by tests that need a pre-running shard. Tests that modify
    the shard (crash nodes, deplete wallets) should create their own
    via ``provider.create_shard()`` instead.

    With ``--skip-setup --session-id <id>``, adopts an existing shard
    from a previous ``--keep-running`` run instead of creating a fresh one.
    """
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        heartbeat=True,
        include_readonly=True,
    )

    if request.config.getoption("--skip-setup"):
        adopted_id = request.config.getoption("--session-id")
        handles = provider.adopt_session(adopted_id)
        shard = Shard.from_handles(provider, handles, config, timeouts)
        yield shard
        shard.destroy()  # no-op for adopted shards
        return

    shard = Shard.create(provider, config, timeouts)
    yield shard
    shard.destroy()  # no-ops if provider.keep_running


# ── Convenience fixtures for shared shard nodes ──────────────────────


@pytest.fixture(scope="session")
def boot_node(shared_shard) -> Node:
    return shared_shard.boot


@pytest.fixture(scope="session")
def validator1_node(shared_shard) -> Node:
    return shared_shard.node("validator1")


@pytest.fixture(scope="session")
def validator2_node(shared_shard) -> Node:
    return shared_shard.node("validator2")


@pytest.fixture(scope="session")
def validator3_node(shared_shard) -> Node:
    return shared_shard.node("validator3")


@pytest.fixture(scope="session")
def readonly_node(shared_shard) -> Node:
    return shared_shard.readonly


@pytest.fixture(scope="session")
def validator_nodes(shared_shard) -> list:
    return shared_shard.validators


@pytest.fixture(scope="session")
def all_nodes(shared_shard) -> list:
    return shared_shard.all_nodes


# ── Log scanning ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def check_node_logs_after_test(provider):
    """Post-test log scan for panics on all active nodes.

    Runs after every test (shared, custom, standalone). Queries the
    provider for all active node handles and scans their logs for
    PANIC entries via the provider-agnostic ``handle.logs()`` method.

    Per-test (not per-session) so it pinpoints which test caused the
    panic and fails fast before teardown destroys the evidence.

    Provider-agnostic: works with Docker, Kubernetes, or any future
    provider that implements the NodeHandle protocol.

    Currently checks for PANIC only. ERROR/WARN whitelist will be built
    incrementally by running tests and triaging normal log entries.
    """
    yield

    from .infra.log_events import scan_for_errors, format_errors

    all_errors = []
    for handle in provider.active_handles:
        try:
            logs = handle.logs()
        except Exception:
            continue
        errors = scan_for_errors(logs, handle.name)
        critical = [e for e in errors if e.level == "PANIC"]
        all_errors.extend(critical)

    if all_errors:
        pytest.fail(format_errors(all_errors), pytrace=False)


# ── Resource monitoring ──────────────────────────────────────────────


@pytest.fixture(scope="session", autouse=True)
def resource_monitor(request):
    """Sample Docker resource usage during the test session.

    Enabled with ``--monitor``. Discovers all ``rnode.test.*`` containers
    dynamically via ``docker stats`` — sees all containers globally,
    including those created by other xdist workers.

    In parallel mode, only the first worker (gw0) or the master process
    runs the monitor to avoid duplicate sampling. The monitor's global
    Docker discovery ensures it captures containers from all workers.
    """
    if not request.config.getoption("--monitor"):
        yield None
        return

    # In xdist parallel mode, only run monitor on gw0 (or master)
    worker_id = getattr(request.config, "workerinput", {}).get("workerid", "")
    if worker_id and worker_id != "gw0":
        yield None
        return

    from .infra.resource_monitor import ResourceMonitor

    monitor = ResourceMonitor(interval=5.0)
    monitor.start()
    yield monitor
    monitor.stop()
    logging.info(monitor.report())

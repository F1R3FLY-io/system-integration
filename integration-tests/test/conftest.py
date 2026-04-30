"""Integration test fixtures.

This file contains ONLY pytest hooks and fixture definitions.
All infrastructure logic lives in infra/ modules.
"""
from __future__ import annotations

import uuid

import pytest

import logging

from .infra.cleanup import DockerCleanupRegistry
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
    group.addoption(
        "--provider", action="store", default="docker",
        choices=["docker", "subprocess"],
        help="Infrastructure backend: 'docker' (default) spawns nodes as "
             "containers; 'subprocess' spawns the locally-built node binary "
             "directly on the host (set F1R3FLY_NODE_BINARY or build "
             "services/f1r3node-rust first).",
    )


def pytest_configure(config):
    """Validate option combinations before any test runs."""
    if config.getoption("--skip-setup") and not config.getoption("--session-id"):
        raise pytest.UsageError(
            "--skip-setup requires --session-id <id>. "
            "The session ID is printed by a prior `shardctl test --keep-running` run."
        )

    config.addinivalue_line(
        "markers",
        "allow_forbidden_patterns(*keys): exempt this test from named "
        "FORBIDDEN_PATTERNS keys (e.g. 'RecordingInvalidBlock'). Use only "
        "when the test legitimately produces the pattern as part of its "
        "verification. See infra/log_events.py for the pattern set.",
    )


def _stale_cleanup_for_provider(provider_choice: str) -> None:
    """Dispatch stale-session cleanup to the right provider.

    Each provider owns its own resource-discovery logic; the conftest hook
    just routes by `--provider`. This is the only place provider awareness
    leaks into the conftest hook layer.
    """
    if provider_choice == "docker":
        DockerCleanupRegistry.cleanup_stale_sessions()
    elif provider_choice == "subprocess":
        from .infra.providers.subprocess import SubprocessProvider
        SubprocessProvider.cleanup_stale_sessions()
    else:
        # Unknown provider — silently skip rather than crash session start.
        pass


def pytest_sessionstart(session):
    """Clean up stale resources from crashed sessions."""
    choice = session.config.getoption("--provider", default="docker")
    _stale_cleanup_for_provider(choice)


def pytest_sessionfinish(session, exitstatus):
    """Belt-and-suspenders cleanup."""
    choice = session.config.getoption("--provider", default="docker")
    _stale_cleanup_for_provider(choice)


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
def node_conf() -> NodeConf:
    """Effective node configuration parsed from defaults.conf + rust.conf."""
    return NodeConf.resolve()


@pytest.fixture(scope="session")
def resource_paths() -> ResourcePaths:
    return ResourcePaths.resolve()


@pytest.fixture(scope="session")
def provider(request, port_allocator, session_id, timeouts, resource_paths):
    """Construct the chosen Provider (Docker or Subprocess).

    Each provider owns its own resource lifecycle. For Docker, a
    `DockerCleanupRegistry` is created inline and passed in. For Subprocess,
    the provider tracks PIDs and data dirs internally; no shared registry.

    Yields the provider; calls `cleanup_all()` on teardown unless
    `--keep-running` is set.
    """
    choice = request.config.getoption("--provider")
    keep = request.config.getoption("--keep-running")

    if keep:
        logging.warning(
            "Session %s started with --keep-running. "
            "To reuse this shard: `pytest --skip-setup --session-id %s`",
            session_id, session_id,
        )

    if choice == "docker":
        registry = DockerCleanupRegistry(session_id, keep_running=keep)
        prov = DockerProvider(
            port_allocator=port_allocator,
            registry=registry,
            timeouts=timeouts,
            paths=resource_paths,
        )
    elif choice == "subprocess":
        from .infra.providers.subprocess import SubprocessProvider
        prov = SubprocessProvider(
            port_allocator=port_allocator,
            session_id=session_id,
            keep_running=keep,
            timeouts=timeouts,
            paths=resource_paths,
        )
    else:
        raise pytest.UsageError(f"unknown --provider: {choice!r}")

    yield prov
    prov.cleanup_all()


# ── Shared shard fixtures ───────────────────────────────────────────


@pytest.fixture(scope="session")
def shared_shard(request, provider, timeouts) -> Shard:
    """Session-scoped 3-validator shard (boot + v1 + v2 + v3).

    Used by tests that need a pre-running shard. Tests that modify
    the shard (crash nodes, deplete wallets) should create their own
    via ``provider.create_shard()`` instead.

    Seeds vaults for VALIDATOR4_ID and VALIDATOR5_ID at genesis. Existing
    tests do not depend on these wallets being absent; bonding tests
    (`tests/shared/test_bonding_validators.py`) add the joiners mid-session
    and the bond deploys are signed by V4 / V5 keys, which require their
    vaults to exist for phlo + stake.

    With ``--skip-setup --session-id <id>``, adopts an existing shard
    from a previous ``--keep-running`` run instead of creating a fresh one.
    """
    from .infra.keys import VALIDATOR4_ID, VALIDATOR5_ID
    joiner_balance = 50_000_000_000_000_000
    extra_wallets = [
        (
            ident.private_key().get_public_key().get_vault_address(),
            joiner_balance,
        )
        for ident in (VALIDATOR4_ID, VALIDATOR5_ID)
    ]
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        heartbeat=True,
        include_readonly=True,
        extra_wallets=extra_wallets,
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
def check_node_logs_after_test(request, provider):
    """Post-test log scan for panics AND forbidden patterns on all active
    nodes.

    Runs after every test (shared, custom, standalone). Queries the
    provider for all active node handles and scans their logs:
      * PANIC entries — always fail
      * FORBIDDEN_PATTERNS — fail unless the test opts out via
        @pytest.mark.allow_forbidden_patterns(<key>, ...). Pattern keys
        are defined in infra/log_events.py (e.g. "InvalidBondsCache",
        "RecordingInvalidBlock", "DAGStorageMissingHash").

    Per-test (not per-session) so it pinpoints which test caused the
    failure and fails fast before teardown destroys the evidence.

    ERROR/WARN whitelist scanning is still future work — see
    docs/TODO.md.
    """
    yield

    from .infra.log_events import (
        scan_for_errors,
        scan_for_forbidden,
        format_errors,
    )

    # Collect opt-out keys from this test's markers.
    allowed = frozenset()
    for marker in request.node.iter_markers("allow_forbidden_patterns"):
        allowed = allowed | frozenset(marker.args)

    fatal: list = []
    for handle in provider.active_handles:
        try:
            logs = handle.logs()
        except Exception:
            continue
        fatal.extend(
            e for e in scan_for_errors(logs, handle.name) if e.level == "PANIC"
        )
        fatal.extend(scan_for_forbidden(logs, handle.name, allowed))

    if fatal:
        pytest.fail(format_errors(fatal), pytrace=False)


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

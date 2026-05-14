"""Integration test fixtures.

This file contains ONLY pytest hooks and fixture definitions.
All infrastructure logic lives in infra/ modules.
"""
from __future__ import annotations

import time
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
        "--startup-timeout", type=int, default=None,
        help="Override max seconds for a node to reach Running state. "
             "Default comes from TimeoutConfig.node_startup (currently 300s) "
             "— the dataclass is the single source of truth.",
    )
    group.addoption(
        "--timeout-scale", type=float, default=None,
        help="Override timeout scale multiplier. Default comes from "
             "TimeoutConfig.scale (currently 1.0). Use 1.5 on slow CI runners.",
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
    """Clean up stale resources from crashed sessions.

    Runs only on the xdist controller (or in non-xdist runs). Workers
    inherit a clean slate from this single sweep before any test starts
    — running the scan in each worker would race with peers mid-test
    and risk classifying their transiently-exited containers as stale.
    """
    if hasattr(session.config, "workerinput"):
        return
    choice = session.config.getoption("--provider", default="docker")
    _stale_cleanup_for_provider(choice)


# ── Session-scoped fixtures ──────────────────────────────────────────


@pytest.fixture(scope="session")
def session_id() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture(scope="session")
def timeout_config(request) -> TimeoutConfig:
    # CLI options are overrides, not defaults. None → use the
    # TimeoutConfig dataclass default. This keeps the dataclass as
    # the single source of truth.
    overrides = {}
    startup = request.config.getoption("--startup-timeout")
    if startup is not None:
        overrides["node_startup"] = startup
    scale = request.config.getoption("--timeout-scale")
    if scale is not None:
        overrides["scale"] = scale
    return TimeoutConfig(**overrides)


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


# ── Shared shard health check ───────────────────────────────────────


@pytest.fixture(autouse=True)
def _shared_shard_health_check(request):
    """Fail-fast skip if a prior test left ``shared_shard`` in a broken state.

    Only fires for tests that depend on ``shared_shard`` (directly or
    via convenience fixtures like ``boot_node``, ``validator1_node``,
    etc. — pytest's transitive fixture closure handles that). No-op
    for standalone and custom-shard tests.

    Catches the cascading-shared-shard pattern: when one test
    destabilizes a shard node, every subsequent test on the same xdist
    worker that uses ``shared_shard`` would otherwise wait up to 450s
    in ``wait_for_node_running`` (or longer in block-visibility polls)
    before its own assertion times out. This guard surfaces that
    condition in milliseconds via ``is_running()`` on each node,
    issuing a SKIP (not a FAIL) so the cascade-causing test stays the
    visible failure in PR signal.

    Out of scope: custom-shard cascade (each test creates its own
    custom shard; failures there are usually environmental
    worker-degradation, not shared-fixture state).
    """
    if "shared_shard" not in request.fixturenames:
        yield
        return

    shard = request.getfixturevalue("shared_shard")
    not_running = [n for n in shard.all_nodes if not n.is_running()]
    if not_running:
        names = ", ".join(n.name for n in not_running)
        pytest.skip(
            f"shared_shard pre-test health check failed: nodes not "
            f"running ({names}). A prior test in this xdist worker's "
            f"queue destabilized the shared shard. The cascade-causing "
            f"test is the FIRST failure in this worker's log — fix that "
            f"and this test will run again."
        )
    yield


# ── Custom-shard cascade guard ──────────────────────────────────────


def _is_custom_shard_test(item) -> bool:
    """Tests under ``tests/custom/`` each build their own multi-node shard.

    They share the cascade pattern: one shard-bringup failure on the xdist
    worker poisons subsequent shard creations on that worker (suspected
    environmental causes — leaked ports, TIME_WAIT sockets, fd pressure).
    The guard only fires for tests matching this directory.
    """
    return "tests/custom/" in str(item.fspath)


def _extract_error_line(longrepr: str) -> str:
    """Pull the "E   <Type>: <message>" line out of a pytest longrepr.

    Falls back to the first longrepr line if the standard format isn't
    present. Truncated to 200 chars to keep skip messages readable.
    """
    for line in longrepr.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("E   "):
            return stripped[4:].strip()[:200]
    return longrepr.split("\n")[0][:200]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Detect shard bring-up failures and mark this xdist worker
    appropriately so subsequent dependent tests can fail-fast.

    Two cascade patterns covered:

    1. **Custom-shard worker degradation** — a test in ``tests/custom/``
       creates its own multi-node shard, and the bring-up fails (node
       exits before reaching Running). Subsequent custom-shard tests on
       the same worker tend to fail the same way (environmental
       degradation: leaked ports, TIME_WAIT sockets, fd pressure).
       Trigger: ``call`` phase failure on a ``tests/custom/`` test.

    2. **shared_shard initial-creation cascade** — the session-scoped
       ``shared_shard`` fixture fails during setup (typically: boot
       didn't reach Running). pytest caches the exception and re-raises
       it for every dependent test, producing 20+ ERRORs at setup that
       all trace to the same root cause. Trigger: ``setup`` phase
       failure on any test that declares ``shared_shard`` in its
       fixture closure.

    Once set, each flag persists for the rest of the xdist worker's
    lifetime — workers do not self-heal from either pattern.
    """
    outcome = yield
    report = outcome.get_result()
    if not report.failed:
        return
    longrepr = str(report.longrepr or "")
    is_shard_bringup_failure = (
        "exited before reaching Running" in longrepr
        or "did not reach Running" in longrepr
        or "docker compose up failed" in longrepr
    )
    if not is_shard_bringup_failure:
        return

    error_line = _extract_error_line(longrepr)

    # Custom-shard worker degradation (test body failure in tests/custom/)
    if report.when == "call" and _is_custom_shard_test(item):
        item.session._custom_shard_worker_degraded = True
        item.session._custom_shard_degradation_at = time.time()
        item.session._custom_shard_degradation_reason = error_line

    # shared_shard initial-creation cascade (setup-phase failure of a
    # shared_shard-dependent test = the fixture itself failed to come up)
    if report.when == "setup" and "shared_shard" in item.fixturenames:
        item.session._shared_shard_init_failed = True
        item.session._shared_shard_init_failure_at = time.time()
        item.session._shared_shard_init_failure_reason = error_line


def pytest_runtest_setup(item):
    """Pre-fixture-resolution guard: skip if shared_shard's initial
    creation failed earlier in this xdist worker's session.

    pytest caches a failed session-scoped fixture's exception and
    re-raises it for every dependent test's setup phase. Without this
    hook, each dependent test ERRORs with the same cached boot timeout
    (300s × dozens of tests = many minutes wasted). With it, only the
    first test errors (the actual cascade trigger); subsequent tests
    skip in ~1ms with a pointer to the root cause.

    Runs before any fixture resolution because it's a hook, not a
    function-scope autouse fixture — those run AFTER session-scope
    fixtures and would never get a chance when shared_shard's cached
    exception is re-raised at setup.
    """
    if "shared_shard" not in item.fixturenames:
        return
    session = item.session
    if not getattr(session, "_shared_shard_init_failed", False):
        return
    elapsed = time.time() - session._shared_shard_init_failure_at
    reason = session._shared_shard_init_failure_reason
    pytest.skip(
        f"Skipping shared_shard test: shared_shard fixture failed to "
        f"instantiate {elapsed:.0f}s ago in this xdist worker — {reason!r}. "
        f"All subsequent shared_shard tests will skip until investigated. "
        f"Check the FIRST shared_shard ERROR in this worker's log."
    )


@pytest.fixture(autouse=True)
def _custom_shard_cascade_guard(request):
    """Skip custom-shard tests after a prior shard-bring-up failure on
    this xdist worker.

    Works in tandem with the ``pytest_runtest_makereport`` hook: when a
    prior test in ``tests/custom/`` failed because a node didn't reach
    Running, the hook marks the worker degraded; this fixture sees the
    flag on subsequent custom-shard tests and skips them fast (~10ms)
    with a message pointing back to the original failure — instead of
    each test independently spending its full bring-up timeout (up to
    several minutes) failing the same way.
    """
    if not _is_custom_shard_test(request.node):
        yield
        return
    session = request.node.session
    if not getattr(session, "_custom_shard_worker_degraded", False):
        yield
        return
    elapsed = time.time() - session._custom_shard_degradation_at
    reason = session._custom_shard_degradation_reason
    pytest.skip(
        f"Skipping custom-shard test: a prior test in this xdist worker "
        f"failed to bring up its shard {elapsed:.0f}s ago — {reason!r}. "
        f"Worker environment is likely degraded (suspected leaked ports / "
        f"TIME_WAIT sockets / fd pressure). Fix the FIRST custom-shard "
        f"failure in this worker's log; subsequent tests will run again."
    )
    yield


# ── Log scanning ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def check_node_logs_after_test(request, provider):
    """Post-test log scan for forbidden patterns on all active nodes.

    Runs after every test (shared, custom, standalone). Queries the
    provider for all active node handles and runs ``scan_for_forbidden``
    on each node's logs (via the provider-agnostic ``handle.logs()``
    method).

    Patterns are defined in ``infra/log_events.py`` as a single
    ``FORBIDDEN_PATTERNS`` dict — covers panics, KvStore failures,
    bonds-cache mismatches, missing DAG hashes, and other consensus or
    runtime bug signatures. Tests that legitimately exercise a known
    bug class can opt out per-pattern via
    ``@pytest.mark.allow_forbidden_patterns("KeyA", "KeyB", ...)``.

    Per-test (not per-session) so the failing test name surfaces, not
    whatever ran last, and the failure fires before teardown destroys
    the evidence.

    Provider-agnostic: works with Docker, Subprocess, K8s, or any
    future provider that implements the NodeHandle protocol.
    """
    yield

    from .infra.log_events import scan_for_forbidden, format_errors

    # Collect opt-out keys from this test's markers.
    allowed = frozenset()
    for marker in request.node.iter_markers("allow_forbidden_patterns"):
        allowed = allowed | frozenset(marker.args)

    forbidden: list = []
    for handle in provider.active_handles:
        try:
            logs = handle.logs()
        except Exception:
            continue
        forbidden.extend(scan_for_forbidden(logs, handle.name, allowed))

    # Also scan logs from transient nodes that were attached and
    # detached during this test (e.g., observers attached via the
    # ``add_observer`` context manager). The provider snapshots each
    # node's log content before its handle is removed; without this
    # path, panics on transient nodes silently escape the scanner.
    for snapshot in getattr(provider, "retired_log_snapshots", []):
        forbidden.extend(
            scan_for_forbidden(snapshot.log_text, snapshot.name, allowed)
        )
    if hasattr(provider, "clear_retired_log_snapshots"):
        provider.clear_retired_log_snapshots()

    if forbidden:
        pytest.fail(format_errors(forbidden), pytrace=False)


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

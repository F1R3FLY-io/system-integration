"""Provider and NodeHandle protocols.

These define the contract between the test framework (Node, Shard) and
the infrastructure layer (Docker, Kubernetes). Tests interact with
Node and Shard objects; providers create the underlying resources.

Using ``typing_extensions.Protocol`` for structural subtyping — provider
implementations don't need to inherit from these classes, just match
the method signatures.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import typing_extensions

from ..config import NodeConfig, ShardConfig
from ..types import PortMapping

logger = logging.getLogger(__name__)


@dataclass
class RetiredLogSnapshot:
    """Frozen log content captured from a node before its data dir was
    destroyed. Used by the autouse log-scanner fixture so transient
    nodes (attached via ``add_observer`` / ``add_joiner`` context
    managers) don't escape the scanner when their handles are removed
    from ``active_handles`` at context exit.
    """

    name: str
    log_text: str


class NodeHandle(typing_extensions.Protocol):
    """Provider-specific handle to a running node.

    Tests never interact with NodeHandle directly — they use the
    ``Node`` wrapper which provides pyf1r3fly-based blockchain
    operations on top of the handle's connectivity info.
    """

    @property
    def name(self) -> str:
        """Container/pod name (e.g., 'rnode.test.a3f7b2c1.validator1')."""
        ...

    @property
    def ports(self) -> PortMapping:
        """Host-accessible port mapping for this node."""
        ...

    @property
    def grpc_host(self) -> str:
        """Hostname for gRPC connections (e.g., 'localhost' for Docker,
        service FQDN for K8s)."""
        ...

    @property
    def network_name(self) -> str:
        """Docker network or K8s namespace this node belongs to."""
        ...

    def logs(self, tail: Optional[int] = None) -> str:
        """Return the node's log content.

        Source is provider-specific: Docker reads from the file written
        by the node's file sink; subprocess reads the captured log file.
        Returns an empty string if no log content is available yet.
        """
        ...

    def archive_log(self, dest_path: Path) -> None:
        """Persist the node's complete log to ``dest_path``.

        Called during teardown so logs survive the destruction of
        container/process resources. Implementations should:
          - Create parent directories as needed.
          - Capture the COMPLETE log (no tail truncation).
          - Be exception-safe — failures are logged, never propagated.
          - Always produce a file at ``dest_path`` (write a diagnostic
            placeholder on failure) so artifact uploaders never silently
            drop the entry.
        """
        ...

    def is_running(self) -> bool:
        """Check if the node's process is still alive."""
        ...

    def restart(self) -> None:
        """Restart the node (Docker restart / K8s pod delete)."""
        ...

    def pause(self) -> None:
        """Pause the node to simulate network partition.

        Docker: ``docker pause``. K8s: network policy or pod eviction.
        Local: ``kill -STOP``.
        """
        ...

    def unpause(self) -> None:
        """Resume a paused node.

        Docker: ``docker unpause``. K8s: remove network policy.
        Local: ``kill -CONT``.
        """
        ...

    def exit_code(self) -> Optional[int]:
        """Return the node's exit code, or None if still running.

        Docker: ``docker inspect`` exit code. K8s: pod termination status.
        Local: ``waitpid`` with ``WNOHANG``.
        """
        ...

    def wait_for_exit(self, timeout: int = 180) -> Optional[int]:
        """Wait for the node to exit. Returns exit code or None on timeout."""
        ...

    def resource_usage(self) -> dict:
        """Return current resource usage for this node.

        Returns a dict with keys: ``memory_mb``, ``cpu_percent``,
        ``memory_limit_mb``.

        Docker: ``docker stats --no-stream``.
        K8s: ``kubectl top pod``.
        Local: ``/proc/{pid}/status``.
        """
        ...

    def stop(self) -> None:
        """Stop the node without removing resources."""
        ...

    def remove(self) -> None:
        """Force-remove the node and its resources."""
        ...


class Provider(typing_extensions.Protocol):
    """Infrastructure provider: creates and destroys nodes.

    Implementations:
      - ``DockerProvider``: uses ``docker run`` / ``docker compose``
      - ``K8sProvider``: uses ``helm install`` / ``kubectl`` (future)

    The provider manages the complete lifecycle: network creation,
    volume provisioning, container/pod startup, health checking,
    and teardown. The test framework's ``Shard`` class calls these
    methods but doesn't know which provider it's using.
    """

    @property
    def keep_running(self) -> bool:
        """If True, skip resource destruction on teardown.

        Set via ``--keep-running`` CLI flag. Allows post-failure
        inspection of containers, logs, and state.
        """
        ...

    def create_shard(self, config: ShardConfig) -> List[NodeHandle]:
        """Create all nodes for a shard.

        Returns handles in order: [bootstrap, validator1, ..., readonly].
        Blocks until all nodes reach Running state (provider checks
        logs for the Running marker).
        """
        ...

    def add_node(
        self,
        shard_network: str,
        node_config: NodeConfig,
        bootstrap_handle: NodeHandle,
    ) -> NodeHandle:
        """Add a joiner/observer node to an existing shard network.

        The joiner bootstraps from ``bootstrap_handle``'s address.
        """
        ...

    def remove_node(self, handle: NodeHandle) -> None:
        """Remove a single node and its volume. Used for joiner teardown."""
        ...

    def destroy_shard(self, handles: Sequence[NodeHandle]) -> None:
        """Destroy all shard nodes and associated resources
        (network, volumes, temp files)."""
        ...

    def create_standalone(self, config: NodeConfig) -> NodeHandle:
        """Create a standalone (single-node) shard for isolated tests.

        Uses standalone-dev.conf (instant finalization, no peers).
        """
        ...

    def destroy_standalone(self, handle: NodeHandle) -> None:
        """Destroy a standalone node and its resources."""
        ...

    @property
    def active_handles(self) -> List[NodeHandle]:
        """All currently active node handles (shard, standalone, joiner).

        Used by the log scanning fixture to inspect all node logs
        after each test, regardless of how the nodes were created.
        """
        ...

    @property
    def retired_log_snapshots(self) -> List[RetiredLogSnapshot]:
        """Log snapshots captured from nodes that have been removed via
        ``remove_node`` since the snapshots were last cleared. The
        autouse log-scanner fixture iterates these alongside
        ``active_handles`` so transient nodes (e.g. observers attached
        with the ``add_observer`` context manager) cannot escape
        scanning when their handles are detached at context exit.
        """
        ...

    def clear_retired_log_snapshots(self) -> None:
        """Drop all retired log snapshots. The autouse scanner calls
        this after each test so snapshots from one test don't carry
        forward into the next test's scan.
        """
        ...

    def cleanup_all(self) -> None:
        """Force-cleanup ALL test resources.

        Called at session start (stale cleanup), session end, and
        atexit. Must be idempotent and crash-safe.
        """
        ...

    @classmethod
    def force_cleanup_all_test_resources(cls) -> None:
        """Aggressively remove every test-framework resource on this backend.

        Used by ``shardctl test-reset`` — user-invoked only, never called
        from pytest hooks. Unlike :py:meth:`cleanup_all`, this ignores
        container/pod status: running resources are force-stopped and
        removed too. Provider-specific discovery (Docker: scan container
        names; K8s: scan namespaces by label).
        """
        ...

    def adopt_session(self, session_id: str) -> List[NodeHandle]:
        """Adopt nodes from a previously started session.

        Used by ``pytest --skip-setup --session-id <id>`` to reuse a
        shard left running by a previous ``--keep-running`` invocation.
        Returns handles in the same role order as :py:meth:`create_shard`
        (bootstrap, validators..., readonly).

        Raises if no resources match ``session_id`` or the adoption would
        produce a partial shard.
        """
        ...


# ── Provider-agnostic helpers ───────────────────────────────────────────

_ARCHIVE_BASENAME = "log-archive"


def archive_root_for(integration_tests_dir: str, session_id: str) -> Path:
    """Canonical on-disk location for archived node logs.

    Producing the same path from every provider keeps the artifact upload
    in CI (which captures the entire ``integration-tests/`` tree) language-
    free: there's a single directory, one subdirectory per session.
    """
    return Path(integration_tests_dir) / _ARCHIVE_BASENAME / session_id


def archive_handles(handles: Sequence[NodeHandle], archive_dir: Path) -> None:
    """Archive each handle's full log into ``archive_dir/<handle.name>.log``.

    Providers call this from their destroy/cleanup paths immediately
    before container/process resources are removed. Errors are isolated
    per-handle so one failed archive cannot block the rest of teardown.
    """
    for h in handles:
        try:
            h.archive_log(archive_dir / f"{h.name}.log")
        except Exception as e:  # pragma: no cover — defensive
            logger.warning("archive_handles: %s failed: %s", h.name, e)


def wait_for_handles_or_archive(
    handles: Sequence[NodeHandle],
    archive_dir: Path,
    timeout: int,
) -> None:
    """Wait for each handle to reach Running, archiving logs on failure.

    Providers call this from their spawn sites (``create_shard``,
    ``create_standalone``, ``add_node``) BEFORE the handle is tracked
    in ``_active_handles``. That ordering matters: if the wait raises,
    the framework's normal teardown paths can't see the handle to
    archive it, and the post-mortem rnode log is lost. Archive every
    handle to ``archive_dir`` here before re-raising so each spawned
    process's log survives an early startup failure.
    """
    # Local import to avoid circular dependency on infra.polling at
    # module-load time. base.py is a protocol contract that polling
    # transitively imports.
    from ..polling import wait_for_node_running

    try:
        for h in handles:
            wait_for_node_running(
                get_logs=h.logs,
                is_running=h.is_running,
                node_name=h.name,
                timeout=timeout,
                status_url=f"http://{h.grpc_host}:{h.ports.http}/api/status",
            )
    except Exception:
        archive_handles(handles, archive_dir)
        raise

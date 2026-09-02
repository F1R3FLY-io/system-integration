"""Shard — a collection of Nodes with lifecycle management.

Provides dictionary-style access to nodes by role name and convenience
properties for common patterns (boot, validators, readonly). The
``add_joiner`` context manager handles mid-test joiner attachment and
cleanup.

Created by ``DockerProvider.create_shard()`` or equivalent. Tests
interact with ``Shard`` and ``Node``, never with the provider directly.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Dict, Generator, List, Optional

from .node import Node
from .types import NodeRole, ValidatorIdentity

logger = logging.getLogger(__name__)


class Shard:
    """A running shard: nodes + lifecycle.

    Created via ``Shard.create(provider, config, timeouts)``.
    Destroyed via ``shard.destroy()``.
    """

    def __init__(
        self,
        provider,  # Provider (duck-typed)
        handles: list,  # List[NodeHandle]
        config,  # ShardConfig
        timeouts,  # TimeoutHierarchy
        genesis_dir: Optional[str] = None,
        adopted: bool = False,
    ) -> None:
        self._provider = provider
        self._handles = handles
        self._config = config
        self._timeouts = timeouts
        self._genesis_dir = genesis_dir
        self._adopted = adopted
        self._destroyed = False

        # Build Node wrappers
        self._nodes: Dict[str, Node] = {}
        for handle in handles:
            role = handle.role
            if role == NodeRole.BOOTSTRAP:
                key = "boot"
            elif role == NodeRole.READONLY:
                key = "readonly"
            elif role == NodeRole.VALIDATOR:
                # Derive slot number from container name suffix
                # e.g., "rnode.test.abc123.validator2" → "validator2"
                parts = handle.name.split(".")
                key = parts[-1] if parts else handle.name
            else:
                key = handle.name
            self._nodes[key] = Node(
                handle=handle,
                role=role,
                identity=getattr(handle, "identity", None),
            )

    @classmethod
    def create(cls, provider, config, timeouts) -> "Shard":
        """Create a shard from a provider and config."""
        handles = provider.create_shard(config)
        return cls(
            provider=provider,
            handles=handles,
            config=config,
            timeouts=timeouts,
        )

    @classmethod
    def from_handles(cls, provider, handles, config, timeouts) -> "Shard":
        """Build a Shard around already-running handles (no provider.create_shard).

        Used by the ``--skip-setup --session-id`` path to wrap containers
        adopted from a previous session. ``destroy()`` on an adopted shard
        is a no-op — the shard was kept alive intentionally and will be
        cleaned up explicitly by ``shardctl test-reset`` or the next
        ``shardctl test --keep-running`` cycle.
        """
        return cls(
            provider=provider,
            handles=handles,
            config=config,
            timeouts=timeouts,
            adopted=True,
        )

    @property
    def config(self):
        """The ShardConfig used to create this shard."""
        return self._config

    # ── Node access ─────────────────────────────────────────────────

    @property
    def boot(self) -> Node:
        return self._nodes["boot"]

    @property
    def validators(self) -> List[Node]:
        return [n for key, n in sorted(self._nodes.items()) if n.role == NodeRole.VALIDATOR]

    @property
    def readonly(self) -> Optional[Node]:
        return self._nodes.get("readonly")

    @property
    def all_nodes(self) -> List[Node]:
        return list(self._nodes.values())

    def node(self, name: str) -> Node:
        """Access node by role key: 'boot', 'validator1', 'validator2', etc."""
        if name not in self._nodes:
            available = list(self._nodes.keys())
            raise KeyError(f"Node '{name}' not found in shard. Available: {available}")
        return self._nodes[name]

    @property
    def network_name(self) -> str:
        """Docker network name for this shard."""
        return self._handles[0].network_name if self._handles else ""

    def _attached_node_cli_options(
        self,
        cli_options: Optional[Dict[str, str]],
    ) -> Dict[str, str]:
        options = dict(cli_options or {})
        options.setdefault(
            "--required-signatures",
            str(self._config.effective_required_signatures),
        )
        return options

    # ── Joiner lifecycle ────────────────────────────────────────────

    @contextmanager
    def add_joiner(
        self,
        identity: ValidatorIdentity,
        cli_options: Optional[Dict[str, str]] = None,
        cli_flags: Optional[set] = None,
        wait_running: bool = True,
    ) -> Generator[Node, None, None]:
        """Attach a joiner node to this shard.

        Yields a ``Node`` wrapper. On exit, removes the joiner and
        cleans up its volume. Before removal, the provider snapshots
        the joiner's logs into its ``retired_log_snapshots`` bucket so
        the autouse log scanner still sees any errors emitted while
        the joiner was attached.

        Args:
            wait_running: If True (default), wait for the joiner to reach
                Running state. Set False for tests expecting startup failure.

        Example::

            with shard.add_joiner(VALIDATOR4_ID, cli_options={"--epoch-length": "4"}) as joiner:
                joiner.deploy_string(bond_rholang, VALIDATOR4_ID.private_key())
        """
        from .config import NodeConfig

        node_config = NodeConfig(
            role=NodeRole.JOINER,
            identity=identity,
            cli_flags=frozenset(cli_flags or set()),
            cli_options=self._attached_node_cli_options(cli_options),
        )

        handle = self._provider.add_node(
            shard_network=self.network_name,
            node_config=node_config,
            bootstrap_handle=self._handles[0],  # bootstrap is always first
            wait_running=wait_running,
        )

        joiner_node = Node(
            handle=handle,
            role=NodeRole.JOINER,
            identity=identity,
        )

        try:
            yield joiner_node
        finally:
            joiner_node.close()
            self._provider.remove_node(handle)

    def attach_joiner(
        self,
        identity: ValidatorIdentity,
        cli_options: Optional[Dict[str, str]] = None,
        cli_flags: Optional[set] = None,
        wait_running: bool = True,
    ) -> Node:
        """Attach a persistent joiner to this shard.

        Unlike ``add_joiner`` (context-managed, transient), the joiner
        becomes part of the shard for the remainder of its lifetime:
        addressable via ``shard.node(identity.name)``, included in
        ``shard.all_nodes``, and torn down by ``Shard.destroy()`` at
        session end.

        Use when a test bonds a validator that must remain live so
        consensus state and node liveness stay aligned for subsequent
        tests on the same shard.
        """
        from .config import NodeConfig

        if identity.name in self._nodes:
            raise ValueError(f"Cannot attach joiner '{identity.name}': name already in use")

        node_config = NodeConfig(
            role=NodeRole.JOINER,
            identity=identity,
            cli_flags=frozenset(cli_flags or set()),
            cli_options=self._attached_node_cli_options(cli_options),
        )
        handle = self._provider.add_node(
            shard_network=self.network_name,
            node_config=node_config,
            bootstrap_handle=self._handles[0],
            wait_running=wait_running,
        )
        joiner = Node(handle=handle, role=NodeRole.JOINER, identity=identity)
        self._handles.append(handle)
        self._nodes[identity.name] = joiner
        return joiner

    @contextmanager
    def add_observer(
        self,
        cli_options: Optional[Dict[str, str]] = None,
        cli_flags: Optional[set] = None,
        wait_running: bool = True,
    ) -> Generator[Node, None, None]:
        """Attach a transient readonly observer for the duration of a ``with`` block.

        Symmetric to ``add_joiner`` (context-managed, no identity). On
        exit the observer is removed and its volume cleaned up. Before
        removal, the provider snapshots the observer's logs into its
        ``retired_log_snapshots`` bucket so the autouse log scanner
        still sees any post-attach errors emitted while syncing
        against the live shard.

        Use when a test wants to verify a fresh node can LFS-sync
        against the live shard (production scenario for forward-horizon
        rspace history sync) without leaving the observer alive past
        the test's own assertions. For persistent attachment that
        survives the test, use ``attach_observer``.
        """
        from .config import NodeConfig

        node_config = NodeConfig(
            role=NodeRole.READONLY,
            identity=None,
            cli_flags=frozenset(cli_flags or set()),
            cli_options=self._attached_node_cli_options(cli_options),
        )
        handle = self._provider.add_node(
            shard_network=self.network_name,
            node_config=node_config,
            bootstrap_handle=self._handles[0],
            wait_running=wait_running,
        )
        observer = Node(handle=handle, role=NodeRole.READONLY, identity=None)
        try:
            yield observer
        finally:
            observer.close()
            self._provider.remove_node(handle)

    def attach_observer(
        self,
        cli_options: Optional[Dict[str, str]] = None,
        cli_flags: Optional[set] = None,
        wait_running: bool = True,
    ) -> Node:
        """Attach a persistent readonly observer to this shard.

        Symmetric to ``attach_joiner`` but without a validator identity:
        the observer never proposes (``--heartbeat-disabled``) and only
        syncs + serves reads. Auto-named ``observer1``, ``observer2``,
        ... by the provider; addressable via ``shard.node(name)``.

        Use to verify that a fresh node can LFS-sync against the live
        shard mid-test (the production scenario for forward-horizon
        sync).
        """
        from .config import NodeConfig

        node_config = NodeConfig(
            role=NodeRole.READONLY,
            identity=None,
            cli_flags=frozenset(cli_flags or set()),
            cli_options=self._attached_node_cli_options(cli_options),
        )
        handle = self._provider.add_node(
            shard_network=self.network_name,
            node_config=node_config,
            bootstrap_handle=self._handles[0],
            wait_running=wait_running,
        )
        # Provider names the observer ``observer{n}``; recover from handle.
        observer_key = handle.name.split(".")[-1]
        if observer_key in self._nodes:
            raise ValueError(f"Provider returned duplicate observer name: {observer_key}")
        observer = Node(handle=handle, role=NodeRole.READONLY, identity=None)
        self._handles.append(handle)
        self._nodes[observer_key] = observer
        return observer

    # ── Lifecycle ───────────────────────────────────────────────────

    def destroy(self) -> None:
        """Destroy the shard and all its resources.

        No-op for adopted shards (session reused via ``--skip-setup``):
        the user asked to keep them, so we leave them for explicit
        cleanup via ``shardctl test-reset``.
        """
        if self._destroyed:
            return
        self._destroyed = True
        if self._adopted:
            logger.info("Adopted shard left running (--skip-setup)")
            return
        if not self._provider.keep_running:
            for node in self._nodes.values():
                node.close()
        self._provider.destroy_shard(self._handles)

    def __repr__(self) -> str:
        nodes = ", ".join(f"{k}={v}" for k, v in self._nodes.items())
        return f"Shard({nodes})"

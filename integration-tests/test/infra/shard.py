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
        provider,       # Provider (duck-typed)
        handles: list,  # List[NodeHandle]
        config,         # ShardConfig
        timeouts,       # TimeoutHierarchy
        genesis_dir: Optional[str] = None,
    ) -> None:
        self._provider = provider
        self._handles = handles
        self._config = config
        self._timeouts = timeouts
        self._genesis_dir = genesis_dir
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
        return [
            n for key, n in sorted(self._nodes.items())
            if n.role == NodeRole.VALIDATOR
        ]

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
            raise KeyError(
                f"Node '{name}' not found in shard. Available: {available}"
            )
        return self._nodes[name]

    @property
    def network_name(self) -> str:
        """Docker network name for this shard."""
        return self._handles[0].network_name if self._handles else ""

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
        cleans up its volume.

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
            cli_options=cli_options or {},
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

    # ── Lifecycle ───────────────────────────────────────────────────

    def destroy(self) -> None:
        """Destroy the shard and all its resources."""
        if self._destroyed:
            return
        self._destroyed = True
        if not self._provider.keep_running:
            for node in self._nodes.values():
                node.close()
        self._provider.destroy_shard(self._handles)

    def __repr__(self) -> str:
        nodes = ", ".join(f"{k}={v}" for k, v in self._nodes.items())
        return f"Shard({nodes})"

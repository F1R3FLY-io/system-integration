"""Provider for a shard the harness adopts but does not run.

Nodes come from a :class:`ShardInventory`. The provider reaches them over the
network only: it never starts, stops, restarts or removes a node, and it has
no access to node logs or process resources. Tests that build their own
shard cannot run on it; a test adopts the shard with
``--skip-setup --session-id <inventory shard>``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Sequence

import requests

from ..config import NodeConfig, ShardConfig
from ..inventory import InventoryNode, ShardInventory
from ..types import NodeRole, PortMapping
from .base import RetiredLogSnapshot

logger = logging.getLogger(__name__)

STATUS_TIMEOUT_SECONDS = 5


class RemoteShardError(RuntimeError):
    """An operation the remote provider does not perform, or an adoption check failed."""


def _unsupported(operation: str, name: str) -> RemoteShardError:
    return RemoteShardError(
        f"{operation} is not available for remote node {name}: the harness does not "
        f"control nodes it adopted from an inventory"
    )


def read_status(host: str, http_port: int) -> dict:
    response = requests.get(f"http://{host}:{http_port}/api/status", timeout=STATUS_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


class RemoteNodeHandle:
    """A node reached by address, with no control over its process."""

    def __init__(self, node: InventoryNode, network_id: str, shard: str) -> None:
        self._node = node
        self._network_id = network_id
        self._shard = shard

    @property
    def name(self) -> str:
        return self._node.name

    @property
    def role(self) -> NodeRole:
        return self._node.role

    @property
    def identity(self) -> None:
        return None

    @property
    def ports(self) -> PortMapping:
        return self._node.ports

    @property
    def grpc_host(self) -> str:
        return self._node.host

    @property
    def network_name(self) -> str:
        return self._shard

    def logs(self, tail: Optional[int] = None) -> str:
        raise _unsupported("Reading logs", self.name)

    def archive_log(self, dest_path: Path) -> None:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(
            f"Remote node {self.name} ({self.grpc_host}): its log stays on its host.\n"
        )

    def is_running(self) -> bool:
        """True when the node answers /api/status on the expected network."""
        try:
            status = read_status(self.grpc_host, self.ports.http)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("remote node %s status check failed: %s", self.name, exc)
            return False
        return status.get("networkId") == self._network_id

    def restart(self) -> None:
        raise _unsupported("Restart", self.name)

    def pause(self) -> None:
        raise _unsupported("Pause", self.name)

    def unpause(self) -> None:
        raise _unsupported("Unpause", self.name)

    def exit_code(self) -> Optional[int]:
        raise _unsupported("Reading the exit code", self.name)

    def wait_for_exit(self, timeout: int = 180) -> Optional[int]:
        raise _unsupported("Waiting for exit", self.name)

    def resource_usage(self) -> dict:
        raise _unsupported("Sampling resource usage", self.name)

    def stop(self) -> None:
        raise _unsupported("Stop", self.name)

    def remove(self) -> None:
        raise _unsupported("Remove", self.name)


class RemoteProvider:
    """Adopts the shard an inventory describes. Never changes it."""

    def __init__(self, inventory: ShardInventory) -> None:
        self._inventory = inventory
        self._handles: List[RemoteNodeHandle] = []

    @property
    def inventory(self) -> ShardInventory:
        return self._inventory

    @property
    def keep_running(self) -> bool:
        return True

    @property
    def node_logs_available(self) -> bool:
        return False

    @property
    def active_handles(self) -> List[RemoteNodeHandle]:
        return list(self._handles)

    @property
    def retired_log_snapshots(self) -> List[RetiredLogSnapshot]:
        return []

    def clear_retired_log_snapshots(self) -> None:
        pass

    def adopt_session(self, session_id: str) -> List[RemoteNodeHandle]:
        """Adopt every inventory node after checking it is up on the right network.

        Order matches ``create_shard``: boot, validators, then readonly nodes.
        """
        inventory = self._inventory
        if session_id != inventory.shard:
            raise RemoteShardError(
                f"--session-id {session_id!r} does not name the inventory's shard "
                f"{inventory.shard!r}"
            )
        order = {NodeRole.BOOTSTRAP: 0, NodeRole.VALIDATOR: 1, NodeRole.READONLY: 2}
        nodes = sorted(inventory.nodes, key=lambda n: order[n.role])
        handles = [RemoteNodeHandle(n, inventory.network_id, inventory.shard) for n in nodes]

        problems = []
        for handle in handles:
            try:
                status = read_status(handle.grpc_host, handle.ports.http)
            except (requests.RequestException, ValueError) as exc:
                problems.append(f"{handle.name} ({handle.grpc_host}): unreachable: {exc}")
                continue
            network_id = status.get("networkId")
            if network_id != inventory.network_id:
                problems.append(
                    f"{handle.name} ({handle.grpc_host}): network id {network_id!r}, "
                    f"expected {inventory.network_id!r}"
                )
        if problems:
            raise RemoteShardError(
                "cannot adopt shard " + inventory.shard + ":\n  " + "\n  ".join(problems)
            )

        self._handles = handles
        logger.info(
            "Adopted remote shard %s: %d nodes (%d validators) on network %s",
            inventory.shard,
            len(handles),
            len(inventory.validators),
            inventory.network_id,
        )
        return list(handles)

    def create_shard(self, config: ShardConfig) -> List[RemoteNodeHandle]:
        raise RemoteShardError(
            "the remote provider cannot create a shard; adopt the inventory's shard "
            f"with --skip-setup --session-id {self._inventory.shard}"
        )

    def add_node(self, shard_network, node_config: NodeConfig, bootstrap_handle, **kwargs):
        raise RemoteShardError("the remote provider cannot add nodes to an adopted shard")

    def remove_node(self, handle) -> None:
        raise RemoteShardError("the remote provider cannot remove nodes from an adopted shard")

    def destroy_shard(self, handles: Sequence[RemoteNodeHandle]) -> None:
        raise RemoteShardError("the remote provider cannot destroy an adopted shard")

    def create_standalone(self, config: NodeConfig):
        raise RemoteShardError("the remote provider cannot create a standalone node")

    def destroy_standalone(self, handle) -> None:
        raise RemoteShardError("the remote provider cannot destroy a standalone node")

    def cleanup_all(self) -> None:
        self._handles = []

    def host_process_guardian_token(self) -> Optional[str]:
        return None

    @property
    def monitor_output_dir(self) -> Optional[Path]:
        return None

    @classmethod
    def force_cleanup_all_test_resources(cls) -> None:
        raise RemoteShardError("the remote provider owns no resources to clean up")

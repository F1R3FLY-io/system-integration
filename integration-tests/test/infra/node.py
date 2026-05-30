"""Node — test-facing wrapper around a provider NodeHandle.

All blockchain interaction goes through pyf1r3fly. Node adds no
gRPC/HTTP logic of its own — it constructs F1r3flyClient instances
pointed at the handle's connectivity info.

Key difference from v1: Node holds a ``NodeHandle`` (provider-agnostic),
not a ``docker.Container``.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from f1r3fly.client import F1r3flyClient
from f1r3fly.const import DEFAULT_PHLO_LIMIT, DEFAULT_PHLO_PRICE
from f1r3fly.crypto import PrivateKey
from f1r3fly.pos import PosAPI
from f1r3fly.vault import VaultAPI

from .types import NodeRole, PortMapping, ValidatorIdentity

logger = logging.getLogger(__name__)

# gRPC options for fast failure on dead connections
_GRPC_OPTIONS = (
    ("grpc.enable_retries", 0),
    ("grpc.initial_reconnect_backoff_ms", 500),
    ("grpc.max_reconnect_backoff_ms", 2000),
    ("grpc.keepalive_time_ms", 10000),
    ("grpc.keepalive_timeout_ms", 5000),
    ("grpc.keepalive_permit_without_calls", 1),
    ("grpc.max_send_message_length", 64 * 1024 * 1024),
    ("grpc.max_receive_message_length", 64 * 1024 * 1024),
)


class Node:
    """A running F1r3fly node with known endpoints.

    Tests use this class for all blockchain interaction. The underlying
    infrastructure (Docker container, K8s pod) is abstracted away.
    """

    def __init__(
        self,
        handle,  # NodeHandle (duck-typed, no import needed)
        role: NodeRole,
        identity: Optional[ValidatorIdentity] = None,
    ) -> None:
        self._handle = handle
        self._role = role
        self._identity = identity
        self._grpc_external_client: Optional[F1r3flyClient] = None
        self._grpc_internal_client: Optional[F1r3flyClient] = None
        self._vault_api: Optional[VaultAPI] = None
        self._pos_api: Optional[PosAPI] = None

    @property
    def name(self) -> str:
        return self._handle.name

    @property
    def role(self) -> NodeRole:
        return self._role

    @property
    def identity(self) -> Optional[ValidatorIdentity]:
        return self._identity

    @property
    def ports(self) -> PortMapping:
        return self._handle.ports

    @property
    def grpc_host(self) -> str:
        return self._handle.grpc_host

    @property
    def external_grpc_port(self) -> int:
        return self._handle.ports.grpc_ext

    @property
    def internal_grpc_port(self) -> int:
        return self._handle.ports.grpc_int

    @property
    def http_port(self) -> int:
        return self._handle.ports.http

    @property
    def http_url(self) -> str:
        return f"http://{self.grpc_host}:{self.http_port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.grpc_host}:{self.http_port}/ws/events"

    @property
    def network_name(self) -> str:
        return self._handle.network_name

    # ── HTTP API helpers ──

    def http_get(self, path: str, timeout: int = 60):
        """GET /{path} (no /api prefix) and return the response object."""
        import requests

        url = f"{self.http_url}{path}"
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp

    def api_get(self, path: str, timeout: int = 60) -> dict:
        """GET /api/{path} and return the JSON response."""
        import requests

        url = f"{self.http_url}/api{path}"
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def api_post(self, path: str, json_data=None, timeout: int = 60):
        """POST to /api/{path} and return the response object."""
        import requests

        url = f"{self.http_url}/api{path}"
        resp = requests.post(url, json=json_data, timeout=timeout)
        resp.raise_for_status()
        return resp

    # ── pyf1r3fly gRPC client (lazy, cached) ──

    def _external_client(self) -> F1r3flyClient:
        """gRPC client connected to the external port (DeployService)."""
        if self._grpc_external_client is None:
            self._grpc_external_client = F1r3flyClient(
                self.grpc_host,
                self.external_grpc_port,
                grpc_options=_GRPC_OPTIONS,
            )
        return self._grpc_external_client

    def _internal_client(self) -> F1r3flyClient:
        """gRPC client connected to the internal port (ProposeService)."""
        if self._grpc_internal_client is None:
            self._grpc_internal_client = F1r3flyClient(
                self.grpc_host,
                self.internal_grpc_port,
                grpc_options=_GRPC_OPTIONS,
            )
        return self._grpc_internal_client

    def close(self) -> None:
        if self._grpc_external_client is not None:
            self._grpc_external_client.close()
            self._grpc_external_client = None
        if self._grpc_internal_client is not None:
            self._grpc_internal_client.close()
            self._grpc_internal_client = None
        self._vault_api = None
        self._pos_api = None

    def get_vault(self, shard_id: str = "root") -> VaultAPI:
        """Construct a VaultAPI with the given shard ID."""
        return VaultAPI(self._external_client(), shard_id=shard_id)

    @property
    def vault(self) -> VaultAPI:
        """Lazily construct a VaultAPI backed by this node's gRPC client.

        Uses default shard_id='root'. For dynamic shard_id, use
        ``get_vault(shard_id)`` instead.
        """
        if self._vault_api is None:
            self._vault_api = VaultAPI(self._external_client())
        return self._vault_api

    def get_pos(self, shard_id: str = "root") -> PosAPI:
        """Construct a PosAPI with the given shard ID."""
        return PosAPI(self._external_client(), shard_id=shard_id)

    @property
    def pos(self) -> PosAPI:
        """Lazily construct a PosAPI backed by this node's gRPC client.

        Exploratory reads (get_bonds/get_rewards/get_withdrawers/
        get_pending_withdrawer) only work on a read-only node. Bond/withdraw
        deploys work on any node and are signed by the acting validator's key.
        """
        if self._pos_api is None:
            self._pos_api = PosAPI(self._external_client())
        return self._pos_api

    def exit_code(self) -> Optional[int]:
        """Return the container's exit code, or None if still running."""
        return self._handle.exit_code()

    def wait_for_exit(self, timeout: int = 180) -> Optional[int]:
        """Wait for the container to exit. Returns exit code or None on timeout."""
        return self._handle.wait_for_exit(timeout)

    # ── Deploy operations ──

    def send_deploy(self, deploy_proto) -> str:
        """Submit a pre-built DeployDataProto. Returns deploy ID (signature hex)."""
        return self._external_client().send_deploy(deploy_proto)

    def deploy_string(
        self,
        rholang_code: str,
        private_key: PrivateKey,
        phlo_limit: int = DEFAULT_PHLO_LIMIT,
        phlo_price: int = DEFAULT_PHLO_PRICE,
        valid_after_block_no: Optional[int] = None,
        shard_id: str = "root",
    ) -> str:
        """Deploy Rholang code. Returns the deploy ID (signature hex).

        ``valid_after_block_no`` defaults to the current latest block
        number (auto-filled). Pass an explicit value to override.
        """
        if valid_after_block_no is not None:
            return self._external_client().deploy(
                key=private_key,
                term=rholang_code,
                phlo_price=phlo_price,
                phlo_limit=phlo_limit,
                valid_after_block_no=valid_after_block_no,
                shard_id=shard_id,
            )
        return self._external_client().deploy_with_vabn_filled(
            key=private_key,
            term=rholang_code,
            phlo_price=phlo_price,
            phlo_limit=phlo_limit,
            shard_id=shard_id,
        )

    def deploy_rho_file(
        self,
        rho_file_path: str,
        private_key: PrivateKey,
        substitutions: Optional[Dict[str, str]] = None,
        phlo_limit: int = DEFAULT_PHLO_LIMIT,
        phlo_price: int = DEFAULT_PHLO_PRICE,
        valid_after_block_no: Optional[int] = None,
        shard_id: str = "root",
    ) -> str:
        """Deploy a .rho file with optional string substitutions.

        Relative paths are resolved from the integration-tests/ directory.
        Returns the deploy ID (signature hex).
        """
        import os

        resolved_path = rho_file_path
        if not os.path.isabs(rho_file_path) and not os.path.exists(rho_file_path):
            # integration-tests/test/infra/node.py → integration-tests/
            integration_tests_dir = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            resolved_path = os.path.join(integration_tests_dir, rho_file_path)

        with open(resolved_path, "r") as f:
            code = f.read()

        if substitutions:
            for key, value in substitutions.items():
                code = code.replace(key, value)

        return self.deploy_string(
            code,
            private_key,
            phlo_limit=phlo_limit,
            phlo_price=phlo_price,
            valid_after_block_no=valid_after_block_no,
            shard_id=shard_id,
        )

    def propose(self) -> str:
        """Trigger a block proposal. Returns the block hash.

        Uses the internal gRPC port (40402) where ProposeService is served.
        """
        return self._internal_client().propose()

    def exploratory_deploy(self, rholang_code: str, block_hash: str = "") -> list:
        """Execute a read-only deploy. Returns list of Par results."""
        return self._external_client().exploratory_deploy(rholang_code, block_hash)

    def registry_lookup(self, uri: str, block_hash: str = "") -> list:
        """Look up a value in the registry via exploratory deploy.

        Read-only — no block created, no phlo consumed. Returns Par results.
        """
        from f1r3fly.contracts import registry_lookup

        return registry_lookup(self._external_client(), uri, block_hash)

    def registry_query(
        self,
        uri: str,
        method: str,
        param="Nil",
        block_hash: str = "",
    ) -> list:
        """Query a registry-registered contract via exploratory deploy.

        Read-only — no block created, no phlo consumed. Returns Par results.
        Default ``param="Nil"`` matches the 3-arg pattern
        ``(@method, @param, ret)`` used by bridge contracts. Pass
        ``param=None`` for contracts whose pattern is the 2-arg
        ``(@method, ret)``.
        """
        from f1r3fly.contracts import registry_query

        return registry_query(self._external_client(), uri, method, param, block_hash)

    # ── Query operations ──

    def find_deploy(self, deploy_id: str):
        """Find the block containing a deploy. Returns LightBlockInfo."""
        return self._external_client().find_deploy(deploy_id)

    def last_finalized_block(self):
        """Get the last finalized block. Returns BlockInfo."""
        return self._external_client().last_finalized_block()

    def get_block(self, block_hash: str):
        """Get full block info. Returns BlockInfo."""
        return self._external_client().show_block(block_hash)

    def get_blocks(self, depth: int = 5):
        """Get recent blocks. Returns List[LightBlockInfo]."""
        return self._external_client().show_blocks(depth)

    def get_deploy_data(self, deploy_id: str, block_hash: str = ""):
        """Read data from the deployId channel."""
        return self._external_client().get_data_at_deploy_id(deploy_id, block_hash=block_hash)

    def is_finalized(self, block_hash: str) -> bool:
        """Check if a block is finalized."""
        return self._external_client().is_finalized(block_hash)

    def get_current_block_number(self) -> int:
        """Get the current LFB block number."""
        lfb = self.last_finalized_block()
        return lfb.blockInfo.blockNumber

    def grpc_status(self):
        """Get node status via gRPC. Returns Status proto object."""
        return self._external_client().status()

    def grpc_bond_status(self, public_key_hex: str) -> bool:
        """Check if a public key is bonded via gRPC. Returns bool."""
        return self._external_client().bond_status(public_key_hex)

    def show_main_chain(self, depth: int = 5):
        """Get blocks on the main chain. Returns List[LightBlockInfo]."""
        return self._external_client().show_main_chain(depth)

    def preview_private_names(self, timestamp: int, name_qty: int = 1):
        """Preview unforgeable names for a deployer key + timestamp."""
        from f1r3fly.crypto import PublicKey

        pub_key = PublicKey.from_hex(self._identity.public_hex)
        return self._external_client().previewPrivateNames(pub_key, timestamp, name_qty)

    def get_event_data(self, block_hash: str, force_replay: bool = False):
        """Get block execution trace. Returns EventInfoResponse."""
        return self._external_client().get_event_data(block_hash, force_replay)

    def get_continuation(self, par, depth: int = 1):
        """Get continuations waiting on a channel. Returns ContinuationAtNameResponse."""
        return self._external_client().get_continuation(par, depth)

    # ── Diagnostics ──

    def logs(self, tail: Optional[int] = None) -> str:
        return self._handle.logs(tail)

    def is_running(self) -> bool:
        return self._handle.is_running()

    def pause(self) -> None:
        self._handle.pause()

    def unpause(self) -> None:
        self._handle.unpause()

    def resource_usage(self) -> dict:
        """Return current memory and CPU usage."""
        return self._handle.resource_usage()

    def restart(self) -> None:
        self.close()
        self._handle.restart()

    def __repr__(self) -> str:
        return f"Node({self.name}, role={self._role.value})"

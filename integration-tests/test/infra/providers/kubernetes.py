"""Kubernetes provider stub — Level 3 test infrastructure.

This module exists so the Provider protocol has a second implementation
and the architecture is validated at the type level. All methods raise
``NotImplementedError`` with guidance on what's needed.

When implemented, this provider will:
  - Use the Helm chart at ``f1r3node-rust/docker/helm/f1r3fly/``
  - Create a namespace per test session (``f1r3fly-test-{session_id}``)
  - Deploy shards via ``helm install`` with values overrides
  - Resolve node addresses via K8s service DNS
  - Use ``kubectl port-forward`` for host-accessible gRPC/HTTP
  - Clean up via ``helm uninstall`` + ``kubectl delete namespace``
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from ..config import NodeConfig, ShardConfig
from ..types import PortMapping, NodeRole


class K8sNodeHandle:
    """Stub handle for a Kubernetes pod."""

    def __init__(self, name: str, namespace: str, ports: PortMapping) -> None:
        self._name = name
        self._namespace = namespace
        self._ports = ports

    @property
    def name(self) -> str:
        return self._name

    @property
    def ports(self) -> PortMapping:
        return self._ports

    @property
    def grpc_host(self) -> str:
        return f"{self._name}.{self._namespace}.svc.cluster.local"

    @property
    def network_name(self) -> str:
        return self._namespace

    def logs(self, tail: Optional[int] = None) -> str:
        raise NotImplementedError("K8s provider not yet implemented")

    def is_running(self) -> bool:
        raise NotImplementedError("K8s provider not yet implemented")

    def restart(self) -> None:
        raise NotImplementedError("K8s provider not yet implemented")

    def stop(self) -> None:
        raise NotImplementedError("K8s provider not yet implemented")

    def remove(self) -> None:
        raise NotImplementedError("K8s provider not yet implemented")


class K8sProvider:
    """Kubernetes provider stub.

    To implement:
      1. ``pip install kubernetes`` + ``pip install pyyaml``
      2. Load kubeconfig from ``--kubeconfig`` pytest option or env
      3. ``create_shard``: ``helm install test-{session_id} <chart>``
         with values from ShardConfig
      4. ``destroy_shard``: ``helm uninstall`` + ``kubectl delete ns``
      5. ``cleanup_all``: ``helm ls -n f1r3fly-test-*`` + uninstall all

    The Helm chart at ``f1r3node-rust/docker/helm/f1r3fly/`` supports:
      - ``shardConfig.deployableReplicas`` (validators + bootstrap)
      - ``shardConfig.readOnlyReplicas`` (observer count)
      - Per-node key configuration via values.yaml
      - Minikube and cloud provider deployments
    """

    def __init__(self, **kwargs) -> None:
        raise NotImplementedError(
            "Kubernetes provider is not yet implemented. "
            "See the docstring for implementation guidance. "
            "Use --provider=docker (the default) for now."
        )

    def create_shard(self, config: ShardConfig) -> List[K8sNodeHandle]:
        raise NotImplementedError

    def add_node(self, shard_network, node_config, bootstrap_handle):
        raise NotImplementedError

    def remove_node(self, handle):
        raise NotImplementedError

    def destroy_shard(self, handles):
        raise NotImplementedError

    def create_standalone(self, config: NodeConfig) -> K8sNodeHandle:
        raise NotImplementedError

    def destroy_standalone(self, handle):
        raise NotImplementedError

    def cleanup_all(self) -> None:
        raise NotImplementedError

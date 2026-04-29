"""Kubernetes provider stub — future Level 3 test infrastructure.

Exists so the Provider protocol has a second implementation and the
architecture is validated at the type level. All methods raise
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
    """Stub handle for a Kubernetes pod.

    Every method matches the :py:class:`~test.infra.providers.base.NodeHandle`
    Protocol. All raise :py:class:`NotImplementedError` until the backend
    ships.
    """

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
        raise NotImplementedError(
            "Implement via `kubectl logs <pod> -n <ns> [--tail N]`."
        )

    def is_running(self) -> bool:
        raise NotImplementedError(
            "Implement via `kubectl get pod <pod> -n <ns> -o jsonpath='{.status.phase}'` "
            "returning 'Running'."
        )

    def restart(self) -> None:
        raise NotImplementedError(
            "Implement via `kubectl delete pod <pod> -n <ns>` "
            "(StatefulSet/Deployment will recreate it)."
        )

    def pause(self) -> None:
        raise NotImplementedError(
            "K8s has no direct equivalent. Options: apply a NetworkPolicy that "
            "denies all traffic to/from the pod, or scale the controller to 0."
        )

    def unpause(self) -> None:
        raise NotImplementedError(
            "Reverse of pause() — remove the NetworkPolicy, or scale the "
            "controller back up."
        )

    def exit_code(self) -> Optional[int]:
        raise NotImplementedError(
            "Implement via `kubectl get pod <pod> -n <ns> -o jsonpath="
            "'{.status.containerStatuses[0].state.terminated.exitCode}'`."
        )

    def wait_for_exit(self, timeout: int = 180) -> Optional[int]:
        raise NotImplementedError(
            "Implement via `kubectl wait --for=condition=Ready=false pod/<pod> "
            "-n <ns> --timeout=<timeout>s` + exit_code()."
        )

    def resource_usage(self) -> dict:
        raise NotImplementedError(
            "Implement via `kubectl top pod <pod> -n <ns>` (requires metrics-server) "
            "returning {'memory_mb': ..., 'cpu_percent': ..., 'memory_limit_mb': ...}."
        )

    def stop(self) -> None:
        raise NotImplementedError(
            "Implement via `kubectl delete pod <pod> -n <ns> --grace-period=30`. "
            "For graceful stop without removal, scale the controller instead."
        )

    def remove(self) -> None:
        raise NotImplementedError(
            "Implement via `kubectl delete pod <pod> -n <ns> --force --grace-period=0`. "
            "For a full shard teardown use Provider.destroy_shard() which removes the namespace."
        )


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

    @property
    def keep_running(self) -> bool:
        raise NotImplementedError(
            "Mirror DockerProvider.keep_running (from the cleanup registry)."
        )

    @property
    def active_handles(self) -> list:
        raise NotImplementedError(
            "Track handles added via create_shard/create_standalone/add_node "
            "in an internal list, same pattern as DockerProvider."
        )

    def create_shard(self, config: ShardConfig) -> List[K8sNodeHandle]:
        raise NotImplementedError(
            "Implement via `helm install test-{session_id} <chart> "
            "--values <generated-values.yaml>`, wait for all pods Running, "
            "set up `kubectl port-forward` for each pod's 40401-40403, "
            "return handles in [bootstrap, validator1..N, readonly] order."
        )

    def add_node(self, shard_network, node_config, bootstrap_handle):
        raise NotImplementedError(
            "Implement via `helm upgrade` or a separate manifest for the joiner "
            "in the same namespace. Joiner bootstraps from bootstrap_handle.grpc_host."
        )

    def remove_node(self, handle):
        raise NotImplementedError(
            "Implement via `kubectl delete pod <handle.name> -n <ns>` + "
            "`kubectl delete pvc <handle.volume> -n <ns>`."
        )

    def destroy_shard(self, handles: Sequence[K8sNodeHandle]) -> None:
        raise NotImplementedError(
            "Implement via `helm uninstall test-{session_id} -n <ns>` followed "
            "by `kubectl delete namespace <ns>`."
        )

    def create_standalone(self, config: NodeConfig) -> K8sNodeHandle:
        raise NotImplementedError(
            "Implement via a single-replica Deployment + Service in its own "
            "namespace. Smaller subset of create_shard."
        )

    def destroy_standalone(self, handle):
        raise NotImplementedError(
            "Implement via `kubectl delete namespace <handle.namespace>`."
        )

    def cleanup_all(self) -> None:
        raise NotImplementedError(
            "Implement as this session's own registered-resource cleanup. "
            "Mirror DockerProvider.cleanup_all which delegates to DockerCleanupRegistry. "
            "Distinct from force_cleanup_all_test_resources (which spans all sessions)."
        )

    @classmethod
    def force_cleanup_all_test_resources(cls) -> None:
        raise NotImplementedError(
            "Aggressive, user-invoked cleanup. Implement via "
            "`helm ls --all-namespaces -l f1r3fly-test-framework=true -o json` to "
            "list releases, then `helm uninstall` each, then "
            "`kubectl delete namespace -l f1r3fly-test-framework=true`."
        )

    def adopt_session(self, session_id: str) -> List[K8sNodeHandle]:
        raise NotImplementedError(
            "Implement via `kubectl get pods -n f1r3fly-test-{session_id} "
            "-l f1r3fly-test-framework=true -o json`, then build a K8sNodeHandle "
            "per pod. Fail if the namespace doesn't exist or has no matching pods."
        )

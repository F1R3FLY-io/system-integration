"""Docker provider — creates nodes via docker compose / docker run.

Implements the ``Provider`` protocol for Docker-based test environments.
All container/volume/network names are prefixed with the session ID to
prevent collisions across parallel runs and with v1 tests.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Dict, List, Optional, Sequence

from ..cleanup import CleanupRegistry
from ..compose import generate_compose
from ..config import NodeConfig, ResourcePaths, ShardConfig, resolve_node_image
from ..genesis import generate_genesis
from ..keys import BOOTSTRAP_NODE_ID
from ..polling import wait_for_node_running
from ..ports import PortAllocator
from ..timeouts import TimeoutHierarchy
from ..types import NodeRole, PortMapping, ValidatorIdentity

logger = logging.getLogger(__name__)

# Bootstrap private key — same across all shards (matches shipped certs)
_BOOTSTRAP_PRIVATE_KEY = (
    "5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"
)


def _docker(*args: str, check: bool = False, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _compose(*args: str, compose_file: str, project_name: str,
             check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-p", project_name, "-f", compose_file, *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=300,
    )


class DockerNodeHandle:
    """Handle to a Docker container created by DockerProvider."""

    def __init__(
        self,
        name: str,
        ports: PortMapping,
        network: str,
        role: NodeRole,
        identity: Optional[ValidatorIdentity] = None,
        volume_name: Optional[str] = None,
    ) -> None:
        self._name = name
        self._ports = ports
        self._network = network
        self._role = role
        self._identity = identity
        self._volume_name = volume_name

    @property
    def volume_name(self) -> Optional[str]:
        return self._volume_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def ports(self) -> PortMapping:
        return self._ports

    @property
    def grpc_host(self) -> str:
        return "localhost"

    @property
    def network_name(self) -> str:
        return self._network

    @property
    def role(self) -> NodeRole:
        return self._role

    @property
    def identity(self) -> Optional[ValidatorIdentity]:
        return self._identity

    def logs(self, tail: Optional[int] = None) -> str:
        args = ["logs"]
        if tail:
            args.extend(["--tail", str(tail)])
        args.append(self._name)
        result = _docker(*args)
        return (result.stdout or "") + (result.stderr or "")

    def is_running(self) -> bool:
        result = _docker("inspect", "-f", "{{.State.Status}}", self._name)
        if result.returncode != 0:
            return False
        return (result.stdout or "").strip() == "running"

    def restart(self) -> None:
        _docker("restart", self._name, check=True)

    def pause(self) -> None:
        _docker("pause", self._name, check=True)

    def unpause(self) -> None:
        _docker("unpause", self._name, check=True)

    def exit_code(self) -> Optional[int]:
        """Return the container's exit code, or None if still running."""
        result = _docker(
            "inspect", "-f", "{{.State.ExitCode}}|{{.State.Status}}", self._name
        )
        if result.returncode != 0:
            return None
        parts = (result.stdout or "").strip().split("|")
        if len(parts) == 2 and parts[1] in ("exited", "dead"):
            try:
                return int(parts[0])
            except ValueError:
                return None
        return None

    def wait_for_exit(self, timeout: int = 180) -> Optional[int]:
        """Wait for the container to exit. Returns exit code or None on timeout."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            code = self.exit_code()
            if code is not None:
                return code
            if not self.is_running():
                return self.exit_code()
            time.sleep(3)
        return None

    def resource_usage(self) -> dict:
        """Return current memory and CPU usage from docker stats."""
        result = _docker(
            "stats", "--no-stream", "--format",
            "{{.MemUsage}}|{{.CPUPerc}}|{{.MemPerc}}",
            self._name,
        )
        if result.returncode != 0:
            return {"memory_mb": 0, "cpu_percent": 0, "memory_limit_mb": 0}
        parts = (result.stdout or "").strip().split("|")
        if len(parts) != 3:
            return {"memory_mb": 0, "cpu_percent": 0, "memory_limit_mb": 0}
        try:
            # MemUsage format: "123.4MiB / 7.748GiB"
            mem_parts = parts[0].split("/")
            mem_used = mem_parts[0].strip()
            mem_limit = mem_parts[1].strip() if len(mem_parts) > 1 else "0"
            cpu = parts[1].strip().rstrip("%")
            return {
                "memory_mb": _parse_mem(mem_used),
                "cpu_percent": float(cpu),
                "memory_limit_mb": _parse_mem(mem_limit),
            }
        except (ValueError, IndexError):
            return {"memory_mb": 0, "cpu_percent": 0, "memory_limit_mb": 0}

    def stop(self) -> None:
        _docker("stop", self._name)

    def remove(self) -> None:
        _docker("rm", "-f", self._name)


def _parse_mem(s: str) -> float:
    """Parse Docker memory string like '123.4MiB' or '7.748GiB' to MB."""
    s = s.strip()
    if s.endswith("GiB"):
        return float(s[:-3]) * 1024
    if s.endswith("MiB"):
        return float(s[:-3])
    if s.endswith("KiB"):
        return float(s[:-3]) / 1024
    if s.endswith("B"):
        return float(s[:-1]) / (1024 * 1024)
    return 0


class DockerProvider:
    """Creates and destroys Docker-based test infrastructure.

    All resources use session-prefixed names to prevent collisions
    across parallel sessions.
    """

    def __init__(
        self,
        port_allocator: PortAllocator,
        registry: CleanupRegistry,
        timeouts: TimeoutHierarchy,
        paths: Optional[ResourcePaths] = None,
    ) -> None:
        self._ports = port_allocator
        self._registry = registry
        self._timeouts = timeouts
        self._paths = paths or ResourcePaths.resolve()
        self._session_id = registry.session_id
        self._standalone_counter = 0
        self._active_handles: list = []

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def keep_running(self) -> bool:
        return self._registry.keep_running

    @property
    def active_handles(self) -> list:
        return list(self._active_handles)

    # ── Shard lifecycle ─────────────────────────────────────────────

    def create_shard(self, config: ShardConfig, wait_running: bool = True) -> List[DockerNodeHandle]:
        """Create a full shard via docker compose.

        Generates genesis, compose file, runs ``docker compose up -d``.
        If ``wait_running`` is True (default), waits for all nodes to
        reach Running state. Set False for tests expecting startup failure.

        Returns handles in order: [boot, validator1, ..., readonly].
        """
        genesis_dir = generate_genesis(config, self._paths, self._registry)

        # Allocate ports: boot + N validators + optional readonly
        roles = ["boot"] + [f"validator{i+1}" for i in range(len(config.bonds))]
        if config.include_readonly:
            roles.append("readonly")

        port_map: Dict[str, PortMapping] = {}
        for role in roles:
            port_map[role] = self._ports.allocate()

        compose_path = generate_compose(
            config=config,
            genesis_dir=genesis_dir,
            port_assignments=port_map,
            session_id=self._session_id,
            paths=self._paths,
            registry=self._registry,
        )

        project_name = f"test-{self._session_id}"

        # Start all services
        result = _compose("up", "-d", compose_file=compose_path, project_name=project_name)
        if result.returncode != 0:
            logger.error("docker compose up failed: %s", result.stderr)
            raise RuntimeError(f"docker compose up failed: {result.stderr}")

        # Build handles
        handles: List[DockerNodeHandle] = []
        boot_handle = DockerNodeHandle(
            name=f"rnode.test.{self._session_id}.boot",
            ports=port_map["boot"],
            network=f"f1r3fly-test-{self._session_id}",
            role=NodeRole.BOOTSTRAP,
        )
        handles.append(boot_handle)

        for idx, (identity, _) in enumerate(config.bonds):
            role_name = f"validator{idx + 1}"
            handles.append(DockerNodeHandle(
                name=f"rnode.test.{self._session_id}.{role_name}",
                ports=port_map[role_name],
                network=f"f1r3fly-test-{self._session_id}",
                role=NodeRole.VALIDATOR,
                identity=identity,
            ))

        if config.include_readonly:
            handles.append(DockerNodeHandle(
                name=f"rnode.test.{self._session_id}.readonly",
                ports=port_map["readonly"],
                network=f"f1r3fly-test-{self._session_id}",
                role=NodeRole.READONLY,
            ))

        # Wait for all nodes to reach Running state
        if wait_running:
            for handle in handles:
                wait_for_node_running(
                    get_logs=handle.logs,
                    is_running=handle.is_running,
                    node_name=handle.name,
                    timeout=self._timeouts.node_startup,
                )

        # Store compose path for teardown
        self._compose_files = getattr(self, "_compose_files", {})
        shard_key = f"shard-{self._session_id}"
        self._compose_files[shard_key] = (compose_path, project_name, genesis_dir)

        self._active_handles.extend(handles)
        logger.info(
            "Shard created: %d nodes (%s)",
            len(handles),
            ", ".join(h.name.split(".")[-1] for h in handles),
        )
        return handles

    def destroy_shard(self, handles: Sequence[DockerNodeHandle]) -> None:
        """Destroy a shard — compose down with volumes."""
        for h in handles:
            if h in self._active_handles:
                self._active_handles.remove(h)
        if self.keep_running:
            logger.info("Shard kept running (--keep-running)")
            return

        shard_key = f"shard-{self._session_id}"
        compose_files = getattr(self, "_compose_files", {})
        if shard_key in compose_files:
            compose_path, project_name, genesis_dir = compose_files.pop(shard_key)
            _compose(
                "down", "--volumes", "--remove-orphans",
                compose_file=compose_path, project_name=project_name,
            )
        else:
            for handle in handles:
                handle.remove()

        logger.info("Shard destroyed")

    # ── Standalone lifecycle ────────────────────────────────────────

    def create_standalone(
        self,
        config: NodeConfig,
        wait_running: bool = True,
        volume_name: Optional[str] = None,
        use_shard_conf: bool = False,
    ) -> DockerNodeHandle:
        """Create a standalone node via docker run.

        Args:
            config: Node configuration with CLI flags/options.
            wait_running: If True (default), wait for the node to reach
                Running state. Set False for tests that expect startup failure.
            volume_name: If provided, use a named volume for /var/lib/rnode
                instead of an anonymous volume. The volume persists across
                container recreations (used for restart-drift tests).
            use_shard_conf: If True, mount rust.conf (shard config) and
                shard genesis files instead of standalone-dev.conf. Used for
                observers joining a standalone baseline.
        """
        ports = self._ports.allocate()
        self._standalone_counter += 1
        suffix = self._standalone_counter
        container_name = f"rnode.test.{self._session_id}.standalone{suffix}"
        network_name = f"f1r3fly-test-{self._session_id}-standalone{suffix}"

        # Create network
        _docker("network", "create", network_name)
        self._registry.register_network(network_name)

        # Build CLI args from config
        extra_cli: List[str] = []
        for flag in sorted(config.cli_flags):
            extra_cli.append(flag)
        for k, v in sorted(config.cli_options.items()):
            extra_cli.append(f"{k}={v}" if v else k)

        image = resolve_node_image()

        # Always use named volumes so cleanup can find them
        if not volume_name:
            volume_name = f"test-{self._session_id}-standalone{suffix}-data"
        _docker("volume", "create", volume_name, check=False)
        self._registry.register_volume(volume_name)
        volume_arg = f"{volume_name}:/var/lib/rnode"

        # Config file selection
        if use_shard_conf:
            conf_path = self._paths.rust_conf
            wallets_path = self._paths.genesis_wallets
            bonds_path = self._paths.genesis_bonds
        else:
            conf_path = self._paths.standalone_conf
            wallets_path = self._paths.standalone_wallets
            bonds_path = self._paths.standalone_bonds

        run_args = [
            "run", "-d", "--rm=false", "--user", "root",
            "--name", container_name,
            "--network", network_name,
            "--network-alias", container_name,
            "-v", volume_arg,
            "-v", f"{conf_path}:/var/lib/rnode/rnode.conf:ro",
            "-v", f"{wallets_path}:/var/lib/rnode/genesis/wallets.txt:ro",
            "-v", f"{bonds_path}:/var/lib/rnode/genesis/bonds.txt:ro",
        ]

        # Mount bootstrap TLS certs when using shard conf so joiners can
        # connect using the known BOOTSTRAP_NODE_ID
        if use_shard_conf:
            run_args.extend([
                "-v", f"{self._paths.certs_dir}/bootstrap/node.certificate.pem:/var/lib/rnode/node.certificate.pem:ro",
                "-v", f"{self._paths.certs_dir}/bootstrap/node.key.pem:/var/lib/rnode/node.key.pem:ro",
            ])

        run_args.extend([
            "-p", f"{ports.protocol}:40400",
            "-p", f"{ports.grpc_ext}:40401",
            "-p", f"{ports.grpc_int}:40402",
            "-p", f"{ports.http}:40403",
            "-p", f"{ports.discovery}:40404",
            "-p", f"{ports.admin}:40405",
            image,
            "run", "-s",
            f"--host={container_name}",
            f"--validator-private-key={_BOOTSTRAP_PRIVATE_KEY}",
            "--allow-private-addresses",
            *extra_cli,
        ])

        result = _docker(*run_args)
        if result.returncode != 0:
            raise RuntimeError(f"docker run failed: {result.stderr}")

        self._registry.register_container(container_name)

        handle = DockerNodeHandle(
            name=container_name,
            ports=ports,
            network=network_name,
            role=NodeRole.STANDALONE,
            volume_name=volume_name,
        )

        if wait_running:
            wait_for_node_running(
                get_logs=handle.logs,
                is_running=handle.is_running,
                node_name=handle.name,
                timeout=self._timeouts.node_startup,
            )

        self._active_handles.append(handle)
        return handle

    def recreate_standalone(
        self,
        handle: DockerNodeHandle,
        config: NodeConfig,
        wait_running: bool = True,
    ) -> DockerNodeHandle:
        """Recreate a standalone container with new config but same volume/network.

        Stops and removes the existing container, then starts a new one on
        the same network with the same ports. The named volume (if any)
        persists, so the node restarts against existing data.

        Used for restart-drift tests.
        """
        container_name = handle.name
        network_name = handle.network_name
        ports = handle.ports

        handle.stop()
        handle.remove()

        extra_cli: List[str] = []
        for flag in sorted(config.cli_flags):
            extra_cli.append(flag)
        for k, v in sorted(config.cli_options.items()):
            extra_cli.append(f"{k}={v}" if v else k)

        image = resolve_node_image()

        volume_name = handle.volume_name
        volume_arg = f"{volume_name}:/var/lib/rnode" if volume_name else "/var/lib/rnode"

        run_args = [
            "run", "-d", "--rm=false", "--user", "root",
            "--name", container_name,
            "--network", network_name,
            "--network-alias", container_name,
            "-v", volume_arg,
            "-v", f"{self._paths.standalone_conf}:/var/lib/rnode/rnode.conf:ro",
            "-v", f"{self._paths.standalone_wallets}:/var/lib/rnode/genesis/wallets.txt:ro",
            "-v", f"{self._paths.standalone_bonds}:/var/lib/rnode/genesis/bonds.txt:ro",
            "-p", f"{ports.protocol}:40400",
            "-p", f"{ports.grpc_ext}:40401",
            "-p", f"{ports.grpc_int}:40402",
            "-p", f"{ports.http}:40403",
            "-p", f"{ports.discovery}:40404",
            "-p", f"{ports.admin}:40405",
            image,
            "run", "-s",
            f"--host={container_name}",
            f"--validator-private-key={_BOOTSTRAP_PRIVATE_KEY}",
            "--allow-private-addresses",
            *extra_cli,
        ]

        result = _docker(*run_args)
        if result.returncode != 0:
            raise RuntimeError(f"docker run (recreate) failed: {result.stderr}")

        new_handle = DockerNodeHandle(
            name=container_name,
            ports=ports,
            network=network_name,
            role=NodeRole.STANDALONE,
            volume_name=volume_name,
        )

        if wait_running:
            wait_for_node_running(
                get_logs=new_handle.logs,
                is_running=new_handle.is_running,
                node_name=new_handle.name,
                timeout=self._timeouts.node_startup,
            )

        return new_handle

    def destroy_standalone(self, handle: DockerNodeHandle) -> None:
        if handle in self._active_handles:
            self._active_handles.remove(handle)
        if self.keep_running:
            logger.info("Standalone %s kept running (--keep-running)", handle.name)
            return

        handle.remove()
        suffix = handle.name.split("standalone")[-1] if "standalone" in handle.name else ""
        vol = f"test-{self._session_id}-standalone{suffix}-data"
        _docker("volume", "rm", "-f", vol)
        _docker("network", "rm", handle.network_name)

    # ── Joiner lifecycle ────────────────────────────────────────────

    def add_node(
        self,
        shard_network: str,
        node_config: NodeConfig,
        bootstrap_handle: DockerNodeHandle,
        wait_running: bool = True,
    ) -> DockerNodeHandle:
        """Add a joiner node to an existing shard.

        Args:
            wait_running: If True (default), wait for the node to reach
                Running state. Set False for tests expecting startup failure
                (e.g. token metadata mismatch).
        """
        ports = self._ports.allocate()
        identity = node_config.identity
        self._joiner_counter = getattr(self, "_joiner_counter", 0) + 1
        joiner_name = f"rnode.test.{self._session_id}.joiner{self._joiner_counter}"
        volume_name = f"test-{self._session_id}-joiner{self._joiner_counter}-data"

        bootstrap_url = (
            f"rnode://{BOOTSTRAP_NODE_ID}@{bootstrap_handle.name}"
            f"?protocol=40400&discovery=40404"
        )

        image = resolve_node_image()

        extra_cli: List[str] = []
        for flag in sorted(node_config.cli_flags):
            extra_cli.append(flag)
        for k, v in sorted(node_config.cli_options.items()):
            extra_cli.append(f"{k}={v}" if v else k)

        # Joiner command depends on whether it has a validator identity
        cmd: List[str] = [
            "run",
            f"--host={joiner_name}",
            f"--bootstrap={bootstrap_url}",
            "--allow-private-addresses",
        ]
        if identity:
            cmd.extend([
                f"--validator-public-key={identity.public_hex}",
                f"--validator-private-key={identity.private_hex}",
            ])
        cmd.extend(extra_cli)

        run_args = [
            "run", "-d", "--rm=false", "--user", "root",
            "--name", joiner_name,
            "--network", shard_network,
            "--network-alias", joiner_name,
            "-v", f"{volume_name}:/var/lib/rnode",
            "-v", f"{self._paths.rust_conf}:/var/lib/rnode/rnode.conf:ro",
            "-p", f"{ports.protocol}:40400",
            "-p", f"{ports.grpc_ext}:40401",
            "-p", f"{ports.grpc_int}:40402",
            "-p", f"{ports.http}:40403",
            "-p", f"{ports.discovery}:40404",
            "-p", f"{ports.admin}:40405",
            image,
            *cmd,
        ]

        _docker("volume", "create", volume_name)
        self._registry.register_volume(volume_name)

        result = _docker(*run_args)
        if result.returncode != 0:
            raise RuntimeError(f"docker run (joiner) failed: {result.stderr}")

        self._registry.register_container(joiner_name)

        handle = DockerNodeHandle(
            name=joiner_name,
            ports=ports,
            network=shard_network,
            role=NodeRole.JOINER,
            identity=identity,
        )

        if wait_running:
            wait_for_node_running(
                get_logs=handle.logs,
                is_running=handle.is_running,
                node_name=handle.name,
                timeout=self._timeouts.node_startup,
            )

        self._active_handles.append(handle)
        return handle

    def remove_node(self, handle: DockerNodeHandle) -> None:
        if handle in self._active_handles:
            self._active_handles.remove(handle)
        if self.keep_running:
            logger.info("Joiner %s kept running (--keep-running)", handle.name)
            return

        handle.remove()
        joiner_suffix = handle.name.split("joiner")[-1] if "joiner" in handle.name else ""
        vol = f"test-{self._session_id}-joiner{joiner_suffix}-data"
        _docker("volume", "rm", "-f", vol)

    # ── Global cleanup ──────────────────────────────────────────────

    def cleanup_all(self) -> None:
        self._registry.cleanup_all()

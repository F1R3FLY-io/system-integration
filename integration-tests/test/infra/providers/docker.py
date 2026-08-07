"""Docker provider — creates nodes via docker compose / docker run.

Implements the ``Provider`` protocol for Docker-based test environments.
All container/volume/network names are prefixed with the session ID to
prevent collisions across parallel runs and with v1 tests.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..cleanup import DockerCleanupRegistry
from ..compose import generate_compose
from ..config import NodeConfig, ResourcePaths, ShardConfig, resolve_node_image
from ..genesis import generate_genesis
from ..keys import BOOTSTRAP_NODE_ID
from ..ports import PortAllocator
from ..run_outcome import current_test_failed
from ..timeouts import TimeoutHierarchy
from ..types import NodeRole, PortMapping, ValidatorIdentity
from .base import (
    RetiredLogSnapshot,
    archive_handles,
    archive_root_for,
    wait_for_handles_or_archive,
)

logger = logging.getLogger(__name__)

# Bootstrap private key — same across all shards (matches shipped certs)
_BOOTSTRAP_PRIVATE_KEY = "5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"

# rnode hardcodes 40400-40405 for its listen sockets (transport / ext gRPC /
# int gRPC / HTTP / discovery / admin). The Linux default ephemeral range
# (32768-60999) overlaps with these — an outbound TCP from rnode at startup
# (e.g. ApprovedBlockRequest to bootstrap) can be assigned 40400 as its
# source port, which then blocks rnode's subsequent server bind with
# EADDRINUSE at servers_instances.rs:155. Each container has its own
# network namespace, so the host-level sysctl on the OCI runner doesn't
# propagate — reserve the range per-container via --sysctl.
_NODE_PORT_RESERVATION_ARGS: List[str] = [
    "--sysctl",
    "net.ipv4.ip_local_reserved_ports=40400-40405",
]


def _docker(*args: str, check: bool = False, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _compose(
    *args: str, compose_file: str, project_name: str, check: bool = False
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-p", project_name, "-f", compose_file, *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=300,
    )


def _daemon_diagnostics() -> str:
    """Capture a snapshot of docker daemon state for failure diagnostics.

    Used by ``_ensure_*`` helpers when their inspect-poll times out — the
    snapshot helps the next debugger understand WHY the daemon couldn't
    propagate the state we asked for (under load? resource cap hit?
    stale leftovers? wrong driver?). All probes are cheap (<100ms each)
    and only run on failure.
    """
    parts: List[str] = []

    nls = _docker(
        "network",
        "ls",
        "--format",
        "{{.Name}}\t{{.ID}}\t{{.Driver}}\t{{.Scope}}",
        timeout=5,
    )
    parts.append(f"docker network ls:\n{(nls.stdout or '(empty)').rstrip()}")

    ps = _docker(
        "ps",
        "-a",
        "--format",
        "{{.Names}}\t{{.Status}}\t{{.ID}}",
        timeout=5,
    )
    parts.append(f"\ndocker ps -a:\n{(ps.stdout or '(empty)').rstrip()}")

    info = _docker(
        "info",
        "--format",
        "Containers: {{.Containers}} (running: {{.ContainersRunning}}, "
        "paused: {{.ContainersPaused}}, stopped: {{.ContainersStopped}}) | "
        "Server: {{.ServerVersion}} | Kernel: {{.KernelVersion}} | "
        "OS: {{.OperatingSystem}}",
        timeout=5,
    )
    parts.append(f"\ndocker info: {(info.stdout or '(empty)').rstrip()}")

    return "\n".join(parts)


def _ensure_no_container(name: str, timeout: float = 30.0) -> None:
    """Force-remove a container by name and wait until the daemon agrees.

    Idempotent: no-op if no such container exists. After ``docker rm -f``
    returns, ``docker inspect`` is polled until it reports not-present.
    Eliminates the race where ``rm -f`` succeeds but the name is still
    claimed when a follow-up ``docker run --name`` references it.

    Timeout default is 30s — under daemon load, propagation can take
    >10s. On timeout, the original ``rm -f`` output is included in the
    error along with a daemon-state snapshot — so we can distinguish
    "rm returned 0 but propagation stalled" from "rm itself errored
    and we lost the cause."
    """
    rm = _docker("rm", "-f", name)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _docker("inspect", "--type", "container", name).returncode != 0:
            return
        time.sleep(0.1)
    raise RuntimeError(
        f"Container {name!r} still visible to docker daemon after rm -f "
        f"and {timeout}s wait.\n"
        f"docker rm -f returned: "
        f"returncode={rm.returncode}, "
        f"stdout={rm.stdout.strip()!r}, "
        f"stderr={rm.stderr.strip()!r}\n\n"
        f"Daemon state at timeout:\n{_daemon_diagnostics()}"
    )


def _ensure_network(name: str, timeout: float = 30.0) -> None:
    """Ensure a docker network with ``name`` exists and is daemon-visible.

    Idempotent: creates if missing, accepts "already exists" silently.
    After the create call returns, ``docker network inspect`` is polled
    until consistently visible. Eliminates the race where ``network
    create`` reports success but a follow-up ``docker run --network``
    sees ``network not found``.

    Timeout default is 30s — same reasoning as ``_ensure_no_container``:
    daemon under load can take >10s to propagate. On timeout, the
    original create-step's output is included in the error along with
    a daemon-state snapshot — under heavy concurrent load
    ``docker network create`` has been observed returning 0 without
    effect, and the inspect-poll alone can't distinguish that from a
    plain propagation lag.
    """
    create = _docker("network", "create", name)
    if create.returncode != 0 and "already exists" not in (create.stderr or ""):
        raise RuntimeError(
            f"docker network create {name!r} failed "
            f"(returncode={create.returncode}, "
            f"stdout={create.stdout.strip()!r}, "
            f"stderr={create.stderr.strip()!r})"
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _docker("network", "inspect", name).returncode == 0:
            return
        time.sleep(0.1)
    raise RuntimeError(
        f"Network {name!r} not visible to docker daemon after create "
        f"and {timeout}s wait.\n"
        f"docker network create returned: "
        f"returncode={create.returncode}, "
        f"stdout={create.stdout.strip()!r}, "
        f"stderr={create.stderr.strip()!r}\n\n"
        f"Daemon state at timeout:\n{_daemon_diagnostics()}"
    )


def _docker_run(run_args: List[str], settle: float = 1.0) -> subprocess.CompletedProcess:
    """Run ``docker run`` with a single retry that repairs daemon state.

    Callers should set up preconditions via ``_ensure_no_container`` /
    ``_ensure_network``, but ``_docker_run`` does not trust them — the
    daemon's inspect-view and run-attach view diverge under contention
    (a network that ``docker network inspect`` confirms can still be
    invisible to ``docker run --network``).

    If the first attempt fails, the retry repairs the two known forms
    of state divergence before re-running:

    1. **Half-created container.** ``docker run`` reserves the
       container name early — before binding ports or attaching the
       network — so late failures leave a Created-state container
       holding the name. Force-remove by ``--name`` from ``run_args``.

    2. **Network not found despite inspect claim.** When the first
       attempt's stderr says "network ... not found", ``docker network
       rm`` + ``docker network create`` are issued for the
       ``--network`` value in ``run_args``. ``rm`` is a no-op if other
       containers are attached (e.g. shard network in ``add_node``) —
       harmless, since the retry will then surface the same error.

    The first attempt's stderr is preserved in the returned
    ``CompletedProcess`` so callers see the real cause instead of a
    retry artifact.
    """
    result = _docker(*run_args)
    if result.returncode == 0:
        return result

    first_err = (result.stderr or "(no stderr from first attempt)").strip()
    first_err_lc = first_err.lower()

    container_name: Optional[str] = None
    network_name: Optional[str] = None
    try:
        container_name = run_args[run_args.index("--name") + 1]
    except (ValueError, IndexError):
        pass
    try:
        network_name = run_args[run_args.index("--network") + 1]
    except (ValueError, IndexError):
        pass

    network_missing = "network" in first_err_lc and "not found" in first_err_lc

    time.sleep(settle)
    if container_name is not None:
        _ensure_no_container(container_name)
    if network_missing and network_name is not None:
        _docker("network", "rm", network_name)
        _ensure_network(network_name)

    retry = _docker(*run_args)
    if retry.returncode == 0:
        return retry

    retry_err = (retry.stderr or "(no stderr from retry)").strip()
    repair_notes = []
    if container_name is not None:
        repair_notes.append(f"cleaned {container_name!r}")
    if network_missing and network_name is not None:
        repair_notes.append(f"recreated network {network_name!r}")
    repair_summary = ", ".join(repair_notes) if repair_notes else "no repair"
    return subprocess.CompletedProcess(
        args=retry.args,
        returncode=retry.returncode,
        stdout=retry.stdout,
        stderr=(
            f"--- first attempt ---\n{first_err}\n"
            f"--- retry (after {repair_summary}) ---\n{retry_err}"
        ),
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

    # Path inside every container where the node writes its structured log.
    _LOG_FILE_PATH = "/var/lib/rnode/logs/node.log"

    def logs(self, tail: Optional[int] = None) -> str:
        """Return the node's log content.

        Reads from the structured log file written by the node's file
        sink (``--log-sink=both``). Falls back to ``docker logs``
        (stdout/stderr buffer) when the file does not exist or is not
        yet accessible — this covers the startup-failure case where the
        node crashes before the file sink opens.
        """
        result = _docker("exec", self._name, "cat", self._LOG_FILE_PATH)
        if result.returncode == 0:
            text = result.stdout or ""
        else:
            fallback = _docker("logs", self._name)
            text = (fallback.stdout or "") + (fallback.stderr or "")
        if tail:
            lines = text.splitlines()
            text = "\n".join(lines[-tail:])
        return text

    def archive_log(self, dest_path: Path) -> None:
        """Persist the node's complete log to ``dest_path``.

        Tries ``docker cp`` from the file sink first (works on running
        and stopped containers). Falls back to ``docker logs`` redirect
        when the file does not exist — ensures startup-failure output
        is always captured even when the node crashed before the file
        sink was opened. Always produces a file so
        ``actions/upload-artifact@v4`` never silently drops the entry.
        """
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            cp_result = subprocess.run(
                ["docker", "cp", f"{self._name}:{self._LOG_FILE_PATH}", str(dest_path)],
                capture_output=True,
                check=False,
                timeout=30,
            )
            if cp_result.returncode != 0:
                with dest_path.open("w") as f:
                    subprocess.run(
                        ["docker", "logs", self._name],
                        stdout=f,
                        stderr=subprocess.STDOUT,
                        check=False,
                        timeout=30,
                    )
        except Exception as e:
            logger.warning("DockerNodeHandle.archive_log: %s failed: %s", self._name, e)
            try:
                dest_path.write_text(
                    f"archive_log: exception raised: {e!r}\n  container name: {self._name}\n"
                )
            except Exception:
                pass

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
        result = _docker("inspect", "-f", "{{.State.ExitCode}}|{{.State.Status}}", self._name)
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
        """Wait for the container to exit. Returns exit code or None on timeout.

        Uses ``docker wait`` — a daemon-blocking RPC that returns precisely
        when the container exits, analogous to ``proc.wait()`` for a local
        process. This avoids the polling race the previous implementation
        had: it inspected ``State.Status`` and bailed early when the
        container was momentarily in a transitional state (``created``,
        ``removing``, ``restarting``) or when ``docker inspect`` transiently
        failed under daemon contention, causing the function to return
        ``None`` long before the timeout deadline.
        """
        try:
            result = _docker("wait", self._name, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        if result.returncode != 0:
            return None
        try:
            return int(result.stdout.strip())
        except ValueError:
            return None

    def resource_usage(self) -> dict:
        """Return current memory and CPU usage from docker stats."""
        result = _docker(
            "stats",
            "--no-stream",
            "--format",
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
        # 30s grace period (vs Docker's 10s default) — rnode needs time to
        # flush LMDB state to disk under load. Tests that recreate against
        # the same volume (e.g. token-config drift) rely on this.
        _docker("stop", "-t", "30", self._name)

    def remove(self) -> None:
        _docker("rm", "-f", self._name)

    @classmethod
    def from_container(cls, container_name: str) -> "DockerNodeHandle":
        """Build a handle for an already-running container by inspecting it.

        Used by :py:meth:`DockerProvider.adopt_session` to wrap containers
        that were created by a previous pytest session (via ``--keep-running``)
        and are being reused now. Extracts the host port mapping and the
        attached network from ``docker inspect``.

        Role is derived from the container name suffix:
        ``rnode.test.{session_id}.{role}`` → role (``boot``/``validator{N}``/``readonly``).
        """
        # Verify the container exists
        check = _docker("inspect", "-f", "{{.State.Status}}", container_name)
        if check.returncode != 0:
            raise ValueError(f"container {container_name!r} not found")

        # Role from name suffix
        parts = container_name.split(".")
        if len(parts) < 4 or parts[0] != "rnode" or parts[1] != "test":
            raise ValueError(
                f"unexpected container name {container_name!r} — "
                "expected rnode.test.{session}.{role}"
            )
        role_suffix = parts[-1]
        if role_suffix == "boot":
            role = NodeRole.BOOTSTRAP
        elif role_suffix == "readonly":
            role = NodeRole.READONLY
        elif role_suffix.startswith("validator"):
            role = NodeRole.VALIDATOR
        elif role_suffix.startswith("standalone"):
            role = NodeRole.STANDALONE
        elif role_suffix.startswith("joiner"):
            role = NodeRole.JOINER
        else:
            raise ValueError(f"unrecognized role suffix {role_suffix!r} in {container_name!r}")

        # Port mapping: inspect each internal port
        ports = _inspect_port_mapping(container_name)

        # Network name: first network attached (we expect exactly one)
        net_result = _docker(
            "inspect",
            "-f",
            "{{range $k, $_ := .NetworkSettings.Networks}}{{$k}} {{end}}",
            container_name,
        )
        networks = (net_result.stdout or "").strip().split()
        network = networks[0] if networks else ""

        # Volume name follows framework convention (not recoverable from inspect
        # reliably; best-effort for cleanup hooks that need it).
        session_id = parts[2]
        volume_name = f"test-{session_id}-{role_suffix}-data"

        return cls(
            name=container_name,
            ports=ports,
            network=network,
            role=role,
            identity=None,
            volume_name=volume_name,
        )


def _inspect_port_mapping(container_name: str) -> PortMapping:
    """Read the host ports that map to each internal 40400-40405 port."""
    internal_ports = (40400, 40401, 40402, 40403, 40404, 40405)
    host_ports: Dict[int, int] = {}
    for iport in internal_ports:
        result = _docker("port", container_name, f"{iport}/tcp")
        line = (result.stdout or "").strip().splitlines()
        if not line:
            raise ValueError(
                f"container {container_name!r} has no host mapping for "
                f"internal port {iport} — was it started by this framework?"
            )
        # Format: "0.0.0.0:41234"  (may include IPv6 line; take IPv4)
        ipv4 = next(
            (entry for entry in line if ":" in entry and not entry.startswith("[")), line[0]
        )
        host_ports[iport] = int(ipv4.rsplit(":", 1)[1])
    return PortMapping(
        protocol=host_ports[40400],
        grpc_ext=host_ports[40401],
        grpc_int=host_ports[40402],
        http=host_ports[40403],
        discovery=host_ports[40404],
        admin=host_ports[40405],
    )


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
        registry: DockerCleanupRegistry,
        timeouts: TimeoutHierarchy,
        paths: Optional[ResourcePaths] = None,
    ) -> None:
        self._ports = port_allocator
        self._registry = registry
        self._timeouts = timeouts
        self._paths = paths or ResourcePaths.resolve()
        self._session_id = registry.session_id
        self._standalone_counter = 0
        self._shard_counter = 0
        self._active_handles: list = []
        self._retired_log_snapshots: List[RetiredLogSnapshot] = []

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def keep_running(self) -> bool:
        return self._registry.keep_running

    @property
    def keep_on_failure(self) -> bool:
        return self._registry.keep_on_failure

    @property
    def _archive_dir(self) -> Path:
        """Root directory for per-session log archival.

        Resolved once via :py:func:`archive_root_for`; tied to this
        provider's session id. Per-shard / standalone / leftover paths
        are formed by joining a subdir onto this.
        """
        return archive_root_for(self._paths.integration_tests, self._session_id)

    @property
    def active_handles(self) -> list:
        return list(self._active_handles)

    def host_process_guardian_token(self) -> Optional[str]:
        """Docker nodes are cgroup-capped containers, not host processes — host
        protection is delegated to the container runtime, so there is no
        out-of-process guardian (the in-process RSS ceiling still applies)."""
        return None

    @property
    def monitor_output_dir(self) -> Optional[Path]:
        """Monitor artifacts land in the per-session archive dir.

        The monitor runs host-side (sampling ``docker stats``), so nothing
        about containerized nodes prevents host-visible output. Returning
        ``None`` here silently disabled BOTH the resource/metrics CSV
        time-series (soak per-node RSS attribution joined against them) and
        the ``host-protection-breach.txt`` marker channel on docker
        iterations — the 2026-08-04 soak breach (f1r3node-rust run
        30880995655) had to be attributed from the teardown peak/avg table
        alone. The archive dir is the same host-visible per-session
        location the log archival uses, so CI's integration-tests tree
        capture picks these up with no extra wiring."""
        return self._archive_dir

    @property
    def retired_log_snapshots(self) -> List[RetiredLogSnapshot]:
        return list(self._retired_log_snapshots)

    def clear_retired_log_snapshots(self) -> None:
        self._retired_log_snapshots.clear()

    # ── Shard lifecycle ─────────────────────────────────────────────

    def create_shard(
        self, config: ShardConfig, wait_running: bool = True
    ) -> List[DockerNodeHandle]:
        """Create a full shard via docker compose.

        Generates genesis, compose file, runs ``docker compose up -d``.
        If ``wait_running`` is True (default), waits for all nodes to
        reach Running state. Set False for tests expecting startup failure.

        Returns handles in order: [boot, validator1, ..., readonly].
        """
        genesis_dir = generate_genesis(config, self._paths, self._registry)

        # Allocate ports: boot + N validators + optional readonly
        roles = ["boot"] + [f"validator{i + 1}" for i in range(len(config.bonds))]
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

        # Per-test compose project name. Each call to create_shard gets a
        # unique project, so tests on the same xdist worker can't leave
        # stale state in each other's compose project (e.g. a container
        # ID that one test's teardown removed but another test's compose
        # still references — surfaces as "Container ... Recreate" /
        # "No such container" on the next compose up).
        self._shard_counter += 1
        project_name = f"test-{self._session_id}-{self._shard_counter}"

        # Start all services. Retry on Docker-on-Mac transient network race
        # (network just created but daemon reports "not found" when attaching
        # the first container) — clean up the partial state and try again.
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            result = _compose("up", "-d", compose_file=compose_path, project_name=project_name)
            if result.returncode == 0:
                break

            stderr = result.stderr or ""
            transient_network_race = (
                "failed to set up container networking" in stderr and "not found" in stderr
            )
            if not transient_network_race or attempt == max_attempts:
                logger.error("docker compose up failed: %s", stderr)
                raise RuntimeError(f"docker compose up failed: {stderr}")

            logger.warning(
                "docker compose up hit transient network race (attempt %d/%d), "
                "tearing down and retrying",
                attempt,
                max_attempts,
            )
            _compose(
                "down",
                "--volumes",
                "--remove-orphans",
                compose_file=compose_path,
                project_name=project_name,
            )
            import time

            time.sleep(2)

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
            handles.append(
                DockerNodeHandle(
                    name=f"rnode.test.{self._session_id}.{role_name}",
                    ports=port_map[role_name],
                    network=f"f1r3fly-test-{self._session_id}",
                    role=NodeRole.VALIDATOR,
                    identity=identity,
                )
            )

        if config.include_readonly:
            handles.append(
                DockerNodeHandle(
                    name=f"rnode.test.{self._session_id}.readonly",
                    ports=port_map["readonly"],
                    network=f"f1r3fly-test-{self._session_id}",
                    role=NodeRole.READONLY,
                )
            )

        # Wait for all nodes to reach Running state
        if wait_running:
            wait_for_handles_or_archive(
                handles,
                self._archive_dir / f"shard{self._shard_counter}",
                self._timeouts.node_startup,
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
        """Destroy a shard — compose down with volumes.

        Snapshots each handle's log into the retired bucket before the
        compose teardown so the autouse forbidden-pattern scanner sees
        them — the scanner runs after the test's ``finally:
        shard.destroy()`` returns and the live handles are gone by then.
        Symmetric to the per-handle snapshot in ``remove_node`` used by
        ``add_joiner`` / ``add_observer``.
        """
        for h in handles:
            try:
                snapshot_text = h.logs()
            except Exception:
                snapshot_text = ""
            self._retired_log_snapshots.append(
                RetiredLogSnapshot(name=h.name, log_text=snapshot_text)
            )
            if h in self._active_handles:
                self._active_handles.remove(h)
        if self.keep_running:
            logger.info("Shard kept running (--keep-running)")
            return
        if self.keep_on_failure and current_test_failed():
            self._registry.preserved_on_failure = True
            logger.warning(
                "Shard PRESERVED (--keep-on-failure; test failed). Run "
                "`shardctl test-reset` when done inspecting."
            )
            return

        archive_handles(handles, self._archive_dir / f"shard{self._shard_counter}")

        shard_key = f"shard-{self._session_id}"
        compose_files = getattr(self, "_compose_files", {})
        if shard_key in compose_files:
            compose_path, project_name, genesis_dir = compose_files.pop(shard_key)
            _compose(
                "down",
                "--volumes",
                "--remove-orphans",
                compose_file=compose_path,
                project_name=project_name,
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

        _ensure_network(network_name)
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
            "run",
            "-d",
            "--rm=false",
            "--user",
            "root",
            *_NODE_PORT_RESERVATION_ARGS,
            "--name",
            container_name,
            "--network",
            network_name,
            "--network-alias",
            container_name,
            "-v",
            volume_arg,
            "-v",
            f"{conf_path}:/var/lib/rnode/rnode.conf:ro",
            "-v",
            f"{wallets_path}:/var/lib/rnode/genesis/wallets.txt:ro",
            "-v",
            f"{bonds_path}:/var/lib/rnode/genesis/bonds.txt:ro",
        ]

        # Mount bootstrap TLS certs when using shard conf so joiners can
        # connect using the known BOOTSTRAP_NODE_ID
        if use_shard_conf:
            run_args.extend(
                [
                    "-v",
                    f"{self._paths.certs_dir}/bootstrap/node.certificate.pem:/var/lib/rnode/node.certificate.pem:ro",
                    "-v",
                    f"{self._paths.certs_dir}/bootstrap/node.key.pem:/var/lib/rnode/node.key.pem:ro",
                ]
            )

        run_args.extend(
            [
                "-p",
                f"{ports.protocol}:40400",
                "-p",
                f"{ports.grpc_ext}:40401",
                "-p",
                f"{ports.grpc_int}:40402",
                "-p",
                f"{ports.http}:40403",
                "-p",
                f"{ports.discovery}:40404",
                "-p",
                f"{ports.admin}:40405",
                image,
                "run",
                "-s",
                f"--host={container_name}",
                f"--validator-private-key={_BOOTSTRAP_PRIVATE_KEY}",
                "--allow-private-addresses",
                *extra_cli,
            ]
        )

        _ensure_no_container(container_name)
        result = _docker_run(run_args)
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
            wait_for_handles_or_archive(
                [handle],
                self._archive_dir,
                self._timeouts.node_startup,
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
        # Snapshot the stopped container's logs before _ensure_no_container
        # removes it. The new container reuses the same name, so its
        # destroy_standalone archive would write to the same dest path —
        # use a `.pre-recreate.log` suffix to keep both runs distinct.
        handle.archive_log(self._archive_dir / f"{container_name}.pre-recreate.log")
        _ensure_no_container(container_name)
        _ensure_network(network_name)

        extra_cli: List[str] = []
        for flag in sorted(config.cli_flags):
            extra_cli.append(flag)
        for k, v in sorted(config.cli_options.items()):
            extra_cli.append(f"{k}={v}" if v else k)

        image = resolve_node_image()

        volume_name = handle.volume_name
        volume_arg = f"{volume_name}:/var/lib/rnode" if volume_name else "/var/lib/rnode"

        run_args = [
            "run",
            "-d",
            "--rm=false",
            "--user",
            "root",
            *_NODE_PORT_RESERVATION_ARGS,
            "--name",
            container_name,
            "--network",
            network_name,
            "--network-alias",
            container_name,
            "-v",
            volume_arg,
            "-v",
            f"{self._paths.standalone_conf}:/var/lib/rnode/rnode.conf:ro",
            "-v",
            f"{self._paths.standalone_wallets}:/var/lib/rnode/genesis/wallets.txt:ro",
            "-v",
            f"{self._paths.standalone_bonds}:/var/lib/rnode/genesis/bonds.txt:ro",
            "-p",
            f"{ports.protocol}:40400",
            "-p",
            f"{ports.grpc_ext}:40401",
            "-p",
            f"{ports.grpc_int}:40402",
            "-p",
            f"{ports.http}:40403",
            "-p",
            f"{ports.discovery}:40404",
            "-p",
            f"{ports.admin}:40405",
            image,
            "run",
            "-s",
            f"--host={container_name}",
            f"--validator-private-key={_BOOTSTRAP_PRIVATE_KEY}",
            "--allow-private-addresses",
            *extra_cli,
        ]

        result = _docker_run(run_args)
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
            wait_for_handles_or_archive(
                [new_handle],
                self._archive_dir,
                self._timeouts.node_startup,
            )

        return new_handle

    def destroy_standalone(self, handle: DockerNodeHandle) -> None:
        if handle in self._active_handles:
            self._active_handles.remove(handle)
        if self.keep_running:
            logger.info("Standalone %s kept running (--keep-running)", handle.name)
            return

        archive_handles([handle], self._archive_dir)

        handle.remove()
        suffix = handle.name.split("standalone")[-1] if "standalone" in handle.name else ""
        vol = f"test-{self._session_id}-standalone{suffix}-data"
        _docker("volume", "rm", "-f", vol)
        _docker("network", "rm", handle.network_name)

    # ── Joiner / observer lifecycle ─────────────────────────────────

    def add_node(
        self,
        shard_network: str,
        node_config: NodeConfig,
        bootstrap_handle: DockerNodeHandle,
        wait_running: bool = True,
    ) -> DockerNodeHandle:
        """Add a joiner or observer node to an existing shard.

        Role is taken from ``node_config.role``. JOINER attaches with
        validator identity (proposes); READONLY attaches without
        identity and with ``--heartbeat-disabled`` (sync-only).

        Args:
            wait_running: If True (default), wait for the node to reach
                Running state. Set False for tests expecting startup failure
                (e.g. token metadata mismatch).
        """
        role = node_config.role
        if role == NodeRole.JOINER:
            self._joiner_counter = getattr(self, "_joiner_counter", 0) + 1
            role_key = f"joiner{self._joiner_counter}"
        elif role == NodeRole.READONLY:
            self._observer_counter = getattr(self, "_observer_counter", 0) + 1
            role_key = f"observer{self._observer_counter}"
        else:
            raise ValueError(f"add_node only supports JOINER or READONLY, got {role}")

        ports = self._ports.allocate()
        identity = node_config.identity
        node_name = f"rnode.test.{self._session_id}.{role_key}"
        volume_name = f"test-{self._session_id}-{role_key}-data"

        bootstrap_url = (
            f"rnode://{BOOTSTRAP_NODE_ID}@{bootstrap_handle.name}?protocol=40400&discovery=40404"
        )

        image = resolve_node_image()

        extra_cli: List[str] = []
        for flag in sorted(node_config.cli_flags):
            extra_cli.append(flag)
        for k, v in sorted(node_config.cli_options.items()):
            extra_cli.append(f"{k}={v}" if v else k)

        cmd: List[str] = [
            "run",
            f"--host={node_name}",
            f"--bootstrap={bootstrap_url}",
            "--allow-private-addresses",
        ]
        if role == NodeRole.READONLY:
            cmd.append("--heartbeat-disabled")
        if identity:
            cmd.extend(
                [
                    f"--validator-public-key={identity.public_hex}",
                    f"--validator-private-key={identity.private_hex}",
                ]
            )
        cmd.extend(extra_cli)

        run_args = [
            "run",
            "-d",
            "--rm=false",
            "--user",
            "root",
            *_NODE_PORT_RESERVATION_ARGS,
            "--name",
            node_name,
            "--network",
            shard_network,
            "--network-alias",
            node_name,
            "-v",
            f"{volume_name}:/var/lib/rnode",
            "-v",
            f"{self._paths.rust_conf}:/var/lib/rnode/rnode.conf:ro",
            "-p",
            f"{ports.protocol}:40400",
            "-p",
            f"{ports.grpc_ext}:40401",
            "-p",
            f"{ports.grpc_int}:40402",
            "-p",
            f"{ports.http}:40403",
            "-p",
            f"{ports.discovery}:40404",
            "-p",
            f"{ports.admin}:40405",
            image,
            *cmd,
        ]

        _docker("volume", "create", volume_name)
        self._registry.register_volume(volume_name)

        _ensure_no_container(node_name)
        _ensure_network(shard_network)
        result = _docker_run(run_args)
        if result.returncode != 0:
            raise RuntimeError(f"docker run ({role_key}) failed: {result.stderr}")

        self._registry.register_container(node_name)

        handle = DockerNodeHandle(
            name=node_name,
            ports=ports,
            network=shard_network,
            role=role,
            identity=identity,
            volume_name=volume_name,
        )

        if wait_running:
            wait_for_handles_or_archive(
                [handle],
                self._archive_dir,
                self._timeouts.node_startup,
            )

        self._active_handles.append(handle)
        return handle

    def remove_node(self, handle: DockerNodeHandle) -> None:
        if handle in self._active_handles:
            self._active_handles.remove(handle)
        # Snapshot the node's logs before any teardown that would
        # destroy its container/volume, so the autouse log scanner
        # still sees whatever the transient node emitted before its
        # handle was detached.
        try:
            snapshot_text = handle.logs()
        except Exception:
            snapshot_text = ""
        self._retired_log_snapshots.append(
            RetiredLogSnapshot(name=handle.name, log_text=snapshot_text)
        )
        if self.keep_running:
            logger.info("Node %s kept running (--keep-running)", handle.name)
            return

        archive_handles([handle], self._archive_dir)

        handle.remove()
        if handle.volume_name:
            _docker("volume", "rm", "-f", handle.volume_name)

    # ── Global cleanup ──────────────────────────────────────────────

    def cleanup_all(self) -> None:
        self._registry.cleanup_all()

    @classmethod
    def force_cleanup_all_test_resources(cls) -> None:
        """Delegate to the Docker-specific aggressive cleanup helper.

        Keeps the shell-out-to-``docker`` logic in one place
        (:py:class:`DockerCleanupRegistry`) while exposing a provider-neutral
        entry point for ``shardctl test-reset``.
        """
        DockerCleanupRegistry.force_cleanup_all_test_resources()

    @classmethod
    def cleanup_session(cls, session_id: str) -> None:
        """Delegate to the registry's session-scoped cleanup."""
        DockerCleanupRegistry.cleanup_session(session_id)

    # ── Session adoption (--skip-setup --session-id) ────────────────

    def adopt_session(self, session_id: str) -> List[DockerNodeHandle]:
        """Find and wrap containers from a previous session.

        Scans ``docker ps -a --filter name=rnode.test.{session_id}.``,
        builds a ``DockerNodeHandle`` per container, and returns them
        in canonical role order (bootstrap, validator1..N, readonly).
        """
        prefix = f"rnode.test.{session_id}."
        # Only adopt running containers — stopped ones can't serve requests.
        result = _docker("ps", "--filter", f"name={prefix}", "--format", "{{.Names}}")
        if result.returncode != 0:
            raise RuntimeError(
                f"docker ps failed while adopting session {session_id!r}: {result.stderr.strip()}"
            )
        names = sorted((result.stdout or "").strip().splitlines())
        if not names:
            raise ValueError(
                f"no running containers found for session_id={session_id!r}. "
                f"Expected names matching {prefix}* (was the session torn down?)"
            )

        # Sort so bootstrap comes first, then validators by number, then readonly.
        def _sort_key(name: str) -> tuple:
            suffix = name.rsplit(".", 1)[-1]
            if suffix == "boot":
                return (0, 0)
            if suffix.startswith("validator"):
                try:
                    return (1, int(suffix[len("validator") :]))
                except ValueError:
                    return (1, 0)
            if suffix == "readonly":
                return (2, 0)
            return (3, 0)

        names.sort(key=_sort_key)
        handles = [DockerNodeHandle.from_container(n) for n in names]
        self._active_handles.extend(handles)
        logger.info(
            "Adopted shard for session %s: %d nodes (%s)",
            session_id,
            len(handles),
            ", ".join(h.name.rsplit(".", 1)[-1] for h in handles),
        )
        return handles

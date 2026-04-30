"""Subprocess provider — spawn nodes as host processes (no Docker).

Implements the ``Provider`` protocol by running the locally-built
``services/f1r3node-rust/target/release/node`` binary directly as
``subprocess.Popen`` instances on ``localhost``. Each node gets its own
data directory under a session-scoped tree, its own pre-allocated host
ports, and its own captured stdout/stderr log file.

Compared to ``DockerProvider``:
  - **Real** OS-process isolation, real gRPC over loopback, real network
    timing — every axis the existing in-process casper-test framework
    can't model.
  - **No** container build, image pull, or daemon dependency.

Trades container-level isolation (resource limits, network namespaces)
for fewer moving parts. Suitable for development iterations and CI runs
where Docker overhead matters.

Resource lifetime is owned end-to-end by this provider — process IDs and
data directories are tracked in instance state, not in the Docker-specific
``DockerCleanupRegistry``. Stale-session cleanup (called from
``pytest_sessionstart``) discovers prior runs by scanning the
session-scoped data-dir base.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Sequence

from ..config import NodeConfig, ResourcePaths, ShardConfig, resolve_node_binary
from ..genesis import generate_genesis
from ..keys import BOOTSTRAP_NODE_ID
from ..polling import wait_for_node_running
from ..ports import PortAllocator
from ..timeouts import TimeoutHierarchy
from ..types import NodeRole, PortMapping, ValidatorIdentity

logger = logging.getLogger(__name__)

# Bootstrap private key — same across all shards (matches certs shipped at
# integration-tests/certs/bootstrap/, which the bootstrap node uses for TLS
# so its NODE_ID is deterministic and validators can address the bootstrap
# URL without an introspection round-trip).
_BOOTSTRAP_PRIVATE_KEY = (
    "5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"
)

# Per-session subprocess data lives under the integration-tests directory
# in a hidden folder. Repo-local makes inspection easy; .gitignore should
# keep it out of commits.
_DATA_DIR_BASENAME = ".subprocess-data"

# Sentinel arg used to identify framework-spawned processes during stale
# discovery (every spawned `node run` command line contains this token via
# its `--data-dir` value, but the basename is the most reliable scan key).
_DATA_DIR_SCAN_TOKEN = _DATA_DIR_BASENAME


class SubprocessNodeHandle:
    """Handle to a node spawned as a host process by SubprocessProvider."""

    def __init__(
        self,
        name: str,
        ports: PortMapping,
        role: NodeRole,
        data_dir: Path,
        log_path: Path,
        proc: subprocess.Popen,
        log_fh,
        spawn_args: List[str],
        spawn_env: dict,
        identity: Optional[ValidatorIdentity] = None,
    ) -> None:
        self._name = name
        self._ports = ports
        self._role = role
        self._data_dir = data_dir
        self._log_path = log_path
        self._proc = proc
        # Hold the open file handle so logs() can read it via the path; we
        # close on stop/remove. Without this the OS may delay flushing.
        self._log_fh = log_fh
        self._spawn_args = spawn_args
        self._spawn_env = spawn_env
        self._identity = identity

    # ── Properties ──────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    @property
    def ports(self) -> PortMapping:
        return self._ports

    @property
    def grpc_host(self) -> str:
        # 127.0.0.1, not localhost: Rust gRPC client builds malformed URIs
        # ("http://::1:PORT/") when the peer hostname resolves to IPv6.
        return "127.0.0.1"

    @property
    def network_name(self) -> str:
        # Synthetic — subprocess provider has no network resource of its
        # own. Returns a session-scoped string so callers that key off
        # network_name (e.g. add_node) still get something deterministic.
        return f"subprocess-{self._data_dir.parent.name}"

    @property
    def role(self) -> NodeRole:
        return self._role

    @property
    def identity(self) -> Optional[ValidatorIdentity]:
        return self._identity

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def pid(self) -> int:
        return self._proc.pid

    # ── Logs ────────────────────────────────────────────────────────

    def logs(self, tail: Optional[int] = None) -> str:
        if not self._log_path.exists():
            return ""
        text = self._log_path.read_text(errors="replace")
        if tail is None:
            return text
        return "\n".join(text.splitlines()[-tail:])

    # ── Process state ───────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._proc.poll() is None

    def exit_code(self) -> Optional[int]:
        return self._proc.poll()

    def wait_for_exit(self, timeout: int = 180) -> Optional[int]:
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    # ── Lifecycle controls ──────────────────────────────────────────

    def pause(self) -> None:
        if self.is_running():
            os.kill(self._proc.pid, signal.SIGSTOP)

    def unpause(self) -> None:
        if self.is_running():
            os.kill(self._proc.pid, signal.SIGCONT)

    def restart(self) -> None:
        """Stop the process, then re-spawn with the same args/env."""
        self._stop_once()
        # Re-open log file in append mode so the restart history is kept.
        self._log_fh = open(self._log_path, "a")
        self._proc = subprocess.Popen(
            self._spawn_args,
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            env=self._spawn_env,
            start_new_session=True,
        )

    def _stop_once(self) -> None:
        """SIGTERM the process group; escalate to SIGKILL after 30s."""
        if self._proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self._proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Subprocess %s (pid=%d) survived SIGKILL; ignoring",
                    self._name, self._proc.pid,
                )
        finally:
            try:
                self._log_fh.close()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_once()

    def remove(self) -> None:
        self._stop_once()
        shutil.rmtree(self._data_dir, ignore_errors=True)

    # ── Resource usage ──────────────────────────────────────────────

    def resource_usage(self) -> dict:
        """Returns memory_mb, cpu_percent, memory_limit_mb (None — no limit)."""
        if not self.is_running():
            return {"memory_mb": 0.0, "cpu_percent": 0.0, "memory_limit_mb": None}
        try:
            result = subprocess.run(
                ["ps", "-p", str(self._proc.pid), "-o", "rss=,%cpu="],
                capture_output=True, text=True, timeout=5,
            )
            line = (result.stdout or "").strip()
            if not line:
                return {"memory_mb": 0.0, "cpu_percent": 0.0, "memory_limit_mb": None}
            rss_kb_str, cpu_str = line.split()
            return {
                "memory_mb": float(rss_kb_str) / 1024.0,
                "cpu_percent": float(cpu_str),
                "memory_limit_mb": None,
            }
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return {"memory_mb": 0.0, "cpu_percent": 0.0, "memory_limit_mb": None}


# ── SubprocessProvider ─────────────────────────────────────────────────


class SubprocessProvider:
    """Spawn nodes as host processes. Implements the ``Provider`` protocol."""

    def __init__(
        self,
        port_allocator: PortAllocator,
        session_id: str,
        keep_running: bool,
        timeouts: TimeoutHierarchy,
        paths: Optional[ResourcePaths] = None,
        binary_path: Optional[str] = None,
    ) -> None:
        self._ports = port_allocator
        self._session_id = session_id
        self._keep_running = keep_running
        self._timeouts = timeouts
        self._paths = paths or ResourcePaths.resolve()

        binary = Path(binary_path or resolve_node_binary(self._paths.repo_root))
        if not binary.exists() or not os.access(binary, os.X_OK):
            raise RuntimeError(
                f"Node binary not found or not executable at {binary}. "
                "Build it first:\n"
                "  cd services/f1r3node-rust && cargo build --release -p node\n"
                "Or set F1R3FLY_NODE_BINARY=/path/to/node to override."
            )
        self._binary = binary

        self._session_root = self._session_data_root(self._paths, self._session_id)
        self._session_root.mkdir(parents=True, exist_ok=True)

        self._active_handles: List[SubprocessNodeHandle] = []
        self._standalone_counter = 0
        self._joiner_counter = 0

        # SIGTERM-safe: registered processes are reaped on cleanup_all (which
        # the conftest fixture calls on teardown) and on pytest_sessionfinish
        # via the provider-dispatch hook.

    # ── Helpers ─────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def keep_running(self) -> bool:
        return self._keep_running

    @property
    def active_handles(self) -> List[SubprocessNodeHandle]:
        return list(self._active_handles)

    @staticmethod
    def _session_data_root(paths: ResourcePaths, session_id: str) -> Path:
        return Path(paths.integration_tests) / _DATA_DIR_BASENAME / session_id

    @staticmethod
    def _data_dir_base(paths: ResourcePaths) -> Path:
        return Path(paths.integration_tests) / _DATA_DIR_BASENAME

    def _node_name(self, role_key: str) -> str:
        return f"rnode.test.{self._session_id}.{role_key}"

    def _bootstrap_url(self, boot_handle: SubprocessNodeHandle) -> str:
        # 127.0.0.1, not localhost — see grpc_host comment.
        return (
            f"rnode://{BOOTSTRAP_NODE_ID}@127.0.0.1"
            f"?protocol={boot_handle.ports.protocol}"
            f"&discovery={boot_handle.ports.discovery}"
        )

    def _build_extra_cli(self, node_config: NodeConfig) -> List[str]:
        extra: List[str] = []
        for flag in sorted(node_config.cli_flags):
            extra.append(flag)
        for k, v in sorted(node_config.cli_options.items()):
            extra.append(f"{k}={v}" if v else k)
        return extra

    def _spawn(
        self,
        role_key: str,
        role: NodeRole,
        ports: PortMapping,
        cli_args: List[str],
        cert_subdir: Optional[str],
        identity: Optional[ValidatorIdentity] = None,
        extra_env: Optional[dict] = None,
    ) -> SubprocessNodeHandle:
        """Spawn a node process. ``cli_args`` is the list of arguments AFTER
        ``run`` (i.e. flags like ``--data-dir``, ``--bootstrap``, etc.).
        ``cert_subdir``, if set, points the node at the pre-generated TLS
        keypair under ``paths.certs_dir/<cert_subdir>/``."""
        data_dir = self._session_root / role_key
        data_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._session_root / f"{role_key}.log"

        cmd: List[str] = [
            str(self._binary), "run",
            "--config-file", self._paths.rust_conf,
            "--data-dir", str(data_dir),
            "--host", "127.0.0.1",
            "--protocol-port", str(ports.protocol),
            "--api-port-grpc-external", str(ports.grpc_ext),
            "--api-port-grpc-internal", str(ports.grpc_int),
            "--api-port-http", str(ports.http),
            "--api-port-admin-http", str(ports.admin),
            "--discovery-port", str(ports.discovery),
        ]
        if cert_subdir is not None:
            cmd += [
                "--tls-key-path", str(Path(self._paths.certs_dir) / cert_subdir / "node.key.pem"),
                "--tls-certificate-path", str(Path(self._paths.certs_dir) / cert_subdir / "node.certificate.pem"),
            ]
        cmd += cli_args

        env = os.environ.copy()
        env.setdefault("RUST_LOG", "info")
        env.setdefault("OPENAI_ENABLED", "false")
        if extra_env:
            env.update(extra_env)

        log_fh = open(log_path, "w")
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,  # own process group → SIGTERM the group
        )

        return SubprocessNodeHandle(
            name=self._node_name(role_key),
            ports=ports,
            role=role,
            data_dir=data_dir,
            log_path=log_path,
            proc=proc,
            log_fh=log_fh,
            spawn_args=cmd,
            spawn_env=env,
            identity=identity,
        )

    # ── Shard lifecycle ─────────────────────────────────────────────

    def create_shard(self, config: ShardConfig, wait_running: bool = True) -> List[SubprocessNodeHandle]:
        """Create a full shard via host processes.

        Generates genesis files, allocates ports per role, spawns boot first
        (so its TLS-derived NODE_ID matches BOOTSTRAP_NODE_ID), then
        validators and the optional readonly observer. If ``wait_running`` is
        True (default), waits for each node to reach ``isReady`` via
        ``/api/status``.
        """
        # Genesis files (bonds.txt + wallets.txt) — written to a temp dir
        # under the session root so cleanup catches them.
        tmp_genesis_dir = self._session_root / "genesis"
        tmp_genesis_dir.mkdir(parents=True, exist_ok=True)
        # Use the existing generate_genesis machinery; it writes into a
        # NamedTemporaryDirectory but registers nothing here (we own
        # cleanup via session_root). Fall back to writing inline.
        genesis_dir = self._write_genesis(config, tmp_genesis_dir)

        roles: List[str] = ["boot"] + [
            f"validator{i + 1}" for i in range(len(config.bonds))
        ]
        if config.include_readonly:
            roles.append("readonly")

        port_map = {role: self._ports.allocate() for role in roles}

        handles: List[SubprocessNodeHandle] = []

        # ── Bootstrap ──
        boot_cli = [
            f"--validator-private-key={_BOOTSTRAP_PRIVATE_KEY}",
            f"--required-signatures={config.effective_required_signatures}",
            "--ceremony-master-mode",
            "--allow-private-addresses",
            f"--bonds-file={genesis_dir}/bonds.txt",
            f"--wallets-file={genesis_dir}/wallets.txt",
        ]
        if config.ftt is not None:
            boot_cli.append(f"--fault-tolerance-threshold={config.ftt}")
        if not config.heartbeat:
            boot_cli.append("--heartbeat-disabled")

        boot_handle = self._spawn(
            role_key="boot",
            role=NodeRole.BOOTSTRAP,
            ports=port_map["boot"],
            cli_args=boot_cli,
            cert_subdir="bootstrap",
        )
        handles.append(boot_handle)

        # ── Validators ──
        bootstrap_url = self._bootstrap_url(boot_handle)
        for idx, (identity, _stake) in enumerate(config.bonds):
            slot = idx + 1
            role_key = f"validator{slot}"
            cert_subdir = f"validator{slot}" if slot <= 3 else None  # certs shipped for v1-v3
            v_cli = [
                f"--bootstrap={bootstrap_url}",
                f"--validator-public-key={identity.public_hex}",
                f"--validator-private-key={identity.private_hex}",
                "--genesis-validator",
                f"--required-signatures={config.effective_required_signatures}",
                "--allow-private-addresses",
                f"--bonds-file={genesis_dir}/bonds.txt",
                f"--wallets-file={genesis_dir}/wallets.txt",
            ]
            if config.ftt is not None:
                v_cli.append(f"--fault-tolerance-threshold={config.ftt}")
            if not config.heartbeat:
                v_cli.append("--heartbeat-disabled")

            handles.append(self._spawn(
                role_key=role_key,
                role=NodeRole.VALIDATOR,
                ports=port_map[role_key],
                cli_args=v_cli,
                cert_subdir=cert_subdir,
                identity=identity,
            ))

        # ── Readonly observer (optional) ──
        if config.include_readonly:
            ro_cli = [
                f"--bootstrap={bootstrap_url}",
                "--no-upnp",
                "--allow-private-addresses",
                "--heartbeat-disabled",  # readonly never proposes
                f"--bonds-file={genesis_dir}/bonds.txt",
                f"--wallets-file={genesis_dir}/wallets.txt",
            ]
            handles.append(self._spawn(
                role_key="readonly",
                role=NodeRole.READONLY,
                ports=port_map["readonly"],
                cli_args=ro_cli,
                cert_subdir=None,  # readonly auto-generates its own cert
            ))

        # ── Wait for all to reach Running ──
        if wait_running:
            for h in handles:
                wait_for_node_running(
                    get_logs=h.logs,
                    is_running=h.is_running,
                    node_name=h.name,
                    timeout=self._timeouts.node_startup,
                    status_url=f"http://{h.grpc_host}:{h.ports.http}/api/status",
                )

        self._active_handles.extend(handles)
        return handles

    def _write_genesis(self, config: ShardConfig, target_dir: Path) -> Path:
        """Write bonds.txt + wallets.txt into target_dir.

        Equivalent to ``infra.genesis.generate_genesis`` but without the
        ``DockerCleanupRegistry`` dependency — subprocess provider owns its
        own cleanup via the session-root directory.
        """
        # bonds.txt
        bonds_path = target_dir / "bonds.txt"
        with bonds_path.open("w") as f:
            for identity, stake in config.bonds:
                f.write(f"{identity.public_hex} {stake}\n")

        # wallets.txt — copy default + extras
        wallets_path = target_dir / "wallets.txt"
        shutil.copy2(self._paths.genesis_wallets, wallets_path)
        if config.extra_wallets:
            with wallets_path.open("a") as f:
                for vault_addr, balance in config.extra_wallets:
                    f.write(f"{vault_addr},{balance}\n")

        logger.info(
            "Subprocess genesis written to %s (bonds: %s, extra_wallets: %d)",
            target_dir,
            ", ".join(f"{v.public_hex[:8]}...={s}" for v, s in config.bonds),
            len(config.extra_wallets or []),
        )
        return target_dir

    def destroy_shard(self, handles: Sequence[SubprocessNodeHandle]) -> None:
        if self._keep_running:
            logger.info(
                "Subprocess shard for session %s kept running (--keep-running). "
                "PIDs: %s. Data: %s",
                self._session_id,
                ", ".join(str(h.pid) for h in handles),
                self._session_root,
            )
            return
        for h in handles:
            try:
                h.remove()
            finally:
                if h in self._active_handles:
                    self._active_handles.remove(h)

    # ── Standalone ──────────────────────────────────────────────────

    def create_standalone(self, config: NodeConfig, wait_running: bool = True) -> SubprocessNodeHandle:
        self._standalone_counter += 1
        suffix = self._standalone_counter
        role_key = f"standalone{suffix}"
        ports = self._ports.allocate()

        extra_cli = self._build_extra_cli(config)
        cli = [
            "-s",
            f"--validator-private-key={_BOOTSTRAP_PRIVATE_KEY}",
            "--allow-private-addresses",
            f"--bonds-file={self._paths.standalone_bonds}",
            f"--wallets-file={self._paths.standalone_wallets}",
        ]
        cli.extend(extra_cli)

        # Override config-file: standalone uses its own conf (smaller
        # validator set, instant finalization, etc.), not rust.conf.
        # _spawn() always passes --config-file=rust_conf; replace with
        # standalone_conf via a per-call override.
        handle = self._spawn_with_config_override(
            role_key=role_key,
            role=NodeRole.STANDALONE,
            ports=ports,
            config_file=self._paths.standalone_conf,
            cli_args=cli,
            cert_subdir=None,  # standalone auto-generates
        )

        if wait_running:
            wait_for_node_running(
                get_logs=handle.logs,
                is_running=handle.is_running,
                node_name=handle.name,
                timeout=self._timeouts.node_startup,
                status_url=f"http://{handle.grpc_host}:{handle.ports.http}/api/status",
            )

        self._active_handles.append(handle)
        return handle

    def _spawn_with_config_override(
        self,
        role_key: str,
        role: NodeRole,
        ports: PortMapping,
        config_file: str,
        cli_args: List[str],
        cert_subdir: Optional[str],
    ) -> SubprocessNodeHandle:
        """Like ``_spawn`` but uses ``config_file`` instead of rust.conf."""
        data_dir = self._session_root / role_key
        data_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._session_root / f"{role_key}.log"

        cmd: List[str] = [
            str(self._binary), "run",
            "--config-file", config_file,
            "--data-dir", str(data_dir),
            "--host", "127.0.0.1",
            "--protocol-port", str(ports.protocol),
            "--api-port-grpc-external", str(ports.grpc_ext),
            "--api-port-grpc-internal", str(ports.grpc_int),
            "--api-port-http", str(ports.http),
            "--api-port-admin-http", str(ports.admin),
            "--discovery-port", str(ports.discovery),
        ]
        if cert_subdir is not None:
            cmd += [
                "--tls-key-path", str(Path(self._paths.certs_dir) / cert_subdir / "node.key.pem"),
                "--tls-certificate-path", str(Path(self._paths.certs_dir) / cert_subdir / "node.certificate.pem"),
            ]
        cmd += cli_args

        env = os.environ.copy()
        env.setdefault("RUST_LOG", "info")
        env.setdefault("OPENAI_ENABLED", "false")

        log_fh = open(log_path, "w")
        proc = subprocess.Popen(
            cmd, stdout=log_fh, stderr=subprocess.STDOUT, env=env,
            start_new_session=True,
        )

        return SubprocessNodeHandle(
            name=self._node_name(role_key),
            ports=ports,
            role=role,
            data_dir=data_dir,
            log_path=log_path,
            proc=proc,
            log_fh=log_fh,
            spawn_args=cmd,
            spawn_env=env,
        )

    def destroy_standalone(self, handle: SubprocessNodeHandle) -> None:
        if handle in self._active_handles:
            self._active_handles.remove(handle)
        if self._keep_running:
            logger.info("Standalone %s kept running (--keep-running)", handle.name)
            return
        handle.remove()

    # ── Joiner lifecycle ────────────────────────────────────────────

    def add_node(
        self,
        shard_network: str,
        node_config: NodeConfig,
        bootstrap_handle: SubprocessNodeHandle,
        wait_running: bool = True,
    ) -> SubprocessNodeHandle:
        """Add a joiner node to an existing shard.

        ``shard_network`` is the synthetic network name returned by
        ``bootstrap_handle.network_name``; it isn't a real network here but
        the parameter is kept for protocol parity.
        """
        del shard_network  # unused for subprocess
        self._joiner_counter += 1
        role_key = f"joiner{self._joiner_counter}"
        ports = self._ports.allocate()

        bootstrap_url = self._bootstrap_url(bootstrap_handle)
        identity = node_config.identity

        cli = [
            f"--bootstrap={bootstrap_url}",
            "--allow-private-addresses",
        ]
        if identity:
            cli += [
                f"--validator-public-key={identity.public_hex}",
                f"--validator-private-key={identity.private_hex}",
            ]
        cli.extend(self._build_extra_cli(node_config))

        handle = self._spawn(
            role_key=role_key,
            role=NodeRole.JOINER,
            ports=ports,
            cli_args=cli,
            cert_subdir=None,  # joiners auto-generate
            identity=identity,
        )

        if wait_running:
            wait_for_node_running(
                get_logs=handle.logs,
                is_running=handle.is_running,
                node_name=handle.name,
                timeout=self._timeouts.node_startup,
                status_url=f"http://{handle.grpc_host}:{handle.ports.http}/api/status",
            )

        self._active_handles.append(handle)
        return handle

    def remove_node(self, handle: SubprocessNodeHandle) -> None:
        if handle in self._active_handles:
            self._active_handles.remove(handle)
        if self._keep_running:
            logger.info("Joiner %s kept running (--keep-running)", handle.name)
            return
        handle.remove()

    # ── Cleanup ─────────────────────────────────────────────────────

    def cleanup_all(self) -> None:
        """Reap all handles spawned by this provider, then remove the
        session data root. Idempotent — safe to call multiple times.
        """
        if self._keep_running:
            logger.info(
                "SubprocessProvider: --keep-running, skipping cleanup of "
                "%d handles for session %s (data at %s)",
                len(self._active_handles), self._session_id, self._session_root,
            )
            return
        # Stop all known handles first (graceful → escalate inside _stop_once).
        for h in list(self._active_handles):
            try:
                h.remove()
            finally:
                if h in self._active_handles:
                    self._active_handles.remove(h)
        # Then remove the session data root in case anything was left over
        # (e.g. handles that crashed before we registered them).
        shutil.rmtree(self._session_root, ignore_errors=True)
        logger.info(
            "SubprocessProvider: cleaned up session %s", self._session_id,
        )

    @classmethod
    def force_cleanup_all_test_resources(cls) -> None:
        """Aggressive cleanup across ALL subprocess sessions, regardless of
        owner. Used by ``shardctl test-reset`` and ``pytest_sessionstart``.

        Discovers and reaps:
          1. Every running ``node`` process whose argv contains the
             ``.subprocess-data`` token.
          2. Every session directory under ``<integration-tests>/.subprocess-data/``.
        """
        # 1. Reap orphaned processes by argv pattern.
        try:
            result = subprocess.run(
                ["pgrep", "-f", _DATA_DIR_SCAN_TOKEN],
                capture_output=True, text=True, timeout=10,
            )
            pids = [int(p) for p in (result.stdout or "").split() if p.strip().isdigit()]
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pids = []

        for pid in pids:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    break
                # Brief wait between signals.
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break  # process gone

        # 2. Remove all session data dirs.
        try:
            paths = ResourcePaths.resolve()
            base = cls._data_dir_base(paths)
            if base.exists():
                shutil.rmtree(base, ignore_errors=True)
        except FileNotFoundError:
            # Repo layout missing — nothing to clean.
            pass

    @classmethod
    def cleanup_stale_sessions(cls) -> None:
        """Conservative stale-session cleanup. Called from
        ``pytest_sessionstart`` and ``pytest_sessionfinish``.

        Mirrors Docker semantics: walk session dirs under
        ``<integration-tests>/.subprocess-data/`` and reap only those
        with NO live node processes. Sessions whose nodes are still
        running (e.g. from a concurrent ``--keep-running`` run on a
        sibling worker) are left untouched.
        """
        try:
            paths = ResourcePaths.resolve()
        except FileNotFoundError:
            return
        base = cls._data_dir_base(paths)
        if not base.is_dir():
            return

        for session_dir in base.iterdir():
            if not session_dir.is_dir():
                continue
            if cls._session_has_live_processes(session_dir):
                logger.debug(
                    "cleanup_stale_sessions: session %s has live processes; skipping",
                    session_dir.name,
                )
                continue
            shutil.rmtree(session_dir, ignore_errors=True)
            logger.info(
                "cleanup_stale_sessions: reaped dead session %s", session_dir.name,
            )

    @staticmethod
    def _session_has_live_processes(session_dir: Path) -> bool:
        """True if any subprocess is running with this session_dir in argv."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", str(session_dir)],
                capture_output=True, text=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return bool(result.stdout.strip())

    # ── Session adoption (--skip-setup --session-id) ────────────────

    def adopt_session(self, session_id: str) -> List[SubprocessNodeHandle]:
        """Find and wrap nodes from a prior --keep-running session.

        Walks ``<integration-tests>/.subprocess-data/<session_id>/`` for
        per-role data dirs, looks up each running pid by scanning argv for
        the data-dir path, and reconstructs handles. Returns canonical
        order: bootstrap, validator1..N, readonly.
        """
        session_dir = self._session_data_root(self._paths, session_id)
        if not session_dir.is_dir():
            raise ValueError(
                f"No subprocess session data at {session_dir} "
                f"for session_id={session_id!r}"
            )

        # Each subdir corresponds to a role (boot, validator1, …, readonly).
        role_dirs = sorted(
            [d for d in session_dir.iterdir() if d.is_dir() and d.name != "genesis"],
            key=lambda d: _role_sort_key(d.name),
        )

        handles: List[SubprocessNodeHandle] = []
        for role_dir in role_dirs:
            role_key = role_dir.name
            # Find the pid of the node process attached to this data dir.
            pid = _find_pid_for_data_dir(role_dir)
            if pid is None:
                logger.warning(
                    "adopt_session: no running process for %s; skipping", role_dir,
                )
                continue
            # We don't have the original Popen, ports, or identity. Build a
            # minimal handle that supports logs/is_running/stop/remove, which
            # is enough for tests that just want to query an existing shard.
            # Ports are recovered by parsing the cmdline.
            ports = _ports_from_cmdline(pid)
            if ports is None:
                logger.warning(
                    "adopt_session: couldn't recover ports for pid %d; skipping",
                    pid,
                )
                continue
            log_path = session_dir / f"{role_key}.log"
            handle = _AdoptedHandle(
                name=self._node_name(role_key),
                ports=ports,
                role=_role_from_role_key(role_key),
                data_dir=role_dir,
                log_path=log_path,
                pid=pid,
            )
            handles.append(handle)

        if not handles:
            raise ValueError(
                f"adopt_session: no live processes found for session_id={session_id!r}"
            )

        # Take over session ownership.
        self._session_id = session_id
        self._session_root = session_dir
        self._active_handles = list(handles)
        logger.info(
            "Adopted subprocess shard for session %s: %d nodes (%s)",
            session_id, len(handles), ", ".join(h.role_key for h in handles),
        )
        return handles


# ── Helpers for adopt_session ─────────────────────────────────────────


def _role_sort_key(name: str) -> tuple:
    if name == "boot":
        return (0, 0)
    if name.startswith("validator"):
        try:
            return (1, int(name[len("validator"):]))
        except ValueError:
            return (1, 0)
    if name == "readonly":
        return (2, 0)
    if name.startswith("standalone"):
        return (3, 0)
    if name.startswith("joiner"):
        return (4, 0)
    return (5, 0)


def _role_from_role_key(role_key: str) -> NodeRole:
    if role_key == "boot":
        return NodeRole.BOOTSTRAP
    if role_key.startswith("validator"):
        return NodeRole.VALIDATOR
    if role_key == "readonly":
        return NodeRole.READONLY
    if role_key.startswith("standalone"):
        return NodeRole.STANDALONE
    if role_key.startswith("joiner"):
        return NodeRole.JOINER
    return NodeRole.VALIDATOR  # fallback


def _find_pid_for_data_dir(data_dir: Path) -> Optional[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-f", str(data_dir)],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    pids = [int(p) for p in (result.stdout or "").split() if p.strip().isdigit()]
    return pids[0] if pids else None


_PORT_FLAG_RX = re.compile(
    r"--protocol-port[=\s]+(?P<protocol>\d+)|"
    r"--api-port-grpc-external[=\s]+(?P<grpc_ext>\d+)|"
    r"--api-port-grpc-internal[=\s]+(?P<grpc_int>\d+)|"
    r"--api-port-http[=\s]+(?P<http>\d+)|"
    r"--discovery-port[=\s]+(?P<discovery>\d+)|"
    r"--api-port-admin-http[=\s]+(?P<admin>\d+)"
)


def _ports_from_cmdline(pid: int) -> Optional[PortMapping]:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    cmdline = (result.stdout or "").strip()
    if not cmdline:
        return None
    found: dict = {}
    for m in _PORT_FLAG_RX.finditer(cmdline):
        for k, v in m.groupdict().items():
            if v is not None:
                found[k] = int(v)
    required = ("protocol", "grpc_ext", "grpc_int", "http", "discovery", "admin")
    if not all(k in found for k in required):
        return None
    return PortMapping(
        protocol=found["protocol"],
        grpc_ext=found["grpc_ext"],
        grpc_int=found["grpc_int"],
        http=found["http"],
        discovery=found["discovery"],
        admin=found["admin"],
    )


class _AdoptedHandle(SubprocessNodeHandle):
    """Minimal handle for an adopted (pre-existing) node.

    We don't have the original ``Popen`` or env, so ``restart`` is
    unsupported. Everything else (logs, is_running, stop, remove,
    pause/unpause) works via the recovered pid.
    """

    def __init__(
        self,
        name: str,
        ports: PortMapping,
        role: NodeRole,
        data_dir: Path,
        log_path: Path,
        pid: int,
    ) -> None:
        # Bypass parent __init__ — we don't have a Popen.
        self._name = name
        self._ports = ports
        self._role = role
        self._data_dir = data_dir
        self._log_path = log_path
        self._proc = None  # not a Popen
        self._log_fh = None
        self._spawn_args = []
        self._spawn_env = {}
        self._identity = None
        self._adopted_pid = pid

    @property
    def role_key(self) -> str:
        return self._data_dir.name

    @property
    def pid(self) -> int:
        return self._adopted_pid

    def is_running(self) -> bool:
        try:
            os.kill(self._adopted_pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # process exists, owned by another user

    def exit_code(self) -> Optional[int]:
        return None if self.is_running() else 0  # we can't observe code

    def wait_for_exit(self, timeout: int = 180) -> Optional[int]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_running():
                return 0
            time.sleep(0.5)
        return None

    def pause(self) -> None:
        if self.is_running():
            os.kill(self._adopted_pid, signal.SIGSTOP)

    def unpause(self) -> None:
        if self.is_running():
            os.kill(self._adopted_pid, signal.SIGCONT)

    def restart(self) -> None:
        raise NotImplementedError(
            "restart() unsupported for adopted handles. "
            "Run a fresh `shardctl test` invocation."
        )

    def _stop_once(self) -> None:
        if not self.is_running():
            return
        try:
            os.killpg(os.getpgid(self._adopted_pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(2)
        if self.is_running():
            try:
                os.killpg(os.getpgid(self._adopted_pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

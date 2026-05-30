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
from ..keys import BOOTSTRAP_NODE_ID
from ..ports import PortAllocator
from ..timeouts import TimeoutHierarchy
from ..types import NodeRole, PortMapping, ValidatorIdentity
from .base import (
    RetiredLogSnapshot,
    archive_handles,
    archive_root_for,
    wait_for_handles_or_archive,
)

logger = logging.getLogger(__name__)

# Bootstrap private key — same across all shards (matches certs shipped at
# integration-tests/certs/bootstrap/, which the bootstrap node uses for TLS
# so its NODE_ID is deterministic and validators can address the bootstrap
# URL without an introspection round-trip).
_BOOTSTRAP_PRIVATE_KEY = "5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"

# Per-session subprocess data lives under the integration-tests directory
# in a hidden folder. Repo-local makes inspection easy; .gitignore should
# keep it out of commits.
_DATA_DIR_BASENAME = ".subprocess-data"

# Sentinel arg used to identify framework-spawned processes during stale
# discovery (every spawned `node run` command line contains this token via
# its `--data-dir` value, but the basename is the most reliable scan key).
_DATA_DIR_SCAN_TOKEN = _DATA_DIR_BASENAME


def _parse_ps_time(value: str) -> float:
    """Parse a ``ps -o time`` value (``[[dd-]hh:]mm:ss`` or ``mm:ss.cc``) to
    seconds. Returns 0.0 on any unexpected format."""
    try:
        days = 0
        if "-" in value:
            day_str, value = value.split("-", 1)
            days = int(day_str)
        seconds = 0.0
        for p in value.split(":"):
            seconds = seconds * 60 + float(p)
        return days * 86400 + seconds
    except (ValueError, IndexError):
        return 0.0


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
        volume_name: Optional[str] = None,
        use_shard_conf: bool = False,
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
        # Recreate context: which conf was mounted, and the persistent
        # data-dir identifier (Docker-named volumes map to a stable
        # subdir in subprocess-data/).
        self._volume_name = volume_name
        self._use_shard_conf = use_shard_conf

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
    def volume_name(self) -> Optional[str]:
        """Persistent identifier for the data dir, mirroring Docker's volume_name.

        Returned by Docker's ``DockerNodeHandle.volume_name`` so that
        ``recreate_standalone`` can re-mount the same volume. On subprocess,
        we map it to a stable subdir under the session root.
        """
        return self._volume_name

    @property
    def use_shard_conf(self) -> bool:
        """Whether this handle was spawned with ``rust.conf`` (shard config)
        rather than ``standalone-dev.conf``. Recreate honours this so the
        new process keeps the same config family.
        """
        return self._use_shard_conf

    @property
    def pid(self) -> int:
        return self._proc.pid

    # ── Logs ────────────────────────────────────────────────────────

    def logs(self, tail: Optional[int] = None) -> str:
        if not self._log_path.exists():
            return ""
        text = self._log_path.read_text(errors="replace")
        # Guard against a post-exit visibility race: a fast-failing child
        # (e.g. config-validation tests where the node exits in <250ms)
        # can leave the log file empty for a brief window after wait()
        # returns. Poll for content with a bounded budget; the fast path
        # (text already populated) is unchanged.
        if not text and self._proc is not None and self._proc.poll() is not None:
            deadline = time.monotonic() + 0.5
            while not text and time.monotonic() < deadline:
                time.sleep(0.02)
                text = self._log_path.read_text(errors="replace")
        if tail is None:
            return text
        return "\n".join(text.splitlines()[-tail:])

    def archive_log(self, dest_path: Path) -> None:
        """Copy the rnode stdout/stderr log file to ``dest_path``.

        The log file lives under the session data root, which is wiped
        at teardown. Copying out before that gives the artifact upload
        a stable location to publish from.

        Always produces a file at ``dest_path`` — when the source log
        is missing or the copy raises, a diagnostic placeholder is
        written instead. This matters because
        ``actions/upload-artifact@v4`` silently drops empty
        directories: a no-op archive becomes invisible in CI, leaving
        no trace of whether the archive call ran. The placeholder
        gives the next debugger something to grep for.
        """
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if self._log_path.exists():
                shutil.copyfile(self._log_path, dest_path)
                return
            dest_path.write_text(
                "archive_log: source log file did not exist at archive time\n"
                f"  expected path: {self._log_path}\n"
                f"  handle name:   {self._name}\n"
                f"  pid:           {self._proc.pid}\n"
                f"  poll():        {self._proc.poll()}\n"
            )
        except Exception as e:
            logger.warning("SubprocessNodeHandle.archive_log: %s failed: %s", self._name, e)
            try:
                dest_path.write_text(
                    f"archive_log: exception raised: {e!r}\n"
                    f"  source path:   {self._log_path}\n"
                    f"  handle name:   {self._name}\n"
                )
            except Exception:
                pass

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
                    self._name,
                    self._proc.pid,
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
        """Returns memory_mb, cpu_percent, cpu_seconds, memory_limit_mb.

        ``cpu_percent`` from ``ps`` is a lifetime average; for an accurate
        instantaneous figure the resource monitor differences ``cpu_seconds``
        (cumulative CPU time) across samples. ``memory_limit_mb`` is None —
        subprocess nodes have no cgroup cap.
        """
        zero = {"memory_mb": 0.0, "cpu_percent": 0.0, "cpu_seconds": 0.0,
                "memory_limit_mb": None}
        if not self.is_running():
            return zero
        try:
            result = subprocess.run(
                ["ps", "-p", str(self._proc.pid), "-o", "rss=,%cpu=,time="],
                capture_output=True,
                text=True,
                timeout=5,
            )
            line = (result.stdout or "").strip()
            if not line:
                return zero
            rss_kb_str, cpu_str, time_str = line.split()
            return {
                "memory_mb": float(rss_kb_str) / 1024.0,
                "cpu_percent": float(cpu_str),
                "cpu_seconds": _parse_ps_time(time_str),
                "memory_limit_mb": None,
            }
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return zero

    def iter_log_lines(self):
        """Yield the node's log file line-by-line without loading it whole.

        Streaming counterpart to ``logs()`` for the post-test forbidden-pattern
        scan, so a multi-hundred-MB node log is never materialized in memory as
        one string plus a splitlines list.
        """
        if not self._log_path.exists():
            return
        with open(self._log_path, "r", errors="replace") as fh:
            for line in fh:
                yield line.rstrip("\n")


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
        self._retired_log_snapshots: List[RetiredLogSnapshot] = []
        self._standalone_counter = 0
        self._joiner_counter = 0
        self._shard_counter = 0

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
    def _archive_dir(self) -> Path:
        """Root directory for per-session log archival.

        Resolved once via :py:func:`archive_root_for`; tied to this
        provider's session id. Per-shard / standalone / leftover paths
        are formed by joining a subdir onto this.
        """
        return archive_root_for(self._paths.integration_tests, self._session_id)

    @property
    def active_handles(self) -> List[SubprocessNodeHandle]:
        return list(self._active_handles)

    @property
    def retired_log_snapshots(self) -> List[RetiredLogSnapshot]:
        return list(self._retired_log_snapshots)

    def clear_retired_log_snapshots(self) -> None:
        self._retired_log_snapshots.clear()

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
            str(self._binary),
            "run",
            "--config-file",
            self._paths.rust_conf,
            "--data-dir",
            str(data_dir),
            "--host",
            "127.0.0.1",
            "--protocol-port",
            str(ports.protocol),
            "--api-port-grpc-external",
            str(ports.grpc_ext),
            "--api-port-grpc-internal",
            str(ports.grpc_int),
            "--api-port-http",
            str(ports.http),
            "--api-port-admin-http",
            str(ports.admin),
            "--discovery-port",
            str(ports.discovery),
        ]
        if cert_subdir is not None:
            cmd += [
                "--tls-key-path",
                str(Path(self._paths.certs_dir) / cert_subdir / "node.key.pem"),
                "--tls-certificate-path",
                str(Path(self._paths.certs_dir) / cert_subdir / "node.certificate.pem"),
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

    def create_shard(
        self, config: ShardConfig, wait_running: bool = True
    ) -> List[SubprocessNodeHandle]:
        """Create a full shard via host processes.

        Generates genesis files, allocates ports per role, spawns boot first
        (so its TLS-derived NODE_ID matches BOOTSTRAP_NODE_ID), then
        validators and the optional readonly observer. If ``wait_running`` is
        True (default), waits for each node to reach ``isReady`` via
        ``/api/status``.
        """
        # Per-shard discriminator. session_id is worker-scoped so multiple
        # shards on the same worker share node names (boot, validator1, ...);
        # incrementing here gives each shard a unique slot in the log archive
        # so destroy_shard's archival doesn't overwrite earlier shards' logs.
        self._shard_counter += 1
        # Genesis files (bonds.txt + wallets.txt) — written to a temp dir
        # under the session root so cleanup catches them.
        tmp_genesis_dir = self._session_root / "genesis"
        tmp_genesis_dir.mkdir(parents=True, exist_ok=True)
        # Use the existing generate_genesis machinery; it writes into a
        # NamedTemporaryDirectory but registers nothing here (we own
        # cleanup via session_root). Fall back to writing inline.
        genesis_dir = self._write_genesis(config, tmp_genesis_dir)

        roles: List[str] = ["boot"] + [f"validator{i + 1}" for i in range(len(config.bonds))]
        if config.include_readonly:
            roles.append("readonly")

        port_map = {role: self._ports.allocate() for role in roles}

        def _extra_cli(node_key: str) -> List[str]:
            merged = dict(config.global_cli_options)
            merged.update(config.per_node_cli_options.get(node_key, {}))
            return [k if v == "" else f"{k}={v}" for k, v in sorted(merged.items())]

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
        boot_cli += _extra_cli("boot")

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
            v_cli += _extra_cli(role_key)

            handles.append(
                self._spawn(
                    role_key=role_key,
                    role=NodeRole.VALIDATOR,
                    ports=port_map[role_key],
                    cli_args=v_cli,
                    cert_subdir=cert_subdir,
                    identity=identity,
                )
            )

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
            ro_cli += _extra_cli("readonly")
            handles.append(
                self._spawn(
                    role_key="readonly",
                    role=NodeRole.READONLY,
                    ports=port_map["readonly"],
                    cli_args=ro_cli,
                    cert_subdir=None,  # readonly auto-generates its own cert
                )
            )

        # ── Wait for all to reach Running ──
        if wait_running:
            wait_for_handles_or_archive(
                handles,
                self._archive_dir / f"shard{self._shard_counter}",
                self._timeouts.node_startup,
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
        # Snapshot logs into the retired bucket before the handles are
        # removed; the autouse forbidden-pattern scanner reads from this
        # bucket plus `active_handles`, and the test's own `finally:
        # shard.destroy()` runs ahead of the scanner. Symmetric to the
        # per-handle snapshot in `remove_node` used by `add_joiner` /
        # `add_observer`.
        for h in handles:
            try:
                snapshot_text = h.logs()
            except Exception:
                snapshot_text = ""
            self._retired_log_snapshots.append(
                RetiredLogSnapshot(name=h.name, log_text=snapshot_text)
            )
        if self._keep_running:
            logger.info(
                "Subprocess shard for session %s kept running (--keep-running). "
                "PIDs: %s. Data: %s",
                self._session_id,
                ", ".join(str(h.pid) for h in handles),
                self._session_root,
            )
            return
        archive_handles(handles, self._archive_dir / f"shard{self._shard_counter}")
        for h in handles:
            try:
                h.remove()
            finally:
                if h in self._active_handles:
                    self._active_handles.remove(h)

    # ── Standalone ──────────────────────────────────────────────────

    def create_standalone(
        self,
        config: NodeConfig,
        wait_running: bool = True,
        volume_name: Optional[str] = None,
        use_shard_conf: bool = False,
    ) -> SubprocessNodeHandle:
        """Create a standalone node as a host subprocess.

        Args:
            config: Node configuration with CLI flags/options.
            wait_running: If True (default), wait for the node to reach
                Running state. Set False for tests that expect startup failure.
            volume_name: Stable identifier for the data dir, mirroring
                Docker's named volume. When provided, the data dir lives at
                ``<session_root>/<volume_name>/`` and is reused on
                ``recreate_standalone``. When omitted, an auto-numbered
                ``standalone<N>`` subdir is used (anonymous, single-use).
            use_shard_conf: If True, mount ``rust.conf`` (shard config) and
                shard genesis files instead of ``standalone-dev.conf``.
                Used for observers joining a standalone baseline.
        """
        self._standalone_counter += 1
        suffix = self._standalone_counter
        role_key = f"standalone{suffix}"
        # If a stable volume_name was provided, use it as the data subdir
        # so recreate_standalone can find the same directory.
        data_subdir = volume_name if volume_name else role_key
        ports = self._ports.allocate()

        extra_cli = self._build_extra_cli(config)
        cli: List[str] = [
            "-s",
            f"--validator-private-key={_BOOTSTRAP_PRIVATE_KEY}",
            "--allow-private-addresses",
        ]
        if use_shard_conf:
            # Shard-conf standalone: mount the multi-validator genesis
            # files so joiners agree on bonds/wallets.
            cli += [
                f"--bonds-file={self._paths.genesis_bonds}",
                f"--wallets-file={self._paths.genesis_wallets}",
            ]
        else:
            cli += [
                f"--bonds-file={self._paths.standalone_bonds}",
                f"--wallets-file={self._paths.standalone_wallets}",
            ]
        cli.extend(extra_cli)

        # Config-file selection mirrors DockerProvider:
        #   default → standalone-dev.conf (small validator set, instant finalization)
        #   use_shard_conf=True → rust.conf (shard config; required when joiners
        #     will connect)
        config_file = self._paths.rust_conf if use_shard_conf else self._paths.standalone_conf

        # TLS cert selection: when joiners will connect (use_shard_conf=True),
        # mount the prebaked bootstrap cert so its SAN contains
        # BOOTSTRAP_NODE_ID — joiners' TLS verification keys off that ID.
        # An auto-generated cert has a fresh node ID and joiners reject it
        # with `Hostname verification failed: ... not found in certificate
        # CN or SAN`.
        cert_subdir = "bootstrap" if use_shard_conf else None

        handle = self._spawn_with_config_override(
            role_key=role_key,
            role=NodeRole.STANDALONE,
            ports=ports,
            config_file=config_file,
            cli_args=cli,
            cert_subdir=cert_subdir,
            data_subdir=data_subdir,
            volume_name=volume_name,
            use_shard_conf=use_shard_conf,
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
        handle: SubprocessNodeHandle,
        config: NodeConfig,
        wait_running: bool = True,
    ) -> SubprocessNodeHandle:
        """Recreate a standalone with new config but the same data dir.

        Mirrors ``DockerProvider.recreate_standalone``: stops the existing
        process, then spawns a new one against the same data subdir (so
        LMDB state persists across the restart). Used for restart-drift
        tests.

        The new process inherits ``volume_name`` and ``use_shard_conf``
        from the original handle, so the conf file family is preserved.
        Logs are appended to the original log file so post-restart events
        and pre-restart context appear in one place (matching Docker's
        stdout continuation across container recreation).
        """
        ports = handle.ports
        volume_name = handle.volume_name
        use_shard_conf = handle.use_shard_conf

        # Stop the previous process; data dir on disk persists.
        if handle.is_running():
            handle.stop()
        # Snapshot the prior handle's log BEFORE we discard it. The new
        # handle shares the same log file (log_mode="a"), so its
        # destroy_standalone archive would capture both runs — but only
        # if that archive call actually runs. If the test path between
        # here and destroy_standalone fails to archive (inner exception,
        # killed worker), the prior node's history is lost. Archiving
        # here makes the prior run independently recoverable.
        archive_handles([handle], self._archive_dir)
        if handle in self._active_handles:
            self._active_handles.remove(handle)

        # Same data subdir — derive from volume_name (preferred) or fall
        # back to the existing data_dir's name when no volume_name was set.
        data_subdir = volume_name or handle.data_dir.name

        extra_cli = self._build_extra_cli(config)
        cli: List[str] = [
            "-s",
            f"--validator-private-key={_BOOTSTRAP_PRIVATE_KEY}",
            "--allow-private-addresses",
        ]
        if use_shard_conf:
            cli += [
                f"--bonds-file={self._paths.genesis_bonds}",
                f"--wallets-file={self._paths.genesis_wallets}",
            ]
        else:
            cli += [
                f"--bonds-file={self._paths.standalone_bonds}",
                f"--wallets-file={self._paths.standalone_wallets}",
            ]
        cli.extend(extra_cli)

        config_file = self._paths.rust_conf if use_shard_conf else self._paths.standalone_conf

        # See create_standalone: shard-conf standalones must use the
        # prebaked bootstrap cert so joiners can validate its SAN.
        cert_subdir = "bootstrap" if use_shard_conf else None

        # Reuse role_key for the node name so test logs / docs are stable
        # across the recreate. If the handle had a volume_name, use it as
        # the role_key marker; otherwise keep the original subdir name.
        role_key = data_subdir

        new_handle = self._spawn_with_config_override(
            role_key=role_key,
            role=NodeRole.STANDALONE,
            ports=ports,
            config_file=config_file,
            cli_args=cli,
            cert_subdir=cert_subdir,
            data_subdir=data_subdir,
            log_mode="a",  # append, don't truncate the original log
            volume_name=volume_name,
            use_shard_conf=use_shard_conf,
        )

        if wait_running:
            wait_for_handles_or_archive(
                [new_handle],
                self._archive_dir,
                self._timeouts.node_startup,
            )

        self._active_handles.append(new_handle)
        return new_handle

    def _spawn_with_config_override(
        self,
        role_key: str,
        role: NodeRole,
        ports: PortMapping,
        config_file: str,
        cli_args: List[str],
        cert_subdir: Optional[str],
        data_subdir: Optional[str] = None,
        log_mode: str = "w",
        volume_name: Optional[str] = None,
        use_shard_conf: bool = False,
    ) -> SubprocessNodeHandle:
        """Like ``_spawn`` but uses ``config_file`` instead of rust.conf.

        Args:
            data_subdir: Override the subdir name under ``session_root``.
                Used by ``recreate_standalone`` to keep the same data dir
                across restarts.
            log_mode: ``"w"`` (default) truncates; ``"a"`` appends. Use
                append on recreate so the recreated node's logs join the
                original log file (mirrors Docker's stdout continuation
                across container recreation).
            volume_name / use_shard_conf: stored on the handle for
                ``recreate_standalone`` to read.
        """
        subdir = data_subdir or role_key
        data_dir = self._session_root / subdir
        data_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._session_root / f"{subdir}.log"

        cmd: List[str] = [
            str(self._binary),
            "run",
            "--config-file",
            config_file,
            "--data-dir",
            str(data_dir),
            "--host",
            "127.0.0.1",
            "--protocol-port",
            str(ports.protocol),
            "--api-port-grpc-external",
            str(ports.grpc_ext),
            "--api-port-grpc-internal",
            str(ports.grpc_int),
            "--api-port-http",
            str(ports.http),
            "--api-port-admin-http",
            str(ports.admin),
            "--discovery-port",
            str(ports.discovery),
        ]
        if cert_subdir is not None:
            cmd += [
                "--tls-key-path",
                str(Path(self._paths.certs_dir) / cert_subdir / "node.key.pem"),
                "--tls-certificate-path",
                str(Path(self._paths.certs_dir) / cert_subdir / "node.certificate.pem"),
            ]
        cmd += cli_args

        env = os.environ.copy()
        env.setdefault("RUST_LOG", "info")
        env.setdefault("OPENAI_ENABLED", "false")

        log_fh = open(log_path, log_mode)
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
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
            volume_name=volume_name,
            use_shard_conf=use_shard_conf,
        )

    def destroy_standalone(self, handle: SubprocessNodeHandle) -> None:
        if handle in self._active_handles:
            self._active_handles.remove(handle)
        if self._keep_running:
            logger.info("Standalone %s kept running (--keep-running)", handle.name)
            return
        archive_handles([handle], self._archive_dir)
        handle.remove()

    # ── Joiner / observer lifecycle ─────────────────────────────────

    def add_node(
        self,
        shard_network: str,
        node_config: NodeConfig,
        bootstrap_handle: SubprocessNodeHandle,
        wait_running: bool = True,
    ) -> SubprocessNodeHandle:
        """Add a joiner or observer node to an existing shard.

        Role is taken from ``node_config.role``. JOINER attaches with
        validator identity (proposes); READONLY attaches without
        identity and with ``--heartbeat-disabled`` (sync-only).

        ``shard_network`` is the synthetic network name returned by
        ``bootstrap_handle.network_name``; it isn't a real network here but
        the parameter is kept for protocol parity.
        """
        del shard_network  # unused for subprocess

        role = node_config.role
        if role == NodeRole.JOINER:
            self._joiner_counter += 1
            role_key = f"joiner{self._joiner_counter}"
        elif role == NodeRole.READONLY:
            self._observer_counter = getattr(self, "_observer_counter", 0) + 1
            role_key = f"observer{self._observer_counter}"
        else:
            raise ValueError(f"add_node only supports JOINER or READONLY, got {role}")

        ports = self._ports.allocate()
        bootstrap_url = self._bootstrap_url(bootstrap_handle)
        identity = node_config.identity

        cli = [
            f"--bootstrap={bootstrap_url}",
            "--allow-private-addresses",
        ]
        if role == NodeRole.READONLY:
            cli.append("--heartbeat-disabled")
        if identity:
            cli += [
                f"--validator-public-key={identity.public_hex}",
                f"--validator-private-key={identity.private_hex}",
            ]
        cli.extend(self._build_extra_cli(node_config))

        handle = self._spawn(
            role_key=role_key,
            role=role,
            ports=ports,
            cli_args=cli,
            cert_subdir=None,  # joiners/observers auto-generate
            identity=identity,
        )

        if wait_running:
            wait_for_handles_or_archive(
                [handle],
                self._archive_dir,
                self._timeouts.node_startup,
            )

        self._active_handles.append(handle)
        return handle

    def remove_node(self, handle: SubprocessNodeHandle) -> None:
        if handle in self._active_handles:
            self._active_handles.remove(handle)
        # Snapshot the node's logs before any teardown that would
        # destroy its data dir, so the autouse log scanner still sees
        # whatever the transient node emitted (panics, forbidden
        # patterns, etc.) before its handle was detached.
        try:
            snapshot_text = handle.logs()
        except Exception:
            snapshot_text = ""
        self._retired_log_snapshots.append(
            RetiredLogSnapshot(name=handle.name, log_text=snapshot_text)
        )
        if self._keep_running:
            logger.info("Joiner %s kept running (--keep-running)", handle.name)
            return
        archive_handles([handle], self._archive_dir)
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
                len(self._active_handles),
                self._session_id,
                self._session_root,
            )
            return
        # Safety net for handles that bypassed destroy_shard/destroy_standalone
        # (e.g. tests that crashed before their finally clause ran). Archive
        # into a `leftover` subdir so it's clearly separate from the per-shard
        # archives written by the normal destroy path.
        archive_handles(list(self._active_handles), self._archive_dir / "leftover")
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
            "SubprocessProvider: cleaned up session %s",
            self._session_id,
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
                capture_output=True,
                text=True,
                timeout=10,
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
    def cleanup_session(cls, session_id: str) -> None:
        """Cleanup resources for one specific subprocess session.

        Reaps:
          1. Node processes whose argv contains the path
             ``/.subprocess-data/<session_id>/`` (leading + trailing slash
             prevent collision with sessions whose IDs share a prefix).
          2. The session data directory under
             ``<integration-tests>/.subprocess-data/<session_id>/``.

        Idempotent: safe to invoke when no resources for this session
        exist. Sessions for other IDs are not touched.
        """
        # 1. Reap processes scoped to this session by argv path match.
        pattern = f"/{_DATA_DIR_BASENAME}/{session_id}/"
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=10,
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
                time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break

        # 2. Remove the session data dir only.
        try:
            paths = ResourcePaths.resolve()
            session_dir = cls._data_dir_base(paths) / session_id
            if session_dir.exists():
                shutil.rmtree(session_dir, ignore_errors=True)
        except FileNotFoundError:
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
                "cleanup_stale_sessions: reaped dead session %s",
                session_dir.name,
            )

    @staticmethod
    def _session_has_live_processes(session_dir: Path) -> bool:
        """True if any subprocess is running with this session_dir in argv."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", str(session_dir)],
                capture_output=True,
                text=True,
                timeout=5,
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
                f"No subprocess session data at {session_dir} " f"for session_id={session_id!r}"
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
                    "adopt_session: no running process for %s; skipping",
                    role_dir,
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
            session_id,
            len(handles),
            ", ".join(h.role_key for h in handles),
        )
        return handles


# ── Helpers for adopt_session ─────────────────────────────────────────


def _role_sort_key(name: str) -> tuple:
    if name == "boot":
        return (0, 0)
    if name.startswith("validator"):
        try:
            return (1, int(name[len("validator") :]))
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
            capture_output=True,
            text=True,
            timeout=5,
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
            capture_output=True,
            text=True,
            timeout=5,
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
            "restart() unsupported for adopted handles. " "Run a fresh `shardctl test` invocation."
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

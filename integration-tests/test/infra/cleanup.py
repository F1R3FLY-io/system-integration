"""Crash-resilient resource tracker and cleanup.

Every container, volume, network, and temp file created during a test
test session is registered here. On teardown (normal or crash),
everything is cleaned up.

Defense-in-depth strategy:
  1. Normal pytest fixture teardown (yield/finally in Shard.destroy)
  2. ``atexit`` handler (fires on SIGTERM, SIGALRM from pytest-timeout)
  3. ``pytest_sessionfinish`` hook (belt-and-suspenders)
  4. ``pytest_sessionstart`` of the NEXT session (catches OOM/SIGKILL
     survivors where no Python cleanup ran)

All test resources are identified by a prefix pattern (``rnode.test.*``,
``f1r3fly-test-*``, ``test-*``) so this never touches v1 resources or
production containers.
"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import subprocess
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

# Prefixes used by test resources — scanning for these at session start
# catches zombies from crashed sessions.
_CONTAINER_PREFIX = "rnode.test."
_NETWORK_PREFIX = "f1r3fly-test-"
_VOLUME_PREFIX = "test-"


def _docker(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=60,
    )


def _safe(label: str, fn) -> None:
    """Run fn, log and swallow any exception."""
    try:
        fn()
    except Exception as e:
        logger.debug("Cleanup '%s' failed (non-fatal): %s", label, e)


class DockerCleanupRegistry:
    """Tracks and cleans up Docker resources created during a test session.

    Each resource is registered by name when created. ``cleanup_all()``
    force-removes everything registered, plus a fallback scan for any
    resources matching the session prefix.

    Thread-safe: all mutations are on sets (GIL-protected for add/discard).
    """

    def __init__(self, session_id: str, keep_running: bool = False) -> None:
        self.session_id = session_id
        self.keep_running = keep_running
        self._containers: Set[str] = set()
        self._networks: Set[str] = set()
        self._volumes: Set[str] = set()
        self._tempdirs: Set[str] = set()
        self._tempfiles: Set[str] = set()
        self._cleaned = False

        # Last line of defense: atexit fires on SIGTERM and SIGALRM
        # (pytest-timeout) but NOT on SIGKILL/OOM.
        atexit.register(self.cleanup_all)

    # ── Registration ────────────────────────────────────────────────

    def register_container(self, name: str) -> None:
        self._containers.add(name)

    def register_network(self, name: str) -> None:
        self._networks.add(name)

    def register_volume(self, name: str) -> None:
        self._volumes.add(name)

    def register_tempdir(self, path: str) -> None:
        self._tempdirs.add(path)

    def register_tempfile(self, path: str) -> None:
        self._tempfiles.add(path)

    # ── Cleanup ─────────────────────────────────────────────────────

    def cleanup_all(self) -> None:
        """Force-remove all registered resources. Idempotent.

        If ``keep_running`` is set, logs what would be cleaned but
        skips destruction. Resources persist for debugging.
        """
        if self._cleaned:
            return
        self._cleaned = True

        if self.keep_running:
            total = (
                len(self._containers) + len(self._networks) +
                len(self._volumes) + len(self._tempdirs)
            )
            if total:
                logger.info(
                    "DockerCleanupRegistry: --keep-running, skipping cleanup of "
                    "%d resources for session %s",
                    total, self.session_id,
                )
            return

        removed = []

        # 1. Containers first (must stop before removing networks/volumes)
        for name in list(self._containers):
            _safe(f"rm container {name}", lambda n=name: _docker("rm", "-f", n))
            removed.append(name)
        self._containers.clear()

        # 2. Networks
        for name in list(self._networks):
            _safe(f"rm network {name}", lambda n=name: _docker("network", "rm", n))
        self._networks.clear()

        # 3. Volumes
        for name in list(self._volumes):
            _safe(f"rm volume {name}", lambda n=name: _docker("volume", "rm", "-f", n))
        self._volumes.clear()

        # 4. Temp files and directories
        for path in list(self._tempfiles):
            _safe(f"rm tempfile {path}", lambda p=path: os.unlink(p))
        self._tempfiles.clear()

        for path in list(self._tempdirs):
            _safe(
                f"rm tempdir {path}",
                lambda p=path: shutil.rmtree(p, ignore_errors=True),
            )
        self._tempdirs.clear()

        # 5. Fallback: scan for any test resources matching this session
        self._scan_and_remove_session(self.session_id)

        if removed:
            logger.info(
                "DockerCleanupRegistry: removed %d resources for session %s",
                len(removed),
                self.session_id,
            )

    # ── Stale Session Cleanup ───────────────────────────────────────

    @classmethod
    def cleanup_stale_sessions(cls) -> None:
        """Find and remove test resources from previously crashed sessions.

        Called unconditionally at ``pytest_sessionstart``. Scans for
        containers/networks/volumes matching test prefixes and removes
        any that belong to sessions with no running containers.

        Safe to call concurrently from multiple pytest workers —
        ``docker rm -f`` on a non-existent container is a no-op.
        """
        stale_sessions = cls._find_stale_session_ids()
        for sid in stale_sessions:
            logger.warning(
                "Cleaning stale test session: %s (no running containers)", sid
            )
            cls._scan_and_remove_session(sid)

        # Also clean any orphaned resources that don't match a known session
        # (e.g., volumes whose containers were already removed by a previous
        # cleanup pass but the volumes weren't caught).
        cls._remove_orphaned_test_resources()

    @classmethod
    def _find_stale_session_ids(cls) -> Set[str]:
        """Return session IDs that have exited/dead containers but no running ones."""
        result = _docker(
            "ps", "-a",
            "--filter", f"name={_CONTAINER_PREFIX}",
            "--format", "{{.Names}}|{{.Status}}",
        )
        if result.returncode != 0 or not result.stdout.strip():
            return set()

        sessions_running: Set[str] = set()
        sessions_all: Set[str] = set()

        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 1)
            if len(parts) != 2:
                continue
            name, status = parts
            # Extract session_id from "rnode.test.{session_id}.{role}"
            name_parts = name.split(".")
            if len(name_parts) >= 4 and name_parts[1] == "test":
                sid = name_parts[2]
                sessions_all.add(sid)
                if status.lower().startswith("up"):
                    sessions_running.add(sid)

        return sessions_all - sessions_running

    @classmethod
    def _scan_and_remove_session(cls, session_id: str) -> None:
        """Remove all test resources for a specific session."""
        prefix = f"{_CONTAINER_PREFIX}{session_id}."

        # Containers
        result = _docker(
            "ps", "-aq", "--filter", f"name={prefix}",
        )
        if result.returncode == 0 and result.stdout.strip():
            for cid in result.stdout.strip().splitlines():
                _safe(f"rm stale container {cid}", lambda c=cid: _docker("rm", "-f", c))

        # Networks
        net_name = f"{_NETWORK_PREFIX}{session_id}"
        _safe(f"rm stale network {net_name}", lambda: _docker("network", "rm", net_name))

        # Volumes
        vol_prefix = f"{_VOLUME_PREFIX}{session_id}-"
        result = _docker(
            "volume", "ls", "--filter", f"name={vol_prefix}", "--format", "{{.Name}}",
        )
        if result.returncode == 0 and result.stdout.strip():
            for vol in result.stdout.strip().splitlines():
                _safe(f"rm stale volume {vol}", lambda v=vol: _docker("volume", "rm", "-f", v))

    @classmethod
    def force_cleanup_all_test_resources(cls) -> None:
        """Force-remove every container/network/volume matching the test prefixes.

        Intended for the user-invoked ``shardctl test-reset`` command. Unlike
        :meth:`cleanup_stale_sessions`, this does NOT inspect container status —
        running containers are force-stopped and removed too. Use when you want
        a clean slate regardless of what is currently up.

        Never called from pytest hooks — automatic cleanup paths must remain
        conservative to avoid clobbering concurrent test sessions.
        """
        # Containers (force-remove also stops running ones)
        result = _docker(
            "ps", "-aq", "--filter", f"name={_CONTAINER_PREFIX}",
        )
        if result.returncode == 0 and result.stdout.strip():
            for cid in result.stdout.strip().splitlines():
                _safe(f"rm container {cid}", lambda c=cid: _docker("rm", "-f", c))

        # Networks (now safe — attached containers gone)
        result = _docker(
            "network", "ls", "--filter", f"name={_NETWORK_PREFIX}", "--format", "{{.Name}}",
        )
        if result.returncode == 0 and result.stdout.strip():
            for net in result.stdout.strip().splitlines():
                _safe(f"rm network {net}", lambda n=net: _docker("network", "rm", n))

        # Volumes (now safe — using containers gone)
        result = _docker(
            "volume", "ls", "--filter", f"name={_VOLUME_PREFIX}", "--format", "{{.Name}}",
        )
        if result.returncode == 0 and result.stdout.strip():
            for vol in result.stdout.strip().splitlines():
                _safe(f"rm volume {vol}", lambda v=vol: _docker("volume", "rm", "-f", v))

    @classmethod
    def cleanup_session(cls, session_id: str) -> None:
        """Force-remove Docker resources for one specific session.

        Reaps:
          1. Containers ``rnode.test.<session_id>.*``
          2. Networks ``f1r3fly-test-<session_id>`` (and per-test
             ``f1r3fly-test-<session_id>-standalone<N>`` networks)
          3. Volumes ``test-<session_id>_*``

        Resources are filtered first by Docker's ``name=`` substring
        matcher (cheap narrowing) and then re-checked in Python with
        anchored regexes so a longer session ID that shares a prefix
        cannot collide. Idempotent — safe to invoke when no resources
        for this session exist. Other sessions are not touched.
        """
        import re
        container_re = re.compile(
            rf"^{re.escape(_CONTAINER_PREFIX)}{re.escape(session_id)}\."
        )
        network_re = re.compile(
            rf"^{re.escape(_NETWORK_PREFIX)}{re.escape(session_id)}"
            r"(-standalone\d+)?$"
        )
        volume_re = re.compile(
            rf"^{re.escape(_VOLUME_PREFIX)}{re.escape(session_id)}_"
        )

        # Containers — force-remove also stops running ones.
        result = _docker(
            "ps", "-a",
            "--filter", f"name={_CONTAINER_PREFIX}{session_id}.",
            "--format", "{{.Names}}",
        )
        if result.returncode == 0 and result.stdout.strip():
            for name in result.stdout.strip().splitlines():
                if container_re.match(name):
                    _safe(f"rm container {name}", lambda n=name: _docker("rm", "-f", n))

        # Networks (now safe — attached containers gone).
        result = _docker(
            "network", "ls",
            "--filter", f"name={_NETWORK_PREFIX}{session_id}",
            "--format", "{{.Name}}",
        )
        if result.returncode == 0 and result.stdout.strip():
            for net in result.stdout.strip().splitlines():
                if network_re.match(net):
                    _safe(f"rm network {net}", lambda n=net: _docker("network", "rm", n))

        # Volumes (now safe — using containers gone).
        result = _docker(
            "volume", "ls",
            "--filter", f"name={_VOLUME_PREFIX}{session_id}_",
            "--format", "{{.Name}}",
        )
        if result.returncode == 0 and result.stdout.strip():
            for vol in result.stdout.strip().splitlines():
                if volume_re.match(vol):
                    _safe(f"rm volume {vol}", lambda v=vol: _docker("volume", "rm", "-f", v))

    @classmethod
    def _remove_orphaned_test_resources(cls) -> None:
        """Remove any test-prefixed resources not associated with running sessions."""
        # Orphaned volumes
        result = _docker(
            "volume", "ls", "--filter", f"name={_VOLUME_PREFIX}", "--format", "{{.Name}}",
        )
        if result.returncode == 0 and result.stdout.strip():
            for vol in result.stdout.strip().splitlines():
                _safe(f"rm orphan volume {vol}", lambda v=vol: _docker("volume", "rm", "-f", v))

        # Orphaned networks
        result = _docker(
            "network", "ls", "--filter", f"name={_NETWORK_PREFIX}", "--format", "{{.Name}}",
        )
        if result.returncode == 0 and result.stdout.strip():
            for net in result.stdout.strip().splitlines():
                _safe(f"rm orphan network {net}", lambda n=net: _docker("network", "rm", n))

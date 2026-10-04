"""Port allocation for the test framework.

Allocates non-overlapping 6-port blocks from the reserved range
(41000-42999). Each block maps to a single node's port layout:
protocol, gRPC-ext, gRPC-int, HTTP, discovery, admin-HTTP.

Thread-safe via ``threading.Lock`` for pytest-xdist compatibility.
Socket-verified: each allocation checks that the base port is
actually bindable before returning it.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import List

from .types import PortMapping

logger = logging.getLogger(__name__)

_BASE = 41000
_CEILING = 49000
_BLOCK_SIZE = 6
_WORKER_RANGE = 500  # ports per xdist worker (83 nodes max per worker)


class PortAllocator:
    """Session-scoped port allocator that prevents collisions.

    When running under pytest-xdist, each worker gets a non-overlapping
    port range based on its worker ID (gw0, gw1, ...). This prevents
    port collisions across parallel workers without inter-process
    coordination.
    """

    def __init__(self, base: int = _BASE, ceiling: int = _CEILING, worker_id: str = "") -> None:
        self._lock = threading.Lock()
        if worker_id and worker_id.startswith("gw"):
            worker_num = int(worker_id[2:])
            base = _BASE + (worker_num * _WORKER_RANGE)
            ceiling = base + _WORKER_RANGE
        self._base = base
        self._next = base
        self._ceiling = min(ceiling, _CEILING)
        # Blocks released by providers when their node is removed. Reused
        # (oldest first, still bind-probed) before consuming fresh range.
        # Without reuse the allocator is strictly monotonic and a long
        # single-worker session that creates/destroys many shards exhausts
        # its whole range while nearly every earlier block is free again
        # (soak preflight 31919610258: 14 errors + 2 failures at 46% of
        # the custom suite).
        self._released: List[int] = []
        # Blocks currently handed out and not yet released. Guards reuse:
        # a just-allocated block whose node has not bound its ports yet
        # must never be handed out a second time via the free-list.
        self._in_use: set = set()

    def allocate(self) -> PortMapping:
        """Allocate a 6-port block for a single node.

        Serves released blocks first (FIFO), then fresh range. Skips a
        block if any of its 6 ports is already bound (TIME_WAIT from a
        previous test, ephemeral assignment by another process, or a
        leftover container).

        Raises RuntimeError if the entire test range is exhausted.
        """
        with self._lock:
            # Released blocks: probe each once per call; blocks still in
            # TIME_WAIT go back to the end of the queue for a later call.
            requeue: List[int] = []
            reused = None
            while self._released:
                base = self._released.pop(0)
                if self._block_free(base) is None:
                    reused = base
                    break
                requeue.append(base)
            self._released.extend(requeue)
            if reused is not None:
                self._in_use.add(reused)
                return PortMapping.from_base(reused)

            while self._next + _BLOCK_SIZE <= self._ceiling:
                base = self._next
                self._next += _BLOCK_SIZE
                busy = self._block_free(base)
                if busy is None:
                    self._in_use.add(base)
                    return PortMapping.from_base(base)
                logger.debug(
                    "Block %d-%d skipped: port %d in use", base, base + _BLOCK_SIZE - 1, busy
                )
            raise RuntimeError(
                f"test port range exhausted ({self._base}-{self._ceiling}). "
                f"Too many concurrent nodes or leftover TIME_WAIT sockets."
            )

    def release(self, mapping: PortMapping) -> None:
        """Return a block to the allocator after its node is removed.

        Safe to call more than once for the same block (subsequent calls
        are no-ops), and ignores blocks this allocator never handed out
        (e.g. handles adopted from a pre-existing container).
        """
        base = mapping.protocol
        with self._lock:
            if base not in self._in_use:
                return
            self._in_use.discard(base)
            self._released.append(base)

    def allocate_many(self, count: int) -> List[PortMapping]:
        """Allocate ``count`` port blocks."""
        return [self.allocate() for _ in range(count)]

    @classmethod
    def _block_free(cls, base: int):
        """Return the first busy port in the block, or None if all free."""
        return next(
            (p for p in range(base, base + _BLOCK_SIZE) if not cls._is_port_free(p)),
            None,
        )

    @staticmethod
    def _is_port_free(port: int) -> bool:
        """Check if a port is bindable (not in TIME_WAIT or in use).

        Deliberately does NOT set ``SO_REUSEADDR``: we want to know if a
        plain ``bind()`` will succeed — which is what rnode's gRPC
        server and Docker's host-port publishing both use. With
        ``SO_REUSEADDR`` the probe accepts ports in TIME_WAIT that the
        actual consumer would reject, letting the allocator hand out a
        port that then fails at the next layer with ``Address already
        in use``.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("0.0.0.0", port))
                return True
        except OSError:
            return False

    @staticmethod
    def wait_for_port_free(port: int, timeout: float = 60.0) -> None:
        """Block until ``port`` is no longer in TIME_WAIT."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if PortAllocator._is_port_free(port):
                return
            time.sleep(0.5)
        raise TimeoutError(f"Port {port} still in use after {timeout}s")

    @staticmethod
    def wait_for_port_listening(host: str, port: int, timeout: float = 120.0) -> None:
        """Block until a TCP connection to ``host:port`` succeeds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(2.0)
                    s.connect((host, port))
                    return
            except (OSError, ConnectionRefusedError):
                time.sleep(1.0)
        raise TimeoutError(f"{host}:{port} not listening after {timeout}s")

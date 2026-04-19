"""Resource monitoring for test sessions.

Samples memory and CPU usage across all test containers at a configurable
interval. Discovers containers dynamically by the ``rnode.test.`` prefix
so it works regardless of which tests are running or how many shards exist.

Enable via ``--monitor`` CLI flag or by adding the fixture to conftest.py.

Reports peak and average values at shutdown.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass
from typing import Dict

logger = logging.getLogger(__name__)


@dataclass
class NodeStats:
    """Accumulated stats for one container."""
    name: str
    peak_memory_mb: float = 0
    peak_cpu_percent: float = 0
    samples: int = 0
    total_memory_mb: float = 0
    total_cpu_percent: float = 0

    def record(self, memory_mb: float, cpu_percent: float) -> None:
        self.samples += 1
        self.total_memory_mb += memory_mb
        self.total_cpu_percent += cpu_percent
        if memory_mb > self.peak_memory_mb:
            self.peak_memory_mb = memory_mb
        if cpu_percent > self.peak_cpu_percent:
            self.peak_cpu_percent = cpu_percent

    @property
    def avg_memory_mb(self) -> float:
        return self.total_memory_mb / self.samples if self.samples else 0

    @property
    def avg_cpu_percent(self) -> float:
        return self.total_cpu_percent / self.samples if self.samples else 0


from .providers.docker import _parse_mem


class ResourceMonitor:
    """Samples resource usage across all ``rnode.test.*`` containers.

    Discovers containers dynamically each sample — handles containers
    starting and stopping during the test session.
    """

    def __init__(self, interval: float = 5.0) -> None:
        self._interval = interval
        self._stats: Dict[str, NodeStats] = {}
        self._stop_event = threading.Event()
        self._thread = None
        self._total_peak_memory_mb = 0.0
        self._peak_container_count = 0

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _sample_loop(self) -> None:
        while not self._stop_event.is_set():
            self._sample()
            self._stop_event.wait(timeout=self._interval)

    def _sample(self) -> None:
        """One sample: discover all rnode.test.* containers and read their stats."""
        try:
            result = subprocess.run(
                [
                    "docker", "stats", "--no-stream", "--format",
                    "{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}",
                    "--filter", "name=rnode.test.",
                ],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            return

        if result.returncode != 0:
            # --filter not supported on docker stats; fall back to grep
            try:
                result = subprocess.run(
                    [
                        "docker", "stats", "--no-stream", "--format",
                        "{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}",
                    ],
                    capture_output=True, text=True, timeout=15,
                )
            except Exception:
                return

        session_memory = 0.0
        container_count = 0

        for line in (result.stdout or "").splitlines():
            parts = line.split("|")
            if len(parts) != 3:
                continue

            name = parts[0].strip()
            if not name.startswith("rnode.test."):
                continue

            try:
                mem_parts = parts[1].split("/")
                mem_mb = _parse_mem(mem_parts[0])
                cpu = float(parts[2].strip().rstrip("%"))
            except (ValueError, IndexError):
                continue

            if name not in self._stats:
                self._stats[name] = NodeStats(name=name)
            self._stats[name].record(mem_mb, cpu)
            session_memory += mem_mb
            container_count += 1

        if session_memory > self._total_peak_memory_mb:
            self._total_peak_memory_mb = session_memory
        if container_count > self._peak_container_count:
            self._peak_container_count = container_count

    @property
    def peak_total_memory_mb(self) -> float:
        return self._total_peak_memory_mb

    @property
    def peak_container_count(self) -> int:
        return self._peak_container_count

    @property
    def node_stats(self) -> Dict[str, NodeStats]:
        return dict(self._stats)

    def report(self) -> str:
        """Return a formatted summary of resource usage."""
        if not any(s.samples for s in self._stats.values()):
            return "Resource monitor: no samples collected"

        lines = [
            "",
            "RESOURCE USAGE",
            "=" * 90,
            f"  {'Container':<45} {'Peak Mem':>10} {'Avg Mem':>10} {'Peak CPU':>10} {'Avg CPU':>10}",
            "  " + "-" * 86,
        ]

        for stats in sorted(self._stats.values(), key=lambda s: s.name):
            if stats.samples == 0:
                continue
            lines.append(
                f"  {stats.name:<45} "
                f"{stats.peak_memory_mb:>8.0f}MB "
                f"{stats.avg_memory_mb:>8.0f}MB "
                f"{stats.peak_cpu_percent:>9.1f}% "
                f"{stats.avg_cpu_percent:>9.1f}%"
            )

        lines.append("  " + "-" * 86)
        lines.append(f"  Peak total memory (all containers): {self._total_peak_memory_mb:.0f}MB")
        lines.append(f"  Peak container count: {self._peak_container_count}")
        max_samples = max((s.samples for s in self._stats.values()), default=0)
        lines.append(f"  Samples collected: {max_samples}")
        lines.append("=" * 90)

        return "\n".join(lines)

"""Resource monitoring for test sessions.

Samples memory and CPU usage across all test nodes at a configurable interval
and, when an output directory is given, writes time-series + summary files plus
a per-node Prometheus ``/metrics`` scrape for subsystem-bottleneck attribution.

Two sampling sources:

* **Provider-agnostic** (preferred) — when constructed with a ``provider``,
  samples every ``provider.active_handles`` via ``handle.resource_usage()``.
  Works for any provider (subprocess, Docker, K8s) through the ``NodeHandle``
  protocol. Also scrapes each node's admin-HTTP ``/metrics`` endpoint.
* **Docker-global fallback** — when constructed without a provider, discovers
  ``rnode.test.*`` containers via ``docker stats`` (original behavior; sees
  containers across all xdist workers).

Enable via the ``--monitor`` CLI flag. Reports peak/average at shutdown and,
with an output directory, writes:

* ``resource-timeseries.csv``      — per-node RSS/CPU + a ``__system__`` row
                                      (host free/swap) per sample
* ``node-metrics-timeseries.csv``  — per-node subsystem timers from /metrics
* ``resource-summary.txt``         — peak/avg table
"""
from __future__ import annotations

import csv
import logging
import os
import re
import signal
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .providers.docker import _parse_mem

logger = logging.getLogger(__name__)

# Prometheus metric-name substrings worth recording for bottleneck attribution.
# A scraped sample is kept if its (punctuation-normalized) metric name contains
# any of these tokens. metrics-exporter-prometheus sanitizes names by replacing
# '-' and '.' with '_', so tokens are matched against a normalized form where
# '-', '.', and '_' are all collapsed to '_' on both sides.
_METRIC_KEYWORDS = (
    "merge",
    "finaliz",
    "clique",
    "resolve_at_parents",
    "replay",
    "propose",
    "block_processing",
    "validate",
    "deploy",
    "rspace",
    "casper",
)


def _normalize_metric_name(name: str) -> str:
    return name.replace("-", "_").replace(".", "_")


@dataclass
class NodeStats:
    """Accumulated stats for one node."""

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


def _host_memory() -> Dict[str, float]:
    """Best-effort host free RAM + swap-used in MB (darwin). Empty on failure."""
    out: Dict[str, float] = {}
    try:
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        page = 4096
        free = 0
        for line in vm.splitlines():
            if line.startswith("Pages free:"):
                free = int(line.split(":")[1].strip().rstrip(".")) * page
        out["free_mb"] = free / 1e6
        sw = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True,
                            text=True, timeout=5).stdout
        # "total = X.00M  used = Y.00M  free = Z.00M"
        m = re.search(r"used\s*=\s*([0-9.]+)M", sw)
        if m:
            out["swap_used_mb"] = float(m.group(1))
    except Exception:  # noqa: BLE001 — monitoring is best-effort
        pass
    return out


class ResourceMonitor:
    """Samples resource usage across test nodes.

    With a ``provider``, samples via the provider's handles (any provider) and
    scrapes each node's ``/metrics``. Without one, falls back to global
    ``docker stats`` discovery of ``rnode.test.*`` containers.
    """

    def __init__(
        self,
        interval: float = 5.0,
        provider=None,
        output_dir: Optional[Path] = None,
        rss_ceiling_mb: Optional[float] = None,
        ceiling_consecutive: int = 2,
    ) -> None:
        self._interval = interval
        self._provider = provider
        self._output_dir = Path(output_dir) if output_dir else None
        self._stats: Dict[str, NodeStats] = {}
        self._stop_event = threading.Event()
        self._thread = None
        self._total_peak_memory_mb = 0.0
        self._peak_node_count = 0
        self._t0 = time.monotonic()
        # cpu_seconds differencing state (provider mode): name -> (cpu_s, wall)
        self._cpu_prev: Dict[str, tuple] = {}
        self._ts_writer = None
        self._ts_fh = None
        self._metrics_writer = None
        self._metrics_fh = None
        # Watchdog: if total node RSS exceeds rss_ceiling_mb for
        # ceiling_consecutive samples, kill the nodes and record a breach so the
        # run aborts before the host is driven into swap-thrash / freeze.
        self._rss_ceiling_mb = rss_ceiling_mb
        self._ceiling_consecutive = max(1, ceiling_consecutive)
        self._over_ceiling_count = 0
        self._breach: Optional[str] = None

    # ── lifecycle ──────────────────────────────────────────────────────
    def start(self) -> None:
        self._stop_event.clear()
        if self._output_dir is not None:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            self._ts_fh = open(self._output_dir / "resource-timeseries.csv", "w", newline="")
            self._ts_writer = csv.writer(self._ts_fh)
            self._ts_writer.writerow(
                ["elapsed_s", "node", "memory_mb", "cpu_percent", "memory_limit_mb"]
            )
            self._metrics_fh = open(
                self._output_dir / "node-metrics-timeseries.csv", "w", newline=""
            )
            self._metrics_writer = csv.writer(self._metrics_fh)
            self._metrics_writer.writerow(["elapsed_s", "node", "metric", "value"])
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)
        if self._output_dir is not None:
            try:
                (self._output_dir / "resource-summary.txt").write_text(self.report())
            except Exception:  # noqa: BLE001
                pass
        for fh in (self._ts_fh, self._metrics_fh):
            if fh is not None:
                try:
                    fh.close()
                except Exception:  # noqa: BLE001
                    pass

    def _sample_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._provider is not None:
                    self._sample_via_provider()
                else:
                    self._sample_via_docker()
            except Exception:  # noqa: BLE001 — never let monitoring kill the run
                logger.debug("resource sample failed", exc_info=True)
            self._stop_event.wait(timeout=self._interval)

    # ── provider-agnostic sampling ─────────────────────────────────────
    def _sample_via_provider(self) -> None:
        elapsed = time.monotonic() - self._t0
        session_memory = 0.0
        node_count = 0
        try:
            handles = list(self._provider.active_handles)
        except Exception:  # noqa: BLE001
            return
        for handle in handles:
            name = getattr(handle, "name", "?")
            try:
                usage = handle.resource_usage()
            except Exception:  # noqa: BLE001
                continue
            mem = float(usage.get("memory_mb", 0.0) or 0.0)
            cpu = self._cpu_percent(name, usage, elapsed)
            limit = usage.get("memory_limit_mb")
            self._stats.setdefault(name, NodeStats(name=name)).record(mem, cpu)
            session_memory += mem
            node_count += 1
            if self._ts_writer is not None:
                self._ts_writer.writerow(
                    [f"{elapsed:.1f}", name, f"{mem:.1f}", f"{cpu:.1f}",
                     "" if limit is None else f"{limit:.1f}"]
                )
            self._scrape_metrics(handle, name, elapsed)

        # System-wide row (page cache / swap not visible in per-proc RSS).
        host = _host_memory()
        if self._ts_writer is not None and host:
            self._ts_writer.writerow([
                f"{elapsed:.1f}", "__system__",
                f"{host.get('free_mb', 0.0):.1f}", "",
                f"{host.get('swap_used_mb', 0.0):.1f}",
            ])
        if self._ts_fh is not None:
            self._ts_fh.flush()
        if self._metrics_fh is not None:
            self._metrics_fh.flush()

        if session_memory > self._total_peak_memory_mb:
            self._total_peak_memory_mb = session_memory
        if node_count > self._peak_node_count:
            self._peak_node_count = node_count

        self._check_ceiling(session_memory, elapsed)

    def _check_ceiling(self, session_memory_mb: float, elapsed: float) -> None:
        """Abort the run if total node RSS stays above the ceiling.

        Protects the host: a stalled/looping shard can grow its working set
        unbounded and drive the machine into swap-thrash. When breached, kill
        every node immediately and record the breach; the fixture surfaces it
        as a test failure so the run ends instead of freezing the host.
        """
        if self._rss_ceiling_mb is None or self._breach is not None:
            return
        if session_memory_mb <= self._rss_ceiling_mb:
            self._over_ceiling_count = 0
            return
        self._over_ceiling_count += 1
        logger.warning(
            "ResourceMonitor: total node RSS %.0fMB > ceiling %.0fMB "
            "(%d/%d consecutive) at t=%.0fs",
            session_memory_mb, self._rss_ceiling_mb,
            self._over_ceiling_count, self._ceiling_consecutive, elapsed,
        )
        if self._over_ceiling_count < self._ceiling_consecutive:
            return
        self._breach = (
            f"Resource ceiling breached: total node RSS {session_memory_mb:.0f}MB "
            f"exceeded {self._rss_ceiling_mb:.0f}MB for {self._ceiling_consecutive} "
            f"consecutive samples (t={elapsed:.0f}s). Nodes killed to protect the "
            f"host from swap-thrash."
        )
        logger.error("%s — killing nodes now.", self._breach)
        self._kill_all_nodes()
        self._stop_event.set()

    def _kill_all_nodes(self) -> None:
        """SIGKILL every node process immediately to free RAM on a breach.

        The breach path must reclaim memory fast, so it sends SIGKILL to each
        node's process group rather than a graceful stop()/remove() (which can
        block while the host is already under memory pressure, and which also
        tears down data dirs). SIGKILL just frees the memory. Handles without an
        OS pid (e.g. container providers) fall back to stop()/remove().
        """
        if self._provider is None:
            return
        try:
            handles = list(self._provider.active_handles)
        except Exception:  # noqa: BLE001
            return
        for handle in handles:
            pid = getattr(handle, "pid", None)
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                    continue
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            for method in ("stop", "remove"):
                fn = getattr(handle, method, None)
                if fn is None:
                    continue
                try:
                    fn()
                except Exception:  # noqa: BLE001 — best-effort teardown
                    pass

    @property
    def breach(self) -> Optional[str]:
        """Non-None if the RSS ceiling was breached and nodes were killed."""
        return self._breach

    def _cpu_percent(self, name: str, usage: dict, elapsed: float) -> float:
        """Instantaneous CPU%: difference cpu_seconds across samples when the
        provider supplies it (subprocess); else use the provider's cpu_percent
        (Docker's is already instantaneous)."""
        cpu_s = usage.get("cpu_seconds")
        if cpu_s is None:
            return float(usage.get("cpu_percent", 0.0) or 0.0)
        prev = self._cpu_prev.get(name)
        self._cpu_prev[name] = (float(cpu_s), elapsed)
        if prev is None:
            return 0.0
        prev_cpu, prev_t = prev
        dt = elapsed - prev_t
        if dt <= 0:
            return 0.0
        return max(0.0, (float(cpu_s) - prev_cpu) / dt * 100.0)

    def _scrape_metrics(self, handle, name: str, elapsed: float) -> None:
        if self._metrics_writer is None:
            return
        try:
            host = handle.grpc_host
            # /metrics is served on the public HTTP API server (alongside
            # /status, /version, /api), not the admin server — see
            # node web/routes.rs::create_routes.
            port = handle.ports.http
        except Exception:  # noqa: BLE001
            return
        url = f"http://{host}:{port}/metrics"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — node may not serve metrics; skip
            return
        for line in text.splitlines():
            if not line or line[0] == "#":
                continue
            # "<metric>{labels} <value>" or "<metric> <value>"
            sp = line.rsplit(" ", 1)
            if len(sp) != 2:
                continue
            metric_name, value_str = sp
            base = _normalize_metric_name(metric_name.split("{", 1)[0])
            if not any(k in base for k in _METRIC_KEYWORDS):
                continue
            try:
                value = float(value_str)
            except ValueError:
                continue
            self._metrics_writer.writerow([f"{elapsed:.1f}", name, metric_name, value])

    # ── docker-global fallback (no provider) ───────────────────────────
    def _sample_via_docker(self) -> None:
        try:
            result = subprocess.run(
                ["docker", "stats", "--no-stream", "--format",
                 "{{.Name}}|{{.MemUsage}}|{{.CPUPerc}}"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:  # noqa: BLE001
            return
        elapsed = time.monotonic() - self._t0
        session_memory = 0.0
        node_count = 0
        for line in (result.stdout or "").splitlines():
            parts = line.split("|")
            if len(parts) != 3:
                continue
            name = parts[0].strip()
            if not name.startswith("rnode.test."):
                continue
            try:
                mem_mb = _parse_mem(parts[1].split("/")[0])
                cpu = float(parts[2].strip().rstrip("%"))
            except (ValueError, IndexError):
                continue
            self._stats.setdefault(name, NodeStats(name=name)).record(mem_mb, cpu)
            session_memory += mem_mb
            node_count += 1
            if self._ts_writer is not None:
                self._ts_writer.writerow([f"{elapsed:.1f}", name, f"{mem_mb:.1f}",
                                          f"{cpu:.1f}", ""])
        if self._ts_fh is not None:
            self._ts_fh.flush()
        if session_memory > self._total_peak_memory_mb:
            self._total_peak_memory_mb = session_memory
        if node_count > self._peak_node_count:
            self._peak_node_count = node_count
        self._check_ceiling(session_memory, elapsed)

    # ── accessors / report ─────────────────────────────────────────────
    @property
    def peak_total_memory_mb(self) -> float:
        return self._total_peak_memory_mb

    @property
    def peak_container_count(self) -> int:
        return self._peak_node_count

    @property
    def node_stats(self) -> Dict[str, NodeStats]:
        return dict(self._stats)

    def report(self) -> str:
        if not any(s.samples for s in self._stats.values()):
            return "Resource monitor: no samples collected"
        lines = [
            "",
            "RESOURCE USAGE",
            "=" * 90,
            f"  {'Node':<45} {'Peak Mem':>10} {'Avg Mem':>10} {'Peak CPU':>10} {'Avg CPU':>10}",
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
        lines.append(f"  Peak total memory (all nodes): {self._total_peak_memory_mb:.0f}MB")
        lines.append(f"  Peak node count: {self._peak_node_count}")
        max_samples = max((s.samples for s in self._stats.values()), default=0)
        lines.append(f"  Samples collected: {max_samples}")
        if self._output_dir is not None:
            lines.append(f"  Time-series: {self._output_dir / 'resource-timeseries.csv'}")
            lines.append(f"  Node metrics: {self._output_dir / 'node-metrics-timeseries.csv'}")
        lines.append("=" * 90)
        return "\n".join(lines)

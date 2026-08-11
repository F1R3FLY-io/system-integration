"""Per-core CPU sampling for the soak dashboard's node × core heatmap.

Cross-repo contract (BACKLOG-FI-003 in f1r3node-rust): the harness monitor
emits real core rows to resource-percore-timeseries.csv — a SEPARATE file,
because f1r3node-rust's run-merge-recovery-soak.sh awk aggregates sum
cpu_percent across all resource-timeseries.csv rows per timestamp, and
interleaved per-core rows there would double-count every node's CPU.

Covers the two probe modes (cgroup v1 percpu counters; cgroup v2 has no
per-CPU accounting, so a /proc thread dump attributes CPU-time deltas to
each thread's current core), the differencing math, and the monitor's CSV
emission.
"""

import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration-tests"))

from test.infra.providers.docker import (  # noqa: E402
    _parse_percore_probe,
    _percore_percent,
)
from test.infra.resource_monitor import ResourceMonitor  # noqa: E402


def _stat_line(tid: int, comm: str, utime: int, stime: int, processor: int) -> str:
    """A /proc/<pid>/task/<tid>/stat line with utime/stime at fields 14/15
    and processor at field 39 (1-indexed per proc(5))."""
    fields = ["S"] + ["0"] * 51
    fields[11] = str(utime)  # field 14
    fields[12] = str(stime)  # field 15
    fields[36] = str(processor)  # field 39
    return f"{tid} ({comm}) " + " ".join(fields)


# ── probe parsing ──────────────────────────────────────────────────────


def test_parse_percpu_counters():
    mode, cores = _parse_percore_probe("PERCPU\n1500000000 250000000\n")
    assert mode == "percpu"
    assert cores == {"0": 1.5, "1": 0.25}


def test_parse_percpu_rejects_garbage():
    assert _parse_percore_probe("PERCPU\n1500000000 bogus\n") == ("", {})
    assert _parse_percore_probe("") == ("", {})
    assert _parse_percore_probe("cat: no such file\n") == ("", {})


def test_parse_thread_dump_survives_hostile_comm():
    # comm may contain spaces and even ") " — rpartition on the LAST ") "
    # keeps the fixed-position fields intact.
    text = "THREADS\n" + _stat_line(7, "grpc) worker (1", utime=200, stime=100, processor=3)
    mode, threads = _parse_percore_probe(text)
    assert mode == "threads"
    assert threads == {"7": (3.0, "3")}  # (200+100)/CLK_TCK=100


def test_parse_thread_dump_skips_truncated_lines():
    text = "THREADS\n12 (x) S 0 0\n" + _stat_line(13, "ok", 100, 0, 1)
    mode, threads = _parse_percore_probe(text)
    assert mode == "threads"
    assert list(threads) == ["13"]


# ── differencing ───────────────────────────────────────────────────────


def test_percpu_percent_diffs_per_core_and_clamps_counter_resets():
    prev = ("percpu", {"0": 10.0, "1": 5.0})
    cur = ("percpu", {"0": 12.0, "1": 4.0})  # core 1 reset (container restart)
    pct = _percore_percent(prev, cur, dt=4.0)
    assert pct == {"0": 50.0, "1": 0.0}


def test_thread_percent_attributes_deltas_to_current_core():
    prev = ("threads", {"7": (3.0, "0"), "8": (1.0, "1")})
    # tid 7 burned 2s and migrated to core 2 (delta lands on the current
    # core); tid 8 burned 1s on core 1; tid 9 is new (no baseline — skipped).
    cur = ("threads", {"7": (5.0, "2"), "8": (2.0, "1"), "9": (9.0, "0")})
    pct = _percore_percent(prev, cur, dt=4.0)
    assert pct == {"2": 50.0, "1": 25.0}


def test_thread_percent_skips_reused_tids():
    prev = ("threads", {"7": (5.0, "0")})
    cur = ("threads", {"7": (1.0, "0")})  # cpu time went backwards: new process
    assert _percore_percent(prev, cur, dt=4.0) == {}


def test_percent_requires_matching_modes_and_elapsed_time():
    percpu = ("percpu", {"0": 1.0})
    threads = ("threads", {"7": (1.0, "0")})
    assert _percore_percent(percpu, threads, dt=4.0) == {}
    assert _percore_percent(percpu, percpu, dt=0.0) == {}


# ── monitor CSV emission ───────────────────────────────────────────────


class _Handle:
    name = "rnode.testnet.validator1"

    def resource_usage(self):
        return {"memory_mb": 100.0, "cpu_percent": 12.0}

    def per_core_cpu_percent(self):
        return {"1": 42.0, "0": 7.5}


class _PlainHandle:
    """A provider handle without per-core support (e.g. subprocess)."""

    name = "validator2"

    def resource_usage(self):
        return {"memory_mb": 50.0, "cpu_percent": 1.0}


class _Provider:
    active_handles = [_Handle(), _PlainHandle()]


def _rows(path: Path):
    with open(path, newline="") as fh:
        return list(csv.reader(fh))


def test_monitor_writes_per_core_rows_to_their_own_csv(tmp_path):
    monitor = ResourceMonitor(interval=3600, provider=_Provider(), output_dir=tmp_path)
    monitor.start()
    try:
        percore_csv = tmp_path / "resource-percore-timeseries.csv"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if percore_csv.exists() and len(_rows(percore_csv)) >= 3:
                break
            time.sleep(0.05)
    finally:
        monitor.stop()

    rows = _rows(percore_csv)
    assert rows[0] == ["elapsed_s", "node", "core", "cpu_percent"]
    by_core = {r[2]: r for r in rows[1:] if r[1] == "rnode.testnet.validator1"}
    assert by_core["0"][3] == "7.5"
    assert by_core["1"][3] == "42.0"
    # The handle without per-core support contributes no rows — and nothing
    # per-core leaks into the aggregate CSV the soak driver sums per column.
    assert not any(r[1] == "validator2" for r in rows[1:])
    aggregate_rows = _rows(tmp_path / "resource-timeseries.csv")
    assert aggregate_rows[0] == ["elapsed_s", "node", "memory_mb", "cpu_percent", "memory_limit_mb"]
    assert all(len(r) == 5 for r in aggregate_rows)

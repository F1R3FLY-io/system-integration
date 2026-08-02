"""The host-protection breach marker contract in ResourceMonitor.

`host-protection-breach.txt` in the monitor output dir is a cross-repo
contract: f1r3node-rust's run-merge-recovery-soak.sh fails the soak closed
when it appears. Present must mean THIS run breached — session dirs are
normally unique, but the --skip-setup --session-id debug loop reuses one, so
start() must clear any inherited marker. Absent stays inconclusive (a monitor
that dies with pytest writes nothing); that side is the orchestrator's job.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration-tests"))

from test.infra.resource_monitor import ResourceMonitor  # noqa: E402

MARKER = "host-protection-breach.txt"


def _monitor(tmp_path):
    monitor = ResourceMonitor(interval=3600.0, provider=None, output_dir=tmp_path)
    # The sampling thread is irrelevant here and must not shell out to docker.
    monitor._sample_loop = lambda: None
    return monitor


def test_start_clears_a_stale_marker_from_a_reused_session_dir(tmp_path):
    (tmp_path / MARKER).write_text("breach from a previous run\n")
    monitor = _monitor(tmp_path)

    monitor.start()
    try:
        assert not (tmp_path / MARKER).exists(), (
            "a prior run's marker survived start(); 'present' no longer means 'this run breached'"
        )
    finally:
        monitor.stop()


def test_stop_after_a_breach_writes_the_marker_atomically(tmp_path):
    monitor = _monitor(tmp_path)
    monitor.start()
    monitor._breach = "RSS ceiling breached: 18095MB > 14336MB"

    monitor.stop()

    assert (tmp_path / MARKER).read_text() == "RSS ceiling breached: 18095MB > 14336MB\n"
    assert not (tmp_path / "host-protection-breach.txt.tmp").exists(), (
        "temp file from the atomic rename was left behind"
    )


def test_clean_stop_leaves_no_marker(tmp_path):
    monitor = _monitor(tmp_path)
    monitor.start()

    monitor.stop()

    assert not (tmp_path / MARKER).exists()

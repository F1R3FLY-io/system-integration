"""
Heartbeat Timing Harness (issue #24)

Repeats the ``high`` phase of ``test_load`` N times on one shard, so that the
slow-high-phase signal a 60-hour soak shows about 50 times per provider can be
sampled in under 30 minutes. Two node images (for example F1+F2 against
F1+F2+H1) can then be compared in one day.

Same shard as ``test_deploy_throughput_and_finalization`` (``_load_shard_config``):
4 validators, heartbeat on, readonly included, autopropose off, 3s self-propose
cooldown. One unreported warm-up, then N repetitions. Before each repetition the
deploys finalize and every node's LFB converges, so each one starts from a
drained pool.

Every repetition prints the same lines ``test_load`` prints for a phase: the
``  Phase high: ...`` line and a ``Node internals (<validator>)`` block from
``format_node_metrics``. Every repetition uses the phase name ``high``, so the
node summarizer (f1r3node-rust ``scripts/bench/summarize-issue24-phases.sh``)
reads the output with no change. When the soak applies
``scripts/bench/extend-issue24-metrics.sh`` to ``metrics.py``, the
``ISSUE24_METRICS`` lines appear here too.

Report only. Latency thresholds belong to the maintainer and are applied to the
summarizer output, not here. The test fails only for shard health: a node that
stops, an LFB that does not advance during a repetition, or nodes that do not
converge between repetitions.

Opt-in: set HEARTBEAT_TIMING=1. HEARTBEAT_TIMING_REPS sets N (default 20).
Select the node image with ``shardctl test --image``.
"""

import logging
import os
import time

import pytest  # type: ignore[import-not-found]

from ...infra.metrics import (
    LifecycleTracker,
    compute_metric_deltas,
    format_node_metrics,
    percentiles,
    scrape_metrics,
)
from ...infra.polling import lfb_number, poll_until, wait_for_lfb_converged
from ...infra.shard import Shard
from .test_load import (
    PHASE_LINE_FORMAT,
    PHASES,
    _load_shard_config,
    _log_unfinalized,
    _run_phase,
)

pytestmark = [
    pytest.mark.xdist_group("custom"),
    pytest.mark.skipif(
        os.environ.get("HEARTBEAT_TIMING") != "1",
        reason="opt-in issue #24 timing harness; set HEARTBEAT_TIMING=1",
    ),
]

HIGH_PHASE = next(p for p in PHASES if p["name"] == "high")

DEFAULT_REPETITIONS = 20


def repetitions_from_env() -> int:
    """N from HEARTBEAT_TIMING_REPS, default 20. Rejects anything below 1."""
    raw = os.environ.get("HEARTBEAT_TIMING_REPS", "")
    if not raw.strip():
        return DEFAULT_REPETITIONS
    n = int(raw)
    if n < 1:
        raise ValueError(f"HEARTBEAT_TIMING_REPS must be at least 1, got {n}")
    return n


def _drain(shard, tracker, timeouts) -> None:
    """Finalize the tracked deploys and converge every node's LFB.

    A deploy that does not finalize is reported on the phase line, not failed.
    Convergence that times out is a shard health failure: the next repetition
    would not start from a drained pool.
    """
    tracker.wait_for_finalization(timeout=timeouts.finalization)
    wait_for_lfb_converged(
        shard.all_nodes,
        timeout=timeouts.finalization * 3,
        max_spread=5,
        description="all-node LFB spread <= 5 between repetitions",
    )


def _assert_nodes_running(shard) -> None:
    for node in shard.all_nodes:
        assert node.is_running(), f"{node.name} is not running"


def test_heartbeat_timing(provider, timeouts, resource_monitor) -> None:
    """Repeat the high phase N times and print one summarizer record per repetition."""
    repetitions = repetitions_from_env()
    shard = Shard.create(provider, _load_shard_config(), timeouts)
    try:
        v1 = shard.node("validator1")
        nodes = {name: shard.node(name) for name in ("validator1", "validator2", "validator3")}

        poll_until(
            predicate=lambda: lfb_number(v1) or None,
            timeout=timeouts.finalization,
            interval=5.0,
            description="initial LFB > 0",
        )

        tracker = LifecycleTracker(nodes, inclusion_timeout=timeouts.deploy_inclusion)
        tracker.start_lfb_monitor()
        deploy_index = 100_000
        try:
            # Warm-up: the same phase, but no "  Phase" line, so the summarizer
            # sees no record for it.
            logging.info("--- Warm-up: high ---")
            deploy_count, errors, _ = _run_phase(
                nodes, tracker, HIGH_PHASE, deploy_index, resource_monitor
            )
            deploy_index += deploy_count + len(errors)
            _drain(shard, tracker, timeouts)
            _assert_nodes_running(shard)

            for rep in range(1, repetitions + 1):
                logging.info("--- Repetition %d/%d: high ---", rep, repetitions)
                tracker.clear()
                lfb_start = lfb_number(v1)
                metrics_before = {name: scrape_metrics(node) for name, node in nodes.items()}
                rep_start = time.time()

                deploy_count, errors, submission_duration = _run_phase(
                    nodes, tracker, HIGH_PHASE, deploy_index, resource_monitor
                )
                deploy_index += deploy_count + len(errors)
                for e in errors:
                    logging.warning("  Deploy error: %s", e)
                logging.info(
                    "  Submitted %d deploys in %.1fs (%.1f/sec), %d errors",
                    deploy_count,
                    submission_duration,
                    deploy_count / max(submission_duration, 0.001),
                    len(errors),
                )

                tracker.wait_for_finalization(timeout=timeouts.finalization)
                lfb_end = lfb_number(v1)
                metrics_after = {name: scrape_metrics(node) for name, node in nodes.items()}
                rep_total = time.time() - rep_start

                results = tracker.get_results()
                inc_p50, inc_p95 = percentiles(
                    [r.inclusion_time for r in results if r.inclusion_time is not None], [50, 95]
                )
                fin_p50, fin_p95 = percentiles(
                    [r.finalization_time for r in results if r.finalization_time is not None],
                    [50, 95],
                )
                unfinalized = sum(1 for r in results if r.finalization_time is None)
                lfb_rate = (lfb_end - lfb_start) / rep_total * 60 if rep_total > 0 else 0

                logging.info(
                    PHASE_LINE_FORMAT,
                    HIGH_PHASE["name"],
                    inc_p50,
                    inc_p95,
                    fin_p50,
                    fin_p95,
                    lfb_start,
                    lfb_end,
                    lfb_rate,
                    unfinalized,
                )
                _log_unfinalized(HIGH_PHASE["name"], tracker)
                for v_name in nodes:
                    m = compute_metric_deltas(metrics_before[v_name], metrics_after[v_name])
                    if m:
                        logging.info("  Node internals (%s):\n%s", v_name, format_node_metrics(m))

                assert lfb_end > lfb_start, (
                    f"repetition {rep}: LFB did not advance (#{lfb_start} -> #{lfb_end})"
                )
                _drain(shard, tracker, timeouts)
                _assert_nodes_running(shard)
        finally:
            tracker.shutdown()
    finally:
        shard.destroy()

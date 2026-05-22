"""
Deploy Throughput and Finalization Latency Load Test

Measures deploy throughput and finalization latency under increasing load
to identify the shard's capacity limits. Runs sequential phases from low
(1 deploy/sec) to burst (32 deploys at once), reporting p50/p95/p99
finalization latency and effective throughput per phase.

Uses lightweight contracts (@N!(N)) to stress the deploy pipeline rather
than the Rholang interpreter. Deploy submission is distributed across
3 validators via ThreadPoolExecutor for rates > 1/sec.

Per-deploy latency is measured by polling find_deploy in background
threads concurrently with submission, so inclusion_time reflects actual
time from submission to block inclusion.

Results are logged as a summary table. No hard assertion on latency --
the point is to measure, not gate. Hard assertions: zero deploy failures,
all deploys finalized within timeout, no node crashes.
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

import pytest

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.metrics import (
    DeployRecord,
    LifecycleTracker,
    PhaseReport,
    compute_metric_deltas,
    format_node_metrics,
    percentiles,
    scrape_metrics,
)
from ...infra.polling import poll_until
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PHASES = [
    {"name": "low", "rate": 1, "duration": 30, "workers": 1},
    {"name": "medium", "rate": 5, "duration": 20, "workers": 3},
    {"name": "high", "rate": 10, "duration": 15, "workers": 3},
    {"name": "burst", "rate": 0, "duration": 0, "workers": 3, "burst_count": 32},
]

VABN_REFRESH_INTERVAL = 30

VALIDATORS_AND_KEYS = [
    (VALIDATOR1_ID, "validator1"),
    (VALIDATOR2_ID, "validator2"),
    (VALIDATOR3_ID, "validator3"),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _submit_deploy(node, key, index, vabn, phase):
    submit_time = time.time()
    deploy_id = node.deploy_string(
        f"@{index}!({index})",
        key,
        phlo_limit=100_000,
        phlo_price=1,
        valid_after_block_no=vabn,
    )
    return DeployRecord(
        deploy_id=deploy_id,
        submit_time=submit_time,
        validator_name=node.name,
        phase=phase,
        index=index,
    )


def _run_phase(nodes, tracker, phase, start_index):
    """Submit deploys for a phase, tracking each immediately.

    Returns (deploy_count, errors, submission_duration).
    """
    phase_name = phase["name"]
    rate = phase.get("rate", 0)
    duration = phase.get("duration", 0)
    workers = phase.get("workers", 1)
    burst_count = phase.get("burst_count", 0)

    node_list = [
        (nodes[v_name], identity.private_key()) for identity, v_name in VALIDATORS_AND_KEYS
    ]

    vabn = max(0, node_list[0][0].get_current_block_number() - 1)

    deploy_count = 0
    errors: List[str] = []

    if burst_count > 0:
        logging.info("  Burst: submitting %d deploys across %d workers", burst_count, workers)
        phase_start = time.time()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for i in range(burst_count):
                node, key = node_list[i % len(node_list)]
                idx = start_index + i
                f = executor.submit(_submit_deploy, node, key, idx, vabn, phase_name)
                futures[f] = idx
            for f in as_completed(futures):
                try:
                    rec = f.result()
                    tracker.track_deploy(rec)
                    deploy_count += 1
                except Exception as e:
                    errors.append(f"deploy {futures[f]}: {e}")
        phase_duration = time.time() - phase_start
    else:
        total_deploys = int(rate * duration)
        interval = 1.0 / rate if rate > 0 else 0
        logging.info("  Rated: %d deploys at %.1f/sec (%d workers)", total_deploys, rate, workers)
        phase_start = time.time()
        vabn_refreshed_at = phase_start

        for i in range(total_deploys):
            now = time.time()
            if now - vabn_refreshed_at > VABN_REFRESH_INTERVAL:
                vabn = max(0, node_list[0][0].get_current_block_number() - 1)
                vabn_refreshed_at = now
            node, key = node_list[i % len(node_list)]
            idx = start_index + i
            try:
                rec = _submit_deploy(node, key, idx, vabn, phase_name)
                tracker.track_deploy(rec)
                deploy_count += 1
            except Exception as e:
                errors.append(f"deploy {idx}: {e}")
            target_time = phase_start + (i + 1) * interval
            sleep_time = target_time - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)

        phase_duration = time.time() - phase_start

    return deploy_count, errors, phase_duration


def _format_report(reports):
    lines = [
        "",
        "LOAD TEST RESULTS",
        "=" * 85,
        f"{'Phase':<8} | {'Deploys':>7} | {'Rate':>6} | {'Inclusion (s)':^21} | {'Finalization (s)':^21} | {'LFB':>6}",
        f"{'':8} | {'':>7} | {'(d/s)':>6} | {'p50':>6} {'p95':>6} {'p99':>6} | {'p50':>6} {'p95':>6} {'p99':>6} | {'bl/m':>6}",
        "-" * 85,
    ]
    for r in reports:
        lines.append(
            f"{r.name:<8} | {r.deploys_submitted:>7} | {r.effective_rate:>6.1f} | "
            f"{r.inclusion_p50:>6.1f} {r.inclusion_p95:>6.1f} {r.inclusion_p99:>6.1f} | "
            f"{r.finalization_p50:>6.1f} {r.finalization_p95:>6.1f} {r.finalization_p99:>6.1f} | "
            f"{r.lfb_rate_per_min:>6.1f}"
        )
    lines.append("=" * 85)
    return "\n".join(lines)


def _get_lfb_number(node) -> int:
    try:
        return node.last_finalized_block().blockInfo.blockNumber
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_deploy_throughput_and_finalization(provider, timeouts) -> None:
    """Measure deploy throughput and finalization latency under increasing load."""
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        heartbeat=True,
        include_readonly=True,
        # Heartbeat tuning for stress-test load shape. Production defaults
        # (15s self-propose-cooldown, 0 frontier-chase-max-lag, 12s stale-
        # recovery-min-interval) are tuned for low-load stability and cap
        # propose cadence well below what high-phase deploy submission needs.
        # The values below open the propose cadence so validators can keep
        # the deploy queue drained instead of accumulating 24s+ inclusion
        # latency.
        #
        # Confirmed UNSAFE for promotion to defaults.conf (2026-05-07
        # baseline run): the higher propose cadence + frontier-chase
        # combination raises the sibling-block rate, which breaks any
        # realistic test that follows a specific blockHash through
        # finalization (test_wallets, test_web_api).
        global_cli_options={
            "--heartbeat-self-propose-cooldown": "3seconds",
            "--heartbeat-advanced-frontier-chase-max-lag": "20",
            "--heartbeat-stale-recovery-min-interval": "3seconds",
            # Larger blocks reduce the propose-per-deploy ratio and the
            # number of cross-validator races. Default 32 floods the
            # proposer at 10 d/s — at the new cooldown each validator
            # produces a block per ~3 deploys. 128 lets a single block
            # absorb a full second of submission across 3 validators.
            "--max-user-deploys-per-block": "128",
        },
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")
        v2 = shard.node("validator2")
        v3 = shard.node("validator3")

        nodes = {"validator1": v1, "validator2": v2, "validator3": v3}

        # Wait for shard to be healthy
        baseline_lfb = _get_lfb_number(v1)
        if baseline_lfb == 0:
            logging.info("Waiting for initial LFB advancement...")
            poll_until(
                predicate=lambda: _get_lfb_number(v1) if _get_lfb_number(v1) > 0 else None,
                timeout=timeouts.finalization,
                interval=5.0,
                description="initial LFB > 0",
            )
            baseline_lfb = _get_lfb_number(v1)

        logging.info("Baseline LFB: #%d", baseline_lfb)

        inclusion_timeout = timeouts.deploy_inclusion
        finalization_timeout = timeouts.finalization

        tracker = LifecycleTracker(nodes, inclusion_timeout=inclusion_timeout)
        tracker.start_lfb_monitor()

        all_reports: List[PhaseReport] = []
        total_failures = 0
        total_unfinalized = 0
        deploy_index = 100_000

        try:
            for phase in PHASES:
                phase_name = phase["name"]
                logging.info("--- Phase: %s ---", phase_name)

                tracker.clear()
                lfb_start = _get_lfb_number(v1)
                metrics_before_by_v = {name: scrape_metrics(node) for name, node in nodes.items()}
                metrics_before = metrics_before_by_v["validator1"]
                phase_time_start = time.time()

                deploy_count, errors, submission_duration = _run_phase(
                    nodes,
                    tracker,
                    phase,
                    deploy_index,
                )
                deploy_index += deploy_count + len(errors)
                total_failures += len(errors)

                if errors:
                    for e in errors:
                        logging.warning("  Deploy error: %s", e)

                logging.info(
                    "  Submitted %d deploys in %.1fs (%.1f/sec), %d errors",
                    deploy_count,
                    submission_duration,
                    deploy_count / max(submission_duration, 0.001),
                    len(errors),
                )

                tracker.wait_for_finalization(timeout=finalization_timeout)

                lfb_end = _get_lfb_number(v1)
                metrics_after_by_v = {name: scrape_metrics(node) for name, node in nodes.items()}
                metrics_after = metrics_after_by_v["validator1"]
                node_metrics = compute_metric_deltas(metrics_before, metrics_after)
                node_metrics_by_validator = {
                    name: compute_metric_deltas(metrics_before_by_v[name], metrics_after_by_v[name])
                    for name in nodes
                }
                phase_time_end = time.time()
                phase_total = phase_time_end - phase_time_start

                results = tracker.get_results()
                inclusion_times = [
                    r.inclusion_time for r in results if r.inclusion_time is not None
                ]
                finalization_times = [
                    r.finalization_time for r in results if r.finalization_time is not None
                ]
                unfinalized = sum(1 for r in results if r.finalization_time is None)
                total_unfinalized += unfinalized

                inc_p50, inc_p95, inc_p99 = percentiles(inclusion_times, [50, 95, 99])
                fin_p50, fin_p95, fin_p99 = percentiles(finalization_times, [50, 95, 99])

                lfb_advance = lfb_end - lfb_start
                lfb_rate = (lfb_advance / phase_total * 60) if phase_total > 0 else 0
                effective_rate = deploy_count / max(submission_duration, 0.001)

                report = PhaseReport(
                    name=phase_name,
                    deploys_submitted=deploy_count,
                    deploy_failures=len(errors),
                    effective_rate=effective_rate,
                    inclusion_p50=inc_p50,
                    inclusion_p95=inc_p95,
                    inclusion_p99=inc_p99,
                    finalization_p50=fin_p50,
                    finalization_p95=fin_p95,
                    finalization_p99=fin_p99,
                    unfinalized=unfinalized,
                    lfb_start=lfb_start,
                    lfb_end=lfb_end,
                    lfb_rate_per_min=lfb_rate,
                    phase_duration=phase_total,
                    node_metrics=node_metrics,
                    node_metrics_by_validator=node_metrics_by_validator,
                )
                all_reports.append(report)

                logging.info(
                    "  Phase %s: inclusion p50=%.1fs p95=%.1fs, finalization p50=%.1fs p95=%.1fs, "
                    "LFB #%d->#%d (%.1f blk/min), unfinalized=%d",
                    phase_name,
                    inc_p50,
                    inc_p95,
                    fin_p50,
                    fin_p95,
                    lfb_start,
                    lfb_end,
                    lfb_rate,
                    unfinalized,
                )
                for v_name, m in node_metrics_by_validator.items():
                    if m:
                        logging.info("  Node internals (%s):\n%s", v_name, format_node_metrics(m))

        finally:
            tracker.shutdown()

        logging.info(_format_report(all_reports))

        for report in all_reports:
            if report.node_metrics_by_validator:
                for v_name, m in report.node_metrics_by_validator.items():
                    if m:
                        logging.info(
                            "Node metrics for phase '%s' (%s):\n%s",
                            report.name,
                            v_name,
                            format_node_metrics(m),
                        )
            elif report.node_metrics:
                logging.info(
                    "Node metrics for phase '%s':\n%s",
                    report.name,
                    format_node_metrics(report.node_metrics),
                )

        # Verify readonly LFB tracked validators
        ro = shard.readonly
        if ro:
            ro_lfb = _get_lfb_number(ro)
            v1_lfb = _get_lfb_number(v1)
            lfb_gap = v1_lfb - ro_lfb
            logging.info("Readonly LFB: #%d (V1: #%d, gap: %d)", ro_lfb, v1_lfb, lfb_gap)
            assert (
                lfb_gap <= 5
            ), f"Readonly LFB #{ro_lfb} is {lfb_gap} blocks behind V1 #{v1_lfb} after load test"

        # Hard assertions. Set LOAD_TEST_TELEMETRY_ONLY=1 to skip the
        # finalization gate when collecting metrics across capacity limits.
        assert total_failures == 0, f"{total_failures} deploy(s) failed to submit"
        if os.environ.get("LOAD_TEST_TELEMETRY_ONLY"):
            logging.warning(
                "LOAD_TEST_TELEMETRY_ONLY set — skipping finalization gate "
                "(%d deploy(s) not finalized within %ds)",
                total_unfinalized,
                finalization_timeout,
            )
        else:
            assert (
                total_unfinalized == 0
            ), f"{total_unfinalized} deploy(s) not finalized within {finalization_timeout}s"
        for node in shard.all_nodes:
            assert node.is_running(), f"{node.name} is not running after load test"
    finally:
        shard.destroy()

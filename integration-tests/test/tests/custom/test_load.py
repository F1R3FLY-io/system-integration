"""
Deploy Throughput and Finalization Latency Load Test

Measures deploy throughput and finalization latency under increasing load to
identify the shard's capacity limits AND to reproduce/attribute the
finalization-lag merge runaway. Runs sequential phases from low (1 deploy/sec)
through high and a 32-burst, then a SUSTAINED continuous-load phase with no
drain — long enough to build the un-finalized cone deep, which is the only way
the runaway (cone grows -> O(cone) merge/block -> finalization can't keep pace
-> deeper cone) appears. Reports p50/p95/p99 inclusion/finalization latency,
effective throughput, and the tip-LFB cone depth per phase.

Uses lightweight contracts (@N!(N)) to stress the deploy pipeline (and thereby
block/cone count) rather than the Rholang interpreter. Submission is distributed
across 3 validators via ThreadPoolExecutor for rates > 1/sec.

Per-phase node /metrics deltas attribute block-processing cost across every
sub-stage — including the parents_post_state breakdown (floor compute / FS seal
fold / scope walk / mergeable recompute / dag merge), so the O(cone) climber is
visible. Scraped on EVERY validator (not just v1).

Telemetry-first (measure, not gate) for latency. Hard assertions: zero deploy
failures; all deploys finalized within timeout; all-node LFB convergence (every
node polled to within a spread of 5, not sampled once at drain — nodes are still
catching up the instant load stops); no node crashes. Set
LOAD_TEST_TELEMETRY_ONLY=1 to collect the runaway metrics without the
finalization/convergence gates.
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

import pytest

from ...infra.config import ShardConfig
from ...infra.keys import (
    VALIDATOR1_ID,
    VALIDATOR2_ID,
    VALIDATOR3_ID,
    VALIDATOR4_ID,
)
from ...infra.metrics import (
    DeployRecord,
    LifecycleTracker,
    PhaseReport,
    compute_metric_deltas,
    format_node_metrics,
    percentiles,
    scrape_metrics,
)
from ...infra.polling import lfb_number, poll_until, wait_for_lfb_converged
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
    # Sustained continuous load with NO drain — long enough to build the
    # un-finalized cone deep. The finalization-lag merge runaway (cone grows ->
    # O(cone) merge per block -> finalization can't keep pace -> deeper cone)
    # only appears under sustained pressure; the short phases above never reach
    # it. The parents_post_state sub-stage metrics (floor/fs_seal/scope/merge)
    # attribute the per-block cost as the cone deepens.
    {"name": "sustained", "rate": 4, "duration": 300, "workers": 3},
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


def _current_block_number(node, monitor=None) -> int:
    """``node.get_current_block_number()``, but explaining itself when it fails.

    When the host-protection watchdog trips it SIGKILLs every node, so the next
    query dies with a bare ``grpc StatusCode.UNAVAILABLE / Connection refused``.
    That traceback is what the run *presents* as, while the actual cause — the
    monitor's breach line — is thousands of log lines earlier and only reaches
    the report at fixture teardown (``conftest.py``'s ``resource_monitor``).
    Diagnosing run 30516534214 from the raw traceback took ~40 minutes; the
    breach line answers it immediately. So attribute it here, at the point of
    failure, instead of leaving the two halves to be correlated by hand.
    """
    try:
        return node.get_current_block_number()
    # Deliberately broad. A watchdog kill arrives as grpc.RpcError, but a dead
    # node also surfaces as connection resets and client-wrapper errors, and all
    # of them want the same attribution. Narrowing this to grpc.RpcError would
    # quietly restore the misdiagnosis path for the other shapes. The original
    # exception is chained, so nothing is hidden.
    except Exception as exc:
        breach = getattr(monitor, "breach", None) if monitor is not None else None
        if breach:
            raise AssertionError(
                f"Node {node.name} is unreachable because the host-protection "
                f"watchdog killed the nodes: {breach}. Raise --rss-ceiling-mb to "
                f"suit this host, or reduce the load profile — the nodes did not "
                f"crash on their own. Underlying error: {exc}"
            ) from exc
        # Distinguish "asked the monitor, it said no" from "never asked it".
        # Claiming no breach when no monitor was consulted is the same class of
        # misleading attribution this helper exists to remove.
        if monitor is None:
            diagnosis = (
                "No resource monitor was attached to this call, so a "
                "host-protection kill cannot be ruled out here"
            )
        else:
            diagnosis = (
                "The resource monitor reports no breach, so this is a node-side "
                "failure rather than a watchdog kill — check that node's logs"
            )
        raise AssertionError(
            f"Node {node.name} became unreachable while querying the last "
            f"finalized block. {diagnosis}. Underlying error: {exc}"
        ) from exc


def _run_phase(nodes, tracker, phase, start_index, monitor=None):
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

    vabn = max(0, _current_block_number(node_list[0][0], monitor) - 1)

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
                vabn = max(0, _current_block_number(node_list[0][0], monitor) - 1)
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


def _get_tip(node) -> int:
    """Highest block number the node knows (the DAG tip). ``tip - LFB`` is the
    un-finalized cone depth — the direct finalization-lag / runaway signal."""
    try:
        return node.get_current_block_number()
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_deploy_throughput_and_finalization(provider, timeouts, resource_monitor) -> None:
    """Measure deploy throughput and finalization latency under increasing load."""
    config = ShardConfig(
        # 4 genesis validators (6 nodes total with boot + readonly): enough concurrent
        # heartbeat proposers to produce sibling blocks -> multi-parent merges build a real
        # un-finalized cone, WITHOUT the 8-node simultaneous bring-up that froze the host at
        # 6 validators (the lifecycle test also has 8 nodes but staggers joiners in, so it
        # never spikes). A 3-validator shard barely forks, so it never reaches the cone cost.
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
            (VALIDATOR4_ID, 100),
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
        baseline_lfb = lfb_number(v1)
        if baseline_lfb == 0:
            logging.info("Waiting for initial LFB advancement...")
            poll_until(
                predicate=lambda: lfb_number(v1) if lfb_number(v1) > 0 else None,
                timeout=timeouts.finalization,
                interval=5.0,
                description="initial LFB > 0",
            )
            baseline_lfb = lfb_number(v1)

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
                lfb_start = lfb_number(v1)
                metrics_before_by_v = {name: scrape_metrics(node) for name, node in nodes.items()}
                metrics_before = metrics_before_by_v["validator1"]
                phase_time_start = time.time()

                deploy_count, errors, submission_duration = _run_phase(
                    nodes,
                    tracker,
                    phase,
                    deploy_index,
                    resource_monitor,
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

                # Cone depth at peak — end of submission, BEFORE the drain wait.
                # tip - LFB (both current) = un-finalized blocks. A value that climbs
                # across the rated phases (esp. the sustained phase) is the runaway;
                # one that stays bounded means finalization keeps pace.
                tip_now = _get_tip(v1)
                lfb_now = lfb_number(v1)
                peak_lag = tip_now - lfb_now
                logging.info(
                    "  Cone at end of %s submission: tip-LFB lag = %d blocks (tip #%d, LFB #%d)",
                    phase_name,
                    peak_lag,
                    tip_now,
                    lfb_now,
                )

                tracker.wait_for_finalization(timeout=finalization_timeout)

                lfb_end = lfb_number(v1)
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

        # All-node LFB convergence: after the load drains, every node — all
        # validators + readonly — should settle within a few blocks of the max
        # LFB. A persistent spread is a node failing to finalize / falling
        # behind (the runaway leaves laggards).
        #
        # Snapshot at drain first, for the record only. This is the state at the
        # instant load stopped: it is what a telemetry-only run wants, and it
        # makes a later convergence failure interpretable by showing how far
        # behind the shard started from.
        drain_lfbs = {n.name: lfb_number(n) for n in shard.all_nodes}
        drain_spread = max(drain_lfbs.values()) - min(drain_lfbs.values())
        logging.info("All-node LFBs at drain: %s (spread %d blocks)", drain_lfbs, drain_spread)

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
            assert total_unfinalized == 0, (
                f"{total_unfinalized} deploy(s) not finalized within {finalization_timeout}s"
            )
            # Wait for convergence instead of asserting the drain snapshot.
            # Nodes are legitimately still catching up the moment load stops, so
            # asserting the instantaneous spread makes this gate a race against
            # normal catch-up: it fails whenever the sample happens to land
            # mid-recovery. A single test run mostly gets away with that; a soak
            # runs this hundreds of times and will hit it. Polling asserts the
            # property actually meant — "the shard converges after load" — rather
            # than "it had already converged at the microsecond load drained".
            #
            # Budget is *3 rather than a single finalization window. Unlike the
            # sibling convergence tests there is no prior budget to restore here
            # — the old code allowed zero — so this is an estimate, chosen from
            # what the check has to cover: every deploy is already finalized by
            # this point (asserted above), so what remains is laggards, mostly
            # readonly, catching up across up to 7 nodes on a host carrying a
            # soak's accumulated load. One 45s window is a tight floor for that.
            #
            # max_spread stays 5. If this times out the shard genuinely failed to
            # converge within the budget, which is the signal the soak exists to
            # surface; widen the timeout, never the tolerance.
            converged_lfbs = wait_for_lfb_converged(
                shard.all_nodes,
                timeout=finalization_timeout * 3,
                max_spread=5,
                description="all-node LFB spread <= 5 after load drains",
            )
            logging.info(
                "All-node LFBs converged: %s (spread %d blocks)",
                converged_lfbs,
                max(converged_lfbs.values()) - min(converged_lfbs.values()),
            )
        for node in shard.all_nodes:
            assert node.is_running(), f"{node.name} is not running after load test"
    finally:
        shard.destroy()

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
time from submission to block inclusion (not submission window + polling).

Results are logged as a summary table. No hard assertion on latency —
the point is to measure, not gate. Hard assertions: zero deploy failures,
all deploys finalized within timeout, no node crashes.
"""

import dataclasses
import logging
import math
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import pytest
import requests
from docker.client import DockerClient

from .conftest import (
    assert_containers_running,
    VALIDATOR1_KEY,
    VALIDATOR2_KEY,
    VALIDATOR3_KEY,
    ALL_CONTAINERS,
)
from .rnode import Node

pytestmark = pytest.mark.xdist_group("shard")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PHASES = [
    {"name": "low",    "rate": 1,  "duration": 30, "workers": 1},
    {"name": "medium", "rate": 5,  "duration": 20, "workers": 3},
    {"name": "high",   "rate": 10, "duration": 15, "workers": 3},
    {"name": "burst",  "rate": 0,  "duration": 0,  "workers": 3, "burst_count": 32},
]

FINALIZATION_TIMEOUT = 120
INCLUSION_TIMEOUT = 90
PHASE_DRAIN_PAUSE = 5
VABN_REFRESH_INTERVAL = 30

VALIDATORS_AND_KEYS = [
    ("validator1", VALIDATOR1_KEY),
    ("validator2", VALIDATOR2_KEY),
    ("validator3", VALIDATOR3_KEY),
]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class DeployRecord:
    deploy_id: str
    submit_time: float
    validator_name: str
    phase: str
    index: int


@dataclasses.dataclass
class DeployResult:
    record: DeployRecord
    inclusion_time: Optional[float] = None
    block_number: Optional[int] = None
    finalization_time: Optional[float] = None


@dataclasses.dataclass
class PhaseReport:
    name: str
    deploys_submitted: int
    deploy_failures: int
    effective_rate: float
    inclusion_p50: float
    inclusion_p95: float
    inclusion_p99: float
    finalization_p50: float
    finalization_p95: float
    finalization_p99: float
    unfinalized: int
    lfb_start: int
    lfb_end: int
    lfb_rate_per_min: float
    phase_duration: float
    node_metrics: Optional[Dict[str, float]] = None


# Prometheus metrics to scrape — these are histogram _sum/_count pairs.
# The node uses dots in metric names; Prometheus converts to underscores.
METRICS_TO_SCRAPE = [
    "block_validation_step_checkpoint_time",
    "block_validation_step_bonds_cache_time",
    "block_validation_step_block_summary_time",
    "block_processing_stage_parents_post_state_time",
    "block_processing_stage_replay_time",
    "dag_merge_total_time",
    "dag_merge_index_time",
    "dag_merge_conflict_time",
    "dag_merge_branches_time",
    "dag_merge_conflicts_map_time",
    "dag_merge_rejection_options_time",
    "dag_merge_channel_reads_time",
    "dag_merge_combine_changes_time",
    "dag_merge_compute_trie_actions_time",
    "dag_merge_apply_trie_actions_time",
    "block_replay_phase_reset_time",
    "block_replay_phase_user_deploys_time",
    "block_replay_phase_system_deploys_time",
    "block_replay_phase_create_checkpoint_time",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentiles(values: List[float], pcts: List[float]) -> List[float]:
    if not values:
        return [0.0] * len(pcts)
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    result = []
    for p in pcts:
        idx = (p / 100.0) * (n - 1)
        lo = int(math.floor(idx))
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        result.append(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)
    return result


def _scrape_metrics(node: Node) -> Dict[str, float]:
    """Scrape Prometheus metrics from the node's /metrics endpoint.

    Returns a dict of metric_name -> value for histogram _sum and _count lines.
    """
    result = {}
    try:
        resp = requests.get(
            f"http://localhost:{node.ports.http}/metrics", timeout=10,
        )
        for line in resp.text.splitlines():
            if line.startswith("#"):
                continue
            for metric in METRICS_TO_SCRAPE:
                for suffix in ("_sum", "_count"):
                    key = metric + suffix
                    if line.startswith(key + "{") or line.startswith(key + " "):
                        match = re.search(r'\s+([\d.eE+-]+)$', line)
                        if match:
                            result[key] = float(match.group(1))
    except Exception:
        pass
    return result


def _compute_metric_deltas(
    before: Dict[str, float], after: Dict[str, float],
) -> Dict[str, float]:
    """Compute per-block average times from histogram deltas.

    For each metric, computes: (after_sum - before_sum) / (after_count - before_count)
    Returns dict of metric_name -> avg_seconds.
    """
    result = {}
    for metric in METRICS_TO_SCRAPE:
        sum_key = metric + "_sum"
        count_key = metric + "_count"
        if sum_key in after and count_key in after:
            delta_sum = after.get(sum_key, 0) - before.get(sum_key, 0)
            delta_count = after.get(count_key, 0) - before.get(count_key, 0)
            if delta_count > 0:
                result[metric] = delta_sum / delta_count
            result[metric + ".count"] = delta_count
    return result


def _format_node_metrics(metrics: Dict[str, float]) -> str:
    """Format node metrics as a readable block."""
    if not metrics:
        return "  (no node metrics available)"
    lines = []
    # Validation steps
    val_steps = [
        ("checkpoint", "block_validation_step_checkpoint_time"),
        ("bonds_cache", "block_validation_step_bonds_cache_time"),
        ("block_summary", "block_validation_step_block_summary_time"),
    ]
    lines.append("  Validation steps (avg per block):")
    for label, key in val_steps:
        avg = metrics.get(key, 0)
        count = metrics.get(key + ".count", 0)
        if count > 0:
            lines.append(f"    {label}: {avg*1000:.0f}ms ({int(count)} blocks)")
    # Checkpoint breakdown (merge vs replay)
    merge_key = "block_processing_stage_parents_post_state_time"
    replay_key = "block_processing_stage_replay_time"
    merge_avg = metrics.get(merge_key, 0)
    merge_count = metrics.get(merge_key + ".count", 0)
    replay_avg = metrics.get(replay_key, 0)
    replay_count = metrics.get(replay_key + ".count", 0)
    if merge_count > 0 or replay_count > 0:
        lines.append("  Checkpoint breakdown (avg per block):")
        if merge_count > 0:
            lines.append(f"    parents_post_state (merge): {merge_avg*1000:.0f}ms ({int(merge_count)} blocks)")
        if replay_count > 0:
            lines.append(f"    replay_block (execution): {replay_avg*1000:.0f}ms ({int(replay_count)} blocks)")
    # DAG merge breakdown
    dag_metrics = [
        ("dag_merge_total", "dag_merge_total_time"),
        ("dag_merge_index (LMDB)", "dag_merge_index_time"),
        ("dag_merge_conflict", "dag_merge_conflict_time"),
        ("  branches (depends O(D²))", "dag_merge_branches_time"),
        ("  conflicts_map (O(B²))", "dag_merge_conflicts_map_time"),
        ("  rejection_options", "dag_merge_rejection_options_time"),
        ("  channel_reads (storage)", "dag_merge_channel_reads_time"),
        ("  combine_changes", "dag_merge_combine_changes_time"),
        ("  compute_trie_actions", "dag_merge_compute_trie_actions_time"),
        ("  apply_trie_actions", "dag_merge_apply_trie_actions_time"),
    ]
    dag_has_data = any(metrics.get(k + ".count", 0) > 0 for _, k in dag_metrics)
    if dag_has_data:
        lines.append("  DAG merge breakdown (avg per merge):")
        for label, key in dag_metrics:
            avg = metrics.get(key, 0)
            count = metrics.get(key + ".count", 0)
            if count > 0:
                lines.append(f"    {label}: {avg*1000:.0f}ms ({int(count)} merges)")
    # Replay phases
    replay_phases = [
        ("reset", "block_replay_phase_reset_time"),
        ("user_deploys", "block_replay_phase_user_deploys_time"),
        ("system_deploys", "block_replay_phase_system_deploys_time"),
        ("checkpoint", "block_replay_phase_create_checkpoint_time"),
    ]
    lines.append("  Replay phases (avg per block):")
    for label, key in replay_phases:
        avg = metrics.get(key, 0)
        count = metrics.get(key + ".count", 0)
        if count > 0:
            lines.append(f"    {label}: {avg*1000:.0f}ms ({int(count)} blocks)")
    return "\n".join(lines)


def _get_lfb_number(node: Node) -> int:
    try:
        return node.last_finalized_block().blockInfo.blockNumber
    except Exception:
        return 0


class LifecycleTracker:
    """Tracks deploy inclusion and finalization in background threads.

    For each deploy, a background thread polls find_deploy until the deploy
    appears in a block. A separate background thread polls last_finalized_block
    continuously and marks deploys as finalized when LFB passes their block.
    """

    def __init__(self, nodes: Dict[str, Node]):
        self._nodes = nodes
        self._node_list = list(nodes.values())
        self._lock = threading.Lock()
        self._records: Dict[str, DeployRecord] = {}
        self._inclusion: Dict[str, Tuple[int, float]] = {}  # deploy_id -> (block_number, find_time)
        self._finalization: Dict[str, float] = {}  # deploy_id -> finalization_time
        self._max_block = 0
        self._executor = ThreadPoolExecutor(max_workers=6)
        self._lfb_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start_lfb_monitor(self):
        self._stop_event.clear()
        self._lfb_thread = threading.Thread(target=self._lfb_poll_loop, daemon=True)
        self._lfb_thread.start()

    def stop_lfb_monitor(self):
        self._stop_event.set()
        if self._lfb_thread:
            self._lfb_thread.join(timeout=10)

    def _lfb_poll_loop(self):
        node = self._node_list[0]
        while not self._stop_event.is_set():
            try:
                lfb = node.last_finalized_block().blockInfo.blockNumber
                now = time.time()
                with self._lock:
                    for deploy_id, (bn, _) in self._inclusion.items():
                        if bn > 0 and bn <= lfb and deploy_id not in self._finalization:
                            self._finalization[deploy_id] = now
            except Exception:
                pass
            self._stop_event.wait(timeout=1)

    def track_deploy(self, record: DeployRecord):
        with self._lock:
            self._records[record.deploy_id] = record
        self._executor.submit(self._poll_inclusion, record)

    def _poll_inclusion(self, record: DeployRecord):
        node = self._node_list[0]
        deadline = time.time() + INCLUSION_TIMEOUT
        while time.time() < deadline:
            try:
                light_block = node.find_deploy(record.deploy_id)
                find_time = time.time()
                with self._lock:
                    self._inclusion[record.deploy_id] = (light_block.blockNumber, find_time)
                    self._max_block = max(self._max_block, light_block.blockNumber)
                return
            except Exception:
                time.sleep(1)

    def wait_for_finalization(self, timeout: float):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                all_included = all(
                    did in self._inclusion for did in self._records
                )
                if not all_included:
                    pass
                else:
                    all_finalized = all(
                        did in self._finalization for did in self._records
                    )
                    if all_finalized:
                        return
            time.sleep(1)

    def get_results(self) -> List[DeployResult]:
        results = []
        with self._lock:
            for deploy_id, record in self._records.items():
                inc = self._inclusion.get(deploy_id)
                inclusion_time = (inc[1] - record.submit_time) if inc else None
                block_number = inc[0] if inc else None
                fin_time = self._finalization.get(deploy_id)
                finalization_time = (fin_time - record.submit_time) if fin_time else None
                results.append(DeployResult(
                    record=record,
                    inclusion_time=inclusion_time,
                    block_number=block_number,
                    finalization_time=finalization_time,
                ))
        return results

    def clear(self):
        with self._lock:
            self._records.clear()
            self._inclusion.clear()
            self._finalization.clear()
            self._max_block = 0

    def shutdown(self):
        self.stop_lfb_monitor()
        self._executor.shutdown(wait=False)


def _submit_deploy(node: Node, key, index: int, vabn: int, phase: str) -> DeployRecord:
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


def _run_phase(
    nodes: Dict[str, Node],
    tracker: LifecycleTracker,
    phase: dict,
    start_index: int,
) -> Tuple[int, List[str], float]:
    """Submit deploys for a phase, tracking each immediately.

    Returns (deploy_count, errors, submission_duration).
    """
    phase_name = phase["name"]
    rate = phase.get("rate", 0)
    duration = phase.get("duration", 0)
    workers = phase.get("workers", 1)
    burst_count = phase.get("burst_count", 0)

    validator_list = list(VALIDATORS_AND_KEYS)
    node_list = [(nodes[v_name], key) for v_name, key in validator_list]

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


def _format_report(reports: List[PhaseReport]) -> str:
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


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.timeout(900)
def test_deploy_throughput_and_finalization(
    docker_client: DockerClient,
    validator1_node: Node,
    validator2_node: Node,
    validator3_node: Node,
) -> None:
    """Measure deploy throughput and finalization latency under increasing load."""
    assert_containers_running(docker_client, ALL_CONTAINERS)

    nodes = {
        "validator1": validator1_node,
        "validator2": validator2_node,
        "validator3": validator3_node,
    }

    # Wait for shard to be healthy
    baseline_lfb = _get_lfb_number(validator1_node)
    if baseline_lfb == 0:
        logging.info("Waiting for initial LFB advancement...")
        deadline = time.time() + 60
        while time.time() < deadline and baseline_lfb == 0:
            time.sleep(5)
            baseline_lfb = _get_lfb_number(validator1_node)
        assert baseline_lfb > 0, "Shard did not finalize any blocks within 60s"

    logging.info("Baseline LFB: #%d", baseline_lfb)

    tracker = LifecycleTracker(nodes)
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
            lfb_start = _get_lfb_number(validator1_node)
            metrics_before = _scrape_metrics(validator1_node)
            phase_time_start = time.time()

            deploy_count, errors, submission_duration = _run_phase(
                nodes, tracker, phase, deploy_index,
            )
            deploy_index += deploy_count + len(errors)
            total_failures += len(errors)

            if errors:
                for e in errors:
                    logging.warning("  Deploy error: %s", e)

            logging.info(
                "  Submitted %d deploys in %.1fs (%.1f/sec), %d errors",
                deploy_count, submission_duration,
                deploy_count / max(submission_duration, 0.001),
                len(errors),
            )

            # Wait for all deploys to finalize
            tracker.wait_for_finalization(timeout=FINALIZATION_TIMEOUT)

            lfb_end = _get_lfb_number(validator1_node)
            metrics_after = _scrape_metrics(validator1_node)
            node_metrics = _compute_metric_deltas(metrics_before, metrics_after)
            phase_time_end = time.time()
            phase_total = phase_time_end - phase_time_start

            results = tracker.get_results()

            inclusion_times = [r.inclusion_time for r in results if r.inclusion_time is not None]
            finalization_times = [r.finalization_time for r in results if r.finalization_time is not None]
            unfinalized = sum(1 for r in results if r.finalization_time is None)
            total_unfinalized += unfinalized

            inc_p50, inc_p95, inc_p99 = _percentiles(inclusion_times, [50, 95, 99])
            fin_p50, fin_p95, fin_p99 = _percentiles(finalization_times, [50, 95, 99])

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
            )
            all_reports.append(report)

            logging.info(
                "  Phase %s: inclusion p50=%.1fs p95=%.1fs, finalization p50=%.1fs p95=%.1fs, "
                "LFB #%d->#%d (%.1f blk/min), unfinalized=%d",
                phase_name, inc_p50, inc_p95, fin_p50, fin_p95,
                lfb_start, lfb_end, lfb_rate, unfinalized,
            )
            if node_metrics:
                logging.info("  Node internals (V1):\n%s", _format_node_metrics(node_metrics))

    finally:
        tracker.shutdown()

    logging.info(_format_report(all_reports))

    # Log node internal metrics for each phase
    for report in all_reports:
        if report.node_metrics:
            logging.info("Node metrics for phase '%s':\n%s",
                         report.name, _format_node_metrics(report.node_metrics))

    assert total_failures == 0, f"{total_failures} deploy(s) failed to submit"
    assert total_unfinalized == 0, (
        f"{total_unfinalized} deploy(s) not finalized within {FINALIZATION_TIMEOUT}s"
    )
    for node in nodes.values():
        node.check_alive()

"""Performance metrics and deploy lifecycle tracking.

Provides Prometheus metrics scraping, percentile calculations, and
background deploy inclusion/finalization tracking for load and
degradation tests.
"""
from __future__ import annotations

import dataclasses
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

# Histogram _sum/_count pairs to scrape from /metrics.
# Labeled metrics (e.g. different phase values) are summed together.
METRICS_TO_SCRAPE = [
    "block_validation_step_checkpoint_time",
    "block_validation_step_bonds_cache_time",
    "block_validation_step_block_summary_time",
    "block_processing_stage_parents_post_state_time",
    # parents-post-state sub-stages — attribute the multi-parent merge cost
    # (floor compute / FS seal fold / scope build / mergeable recompute / dag merge).
    "block_processing_stage_parents_post_state_floor_compute_time",
    "block_processing_stage_parents_post_state_fs_seal_time",
    "block_processing_stage_parents_post_state_scope_build_time",
    "block_processing_stage_parents_post_state_ensure_mergeable_time",
    "block_processing_stage_parents_post_state_merge_time",
    "block_processing_stage_replay_time",
    "dag_merge_total_time",
    "dag_merge_index_time",
    "dag_merge_conflict_time",
    # dag_merger::merge pre-conflict phases — full attribution of the merge total
    # (index_build = per-scope-block DeployChainIndex build, the dominant fat-block cost).
    "dag_merge_actual_blocks_time",
    "dag_merge_index_build_time",
    "dag_merge_dedup_time",
    "dag_merge_branches_time",
    "dag_merge_conflicts_map_time",
    "dag_merge_rejection_options_time",
    "dag_merge_channel_reads_time",
    "dag_merge_combine_changes_time",
    "dag_merge_compute_trie_actions_time",
    "dag_merge_apply_trie_actions_time",
    # dag_merger::merge rejection-expansion path
    "dag_merge_rejection_expansion_time",
    "dag_merge_rejection_expansion_fired",
    "block_replay_phase_reset_time",
    "block_replay_phase_user_deploys_time",
    "block_replay_phase_system_deploys_time",
    "block_replay_phase_create_checkpoint_time",
    # Per-deploy replay breakdown
    "block_replay_deploy_rig_time",
    "block_replay_deploy_precharge_time",
    "block_replay_deploy_evaluate_time",
    "block_replay_deploy_refund_time",
    "block_replay_deploy_discard_event_log_time",
    "block_replay_deploy_check_replay_data_time",
    # System-deploy evaluation breakdown — what happens inside a precharge
    # or refund call (replay_system_deploy_internal -> eval_system_deploy ->
    # evaluate_system_source / consume_system_result).
    "block_replay_sysdeploy_eval_time",
    "block_replay_sysdeploy_check_time",
    "block_replay_sysdeploy_rig_time",
    "block_replay_sysdeploy_checkpoint_mergeable_time",
    "block_replay_sysdeploy_eval_evaluate_source_time",
    "block_replay_sysdeploy_eval_consume_result_time",
    # inj_attempt phases — the interior of every Rholang `evaluate` call:
    # set initial cost, charge parsing cost, build normalized term (parse),
    # reduce term (run AST through RSpace).
    "inj_attempt_set_initial_cost_time",
    "inj_attempt_charge_parsing_cost_time",
    "inj_attempt_build_normalized_term_time",
    "inj_attempt_reduce_term_time",
    # play_exploratory_par sub-step split (compute_bonds + active_validators).
    "bonds_cache_reset_time",
    "bonds_cache_inj_time",
    "bonds_cache_get_data_time",
    # Validate::block_summary sub-step breakdown.
    "block_validation_block_hash_time",
    "block_validation_timestamp_time",
    "block_validation_shard_identifier_time",
    "block_validation_deploys_shard_identifier_time",
    "block_validation_repeat_deploy_time",
    "block_validation_block_number_time",
    "block_validation_future_transaction_time",
    "block_validation_transaction_expiration_time",
    "block_validation_time_based_expiration_time",
    "block_validation_justification_follows_time",
    "block_validation_parents_time",
    "block_validation_sequence_number_time",
    "block_validation_justification_regressions_time",
    # Block creator (proposer side) phase breakdown.
    "block_creator_prepare_user_deploys_time",
    "block_creator_compute_parents_post_state_time",
    "block_creator_compute_deploys_checkpoint_time",
    "block_creator_package_block_time",
    "block_creator_total_time",
    # Finalization pipeline.
    "finalizer_run_time",
    "clique_oracle_compute_time",
    # compute_rejected_buffer_admits (called from compute_parents_post_state).
    "compute_rejected_buffer_admits_time",
    # Counter: compute_parents_post_state falling back to a single parent
    # because the visible-blocks set or LCA distance exceeded its caps.
    "compute_parents_post_state_fallback_merge_scope_too_large_fired",
    # DAG insert.
    "dag_insert_time",
    # Counter: is_mergeable_channel calls (every channel produce/consume).
    "is_mergeable_channel_calls",
    # Runtime spawn timing
    "runtime_spawn_time",
    "runtime_spawn_replay_time",
    # RSpace operation timing
    "comm_consume_time_seconds",
    "comm_produce_time_seconds",
    "install_time_seconds",
    "replay_consume_time_seconds",
    "replay_produce_time_seconds",
]


def percentiles(values: List[float], pcts: List[float]) -> List[float]:
    """Compute percentiles from a list of values.

    Args:
        values: Raw measurements.
        pcts: Percentile levels (e.g. [50, 95, 99]).

    Returns:
        List of percentile values, same length as pcts.
    """
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


def scrape_metrics(node) -> Dict[str, float]:
    """Scrape Prometheus metrics from a node's /metrics endpoint.

    Returns a dict of metric_name -> value for histogram _sum and _count
    lines. Labeled metrics are summed together.
    """
    result: Dict[str, float] = {}
    try:
        resp = node.http_get("/metrics", timeout=10)
        for line in resp.text.splitlines():
            if line.startswith("#"):
                continue
            for metric in METRICS_TO_SCRAPE:
                for suffix in ("_sum", "_count"):
                    key = metric + suffix
                    if line.startswith(key + "{") or line.startswith(key + " "):
                        match = re.search(r"\s+([\d.eE+-]+)$", line)
                        if match:
                            val = float(match.group(1))
                            result[key] = result.get(key, 0) + val
    except Exception:
        pass
    return result


def compute_metric_deltas(
    before: Dict[str, float],
    after: Dict[str, float],
) -> Dict[str, float]:
    """Compute per-block average times from histogram deltas.

    For each metric: (after_sum - before_sum) / (after_count - before_count).
    Returns dict of metric_name -> avg_seconds, plus metric_name.count -> delta_count.
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


def format_node_metrics(metrics: Dict[str, float]) -> str:
    """Format node metrics as a readable block for logging."""
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
    # bonds_cache sub-step breakdown
    bonds_sub = [
        ("reset (load hot store)", "bonds_cache_reset_time"),
        ("inj (Rholang query)", "bonds_cache_inj_time"),
        ("get_data (collect)", "bonds_cache_get_data_time"),
    ]
    bonds_sub_has_data = any(metrics.get(k + ".count", 0) > 0 for _, k in bonds_sub)
    if bonds_sub_has_data:
        lines.append("  bonds_cache breakdown (avg per call):")
        for label, key in bonds_sub:
            avg = metrics.get(key, 0)
            count = metrics.get(key + ".count", 0)
            if count > 0:
                lines.append(f"    {label}: {avg*1000:.1f}ms ({int(count)} calls)")
    # block_summary sub-step breakdown
    summary_steps = [
        ("block_hash", "block_validation_block_hash_time"),
        ("timestamp", "block_validation_timestamp_time"),
        ("shard_identifier", "block_validation_shard_identifier_time"),
        ("deploys_shard_identifier", "block_validation_deploys_shard_identifier_time"),
        ("repeat_deploy", "block_validation_repeat_deploy_time"),
        ("block_number", "block_validation_block_number_time"),
        ("future_transaction", "block_validation_future_transaction_time"),
        ("transaction_expiration", "block_validation_transaction_expiration_time"),
        ("time_based_expiration", "block_validation_time_based_expiration_time"),
        ("justification_follows", "block_validation_justification_follows_time"),
        ("parents", "block_validation_parents_time"),
        ("sequence_number", "block_validation_sequence_number_time"),
        ("justification_regressions", "block_validation_justification_regressions_time"),
    ]
    summary_has_data = any(metrics.get(k + ".count", 0) > 0 for _, k in summary_steps)
    if summary_has_data:
        lines.append("  block_summary sub-steps (avg per call):")
        for label, key in summary_steps:
            avg = metrics.get(key, 0)
            count = metrics.get(key + ".count", 0)
            if count > 0:
                lines.append(f"    {label}: {avg*1000:.2f}ms ({int(count)} calls)")
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
            lines.append(
                f"    parents_post_state (merge): {merge_avg*1000:.0f}ms ({int(merge_count)} blocks)"
            )
        if replay_count > 0:
            lines.append(
                f"    replay_block (execution): {replay_avg*1000:.0f}ms ({int(replay_count)} blocks)"
            )
    # parents_post_state sub-stage breakdown — where the multi-parent merge cost goes
    # (the ~1.8s/multi-parent that used to be lumped under parents_post_state).
    pps_substages = [
        ("floor_compute (clique floor)", "block_processing_stage_parents_post_state_floor_compute_time"),
        ("fs_seal (FS fold)", "block_processing_stage_parents_post_state_fs_seal_time"),
        ("scope_build (cone walk)", "block_processing_stage_parents_post_state_scope_build_time"),
        ("ensure_mergeable (recompute)", "block_processing_stage_parents_post_state_ensure_mergeable_time"),
        ("dag merge", "block_processing_stage_parents_post_state_merge_time"),
    ]
    pps_has_data = any(metrics.get(k + ".count", 0) > 0 for _, k in pps_substages)
    if pps_has_data:
        lines.append("  parents_post_state sub-stages (avg per multi-parent block):")
        for label, key in pps_substages:
            avg = metrics.get(key, 0)
            count = metrics.get(key + ".count", 0)
            if count > 0:
                lines.append(f"    {label}: {avg*1000:.0f}ms ({int(count)} blocks)")
    # DAG merge breakdown
    dag_metrics = [
        ("dag_merge_total", "dag_merge_total_time"),
        ("  actual_blocks (scope scan)", "dag_merge_actual_blocks_time"),
        ("  index_build (DeployChainIndex)", "dag_merge_index_build_time"),
        ("  dedup (freshest-source)", "dag_merge_dedup_time"),
        ("dag_merge_index (LMDB)", "dag_merge_index_time"),
        ("dag_merge_conflict", "dag_merge_conflict_time"),
        ("  branches (depends O(D\u00b2))", "dag_merge_branches_time"),
        ("  conflicts_map (O(B\u00b2))", "dag_merge_conflicts_map_time"),
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
    # dag_merger rejection-expansion path: called every merge, fires only
    # when there are rejected source blocks with descendants in scope.
    rej_exp_count = metrics.get("dag_merge_rejection_expansion_time.count", 0)
    rej_exp_fired = metrics.get("dag_merge_rejection_expansion_fired.count", 0)
    if rej_exp_count > 0:
        rej_exp_avg = metrics.get("dag_merge_rejection_expansion_time", 0)
        lines.append(
            f"    rejection_expansion: {rej_exp_avg*1000:.2f}ms avg, "
            f"called {int(rej_exp_count)}× ({int(rej_exp_fired)} fired)"
        )
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
    # Per-deploy replay breakdown
    deploy_breakdown = [
        ("rig", "block_replay_deploy_rig_time"),
        ("precharge", "block_replay_deploy_precharge_time"),
        ("evaluate (Rholang)", "block_replay_deploy_evaluate_time"),
        ("refund", "block_replay_deploy_refund_time"),
        ("discard_event_log", "block_replay_deploy_discard_event_log_time"),
        ("check_replay_data", "block_replay_deploy_check_replay_data_time"),
    ]
    deploy_has_data = any(metrics.get(k + ".count", 0) > 0 for _, k in deploy_breakdown)
    if deploy_has_data:
        lines.append("  Per-deploy replay breakdown (avg per deploy):")
        for label, key in deploy_breakdown:
            avg = metrics.get(key, 0)
            count = metrics.get(key + ".count", 0)
            if count > 0:
                lines.append(f"    {label}: {avg*1000:.0f}ms ({int(count)} deploys)")
    # System-deploy evaluation breakdown — what runs inside every precharge
    # / refund / close-block call. The eval phase = source-parse + reduce
    # + result-consume; check / rig / checkpoint-mergeable are bookkeeping.
    sysdeploy = [
        ("eval (total)", "block_replay_sysdeploy_eval_time"),
        ("  evaluate-source (parse + reduce)", "block_replay_sysdeploy_eval_evaluate_source_time"),
        ("  consume-result", "block_replay_sysdeploy_eval_consume_result_time"),
        ("rig", "block_replay_sysdeploy_rig_time"),
        ("check", "block_replay_sysdeploy_check_time"),
        ("checkpoint-mergeable", "block_replay_sysdeploy_checkpoint_mergeable_time"),
    ]
    sysdeploy_has_data = any(metrics.get(k + ".count", 0) > 0 for _, k in sysdeploy)
    if sysdeploy_has_data:
        lines.append("  System-deploy evaluation breakdown (avg per call):")
        for label, key in sysdeploy:
            avg = metrics.get(key, 0)
            count = metrics.get(key + ".count", 0)
            if count > 0:
                lines.append(f"    {label}: {avg*1000:.2f}ms ({int(count)} calls)")
    # inj_attempt phases — the four steps inside every Rholang `evaluate`
    # call (set initial cost / charge parsing cost / build normalized term
    # = parse / reduce term = run AST through RSpace).
    inj_phases = [
        ("set_initial_cost", "inj_attempt_set_initial_cost_time"),
        ("charge_parsing_cost", "inj_attempt_charge_parsing_cost_time"),
        ("build_normalized_term (parse)", "inj_attempt_build_normalized_term_time"),
        ("reduce_term (run AST)", "inj_attempt_reduce_term_time"),
    ]
    inj_has_data = any(metrics.get(k + ".count", 0) > 0 for _, k in inj_phases)
    if inj_has_data:
        lines.append("  Rholang inj_attempt phases (avg per evaluate call):")
        for label, key in inj_phases:
            avg = metrics.get(key, 0)
            count = metrics.get(key + ".count", 0)
            if count > 0:
                lines.append(f"    {label}: {avg*1000:.2f}ms ({int(count)} calls)")
    # Block creator (proposer side) breakdown
    creator_metrics = [
        ("prepare_user_deploys", "block_creator_prepare_user_deploys_time"),
        ("compute_parents_post_state", "block_creator_compute_parents_post_state_time"),
        ("compute_deploys_checkpoint", "block_creator_compute_deploys_checkpoint_time"),
        ("package_block", "block_creator_package_block_time"),
        ("TOTAL", "block_creator_total_time"),
    ]
    creator_has_data = any(metrics.get(k + ".count", 0) > 0 for _, k in creator_metrics)
    if creator_has_data:
        lines.append("  Block creator (proposer) phases (avg per block created):")
        for label, key in creator_metrics:
            avg = metrics.get(key, 0)
            count = metrics.get(key + ".count", 0)
            if count > 0:
                lines.append(f"    {label}: {avg*1000:.0f}ms ({int(count)} blocks)")
    # Finalization pipeline
    fin_metrics = [
        ("finalizer.run (top-level)", "finalizer_run_time"),
        ("clique_oracle.compute_max_clique_weight", "clique_oracle_compute_time"),
    ]
    fin_has_data = any(metrics.get(k + ".count", 0) > 0 for _, k in fin_metrics)
    if fin_has_data:
        lines.append("  Finalization pipeline (avg per call):")
        for label, key in fin_metrics:
            avg = metrics.get(key, 0)
            count = metrics.get(key + ".count", 0)
            if count > 0:
                lines.append(f"    {label}: {avg*1000:.1f}ms ({int(count)} calls)")
    # compute_rejected_buffer_admits (called inside compute_parents_post_state)
    admits_count = metrics.get("compute_rejected_buffer_admits_time.count", 0)
    if admits_count > 0:
        admits_avg = metrics.get("compute_rejected_buffer_admits_time", 0)
        lines.append(
            f"  compute_rejected_buffer_admits: {admits_avg*1000:.2f}ms avg, "
            f"{int(admits_count)} calls"
        )
    # compute_parents_post_state fallback counter
    fallback_fired = metrics.get(
        "compute_parents_post_state_fallback_merge_scope_too_large_fired.count", 0
    )
    if fallback_fired > 0:
        lines.append(f"  merge_scope_too_large fallback fired {int(fallback_fired)}×")
    # DAG insert
    dag_insert_count = metrics.get("dag_insert_time.count", 0)
    if dag_insert_count > 0:
        dag_insert_avg = metrics.get("dag_insert_time", 0)
        lines.append(
            f"  dag.insert: {dag_insert_avg*1000:.2f}ms avg ({int(dag_insert_count)} inserts)"
        )
    # is_mergeable_channel call count (per channel produce/consume during deploy execution)
    is_merge_calls = metrics.get("is_mergeable_channel_calls.count", 0)
    if is_merge_calls > 0:
        lines.append(f"  is_mergeable_channel calls: {int(is_merge_calls)}")
    # Runtime spawn timing
    spawn_metrics = [
        ("spawn_runtime", "runtime_spawn_time"),
        ("spawn_replay_runtime", "runtime_spawn_replay_time"),
    ]
    spawn_has_data = any(metrics.get(k + ".count", 0) > 0 for _, k in spawn_metrics)
    if spawn_has_data:
        lines.append("  Runtime spawn timing (avg per call):")
        for label, key in spawn_metrics:
            avg = metrics.get(key, 0)
            count = metrics.get(key + ".count", 0)
            if count > 0:
                lines.append(f"    {label}: {avg*1000:.0f}ms ({int(count)} calls)")
    # RSpace operation timing
    rspace_metrics = [
        ("consume (create)", "comm_consume_time_seconds"),
        ("produce (create)", "comm_produce_time_seconds"),
        ("install", "install_time_seconds"),
        ("consume (replay)", "replay_consume_time_seconds"),
        ("produce (replay)", "replay_produce_time_seconds"),
    ]
    rspace_has_data = any(metrics.get(k + ".count", 0) > 0 for _, k in rspace_metrics)
    if rspace_has_data:
        lines.append("  RSpace operations (avg per call):")
        for label, key in rspace_metrics:
            avg = metrics.get(key, 0)
            count = metrics.get(key + ".count", 0)
            if count > 0:
                lines.append(f"    {label}: {avg*1000:.1f}ms ({int(count)} calls)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deploy lifecycle tracking
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PhaseReport:
    """Summary report for a single load test phase."""

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
    # Per-validator metric deltas for the phase, keyed by validator name.
    # node_metrics (above) keeps the legacy v1-only view for callers that
    # already consume it; node_metrics_by_validator is the broader picture
    # that catches load on v2/v3 — important when v1 is mostly proposing
    # while peers do the validation work.
    node_metrics_by_validator: Optional[Dict[str, Dict[str, float]]] = None


@dataclasses.dataclass
class DeployRecord:
    """A single deploy submission record."""

    deploy_id: str
    submit_time: float
    validator_name: str
    phase: str
    index: int


@dataclasses.dataclass
class DeployResult:
    """A deploy with its measured inclusion and finalization times."""

    record: DeployRecord
    inclusion_time: Optional[float] = None
    block_number: Optional[int] = None
    finalization_time: Optional[float] = None


class LifecycleTracker:
    """Tracks deploy inclusion and finalization in background threads.

    For each deploy, a background thread polls find_deploy until the deploy
    appears in a block. A separate background thread polls last_finalized_block
    continuously and marks deploys as finalized when LFB passes their block.

    Usage::

        tracker = LifecycleTracker({"v1": node1, "v2": node2})
        tracker.start_lfb_monitor()
        tracker.track_deploy(record)
        tracker.wait_for_finalization(timeout=120)
        results = tracker.get_results()
        tracker.shutdown()
    """

    def __init__(self, nodes: dict, inclusion_timeout: int = 90):
        self._nodes = nodes
        self._node_list = list(nodes.values())
        self._inclusion_timeout = inclusion_timeout
        self._lock = threading.Lock()
        self._records: Dict[str, DeployRecord] = {}
        self._inclusion: Dict[str, Tuple[int, float]] = {}
        self._finalization: Dict[str, float] = {}
        self._max_block = 0
        self._executor = ThreadPoolExecutor(max_workers=6)
        self._lfb_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start_lfb_monitor(self):
        """Start background LFB polling thread."""
        self._stop_event.clear()
        self._lfb_thread = threading.Thread(target=self._lfb_poll_loop, daemon=True)
        self._lfb_thread.start()

    def stop_lfb_monitor(self):
        """Stop background LFB polling thread."""
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
        """Submit a deploy record for background inclusion tracking."""
        with self._lock:
            self._records[record.deploy_id] = record
        self._executor.submit(self._poll_inclusion, record)

    def _poll_inclusion(self, record: DeployRecord):
        node = self._node_list[0]
        deadline = time.time() + self._inclusion_timeout
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
        """Block until all tracked deploys are finalized or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                all_included = all(did in self._inclusion for did in self._records)
                if all_included:
                    all_finalized = all(did in self._finalization for did in self._records)
                    if all_finalized:
                        return
            time.sleep(1)

    def get_results(self) -> List[DeployResult]:
        """Return all tracked deploys with their measured times."""
        results = []
        with self._lock:
            for deploy_id, record in self._records.items():
                inc = self._inclusion.get(deploy_id)
                inclusion_time = (inc[1] - record.submit_time) if inc else None
                block_number = inc[0] if inc else None
                fin_time = self._finalization.get(deploy_id)
                finalization_time = (fin_time - record.submit_time) if fin_time else None
                results.append(
                    DeployResult(
                        record=record,
                        inclusion_time=inclusion_time,
                        block_number=block_number,
                        finalization_time=finalization_time,
                    )
                )
        return results

    def clear(self):
        """Clear all tracked records for a new phase."""
        with self._lock:
            self._records.clear()
            self._inclusion.clear()
            self._finalization.clear()
            self._max_block = 0

    def shutdown(self):
        """Stop all background threads and executor."""
        self.stop_lfb_monitor()
        self._executor.shutdown(wait=False)

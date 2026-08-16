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
            lines.append(f"    {label}: {avg * 1000:.0f}ms ({int(count)} blocks)")
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
                lines.append(f"    {label}: {avg * 1000:.1f}ms ({int(count)} calls)")
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
                lines.append(f"    {label}: {avg * 1000:.2f}ms ({int(count)} calls)")
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
                f"    parents_post_state (merge): {merge_avg * 1000:.0f}ms ({int(merge_count)} blocks)"
            )
        if replay_count > 0:
            lines.append(
                f"    replay_block (execution): {replay_avg * 1000:.0f}ms ({int(replay_count)} blocks)"
            )
    # parents_post_state sub-stage breakdown — where the multi-parent merge cost goes
    # (the ~1.8s/multi-parent that used to be lumped under parents_post_state).
    pps_substages = [
        (
            "floor_compute (clique floor)",
            "block_processing_stage_parents_post_state_floor_compute_time",
        ),
        ("fs_seal (FS fold)", "block_processing_stage_parents_post_state_fs_seal_time"),
        ("scope_build (cone walk)", "block_processing_stage_parents_post_state_scope_build_time"),
        (
            "ensure_mergeable (recompute)",
            "block_processing_stage_parents_post_state_ensure_mergeable_time",
        ),
        ("dag merge", "block_processing_stage_parents_post_state_merge_time"),
    ]
    pps_has_data = any(metrics.get(k + ".count", 0) > 0 for _, k in pps_substages)
    if pps_has_data:
        lines.append("  parents_post_state sub-stages (avg per multi-parent block):")
        for label, key in pps_substages:
            avg = metrics.get(key, 0)
            count = metrics.get(key + ".count", 0)
            if count > 0:
                lines.append(f"    {label}: {avg * 1000:.0f}ms ({int(count)} blocks)")
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
                lines.append(f"    {label}: {avg * 1000:.0f}ms ({int(count)} merges)")
    # dag_merger rejection-expansion path: called every merge, fires only
    # when there are rejected source blocks with descendants in scope.
    rej_exp_count = metrics.get("dag_merge_rejection_expansion_time.count", 0)
    rej_exp_fired = metrics.get("dag_merge_rejection_expansion_fired.count", 0)
    if rej_exp_count > 0:
        rej_exp_avg = metrics.get("dag_merge_rejection_expansion_time", 0)
        lines.append(
            f"    rejection_expansion: {rej_exp_avg * 1000:.2f}ms avg, "
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
            lines.append(f"    {label}: {avg * 1000:.0f}ms ({int(count)} blocks)")
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
                lines.append(f"    {label}: {avg * 1000:.0f}ms ({int(count)} deploys)")
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
                lines.append(f"    {label}: {avg * 1000:.2f}ms ({int(count)} calls)")
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
                lines.append(f"    {label}: {avg * 1000:.2f}ms ({int(count)} calls)")
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
                lines.append(f"    {label}: {avg * 1000:.0f}ms ({int(count)} blocks)")
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
                lines.append(f"    {label}: {avg * 1000:.1f}ms ({int(count)} calls)")
    # compute_rejected_buffer_admits (called inside compute_parents_post_state)
    admits_count = metrics.get("compute_rejected_buffer_admits_time.count", 0)
    if admits_count > 0:
        admits_avg = metrics.get("compute_rejected_buffer_admits_time", 0)
        lines.append(
            f"  compute_rejected_buffer_admits: {admits_avg * 1000:.2f}ms avg, "
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
            f"  dag.insert: {dag_insert_avg * 1000:.2f}ms avg ({int(dag_insert_count)} inserts)"
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
                lines.append(f"    {label}: {avg * 1000:.0f}ms ({int(count)} calls)")
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
                lines.append(f"    {label}: {avg * 1000:.1f}ms ({int(count)} calls)")
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
    """Tracks deploy inclusion and finalization with one batch-poll thread.

    A single background thread sweeps every pending deploy through
    ``deploy_finalization_status`` — the node's authoritative canonical-state
    answer. Inclusion is observed when the status first reports a containing
    block; finalization when the state reaches ``DEPLOY_STATE_FINALIZED``.

    This replaces two unreliable mechanisms that produced the 640 bogus
    "unfinalized" results in soak preflight 31919610258:

    - a 6-worker pool running one ``find_deploy`` poll loop PER DEPLOY
      (each occupying a worker for up to ``inclusion_timeout``) — at
      sustained-phase volume (1200 deploys) the pool starved and most
      deploys were never polled at all, and
    - finalization inferred from ``included_block_number <= LFB_number``,
      which is orphan-unsafe: the recorded inclusion block can lose fork
      choice and the deploy re-home to a later block (the same
      ``find_deploy`` race fixed in PR #118's bonding anchor).

    A batch sweep visits EVERY pending deploy each cycle regardless of
    backlog, and the status API already accounts for merge rejection and
    re-homing, so no block-number inference is involved.

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
        # Kept for API compatibility; the sweep is continuous and the
        # overall bound is wait_for_finalization's timeout.
        self._inclusion_timeout = inclusion_timeout
        self._lock = threading.Lock()
        self._records: Dict[str, DeployRecord] = {}
        self._inclusion: Dict[str, Tuple[int, float]] = {}
        self._finalization: Dict[str, float] = {}
        # Terminal FAILED/EXPIRED deploys: excluded from further sweeps but
        # deliberately left un-finalized so they count against the load
        # test's unfinalized assertion instead of hiding.
        self._terminal: Dict[str, str] = {}
        # Block-hash → block-number cache for inclusion enrichment. Many
        # deploys share a containing block, so this collapses the
        # get_block volume from per-deploy to per-unique-block.
        self._block_numbers: Dict[str, int] = {}
        self._max_block = 0
        self._lfb_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start_lfb_monitor(self):
        """Start the background status-sweep thread."""
        self._stop_event.clear()
        self._lfb_thread = threading.Thread(target=self._sweep_loop, daemon=True)
        self._lfb_thread.start()

    def stop_lfb_monitor(self):
        """Stop the background status-sweep thread."""
        self._stop_event.set()
        if self._lfb_thread:
            self._lfb_thread.join(timeout=10)

    # Bounded concurrency for the per-cycle status probes. Small enough
    # to be gentle on a loaded node, large enough that one slow RPC
    # (e.g. a call riding out its whole gRPC deadline) delays a cycle by
    # deadline/8 rather than blocking every deploy queued behind it.
    _SWEEP_WORKERS = 8
    # Consecutive all-fail cycles before warning: distinguishes a dead
    # channel / crashed node from ordinary not-yet-included polling.
    _SWEEP_FAIL_STREAK_WARN = 5

    def _sweep_loop(self):
        from concurrent.futures import ThreadPoolExecutor

        from f1r3fly.pb.DeployServiceCommon_pb2 import (
            DEPLOY_STATE_EXPIRED,
            DEPLOY_STATE_FAILED,
            DEPLOY_STATE_FINALIZED,
        )

        terminal_states = (DEPLOY_STATE_FAILED, DEPLOY_STATE_EXPIRED)
        node = self._node_list[0]
        # NOTE: the node's client is cached, so this is NOT a
        # reconnection mechanism — gRPC channels reconnect transient
        # failures on their own, per-probe failures are tolerated, and a
        # permanently dead channel surfaces via the fail-streak warning.
        client = node._external_client()
        fail_streak = 0
        with ThreadPoolExecutor(max_workers=self._SWEEP_WORKERS) as pool:
            while not self._stop_event.is_set():
                cycle_start = time.time()
                pending, errors = self._sweep_cycle(
                    pool, node, client, DEPLOY_STATE_FINALIZED, terminal_states
                )

                if pending and errors == pending:
                    fail_streak += 1
                    if fail_streak == self._SWEEP_FAIL_STREAK_WARN:
                        logger.warning(
                            "LifecycleTracker: every status probe has failed for %d "
                            "consecutive cycles (%d pending) — node unreachable or "
                            "channel dead; deploys will report unfinalized",
                            fail_streak,
                            pending,
                        )
                else:
                    fail_streak = 0

                # Don't compound a long sweep with the full inter-cycle
                # pause — target ~1s between cycle STARTS.
                elapsed = time.time() - cycle_start
                self._stop_event.wait(timeout=max(0.0, 1.0 - elapsed))

    def _sweep_cycle(self, pool, node, client, finalized_state, terminal_states):
        """One sweep pass over every pending deploy.

        Two phases, strictly ordered: phase 1 folds EVERY probe result's
        finalized/terminal state into the bookkeeping; phase 2 runs
        block-number enrichment. A slow (not just failing) ``get_block``
        therefore cannot delay any deploy's finalization write in the
        same sweep — the verdict ``wait_for_finalization`` reads is fully
        recorded before the first enrichment RPC is issued.

        Returns ``(pending_count, probe_error_count)``. Factored out of
        the loop so lifecycle unit tests can drive a cycle synchronously
        (no thread, no inter-cycle wait).
        """
        with self._lock:
            pending = [
                did
                for did in self._records
                if did not in self._finalization and did not in self._terminal
            ]

        def _probe(deploy_id):
            try:
                return deploy_id, client.deploy_finalization_status(deploy_id), None
            except Exception as exc:
                return deploy_id, None, exc

        errors = 0
        succeeded = []
        for deploy_id, status, exc in pool.map(_probe, pending):
            if self._stop_event.is_set():
                return len(pending), errors
            if exc is not None:
                errors += 1
                logger.debug("status probe failed for %s: %s", deploy_id[:16], exc)
                continue
            self._apply_state(deploy_id, status, finalized_state, terminal_states)
            succeeded.append((deploy_id, status))
        for deploy_id, status in succeeded:
            if self._stop_event.is_set():
                break
            self._enrich_inclusion(node, deploy_id, status)
        return len(pending), errors

    def _apply_state(self, deploy_id, status, finalized_state, terminal_states):
        """Fold one status response's STATE into the bookkeeping maps.

        Pure dictionary writes — no RPCs, nothing here can be slow.
        Every write is gated on ``deploy_id in self._records`` under the
        lock: ``clear()`` runs between load phases while the sweep
        thread is alive, and a stale write after the clear would corrupt
        the next phase's counts.
        """
        now = time.time()
        if status.state == finalized_state:
            with self._lock:
                if deploy_id in self._records:
                    self._finalization[deploy_id] = now
        elif status.state in terminal_states:
            with self._lock:
                if deploy_id in self._records:
                    self._terminal[deploy_id] = str(status.state)

    def _enrich_inclusion(self, node, deploy_id, status):
        """Resolve inclusion telemetry (block number) for one deploy.

        Telemetry only — runs strictly AFTER every state write of the
        sweep, so a slow or broken ``get_block`` can never delay a
        finalization the verdict depends on. Same ``clear()`` gating as
        ``_apply_state``.
        """
        now = time.time()
        if not status.latestBlockHash:
            return
        with self._lock:
            tracked = deploy_id in self._records
            existing = self._inclusion.get(deploy_id)
        # (Re-)resolve the block number when unknown — including a
        # previous cycle's failed lookup, stored as 0. The original
        # inclusion timestamp is preserved on re-resolution.
        if not tracked or (existing is not None and existing[0] != 0):
            return
        block_hash = status.latestBlockHash.hex()
        with self._lock:
            block_number = self._block_numbers.get(block_hash, 0)
        if block_number == 0:
            try:
                block_number = node.get_block(block_hash).blockInfo.blockNumber
            except Exception as exc:
                logger.debug("block-number lookup failed for %s: %s", deploy_id[:16], exc)
        with self._lock:
            if block_number:
                self._block_numbers[block_hash] = block_number
            if deploy_id in self._records:
                included_at = existing[1] if existing else now
                self._inclusion[deploy_id] = (block_number, included_at)
                self._max_block = max(self._max_block, block_number)

    def track_deploy(self, record: DeployRecord):
        """Register a deploy; the sweep thread picks it up on its next pass."""
        with self._lock:
            self._records[record.deploy_id] = record

    def wait_for_finalization(self, timeout: float):
        """Block until every tracked deploy is settled, or timeout.

        Settled means finalized OR terminal (FAILED/EXPIRED). Terminal
        deploys still surface as unfinalized in ``get_results`` — this
        just avoids burning the full timeout waiting on a deploy the
        node has already declared dead.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                settled = all(
                    did in self._finalization or did in self._terminal for did in self._records
                )
            if settled:
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
            self._terminal.clear()
            self._block_numbers.clear()
            self._max_block = 0

    def shutdown(self):
        """Stop the background sweep thread."""
        self.stop_lfb_monitor()

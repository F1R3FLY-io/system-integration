"""Steps of open-loop load against a shard, and what the shard did with them.

``run_load`` submits a workload at each step's rate for the step's hold,
samples the shard's health throughout, then stops the load and drains until
every accepted deploy has an outcome. It returns per-step rows and a summary
and asserts nothing: the calling test decides what fails.

A step fails when the shard stalls or breaks while it runs: finality does not
advance, its health cannot be read, or a submission fails other than by a
deploy queue's refusal. A step whose submitter fell behind its target rate is
invalid rather than failed: the shard's own slowness would otherwise lower
the load it is measured under. Depths still growing when a step ends (LFB
spread, tip-minus-LFB, block-ingest backlog) are reported as pressure.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import statistics
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import requests

from .load import (
    REFUSAL_PATIENCE_SECONDS,
    OpenLoopSubmitter,
    PlannedDeploy,
    Resubmitter,
    VabnSource,
    node_key_pairs,
)
from .metrics import (
    LifecycleTracker,
    compute_metric_deltas,
    percentiles,
    scrape_gauges,
    scrape_metrics,
)
from .polling import wait_for_lfb_converged
from .workloads import SetupContext, Workload, check_execution

# Rise per minute, from a step's first half to its second, beyond which a depth
# is still growing.
_GROWTH_PER_MINUTE = 1.0
# Depths a working shard sits at under any load: the LFB spread the load test
# accepts as converged, about three heights of tip-minus-LFB, and a few blocks
# per node in intake. Below these a rising series is sampling noise, not lag.
_WORKING_DEPTH = {"lfb_spread": 5, "tip_depth": 10, "ingest_backlog": 20}
# A step is invalid when the submitter achieved less than this share of the
# target rate, or started a slot later than _MAX_START_LAG after it was due.
_ACHIEVED_SHARE = 0.95
_MAX_START_LAG = 2.0

SAMPLE_SECONDS = 10.0
_GAUGE_EVERY = 3

# The drain checks for outcomes this often, gives up once finality has not
# advanced for _DRAIN_STALL_SECONDS, and waits _VERDICT_GRACE_SECONDS for the
# last verdicts to reach the tracker once every window has closed.
_DRAIN_POLL_SECONDS = 10.0
_DRAIN_STALL_SECONDS = 300.0
_VERDICT_GRACE_SECONDS = 120.0
_BACKLOG_GAUGES = ["block_ingest_held", "block_ingest_validating", "block_ingest_needs"]


@dataclasses.dataclass(frozen=True)
class Step:
    """One rate held for ``hold`` seconds; ``phase`` names its deploys."""

    phase: str
    rate: float
    hold: float


@dataclasses.dataclass
class LoadRun:
    """What ``run_load`` measured. ``rows`` has one entry per step that ran."""

    rows: List[dict]
    summary: dict
    tracker: LifecycleTracker
    # Per step, per validator key ("validator1"): /metrics deltas over the step.
    node_metrics: Dict[str, Dict[str, Dict[str, float]]]
    # Every node's LFB the moment load stopped.
    drain_lfbs: Dict[str, Optional[int]]


def _slope_per_minute(series: Sequence[Tuple[float, float]]) -> float:
    """Least-squares slope of (time, value) samples, per minute."""
    if len(series) < 2:
        return 0.0
    t0 = series[0][0]
    xs = [t - t0 for t, _ in series]
    ys = [v for _, v in series]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    var = sum((x - mean_x) ** 2 for x in xs)
    if var == 0:
        return 0.0
    return 60 * sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var


def _half_rise_per_minute(series: Sequence[Tuple[float, float]]) -> float:
    """Mean of the second half minus the mean of the first, per minute between
    the halves' midpoints. Less sensitive to one outlying sample than a slope."""
    if len(series) < 4:
        return 0.0
    half = len(series) // 2
    first, second = series[:half], series[half:]
    minutes = (statistics.fmean(t for t, _ in second) - statistics.fmean(t for t, _ in first)) / 60
    if minutes <= 0:
        return 0.0
    return (
        statistics.fmean(v for _, v in second) - statistics.fmean(v for _, v in first)
    ) / minutes


def _longest_flat(series: Sequence[Tuple[float, float]], start: float, end: float) -> float:
    """Longest stretch of [start, end] over which ``series`` did not rise."""
    rises = [start]
    for (_, before), (t, value) in zip(series, series[1:]):
        if value > before:
            rises.append(t)
    rises.append(end)
    return max(b - a for a, b in zip(rises, rises[1:]))


class HealthSampler:
    """Samples the shard every SAMPLE_SECONDS on a background thread.

    Per sample: every node's LFB (its status endpoint, timed), the first
    validator's tip, and every _GAUGE_EVERY samples each node's block-ingest
    backlog and the deploys held by the validators' deploy queues. Read
    failures are counted per node, never folded in as zeros. A shard whose
    validators answer without a deploy queue has no queue series.
    """

    def __init__(self, nodes: Sequence, tip_node, queue_nodes: Sequence):
        self._nodes = nodes
        self._tip_node = tip_node
        self._queue_nodes = list(queue_nodes)
        self._lock = threading.Lock()
        self._max_lfb: List[Tuple[float, float]] = []
        self._spread: List[Tuple[float, float]] = []
        self._depth: List[Tuple[float, float]] = []
        self._backlog: List[Tuple[float, float]] = []
        self._queued: List[Tuple[float, float]] = []
        self._status_seconds: List[Tuple[float, float]] = []
        self.read_failures: Dict[str, int] = {n.name: 0 for n in nodes}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def lfb(self, node) -> Optional[int]:
        began = time.time()
        try:
            value = int(node.api_get("/status", timeout=10)["lastFinalizedBlockNumber"])
        except Exception:
            with self._lock:
                self.read_failures[node.name] += 1
            return None
        with self._lock:
            self._status_seconds.append((began, time.time() - began))
        return value

    def _run(self) -> None:
        sample = 0
        while not self._stop.is_set():
            now = time.time()
            lfbs = {n.name: self.lfb(n) for n in self._nodes}
            known = [v for v in lfbs.values() if v is not None]
            tip_lfb = lfbs.get(self._tip_node.name)
            try:
                blocks = self._tip_node.get_blocks(1)
                tip = max(b.blockNumber for b in blocks) if blocks else None
            except Exception:
                tip = None
            with self._lock:
                if known:
                    self._max_lfb.append((now, max(known)))
                if len(known) == len(self._nodes):
                    self._spread.append((now, max(known) - min(known)))
                if tip is not None and tip_lfb is not None:
                    self._depth.append((now, tip - tip_lfb))
            if sample % _GAUGE_EVERY == 0:
                worst = None
                for node in self._nodes:
                    try:
                        gauges = scrape_gauges(node, _BACKLOG_GAUGES)
                    except Exception:
                        with self._lock:
                            self.read_failures[node.name] += 1
                        continue
                    backlog = sum(gauges.values())
                    worst = backlog if worst is None else max(worst, backlog)
                if worst is not None:
                    with self._lock:
                        self._backlog.append((now, worst))
                queued = self._queued_deploys()
                if queued is not None:
                    with self._lock:
                        self._queued.append((now, queued))
            sample += 1
            self._stop.wait(SAMPLE_SECONDS)

    def _queued_deploys(self) -> Optional[int]:
        """Deploys the validators' queues hold and have not settled, summed;
        None when any queue could not be read or there are none."""
        total = 0
        for node in self._queue_nodes:
            try:
                status = node.http_get("/queue/status", timeout=10).json()
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    self._queue_nodes = []
                    return None
                with self._lock:
                    self.read_failures[node.name] += 1
                return None
            except Exception:
                with self._lock:
                    self.read_failures[node.name] += 1
                return None
            total += sum(status[k] for k in ("ready", "waiting", "submitted", "in_block"))
        return total if self._queue_nodes else None

    def window(self, start: float, end: float) -> dict:
        """Health over [start, end]: last value, peak and slope per series;
        how far finality advanced, its seconds per height and its longest
        stretch without advancing; the slowest status read."""
        with self._lock:
            lfb = [s for s in self._max_lfb if start <= s[0] <= end]
            series = {
                "lfb_spread": [s for s in self._spread if start <= s[0] <= end],
                "tip_depth": [s for s in self._depth if start <= s[0] <= end],
                "ingest_backlog": [s for s in self._backlog if start <= s[0] <= end],
                "queued_deploys": [s for s in self._queued if start <= s[0] <= end],
            }
            status_seconds = [d for t, d in self._status_seconds if start <= t <= end]
        advanced = lfb[-1][1] - lfb[0][1] if len(lfb) >= 2 else None
        summary: dict = {
            "finality": {
                "start": lfb[0][1] if lfb else None,
                "end": lfb[-1][1] if lfb else None,
                "advanced": advanced,
                "secs_per_height": (
                    round((lfb[-1][0] - lfb[0][0]) / advanced, 2) if advanced else None
                ),
                "longest_stall_seconds": round(_longest_flat(lfb, start, end), 1),
                "samples": len(lfb),
            },
            "status_seconds_max": round(max(status_seconds), 3) if status_seconds else None,
        }
        for name, samples in series.items():
            values = [v for _, v in samples]
            summary[name] = {
                "end": values[-1] if values else None,
                "max": max(values) if values else None,
                "slope_per_min": round(_slope_per_minute(samples), 3),
                "half_rise_per_min": round(_half_rise_per_minute(samples), 3),
                "samples": len(values),
            }
        return summary


def step_results(tracker: LifecycleTracker, phase: str):
    return [r for r in tracker.get_results() if r.record.phase == phase]


def judge_step(rate: float, stats, health: dict) -> dict:
    """Whether the shard kept working while a step ran (``failures``), and
    which depths were still growing when it ended (``pressure``). A step whose
    load generator fell behind its rate is ``invalid``: its numbers do not
    measure the target rate, and say nothing against the shard. The step's
    deploys are judged later, once they have had their window to settle."""
    failures = []
    valid = stats.offered_rate >= _ACHIEVED_SHARE * rate and stats.max_start_lag <= _MAX_START_LAG
    invalid = (
        None
        if valid
        else (
            f"load generator fell behind: {stats.offered_rate:.2f}/s of {rate:.2f}/s, "
            f"max start lag {stats.max_start_lag:.1f}s"
        )
    )
    if stats.errors:
        failures.append(f"{len(stats.errors)} submissions failed")
    finality = health["finality"]
    if finality["samples"] < 2:
        failures.append(f"shard health unmeasured: {finality['samples']} LFB samples in the step")
    elif not finality["advanced"]:
        failures.append("finality stalled: the LFB did not advance during the step")
    pressure = []
    for name, working in _WORKING_DEPTH.items():
        h = health[name]
        if (
            h["end"] is not None
            and h["end"] > working
            and h["half_rise_per_min"] > _GROWTH_PER_MINUTE
        ):
            pressure.append(f"{name} growing {h['half_rise_per_min']:.2f}/min (end {h['end']})")
    return {
        "valid": valid,
        "invalid": invalid,
        "ok": not failures,
        "failures": failures,
        "pressure": pressure,
    }


def settlement(tracker: LifecycleTracker, phase: str) -> dict:
    """How a step's accepted deploys ended: finalized, failed in execution,
    expired, or unsettled (no outcome when the drain gave up)."""
    step_ids = {r.record.deploy_id for r in step_results(tracker, phase)}
    unfinalized = [u for u in tracker.get_unfinalized() if u.deploy_id in step_ids]
    outcomes = [u.outcome for u in unfinalized]
    return {
        "accepted": len(step_ids),
        "finalized": len(step_ids) - len(unfinalized),
        "failed": outcomes.count("FAILED"),
        "expired": outcomes.count("EXPIRED"),
        "unsettled": outcomes.count("timed out"),
    }


def step_status(row: dict) -> str:
    """FAILED when the shard stalled or broke, INVALID when only the load
    generator fell behind, ok otherwise."""
    if not row["ok"]:
        return "FAILED: " + "; ".join(row["failures"])
    if row["invalid"]:
        return "INVALID: " + row["invalid"]
    return "ok"


def finalized_between(results, start: float, end: float) -> int:
    """Deploys of any step that finalized between ``start`` and ``end``."""
    return sum(
        1
        for r in results
        if r.finalization_time is not None
        and start <= r.record.submit_time + r.finalization_time <= end
    )


def settlement_failures(settled: dict) -> List[str]:
    if settled["unsettled"]:
        return [f"{settled['unsettled']} accepted deploys have no outcome"]
    return []


def _by_kind(tracker: LifecycleTracker, phase: str) -> Dict[str, dict]:
    """Finalization latency per deploy kind for one step: count, p50, p95."""
    groups: Dict[str, List[Optional[float]]] = {}
    for r in step_results(tracker, phase):
        groups.setdefault(r.record.kind, []).append(r.finalization_time)
    out = {}
    for kind, times in sorted(groups.items()):
        done = [t for t in times if t is not None]
        p50, p95 = percentiles(done, [50, 95]) if done else (None, None)
        out[kind] = {
            "count": len(times),
            "finalized": len(done),
            "fin_p50": None if p50 is None else round(p50, 1),
            "fin_p95": None if p95 is None else round(p95, 1),
        }
    return out


def window_seconds(rows: List[dict], deploy_lifespan: int, max_parent_depth: int) -> float:
    """One deploy window, plus the depth its verdict trails inclusion by, at
    the slowest finality pace any step saw."""
    paces = [
        r["health"]["finality"]["secs_per_height"]
        for r in rows
        if r["health"]["finality"]["secs_per_height"] is not None
    ]
    if not paces:
        raise AssertionError("no step timed a finality height; convergence cannot be sized")
    return (deploy_lifespan + max_parent_depth) * max(paces)


def drain(
    settled: Callable[[float], bool],
    finalized_height: Callable[[], Optional[int]],
    target: int,
    stall_seconds: float = _DRAIN_STALL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Tuple[bool, Optional[str]]:
    """Waits until every accepted deploy has an outcome, which each has once
    finality passes ``target``, the height past which every window the run
    signed is closed. Returns whether all settled, and why the drain stopped
    early when finality stopped advancing."""
    last: Optional[int] = None
    advanced_at = clock()
    while True:
        if settled(_DRAIN_POLL_SECONDS):
            return True, None
        height = finalized_height()
        if height is not None and (last is None or height > last):
            last, advanced_at = height, clock()
        if last is not None and last >= target:
            return settled(_VERDICT_GRACE_SECONDS), None
        if clock() - advanced_at > stall_seconds:
            return False, (
                f"finality stalled at {last} for {stall_seconds:.0f}s while draining "
                f"toward {target}"
            )


def client_seconds(results) -> List[float]:
    """Seconds from each finalized deploy's first submission attempt, refused
    or not, to its finalization: what the client waited."""
    return [
        r.record.submit_time + r.finalization_time - r.record.first_attempt_time
        for r in results
        if r.finalization_time is not None
    ]


def _final_latencies(tracker: LifecycleTracker, phase: str) -> dict:
    results = step_results(tracker, phase)
    client_p = percentiles(client_seconds(results), [50, 95])
    fin = [r.finalization_time for r in results if r.finalization_time is not None]
    first = [r.first_finalization_time for r in results if r.first_finalization_time is not None]
    inc = [r.inclusion_time for r in results if r.inclusion_time is not None]
    fin_p = percentiles(fin, [50, 95, 99])
    inc_p = percentiles(inc, [50, 95, 99])
    first_p = percentiles(first, [50, 95])
    rejection_counts = tracker.rejection_counts()
    rejected = [
        rejection_counts[r.record.deploy_id]
        for r in results
        if r.record.deploy_id in rejection_counts
    ]
    return {
        "merge_rejected": len(rejected),
        "merge_rejections_mean": round(statistics.fmean(rejected), 2) if rejected else 0.0,
        "merge_rejections_max": max(rejected, default=0),
        "finalized": len(fin),
        "unfinalized": len(results) - len(fin),
        "finalization_p50": round(fin_p[0], 1),
        "finalization_p95": round(fin_p[1], 1),
        "finalization_p99": round(fin_p[2], 1),
        "client_p50": round(client_p[0], 1),
        "client_p95": round(client_p[1], 1),
        "first_node_p50": round(first_p[0], 1),
        "first_node_p95": round(first_p[1], 1),
        "inclusion_p50": round(inc_p[0], 1),
        "inclusion_p95": round(inc_p[1], 1),
        "inclusion_p99": round(inc_p[2], 1),
    }


def _converge(nodes: Sequence, timeout: float) -> Tuple[Optional[dict], Optional[str]]:
    """Every node's LFB once all are within 5 of each other, or why they were
    not within ``timeout``."""
    try:
        lfbs = wait_for_lfb_converged(
            nodes,
            timeout=timeout,
            max_spread=5,
            description="all-node LFB spread <= 5 after the load",
        )
    except AssertionError as exc:
        return None, str(exc)
    return lfbs, None


def _write(path: Optional[str], row: dict) -> None:
    if path is None:
        return
    with open(path, "a") as out:
        out.write(json.dumps(row) + "\n")


def run_load(
    shard,
    senders: Sequence,
    workload: Workload,
    steps: Sequence[Step],
    timeouts,
    *,
    resubmit: bool,
    stop_on_failure: bool,
    spread: bool = False,
    out: Optional[str] = None,
    wait_for_baseline: bool = True,
) -> LoadRun:
    """Run ``steps`` of ``workload`` against ``shard``, signed by ``senders``.

    Each sender submits to one validator, or with ``spread`` a sender's
    consecutive deploys go to different validators. With ``resubmit``, a
    deploy a queue refused is signed again and resubmitted after its
    retry-after, as a client that honors refusals would; otherwise it is
    dropped. ``stop_on_failure`` ends the run at the first failed step. Rows,
    the summary and every deploy's record are appended to ``out`` (JSON
    lines, deploys beside it as ``<out>-deploys.jsonl``) when given. Unless
    ``wait_for_baseline`` is off, the run starts once every node has
    finalized a block and all are within 5 heights of each other.
    """
    named_validators = shard.named_validators
    validators = list(named_validators.values())
    pairs = node_key_pairs(validators, senders)
    readonly = shard.readonly
    if readonly is None:
        raise AssertionError("a load run needs a read-only node to verify state")
    if wait_for_baseline:
        wait_for_lfb_converged(
            shard.all_nodes,
            timeout=timeouts.finalization * 2,
            min_height=1,
            max_spread=5,
            description="finalized baseline before the load",
        )

    node, key = pairs[0]
    workload.prepare(
        SetupContext(node=node, key=key, all_nodes=shard.all_nodes, timeout=timeouts.finalization)
    )

    def plan(index: int) -> PlannedDeploy:
        if spread:
            node = validators[index % len(validators)]
            key = senders[(index // len(validators)) % len(senders)]
        else:
            node, key = pairs[index % len(pairs)]
        term, phlo_limit = workload.term(index, key)
        return PlannedDeploy(node, key, term, phlo_limit, workload.kind(index))

    shard_status = validators[0].api_get("/status", timeout=30)
    deploy_lifespan = int(shard_status["deployLifespan"])
    max_parent_depth = int(shard_status["maxParentDepth"])
    tracker = LifecycleTracker({n.name: n for n in shard.all_nodes})
    tracker.start_lfb_monitor()
    sampler = HealthSampler(shard.all_nodes, validators[0], validators)
    sampler.start()
    # The tip is read from an observer: a validator under load is slow to
    # answer queries, and a failed read would fail the deploy it signs.
    vabn = VabnSource(readonly)
    resubmitter = (
        Resubmitter(
            OpenLoopSubmitter.workers_for(max(s.rate for s in steps)), vabn, tracker.track_deploy
        )
        if resubmit
        else None
    )
    index = 1_000_000
    rows: List[dict] = []
    node_metrics: Dict[str, Dict[str, Dict[str, float]]] = {}
    logging.info(
        "Load: %s, steps %s, %d senders over %d validators, refused deploys %s",
        workload.name,
        ", ".join(f"{s.phase} {s.rate:g}/s for {s.hold:.0f}s" for s in steps),
        len(senders),
        len(validators),
        "resubmitted after their retry-after" if resubmitter else "dropped",
    )
    try:
        for step in steps:
            submitter = OpenLoopSubmitter(OpenLoopSubmitter.workers_for(step.rate))
            tracker.cycle_stats()
            metrics_before = {key: scrape_metrics(v) for key, v in named_validators.items()}
            start = time.time()
            stats = submitter.run(
                step.rate,
                step.hold,
                index,
                plan,
                vabn,
                step.phase,
                tracker.track_deploy,
                retry_refusals=False,
                on_refused=resubmitter.refused if resubmitter else None,
            )
            end = time.time()
            node_metrics[step.phase] = {
                key: compute_metric_deltas(metrics_before[key], scrape_metrics(v))
                for key, v in named_validators.items()
            }
            index += int(step.rate * step.hold)
            health = sampler.window(start, end)
            verdict = judge_step(step.rate, stats, health)
            by_kind = _by_kind(tracker, step.phase)
            cycles = tracker.cycle_stats()
            row = {
                "workload": workload.name,
                "phase": step.phase,
                "rate_target": step.rate,
                "rate_achieved": round(stats.achieved_rate, 3),
                "rate_offered": round(stats.offered_rate, 3),
                "submitted": stats.submitted,
                "refused": stats.refused,
                "errors": len(stats.errors),
                "max_start_lag": round(stats.max_start_lag, 2),
                "submit_p95": round(percentiles(stats.submit_seconds, [95])[0], 3),
                "submit_errors": stats.errors[:5],
                "hold_seconds": step.hold,
                "started": start,
                "ended": end,
                "health": health,
                "tracker_cycle_max": round(max(c[2] for c in cycles.values()), 2),
                "by_kind": by_kind,
                **verdict,
            }
            rows.append(row)
            _write(out, {"stage": "judged", **row})
            logging.info(
                "  %s: %s — %s; accepted %.2f/s of %.2f/s offered, %d refused, "
                "queues held up to %s, finality %s s/height",
                step.phase,
                step_status(verdict),
                "; ".join(verdict["failures"] + verdict["pressure"]) or "no pressure",
                stats.achieved_rate,
                stats.offered_rate,
                stats.refused,
                health["queued_deploys"]["max"],
                health["finality"]["secs_per_height"],
            )
            for kind, k in by_kind.items():
                logging.info(
                    "    %-10s n=%-5d finalized so far %-5d p50=%s p95=%s",
                    kind,
                    k["count"],
                    k["finalized"],
                    k["fin_p50"],
                    k["fin_p95"],
                )
            if stop_on_failure and not verdict["ok"]:
                break

        drain_lfbs = {n.name: sampler.lfb(n) for n in shard.all_nodes}
        if resubmitter is not None:
            logging.info(
                "Waiting up to %.0fs for refused deploys to be resubmitted",
                REFUSAL_PATIENCE_SECONDS,
            )
            resubmitter.wait_idle(REFUSAL_PATIENCE_SECONDS + 60)
            resubmitter.close()
        # Every deploy was signed at or below the current
        # validAfterBlockNumber, so its window closes, and the node
        # decides it, once finality passes this height.
        windows_closed_at = vabn.get() + deploy_lifespan + max_parent_depth
        logging.info("Draining until finality passes %d", windows_closed_at)
        drain_start = time.time()
        settled, drain_stall = drain(
            tracker.wait_for_finalization,
            lambda: sampler.lfb(readonly),
            windows_closed_at,
        )
        drained_after = time.time() - drain_start
        # Nodes that fell behind under load catch up after it stops, so
        # convergence gets a deploy window of its own.
        converge_start = time.time()
        converged, convergence_failure = _converge(
            shard.all_nodes, timeout=window_seconds(rows, deploy_lifespan, max_parent_depth)
        )
        converged_after = time.time() - converge_start
    finally:
        if resubmitter is not None:
            resubmitter.close()
        sampler.stop()
        tracker.shutdown()

    # Every finalized deploy, tails included: they all changed state.
    finalized = {
        r.record.deploy_id: r.record.index
        for r in tracker.get_results()
        if r.finalization_time is not None
    }
    execution_problems, cost = check_execution(readonly, finalized)
    problems = execution_problems + (
        workload.verify(readonly, finalized)
        if settled
        else ["deploys without an outcome after the drain; state not checked"]
    )

    resubmitted = resubmitter.outcomes if resubmitter is not None else {}
    results = tracker.get_results()
    for row in rows:
        row.update(_final_latencies(tracker, row["phase"]))
        settled_row = settlement(tracker, row["phase"])
        row["settlement"] = settled_row
        finalized_in_step = finalized_between(results, row["started"], row["ended"])
        row["rate_finalized"] = round(finalized_in_step / (row["ended"] - row["started"]), 3)
        row["resubmitted"] = resubmitted.get(row["phase"], {})
        # Offered deploys that never finalized for want of room: clients
        # that gave up after refusals, and deploys that expired queued.
        offered = row["submitted"] + row["refused"]
        row["abandoned"] = row["resubmitted"].get("abandoned", 0)
        row["not_in"] = row["abandoned"] + settled_row["expired"]
        row["not_in_share"] = round(row["not_in"] / offered, 4) if offered else None
        row["failures"] = row["failures"] + settlement_failures(settled_row)
        row["ok"] = not row["failures"]
        _write(out, {"stage": "final", **row})
    deploys_out = out.removesuffix(".jsonl") + "-deploys.jsonl" if out is not None else None
    if deploys_out is not None:
        with open(deploys_out, "w") as deploys:
            for deploy in tracker.deploy_rows():
                deploys.write(json.dumps(deploy) + "\n")

    def total(key: str) -> int:
        return sum(r["settlement"][key] for r in rows)

    failed_steps = [r["phase"] for r in rows if not r["ok"]]
    invalid_steps = [r["phase"] for r in rows if r["invalid"]]
    summary = {
        "stage": "summary",
        "workload": workload.name,
        "stalled_or_broken": bool(failed_steps) or not converged or drain_stall is not None,
        "failed_steps": failed_steps,
        "invalid_steps": invalid_steps,
        "sustained_without_pressure": max(
            (r["rate_target"] for r in rows if r["ok"] and not r["invalid"] and not r["pressure"]),
            default=None,
        ),
        "peak_finalized_rate": max((r["rate_finalized"] for r in rows), default=None),
        "accepted": total("accepted"),
        "finalized": total("finalized"),
        "failed": total("failed"),
        "expired": total("expired"),
        "unsettled": total("unsettled"),
        "refused": sum(r["refused"] for r in rows),
        "abandoned": sum(r["abandoned"] for r in rows),
        "queued_peak": max(
            (
                r["health"]["queued_deploys"]["max"]
                for r in rows
                if r["health"]["queued_deploys"]["max"] is not None
            ),
            default=None,
        ),
        "drained": settled,
        "drain_seconds": round(drained_after, 1),
        "drain_stall": drain_stall,
        "converged_seconds": round(converged_after, 1),
        "converged_lfbs": converged,
        "convergence_failure": convergence_failure,
        "deploy_cost": cost,
        "state_problems": problems,
        "sampler_read_failures": sampler.read_failures,
        "deploys_out": deploys_out,
    }
    _write(out, summary)
    return LoadRun(
        rows=rows,
        summary=summary,
        tracker=tracker,
        node_metrics=node_metrics,
        drain_lfbs=drain_lfbs,
    )


def format_table(rows: List[dict], summary: dict) -> str:
    """The run's summary line and one line per step."""
    lines = [
        "",
        f"LOAD: {summary['workload']}  "
        f"{'STALLED OR BROKE' if summary['stalled_or_broken'] else 'no stall'}; "
        + (
            f"invalid (load generator behind): {', '.join(summary['invalid_steps'])}; "
            if summary["invalid_steps"]
            else ""
        )
        + f"no pressure up to {summary['sustained_without_pressure']}/s, "
        f"peak {summary['peak_finalized_rate']}/s finalized; "
        f"accepted {summary['accepted']}, finalized {summary['finalized']}, "
        f"expired {summary['expired']}, failed {summary['failed']}, "
        f"no outcome {summary['unsettled']}, refused {summary['refused']}, "
        f"abandoned {summary['abandoned']}; queues held up to {summary['queued_peak']} "
        f"(drained in {summary['drain_seconds']}s, converged in {summary['converged_seconds']}s)",
        f"{'step':<22} {'target':>6} {'got':>6} {'fin/s':>6} {'refused':>7} {'resub':>5} "
        f"{'p50':>6} {'p95':>6} {'clientp95':>9} {'expired':>7} {'aband':>6} {'notin%':>6} "
        f"{'failed':>6} {'queued':>6} "
        f"{'spread':>6} {'backlog':>7} {'merge-rejected':>14}  verdict",
    ]
    for r in rows:
        h = r["health"]
        s = r["settlement"]
        verdict = step_status(r) + (
            "; pressure: " + "; ".join(r["pressure"]) if r["pressure"] else ""
        )
        lines.append(
            f"{r['phase']:<22} {r['rate_target']:>6g} {r['rate_achieved']:>6.2f} "
            f"{r['rate_finalized']:>6.2f} {r['refused']:>7} "
            f"{r['resubmitted'].get('accepted', 0):>5} "
            f"{r['finalization_p50']:>6.1f} {r['finalization_p95']:>6.1f} "
            f"{r['client_p95']:>9.1f} "
            f"{s['expired']:>7} {r['abandoned']:>6} "
            f"{'-' if r['not_in_share'] is None else format(100 * r['not_in_share'], '.1f'):>6} "
            f"{s['failed']:>6} {str(h['queued_deploys']['max']):>6} "
            f"{str(h['lfb_spread']['max']):>6} {str(h['ingest_backlog']['max']):>7} "
            f"{r['merge_rejected']:>5} (max {r['merge_rejections_max']:>3})  {verdict}"
        )
    if summary["drain_stall"]:
        lines.append(f"DRAIN STALLED: {summary['drain_stall']}")
    if summary["state_problems"]:
        lines.append(f"STATE PROBLEMS: {summary['state_problems'][:5]}")
    return "\n".join(lines)

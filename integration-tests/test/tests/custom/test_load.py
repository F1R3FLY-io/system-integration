"""
Deploy load through deploy queues: the shard keeps working at its sustained
rate and above it, and every deploy a queue accepts gets an outcome.

Builds the load shard (4 validators, a read-only node) with a deploy queue in
front of each validator and runs the mixed workload (``DEFAULT_MIX`` in
infra/workloads.py) in three steps:

- ``baseline``: 1 deploy/s for BASELINE_SECONDS. Its finality pace is the
  reference the loaded steps are held to.
- ``sustained``: SUSTAINED_RATE, the rate this shard settles without the
  queues refusing, for SUSTAINED_SECONDS.
- ``overload``: twice SUSTAINED_RATE for OVERLOAD_SECONDS. The queues refuse
  what the shard cannot take on time; senders resubmit after the retry-after,
  as clients that honor a refusal do.

Load then stops and the shard drains until every accepted deploy has an
outcome. The test fails when:

- a submission fails other than by a queue's refusal;
- a node logs a finalizer run that exceeded its timeout;
- a loaded step finalizes heights at less than half the baseline's pace;
- the LFB spread across nodes exceeds MAX_LFB_SPREAD while a step runs;
- finality does not advance for STALL_SECONDS while a step runs;
- a node takes longer than MAX_STATUS_SECONDS to answer its status;
- an accepted deploy has no outcome after the drain, the nodes do not
  converge, or the state the finalized deploys produced is wrong;
- a node is not running at the end.

Deploys refused and abandoned, expired, finalized share and client latency
are reported, not judged. A deploy counts as finalized only when every node
reports it Finalized; the time the first node did is reported alongside.

Settings: LOAD_TEST_SUSTAINED_RATE and LOAD_TEST_SUSTAINED_DURATION override
the sustained step's rate and length (the overload step follows the rate);
LOAD_TEST_DEPLOY_MIX overrides the mix weights; LOAD_TEST_NODE_CPUS
("readonly:2") starts named nodes on fewer CPUs; LOAD_TEST_OUT writes every
step, the summary and every deploy as JSON lines.

With ``--provider=remote --inventory <file> --skip-setup --session-id
<shard>`` the test adopts an existing shard instead, signs with the
inventory's funded deployer keys, and leaves the shard running.
"""

import dataclasses
import logging
import os
import re
from typing import Dict, List, Optional, Sequence

import pytest  # type: ignore[import-not-found]

from shardctl.soak_metrics import emit_lfb_spread_metric

from ...infra.config import ShardConfig
from ...infra.keys import (
    VALIDATOR1_ID,
    VALIDATOR2_ID,
    VALIDATOR3_ID,
    VALIDATOR4_ID,
)
from ...infra.load import sender_key, with_funded_senders
from ...infra.load_run import LoadRun, Step, format_table, run_load, step_results
from ...infra.metrics import PhaseReport, format_node_metrics, log_unfinalized
from ...infra.shard import Shard
from ...infra.workloads import DEFAULT_MIX, make_workload

pytestmark = [pytest.mark.xdist_group("custom"), pytest.mark.remote_shard]

BASELINE_RATE = 1.0
BASELINE_SECONDS = 60.0
SUSTAINED_RATE = 2.0
SUSTAINED_SECONDS = 240.0
OVERLOAD_FACTOR = 2.0
OVERLOAD_SECONDS = 60.0
SENDERS = 16
CONTENTION_CELLS = 8
CALL_CONTRACTS = 16
PAYLOAD_BYTES = 262144

MIN_FINALITY_PACE_RATIO = 0.5
MAX_LFB_SPREAD = 5
STALL_SECONDS = 60.0
MAX_STATUS_SECONDS = 2.0

FINALIZER_TIMEOUT_PATTERN = re.compile(r"Finalizer run exceeded timeout|finalizer-run exceeded")

# The per-phase result line. The node summarizer
# (f1r3node-rust scripts/bench/summarize-issue24-phases.sh) parses this exact
# shape, so test_heartbeat_timing prints it through this one constant. Its
# finalization latencies are to the LAST node that reported the deploy
# Finalized.
PHASE_LINE_FORMAT = (
    "  Phase %s: inclusion p50=%.1fs p95=%.1fs, finalization p50=%.1fs p95=%.1fs, "
    "LFB #%d->#%d (%.1f blk/min), unfinalized=%d"
)


def parse_node_cpus(raw: str, available: Sequence[int]) -> Dict[str, frozenset]:
    """Per-node CPU sets for counts like "readonly:2": each named node starts
    on that many of the lowest ``available`` CPUs."""
    cpus = {}
    for part in raw.split(","):
        name, sep, count = part.strip().partition(":")
        if not name or not sep or not count.isdigit() or not 1 <= int(count) <= len(available):
            raise ValueError(
                f"node CPU entry {part!r} must be <node>:<count> with count from 1 to "
                f"{len(available)}"
            )
        if name in cpus:
            raise ValueError(f"node CPU entries name {name!r} twice")
        cpus[name] = frozenset(sorted(available)[: int(count)])
    return cpus


def _acquire_shard(request, provider, timeouts, config: Optional[ShardConfig] = None) -> Shard:
    """The shard to load: adopted under --skip-setup, otherwise built here
    from ``config`` (the load-test shard by default).

    LOAD_TEST_NODE_CPUS ("readonly:2") starts the named nodes of a built shard
    on fewer CPUs, so they derive a narrower processing pipeline.
    """
    node_cpus = os.environ.get("LOAD_TEST_NODE_CPUS", "").strip()
    if request.config.getoption("--skip-setup"):
        if node_cpus:
            raise ValueError("LOAD_TEST_NODE_CPUS applies only to a shard this test builds")
        handles = provider.adopt_session(request.config.getoption("--session-id"))
        return Shard.from_handles(provider, handles, None, timeouts)
    config = config or _load_shard_config()
    if node_cpus:
        config = dataclasses.replace(
            config, per_node_cpus=parse_node_cpus(node_cpus, sorted(os.sched_getaffinity(0)))
        )
    return Shard.create(provider, config, timeouts)


def _load_shard_config() -> ShardConfig:
    """The load-test shard. Shared with test_heartbeat_timing, which must
    measure the same high phase on the same shard."""
    return ShardConfig(
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
        extra_wallets=[
            (
                VALIDATOR4_ID.private_key().get_public_key().get_vault_address(),
                50_000_000_000_000_000,
            )
        ],
    )


def _positive(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _steps() -> List[Step]:
    """The baseline, sustained and overload steps, with the sustained
    step's environment overrides applied."""
    rate = _positive("LOAD_TEST_SUSTAINED_RATE", SUSTAINED_RATE)
    hold = _positive("LOAD_TEST_SUSTAINED_DURATION", SUSTAINED_SECONDS)
    return [
        Step("baseline", BASELINE_RATE, BASELINE_SECONDS),
        Step("sustained", rate, hold),
        Step("overload", rate * OVERLOAD_FACTOR, OVERLOAD_SECONDS),
    ]


def _finalizer_timeouts(nodes) -> Dict[str, int]:
    return {n.name: len(FINALIZER_TIMEOUT_PATTERN.findall(n.logs())) for n in nodes}


def _phase_report(run: LoadRun, row: dict, v1_name: str) -> PhaseReport:
    finality = row["health"]["finality"]
    by_validator = run.node_metrics[row["phase"]]
    return PhaseReport(
        name=row["phase"],
        deploys_submitted=row["settlement"]["accepted"],
        deploy_failures=row["errors"],
        effective_rate=row["rate_achieved"],
        inclusion_p50=row["inclusion_p50"],
        inclusion_p95=row["inclusion_p95"],
        inclusion_p99=row["inclusion_p99"],
        finalization_p50=row["finalization_p50"],
        finalization_p95=row["finalization_p95"],
        finalization_p99=row["finalization_p99"],
        unfinalized=row["unfinalized"],
        lfb_start=finality["start"] or 0,
        lfb_end=finality["end"] or 0,
        lfb_rate_per_min=(finality["advanced"] or 0) / (row["hold_seconds"] / 60),
        phase_duration=row["hold_seconds"],
        node_metrics=by_validator[v1_name],
        node_metrics_by_validator=by_validator,
    )


def _log_phase(run: LoadRun, report: PhaseReport, row: dict) -> None:
    logging.info(
        PHASE_LINE_FORMAT,
        report.name,
        report.inclusion_p50,
        report.inclusion_p95,
        report.finalization_p50,
        report.finalization_p95,
        report.lfb_start,
        report.lfb_end,
        report.lfb_rate_per_min,
        report.unfinalized,
    )
    logging.info(
        "  Phase %s first node: finalization p50=%.1fs p95=%.1fs",
        report.name,
        row["first_node_p50"],
        row["first_node_p95"],
    )
    ids = {r.record.deploy_id for r in step_results(run.tracker, report.name)}
    log_unfinalized(report.name, [u for u in run.tracker.get_unfinalized() if u.deploy_id in ids])
    for v_name, m in report.node_metrics_by_validator.items():
        if m:
            logging.info("  Node internals (%s):\n%s", v_name, format_node_metrics(m))


def _format_report(reports: List[PhaseReport]) -> str:
    lines = [
        "",
        "LOAD TEST RESULTS",
        "=" * 85,
        f"{'Phase':<9} | {'Deploys':>7} | {'Rate':>6} | {'Inclusion (s)':^21} | {'Finalization (s)':^21} | {'LFB':>6}",
        f"{'':9} | {'':>7} | {'(d/s)':>6} | {'p50':>6} {'p95':>6} {'p99':>6} | {'p50':>6} {'p95':>6} {'p99':>6} | {'bl/m':>6}",
        "-" * 85,
    ]
    for r in reports:
        lines.append(
            f"{r.name:<9} | {r.deploys_submitted:>7} | {r.effective_rate:>6.1f} | "
            f"{r.inclusion_p50:>6.1f} {r.inclusion_p95:>6.1f} {r.inclusion_p99:>6.1f} | "
            f"{r.finalization_p50:>6.1f} {r.finalization_p95:>6.1f} {r.finalization_p99:>6.1f} | "
            f"{r.lfb_rate_per_min:>6.1f}"
        )
    lines.append("=" * 85)
    return "\n".join(lines)


def _health_failures(rows: List[dict]) -> List[str]:
    """The degradation checks over the steps' health samples."""
    failures = []
    baseline = rows[0]["health"]["finality"]["secs_per_height"]
    for row in rows:
        health = row["health"]
        finality = health["finality"]
        phase = row["phase"]
        if row["errors"]:
            failures.append(f"{phase}: {row['errors']} submissions failed: {row['submit_errors']}")
        if not row["ok"]:
            failures.append(f"{phase}: {'; '.join(row['failures'])}")
        pace = finality["secs_per_height"]
        if row is not rows[0] and baseline and pace and baseline / pace < MIN_FINALITY_PACE_RATIO:
            failures.append(
                f"{phase}: finality took {pace:.1f}s per height, under "
                f"{MIN_FINALITY_PACE_RATIO:.0%} of the baseline's pace ({baseline:.1f}s)"
            )
        spread = health["lfb_spread"]["max"]
        if spread is not None and spread > MAX_LFB_SPREAD:
            failures.append(f"{phase}: LFB spread reached {spread} (max {MAX_LFB_SPREAD})")
        if finality["longest_stall_seconds"] >= STALL_SECONDS:
            failures.append(
                f"{phase}: finality did not advance for {finality['longest_stall_seconds']:.0f}s"
            )
        slowest = health["status_seconds_max"]
        if slowest is not None and slowest > MAX_STATUS_SECONDS:
            failures.append(f"{phase}: a status read took {slowest:.2f}s")
    return failures


def test_deploy_throughput_and_finalization(
    request, provider, timeouts, resource_monitor, remote_inventory
) -> None:
    """Load the shard through its deploy queues, at and above its sustained rate."""
    steps = _steps()
    if remote_inventory is not None:
        senders = remote_inventory.deploy_keys()
        config = None
    else:
        senders = [sender_key(i) for i in range(SENDERS)]
        config = dataclasses.replace(
            with_funded_senders(_load_shard_config(), senders), deploy_queue=True
        )
    shard = _acquire_shard(request, provider, timeouts, config)
    try:
        logs_readable = provider.node_logs_available
        timeouts_before = _finalizer_timeouts(shard.all_nodes) if logs_readable else {}
        workload = make_workload(
            "mix",
            senders,
            cells=CONTENTION_CELLS,
            contracts=CALL_CONTRACTS,
            payload_bytes=PAYLOAD_BYTES,
            mix=os.environ.get("LOAD_TEST_DEPLOY_MIX", DEFAULT_MIX),
        )
        run = run_load(
            shard,
            senders,
            workload,
            steps,
            timeouts,
            resubmit=True,
            stop_on_failure=False,
            out=os.environ.get("LOAD_TEST_OUT"),
        )
        summary = run.summary

        v1_name = next(iter(shard.named_validators))
        reports = []
        for row in run.rows:
            report = _phase_report(run, row, v1_name)
            reports.append(report)
            _log_phase(run, report, row)
        logging.info(_format_report(reports))
        logging.info(format_table(run.rows, summary))

        failures = _health_failures(run.rows)
        drain = {k: v for k, v in run.drain_lfbs.items() if v is not None}
        if drain:
            drain_spread = max(drain.values()) - min(drain.values())
            logging.info("All-node LFBs at drain: %s (spread %d blocks)", drain, drain_spread)
            emit_lfb_spread_metric(drain_spread)
        else:
            failures.append("no node's LFB could be read when the load stopped")
        if summary["converged_lfbs"]:
            converged = summary["converged_lfbs"]
            logging.info(
                "All-node LFBs converged: %s (spread %d blocks)",
                converged,
                max(converged.values()) - min(converged.values()),
            )

        if logs_readable:
            timeouts_after = _finalizer_timeouts(shard.all_nodes)
            exceeded = {
                name: count - timeouts_before.get(name, 0)
                for name, count in timeouts_after.items()
                if count > timeouts_before.get(name, 0)
            }
            if exceeded:
                failures.append(f"finalizer runs exceeded their timeout: {exceeded}")
        if summary["drain_stall"]:
            failures.append(summary["drain_stall"])
        if summary["unsettled"]:
            failures.append(f"{summary['unsettled']} accepted deploys have no outcome")
        if summary["state_problems"]:
            failures.append(f"inconsistent state: {summary['state_problems'][:5]}")
        if not summary["converged_lfbs"]:
            failures.append(f"the nodes did not converge: {summary['convergence_failure']}")
        failures += [f"{n.name} is not running" for n in shard.all_nodes if not n.is_running()]
        assert not failures, "the shard did not hold up under load:\n  " + "\n  ".join(failures)
    finally:
        shard.destroy()

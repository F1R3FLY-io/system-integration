"""
The soak's deploy load through deploy queues: every deploy finalizes on every
node within the finalization timeout of its phase's end.

Builds the load shard (4 validators, boot, a read-only node) with a deploy
queue in front of each validator and submits light ``@N!(N)`` deploys,
round-robin across the validators, in the soak's phases: low 1/s for 30 s,
medium 5/s for 20 s, high 10/s for 15 s, a burst of 32 within one second,
then sustained 4/s for 300 s.

A deploy a queue refuses is resubmitted after its retry-after, as a client
that honors a refusal does. Its deadline does not move: every deploy of a
phase, refused or not, must be Finalized on every node within
``timeouts.finalization`` of the phase's last scheduled submission. The test
fails when:

- a submission fails other than by a queue's refusal;
- a refused deploy is not accepted before its phase's deadline;
- an accepted deploy is not Finalized on every node by its phase's deadline;
- the nodes' LFBs do not come within 5 heights of each other after the load;
- a node is not running at the end.

Per phase it reports inclusion and finalization latency from the accepted
submission to the last node, client latency from the first attempt, the
refusals, and the pace of finality.

Settings: LOAD_TEST_SUSTAINED_RATE and LOAD_TEST_SUSTAINED_DURATION override
the sustained phase's rate and length; LOAD_TEST_NODE_CPUS ("readonly:2")
starts named nodes on fewer CPUs.

With ``--provider=remote --inventory <file> --skip-setup --session-id
<shard>`` the test adopts an existing shard instead, signs with the
inventory's funded deployer keys, and leaves the shard running.
"""

import dataclasses
import logging
import os
import time
from typing import Callable, Dict, List, Optional, Sequence

import pytest  # type: ignore[import-not-found]

from shardctl.soak_metrics import emit_lfb_spread_metric

from ...infra.config import ShardConfig
from ...infra.keys import (
    VALIDATOR1_ID,
    VALIDATOR2_ID,
    VALIDATOR3_ID,
    VALIDATOR4_ID,
)
from ...infra.load import (
    DEPLOY_KINDS,
    OpenLoopSubmitter,
    PlannedDeploy,
    Resubmitter,
    VabnSource,
    current_block_number,
    node_key_pairs,
    sender_key,
    with_funded_senders,
)
from ...infra.metrics import LifecycleTracker, log_unfinalized, percentiles
from ...infra.polling import lfb_number, wait_for_lfb_converged
from ...infra.shard import Shard

pytestmark = [pytest.mark.xdist_group("custom"), pytest.mark.remote_shard]


@dataclasses.dataclass(frozen=True)
class Phase:
    """``rate`` deploys per second for ``seconds``."""

    name: str
    rate: float
    seconds: float


PHASES = [
    Phase("low", 1.0, 30.0),
    Phase("medium", 5.0, 20.0),
    Phase("high", 10.0, 15.0),
    Phase("burst", 32.0, 1.0),
    Phase("sustained", 4.0, 300.0),
]

# Each queue limits one deployer to a share of its capacity, so the load is
# signed by several senders per validator, as independent clients would.
SENDERS = 16
MAX_LFB_SPREAD = 5

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


def _phases() -> List[Phase]:
    """PHASES with the sustained phase's environment overrides applied."""
    rate = _positive("LOAD_TEST_SUSTAINED_RATE", PHASES[-1].rate)
    seconds = _positive("LOAD_TEST_SUSTAINED_DURATION", PHASES[-1].seconds)
    return PHASES[:-1] + [dataclasses.replace(PHASES[-1], rate=rate, seconds=seconds)]


def _run_phase(
    phase: Phase,
    first_index: int,
    plan,
    vabn: VabnSource,
    tracker: LifecycleTracker,
    timeout,
    on_submitted: Callable[[], None],
) -> dict:
    """Submit one phase, call ``on_submitted`` once its schedule has run,
    resubmit refusals until the phase's deadline, then wait out the deadline
    for every deploy to finalize on every node. Returns the phase's submission
    counts and failures."""
    resubmitter = Resubmitter(OpenLoopSubmitter.workers_for(phase.rate), vabn, tracker.track_deploy)
    try:
        stats = OpenLoopSubmitter(OpenLoopSubmitter.workers_for(phase.rate)).run(
            phase.rate,
            phase.seconds,
            first_index,
            plan,
            vabn,
            phase.name,
            tracker.track_deploy,
            retry_refusals=False,
            on_refused=resubmitter.refused,
        )
        deadline = time.time() + timeout
        on_submitted()
        resubmitter.wait_idle(max(0.0, deadline - time.time()))
    finally:
        resubmitter.close()
    resubmitted = resubmitter.outcomes.get(
        phase.name, {"accepted": 0, "abandoned": 0, "error": 0, "attempts": 0}
    )
    tracker.wait_for_finalization(timeout=max(0.0, deadline - time.time()))
    failures = [f"{phase.name}: {e}" for e in stats.errors]
    if resubmitted["error"]:
        failures.append(f"{phase.name}: {resubmitted['error']} resubmissions failed")
    if resubmitted["abandoned"]:
        failures.append(
            f"{phase.name}: {resubmitted['abandoned']} refused deploys were not accepted "
            f"within {timeout}s of the phase's end"
        )
    return {
        "submitted": stats.submitted,
        "achieved_rate": stats.achieved_rate,
        "refused": stats.refused,
        "resubmitted": resubmitted,
        "failures": failures,
    }


def _log_phase(phase: Phase, tracker: LifecycleTracker, submission: dict, lfbs, seconds) -> int:
    """Log the phase's result lines; return how many of its accepted deploys
    were not Finalized on every node."""
    results = tracker.get_results()
    inclusion = [r.inclusion_time for r in results if r.inclusion_time is not None]
    finalized = [r for r in results if r.finalization_time is not None]
    finalization = [r.finalization_time for r in finalized]
    first_node = [r.first_finalization_time for r in finalized]
    client = [
        r.record.submit_time + r.finalization_time - r.record.first_attempt_time for r in finalized
    ]
    unfinalized = len(results) - len(finalized)
    inc_p50, inc_p95 = percentiles(inclusion, [50, 95])
    fin_p50, fin_p95 = percentiles(finalization, [50, 95])
    first_p50, first_p95 = percentiles(first_node, [50, 95])
    client_p50, client_p95, client_max = percentiles(client, [50, 95, 100])
    lfb_start, lfb_end = lfbs
    logging.info(
        PHASE_LINE_FORMAT,
        phase.name,
        inc_p50,
        inc_p95,
        fin_p50,
        fin_p95,
        lfb_start,
        lfb_end,
        (lfb_end - lfb_start) / seconds * 60 if seconds > 0 else 0.0,
        unfinalized,
    )
    logging.info(
        "  Phase %s first node: finalization p50=%.1fs p95=%.1fs",
        phase.name,
        first_p50,
        first_p95,
    )
    resubmitted = submission["resubmitted"]
    logging.info(
        "  Phase %s queues: %d accepted first time at %.2f/s, %d refused, %d accepted on "
        "resubmission (%d attempts), %d abandoned; client latency p50=%.1fs p95=%.1fs "
        "max=%.1fs",
        phase.name,
        submission["submitted"],
        submission["achieved_rate"],
        submission["refused"],
        resubmitted["accepted"],
        resubmitted["attempts"],
        resubmitted["abandoned"],
        client_p50,
        client_p95,
        client_max,
    )
    log_unfinalized(phase.name, tracker.get_unfinalized())
    return unfinalized


def test_deploy_throughput_and_finalization(
    request, provider, timeouts, resource_monitor, remote_inventory
) -> None:
    """The soak's phases through deploy queues, each deploy finalized on every
    node within the finalization timeout of its phase's end."""
    phases = _phases()
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
        validators = list(shard.named_validators.values())
        v1 = validators[0]
        readonly = shard.readonly
        if readonly is None:
            raise AssertionError("the load test needs a read-only node to read the tip")
        wait_for_lfb_converged(
            shard.all_nodes,
            timeout=timeouts.finalization * 2,
            min_height=1,
            max_spread=MAX_LFB_SPREAD,
            description="finalized baseline before the load",
        )
        pairs = node_key_pairs(validators, senders)
        light_term, light_phlo = DEPLOY_KINDS["light"]

        def plan(index: int) -> PlannedDeploy:
            node, key = pairs[index % len(pairs)]
            return PlannedDeploy(node, key, light_term(index), light_phlo, "light")

        vabn = VabnSource(validators, resource_monitor)
        tracker = LifecycleTracker(
            {n.name: n for n in shard.all_nodes}, inclusion_timeout=timeouts.deploy_inclusion
        )
        tracker.start_lfb_monitor()
        failures: List[str] = []
        total_unfinalized = 0
        index = 100_000
        try:
            for phase in phases:
                logging.info(
                    "--- Phase: %s (%g/s for %gs) ---", phase.name, phase.rate, phase.seconds
                )
                tracker.clear()
                lfb_start = lfb_number(v1)
                phase_start = time.time()

                def log_cone(name: str = phase.name) -> None:
                    tip = current_block_number(readonly, resource_monitor)
                    lfb = lfb_number(v1)
                    logging.info(
                        "  Cone at end of %s submission: tip-LFB lag = %d blocks "
                        "(tip #%d, LFB #%d)",
                        name,
                        tip - lfb,
                        tip,
                        lfb,
                    )

                submission = _run_phase(
                    phase, index, plan, vabn, tracker, timeouts.finalization, log_cone
                )
                index += int(phase.rate * phase.seconds)
                failures += submission["failures"]
                lfb_end = lfb_number(v1)
                unfinalized = _log_phase(
                    phase, tracker, submission, (lfb_start, lfb_end), time.time() - phase_start
                )
                if unfinalized:
                    failures.append(
                        f"{phase.name}: {unfinalized} deploy(s) not finalized within "
                        f"{timeouts.finalization}s"
                    )
                total_unfinalized += unfinalized
        finally:
            tracker.shutdown()

        drain_lfbs = {n.name: lfb_number(n) for n in shard.all_nodes}
        drain_spread = max(drain_lfbs.values()) - min(drain_lfbs.values())
        logging.info("All-node LFBs at drain: %s (spread %d blocks)", drain_lfbs, drain_spread)
        emit_lfb_spread_metric(drain_spread)
        try:
            converged = wait_for_lfb_converged(
                shard.all_nodes,
                timeout=timeouts.finalization * 3,
                max_spread=MAX_LFB_SPREAD,
                description=f"all-node LFB spread <= {MAX_LFB_SPREAD} after load drains",
            )
            logging.info(
                "All-node LFBs converged: %s (spread %d blocks)",
                converged,
                max(converged.values()) - min(converged.values()),
            )
        except AssertionError as exc:
            failures.append(f"the nodes did not converge: {exc}")
        failures += [f"{n.name} is not running" for n in shard.all_nodes if not n.is_running()]
        assert not failures, (
            f"{total_unfinalized} deploy(s) not finalized within {timeouts.finalization}s; "
            "the shard did not hold the soak's load:\n  " + "\n  ".join(failures)
        )
    finally:
        shard.destroy()

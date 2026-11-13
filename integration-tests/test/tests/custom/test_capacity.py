"""Shard capacity under load: whether the shard keeps working, and what it
does with the deploys it is offered.

Ramps one workload through increasing rates, holding each for
CAPACITY_HOLD_SECONDS. The run fails only when the shard stalls or breaks:

- finality stops advancing during a step, or the shard's health cannot be
  read;
- a submission fails other than by a deploy queue's refusal;
- after the ramp, a node does not converge again, an accepted deploy has no
  outcome, or the state the finalized deploys produced is wrong.

Everything else is measured and reported: refusals, deploys finalized,
failed in execution or expired, latency, and pressure, meaning the LFB spread,
the validators' tip-minus-LFB depth or the block-ingest backlog still growing
when a step ended. A step whose submitter fell behind its target rate is
invalid rather than a pass: the shard's own slowness would otherwise lower the
load it is measured under. The ramp continues until a step fails; the highest
rate without pressure is reported as sustained. Load then stops, the shard
drains until finality passes every window the ramp signed, and the workload
checks the state its finalized deploys produced. Finality that stops
advancing while draining is a stall. Every deploy's record is written beside
CAPACITY_OUT, as ``<out>-deploys.jsonl``.

The shard this test builds is test_load's, with a deploy queue in front of
each validator. CAPACITY_RESUBMIT=1 makes the senders behave as clients that
honor a refusal: a refused deploy is signed again and resubmitted after its
retry-after, until accepted or abandoned after REFUSAL_PATIENCE_SECONDS.

Opt in with CAPACITY_WORKLOAD (transfer, contention, nonce, install, call,
payload, light, compute, data, heavy, or mix). ``mix`` interleaves workloads by
the weights in CAPACITY_MIX (default ``DEFAULT_MIX`` in infra/workloads.py), so
every rate carries the same blend. Each sender submits to one validator;
CAPACITY_SPREAD=1 sends each sender's consecutive deploys to different
validators instead. Other settings: CAPACITY_RATES
("1,2,3,4,6,8"), CAPACITY_HOLD_SECONDS (600), CAPACITY_CELLS (8, contention),
CAPACITY_CONTRACTS (16, call), CAPACITY_PAYLOAD_BYTES (262144, payload),
CAPACITY_SENDERS (32, a shard this test builds), CAPACITY_OUT (JSON lines,
one row per step), CAPACITY_SKIP_BASELINE=1 (start without waiting for every
node to converge, for an adopted shard whose observers lag).

Against an existing shard (``--provider=remote --inventory <file>
--skip-setup --session-id <shard>``) the inventory's deployers are the
senders.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import time
from typing import Tuple

import pytest  # type: ignore[import-not-found]

from ...infra.load import sender_key, with_funded_senders
from ...infra.load_run import Step, format_table, run_load
from ...infra.workloads import DEFAULT_MIX, make_workload
from .test_load import _acquire_shard, _load_shard_config

pytestmark = [
    pytest.mark.xdist_group("custom"),
    pytest.mark.remote_shard,
    pytest.mark.skipif(
        not os.environ.get("CAPACITY_WORKLOAD"),
        reason="capacity ramps run for an hour or more; set CAPACITY_WORKLOAD to run one",
    ),
]


@dataclasses.dataclass(frozen=True)
class Settings:
    workload: str
    rates: Tuple[float, ...]
    hold: float
    cells: int
    contracts: int
    payload_bytes: int
    senders: int
    mix: str
    spread: bool
    resubmit: bool
    skip_baseline: bool
    out: str

    @classmethod
    def from_env(cls) -> "Settings":
        workload = os.environ["CAPACITY_WORKLOAD"].strip()
        rates = tuple(float(r) for r in os.environ.get("CAPACITY_RATES", "1,2,3,4,6,8").split(","))
        if not rates or any(r <= 0 for r in rates) or list(rates) != sorted(rates):
            raise ValueError("CAPACITY_RATES must be positive and increasing")
        return cls(
            workload=workload,
            rates=rates,
            hold=float(os.environ.get("CAPACITY_HOLD_SECONDS", "600")),
            cells=int(os.environ.get("CAPACITY_CELLS", "8")),
            contracts=int(os.environ.get("CAPACITY_CONTRACTS", "16")),
            payload_bytes=int(os.environ.get("CAPACITY_PAYLOAD_BYTES", "262144")),
            senders=int(os.environ.get("CAPACITY_SENDERS", "32")),
            mix=os.environ.get("CAPACITY_MIX", DEFAULT_MIX),
            spread=os.environ.get("CAPACITY_SPREAD", "") == "1",
            resubmit=os.environ.get("CAPACITY_RESUBMIT", "") == "1",
            skip_baseline=os.environ.get("CAPACITY_SKIP_BASELINE", "") == "1",
            out=os.environ.get("CAPACITY_OUT", f"capacity-{workload}-{int(time.time())}.jsonl"),
        )


def test_capacity_ramp(request, provider, timeouts, remote_inventory) -> None:
    settings = Settings.from_env()
    if remote_inventory is not None:
        senders = remote_inventory.deploy_keys()
        config = None
    else:
        senders = [sender_key(i) for i in range(settings.senders)]
        config = dataclasses.replace(
            with_funded_senders(_load_shard_config(), senders), deploy_queue=True
        )
    shard = _acquire_shard(request, provider, timeouts, config)
    try:
        workload = make_workload(
            settings.workload,
            senders,
            settings.cells,
            settings.contracts,
            settings.payload_bytes,
            settings.mix,
        )
        run = run_load(
            shard,
            senders,
            workload,
            [Step(f"{workload.name}@{rate:g}", rate, settings.hold) for rate in settings.rates],
            timeouts,
            resubmit=settings.resubmit,
            stop_on_failure=True,
            spread=settings.spread,
            out=settings.out,
            wait_for_baseline=not settings.skip_baseline,
        )
        summary = run.summary
        logging.info(format_table(run.rows, summary))
        assert summary["drain_stall"] is None, summary["drain_stall"]
        assert not summary["state_problems"], (
            f"the finalized deploys left inconsistent state: {summary['state_problems'][:5]}"
        )
        assert summary["converged_lfbs"], (
            f"the nodes did not converge again after the load stopped: "
            f"{summary['convergence_failure']}"
        )
        assert not summary["failed_steps"], (
            f"the shard stalled or broke in {summary['failed_steps']}"
        )
    finally:
        shard.destroy()

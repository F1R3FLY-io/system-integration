# test_load

## Purpose

Measures deploy throughput and finalization latency under increasing load, ending in a sustained phase long enough to build a deep un-finalized cone. It is a benchmark, not a latency gate: latency is measured and reported, and the hard assertions cover correctness only (no submission failures, every deploy finalized on every node, all-node LFB convergence, no crashes).

## Test phases

| Phase | Rate | Duration | Total deploys |
|-------|------|----------|---------------|
| low | 1/sec | 30s | 30 |
| medium | 5/sec | 20s | 100 |
| high | 10/sec | 15s | 150 |
| burst | instant | - | 32 |
| sustained | 4/sec | 300s | 1200 |

Deploys are submitted round-robin across all 4 validators, each signed with that validator's key. The burst phase submits concurrently through a `ThreadPoolExecutor`; the rated phases submit on a fixed schedule. Lightweight contracts (`@N!(N)`) stress the deploy pipeline and block production, not the Rholang interpreter.

Each deploy carries a `validAfterBlockNumber` read from the DAG tip, refreshed every 5s. A deploy rejected for vabn expiration is retried once with the height the rejection reports. No other failure is retried, because the first submission may have landed.

## How measurement works

### LifecycleTracker

`LifecycleTracker` in `infra/metrics.py` runs one status-sweep thread per node of the shard: all validators, boot, and readonly. Each thread polls its own node's `deploy_finalization_status` about once a second, for the deploys that node has not yet reported. A node stops polling a deploy once it has reported it Finalized.

- **Inclusion** is recorded when any node first reports a containing block.
- **Finalization** is recorded only when every node reports the deploy Finalized. The reported latency is the last node's report time minus the submit time.
- **First-node finalization** is kept too, so the cross-node spread is visible in the phase log.
- A terminal Failed or Expired from any node settles the deploy as unfinalized.

The status API follows a deploy across merge rejection and re-homing, so no block-number inference is involved.

### Cone depth

At the end of each phase's submission, before the drain wait, the test logs `tip - LFB` on validator1. A value that climbs across phases is finalization falling behind block production.

### Prometheus metrics

Before and after each phase, the test scrapes `/metrics` on every validator. `compute_metric_deltas()` turns the difference into per-stage costs for the phase, which `format_node_metrics()` logs per validator.

### Report format

```
LOAD TEST RESULTS
=====================================================================================
Phase    | Deploys |   Rate |     Inclusion (s)     |   Finalization (s)    |    LFB
         |         |  (d/s) |    p50    p95    p99 |    p50    p95    p99 |   bl/m
-------------------------------------------------------------------------------------
low      |      30 |    1.0 |    5.5    9.0    9.5 |   25.9   30.4   31.1 |    7.1
...
```

Finalization columns are all-node figures. Each phase logs its result line in the shape `PHASE_LINE_FORMAT` pins, which f1r3node-rust's `summarize-issue24-phases.sh` parses; its finalization latencies are to the last node. A second line, `Phase <name> first node: finalization p50=… p95=…`, follows it.

## Tests (1)

### test_deploy_throughput_and_finalization

Runs all 5 phases sequentially on a fresh shard. After the phases:

- Asserts zero deploy submission failures.
- Asserts every deploy was finalized on every node within the finalization timeout.
- Waits for all-node LFB convergence (spread at most 5 blocks) within 3x the finalization timeout, since nodes are still catching up when load stops.
- Asserts every node is still running.

Set `LOAD_TEST_TELEMETRY_ONLY=1` to skip the finalization and convergence gates and collect metrics past the shard's capacity. The submission-failure and running-node assertions still apply.

Set `LOAD_TEST_DEPLOY_DUMP=<path>` to append one JSON line per deploy after each phase: phase, submitting validator, submit and inclusion times, each node's Finalized report time, and any terminal state. Times are epoch seconds.

Set `LOAD_TEST_LATE_CAP_SECONDS=<n>` to keep following, after the last phase, every deploy not finalized on every node within its phase, for up to `n` seconds. The test then logs a `LATE OUTCOMES` block per phase: how many later finalized on every node and with what latency, and what each unsettled node last reported. With `LOAD_TEST_DEPLOY_DUMP` set, those outcomes are appended as `"stage": "late"` lines. Phase results and gates are unaffected.

Set `LOAD_TEST_SUSTAINED_RATE=<deploys/sec>` to replace the sustained phase's 4/sec, for capacity probing.

## Setup

- **Topology**: 4 validators (100/100/100/100 bonds), boot, and readonly: 6 nodes.
- **FTT**: From `conf/rust.conf`.
- **Heartbeat**: Enabled. Cadence, frontier-chase cap and deploys per block come from the node defaults and `conf/rust.conf`; the test sets no node flags of its own.

## What it proves

- The deploy pipeline accepts load up to 10/sec, and a 300s sustained phase, without submission failures.
- Every deploy reaches finalization on every node, not only on the node it was submitted to.
- The shard converges after load and no node crashes.
- It gives per-phase latency and per-stage node metrics for regression comparison.

## Key assertions

- `total_failures == 0`: every deploy was submitted.
- `total_unfinalized == 0`: every deploy finalized on every node within the timeout (skipped in telemetry-only mode).
- `wait_for_lfb_converged(shard.all_nodes, max_spread=5)` (skipped in telemetry-only mode).
- `node.is_running()` for every node.

## Infrastructure used

- `Shard.create()` / `shard.destroy()` lifecycle
- `Node.deploy_string()`, `Node.get_blocks()`, `Node.is_running()`
- `LifecycleTracker`, `DeployRecord`, `PhaseReport`, `percentiles()`, `scrape_metrics()`, `compute_metric_deltas()`, `format_node_metrics()` from `infra/metrics.py`
- `lfb_number()`, `parse_vabn_expiration()`, `poll_until()`, `wait_for_lfb_converged()` from `infra/polling.py`
- Framework timeouts (`timeouts.deploy_inclusion`, `timeouts.finalization`)

## Related

- [test_shard_degradation](test_shard_degradation.md): sustained load test with degradation detection
- [test_bridge_admin](test_bridge_admin.md): sustained load with complex contract interactions

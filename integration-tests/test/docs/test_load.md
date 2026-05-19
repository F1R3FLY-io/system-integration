# test_load

## Purpose

Measures deploy throughput and finalization latency under increasing load to identify the shard's capacity limits. This is a benchmarking test, not a pass/fail gate on latency -- the point is to measure and report, with hard assertions only on correctness (zero failures, all deploys finalized, no crashes).

## Test phases

| Phase | Rate | Duration | Workers | Total deploys |
|-------|------|----------|---------|---------------|
| low | 1/sec | 30s | 1 | 30 |
| medium | 5/sec | 20s | 3 | 100 |
| high | 10/sec | 15s | 3 | 150 |
| burst | instant | - | 3 | 32 |

Each phase submits deploys at the specified rate distributed across 3 validators via `ThreadPoolExecutor`. Lightweight contracts (`@N!(N)`) stress the deploy pipeline, not the Rholang interpreter.

## How measurement works

### LifecycleTracker

A background tracking system with two components:
1. **Inclusion tracker**: For each deploy, a background thread polls `find_deploy()` until the deploy appears in a block. Records `inclusion_time = find_time - submit_time`.
2. **LFB monitor**: A single background thread continuously polls `last_finalized_block()`. When the LFB passes a deploy's block number, records `finalization_time = lfb_time - submit_time`.

This concurrent polling measures actual latency, not serialized submission + polling time.

### Prometheus metrics

Between phases, the test scrapes the node's `/metrics` endpoint via `scrape_metrics()` from `infra/metrics.py`. Delta computation via `compute_metric_deltas()` gives per-block averages for each phase, identifying which internal stages are bottlenecks under load. The extended metrics list includes per-deploy replay breakdown, runtime spawn, and RSpace operations. Results are formatted via `format_node_metrics()`.

### Report format

```
LOAD TEST RESULTS
=====================================================================================
Phase    | Deploys |   Rate | Inclusion (s)         | Finalization (s)      |    LFB
         |         |  (d/s) |   p50   p95   p99     |   p50   p95   p99     |  bl/m
-------------------------------------------------------------------------------------
low      |      30 |    1.0 |   3.2   5.1   6.0     |   8.4  12.3  14.1     |   7.2
...
```

## Tests (1)

### test_deploy_throughput_and_finalization

Runs all 4 phases sequentially on a fresh 3-validator shard with readonly observer. After all phases:
- Asserts zero deploy submission failures
- Asserts all deploys were finalized within the timeout
- Asserts readonly LFB is within 5 blocks of V1 LFB (readonly gap check)
- Asserts all nodes (including readonly) are still running via `is_running()` (no crashes)
- Logs the full results table and per-phase node metrics

## Setup

- **Topology**: Custom 3-validator shard (100/100/100 bonds) + readonly observer
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled (for automatic block inclusion)
- **include_readonly**: True

## What it proves

- The deploy pipeline handles increasing load up to 10/sec without failures
- All deploys are finalized within bounded time
- No node crashes under load
- Provides baseline metrics for performance regression detection

## Key assertions

- `total_failures == 0` -- all deploys submitted successfully
- `total_unfinalized == 0` -- all deploys finalized within timeout
- Readonly LFB gap <= 5 blocks behind V1
- `node.is_running()` for all nodes (including readonly) -- no crashes

## Infrastructure used

- `Shard.create()` / `shard.destroy()` lifecycle
- `Node.deploy_string()`, `Node.find_deploy()`, `Node.last_finalized_block()`, `Node.get_current_block_number()`, `Node.is_running()`
- `LifecycleTracker`, `DeployRecord`, `PhaseReport` from `infra/metrics.py` for background inclusion/finalization monitoring
- `scrape_metrics()`, `compute_metric_deltas()`, `format_node_metrics()`, `percentiles()` from `infra/metrics.py`
- `ThreadPoolExecutor` for concurrent deploy submission
- Framework timeouts (`timeouts.deploy_inclusion`, `timeouts.finalization`) instead of hardcoded values

## Related

- [test_shard_degradation](test_shard_degradation.md) -- sustained load test with degradation detection
- [test_bridge_admin](test_bridge_admin.md) -- sustained load with complex contract interactions

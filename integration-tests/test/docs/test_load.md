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

Between phases, the test scrapes the node's `/metrics` endpoint for histogram data (validation step times, DAG merge times, replay phase times). Delta computation gives per-block averages for each phase, identifying which internal stages are bottlenecks under load.

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

Runs all 4 phases sequentially on a fresh 3-validator shard. After all phases:
- Asserts zero deploy submission failures
- Asserts all deploys were finalized within the timeout
- Asserts all nodes are still running (no crashes)
- Logs the full results table and per-phase node metrics

## Setup

- **Topology**: Custom 3-validator shard (100/100/100 bonds)
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled (for automatic block inclusion)

## What it proves

- The deploy pipeline handles increasing load up to 10/sec without failures
- All deploys are finalized within bounded time
- No node crashes under load
- Provides baseline metrics for performance regression detection

## Key assertions

- `total_failures == 0` -- all deploys submitted successfully
- `total_unfinalized == 0` -- all deploys finalized within timeout
- `node.is_running()` for all nodes -- no crashes

## Infrastructure used

- `Shard.create()` / `shard.destroy()` lifecycle
- `Node.deploy_string()`, `Node.find_deploy()`, `Node.last_finalized_block()`, `Node.get_current_block_number()`
- `Node.http_url` for Prometheus metrics scraping
- `ThreadPoolExecutor` for concurrent deploy submission
- `LifecycleTracker` for background inclusion/finalization monitoring

## Related

- [test_shard_degradation](test_shard_degradation.md) -- sustained load test with degradation detection
- [test_bridge_admin](test_bridge_admin.md) -- sustained load with complex contract interactions
- [Shard Degradation](../../../docs/shard-degradation-context.md) -- known finalizer timeout issues

# test_shard_degradation

## Purpose

Production-readiness gate: deploys 150 non-trivial Rholang contracts across 3 validators and asserts the shard maintains acceptable performance throughout. This test detects degradation patterns that only appear under sustained load -- finalizer timeouts, LFB stalls, validator desync, and API latency spikes.

## How it works

150 contracts are deployed in batches of 10. Every 3rd deploy is `bridge-v2.rho` (complex persistent contract with registry operations); the rest cycle through 6 contract types: registry insert/lookup, map operations, channel joins, nested pattern matching, recursive loops, and set operations.

After each batch, the test pauses 30 seconds for propagation and measures:
- **LFB numbers** on all 5 nodes (boot, 3 validators, readonly)
- **Finalizer timeout count** from container logs
- **API latency** (time to call `last_finalized_block()`)
- **Validator desync** (max LFB - min LFB across nodes)
- **LFB stalls** (consecutive batches with no LFB advancement)

After all 150 deploys, 10 sampled deploys are checked for inclusion and finalization latency.

## Strict assertions (8)

| # | Assertion | Threshold |
|---|-----------|-----------|
| 1 | Deploy send failures | 0 |
| 2 | Finalizer timeouts | 0 |
| 3 | LFB rate degradation | Must stay above 50% of initial rate |
| 4 | Validator desync | Max 5 blocks |
| 5 | LFB stalls | Max 1 consecutive batch |
| 6 | Deploy inclusion | All sampled deploys included within `timeouts.deploy_inclusion` |
| 7 | Deploy finalization | All included deploys finalized within `timeouts.finalization` |
| 8 | API latency | Under 2s |

## Setup

- **Topology**: Custom 3-validator shard + readonly (5 nodes total)
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled
- **Deploy count**: 150 (15 batches of 10)
- **Timeout**: 30 minutes

## Contract types

| Type | Description | Phlo |
|------|-------------|------|
| bridge | Full bridge-v2.rho with registry, vault, admin API | 500M |
| registry | insertArbitrary + lookup | 500K |
| map | create, set, delete | 500K |
| channel_join | 3-way channel join + computation | 500K |
| nested | Nested pattern matching with records | 500K |
| recursive | Tail-recursive loop (depth=50) | 500K |
| set_operations | Set creation, union, diff | 500K |

## What it proves

- The shard handles sustained mixed-complexity load without degradation
- LFB rate remains stable (no progressive slowdown)
- Validators stay in sync (no permanent fork divergence)
- No finalizer timeouts (the most common degradation symptom)
- API remains responsive under load
- Deploy pipeline handles bridge-v2.rho (the heaviest real-world contract) repeatedly

## Infrastructure used

- `ShardConfig` with `include_readonly=True`
- `Shard.create()` / `shard.destroy()` lifecycle
- `Node.deploy_string()`, `Node.deploy_rho_file()`, `Node.find_deploy()`, `Node.last_finalized_block()`, `Node.logs()`, `Node.is_running()`
- `scrape_metrics()`, `compute_metric_deltas()`, `format_node_metrics()` from `infra/metrics.py` for Prometheus metrics (scraped before/after full test)
- Framework timeouts (`timeouts.deploy_inclusion`, `timeouts.finalization`) instead of hardcoded `MAX_DEPLOY_INCLUSION_SECS`/`MAX_DEPLOY_FINALIZATION_SECS`
- Node alive check via `is_running()` on all nodes (including readonly) after test
- Manual `time.sleep()` for deploy pacing and batch propagation (intentional, not polling)

## Related

- [test_load](test_load.md) -- throughput/latency measurement (complementary)
- [test_bridge_admin](test_bridge_admin.md) -- sustained bridge admin API calls
- [Shard Degradation context](../../../docs/shard-degradation-context.md) -- known finalizer timeout issues

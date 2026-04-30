# Integration Test Catalog

Every test file in the framework, grouped by directory, with a link to its detailed doc. Maintained manually — if you add/remove a test file, update this table.

For a file's full spec (purpose, setup, assertions), click the test name. For how to **write** a new test, see [WRITING_TESTS.md](WRITING_TESTS.md). For framework **internals**, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## `tests/shared/` — session-scoped 3-validator shard

The `shared_shard` fixture brings up bootstrap + 3 validators + readonly once per pytest session. These tests are read-only / non-invasive; they share the same shard via `pytest.mark.xdist_group("shared")`.

| Tests | File | Summary |
|---|---|---|
| 2 | [test_bonding_validators](test_bonding_validators.md) | V4 first bond + V5 second-bond succession (8-phase lifecycle on shared shard) |
| 2 | [test_bridge_admin](test_bridge_admin.md) | Bridge contract deploy, URI registration, query API |
| 3 | [test_convergence](test_convergence.md) | Network recovery from DAG tip divergence; FT convergence across nodes |
| 1 | [test_dag_correctness](test_dag_correctness.md) | Multi-parent DAG structural correctness; determinism + FT caching regression |
| 4 | [test_deployment](test_deployment.md) | Deploy lifecycle: syntax validation, phlo errors, cross-validator lookup |
| 1 | [test_genesis_ceremony](test_genesis_ceremony.md) | Genesis ceremony completion validation across all nodes |
| 2 | [test_heartbeat](test_heartbeat.md) | Heartbeat proposer creates blocks when LFB goes stale |
| 12 | [test_query_endpoints](test_query_endpoints.md) | HTTP query endpoints (balance, validators, epoch, estimate-cost, etc.) |
| 2 | [test_storage](test_storage.md) | Registry-based data storage + cross-node retrieval after finalization |
| 5 | [test_token_metadata](test_token_metadata.md) | Native token metadata on the shared shard (happy path + cross-shard checks) |
| 5 | [test_wallets](test_wallets.md) | PoS vault transfers, authorization failures, insufficient funds, Block API transfers |
| 23 | [test_web_api](test_web_api.md) | HTTP API: strict assertions, cross-node consistency, views, status, bond-status |

**Total: 62 tests across 12 files.**

---

## `tests/custom/` — per-test custom shards

Each test builds its own `ShardConfig` and calls `provider.create_shard(...)`. Used for asymmetric bonds, validator failures, bonding/unbonding, trim-state sync, load testing, and anything that mutates shard-wide state. Marked `pytest.mark.xdist_group("custom")`.

| Tests | File | Summary |
|---|---|---|
| 4 | [test_asymmetric_bonds](test_asymmetric_bonds.md) | Consensus with unequal stake weights (60/20/15) |
| 5 | [test_consensus_safety](test_consensus_safety.md) | Consensus safety under validator failure, FTT boundaries, epochs |
| 1 | [test_load](test_load.md) | Deploy throughput + finalization latency benchmark |
| 1 | [test_shard_degradation](test_shard_degradation.md) | Production-readiness gate: 150 deploys, sustained load |
| 1 | [test_synchrony_constraint](test_synchrony_constraint.md) | Per-validator synchrony constraint threshold enforcement |
| 1 | [test_trim_state](test_trim_state.md) | Joiner syncs from Last Finalized State instead of replaying genesis |
| 6 | [test_websocket](test_websocket.md) | `/ws/events` block, genesis, transfer, lifecycle events + startup replay |

**Total: 19 tests across 7 files.**

---

## `tests/standalone/` — per-test standalone nodes

Each test spins up a single node with no peers. Used for heartbeat timing, standalone propose, and isolated node behavior. No xdist group — tests are fully parallelizable (each worker gets its own node).

| Tests | File | Summary |
|---|---|---|
| 2 | [test_heartbeat](test_heartbeat.md) | Standalone heartbeat config: idle block creation, disabled when max-parents=1 |
| 1 | [test_propose](test_propose.md) | Deploy phlo price validation with custom `--min-phlo-price` |
| 11 | [test_token_metadata](test_token_metadata.md) | Native token metadata standalone: config validation, restart drift, genesis blocking |

**Total: 14 tests across 3 files.**

---

## Deferred / unimplemented

Items known but not covered by the framework today. See [deferred-test-coverage.md](deferred-test-coverage.md) for the full list and rationale.

---

## Summary

| Directory | Files | Tests |
|---|---|---|
| `tests/shared/` | 12 | 62 |
| `tests/custom/` | 7 | 19 |
| `tests/standalone/` | 3 | 14 |
| **Total** | **22** | **95** |

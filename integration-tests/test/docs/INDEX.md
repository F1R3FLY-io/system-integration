# Integration Test Catalog

Every test file in the framework, grouped by directory, with a link to its detailed doc. Maintained manually — if you add/remove a test file, update this table.

For a file's full spec (purpose, setup, assertions), click the test name. For how to **write** a new test, see [WRITING_TESTS.md](WRITING_TESTS.md). For framework **internals**, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## `tests/shared/` — session-scoped 3-validator shard

The `shared_shard` fixture brings up bootstrap + 3 validators + readonly once per pytest session. These tests are read-only / non-invasive; they share the same shard via `pytest.mark.xdist_group("shared")`.

| Tests | File | Summary |
|---|---|---|
| 1 | [test_bonding_validators](test_bonding_validators.md) | V4 first bond + V5 second-bond succession + Phase C observer LFS-sync, with background load throughout |
| 2 | [test_bridge_admin](test_bridge_admin.md) | Bridge contract deploy, URI registration, query API |
| 7 | [test_contract_lifecycle](test_contract_lifecycle.md) | Multi-contract parallel deploy + cross-node state agreement + contract-to-contract interaction + multi-block state evolution under merge |
| 3 | [test_convergence](test_convergence.md) | Network recovery from DAG tip divergence; FT convergence across nodes |
| 1 | [test_dag_correctness](test_dag_correctness.md) | Multi-parent DAG structural correctness; determinism + FT caching regression |
| 4 | [test_deployment](test_deployment.md) | Deploy lifecycle: syntax validation, phlo errors, cross-validator lookup |
| 1 | [test_genesis_ceremony](test_genesis_ceremony.md) | Genesis ceremony completion validation across all nodes |
| 2 | [test_heartbeat](test_heartbeat.md) | Heartbeat proposer creates blocks when LFB goes stale |
| 1 | [test_observer_lfs_sync](test_observer_lfs_sync.md) | Fresh readonly observer LFS-syncs cleanly against an actively producing shard; deep cross-node post-state agreement on observer's LFB ancestor chain |
| 12 | [test_query_endpoints](test_query_endpoints.md) | HTTP query endpoints (balance, validators, epoch, estimate-cost, etc.) |
| 2 | [test_storage](test_storage.md) | Registry-based data storage + cross-node retrieval after finalization |
| 5 | [test_token_metadata](test_token_metadata.md) | Native token metadata on the shared shard (happy path + cross-shard checks) |
| 5 | [test_wallets](test_wallets.md) | PoS vault transfers, authorization failures, insufficient funds, Block API transfers |
| 23 | [test_web_api](test_web_api.md) | HTTP API: strict assertions, cross-node consistency, views, status, bond-status |

**Total: 69 tests across 14 files.**

---

## `tests/custom/` — per-test custom shards

Each test builds its own `ShardConfig` and calls `provider.create_shard(...)`. Used for asymmetric bonds, validator failures, bonding/unbonding, trim-state sync, load testing, and anything that mutates shard-wide state. Marked `pytest.mark.xdist_group("custom")`.

| Tests | File | Summary |
|---|---|---|
| 4 | [test_asymmetric_bonds](test_asymmetric_bonds.md) | Consensus with unequal stake weights (60/20/15) |
| 5 | [test_consensus_safety](test_consensus_safety.md) | Consensus safety under validator failure, FTT boundaries, epochs |
| 1 | [test_cold_start_readiness](test_cold_start_readiness.md) | Readiness remains false before the first LFB, then becomes true after finalized-state convergence |
| 1 | [test_concurrent_bridge_locks](test_concurrent_bridge_locks.md) | Concurrent bridge locks preserve exact nonce, history, accounting, and vault state after finalization |
| 1 | [test_finality_stall_recovery](test_finality_stall_recovery.md) | Finality stalls under quorum loss without runaway empty blocks and recovers after quorum restoration |
| 11 | [test_user_contract_concurrency](test_user_contract_concurrency.md) | Multi-parent merge on user-contract state (no PoS): independent channels, single-Map RMW (the bonds shape), mergeable counter — under always-on background load |
| 1 | [test_validator_lifecycle](test_validator_lifecycle.md) | Full PoS validator lifecycle (3 joiners): concurrent bond, rewards, concurrent bond+unbond, epoch-move shrink+grow, quarantine payout, re-bond, commit-reveal randomness, posVaultTransfer + auth-token guards, Mode-A out-of-phlo — bg-on throughout, cross-node FS-identity (slashing + active-validator cap out of scope) |
| 1 | [test_active_validator_cap](test_active_validator_cap.md) | PoS active-validator cap: `pickActiveValidators` take(N) — 5 bonded but only 3 active under `--number-of-active-validators=3`; capped genesis in its own shard |
| 1 | [test_load](test_load.md) | Deploy throughput + finalization latency benchmark |
| 1 | [test_observer_missing_block_retry](test_observer_missing_block_retry.md) | Observer retries missing blocks after peers return and reproduces the canonical post-state hash |
| 1 | [test_observer_overload](test_observer_overload.md) | Observer rejects excess exploratory deploys with bounded memory and recovers API capacity |
| 1 | [test_readonly_catchup_bounded](test_readonly_catchup_bounded.md) | Readonly catch-up keeps API latency and memory bounded while converging to canonical state |
| 1 | [test_shard_degradation](test_shard_degradation.md) | Production-readiness gate: 150 deploys, sustained load |
| 1 | [test_slow_peer_notification](test_slow_peer_notification.md) | A slow low-stake peer does not block deploy finalization while quorum remains available |
| 1 | [test_joiner_self_proposes_at_epoch_boundary](test_joiner_self_proposes_at_epoch_boundary.md) | Negative-control for the joiner-bond-drop bug: deterministic single-node propose does NOT reproduce it; rules out architectural-shape-alone hypothesis |
| 1 | [test_synchrony_constraint](test_synchrony_constraint.md) | Per-validator synchrony constraint threshold enforcement |
| 1 | [test_transient_peer_liveness](test_transient_peer_liveness.md) | Transient failures retain peers, successful heartbeats reset the streak, and removed peers are rediscovered |
| 1 | [test_trim_state](test_trim_state.md) | Joiner syncs from Last Finalized State instead of replaying genesis |
| 6 | [test_websocket](test_websocket.md) | `/ws/events` block, genesis, transfer, lifecycle events + startup replay |

**Total: 41 tests across 19 files.**

---

## `tests/standalone/` — per-test standalone nodes

Each test spins up a single node with no peers. Used for heartbeat timing, standalone propose, and isolated node behavior. No xdist group — tests are fully parallelizable (each worker gets its own node).

| Tests | File | Summary |
|---|---|---|
| 2 | [test_heartbeat](test_heartbeat.md) | Standalone heartbeat config: idle block creation, disabled when max-parents=1 |
| 1 | [test_duplicate_signed_deploy](test_duplicate_signed_deploy.md) | A concurrent duplicate signed-deploy race accepts and executes exactly one copy |
| 1 | [test_expired_deploy_admission](test_expired_deploy_admission.md) | Expired deploys are rejected and discarded while the first live boundary deploy finalizes |
| 1 | [test_propose](test_propose.md) | Deploy phlo price validation with custom `--min-phlo-price` |
| 9 | [test_token_metadata](test_token_metadata.md) | Native token metadata standalone: joiner mismatch, round-trip, restart drift, multi-shard, genesis blocking (validation rejections moved to Rust unit tests) |

**Total: 14 tests across 5 files.**

---

## Summary

| Directory | Files | Tests |
|---|---|---|
| `tests/shared/` | 14 | 69 |
| `tests/custom/` | 19 | 41 |
| `tests/standalone/` | 5 | 14 |
| **Total** | **38** | **124** |

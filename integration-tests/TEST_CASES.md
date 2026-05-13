# All Integration Test Cases

## test_web_api.py (shard, 12 tests)

| Test | What it does |
|------|-------------|
| `test_status` | Calls `/api/status`, verifies it returns a version string |
| `test_prepare_deploy` | Calls `/api/prepare-deploy`, verifies `seq_number >= 3` (confirms finalized state is reflected) |
| `test_data_at_name` | Deploys a contract, calls `/api/data-at-name` with the deploy hash, verifies data returned |
| `test_last_finalized_block` | Calls `/api/last-finalized-block`, verifies a block is returned |
| `test_get_block` | Calls `/api/block/<hash>` with the genesis hash, verifies block info |
| `test_get_blocks` | Calls `/api/blocks/<depth>`, verifies at least 4 blocks returned |
| `test_get_deploy` | Deploys a contract, calls `/api/deploy/<id>`, verifies deploy info returned |
| `test_get_data_at_name_empty_payload` | Queries `getDataAtName` for a deploy that didn't write to `deployId`. Verifies empty data returned (not error). Requires f1r3node PR #472 |
| `test_propose_no_new_deploys` | Calls `propose` with no pending deploys. Verifies `NoNewDeploys` error contains informative message. Requires f1r3node PR #472 |
| `test_get_deploy_detail` | Calls `/api/deploy/<id>?view=detail`, verifies response includes `cost`, `errored`, `blockNumber`, `deployer`. Requires f1r3node PR #472 |
| `test_explore_deploy_returns_cost` | Calls `/api/explore-deploy` on readonly node, verifies response includes `cost > 0` (phlogiston estimation). Requires f1r3node PR #472 |
| `test_deploy_via_http` | Submits a deploy via HTTP POST to `/api/deploy`, verifies accepted |

## test_wallets.py (shard, 4 tests)

| Test | What it does |
|------|-------------|
| `test_validator1_pay_validator2` | V1 transfers tokens to V2 via the vault contract, verifies both balances update correctly |
| `test_transfer_failed_with_invalid_key` | Attempts transfer with wrong private key, verifies it's rejected |
| `test_transfer_failed_with_insufficient_funds` | Attempts overdraw transfer, verifies insufficient funds error |
| `test_block_api_returns_transfer_info` | Makes a transfer, queries the readonly node's BlockReportAPI, verifies transfer details appear in DeployInfo |

## test_heartbeat.py (standalone + shard, 4 tests)

| Test | What it does |
|------|-------------|
| `test_heartbeat_creates_blocks_when_idle` | Standalone node with heartbeat enabled (5s check, 3s max-lfb-age). Asserts 4+ blocks and 3+ "Successfully created block" log lines within 90s with zero deploys |
| `test_heartbeat_disabled_when_max_parents_is_one` | Standalone with `max-number-of-parents=1`. Asserts CONFIGURATION ERROR log and no blocks created (heartbeat requires multi-parent capability) |
| `test_heartbeat_creates_blocks_when_idle_shard` | On the 3-validator shard, asserts all 3 validators advance by 2+ blocks from heartbeat alone |
| `test_manual_propose_during_heartbeat_shard` | Deploys a contract and calls manual `propose()` while heartbeat is running. Asserts no crash (tests the `Semaphore(1)` non-blocking lock between heartbeat and manual propose) |

## test_deployment.py (shard, 1 test)

| Test | What it does |
|------|-------------|
| `test_deploy_with_not_enough_phlo` | Deploys with `phlo_limit=10` (needs ~97). Verifies the deploy is included in a block but marked as `errored=true` |

## test_storage.py (shard, 2 tests)

| Test | What it does |
|------|-------------|
| `test_data_is_stored_and_served_by_node` | Deploys a store contract that writes to a registry channel, then deploys a read contract on the same node. Verifies the stored data is returned |
| `test_data_stored_on_one_validator_served_by_another` | Stores data on V1, waits for finalization, then reads on V2. Verifies cross-validator state propagation through the merge base |

## test_genesis_ceremony.py (shard, 1 test)

| Test | What it does |
|------|-------------|
| `test_successful_genesis_ceremony` | Verifies all 5 nodes (boot + 3 validators + readonly) reached Running state, share the same genesis block hash, and the genesis block has no parents |

## test_internal.py (shard, 1 test)

| Test | What it does |
|------|-------------|
| `test_parse_mvdag_str` | Pure Python unit test -- parses an MVDAG string format into a dict. No node interaction |

## test_dag_correctness.py (shard, 2 tests)

| Test | What it does |
|------|-------------|
| `test_fault_tolerance` | Deploys on all 3 validators, waits for blocks, then checks that fault tolerance values are monotonically non-increasing from genesis forward. Also verifies multi-parent blocks exist in the DAG and FT values agree across nodes |
| `test_cross_validator_post_state_agreement` | Deploys on all 3 validators, finds a block that references multiple parents, verifies all validators computed identical post-state hashes for that block |

## test_finalization.py (shard, 1 test)

| Test | What it does |
|------|-------------|
| `test_finalizes_block` | Deploys on V1 and V2, waits up to 60s for LFB to advance. Verifies the finalized block has `fault_tolerance > 0.1` (the configured FTT). Confirms the FT math: 3 equal validators at 2/3 agreement gives FT=0.33 > 0.1 |

## test_propose.py (standalone + shard, 3 tests)

| Test | What it does |
|------|-------------|
| `test_deploy_invalid_contract` | Deploys invalid Rholang (`not a valid contract`), verifies API rejects it with parsing error. Then deploys a valid contract and verifies it succeeds |
| `test_deploy_phlo_price_too_small` | Starts standalone with `--min-phlo-price=10`, deploys with `phlo_price=1`, verifies rejection with "phlo price is less than minimum" |
| `test_find_block_by_deploy_id_shard` | Deploys on V1, finds the block via `find_deploy` on V1, then verifies V2 and V3 also find the same deploy in the same block (cross-validator consistency) |

## test_replay_correctness.py (shard, 1 test)

| Test | What it does |
|------|-------------|
| `test_duplicate_sends_accepted_by_all_validators` | Deploys `bridge.rho` (which has duplicate channel sends: `requiredSigsCh!(2)` twice, `oracleCountCh!(3)` twice). Verifies the block is accepted by all validators and LFB advances 3+ blocks past the deploy on all 5 nodes |

## test_convergence.py (shard, 2 tests)

| Test | What it does |
|------|-------------|
| `test_network_recovers_from_validator_pause` | Pauses validator1 container for 15s to force DAG tip divergence while V2+V3 produce heartbeat blocks. After unpause, verifies LFB advances 3+ blocks on all nodes (multi-parent convergence blocks merge the diverged forks) |
| `test_network_converges_after_slow_deploy` | Deploys a phlo-exhausting loop (`loop!(100000)` with 20M phlo). The loop blocks V1 for ~200s while V2+V3 produce heartbeat blocks. After the deploy finishes (errored -- phlo exhausted), verifies: (1) the deploy is included in a block, (2) LFB advances 3+ blocks past that block, (3) all 3 validators converge to the same LFB within 2 blocks. **Not in CI** — stalls the shard for subsequent tests |

## test_load.py (shard, 1 test)

| Test | What it does |
|------|-------------|
| `test_deploy_throughput_and_finalization` | Submits deploys at increasing rates (1/sec, 5/sec, 10/sec, burst of 32) across 3 validators. Measures per-deploy inclusion and finalization latency (p50/p95/p99), effective throughput, and LFB advancement rate per phase. Scrapes Prometheus `/metrics` at phase boundaries to report node-internal timing: validation steps, checkpoint breakdown (merge vs replay), DAG merge pipeline (branches, conflicts_map, rejection, trie actions), and replay phases. Hard assertions: zero deploy failures, all deploys finalized within timeout, no node crashes |

## test_bridge_admin.py (shard, 1 test)

| Test | What it does |
|------|-------------|
| `test_bridge_admin_api` | Deploys the bridge.rho contract (multi-sig bridge with oracles, vault integration, lock/unlock operations). Verifies deploy succeeds, block is finalized, and the BlockReportAPI returns correct deploy info including cost, errored status, and deployer. Tests the full lifecycle of a complex production contract |

## test_shard_degradation.py (shard, 1 test)

| Test | What it does |
|------|-------------|
| `test_shard_degradation` | Production-readiness gate: deploys 150 non-trivial Rholang contracts (6 types including bridge.rho) across 3 validators in batches of 10. Monitors all 5 nodes for: LFB advancement rate, validator desync (<5 blocks), finalizer timeouts (zero), deploy inclusion (<15s), deploy finalization (<30s), API latency (<2s), LFB stalls (max 1 batch). Fails if any threshold is violated |

## test_consensus_health.py (shard, 1 test -- runs last)

| Test | What it does |
|------|-------------|
| `test_no_consensus_errors_in_logs` | Scans all 5 nodes' logs accumulated from ALL preceding shard tests. Checks for fatal patterns: InvalidBondsCache, structural errors, FATAL, panic. Asserts zero matches. Acts as a regression guard for the entire shard test suite |

## test_synchrony_constraint.py (custom, 1 test)

| Test | What it does |
|------|-------------|
| `test_synchrony_constraint` | Custom 3-validator shard with per-node synchrony thresholds: V1=0.67, V2=0.33, V3=0.99. Heartbeat disabled, FTT=-1 (instant finalization). Manual propose orchestration: V1 proposes first (genesis exempt), then V2 and V3 propose (setting their baselines). Then deploys on all 3 and verifies: V1 can propose when V2+V3 have advanced (0.67 met), V1 is rejected when only V3 advanced (weight 150 < 200 needed at 0.67), V2 can propose when V1 alone advanced (0.33 met), V3 can never propose because 0.99 requires both V1+V2 to advance simultaneously |

## test_asymmetric_bonds.py (custom, 3 tests)

| Test | What it does |
|------|-------------|
| `test_fault_tolerance_asymmetric_bonds` | Custom shard with bonds 60/20/15, FTT=0.5, heartbeat enabled. Deploys on all 3, verifies FT is monotonically non-increasing and multi-parent blocks exist |
| `test_finalization_asymmetric_bonds` | Same asymmetric bonds. Verifies LFB advances within 60s despite unequal stake distribution |
| `test_cross_validator_state_agreement_asymmetric` | Same bonds. Deploys on all 3, verifies all validators compute identical post-state hashes |

## test_bonding_validators.py (custom, 1 test)

| Test | What it does |
|------|-------------|
| `test_bonding_validators` | Custom 2-validator shard (10M bonds each), epoch-length=4, quarantine-length=20, heartbeat disabled, FTT=-1. Adds a joiner via `add_peer_to_shard`. The joiner deploys a bonding contract, waits for epoch boundary, then verifies: joiner appears in the bonds map, joiner can propose blocks after activation |

## test_trim_state.py (custom, 1 test)

| Test | What it does |
|------|-------------|
| `test_trim_state` | Custom 2-validator shard (V1=10M, V2=1), heartbeat disabled, FTT=-1. Creates several blocks on V1+V2, then adds a joiner. The joiner syncs from LFS (Last Finalized State) -- not replaying from genesis. Verifies: joiner sees the latest block within 240s, joiner keeps up with new blocks after sync, joiner's post-state agrees with V1 |

## test_slash.py (custom, 19 tests, Linux-only)

End-to-end Docker integration tests for the slashing pipeline. Each test spawns a custom 2- or 3-validator shard with heartbeat disabled and FTT=-1 at a distinct `port_base` (40900–41800 in 50-port increments), then uses `NodeClient` to inject crafted malicious blocks over the TLS-encrypted P2P transport (`routing.proto`). The receiving validator's logs are regex-scraped via `wait_for_log_match`, and post-slash bonds are verified via gRPC `block_info`. Skipped on macOS / Windows / WSL2 — `NodeClient` requires native Linux Docker bridge routing. The `rust_block_hash()` helper at the top of the file mirrors Rust's `casper/src/rust/util/proto_util.rs:380 hash_block` byte layout so forged blocks pass `Validate::block_hash` and reach the intended offense (the upstream `pyf1r3fly` helper boxes `sigAlgorithm`/`seqNum`/`shardId` in protobuf wrappers and diverges).

| Test | What it does |
|------|-------------|
| `test_slash_invalid_block_hash` | V1 re-signs a valid block with an `evil` blake2b digest and ships it to V2. V2 records `InvalidBlockHash`, proposes a new block, and V1's bond drops to 0 |
| `test_slash_invalid_block_number` | V1 ships a block with `blockNumber = 5` (not `max(parents)+1 = 2`). V2 records `InvalidBlockNumber` and slashes V1. (Stays inside `epoch_length=10` so `slash_evidence_epoch_matches_target` accepts the evidence.) |
| `test_slash_invalid_block_seq` | V1 ships a block with `seqNum = 1000` (not previous+1). V2 records `InvalidSequenceNumber` and slashes V1 |
| `test_slash_justification_not_correct` | 3-validator shard. V1 ships a block with an extra `Justification` entry from an unknown random key. V2 records `InvalidFollows` and slashes V1 |
| `test_slash_unauthorized_slash_deploy` | **H3 (T-11, attack-tree A2)** — 3-validator shard. V2 proposes a block carrying a forged `SlashSystemDeploy` that cites a non-existent invalid_block_hash. V3 records V2's block as `UnauthorizedSlashDeploy` (rule #3 of `validate_received_slash_deploys` — referenced block unknown to DAG) and slashes V2. V1 (alleged target) remains bonded |
| `test_slash_references_valid_block` | **H4 (T-Auth wire-level sibling)** — 3-validator shard. V2 proposes a block whose `SlashSystemDeploy` references V1's legitimate first block as if it were slashable evidence. V3 records V2's block as `UnauthorizedSlashDeploy` (rule #4 — referenced block known but not flagged invalid) and slashes V2. T-Auth proper (spoofed-token) is in-tree only via `uc_21_auth_token_check.rs` because auth tokens are unforgeable Rholang names |
| `test_slash_self_regression` | **M1 (T-7, bug #6)** — V1 proposes block A; V1 forges a successor B whose creator-self justification points behind A (regressing V1's latest-message). V2 records `JustificationRegression` and slashes V1 |
| `test_slash_invalid_bonds_cache` | **M3 (T-26)** — V1 ships a block whose `body.state.bonds` differs from the post-state replay's computed bonds. V2 records `InvalidBondsCache` and slashes V1 |
| `test_slash_invalid_repeat_deploy` | **M4 (T-29)** — V1 ships two successive blocks containing the same `deployId`. V2 records the second block as `InvalidRepeatDeploy` and slashes V1 |
| `test_slash_GHOST_disobeyed` | 3-validator shard. V1 ships an off-GHOST block (parents mutated away from the GHOST winner + deploy replaced). V2 records the block as `InvalidTransaction` (Rust's `validate_block_checkpoint` runs before `parents`, so the wire-level variant is `InvalidTransaction`, not the formal `InvalidParents` — both are slashable; V1's bond drops to 0). Formal `InvalidParents` semantics are covered in-process by `casper/tests/slashing/integration_t_invalid_parents.rs` via `propose_with_block_mutation` which sidesteps the wire-level replay constraint |
| `test_node_working_right_after_slashing` | Same flow as `test_slash_invalid_block_hash`, but additionally verifies the slashing block contains exactly one `slashSystemDeploy` and the next normal block contains zero (slash deploy emitted once per offense) |
| `test_slash_invalid_validator_approve_evil_block` | Level-2 closure ("neglect of an invalid block"). 3-validator shard: V1 ships a hash-tampered block to V2; V2 (heartbeat off) crafts an "approve" block that cites V1's invalid block in justifications and ships to V3; V3 records V2's block as `InvalidTransaction` (post-state replay diverges from V1's copied post-state) and emits TWO slash deploys in one propose round — both V1 and V2 bonds drop to 0. Exercises `prepare_slashing_deploys`'s uncapped emission of `authorized_slash_candidates` |
| `test_slash_ignorable_equivocation` | **H2 (T-2, Bug #1 wire-level regression)** — V1 honestly proposes b1; the test forges a sibling `b1p` via the timestamp-+1 trick (same body, +1ms timestamp, re-hashed, re-signed). V2 receives both. The detector returns `IgnorableEquivocation` (the receiver did not request b1p by hash, so `requested_as_dependency == false`). After bug-#1 fix this variant is slashable; the test pins that post-fix behaviour against the deployed binary |
| `test_slash_admissible_equivocation` | **H1 (T-1)** — Same sibling-forge as H2, but the test additionally ships a V2-signed "child of b1p" block FIRST so V2's block processor buffers it pending the missing dependency b1p (flipping `requested_as_dependency(b1p)` to true). After waiting for the `"waiting on missing dependencies"` log line, the test ships b1p; the detector returns `AdmissibleEquivocation` |
| `test_slash_neglected_equivocation` | **H5 (T-33)** — 3-validator shard. V1 equivocates (b1/b1p). V2 builds a block citing b1p in justifications WITHOUT a SlashDeploy (forged on the wire because V2 wouldn't do this naturally). V3 records V2's block as `NeglectedInvalidBlock` or `InvalidTransaction` (variant alternates because `validate_block_checkpoint` runs before `neglected_invalid_block`; semantic outcome — V1=0 AND V2=0 in one V3 propose — is preserved). Wire-level pure-`NeglectedEquivocation` is unreachable; in-tree analog `uc_04_neglect_two_level.rs` covers it via test-only helpers |
| `test_slash_late_released_equivocation` | **M6 (T-36 §5.A.5)** — V1 proposes b1, withholds the equivocating sibling b1p. V2 proposes 4 blocks atop b1 (advancing the chain inside epoch 0 so the slash authorization predicate remains satisfied). V1 then releases b1p; V2 detects equivocation (V1's latest-message in V2's DAG is still b1 throughout, so `JustificationRegression` does not interfere) and slashes V1 |
| `test_slash_stale_evidence_rebond` | **M2 (T-12, bug #15)** — 3-validator shard with `--epoch-length=2`. V1 forges an `InvalidBlockNumber` block at epoch 0. V2 and V3 propose naturally into epoch ≥1 (proposer-side filter drops V1's stale evidence so V1 stays bonded). V2 then forges a slash deploy citing V1's epoch-0 evidence with `targetActivationEpoch=0` onto its current-epoch propose. V3 records V2's block as `UnauthorizedSlashDeploy` (rule #2 — `EpochMismatch`) and slashes V2. V1 remains bonded — proposer-side and receive-side stale-evidence filters are mutually consistent |
| `test_slash_self_correcting_block_admitted` | **M5 (T-34, bug #9)** — 3-validator shard. V1 forges an `InvalidBlockHash` block; V2's natural propose response carries a SlashDeploy against V1. V3 admits V2's self-correcting block via the bug-#9 widening at `validate.rs:1080-1092` (`neglected_invalid_justification && !has_slash_system_deploys` short-circuits to false when V2's block carries a slash). V1 is slashed inside V2's block; V2 remains bonded. Reinterprets the task's literal "same (sender, seqNum)" framing per spec §10.9 — that framing is equivocation, not self-correction; the bug-#9 widening is for a different-validator slasher |
| `test_no_false_positive_slash_on_propose_imbalance` | **B1 (T-12PF safety arm)** — 3-validator shard. Pins the ABSENCE of over-eager behavioral-pattern detectors. V1 dominates the propose chain (5 proposes in a row); V2 stays silent (no deploys, no proposes); V3 proposes once at the end. Asserts all bonds remain at 100. Test PASSES iff no over-eager fairness detector exists; would FAIL the moment a regression introduces one. The protocol's *liveness* arm of T-12PF cannot be wire-tested (no detector to fire), but its *safety* arm (no wrongful slashing under proposer unfairness) is testable, and this test is it |

**Carve-out — M7 (`test_slash_censoring_proposer_eventually_slashes`, T-37 / T-12PF liveness arm) is intentionally NOT present.** The reason is stronger than "no runtime detector exists": **the conventional censorship threat is structurally undefined in this protocol's wire semantics.** Deploys are not gossiped — `casper/src/rust/casper_engine/block_admission.rs:60-94 admit_deploy` stores them in the local node's `KeyValueDeployStorage`, the block creator reads from that same local storage, and no code path broadcasts a deploy to peers. A deploy submitted to V2 stays on V2 until V2 proposes it; V1 cannot "censor" V2's deploys because V1 never has them. T-12PF is therefore correctly a *boundary assumption* per `slashing-traceability.md` finding 88 (`model_boundary`, "No source bug confirmed") — this is a positive design finding about the protocol's author-local-mempool semantics, not a deferred TODO. In-tree property tests cover the boundary classification. The *safety* arm of T-12PF is covered by `test_no_false_positive_slash_on_propose_imbalance` above; the wire-level withholding theme is covered by `test_slash_late_released_equivocation`.

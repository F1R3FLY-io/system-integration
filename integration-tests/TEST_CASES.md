# All Integration Test Cases

## test_web_api.py (shard, 7 tests)

| Test | What it does |
|------|-------------|
| `test_status` | Calls `/api/status`, verifies it returns a version string |
| `test_prepare_deploy` | Calls `/api/prepare-deploy`, verifies `seq_number >= 3` (confirms finalized state is reflected) |
| `test_data_at_name` | Deploys a contract, calls `/api/data-at-name` with the deploy hash, verifies data returned |
| `test_last_finalized_block` | Calls `/api/last-finalized-block`, verifies a block is returned |
| `test_get_block` | Calls `/api/block/<hash>` with the genesis hash, verifies block info |
| `test_get_blocks` | Calls `/api/blocks/<depth>`, verifies at least 4 blocks returned |
| `test_get_deploy` | Deploys a contract, calls `/api/deploy/<id>`, verifies deploy info returned |
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

## test_convergence.py (shard, 1 test)

| Test | What it does |
|------|-------------|
| `test_network_converges_after_slow_deploy` | Deploys a phlo-exhausting loop (`loop!(100000)` with 20M phlo). The loop blocks V1 for ~200s while V2+V3 produce heartbeat blocks. After the deploy finishes (errored -- phlo exhausted), verifies: (1) the deploy is included in a block, (2) LFB advances 3+ blocks past that block, (3) all 3 validators converge to the same LFB within 2 blocks |

## test_load.py (shard, 1 test)

| Test | What it does |
|------|-------------|
| `test_deploy_throughput_and_finalization` | Submits deploys at increasing rates (1/sec, 5/sec, 10/sec, burst of 32) across 3 validators. Measures per-deploy inclusion and finalization latency (p50/p95/p99), effective throughput, and LFB advancement rate per phase. Reports a summary table. Hard assertions: zero deploy failures, all deploys finalized within 90s, no node crashes |

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

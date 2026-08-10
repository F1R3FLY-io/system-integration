# test_web_api

## Purpose

Verifies the HTTP API endpoints exposed by F1R3FLY nodes with strict, setup-aware assertions across all nodes. Expected values (bond count, stakes, validator pubkeys) are derived from the shard's `ShardConfig` so tests adapt to different topologies. Cross-node consistency is verified for every endpoint — all nodes must agree on block hashes, post-state hashes, deploy costs, and fault tolerance values.

Includes a cross-check between HTTP API and gRPC fault tolerance values to detect inconsistencies (like the client-reported FT=0.0 vs FT=1.0 bug).

## Setup-aware assertions

The helper `_shard_expectations(shard, node_conf)` derives expected values from `ShardConfig.bonds` and the `node_conf` fixture:
- `bond_count` — number of validators (e.g. 3)
- `stakes` — map of validator pubkey to stake (e.g. all 100)
- `validator_pubkeys` — set of expected validator public keys
- `shard_id` — from `node_conf.shard_id` (parsed from config, not hardcoded)
- `ftt` — from `node_conf.ftt` (used for FT >= FTT assertions on finalized blocks)
- `native_token_name`, `native_token_symbol`, `native_token_decimals` — from `node_conf`
- `sig_algorithm` — "secp256k1"

All block info assertions use `_assert_light_block_info()` which validates every field of `LightBlockInfoSerde` against these expectations: block hash format, sender pubkey format, shardId, sigAlgorithm, version, bonds count and stakes, timestamp, and justification structure.

## Tests (10)

### test_status
`GET /api/status` on **all nodes**. Asserts:
- `version.api` and `version.node` non-empty
- `shardId` == "root", `networkId` non-empty
- `peers` >= 1, `nodes` >= validator count
- `minPhloPrice` matches `node_conf.min_phlo_price`
- `nativeTokenName`, `nativeTokenSymbol`, `nativeTokenDecimals` match `node_conf`
- `lastFinalizedBlockNumber` >= 0 (int)
- `isReady` == True (shard is running)
- `isValidator` is bool (per node role)
- `isReadOnly` is bool (True on readonly, False on others)
- `currentEpoch` >= 0 (int)
- `epochLength` > 0 (int)
- Cross-node: all agree on version, networkId, shardId, minPhloPrice, epochLength
- All addresses unique

### test_prepare_deploy
`GET /api/prepare-deploy` on **V1 and V2**. Deploys 3 contracts, polls until `seqNumber >= 3` on both nodes. Also tests `POST` with deployer params: verifies `nameQty=2` returns exactly 2 names.

### test_last_finalized_block
`GET /api/last-finalized-block` on **all nodes**. Asserts:
- Full `LightBlockInfoSerde` validation on each node
- `blockNumber` > 0, `faultTolerance` >= FTT
- `isFinalized` == True
- `parentsHashList` non-empty (not genesis)
- All nodes agree on LFB hash
- **HTTP vs gRPC FT cross-check**: compares FT from HTTP and gRPC on every node

### test_get_block
`GET /api/block/{hash}` on **all nodes**. Asserts:
- Full `LightBlockInfoSerde` validation
- `blockHash` matches queried hash
- `isFinalized` == True (we waited for finalization)
- Deploy found with `errored == false`, `cost > 0`, empty `systemDeployError`
- All nodes agree on `postStateHash`

### test_get_blocks
`GET /api/blocks/10` on **all nodes**. Response is `BlockInfoSerde` with `blockInfo` wrapper. Asserts:
- >= 4 blocks on every node
- Each block has `blockInfo` wrapper with valid hash, non-negative blockNumber, correct bond count
- `deploys` field omitted (summary view default)

### test_get_deploy_detail
`GET /api/deploy/{id}` on **all nodes** (full view, default). Returns unified `DeployResponse`. Asserts:
- `deployId` matches queried ID
- Valid `blockHash`, `blockNumber` > 0, `timestamp` > 0
- `cost` > 0, `errored` == false, `isFinalized` == true
- `deployer` matches V1's public key
- `systemDeployError` == ""; retired `phloPrice` and `phloLimit` fields are absent
- `sigAlgorithm` == "secp256k1"
- **Transfers**: omitted on validators, present as list on readonly
- Cross-node: all agree on `blockHash` and `cost`

### test_deploy_summary_view
`GET /api/deploy/{id}?view=summary` on **all nodes**. Asserts:
- Core fields present: `deployId`, `blockHash`, `blockNumber`, `timestamp`, `cost`, `errored`, `isFinalized`
- Full-view fields excluded: `deployer`, `term`, `sigAlgorithm`, `systemDeployError`, `validAfterBlockNumber`, `transfers`
- Cross-node: all agree on `blockHash` and `cost`

### test_get_data_at_name_empty_payload
gRPC `get_deploy_data()` on **V1 and V2**. Asserts result is not None and `par` is empty. Verifies PR #472 fix (empty payload instead of error).

### test_explore_deploy_returns_cost
`POST /api/explore-deploy` on **readonly**. Asserts `cost` is positive int, `expr` and `block` present.

### test_deploy_via_http
`POST /api/deploy` on V1. Builds a signed deploy proto, submits via HTTP JSON, asserts 200 response.

### test_block_summary_view
`GET /api/block/{hash}?view=summary` on **all nodes**. Asserts deploys omitted, blockInfo present.

### test_block_list_full_view
`GET /api/blocks/5?view=full` on V1. Asserts deploys included on at least one block.

### test_lfb_summary_view
`GET /api/last-finalized-block?view=summary` on V1. Asserts deploys omitted.

### test_deploy_unknown_view_defaults_full
`GET /api/deploy/{id}?view=bogus` on V1. Asserts falls back to full view (deployer field present).

### test_blocks_by_height_range
`GET /api/blocks/{start}/{end}` on V1. Asserts blocks in requested range, summary default.

### test_is_finalized_http
`GET /api/is-finalized/{hash}` on V1. Asserts true. Cross-checks with gRPC.

### test_grpc_status_matches_http
gRPC `status()` vs HTTP `/api/status` on **all nodes**. Asserts shardId, networkId, minPhloPrice, lastFinalizedBlockNumber, isValidator, isReadOnly, isReady, epochLength match.

### test_transfers_null_on_validator_http
`GET /api/block/{hash}` on **validator vs readonly**. Validator: transfers null. Readonly: transfers populated as list.

### test_removed_endpoints_404
POST `/api/data-at-name` and GET `/api/transactions/{hash}` both return 404.

### test_show_main_chain
gRPC `showMainChain` on V1. Asserts >= 2 blocks, descending block numbers, valid hashes.

### test_preview_private_names
gRPC `previewPrivateNames` on V1. Asserts 3 unique names generated, deterministic across calls.

### test_get_event_data
gRPC `getEventByHash` on **readonly**. Asserts block execution trace returned with deploys and events for a finalized block.

### test_get_continuation
gRPC `listenForContinuationAtName` on V1. Asserts query completes without error.

## What it proves (23 tests)

- All HTTP API endpoints return correct, validated response structures
- Unified `DeployResponse` with full/summary views works correctly
- `isFinalized` field present and correct on block and deploy responses
- Status includes operational state (isReady, isValidator, isReadOnly, epoch info)
- View params work on all block endpoints (full/summary, correct defaults)
- Block list endpoint returns `BlockInfoSerde` with summary default (deploys omitted)
- Blocks by height range returns correct range with summary default
- Finalized blocks have positive fault tolerance
- HTTP and gRPC report identical status and FT values
- Deploy execution details consistent across nodes
- Block content (postStateHash) identical across all nodes
- Transfer availability differs correctly between readonly and validator nodes
- Removed endpoints return 404
- Unknown view param falls back to full
- gRPC-only methods (showMainChain, previewPrivateNames, getEventByHash, listenForContinuationAtName) return correct data
- Removed endpoints return 404
- Unknown view param falls back to full

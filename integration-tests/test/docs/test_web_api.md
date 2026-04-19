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
- `minPhloPrice` matches `node_conf.min_phlo_price` (parsed from config)
- `nativeTokenName`, `nativeTokenSymbol`, `nativeTokenDecimals` match `node_conf` values
- Cross-node: all agree on version, networkId, shardId, minPhloPrice
- All addresses are unique

### test_prepare_deploy
`GET /api/prepare-deploy` on **V1 and V2**. Deploys 3 contracts, polls until `seqNumber >= 3` on both nodes. Also tests `POST` with deployer params: verifies `nameQty=2` returns exactly 2 names.

### test_last_finalized_block
`GET /api/last-finalized-block` on **all nodes**. Asserts:
- Full `LightBlockInfoSerde` validation on each node
- `blockNumber` > 0, `faultTolerance` > 0 (finalized blocks must have positive FT)
- `parentsHashList` non-empty (not genesis)
- All nodes agree on LFB hash
- **HTTP vs gRPC FT cross-check**: for the same block, compares `faultTolerance` from `/api/last-finalized-block` with `faultTolerance` from gRPC `show_block()` on every node

### test_get_block
`GET /api/block/{hash}` on **all nodes**. Deploys 1 contract, queries the containing block on every node. Asserts:
- Full `LightBlockInfoSerde` validation
- Returned `blockHash` matches queried hash
- Our deploy found in block with `errored == false`, `cost > 0`, empty `systemDeployError`
- All nodes agree on `postStateHash`

### test_get_blocks
`GET /api/blocks/10` on **all nodes**. Asserts:
- >= 4 blocks on every node
- Every block has valid hash, non-negative blockNumber, correct bond count

### test_get_deploy_detail
`GET /api/deploy/{id}?view=detail` on **all nodes**. Asserts:
- Valid `blockHash`, `blockNumber` > 0, `timestamp` > 0
- `deployer` matches V1's public key hex
- `cost` > 0 (int), `errored` == false, `systemDeployError` == ""
- `phloPrice` == 1, `phloLimit` == 100000
- `sig` matches deploy ID, `sigAlgorithm` == "secp256k1"
- `transfers` is a list (empty for non-transfer deploys)
- Cross-node: all agree on `blockHash` and `cost`

### test_data_at_name
`POST /api/data-at-name` on V1. Asserts `length == 0`, `exprs == []` for a deploy that writes to `@N` not `deployId`. (Deprecated endpoint — minimal coverage.)

### test_get_data_at_name_empty_payload
gRPC `get_deploy_data()` on **V1 and V2**. Asserts result is not None and `par` is empty. Verifies PR #472 fix (empty payload instead of error).

### test_explore_deploy_returns_cost
`POST /api/explore-deploy` on **all nodes**. Asserts:
- `cost` is positive int, `expr` and `block` present
- **Deterministic execution**: all nodes compute the same cost

### test_deploy_via_http
`POST /api/deploy` on V1. Builds a signed deploy proto, submits via HTTP JSON, asserts 200 response. Single-node is sufficient (write operation).

## What it proves

- All HTTP API endpoints return correct, fully-validated response structures
- Every field in `LightBlockInfoSerde` is correct for the shard configuration
- Deploy sequencing (`seqNumber`) reflects finalized state across nodes
- Finalized blocks have positive fault tolerance (catches FT=0.0 bug)
- HTTP and gRPC report identical fault tolerance for the same block (catches API inconsistency)
- Exploratory deploy cost is deterministic across all nodes
- Deploy execution details (cost, errored, deployer) are consistent across nodes
- Block content (postStateHash) is identical across all nodes
- HTTP deploy submission with signatures is accepted
- Token metadata is correctly reported in /api/status

## Infrastructure used

- Session-scoped `shared_shard` fixture with `shard.config` for setup-aware expectations
- `_assert_light_block_info()` — shared validator for all block info fields
- `Node.api_get()`, `Node.api_post()` for HTTP API calls
- `Node.get_block()` (gRPC) for HTTP vs gRPC FT cross-check
- `wait_for_deploy_included()`, `wait_for_finalized()` from `infra/polling.py`
- `sign_deploy_data()` from pyf1r3fly for HTTP deploy signing

## Related

- [test_deployment](test_deployment.md) -- deploy lifecycle tests (error handling, cross-validator lookup)
- [test_wallets](test_wallets.md) -- Block API transfer extraction via HTTP
- [test_token_metadata](test_token_metadata.md) -- deeper token metadata testing (on-chain, startup logs)

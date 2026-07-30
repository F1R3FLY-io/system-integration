# test_query_endpoints

## Purpose

Tests the high-level HTTP query endpoints added in Phase 7+ of the API redesign. These are convenience endpoints wrapping exploratory deploy or genesis config. Tests verify correct responses on readonly and validator nodes, cross-check gRPC parity where applicable, and cover edge cases (unknown validators, explicit block_hash params).

## Tests (12)

### test_validators_endpoint
`GET /api/validators` on **readonly**. Asserts correct validator count, pubkeys match genesis config, stakes match, totalStake correct, blockNumber/blockHash present.

### test_validator_bonded
`GET /api/validator/{pubkey}` on **readonly** with genesis validator key. Asserts isBonded=true, stake > 0.

### test_validator_unknown
`GET /api/validator/{pubkey}` on **readonly** with fake key. Asserts isBonded=false, stake=null.

### test_epoch_all_nodes
`GET /api/epoch` on **all nodes** (works everywhere, no exploratory deploy). Asserts all fields present and correctly typed. Verifies currentEpoch = lfbNumber // epochLength. Cross-node: epochLength identical.

### test_epoch_rewards
`GET /api/epoch/rewards` on **readonly**. Asserts reward map contains all genesis validator pubkeys.

### test_estimate_cost
`POST /api/estimate-cost` on **readonly**. Asserts cost > 0 for valid Rholang.

### test_estimate_cost_invalid_syntax
`POST /api/estimate-cost` on **readonly** with invalid Rholang. Asserts error response.

### test_bond_status_bonded
`GET /api/bond-status/{pubkey}` on **all nodes** with genesis validator key. Asserts isBonded=true. Cross-checks with gRPC `bondStatus` on each node.

### test_bond_status_unknown
`GET /api/bond-status/{pubkey}` on **all nodes** with fake key. Asserts isBonded=false. Cross-checks with gRPC.

### test_balance_endpoint
`GET /api/balance/{address}` on **readonly** with V1's REV address. Asserts balance >= 0.

### test_registry_endpoint
`GET /api/registry/{uri}` on **readonly** with system URI. Asserts data and block context present.

### test_query_with_block_hash
`GET /api/validators?block_hash=` and `GET /api/epoch?block_hash=` on **readonly** with explicit block hash. Asserts blockHash in response matches query param.

## Infrastructure

- Session-scoped `shared_shard` with 3 validators + readonly
- `VALIDATOR1_ID`, `VALIDATOR2_ID` from `infra/keys`
- `node.api_get()` for HTTP, `node.grpc_bond_status()` for gRPC cross-check
- `get_vault_address()` for REV address derivation

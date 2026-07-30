# test_contract_lifecycle

## Purpose

Comprehensive contract lifecycle test that deploys multiple contracts in parallel, queries them via both real deploy and exploratory deploy across all nodes, verifies cross-node state agreement at every phase, exercises contract-to-contract interaction, vault transfers interleaved with queries, and multi-block state evolution.

This is the primary integration test for verifying the merge pipeline handles real-world contract interactions correctly.

## How it works

A module-scoped `deployed_contracts` fixture deploys all contracts once in parallel. Individual test functions then exercise different aspects of the deployed contracts, with cross-node state verification after each phase.

### Contracts deployed

| Contract | Validator | Purpose |
|----------|-----------|---------|
| `bridge-v2.rho` (instance 1) | V1 | Complex registry-heavy contract with persistent state channels |
| `bridge-v2.rho` (instance 2) | V2 | Independent second instance (different deployer, different URIs) |
| `store-data.rho` | V3 | Simple registry storage |
| `data-provider.rho` | V1 | Provides getData/getNumber methods for contract-to-contract interaction |

## Tests (7)

### test_cross_node_state_after_deployment

Verifies all nodes (boot, V1, V2, V3, readonly) agree on the `postStateHash` for every deployment block. Uses `assert_all_nodes_agree_on_block` for each contract's block.

### test_cross_validator_queries_real_deploy

Queries bridge contracts from validators that didn't deploy them via real deploys (creates blocks). Each query is submitted to a different validator than the one that deployed the contract. Queries run in parallel via `ThreadPoolExecutor`. Verifies cross-node consistency after queries.

### test_cross_validator_queries_exploratory

Same queries as above but via exploratory deploy on the readonly node. Verifies readonly sees all contract state from all validators. Covers bridge getNonce, getTotalLocked, getAddress, storage lookup, and provider getData.

### test_contract_to_contract_interaction

Deploys `data-consumer.rho` on V2 which looks up the data-provider's URI in the registry and calls its `getData` method. Verifies the consumer receives `"hello_from_provider"` — proving cross-contract registry lookup works after merge. Checks cross-node state agreement on the consumer's block.

### test_transfers_interleaved_with_queries

Submits a vault transfer (V1 → V2) and a bridge query in parallel. Verifies the transfer succeeds (V2 balance increases by exact amount) and the query returns correct results. Exercises the merge pipeline under mixed workload.

### test_multi_block_state_evolution

Exercises bridge lock operations across multiple blocks from different validators:
1. Lock 100 tokens from V1 — verify nonce increments
2. Lock 200 tokens from V2 — verify nonce increments again
3. After each lock, verify all nodes agree on the block's post-state

Proves contract state correctly evolves across multiple blocks with mutations from different validators.

### test_final_cross_node_state_agreement

Final verification after all phases:
- All nodes agree on the same LFB hash
- All nodes agree on the LFB's post-state hash
- All contracts accessible via exploratory deploy on readonly (8 contract queries)
- Provider returns expected values ("hello_from_provider", 42)
- Bridge instances have different vault addresses (independence check)

## Setup

- **Topology**: Session-scoped `shared_shard` (3 validators + readonly)
- **Heartbeat**: Enabled (for automatic block inclusion)
- **Fixture scope**: `deployed_contracts` is module-scoped — deployed once, shared across all tests

## What it proves

1. Multiple complex contracts deploy in parallel without interference
2. All nodes converge to identical state after every phase
3. Registry lookups work cross-validator after merge (both real deploy and exploratory)
4. Multiple instances of the same contract are independent
5. Contract-to-contract interaction via registry works after merge
6. Vault transfers and contract queries can interleave without corruption
7. Contract state evolves correctly across multiple blocks from different validators
8. Readonly node sees consistent state matching all validators at every step
9. No node crashes or panics under sustained mixed workload

## Key assertions

- `assert_all_nodes_agree_on_block` after every deployment and mutation block
- `assert_all_nodes_agree_on_lfb` in final verification
- `assert_contracts_consistent_across_nodes` for exploratory queries
- Bridge nonce increments after each lock
- Bridge instances have different vault addresses
- Consumer receives provider's data after cross-contract interaction
- Transfer amounts exactly match balance changes

## Infrastructure used

- Session-scoped `shared_shard` fixture (3 validators + readonly)
- `deploy_and_read()` for contract deployment and real deploy queries
- `Node.registry_query()` for exploratory deploy queries
- `Node.vault.transfer_ensure()` and `Node.vault.get_balance()` for transfers
- `assert_all_nodes_agree_on_block` / `assert_all_nodes_agree_on_lfb` from `infra/assertions.py`
- `assert_contracts_consistent_across_nodes` from `infra/assertions.py`
- `ThreadPoolExecutor` for parallel deploy and query submission
- `wait_for_finalized()` for block-height finalization checks (LFB advances)
- `wait_for_deploy_finalized()` for canonical-state per-deploy finalization (e.g. transfer tracking, where merge-rejection recovery matters)
- `par_as_int`, `par_as_string`, `par_as_uri` for typed Par extraction

## Resources

- [bridge-v2.rho](../../resources/bridge-v2.rho) — bridge contract
- [store-data.rho](../../resources/storage/store-data.rho) — simple storage
- [data-provider.rho](../../resources/lifecycle/data-provider.rho) — provides getData/getNumber
- [data-consumer.rho](../../resources/lifecycle/data-consumer.rho) — reads from provider via registry

## Related

- [test_bridge_admin](test_bridge_admin.md) — simple bridge regression test (exploratory + real deploy)
- [test_wallets](test_wallets.md) — vault transfer tests
- [test_deployment](test_deployment.md) — deploy error handling

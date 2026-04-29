# test_storage

## Purpose

Verifies that data can be stored and retrieved via the Rholang registry, both on the same validator and across all nodes after block propagation and finalization. The registry is the primary mechanism for on-chain data persistence in Rholang.

Store operations use real deploys (state changes). Read operations use exploratory deploy via `Node.registry_lookup()` — instant, no block created.

## How the registry works

1. **Store**: `rho:registry:insertArbitrary` inserts a value and returns a `rho:id:...` URI (real deploy — creates state)
2. **Read**: `rho:registry:lookup` resolves the URI back to the stored value (exploratory deploy — read-only)

The store contract (`store-data.rho`) writes the registry URI to the `deployId` channel, read via `get_deploy_data()`. The read step uses `Node.registry_lookup()` which delegates to `f1r3fly.contracts.registry_lookup` via exploratory deploy.

## Tests (2)

### test_data_is_stored_and_served_by_node

Single-validator round-trip on V1:
1. Generate a random 20-character ASCII string
2. Deploy `store-data.rho` on V1 with the random data (real deploy)
3. Wait for deploy inclusion, read the registry URI from the `deployId` channel via `par_as_uri`
4. Validate URI starts with `rho:id:`
5. Wait for readonly to finalize past the store block, then assert `assert_block_finalized_on_all_nodes` — the store block must be finalized on every validator and the readonly observer, not just the proposer
6. Read the value back via `Node.registry_lookup()` (exploratory deploy — instant)
7. Assert `read_data == random_data`

### test_data_stored_on_one_validator_readable_on_readonly

Cross-validator propagation — store on each of V1/V2/V3 in turn, read on readonly:
1. For each validator (V1, V2, V3):
   a. Store data via `_store_data` (returns URI, block number, block hash)
   b. Wait for readonly's LFB to reach the store block number via `wait_for_finalized`
   c. Assert `assert_block_finalized_on_all_nodes` — the store block must be finalized on every node (catches a peer that accepted the block at the protocol level but rejected it at validation time)
   d. Read the value via `Node.registry_lookup()` on readonly (exploratory deploy)
   e. Assert data matches the original random string

## Setup

- **Topology**: Session-scoped `shared_shard` (3 validators + readonly)
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled (for automatic block inclusion)

## What it proves

- `rho:registry:insertArbitrary` correctly stores data and returns a `rho:id:` URI
- `rho:registry:lookup` correctly retrieves stored data by URI via exploratory deploy
- Registry state propagates to all nodes (V2, V3, readonly) via block replication
- Finalization is necessary for reliable cross-node registry reads
- Exploratory deploy can read registry state without creating blocks
- Readonly nodes can read finalized registry state

## Key assertions

- Registry URI starts with `rho:id:` (validated in `_store_data`)
- Store deploy data has exactly 1 Par value (`len(data.par) == 1`)
- Store block reaches `isFinalized=True` on every node (`assert_block_finalized_on_all_nodes`)
- Same-node: `read_data == random_data`
- Cross-validator: `read_data == random_data` for every store, read via readonly

## Infrastructure used

- Session-scoped `shared_shard` fixture (3 validators + readonly)
- `Node.deploy_rho_file()` with substitutions for store contract (real deploy)
- `Node.registry_lookup()` for read step (delegates to `f1r3fly.contracts.registry_lookup` via exploratory deploy)
- `wait_for_deploy_included()` from `infra/polling.py`
- `wait_for_finalized()` from `infra/polling.py`
- `assert_block_finalized_on_all_nodes()` from `infra/assertions.py` — asserts `isFinalized=True` on every node
- `par_as_uri` from pyf1r3fly for registry URI extraction (uses `par_as_uri` instead of `par_as_string`)

## Related

- [store-data.rho](../../resources/storage/store-data.rho) -- store contract (writes URI to deployId)
- [test_deployment](test_deployment.md) -- deploy validation tests (complementary)

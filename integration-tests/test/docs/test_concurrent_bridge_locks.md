# test_concurrent_bridge_locks

## Purpose
Verifies exact bridge accounting when one funded wallet submits many unique locks concurrently.

## Tests (1)
- `test_concurrent_bridge_locks_exact_accounting` — bursts 12 locks and reconciles finalized deploys, the final nonce, total locked, and vault balances.

## Setup
A fresh three-validator shard with a readonly observer and one deployed `bridge-v2.rho` instance.

## Key assertions
- Every unique lock finalizes before the readonly observer state is queried.
- The final nonce and total locked value match the exact lock count.
- Contract accounting and bridge-vault balance increase by the exact lock total.

## Infrastructure used
`Shard.create`, `deploy_and_read`, `Node.registry_query`, `VaultAPI.get_balance`, and `assert_all_deploys_finalized_on_all_nodes`.

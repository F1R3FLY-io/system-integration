# test_concurrent_bridge_locks

## Purpose
Verifies exact bridge accounting when one funded wallet submits many unique locks concurrently.

## Tests (1)
- `test_concurrent_bridge_locks_exact_accounting` — bursts 24 locks and reconciles finalized deploys, nonce history, total locked, per-address locked value, and vault balances.

## Setup
A fresh three-validator shard with a readonly observer and one deployed `bridge-v2.rho` instance.

## Key assertions
- Every unique lock finalizes on every validator and the readonly observer before state is queried.
- Lock nonces are contiguous with no missing or duplicate history entries.
- Contract accounting and bridge-vault balance increase by the exact lock total.

## Infrastructure used
`Shard.create`, `deploy_and_read`, `Node.registry_query`, `VaultAPI.get_balance`, and `assert_all_deploys_finalized_on_all_nodes`.

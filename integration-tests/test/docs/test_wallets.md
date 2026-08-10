# test_wallets

## Purpose

Verifies wallet-based token transfers using the PoS vault system. Tests cover happy-path transfers across different validators, authorization failures, insufficient funds, and the Block API transfer extraction feature. Each test submits deploys via different validators to exercise cross-validator transfer handling.

## How wallet transfers work

1. Wallets are vault addresses derived from validator public keys via `PrivateKey.get_public_key().get_vault_address()`
2. Genesis seeds each validator's vault with tokens (defined in `genesis/wallets.txt`)
3. Transfers use the `rho:vault:system` registry contract's `transfer` method
4. Balance queries use the vault's `balance` method via pyf1r3fly's `VaultAPI`

All wallet operations use pyf1r3fly's `VaultAPI`:
- `ro.vault.get_balance(addr)` -- exploratory deploy (restricted to readonly node on Rust node)
- `node.vault.deploy_get_balance(addr, key)` -- real deploy for balance queries on validators (uses `DEPLOY_GET_BALANCE_RHO_TPL`)
- `node.vault.transfer_ensure(from, to, amount, key)` -- deploy with auto-create recipient vault
- `node.vault.read_transfer_result(deploy_id, block_hash)` -- read (success, reason) from deployId channel

The `VaultAPI` constructor takes a `shard_id` parameter (default `'root'`). All deploy methods use `self.shard_id`. Use `node.get_vault(shard_id)` to construct a `VaultAPI` with an explicit shard_id.

The local `_transfer_and_read_result` helper takes an optional `all_nodes` argument; when supplied, it asserts `assert_block_finalized_on_all_nodes` on the transfer block after waiting for finalization on the proposer. All four transfer tests pass `all_nodes=shared_shard.all_nodes` so a peer that rejected the transfer block at validation time fails the test instead of going unnoticed.

## Tests (5)

### test_validator1_pay_validator2

Happy-path transfer submitted via V1:
1. Check V1 balance > 0, record V2 balance via `VaultAPI.get_balance()`
2. Transfer 20,000,000 tokens from V1 to V2 via `VaultAPI.transfer_ensure()`
3. Read transfer result and assert success
4. Poll until V2 balance increased by exactly the transfer amount

### test_validator2_pay_validator3

Happy-path transfer submitted via V2 (exercises a different validator's deploy pipeline):
1. Check V2 balance > 0 via exploratory deploy on readonly, record V3 balance
2. Transfer 10,000,000 tokens from V2 to V3 via V2's node
3. Read transfer result and assert success
4. Poll until V3 balance increased by exactly the transfer amount

### test_transfer_failed_with_invalid_key

Authorization failure submitted via V3: attempts to transfer from V3's vault using V2's private key. Reads the transfer result from the deployId channel and asserts `result.reason == "Invalid AuthKey"`.

### test_transfer_failed_with_insufficient_funds

Overdraw failure submitted via V2: queries V1's balance via exploratory deploy on readonly, then attempts to transfer `balance + 1` tokens. Reads the transfer result and asserts `result.reason == "Insufficient funds"`.

### test_block_api_returns_transfer_info

Verifies that the HTTP Block API exposes transfer details in `DeployInfo`.

1. Ensure V2's vault exists via a balance check
2. Transfer 5,000,000 tokens via `VaultAPI.transfer_ensure()` on V1
3. Find the block containing the transfer deploy
4. Query **both** the readonly node and validator2 via `api_get(f"/block/{hash}")`
5. On each node, verify the deploy has a `transfers` array with correct `fromAddr`, `toAddr`, `amount`, `success`, and empty `failReason`

## Setup

- **Topology**: Session-scoped `shared_shard` (3 validators + readonly)
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled

## What it proves

- Token transfers work end-to-end through the vault system on multiple validators
- Balance accounting is correct (exact amount transferred)
- Authorization is enforced (wrong deployer key rejected)
- Overdraw protection works (insufficient funds rejected)
- BlockReportAPI correctly extracts transfer events on both readonly and validator nodes
- Transfer results are consistent across nodes

## Key assertions

- `result.success == True` (happy path, both V1→V2 and V2→V3)
- `result.reason == "Invalid AuthKey"` (wrong key, via V3)
- `result.reason == "Insufficient funds"` (overdraw, via V2)
- Every transfer block (both success and failure cases) is finalized on every node, asserted via `assert_block_finalized_on_all_nodes`
- Block API: `transfer["fromAddr"]`, `transfer["toAddr"]`, `transfer["amount"]`, `transfer["success"]`, `transfer["failReason"]` verified on both readonly and validator2

## Infrastructure used

- Session-scoped `shared_shard` fixture (3 validators + readonly)
- `Node.vault` (pyf1r3fly VaultAPI) for transfer and result reading
- `ro.vault.get_balance()` for exploratory balance queries (readonly node)
- `node.vault.deploy_get_balance()` for real-deploy balance queries (used in test_validator1_pay_validator2 to cross-check against readonly)
- `Node.api_get()` for HTTP Block API queries on multiple nodes
- `wait_for_deploy_included()` from `infra/polling.py` (delegates to `f1r3fly.polling`)
- `wait_for_finalized()` from `infra/polling.py` (delegates to `f1r3fly.polling`)
- `assert_block_finalized_on_all_nodes()` from `infra/assertions.py` — asserted via `_transfer_and_read_result` helper for every transfer test
- `poll_until()` for balance polling

## Related

- [test_deployment](test_deployment.md) -- deploy syntax handling and cross-node lookup

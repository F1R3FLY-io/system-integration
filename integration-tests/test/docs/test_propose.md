# test_propose

## Purpose

Verifies protocol-v6 deployment on a standalone node with a configured economic margin.

Protocol v6 removes client-selected phlo price and limit fields. Authority-backed admission supplies the execution ceiling.

The shard-based deploy validation and lookup tests that were previously in `tests/shared/test_propose.py` have been merged into [test_deployment](test_deployment.md).

## Tests (1)

### test_protocol_v6_deploy_has_no_client_price_gate (standalone)

1. Starts a standalone node with `--min-phlo-price=10`
2. Verifies `/api/status` reports `minPhloPrice=10` (confirms CLI override took effect)
3. Submits a protocol-v6 deployment without retired phlo fields
4. Verifies the authority-funded deployment enters a block

**What it proves:**
- The status API reports the configured economic margin.
- The deployment API does not require a client-selected price or limit.
- SystemVault authority supplies the deployment execution ceiling.

## Setup

- **Node**: Single standalone node via `provider.create_standalone()`
- **Config**: `--min-phlo-price=10`

## Key assertions

- `status["minPhloPrice"] == 10` — API reports configured value
- `wait_for_deploy_included` succeeds for the protocol-v6 deployment.

## Infrastructure used

- `provider.create_standalone()` / `provider.destroy_standalone()` for standalone node
- `NodeConfig` with `cli_options={"--min-phlo-price": "10"}`
- `Node.deploy_string()` for deploy submission
- `Node.api_get("/status")` for minPhloPrice verification
- `wait_for_deploy_included()` for block inclusion confirmation

## Related

- [test_deployment](test_deployment.md) -- deploy lifecycle and state-bound funding tests

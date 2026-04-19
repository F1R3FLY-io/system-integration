# test_propose

## Purpose

Verifies deploy phlo price validation on a standalone node with custom configuration. This test requires a node started with `--min-phlo-price` set, so it runs as a standalone test (not on the shared shard).

The shard-based deploy validation and lookup tests that were previously in `tests/shared/test_propose.py` have been merged into [test_deployment](test_deployment.md).

## Tests (1)

### test_deploy_phlo_price_too_small (standalone)

1. Starts a standalone node with `--min-phlo-price=10`
2. Verifies `/api/status` reports `minPhloPrice=10` (confirms CLI override took effect)
3. Deploys with `phlo_price=1` — expects `F1r3flyClientException` with message `"Phlo price 1 is less than minimum price 10"`
4. Deploys with `phlo_price=10` — expects success, waits for block inclusion

**What it proves:**
- The node's economic validation layer enforces the configured minimum phlo price
- Deploys below the minimum are rejected immediately at the gRPC API level
- Deploys at the threshold are accepted and included in a block
- `/api/status` correctly reports the configured minPhloPrice

## Setup

- **Node**: Single standalone node via `provider.create_standalone()`
- **Config**: `--min-phlo-price=10`

## Key assertions

- `status["minPhloPrice"] == 10` — API reports configured value
- `pytest.raises(F1r3flyClientException, match="Phlo price 1 is less than minimum price 10")` — below threshold rejected
- `wait_for_deploy_included` succeeds for `phlo_price=10` — at threshold accepted

## Infrastructure used

- `provider.create_standalone()` / `provider.destroy_standalone()` for standalone node
- `NodeConfig` with `cli_options={"--min-phlo-price": "10"}`
- `Node.deploy_string()` for deploy submission
- `Node.api_get("/status")` for minPhloPrice verification
- `wait_for_deploy_included()` for block inclusion confirmation

## Related

- [test_deployment](test_deployment.md) -- deploy lifecycle tests (invalid syntax, insufficient phlo, cross-validator lookup)

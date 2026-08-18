# Native Cost-Accounting Workflows

## Purpose

This shared-shard test pins the application-facing cost-accounting contract
between `f1r3node-rust` and `pyf1r3fly`. It exercises the same public APIs that
an application uses instead of reconstructing node-internal certificates or
funding state.

## Workflow under test

The test performs three sequential workflows against the live multi-validator
shard:

1. Install a one-shot lollipop continuation whose private funding slot remains
   on chain, while publishing only the SystemVault address derived from that
   slot.
2. Transfer REV from an authenticated user wallet into the published address,
   reject a public trigger from an unauthorized signer without consuming the
   continuation, then authenticate the configured gateway and verify that
   exactly the continuation's realized COMM cost is drawn from the slot.
3. Resolve the blessed two-sided Exchange and the bounded capability registry
   through their canonical shorthands, then execute their typed client
   workflows across finalized deployments.

## Assertions

- Every node resolves each state-changing deployment to the same canonical
  finalized block.
- The funding transfer succeeds through SystemVault authentication.
- An unauthorized trigger neither debits the slot nor consumes its one-shot
  continuation; the configured gateway remains able to execute it afterward.
- The slot starts empty, receives the requested funding amount, and loses only
  the realized continuation charge after the lollipop fires.
- The public completion observation contains the gateway request, while the
  private slot name is never published.
- Exchange swaps two existing carrier payloads without minting or dropping a
  payload.
- A bounded capability registration returns an opaque handle and a later
  deployment invokes its retained transformer exactly once.

## Run

```bash
poetry run pytest \
  integration-tests/test/tests/shared/test_cost_accounting.py \
  --provider=subprocess -v
```

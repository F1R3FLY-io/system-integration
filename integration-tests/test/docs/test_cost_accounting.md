# Native Cost-Accounting Workflows

## Purpose

This shared-shard test pins the application-facing cost-accounting contract
between `f1r3node-rust` and `pyf1r3fly`. It exercises the same public APIs that
an application uses instead of reconstructing node-internal certificates or
funding state.

## Workflow under test

The test performs three sequential workflows against the live multi-validator
shard:

1. Install an administrator-paid authentication scaffold that retains fresh
   private outer and continuation names while publishing their two SystemVault
   deposit addresses. The empty installation does not eagerly create a located
   lollipop against candidate-created supply.
2. Transfer REV from an authenticated user wallet into both published purses
   with one atomic batch, reject a public trigger from an unauthorized signer,
   and atomically top up both purses without recreating them. Authenticate the
   configured gateway, instantiate and consume the one-shot lollipop, and
   verify its realized cost, protocol-v8 certificate, and witness.
3. Resolve the blessed two-sided Exchange and the bounded capability registry
   through their canonical shorthands, then execute their typed client
   workflows across finalized deployments.

## Assertions

- Every node resolves each state-changing deployment to the same canonical
  finalized block, reports the same LFB hash, and reconstructs the same LFB
  post-state before the workflow reads balances.
- The funding batch succeeds through SystemVault authentication and credits
  both located purses from one source debit.
- An unauthorized trigger debits neither purse and cannot instantiate the
  one-shot continuation; the configured gateway remains able to execute it
  afterward.
- Both purses start empty, receive their requested funding amounts, and lose
  only their own shares of exact compute-and-byte settlement after activation.
- Public gRPC deploy evidence carries matching protocol and byte-schedule
  identities, adjacent state roots, and a byte charge within its certified
  bound.
- A pre-activation batch refills the same persistent purse pair and produces
  the expected finalized balances; it does not replace the wallet, purses, or
  grant.
- The public scalar cost equals the witness's committed authority-event count
  plus its canonical byte cost, and the observed combined located debit is a
  positive subset of the exact multi-lane execution burn.
- The public completion observation contains the gateway request, while the
  private slot name is never published as a first-class Rholang value or
  channel.
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

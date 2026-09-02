# test_deployment

## Purpose

Verifies syntax validation, state-bound funding rejection, lookup consistency, and exploratory-deploy error propagation.

## Tests

### test_deploy_invalid_syntax_rejected

1. Deploys `resources/invalid.rho` on validator 1.
2. Expects `F1r3flyClientException` from the parser.
3. Immediately deploys a valid unmatched send.
4. Waits for inclusion and verifies the deploy succeeded with zero COMM cost.

### test_deploy_lookup_consistent_across_validators

1. Deploys `@"deploy-lookup-test"!(1)` on validator 1.
2. Resolves the deploy on every validator and the readonly node.
3. Asserts every node returns the same block hash.

### test_unfunded_deploy_rejected_without_stopping_finalization

1. Submits a deployment from a signer without SystemVault funds.
2. Verifies every node records the same terminal rejection block.
3. Verifies the finalized floor advances after the rejection.

### test_exploratory_deploy_invalid_syntax_returns_error

1. Sends malformed Rholang to the readonly node's exploratory endpoint.
2. Asserts parse errors are returned to the client instead of becoming empty results.
3. Repeats the check with a reserved keyword used as a variable.

## Setup

- Shared shard with three validators and one readonly node
- Heartbeat enabled for automatic inclusion
- D3 deploy schema without client-supplied phlo price or limit

## Infrastructure used

- `assert_deploy_succeeded()`
- `wait_for_deploy_included()`
- `wait_for_deploy_finalized()`
- `Node.deploy_string()`, `Node.deploy_rho_file()`, and `Node.get_block()`

# test_expired_deploy_admission

## Purpose
Verifies exact deploy-lifespan boundary enforcement at admission and continued node progress after rejection.

## Tests (1)
- `test_expired_deploy_rejected_at_admission` — rejects a deploy at the expired boundary, accepts one block inside the window, and verifies the rejected deploy never enters the DAG.

## Setup
A fast-heartbeat standalone node first reaches the configured 50-block lifespan. The test permits 240 scaled seconds for this multi-block transition.

The node then reports the exact next candidate height and admission boundary.

## Key assertions
- A deploy whose VABN is exactly `next candidate height - lifespan` is rejected immediately.
- A deploy one block inside the lifespan is accepted and finalized.
- The rejected deploy is not recoverable through deploy lookup and the node continues finalizing.

## Infrastructure used
`Node.send_deploy`, `Node.find_deploy`, `provider.create_standalone`, and `wait_for_deploy_finalized`.

# test_convergence

## Purpose

Verifies network recovery after a validator pause or a finite authority-capacity exhaustion.

The tests also verify that fault-tolerance values converge across nodes.

## Tests (3)

### test_network_recovers_from_validator_pause

Simulates a temporary network partition by pausing V1's Docker container for 30 seconds:

1. Deploy on all three validators to create active state before the pause
2. Record the baseline LFB
3. Pause V1 while V2 and V3 produce heartbeat blocks
4. Unpause V1 and deploy on all three validators again
5. Wait for the LFB to advance by at least three blocks on every node
6. Verify post-recovery fault tolerance meets the configured threshold

The test allows `DAGStorageMissingHash` because the paused validator can legitimately report missing hashes while it backfills from peers.

### test_ft_convergence

Verifies that cached fault tolerance for finalized blocks converges to 1.0 across all nodes:

1. Wait for the LFB to advance past genesis
2. Select a finalized ancestor block
3. Verify its fault tolerance meets the configured threshold on V1
4. Poll every node until each reports fault tolerance 1.0
5. Verify the value remains stable

### test_network_converges_after_capacity_exhaustion

1. Funds one dedicated signer with a finite 20,000,000-unit SystemVault balance.
2. Submits a recursive workload that consumes that finite capacity.
3. Lets the other validators create heartbeat blocks during the slow evaluation.
4. Verifies the rejected deployment has no payer-vault effect.
5. Verifies all nodes regain a bounded LFB spread and valid fault tolerance.

## Setup

- Module-scoped shard with three validators and one readonly node
- Fault-tolerance threshold from `conf/rust.conf`
- Heartbeat enabled for independent block production during disruption
- Dedicated finite SystemVault funding for the capacity-exhaustion signer

## Key assertions

- Every node advances its LFB after V1 resumes
- Every post-recovery LFB meets the configured fault-tolerance threshold
- Every node eventually reports fault tolerance 1.0 for the selected finalized block
- Cached fault tolerance never decreases after convergence
- Capacity exhaustion cannot debit the rejected deployment's payer vault.

## Infrastructure used

- `Node.pause()` and `Node.unpause()`
- `Node.deploy_string()`
- `Node.get_block()` and `Node.last_finalized_block()`
- `poll_until()`
- `check_node_logs_after_test`

## Related

- f1r3node#462 — fault-tolerance caching fix
- [test_dag_correctness](test_dag_correctness.md) — DAG structure and fault-tolerance cache correctness
- [test_heartbeat](test_heartbeat.md) — heartbeat-driven block production

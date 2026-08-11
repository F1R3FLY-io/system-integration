# test_convergence

## Purpose

Verifies that the network recovers from DAG tip divergence after a temporary validator pause and that fault tolerance values converge across nodes.

## Tests (2)

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

## Setup

- Session-scoped `shared_shard` with three validators and one readonly node
- Fault-tolerance threshold from `conf/rust.conf`
- Heartbeat enabled for independent block production during disruption

## Key assertions

- Every node advances its LFB after V1 resumes
- Every post-recovery LFB meets the configured fault-tolerance threshold
- Every node eventually reports fault tolerance 1.0 for the selected finalized block
- Cached fault tolerance never decreases after convergence

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

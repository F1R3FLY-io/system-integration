# test_readonly_catchup_bounded

## Purpose
Verifies that readonly block-processing parallelism keeps catch-up memory bounded and the status API responsive.

## Tests (1)
- `test_readonly_catchup_parallelism_keeps_api_responsive` — attaches an empty observer to a 40-block DAG while sampling RSS and continuously probing status.

## Setup
A fast-heartbeat three-validator shard with at least 40 blocks and a transient readonly observer.

## Key assertions
- Status requests continue succeeding within two seconds during catch-up.
- Observer RSS remains below 1.5 GiB.
- The observer reaches the target LFB with the same post-state and serves exploratory queries.

## Infrastructure used
`Shard.add_observer`, `Node.resource_usage`, `/api/status`, `wait_for_node_running`, `wait_for_lfb_at_least`, and `wait_for_block_visible`.

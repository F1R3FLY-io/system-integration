# test_finality_stall_recovery

## Purpose
Verifies bounded empty-block recovery during a finality stall and prompt pending-deploy progress after quorum returns.

## Tests (1)
- `test_finality_stall_bounded_recovery` — isolates one validator until empty-frontier backpressure activates, measures recovery churn, submits a deploy under pressure, and restores quorum.

## Setup
A fresh three-validator shard with one-second heartbeat timing and a four-block empty-frontier cap.

## Key assertions
- A stalled LFB produces at most one bounded recovery round per timeout window.
- A real pending deploy bypasses empty-frontier pressure and is included.
- The deploy finalizes on all nodes and LFBs converge after quorum returns.

## Infrastructure used
`Shard.create`, `Node.pause`, `Node.unpause`, node logs, `wait_for_deploy_included`, `assert_all_deploys_finalized_on_all_nodes`, and `wait_for_lfb_converged`.

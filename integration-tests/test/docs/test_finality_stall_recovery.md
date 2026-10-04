# test_finality_stall_recovery

## Purpose
Verifies that finality stalls cleanly under quorum loss — empty recovery
blocks emitted only at the bounded stale-recovery cadence, with the
empty-frontier width cap engaged between rounds — and that pending work and
finality recover once quorum returns.

## Tests (1)
- `test_finality_stall_bounded_recovery` — pauses two of three validators,
  measures the survivor's recovery emission over a fixed 45s window, submits
  a deploy under pressure, and restores quorum.

## Setup
A fresh three-validator shard (equal stakes) with pinned heartbeat knobs:
2s check interval, 5s max-LFB-age, 5s self-propose cooldown, 5s
stale-recovery-min-interval, and a four-block empty-frontier cap. The check
interval is deliberately below the stale-recovery interval so at least one
heartbeat check always lands inside the survivor's post-mint throttle
window, making the backpressure log line structural evidence rather than a
phase-drift lottery.

## Key assertions
- The LFB holds exactly still while quorum is gone.
- Emission in the 45s window is between 1 and `floor(45/5) + 3`: the lower
  bound proves the stale-recovery lane is alive (a dead lane cannot pass
  vacuously), the ceiling is derived from the per-interval cadence plus one
  leader-dependent convergence one-shot and one async-propose slack. A
  regression to per-check minting (~22 blocks) fails cleanly.
- The `HeartbeatBackpressureActive` marker fires at least once in the
  window — positive proof the width cap engaged between recovery rounds.
- A real pending deploy bypasses empty-frontier pressure and is included
  while stalled.
- The deploy finalizes on all nodes and every LFB passes
  `max(baseline + 3, inclusion height)` within spread 5 after quorum
  returns.

## Infrastructure used
`Shard.create`, `Node.pause`, `Node.unpause`, node logs with
`log_events.marker` (`HeartbeatBlockCreated`, `HeartbeatBackpressureActive`),
`wait_for_node_quiet`, `wait_for_deploy_included`,
`assert_all_deploys_finalized_on_all_nodes`, and `wait_for_lfb_converged`.

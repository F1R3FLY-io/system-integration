# test_slow_peer_notification

## Purpose
Provides a black-box regression for bounded block-hash notifications when one recipient is unresponsive.

## Tests (1)
- `test_slow_peer_does_not_block_block_processing` — pauses one validator, submits concurrent work through the active quorum, and requires continued finalization before restoring the peer.

## Setup
A fresh three-validator shard with FTT `0.1`, a 15-second network timeout, and one paused peer.

## Key assertions
- Block processing and finality continue while one notification recipient is unavailable.
- All submitted deploys finalize on the active quorum.
- The active APIs remain ready and the restored peer catches up.

## Infrastructure used
`Shard.create`, `Node.pause`, `Node.unpause`, `assert_all_deploys_finalized_on_all_nodes`, `wait_for_lfb_at_least`, and `wait_for_lfb_converged`.

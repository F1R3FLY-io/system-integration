# test_observer_missing_block_retry

## Purpose
Verifies that an observer keeps an initial missing-block request scheduled when its source peer temporarily disappears.

## Tests (1)
- `test_observer_retries_missing_block_after_peer_returns` — interrupts every source during initial LFS block retrieval, restores them, and requires the observer to finish without restart.

## Setup
A heartbeat-enabled three-validator shard with five sequential finalized deploy blocks and a transient readonly observer configured with a short network timeout.

## Key assertions
- Failed initial retrieval is explicitly retained for retry.
- The observer reaches Running after peers return.
- The observer catches up to the pre-interruption LFB with the same post-state hash.

## Infrastructure used
`Shard.add_observer`, `Node.pause`, `Node.unpause`, `wait_for_node_running`, `wait_for_lfb_at_least`, and `wait_for_block_visible`.

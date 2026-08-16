# test_transient_peer_liveness

## Purpose
Verifies that peer cleanup tolerates transient heartbeat failures while still removing persistently unavailable peers.

## Tests (1)
- `test_transient_peer_failure_does_not_disconnect` — checks first-failure retention, success-driven streak reset, third-consecutive-failure removal, and rediscovery.

## Setup
A fresh three-validator shard with one-second network timeouts and two-second peer cleanup and discovery intervals.

## Key assertions
- One failed heartbeat retains the peer.
- A successful heartbeat resets the failure streak.
- Three consecutive failures remove the peer.
- The restored peer is rediscovered.

## Infrastructure used
`Shard.create`, `Node.pause`, `Node.unpause`, `/api/status`, node logs, `wait_for_node_quiet`, and `poll_until`.

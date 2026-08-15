# test_cold_start_readiness

## Purpose
Verifies that API readiness is gated by availability of a last finalized block during cold start.

## Tests (1)
- `test_cold_start_readiness_requires_lfb` — samples status throughout genesis and requires `isReady=false` at LFB `-1`, then `isReady=true` only after an LFB exists.

## Setup
A fresh three-validator shard created without waiting for Running so the test can sample the startup window.

## Key assertions
- The HTTP API is observable before the first LFB.
- Every status response with LFB `-1` reports not ready.
- Every node eventually reports ready with a non-negative LFB.

## Infrastructure used
`provider.create_shard(wait_running=False)`, `/api/status`, `wait_for_node_running`, and `poll_until`.

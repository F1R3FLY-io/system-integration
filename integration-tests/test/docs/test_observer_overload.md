# test_observer_overload

## Purpose
Verifies bounded exploratory-query admission and API recovery on a readonly observer.

## Tests (1)
- `test_observer_exploratory_overload_recovers` — occupies the exploratory executor, sends eight excess requests, samples observer memory, and verifies fail-fast overload responses followed by a successful query.

## Setup
A fresh three-validator shard with one readonly observer.

## Key assertions
- Excess requests return HTTP 503 with the `observer_busy` error kind.
- Observer memory remains below the same 1.5 GB ceiling used by the bounded catch-up regression.
- The observer remains ready while rejecting overload.
- The slow query either completes, exhausts phlo, or reaches the bounded execution timeout.
- A normal exploratory query succeeds after the slow query releases the permit.

## Infrastructure used
`Shard.create`, `Node.resource_usage`, the observer HTTP API, and `/api/status`.

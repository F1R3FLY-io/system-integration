# test_duplicate_signed_deploy

## Purpose
Verifies that concurrent submissions of one byte-identical signed deploy are admitted atomically and execute once.

## Tests (1)
- `test_duplicate_signed_deploy_race` — submits one signed deploy 32 times concurrently and requires one acceptance, 31 duplicate rejections, and one canonical execution.

## Setup
A heartbeat-enabled standalone node and one pre-built deploy signed by the funded bootstrap key.

## Key assertions
- Exactly one concurrent submission is accepted.
- Every rejected submission is classified as a duplicate.
- The canonical block contains the deploy exactly once.

## Infrastructure used
`Node.send_deploy`, `Node.get_block`, `provider.create_standalone`, and `wait_for_deploy_finalized`.

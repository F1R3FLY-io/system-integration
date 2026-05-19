# test_deployment

## Purpose

Verifies the full deploy lifecycle on a shared shard: syntax validation at the API level, error handling for insufficient phlo, and cross-validator deploy lookup consistency. These are regression tests for previously critical bugs where certain deploy failures triggered `NeglectedInvalidBlock` crashes.

## Background

When a deploy is submitted:
1. **Syntax check** — the node parses the Rholang source. Invalid syntax is rejected immediately at the gRPC API level (never reaches a block).
2. **Inclusion** — valid deploys are queued and included in the next proposed block (via heartbeat or manual propose).
3. **Execution** — the deploy runs with its phlo budget. If phlo is exhausted, the deploy is marked `errored=True` in the block.
4. **Propagation** — the block propagates to all validators. Every validator should resolve the same deploy ID to the same block hash.

The insufficient-phlo fix involved correcting non-deterministic ordering in `EventLogIndex`, `DeployChainIndex`, and `ConflictSetMerger`, plus adding transient-error recovery in the Proposer.

## Tests (3)

### test_deploy_invalid_syntax_rejected

1. Deploys `resources/invalid.rho` (contains `out,|` — invalid Rholang syntax) on V1
2. Expects `F1r3flyClientException` from the gRPC API (parser rejects it)
3. Immediately deploys a valid contract `@"valid-after-invalid"!(42)` on V1
4. Waits for the valid deploy to be included in a block via `wait_for_deploy_included`
5. Fetches the full block and calls `assert_deploy_succeeded(block_info, deploy_id)` to verify the deploy is not errored and has cost > 0

**What it proves:**
- Invalid syntax is rejected at the API level (never reaches a block)
- The rejection does not poison the deploy pipeline — subsequent valid deploys succeed normally

### test_deploy_insufficient_phlo_errored

1. Deploys `@1!(1)` with `phlo_limit=10` on V1 (too low — even this minimal contract costs ~97 phlo)
2. Heartbeat auto-proposes the block
3. Waits for deploy inclusion via `wait_for_deploy_included`
4. Fetches the full block and calls `assert_deploy_errored(block_info, deploy_id)` to verify the deploy is marked errored
5. Polls until **all nodes** (validators + readonly) advance LFB by 3+ blocks past the errored deploy's block

**What it proves:**
- Deploys with insufficient phlo are accepted but marked as errored (not rejected at API level)
- The proposer correctly includes errored deploys in blocks without crashing
- The `NeglectedInvalidBlock` regression is fixed
- All nodes continue operating after an errored deploy (LFB advances by 3+)

### test_deploy_lookup_consistent_across_validators

1. Deploys `@"deploy-lookup-test"!(1)` on V1
2. Calls `wait_for_deploy_included` on **every node** (all validators + readonly)
3. Collects the block hash from each node's response
4. Asserts all nodes resolved the deploy to the same block hash

**What it proves:**
- Block propagation works — all nodes see the same deploy in the same block
- `find_deploy` returns consistent results across the network
- Readonly nodes can resolve deploys (not just validators)

## Setup

- **Topology**: Session-scoped `shared_shard` (3 validators + readonly)
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled (for automatic block inclusion)

## Key assertions

- Invalid syntax: `F1r3flyClientException` raised, then `assert_deploy_succeeded` on the follow-up deploy
- Insufficient phlo: `assert_deploy_errored(block_info, deploy_id)` (delegates to `f1r3fly.deploy.check_deploy_errored`)
- All nodes advance LFB by 3+ blocks after errored deploy
- Deploy lookup: all nodes return the same block hash (`len(unique_hashes) == 1`)

## Infrastructure used

- Session-scoped `shared_shard` fixture (3 validators + readonly)
- `assert_deploy_succeeded()`, `assert_deploy_errored()` from `infra/assertions.py` (delegates to `f1r3fly.deploy`)
- `wait_for_deploy_included()` from `infra/polling.py` (delegates to `f1r3fly.polling`)
- `poll_until()` for LFB advancement polling
- `Node.deploy_string()`, `Node.deploy_rho_file()`, `Node.get_block()`

## Related

- [test_propose (standalone)](test_propose.md) -- phlo price validation (rejected at API level, requires custom node config)
- [resources/invalid.rho](../../resources/invalid.rho) -- syntactically invalid Rholang contract

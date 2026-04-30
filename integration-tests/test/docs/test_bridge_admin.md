# test_bridge_admin

## Purpose

Verifies the bridge contract's deployment, URI registration, and query API. The bridge contract (bridge-v2.rho) is a persistent Rholang contract that manages cross-chain bridging state (nonce, locked amounts, vault address) with query, lock, and unlock entry points.

Two test paths exercise the same lifecycle through different mechanisms:
- **Exploratory deploy** (readonly node): fast, no blocks for queries
- **Real deploy** (validators): exercises cross-validator merge pipeline

This was the test that originally exposed the Blake2b512Random count_view index bug causing GPrivate ID collisions (fixed in PR #468).

## How the bridge API works

1. **Deploy `bridge-v2.rho`**: Initializes state channels, creates bridge vault, registers 3 contracts in the registry (query, lock, unlock), and emits their URIs to `deployId`
2. **Query calls** (`getNonce`, `getTotalLocked`, `getAddress`): Lookup query URI in registry, call method with parameters, read result from `deployId`

Each query operation uses exploratory deploy via `node.registry_query()` — a read-only call that executes instantly without creating a block or consuming phlo. Only the initial bridge deployment is a real deploy.

## Tests (2)

### test_bridge_api_exploratory

Deploy bridge, then query via exploratory deploy on readonly:
1. Deploy `bridge-v2.rho` on **V1** (real deploy — creates state), extract 3 `rho:id:...` URIs
2. Verify all nodes finalized past the bridge block via `wait_for_finalized`
3. Query `getNonce` on **readonly** via `registry_query()` -> verify returns integer 0
4. Query `getTotalLocked` on **readonly** via `registry_query()` -> verify returns integer 0
5. Query `getAddress` on **readonly** via `registry_query()` -> verify returns non-empty vault address string

Queries use `Node.registry_query()` (exploratory deploy — instant, no block created, no phlo consumed).

### test_bridge_api_real_deploy

Deploy bridge, then query via real deploys across validators:
1. Deploy `bridge-v2.rho` on **V1** (real deploy — creates state), extract 3 `rho:id:...` URIs
2. Verify all nodes finalized past the bridge block via `wait_for_finalized`
3. Query `getNonce` on **V1** via real deploy -> verify returns integer 0
4. Query `getTotalLocked` on **V2** via real deploy -> verify returns integer 0
5. Query `getAddress` on **V3** via real deploy -> verify returns non-empty vault address string

Queries use `deploy_and_read()` (real deploy — creates blocks, exercises cross-validator merge pipeline). Each query is submitted to a different validator.

## Setup

- **Topology**: Session-scoped `shared_shard` (3 validators + readonly)
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled (for automatic block inclusion)

## What it proves

- The bridge contract deploys correctly and registers exactly 3 `rho:id:` URIs
- getNonce returns an integer (initial value 0)
- getTotalLocked returns an integer (initial value 0)
- getAddress returns a non-empty vault address string
- Exploratory deploy works for complex contract queries with persistent state channels
- Real deploy queries work across validators (contract deployed on V1, queried from V2/V3)
- Finalization-gated reads prevent stale data

## Key assertions

- Bridge deploy: `par_as_list` finds exactly 3 URI elements, each a `rho:id:` URI via `par_as_uri`
- getNonce: `par_as_int` returns 0
- getTotalLocked: `par_as_int` returns 0
- getAddress: `par_as_string` returns non-empty string
- Deploy not errored: verified by `deploy_and_read` -> `check_deploy_not_errored`

## Infrastructure used

- Session-scoped `shared_shard` fixture (3 validators + readonly)
- `deploy_and_read()` from `infra/polling.py` for bridge deployment and real-deploy queries
- `Node.registry_query()` for exploratory deploy queries (delegates to `f1r3fly.contracts.registry_query`)
- `wait_for_finalized()` from `infra/polling.py` for all-node finalization check
- `par_as_list`, `par_as_uri`, `par_as_int`, `par_as_string` from pyf1r3fly for typed Par extraction
- `check_node_logs_after_test` autouse fixture for fatal-log detection (panics + `FATAL_PATTERNS`; see [ARCHITECTURE.md § 7](ARCHITECTURE.md#7-log-scanning))

## Related

- [bridge-v2.rho](../../resources/bridge-v2.rho) -- the bridge contract
- [bridge.rho](../../resources/bridge.rho) -- the original bridge contract (v1, with admin API)
- f1r3node-rust PR #468 -- Blake2b512Random count_view bug fix (resolved bridge admin issue)

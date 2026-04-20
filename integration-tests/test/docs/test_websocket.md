# test_websocket

## Purpose

Verifies the `/ws/events` WebSocket endpoint on F1R3FLY nodes. This endpoint streams real-time block lifecycle events, genesis ceremony events, and node lifecycle events. The node buffers startup events and replays them to new WebSocket subscribers on connect.

## Event types (9)

| Category | Event | When |
|----------|-------|------|
| Block lifecycle | `block-created` | Proposer built a block (before validation) |
| Block lifecycle | `block-added` | Block validated and added to DAG |
| Block lifecycle | `block-finalised` | Block finalized by the finalizer |
| Genesis ceremony | `sent-unapproved-block` | Boot broadcasts candidate |
| Genesis ceremony | `block-approval-received` | Boot receives validator approval |
| Genesis ceremony | `sent-approved-block` | Boot broadcasts approved genesis |
| Genesis ceremony | `approved-block-received` | Validator receives approved block |
| Node lifecycle | `entered-running-state` | Engine transitions to Running |
| Node lifecycle | `node-started` | HTTP server is ready |

## Tests (5)

### test_block_events

Verifies all 3 block lifecycle events are received on validator1 with correct structure:
- `schema-version == 1`
- Payload contains: `block-hash`, `parent-hashes`, `justification-hashes`, `deploys`, `creator`, `seq-num`
- Types are correct (string block-hash, list parent-hashes, int seq-num)

Uses `validate_block_event()` from `f1r3fly/websocket.py`.

### test_startup_events_validator

Verifies validator1 receives all startup events (live, since WS connects before Running):
- `node-started` with `address` in payload
- `approved-block-received` with `block-hash` in payload
- `entered-running-state` with `block-hash` in payload

### test_startup_events_boot

Verifies boot receives all genesis ceremony events:
- `sent-unapproved-block` and `sent-approved-block` with `block-hash`
- `block-approval-received` with `block-hash` and `sender`
- `entered-running-state` and `node-started`
- Plus `block-added` and `block-finalised` from heartbeat

### test_startup_events_readonly

Verifies the readonly observer receives block lifecycle and startup events. Readonly receives `block-added` and `block-finalised` but NOT `block-created` (it doesn't propose). Also receives `node-started`, `entered-running-state`, and `approved-block-received`. Asserts `block-created` is absent.

### test_deploy_appears_in_block_event

Deploys a contract after the WebSocket client is connected, then verifies the deploy_id appears in a `block-created` or `block-added` event's `deploys` list. The `deploys` field is a list of objects with `id`, `cost`, `deployer`, `errored`. The test verifies all four fields: `id` matches deploy_id, `cost >= 0`, `deployer` matches V1's public key, `errored` is false.

## Setup

- **Topology**: Custom 2-validator shard (60/40 bonds) + readonly observer
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled
- **include_readonly**: True
- **WebSocket**: Connected to boot, validator1, and readonly BEFORE nodes reach Running state (receives genesis/startup events live, not just from replay buffer). Shard created with `wait_running=False`, WS clients connect as soon as HTTP ports are listening, then `wait_for_node_running` is called.
- **WsShardResult**: Stores events/errors for boot, v1, and readonly (`ro_events`/`ro_errors`)

## Infrastructure used

- `provider.create_shard(config, wait_running=False)` for early WS connect, then manual `wait_for_node_running`
- `Shard()` constructor (not `Shard.create()`) with pre-created handles
- `connect_ws()`, `wait_for_events()`, `validate_block_event()`, `log_event_counts()` from `f1r3fly/websocket.py`
- Event type constants (`BLOCK_LIFECYCLE_EVENTS`, `EXPECTED_BOOT_EVENTS`, `EXPECTED_VALIDATOR_EVENTS`, `VALIDATOR_STARTUP_EVENTS`) from `f1r3fly/websocket.py`
- `Node.ws_url` property for WebSocket URL
- `Node.deploy_string()`, `wait_for_deploy_included()` from `infra/polling.py`
- Threading for concurrent WS event collection

## Related

- [test_genesis_ceremony](test_genesis_ceremony.md) -- verifies genesis via gRPC/logs (complementary)

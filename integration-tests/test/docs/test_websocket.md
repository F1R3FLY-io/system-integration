# test_websocket

## Purpose

Verifies the `/ws/events` WebSocket endpoint on F1R3FLY nodes. This endpoint streams real-time block lifecycle events, genesis ceremony events, transfer extraction events, and node lifecycle events. The node buffers startup events and replays them to new WebSocket subscribers on connect.

## Event types (10)

| Category | Event | When |
|----------|-------|------|
| Block lifecycle | `block-created` | Proposer built a block (before validation) |
| Block lifecycle | `block-added` | Block validated and added to DAG |
| Block lifecycle | `block-finalised` | Block finalized by the finalizer |
| Transfer | `transfers-available` | Transfer extraction completed after finalization (readonly only) |
| Genesis ceremony | `sent-unapproved-block` | Boot broadcasts candidate |
| Genesis ceremony | `sent-approved-block` | Boot broadcasts approved genesis |
| Genesis ceremony | `approved-block-received` | Validator receives approved block |
| Node lifecycle | `entered-running-state` | Engine transitions to Running |
| Node lifecycle | `node-started` | HTTP server is ready |

Block events include `block-number` and `timestamp` fields. Deploy events within blocks include optional `transfers` (omitted on `block-created`/`block-added`).

## Tests (6)

### test_block_events

Verifies all 3 block lifecycle events are received on validator1 with correct structure:
- `schema-version == 1`
- Payload contains: `block-hash`, `block-number`, `timestamp`, `parent-hashes`, `justification-hashes`, `deploys`, `creator`, `seq-num`
- Types are correct (string block-hash, int block-number, int timestamp, list parent-hashes, int seq-num)

Uses `validate_block_event()` from `f1r3fly/websocket.py`.

### test_startup_events_validator

Verifies validator1 receives all startup events (live, since WS connects before Running):
- `node-started` with `address` in payload
- `approved-block-received` with `block-hash` in payload
- `entered-running-state` with `block-hash` in payload

### test_startup_events_boot

Verifies boot receives genesis ceremony events:
- `sent-unapproved-block` and `sent-approved-block` with `block-hash`
- `entered-running-state` and `node-started`
- Plus `block-added` and `block-finalised` from heartbeat

### test_startup_events_readonly

Verifies the readonly observer receives block lifecycle and startup events. Readonly receives `block-added` and `block-finalised` but NOT `block-created` (it doesn't propose). Also receives `node-started`, `entered-running-state`, and `approved-block-received`. Asserts `block-created` is absent.

### test_deploy_appears_in_block_event

Deploys a contract after the WebSocket client is connected, then verifies the deploy_id appears in a `block-created` or `block-added` event's `deploys` list. Verifies `id`, `cost`, `deployer`, `errored` fields. Also verifies `transfers` is absent on `block-created`/`block-added` events (transfer extraction hasn't happened yet).

### test_transfers_available_event

Submits a vault transfer on a validator, then verifies a `transfers-available` event is received on the **readonly** WebSocket with correct `block-hash`, `block-number`, and deploy transfer data. This event fires asynchronously after block report cache warming on readonly nodes only.

## Setup

- **Topology**: Custom 2-validator shard (60/40 bonds) + readonly observer
- **Heartbeat**: Enabled
- **include_readonly**: True
- **WebSocket**: Connected to boot, validator1, and readonly BEFORE nodes reach Running state. Shard created with `wait_running=False`, WS clients connect early, then `wait_for_node_running` (using `isReady` status API) is called.
- **WsShardResult**: Stores events/errors for boot, v1, and readonly

## Infrastructure used

- `wait_for_node_running` with `status_url` for `isReady` API polling
- `connect_ws()`, `wait_for_events()`, `validate_block_event()` from `f1r3fly/websocket.py`
- `Node.vault.transfer_ensure()` for transfer deploy (transfers-available test)
- Threading for concurrent WS event collection

## Related

- [test_genesis_ceremony](test_genesis_ceremony.md) -- verifies genesis via gRPC/logs (complementary)

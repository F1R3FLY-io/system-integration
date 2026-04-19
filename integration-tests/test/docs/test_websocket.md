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

## Tests (3)

### test_block_events

Verifies all 3 block lifecycle events are received on validator1 with correct structure:
- `schema-version == 1`
- Payload contains: `block-hash`, `parent-hashes`, `justification-hashes`, `deploys`, `creator`, `seq-num`
- Types are correct (string block-hash, list parent-hashes, int seq-num)

### test_startup_events_validator

Verifies validator1 receives all startup events (replayed from buffer):
- `node-started` with `address` in payload
- `approved-block-received` with `block-hash` in payload
- `entered-running-state` with `block-hash` in payload

### test_startup_events_boot

Verifies boot receives all genesis ceremony events:
- `sent-unapproved-block` and `sent-approved-block` with `block-hash`
- `block-approval-received` with `block-hash` and `sender`
- `entered-running-state` and `node-started`
- Plus `block-added` and `block-finalised` from heartbeat

## Setup

- **Topology**: Custom 2-validator shard (60/40 bonds)
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled
- **WebSocket**: Connected to boot and validator1 after shard startup (receives buffered events via replay)

## Infrastructure used

- `Shard.create()` / `shard.destroy()` lifecycle
- `websocket.WebSocketApp` for async WebSocket connections
- Threading for concurrent WS event collection
- `_wait_for_events()` for event type accumulation polling

## Related

- [test_genesis_ceremony](test_genesis_ceremony.md) -- verifies genesis via gRPC/logs (complementary)

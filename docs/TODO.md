# TODO

## f1r3node: WebSocket event stream spams ERROR on client disconnect

**File:** `node/src/rust/web/events_info.rs`, lines 38-42

When a WebSocket client disconnects from `/ws/events`, `handle_websocket` logs `ERROR` on every subsequent send attempt (`Broken pipe (os error 32)`) but keeps looping — spamming the log with identical errors until the event stream ends.

**Fix:**

1. `break` out of the loop when `send_event_to_websocket` returns a connection error (broken pipe, connection reset). The client is gone.
2. Downgrade from `error!` to `debug!` or `warn!` — a client disconnecting is normal operation, not an error.

## f1r3node: Scala node crashes with enable-mergeable-channel-gc during genesis

Tracked in [f1r3node#441](https://github.com/F1R3FLY-io/f1r3node/issues/441).

When `enable-mergeable-channel-gc = true`, the Scala node bootstrap crashes during genesis ceremony if an observer connects before the genesis block is approved. The Rust node handles this gracefully.

**Workaround:** All Scala compose services pass `--disable-mergeable-channel-gc` via CLI. Once the Scala bug is fixed, this flag can be removed from Scala compose files.

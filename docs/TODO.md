# TODO

## f1r3node-rust: WebSocket event stream spams ERROR on client disconnect

**File:** `node/src/rust/web/events_info.rs`, lines 38-42

When a WebSocket client disconnects from `/ws/events`, `handle_websocket` logs `ERROR` on every subsequent send attempt (`Broken pipe (os error 32)`) but keeps looping — spamming the log with identical errors until the event stream ends.

**Fix:**

1. `break` out of the loop when `send_event_to_websocket` returns a connection error (broken pipe, connection reset). The client is gone.
2. Downgrade from `error!` to `debug!` or `warn!` — a client disconnecting is normal operation, not an error.

## Configuration Notes

### Settings Without CLI Flags

Two HOCON settings **cannot** be overridden via CLI flags and must be set in config files:

#### `enable-mergeable-channel-gc`

- Set to `true` for both Rust and Scala nodes
- Set in `conf/default.conf`

#### `ceremony-master-mode`

- Only the bootstrap node sets this to `true`
- This is why bootstrap needs a separate `bootstrap.conf` that includes `default.conf` and overrides this single setting
- All other roles (validators, observer) use `default.conf` with the default `false`

### Heartbeat Configuration

Heartbeat is disabled for non-validator nodes via HOCON config overrides:

- `bootstrap.conf` and `observer.conf` both set `casper.heartbeat.enabled = false`
- `default.conf` has `heartbeat.enabled = true` (inherited by validators only)
- `--heartbeat-disabled` CLI flag exists in Rust node (`options.rs:439`) but is **not used** because Scala does not support it — HOCON override keeps both node types consistent

## Completed: Unified default.conf

Rust and Scala nodes now share a single `conf/` directory with unified config files (`default.conf`, `bootstrap.conf`, `observer.conf`, `standalone-dev.conf`). Scala-only `logback.xml` is also in `conf/`.


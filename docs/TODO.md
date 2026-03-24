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

## F1R3_* env vars override HOCON and CLI settings at runtime

Rust nodes read `F1R3_*` env vars via `OnceLock` on first use. These **override** both HOCON config values and CLI flags. This creates a subtle interaction:

- `F1R3_SYNCHRONY_CONSTRAINT_THRESHOLD` overrides `--synchrony-constraint-threshold` CLI flag
- The integration test `test_synchrony_constraint` sets per-validator thresholds via CLI, so `F1R3_SYNCHRONY_CONSTRAINT_THRESHOLD` must NOT be in the custom shard env (conftest.py `rust_env`)
- The static integration compose (`docker-compose.rust.yml`) does include it in the x-rnode anchor since all validators use the same threshold there

**Rule:** Any `F1R3_*` var that a test sets per-node via CLI must be excluded from `rust_env` in conftest.py. Currently only `F1R3_SYNCHRONY_CONSTRAINT_THRESHOLD` is excluded.

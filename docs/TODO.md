# TODO

## f1r3node: WebSocket event stream spams ERROR on client disconnect

**File:** `node/src/rust/web/events_info.rs`, lines 38-42

When a WebSocket client disconnects from `/ws/events`, `handle_websocket` logs `ERROR` on every subsequent send attempt (`Broken pipe (os error 32)`) but keeps looping — spamming the log with identical errors until the event stream ends.

**Fix:**

1. `break` out of the loop when `send_event_to_websocket` returns a connection error (broken pipe, connection reset). The client is gone.
2. Downgrade from `error!` to `debug!` or `warn!` — a client disconnecting is normal operation, not an error.

## ~~f1r3node: Scala node crashes with enable-mergeable-channel-gc during genesis~~ (Fixed)

Tracked in [f1r3node#441](https://github.com/F1R3FLY-io/f1r3node/issues/441). Fixed — `--disable-mergeable-channel-gc` removed from all Scala compose files.

## F1R3_* env vars override HOCON and CLI settings at runtime

Rust nodes read `F1R3_*` env vars via `OnceLock` on first use. These **override** both HOCON config values and CLI flags at runtime. Docker Compose's `--env-file` passes all uncommented vars to containers, even if they're not declared in the compose YAML `environment:` section.

This means any `F1R3_*` var in `.env.node` will reach **every** container started with `--env-file .env.node`, including integration test custom shards.

### What's commented out in `.env.node` and why

All `F1R3_SYNCHRONY_*` vars are commented out in `.env.node` because `test_synchrony_constraint` sets per-validator synchrony thresholds via CLI. If these env vars are active:
- `F1R3_SYNCHRONY_CONSTRAINT_THRESHOLD` overrides `--synchrony-constraint-threshold` CLI flag
- `F1R3_SYNCHRONY_FINALIZED_BASELINE_ENABLED` enables a more permissive synchrony check that bypasses the threshold the test expects to trigger
- `F1R3_SYNCHRONY_RECOVERY_*` vars change recovery behavior during the test

The topology compose files (`compose/f1r3node-rust.yml`) declare these vars in their `environment:` section with `${VAR:-default}` syntax, so they use the inline defaults regardless of `.env.node`.

### Rule

Any `F1R3_*` var that could interfere with per-validator CLI settings in integration tests must be commented out in `.env.node` and set only in compose YAML `environment:` sections.

## Monitoring stack alignment across repos

The monitoring setup (Prometheus, Grafana, cAdvisor) is inconsistent across the three repos:

| Aspect | system-integration | f1r3node-rust | f1r3node (Scala) |
|---|---|---|---|
| **Prometheus targets** | `rnode.bootstrap`, `rnode.validator1-3`, `rnode.readonly` | `boot`, `validator1-3` (no `rnode.` prefix, different job split) | Same as system-integration but no cAdvisor |
| **Recording rules** | Loaded via `rule_files` | Same rules + header comment | `rule_files` directive missing — rules not loaded |
| **cAdvisor** | Yes (separate compose) | No | No |
| **Compose** | `compose/monitoring.yml` (external network) | Embedded in `shard.yml` | `shard-monitoring.yml` overlay |

### Needs

1. Align container names in prometheus.yml targets across all three
2. Add `rule_files` to Scala prometheus.yml so recording rules are loaded
3. Decide whether cAdvisor belongs in f1r3node repos or only in system-integration
4. Test monitoring locally: start shard + monitoring, verify Prometheus targets UP, Grafana dashboards load
5. Add monitoring health check to CI (Prometheus targets UP after shard start)

## F1R3_* env var handling cleanup

The current approach of inlining 40+ F1R3_* env vars in compose YAML `environment:` sections and commenting them out in `.env.node` is fragile. Follow-up PR should:

1. Evaluate whether `.env.node` should be the single source for all F1R3_* values (uncommented) with the compose YAML only declaring `${VAR}` without `:-default` fallbacks
2. Or keep the current split but document clearly which vars are safe in `.env.node` vs which must be compose-only
3. Ensure f1r3node-rust `docker/.env` and system-integration `.env.node` stay in sync

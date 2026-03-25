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

## ~~Monitoring stack alignment across repos~~ (Done)

Aligned in config consolidation PRs (#35, f1r3node#447, f1r3node#448):

- All three repos use identical `prometheus.yml` with DNS-based service discovery (no hardcoded targets, no false DOWN targets for light/standalone — fixes [#32](https://github.com/F1R3FLY-io/system-integration/issues/32))
- `rule_files` added to both f1r3node repos (was missing — recording rules were on disk but never loaded)
- `block-transfer.json` dashboard moved to provisioning directory (was unmounted/undiscoverable)
- Network naming aligned: all shard compose files use `name: f1r3fly`, monitoring compose uses external `f1r3fly`
- cAdvisor remains system-integration only (orchestration concern, not per-repo)

### Remaining monitoring follow-ups

- **Rust metric dashboard queries** ([system-integration#22](https://github.com/F1R3FLY-io/system-integration/pull/22)): `f1r3node.json` panels use Scala Kamon metric names (`rchain_*`). Rust node uses `metric_name{source="f1r3fly.*"}`. Dashboard queries need rewriting for Rust — blocked on [f1r3node#405](https://github.com/F1R3FLY-io/f1r3node/pull/405) (Phase 1 observability gauges)
- **CI monitoring validation**: Add a smoke-test job that starts shard + monitoring and verifies Prometheus targets UP, rules loaded, Grafana dashboards present

## F1R3_* env var handling cleanup

The current approach of inlining 40+ F1R3_* env vars in compose YAML `environment:` sections and commenting them out in `.env.node` is fragile. Follow-up PR should:

1. Evaluate whether `.env.node` should be the single source for all F1R3_* values (uncommented) with the compose YAML only declaring `${VAR}` without `:-default` fallbacks
2. Or keep the current split but document clearly which vars are safe in `.env.node` vs which must be compose-only
3. Ensure f1r3node-rust `docker/.env` and system-integration `.env.node` stay in sync

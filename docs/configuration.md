# Configuration

Two layers of config: HOCON node configs in `conf/` and dotenv files in the repo root.

For consensus parameter semantics (FTT, synchrony constraint), see [consensus-configuration.md](consensus-configuration.md).

---

## Node config files (`conf/`)

Nodes use HOCON config files. These are **minimal overrides** — they contain only settings that differ from the node's built-in defaults. Per-role behavior (bootstrap, validator, observer) is controlled entirely via CLI flags in the compose files, not via separate per-role configs.

| File | Used by | Purpose |
|---|---|---|
| `conf/rust.conf` | All Rust shard roles | Overrides on top of Rust node defaults |
| `conf/standalone-dev.conf` | All standalone nodes | Overrides for standalone mode (instant finalization, no peers) |

The integration test framework reads the same files via `infra/config.py:NodeConf.resolve()`, so test behavior matches production conf.

### Per-role CLI flags

Roles within a shard are differentiated by CLI flags injected via the compose files (not by separate confs):

| Flag | Used by |
|---|---|
| `--ceremony-master-mode` | Bootstrap only |
| `--heartbeat-disabled` | Bootstrap and observer |
| `--genesis-validator` | Validators only |
| `--validator-private-key=<hex>` | Validators only (key from `.env.node`) |

See the `command:` block in any compose file for the full per-role flag list.

### Logging configuration (integration tests)

Test nodes pick up structured logging from the mounted config files, not from CLI flags — keeping both Docker and subprocess providers consistent:

| Config file | Used by | Logging settings |
|---|---|---|
| `conf/rust.conf` | All shard test nodes (Docker + subprocess) | `format = "json"`, `sink = "both"` |
| `conf/standalone-dev.conf` | All standalone test nodes (Docker + subprocess) | `format = "json"`, `sink = "both"` |

`sink = "both"` writes to stdout (live inspection via `docker logs -f`) and to `<data-dir>/logs/node.log` (read by the test framework). Log level for test nodes is controlled by the `RUST_LOG` environment variable.

---

## Environment files

Dotenv files in the repo root, loaded by Docker Compose via `--env-file`:

| File | Loaded by | Contents |
|---|---|---|
| `.env.node` | All node compose files | Container hostnames (`BOOTSTRAP_HOST`, `VALIDATOR1_HOST`, etc.) and validator keys (private + public hex) |
| `.env.embers` | `compose/embers.yml` | Embers API config |
| `.env.f1r3sky` | `compose/f1r3sky.yml` | F1R3Sky / AT Protocol config |

`shardctl` automatically picks the right `--env-file` based on the compose file (see `shardctl/compose.py:_get_env_file`). Direct `docker compose` invocation requires you to pass `--env-file` explicitly:

```bash
docker compose --env-file .env.node -f compose/f1r3node-rust.yml up -d
```

### Image selection

`F1R3FLY_NODE_IMAGE` is the single env var for choosing the node Docker image. It applies to both `shardctl up` (production) and `shardctl test` / `pytest` (integration tests). See [../COMPOSE_STRUCTURE.md#image-selection](../COMPOSE_STRUCTURE.md#image-selection) for details.

### `.env.node` validator keys

`.env.node` ships with stable default validator keys (private + public hex per validator). These are the same keys the integration-test framework uses (`integration-tests/test/infra/keys.py`), so contracts deployed against a `shardctl up` shard work identically against an integration-test shard.

For a real production deployment, generate your own keys and override the env vars in `.env.node`. Don't reuse the defaults — they're public.

---

## Adding configuration

For a new node setting:
1. Decide whether it's a HOCON setting (config file) or a CLI flag (compose file). HOCON for static, per-deployment config; CLI flags for per-role behavior.
2. If HOCON: add to `conf/rust.conf` / `conf/standalone-dev.conf` as appropriate.
3. If CLI: add to the `command:` block of the relevant compose files.

For a new env var:
1. Add to the appropriate `.env.<service>` file.
2. Reference it in the compose file via `${VAR}` substitution.
3. Document it here and in `COMPOSE_STRUCTURE.md` if it's image- or topology-related.

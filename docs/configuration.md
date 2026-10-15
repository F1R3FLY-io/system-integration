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
| `conf/shard.conf` | One-node-per-host shards | Overrides for a long-lived shard on separate machines |

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

### Logging configuration

Test nodes pick up structured logging from the mounted config files, not from CLI flags — keeping both Docker and subprocess providers consistent:

| Config file | Used by | Logging settings |
|---|---|---|
| `conf/rust.conf` | All shard test nodes (Docker + subprocess) | `format = "json"`, `sink = "both"`, `filter` = failure-forensics, `file { rotation = "hourly", retention = 2 }` |
| `conf/standalone-dev.conf` | All standalone test nodes (Docker + subprocess) | `format = "json"`, `sink = "both"` |

`sink = "both"` writes to stdout (live inspection via `docker logs -f`) and to `<data-dir>/logs/node.log` (read by the test framework). Log levels come from `logging.filter` in the mounted config, for test nodes and `shardctl` nodes alike. A `RUST_LOG` environment variable, when set, overrides it — see [Controlling log verbosity](troubleshooting.md#controlling-log-verbosity-rust_log).

The active `filter` in `conf/rust.conf` is the failure-forensics one — an INFO baseline plus about ten debug targets — so a default `shardctl up` is verbose by design. An INFO baseline sits commented out above it. For a shard you intend to leave running, set `RUST_LOG` to that baseline rather than editing the file.

Retention is set in two places, and the file sink is the tighter of the two: hourly rotation keeping 2 files (~2 hours), against Docker's 3 × 100 MB of stdout.

---

## Environment files

Dotenv files in the repo root, loaded by Docker Compose via `--env-file`:

| File | Loaded by | Contents |
|---|---|---|
| `.env.node` | All node compose files | Container hostnames (`BOOTSTRAP_HOST`, `VALIDATOR1_HOST`, etc.) and validator keys (private + public hex) |
| `.env.embers` | `compose/embers.yml` | Embers API config |
| `.env.f1r3sky` | `compose/f1r3sky.yml` | F1R3Sky / AT Protocol config |
| `.env.shard` | The three one-node-per-host files | One host's role, identity and keys. **Gitignored**, generated per host, mode 600. Template: `.env.shard.example` |

`shardctl` automatically picks the right `--env-file` based on the compose file (see `shardctl/compose.py:_get_env_file`). Direct `docker compose` invocation requires you to pass `--env-file` explicitly:

```bash
docker compose --env-file .env.node -f compose/f1r3node-rust.yml up -d
```

### Image selection

`F1R3FLY_NODE_IMAGE` is the single env var for choosing the node Docker image. It applies to both `shardctl up` (production) and `shardctl test` / `pytest` (integration tests). See [../COMPOSE_STRUCTURE.md#image-selection](../COMPOSE_STRUCTURE.md#image-selection) for details.

**Pinning an image for a long-running shard.** The compose files default to `f1r3flyindustries/f1r3fly-rust:latest`, and `.env.node` does not set `F1R3FLY_NODE_IMAGE`, so a shard started without it tracks a moving tag. Setting the variable on the command line pins that invocation only: the next `docker compose up`, or a `restart: always` container respawning after a host reboot, silently falls back to the default and can come back on a different binary.

To pin durably, add the tag — ideally a digest — to `.env.node`, which every `f1r3node-*` compose file receives as its `--env-file`:

```bash
# .env.node
F1R3FLY_NODE_IMAGE=f1r3flyindustries/f1r3fly-rust@sha256:<digest>
```

Confirm what is actually resolved before starting, rather than trusting the shell:

```bash
docker compose --env-file .env.node -f compose/f1r3node-rust.yml config | grep image:
```

### `.env.node` validator keys

`.env.node` ships with stable default validator keys (private + public hex per validator). These are the same keys the integration-test framework uses (`integration-tests/test/infra/keys.py`), so contracts deployed against a `shardctl up` shard work identically against an integration-test shard.

For a real production deployment, generate your own keys and override the env vars in `.env.node`. Don't reuse the defaults — they're public.

---

## One node per host

A long-lived shard with each node on its own machine uses a different pair of
config layers: `conf/shard.conf` instead of `conf/rust.conf`, and a per-host
`.env.shard` instead of the shared `.env.node`. The compose files are described
in [../COMPOSE_STRUCTURE.md#one-node-per-host](../COMPOSE_STRUCTURE.md#one-node-per-host).

### `conf/shard.conf`

Same minimal-override principle as `conf/rust.conf`, different assumptions. It
is for a shard that is expected to stay up for weeks and be joined from outside,
so it sets a lower fault-tolerance threshold, a genesis-ceremony signature
requirement, epoch and quarantine lengths, and a file log sink with daily
rotation and retention — none of which matter for a shard that exists for the
length of a test run.

Two things about it are easy to misread:

- **`network-id` is deliberately absent.** It is passed as `--network-id` from
  `.env.shard` instead, because it is the one value that must differ between
  shards and a stale value in a committed file is how a new node silently
  attaches to an old chain. A node accepts messages only on its own network id,
  and a mismatch completes the TCP and TLS handshake before discarding
  everything — which looks exactly like a broken network.
- **`allow-private-addresses = true` is required**, not a convenience. Nodes
  advertise their cloud-private addresses to each other; without this they
  discard those addresses and never peer.

### `.env.shard`

One per host, generated rather than committed, and holding only what that host's
role needs — a validator host carries its own validator key and no others, so
losing one host does not expose the rest of the shard.

| Variable | boot | validator | read-only |
|---|---|---|---|
| `F1R3FLY_NODE_IMAGE`, `SHARD_NETWORK_ID`, `GENESIS_DIR`, `NODE_HOST` | yes | yes | yes |
| `CEREMONY_PRIVATE_KEY` | yes | — | — |
| `BOOTSTRAP_NODE_ID`, `BOOTSTRAP_HOST` | — | yes | yes |
| `VALIDATOR_PUBLIC_KEY`, `VALIDATOR_PRIVATE_KEY` | — | yes | — |

**Every variable is required and none has a fallback.** The files use
`${VAR:?message}`, so a missing value stops compose with a message naming it
rather than selecting a default — a default `GENESIS_DIR` would quietly point a
new shard at the test shard's genesis, whose `bonds.txt` names committed keys.
`.env.shard.example` is the annotated template; `.env.shard` itself is
gitignored.

### Generating keys and genesis

`scripts/shard-setup.sh` generates the whole set: five keypairs, their derived
vault addresses, the `bonds.txt`/`wallets.txt` genesis pair, and a per-role
`.env.shard` at mode 600. Everything lands in gitignored directories
(`shard-keys/<shard>/`, `genesis-shard/<shard>/`).

Nothing it produces is committed, and that is the point — the repo holds no
wallets, bonds or keys for a real shard. Re-running it reuses existing keys and
refuses to replace an existing genesis pair without `--force`.

It prints a SHA-256 of the genesis pair because **every host must hold a
byte-identical `bonds.txt` and `wallets.txt`**. Nodes that disagree produce a
ceremony that either fails or yields a chain the others reject. Git does not
protect you here: a host's checkout can sit weeks behind without anyone
noticing. Compare the digest on each host before starting the ceremony:

```bash
cat genesis-shard/<shard>/bonds.txt genesis-shard/<shard>/wallets.txt | sha256sum
```

One of the five generated keys is an operator key that is not a validator and is
funded at genesis — the vault to fund joiners and faucets from, so that no
validator's signing key has to be handed out to do it.

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

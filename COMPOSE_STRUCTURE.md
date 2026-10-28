# Docker Compose Structure

Canonical reference for the production compose files in `compose/`. Each file is a single, mutually-exclusive topology (or a service stack). All are managed by `shardctl up`; integration tests bypass these and generate compose YAML dynamically (see [integration-tests/test/docs/ARCHITECTURE.md](integration-tests/test/docs/ARCHITECTURE.md)).

---

## File inventory

### F1R3node (blockchain)

| File | Topology | Validators | Image default |
|---|---|---|---|
| `compose/f1r3node-rust.yml` | Shard | bootstrap + 3 + readonly | `f1r3flyindustries/f1r3fly-rust:latest` |
| `compose/f1r3node-rust-shard-light.yml` | Light shard | bootstrap + 2 (lower memory) | rust:latest |
| `compose/f1r3node-rust-standalone.yml` | Standalone | 1 (single-node, no peers) | rust:latest |
| `compose/f1r3node-rust-observer.yml` | Read-only observer | (joins existing shard) | rust:latest |
| `compose/f1r3node-rust-validator4.yml` | 4th validator | (joins existing shard) | rust:latest |

### One node per host

For a long-lived shard spread across separate machines, rather than one host
running every node. One service per file, selected by the host's role:

| File | Topology | Runs | Image default |
|---|---|---|---|
| `compose/f1r3node-rust-boot.yml` | Ceremony master | 1 (takes no `--bootstrap`) | **none, required** |
| `compose/f1r3node-rust-validator.yml` | One bonded validator | 1 (joins via the boot host) | **none, required** |
| `compose/f1r3node-rust-readonly.yml` | One read-only node | 1 (joins via the boot host) | **none, required** |

**Three files rather than one with three services.** Compose interpolates every
service in a file regardless of which one is selected, so a combined file would
demand a validator key from the host running the read-only node. Splitting them
means each host holds only its own role's secrets.

These files pair with `conf/shard.conf` and a per-host `.env.shard`, not with
`conf/rust.conf` and `.env.node` — see
[docs/configuration.md](docs/configuration.md).

### Service stacks

| File | Purpose | Notes |
|---|---|---|
| `compose/embers.yml` | Embers API + frontend | Requires the `f1r3fly` network (external) |
| `compose/f1r3sky.yml` | AT Protocol services (postgres, redis, bsky, pds, bsync, ozone, f1r3sky) | Requires the `f1r3fly` network (external) |
| `compose/monitoring.yml` | Prometheus + Grafana + cAdvisor | See [Monitoring stack](#monitoring-stack) |

---

## Image selection

Every compose file's `image:` line uses the same env var:

```yaml
image: ${F1R3FLY_NODE_IMAGE:-<file's-own-default>}
```

When `F1R3FLY_NODE_IMAGE` is unset, each file falls back to its own default. To override:

```bash
F1R3FLY_NODE_IMAGE=f1r3flyindustries/f1r3fly-rust:dev poetry run shardctl up f1r3node-rust
```

`F1R3FLY_NODE_IMAGE` is the **same env var** the integration-test framework reads (via `infra/config.py:resolve_node_image`). One variable, one mental model across `shardctl up` and `shardctl test`.

**The one-node-per-host files are the exception: they have no default.** They use
`${F1R3FLY_NODE_IMAGE:?...}` and refuse to start without it, because the nodes
are on separate machines and a silent fallback would let hosts drift onto
different builds. Once that happens the shard is no longer homogeneous and any
difference between nodes stops being interpretable as a software signal. Pin a
digest rather than a moving tag for the same reason — `:dev` moves.

---

## Project naming and volumes

Each compose file declares an explicit top-level `name:` so Docker Compose uses a deterministic project name (independent of the parent directory):

| File | `name:` | Volume prefix |
|---|---|---|
| `f1r3node-rust.yml` | `f1r3fly-rust` | `f1r3fly-rust_*-data` |
| `f1r3node-rust-shard-light.yml` | `f1r3fly-rust-light` | `f1r3fly-rust-light_*-data` |
| `f1r3node-rust-standalone.yml` | `f1r3fly-rust-standalone` | `f1r3fly-rust-standalone_*-data` |
| `f1r3node-rust-observer.yml` | `f1r3fly-rust-observer` | `f1r3fly-rust-observer_*-data` |
| `f1r3node-rust-validator4.yml` | `f1r3fly-rust-validator4` | `f1r3fly-rust-validator4_*-data` |
| `f1r3node-rust-boot.yml` | `f1r3fly-rust-boot` | `f1r3fly-rust-boot_*-data` |
| `f1r3node-rust-validator.yml` | `f1r3fly-rust-validator` | `f1r3fly-rust-validator_*-data` |
| `f1r3node-rust-readonly.yml` | `f1r3fly-rust-readonly` | `f1r3fly-rust-readonly_*-data` |

Cleanup convention: `shardctl reset` (and `shardctl reset --force`) prefix-scan for `f1r3fly-*` volumes and the `f1r3fly` network. Disjoint from integration-test prefixes (`f1r3fly-test-*` networks, `test-*` volumes).

---

## Network

A single shared bridge network named `f1r3fly`, declared explicitly in every node compose file:

```yaml
networks:
  f1r3fly:
    name: f1r3fly
    driver: bridge
```

Service stacks (embers, f1r3sky) attach to the same network as `external: true`, allowing them to reach the running node by container name (`rnode.bootstrap`, `rnode.validator1`, etc.).

---

## Container names

Container names are set by env-var substitution from `.env.node` in the repo root. Defaults are stable across files:

| Env var | Default | Set by |
|---|---|---|
| `BOOTSTRAP_HOST` | `rnode.bootstrap` | shard files |
| `VALIDATOR1_HOST` | `rnode.validator1` | shard files |
| `VALIDATOR2_HOST` | `rnode.validator2` | shard files |
| `VALIDATOR3_HOST` | `rnode.validator3` | shard files |
| `VALIDATOR4_HOST` | `rnode.validator4` | validator4 file |
| `READONLY_HOST` | `rnode.readonly` | shard + observer files |
| `STANDALONE_HOST` | `rnode.standalone` | standalone files |

Override the env vars in `.env.node` to run multiple parallel topologies (rare; integration tests achieve this differently — see [integration-tests/test/docs/ARCHITECTURE.md](integration-tests/test/docs/ARCHITECTURE.md)).

**The one-node-per-host files do not follow this.** They hardcode the container
name to their role — `rnode.bootstrap`, `rnode.validator`, `rnode.readonly` —
because one host runs one node and the name needs to be predictable for `docker
logs` and the monitor, not configurable.

The consequence is worth stating plainly: **every validator host runs a
container called `rnode.validator`**, while the node *advertises* a per-host name
like `rnode.validator3` (that comes from `NODE_HOST`, and is what appears in a
peer list). So `docker logs rnode.validator3` fails with "No such container" on
the very host running validator3.

`BOOTSTRAP_HOST` also means something different in these files. In the
single-host files above it names a **container**; in the one-node-per-host files
it is the **address other instances dial** for the bootstrap node, which is why
it appears in the validator and read-only files but not in the boot file.

---

## Port map

Each node uses six internal ports (40400-40405). The host port mapping varies by topology to allow some scenarios to coexist:

| Internal | Role | Bootstrap | Validator1 | Validator2 | Validator3 | Readonly |
|---|---|---|---|---|---|---|
| 40400 | Protocol | 40400 | 40410 | 40420 | 40430 | 40450 |
| 40401 | gRPC ext | 40401 | 40411 | 40421 | 40431 | 40451 |
| 40402 | gRPC int | 40402 | 40412 | 40422 | 40432 | 40452 |
| 40403 | HTTP | 40403 | 40413 | 40423 | 40433 | 40453 |
| 40404 | Discovery | 40404 | 40414 | 40424 | 40434 | 40454 |
| 40405 | Admin | 40405 | 40415 | 40425 | 40435 | 40455 |

Standalone uses 40400-40405 directly. Validator4 uses 40440-40445.

**Which port to dial.** `rust-client` and the integration harness use the
internal gRPC port (40402 and its per-node offsets) for deploys and proposes;
the external gRPC port (40401) exists for clients outside the compose network.
HTTP (40403) serves the REST API, and exploratory queries — balances, bonds,
PoS state — are served **only** by `rnode.readonly` on 40453.

### Ports on a one-node-per-host shard

No offsets: one node per machine, so every node publishes the native ports. Two
of the six are deliberately bound to loopback instead:

| Internal | Role | Published as |
|---|---|---|
| 40400 | Protocol | `40400` |
| 40401 | gRPC ext | `40401` |
| 40402 | gRPC int | `127.0.0.1:40402` — **loopback only** |
| 40403 | HTTP | `40403` |
| 40404 | Discovery | `40404` |
| 40405 | Admin | `127.0.0.1:40405` — **loopback only** |

The privileged gRPC port grants propose and validator operations, and the admin
HTTP port exposes block-store internals; neither belongs on a public interface.
Clients therefore deploy to **40401**, not to the 40402 the single-host files
and `rust-client`'s own default steer you toward. A client configured with the
internal port will simply find nothing listening.

---

## Logging

Every node compose file sets the same two things:

- `environment: - RUST_LOG` — passed through only when set in the shell or an
  env file, so `logging.filter` in `conf/rust.conf` applies by default. That
  shipped filter is the failure-forensics one, not an INFO baseline; see
  [docs/configuration.md](docs/configuration.md#logging-configuration) before leaving a shard
  running.
- `logging: driver: json-file` with `max-size: 100m`, `max-file: "3"` — 300 MB
  of stdout per container. The mounted conf selects `sink = "stdout"`, so this
  cap is the only log retention for Compose deployments. See
  [docs/configuration.md](docs/configuration.md#logging-configuration) for the
  sink each deployment uses.

---

## Monitoring stack

`compose/monitoring.yml` brings up Prometheus, Grafana, and cAdvisor on the same `f1r3fly` network. Start it after a node compose file is already up.

```bash
poetry run shardctl up f1r3node-rust    # Start a shard first
poetry run shardctl up monitoring       # Then attach monitoring
poetry run shardctl down monitoring     # Stop monitoring (shard stays running)
```

| Component | URL | Notes |
|---|---|---|
| Prometheus | http://localhost:9090 | Metric collection, recording rules, target health |
| Grafana | http://localhost:3000 | Dashboards (admin/admin) |
| cAdvisor | http://localhost:8080 | Container CPU / memory / IO |

Prometheus uses DNS-based service discovery on the Docker network — only nodes that are actually running get scraped (no false DOWN targets when running light shard or standalone).

**Auto-provisioned dashboards:**
- *F1R3FLY Node* — block finalization, validator status, consensus metrics
- *Block Transfer* — block download/validation timing, transport metrics

Config files: `monitoring/prometheus.yml`, `monitoring/prometheus-rules.yml`, `monitoring/grafana/provisioning/`.

**For a long-lived shard, `scripts/shard-monitor.sh` is the alerting half.**
Prometheus and Grafana answer "what are the numbers"; the monitor answers "is
anyone being told". It is a cron script, one per host, that checks container and
host conditions locally and — with `--chain`, on one host only — polls every
node's `/api/status` for finalization progress, lag and network id. It posts to
a webhook with per-condition exponential backoff and a `RECOVERED` notice, plus
a once-daily digest under `--nightly`. `--dry-run` prints instead of posting.

A container healthcheck is not a substitute: it is `curl -f /api/status`, which
reports `healthy` on a node whose `lastFinalizedBlockNumber` is `-1`.

---

## Usage

```bash
# Start a single topology (most common)
poetry run shardctl up f1r3node-rust               # Shard (3 validators + observer)
poetry run shardctl up f1r3node-rust-shard-light   # Light shard (2 validators)
poetry run shardctl up f1r3node-rust-standalone    # Single node

# Service stacks (require a running shard for the f1r3fly network)
poetry run shardctl up embers
poetry run shardctl up f1r3sky
poetry run shardctl up monitoring

# Multiple at once (separate compose projects, independent lifecycles)
poetry run shardctl up f1r3node-rust embers monitoring

# All services in startup_order from services.yml
poetry run shardctl up

# Native services (run foreground via shardctl using services.yml run_command)
poetry run shardctl up f1r3drive

# Stop
poetry run shardctl down                # Stop all
poetry run shardctl down f1r3node-rust  # Stop one
poetry run shardctl reset -y            # Stop all + wipe blockchain data volumes
```

### Starting a one-node-per-host shard

Not driven by `shardctl`, which assumes one host runs the topology. Each machine
runs compose directly, from the `compose/` directory, with its own `.env.shard`:

```bash
docker compose --env-file ../.env.shard -f f1r3node-rust-boot.yml      up -d   # boot host
docker compose --env-file ../.env.shard -f f1r3node-rust-validator.yml up -d   # each validator host
docker compose --env-file ../.env.shard -f f1r3node-rust-readonly.yml  up -d   # read-only host
```

**Order matters once, at genesis.** The boot node generates its own TLS identity
on first start, so its node id does not exist until it has run. Start it alone,
read the id from its `/api/status` `address` field, put that in the other hosts'
`BOOTSTRAP_NODE_ID`, then start them. Genesis completes once enough validators
have approved (`casper.genesis-ceremony.required-signatures` in
`conf/shard.conf`).

Keys, bonds and wallets are generated, never committed —
`scripts/shard-setup.sh` writes them to gitignored directories and prints a
SHA-256 of the genesis pair, because **all hosts must hold a byte-identical
`bonds.txt`/`wallets.txt`** and git cannot guarantee that five checkouts agree.
Compare the digest on every host before starting the ceremony. See
[docs/configuration.md](docs/configuration.md#one-node-per-host).

Restart discipline: stop every node, then start them. A rolling restart
re-propagates dead peer entries from whichever node stayed up.

---

For the full CLI reference, see [docs/cli-reference.md](docs/cli-reference.md).

For node configuration (HOCON files, env files), see [docs/configuration.md](docs/configuration.md).

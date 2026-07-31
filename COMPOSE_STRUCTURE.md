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

For the full CLI reference, see [docs/cli-reference.md](docs/cli-reference.md).

For node configuration (HOCON files, env files), see [docs/configuration.md](docs/configuration.md).

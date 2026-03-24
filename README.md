# F1R3FLY System Integration

Orchestration tooling for the F1R3FLY blockchain ecosystem. Manages multiple service repositories with Docker Compose and the `shardctl` CLI.

## Prerequisites

- **Python 3.10+** ([pyenv setup](docs/prerequisites.md#python-310-pyenv) if needed)
- **Poetry** — `pipx install poetry` or `pip install --user poetry`
- **Docker & Docker Compose**

```bash
# Install shardctl
poetry install
```

For service-specific build tools (Rust, Node.js, SBT), see [docs/prerequisites.md](docs/prerequisites.md). Note that f1r3node (Scala) and f1r3node-rust are the same repository on different branches — see `services.yml` for branch mappings.

## Quick Start

### 1. Start a Shard

```bash
poetry run shardctl up f1r3node-rust
poetry run shardctl wait
```

Genesis takes ~2-3 minutes. `shardctl wait` blocks until all nodes report Running.

### 2. Verify

```bash
poetry run shardctl status
```

| Service | URL |
|---------|-----|
| F1R3node API (validator1) | http://localhost:40413 |
| F1R3node Read-only | http://localhost:40453 |

### 3. Stop

```bash
poetry run shardctl down          # Stop containers
poetry run shardctl reset -y      # Stop and wipe data volumes
```

> **No Poetry?** Shards can be run directly with Docker Compose:
> ```bash
> docker compose --env-file .env.node -f compose/f1r3node-rust.yml up -d
> docker compose --env-file .env.node -f compose/f1r3node-rust.yml logs -f | grep "Making a transition to Running state"
> docker compose --env-file .env.node -f compose/f1r3node-rust.yml down           # stop
> docker compose --env-file .env.node -f compose/f1r3node-rust.yml down -v        # stop and wipe data
> ```

## Node Topologies

| Command | Description |
|---------|-------------|
| `shardctl up f1r3node-rust` | Rust shard (boot + 3 validators + observer) |
| `shardctl up f1r3node-rust-standalone` | Rust standalone (single node) |
| `shardctl up f1r3node` | Scala shard |
| `shardctl up f1r3node-standalone` | Scala standalone |
| `shardctl up f1r3node-shard-light` | Scala light shard (boot + 2 validators, ~7.5 GB RAM) |

See [COMPOSE_STRUCTURE.md](COMPOSE_STRUCTURE.md) for details on each compose file.

All commands require `poetry run` prefix unless you activate the shell with `poetry shell`.

### Custom Docker Images

Override the default image with env vars:

```bash
F1R3FLY_RUST_IMAGE=f1r3flyindustries/f1r3fly-rust-node:dev poetry run shardctl up f1r3node-rust
F1R3FLY_SCALA_IMAGE=f1r3flyindustries/f1r3fly-scala-node:v1.2.3 poetry run shardctl up f1r3node
```

| Variable | Default | Used by |
|---|---|---|
| `F1R3FLY_RUST_IMAGE` | `f1r3flyindustries/f1r3fly-rust-node:latest` | All Rust node compose files |
| `F1R3FLY_SCALA_IMAGE` | `f1r3flyindustries/f1r3fly-scala-node:latest` | All Scala node compose files |

## Full Setup (All Services)

### 1. Clone Service Repositories

```bash
poetry run shardctl clone
```

Clones all enabled services from `services.yml`: f1r3node, f1r3node-rust, rust-client, f1r3sky-backend, embers.

### 2. Build Docker Images

```bash
poetry run shardctl build-service --docker-only

# Or build a single service
poetry run shardctl build-service f1r3node --docker-only

# Sync branches from services.yml before building
poetry run shardctl build-service --docker-only --sync
```

### 3. Start Everything

```bash
poetry run shardctl up
poetry run shardctl wait
```

Which services start is defined by `services.yml`. Default endpoints when all services are running:

| Service | URL |
|---------|-----|
| F1R3node API (validator1) | http://localhost:40413 |
| F1R3node Read-only | http://localhost:40453 |
| Embers API | http://localhost:8080 |
| F1R3Sky PDS | http://localhost:2583 |
| Grafana | http://localhost:3000 |
| Prometheus | http://localhost:9090 |

## CLI Reference

### Service Lifecycle

```
shardctl up [SERVICES...]         Start services (detached)
  --build, -b                     Build images first
  --foreground, -f                Run in foreground
  --profile, -p TEXT              Compose profile (dev/prod)
shardctl down [SERVICES...]       Stop and remove containers
  --volumes, -v                   Remove volumes
shardctl restart [SERVICES...]    Restart services
shardctl reset [-y]               Stop all nodes and delete data volumes
```

### Observability

```
shardctl status [SERVICES...]     Show container status
shardctl ps [SERVICES...]         List running containers
shardctl logs [SERVICES...]       View logs
  --follow, -f                    Follow output
  --tail, -n INTEGER              Number of lines
shardctl wait                     Wait for nodes to reach Running state
  --timeout, -t INTEGER           Timeout in seconds (default: 300)
```

### Images and Builds

```
shardctl pull [SERVICES...]       Pull service images
shardctl build [SERVICES...]      Build services (from services.yml)
  --no-cache                      Build without cache
shardctl build-service [SERVICE]  Build a service's Docker image
  --docker-only                   Skip native build, Docker only
  --sync                          Sync branch from services.yml first
  --list                          List available services
```

### Repository Setup

```
shardctl clone [SERVICES...]      Clone service repos from services.yml
shardctl setup [--force]          Clone all service repositories
shardctl clean                    Delete cloned service repositories
```

### Container Interaction

```
shardctl exec SERVICE COMMAND...  Execute command in container
  --no-tty, -T                   Disable TTY
shardctl shell SERVICE            Open interactive shell
  --shell, -s TEXT                Shell to use (default: /bin/bash)
shardctl compose ARGS...          Run custom docker-compose command
```

### Testing

```
shardctl test [SUITE]             Run integration tests
  --rust / --scala                Node image to test against
  --skip-setup                    Use already-running shard
  --verbose, -v                   Verbose pytest output
shardctl test-report              Show test results from last run
  --failures                      Show failed tests only
shardctl test-reset               Clean up test containers and volumes
```

## Configuration

### Node Config Files

Rust and Scala nodes share 2 config files in `conf/`. Per-role behavior is controlled entirely via CLI flags in compose commands.

| Config File | Used By | Purpose |
|-------------|---------|---------|
| `default.conf` | All shard roles (both Rust and Scala) | Shared defaults, GC enabled |
| `standalone-dev.conf` | All standalone nodes (both Rust and Scala) | Standalone mode |

Per-role CLI flags used in compose files:

| Flag | Used by |
|------|---------|
| `--ceremony-master-mode` | Bootstrap only |
| `--heartbeat-disabled` | Bootstrap and observer |
| `--disable-mergeable-channel-gc` | All Scala services ([f1r3node#441](https://github.com/F1R3FLY-io/f1r3node/issues/441)) |

### Environment Files

| File | Used by |
|------|---------|
| `.env.node` | All node compose files (credentials, keys, F1R3_* tuning) |
| `.env.embers` | Embers API compose |
| `.env.f1r3sky` | F1R3Sky compose |

## Monitoring

```bash
poetry run shardctl up f1r3node-rust    # Start shard first
poetry run shardctl up monitoring       # Then monitoring stack
```

| Component | URL | Description |
|---|---|---|
| Prometheus | http://localhost:9090 | Metrics collection (targets, recording rules) |
| Grafana | http://localhost:3000 | Dashboards (admin/admin) |
| cAdvisor | http://localhost:8080 | Container resource metrics |

Config: `monitoring/prometheus.yml`, `monitoring/prometheus-rules.yml`, `monitoring/grafana/provisioning/`.

## Integration Tests

Tests verify node behavior through HTTP and gRPC APIs. Full docs: [integration-tests/README.md](integration-tests/README.md).

```bash
poetry install --with integration
poetry run shardctl test --rust           # Run all tests against Rust node
poetry run shardctl test --scala          # Against Scala node
poetry run shardctl test test_wallets     # Single suite
```

## Troubleshooting

| Symptom | Quick Fix |
|---------|-----------|
| "casper instance was not available yet" | `shardctl wait` — blockchain needs 2-3 min |
| Nodes stuck, won't complete genesis | `shardctl reset -y` then `shardctl up` |
| Docker "outside of rootfs" on macOS | Switch Docker to gRPC FUSE ([details](docs/troubleshooting.md#docker-outside-of-rootfs-error)) |
| Build fails with "better-sqlite3" | Docker build: `shardctl build-service f1r3sky-backend-bsky` |

For all troubleshooting topics, see [docs/troubleshooting.md](docs/troubleshooting.md).

## Repository Structure

```
.
├── compose/                        # Docker Compose files (one per topology/service)
├── conf/                           # Node HOCON config files
├── certs/                          # TLS certificates for nodes
├── genesis/                        # Genesis wallets and bonds
├── monitoring/                     # Prometheus + Grafana config
├── shardctl/                       # CLI tool package
├── .github/workflows/              # CI smoke test pipeline
├── integration-tests/              # Integration test suite
├── services/                       # Service repositories (git-ignored)
├── docs/                           # Prerequisites, troubleshooting, development guide
├── .env.node                       # Node environment variables
├── services.yml                    # Service repository URLs and branches
└── pyproject.toml                  # Python package config
```

## Contributing

1. Only commit changes to integration tooling (compose files, shardctl code)
2. Never commit service code (it belongs in service repos)
3. CI runs automatically on PRs (compose validation, topology health, integration tests)
4. Update documentation for new features

For development workflow and best practices, see [docs/development.md](docs/development.md).

## License

MIT License - See LICENSE file for details

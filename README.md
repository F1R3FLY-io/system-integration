# System Integration Repository

A microservices integration repository for managing multiple services with docker-compose and the `shardctl` CLI tool.

## Overview

### Key Features

- **Nested Git Repositories**: Service repos are cloned into `services/` and fully git-ignored by the parent repo
- **Independent Development**: Work in each service directory normally with full git functionality
- **Docker Compose Orchestration**: Each service has its own compose file in `compose/`
- **Convenient CLI**: `shardctl` wraps docker-compose with user-friendly commands
- **Profile Support**: Switch between dev and prod configurations easily

## Repository Structure

```
.
├── compose/                        # Docker Compose files (one per service)
│   ├── f1r3node.yml                #   Scala shard (default)
│   ├── f1r3node-standalone.yml     #   Scala standalone
│   ├── f1r3node-observer.yml       #   Scala observer
│   ├── f1r3node-validator4.yml     #   Scala validator4
│   ├── f1r3node-rust.yml           #   Rust shard
│   ├── f1r3node-rust-standalone.yml #  Rust standalone
│   ├── f1r3node-rust-observer.yml  #   Rust observer
│   ├── f1r3node-rust-validator4.yml #  Rust validator4
│   ├── embers.yml                  #   Embers API + frontend
│   ├── f1r3sky.yml                 #   F1R3Sky AT Protocol services
│   └── monitoring.yml              #   Prometheus + Grafana
├── conf/                           # Node configuration files (shared by Rust and Scala)
├── certs/                          # TLS certificates for nodes
├── genesis/                        # Genesis wallets and bonds
├── integration-tests/              # Integration test suite (see integration-tests/README.md)
├── shardctl/                       # CLI tool package
├── services/                       # Service repositories (git-ignored)
├── docs/                           # Additional documentation
│   ├── prerequisites.md            #   Service build dependencies
│   ├── troubleshooting.md          #   Troubleshooting guide
│   ├── development.md              #   Development workflow and advanced usage
│   └── TODO.md                     #   Config notes and known issues
├── .env.node                       # Node environment variables
├── .env.embers                     # Embers environment variables
├── .env.f1r3sky                    # F1R3SKY environment variables
├── services.yml                    # Service repository URLs (optional)
├── pyproject.toml                  # Python package and pytest configuration
└── README.md                       # This file
```

## Quick Start

### Docker Only (No Poetry Needed)

Requirements: **Docker & Docker Compose**, **Git**

Pull pre-built images and start a Rust shard:

```bash
docker compose --env-file .env.node -f compose/f1r3node-rust.yml pull
docker compose --env-file .env.node -f compose/f1r3node-rust.yml up -d
```

Wait for genesis (~2-3 minutes). All validators must transition to Running state:
```bash
docker compose --env-file .env.node -f compose/f1r3node-rust.yml logs -f | grep "Making a transition to Running state"
```

Once all validators report Running, press `Ctrl+C`. The network is ready.

**Stop:**
```bash
docker compose --env-file .env.node -f compose/f1r3node-rust.yml down
```

**Stop and wipe all data (fresh restart):**
```bash
docker compose --env-file .env.node -f compose/f1r3node-rust.yml down -v
```

### With shardctl (Recommended)

Additional requirements: **Python 3.10** ([pyenv setup](docs/prerequisites.md#python-310-pyenv) if needed), **Poetry**

```bash
# Install Poetry (if needed)
pipx install poetry    # or: pip install --user poetry

# Install shardctl
poetry install

# Start, wait, watch, stop
poetry run shardctl up f1r3node-rust
poetry run shardctl wait
poetry run shardctl logs -f
poetry run shardctl down
```

### Complete Setup from Scratch

#### 1. Install shardctl

```bash
poetry install
poetry run shardctl --help
```

For service-specific build tools (Rust, Node.js, SBT, etc.), see [docs/prerequisites.md](docs/prerequisites.md).

#### 2. Clone Service Repositories

```bash
poetry run shardctl clone
```

This clones all enabled services from `services.yml`: f1r3node, f1r3node-rust, rust-client, f1r3sky-backend, embers.

#### 3. Build Docker Images

```bash
# Build Docker images only (Dockerfiles build inside the image)
poetry run shardctl build-service --docker-only

# Or build a single service
poetry run shardctl build-service f1r3node --docker-only

# Sync branches from services.yml before building
poetry run shardctl build-service --docker-only --sync
```

#### 4. Start the Stack

```bash
# Start all services
poetry run shardctl up

# Wait for blockchain to initialize (2-3 minutes)
poetry run shardctl wait
```

#### 5. Verify

```bash
poetry run shardctl status
```

Services are accessible at:

| Service | URL |
|---------|-----|
| F1R3node API (validator1) | http://localhost:40413 |
| F1R3node Read-only | http://localhost:40453 |
| Embers API | http://localhost:8080 |
| F1R3Sky PDS | http://localhost:2583 |
| Grafana | http://localhost:3000 |
| Prometheus | http://localhost:9090 |

## Node Operations

| Command | Description |
|---------|-------------|
| `shardctl up f1r3node` | Start Scala shard (default) |
| `shardctl up f1r3node-standalone` | Start Scala standalone node |
| `shardctl up f1r3node-rust` | Start Rust multi-node shard |
| `shardctl up f1r3node-rust-standalone` | Start Rust standalone node |
| `shardctl down f1r3node` | Stop and remove node containers |
| `shardctl logs f1r3node -f` | Follow node logs |
| `shardctl status` | Show container status |
| `shardctl wait` | Wait for all nodes to be ready |
| `shardctl wait --timeout 120` | Wait with custom timeout |
| `shardctl pull f1r3node` | Pull node images |
| `shardctl reset -y` | Stop nodes and delete data volumes |

All commands require `poetry run` prefix unless you activate the shell with `poetry shell`.

See [COMPOSE_STRUCTURE.md](COMPOSE_STRUCTURE.md) for details on each compose file.

### Configuration

Both Rust and Scala nodes share 3 config files in `conf/`:

| Config File | Used By | Key Difference |
|-------------|---------|----------------|
| `default.conf` | Validators, Observer | `ceremony-master-mode = false` |
| `bootstrap.conf` | Bootstrap | `include "default.conf"` + `ceremony-master-mode = true` |
| `standalone-dev.conf` | Standalone | `standalone = true`, `fault-tolerance-threshold = 0.0` |

Per-role behavior is controlled via CLI flags in compose commands:

| Role | Config File | CLI Overrides |
|------|------------|--------------|
| Bootstrap | `bootstrap.conf` | `--required-signatures 2` |
| Validators 1-3 | `default.conf` | `--genesis-validator` |
| Observer | `default.conf` | *(none)* |
| Validator4 | `default.conf` | *(none - joins via bonding)* |
| Standalone | `standalone-dev.conf` | *(none)* |

#### Custom Docker Images

Override the default image with env vars:

```bash
F1R3FLY_RUST_IMAGE=f1r3flyindustries/f1r3fly-rust-node:dev poetry run shardctl up f1r3node-rust
F1R3FLY_SCALA_IMAGE=f1r3flyindustries/f1r3fly-scala-node:v1.2.3 poetry run shardctl up f1r3node
```

| Variable | Default | Used by |
|---|---|---|
| `F1R3FLY_RUST_IMAGE` | `f1r3flyindustries/f1r3fly-rust-node:latest` | All Rust node compose files |
| `F1R3FLY_SCALA_IMAGE` | `f1r3flyindustries/f1r3fly-scala-node:latest` | All Scala node compose files |

### Light Shard (Development)

Minimal network: bootstrap + 2 validators (~7.5 GB RAM vs 10+ GB for full shard). Suitable for 16 GB machines.

```bash
poetry run shardctl up f1r3node-shard-light
poetry run shardctl wait
```

| Node | Ports |
|------|-------|
| Bootstrap | 40400-40405 |
| Validator 1 | 40410-40415 |
| Validator 2 | 40420-40425 |

## CLI Commands

### Service Management

```bash
shardctl up [SERVICES...] [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)
  --foreground, -f      Run in foreground
  --build, -b           Build images first
  --scala               Use Scala node implementation
  --rust                Use Rust node implementation
  --standalone          Standalone topology (single node)
  --shard               Shard topology (multi-node network)

shardctl down [SERVICES...] [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)
  --volumes, -v         Remove volumes

shardctl restart [SERVICES...]

shardctl status [SERVICES...]

shardctl ps [SERVICES...]

shardctl logs [SERVICES...] [OPTIONS]
  --follow, -f          Follow output
  --tail, -n INTEGER    Number of lines

shardctl wait [OPTIONS]
  --timeout, -t INTEGER  Timeout in seconds (default: 300)

shardctl reset [OPTIONS]
  --yes, -y             Skip confirmation prompt
```

### Build and Images

```bash
shardctl build [SERVICES...] [OPTIONS]
  --no-cache           Build without cache

shardctl pull [SERVICES...]
```

### Container Interaction

```bash
shardctl exec SERVICE COMMAND...
  --no-tty, -T         Disable TTY

shardctl shell SERVICE
  --shell, -s TEXT     Shell to use (default: /bin/bash)
```

### Setup

```bash
shardctl setup --create-config    # Create example services.yml
shardctl setup [--force]          # Clone all service repositories
shardctl compose ARGS...          # Run custom docker-compose command
```

## Monitoring (Prometheus + Grafana + cAdvisor)

| Component | Description | URL |
|---|---|---|
| **cAdvisor** | Container CPU, memory, and I/O metrics | http://localhost:8080 |
| **Prometheus** | Scrapes node metrics and cAdvisor every 15s | http://localhost:9090 |
| **Grafana** | Auto-provisioned dashboards | http://localhost:3000 |

### Start Monitoring

```bash
# Start a shard first, then monitoring
poetry run shardctl up f1r3node-rust
poetry run shardctl up monitoring

# Or start everything at once
poetry run shardctl up
```

### Verify

- **Prometheus targets:** http://localhost:9090/targets — nodes and cAdvisor should show `UP`
- **Recording rules:** http://localhost:9090/rules — should show `block_transfer_metrics` group
- **Grafana:** http://localhost:3000 — dashboards auto-provisioned (default login: admin/admin)

### Configuration Files

| File | Purpose |
|---|---|
| `monitoring/prometheus.yml` | Scrape config (node targets + cAdvisor) |
| `monitoring/prometheus-rules.yml` | Recording rules for aggregated metrics |
| `monitoring/grafana/provisioning/` | Grafana datasource and dashboard provisioning |
| `compose/monitoring.yml` | Docker Compose for cAdvisor + Prometheus + Grafana |

## Integration Tests

Integration tests verify F1R3FLY node behavior through HTTP and gRPC APIs against Docker-managed node clusters. The test suite covers consensus, wallets, deploys, finalization, heartbeat, state trimming, bonding, slashing, and more.

For full documentation, see [integration-tests/README.md](integration-tests/README.md).

```bash
poetry install --with integration
poetry run pytest integration-tests/test/ -v --tb=short --log-cli-level=WARNING
```

## Troubleshooting

| Symptom | Quick Fix |
|---------|-----------|
| "casper instance was not available yet" | Run `poetry run shardctl wait` — blockchain needs 2-3 min to initialize |
| Nodes stuck, won't complete genesis | `poetry run shardctl reset -y` then `poetry run shardctl up` |
| Docker "outside of rootfs" on macOS | Switch Docker to gRPC FUSE ([details](docs/troubleshooting.md#docker-outside-of-rootfs-error)) |
| Build fails with "better-sqlite3" | Use Docker build: `shardctl build-service f1r3sky-backend-bsky` |
| `pnpm: not found` | [Install pnpm](docs/troubleshooting.md#missing-pnpm-or-node-gyp) |
| Rust linker errors | `rustup update stable` + install system deps ([details](docs/troubleshooting.md#rust-compilation-errors)) |

For all troubleshooting topics, see [docs/troubleshooting.md](docs/troubleshooting.md).

## Contributing

1. Only commit changes to integration tooling (compose files, shardctl code)
2. Never commit service code (it belongs in service repos)
3. Test changes with both dev and prod profiles
4. Update documentation for new features

For development workflow, advanced usage, and best practices, see [docs/development.md](docs/development.md).

## License

MIT License - See LICENSE file for details

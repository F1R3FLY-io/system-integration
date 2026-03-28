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

### Using `just` (Recommended)

[`just`](https://github.com/casey/just) is the recommended command runner. Install it with `brew install just`, `cargo install just`, or see [just installation](https://github.com/casey/just#installation).

```bash
just up              # Start Rust multi-node shard (bootstrap + 3 validators + observer)
just wait            # Wait for all nodes to reach Running state (~2-3 min)
just status          # Show container status
just logs            # Follow logs
just down            # Stop shard
just reset           # Stop and wipe data volumes
```

Run `just` with no arguments to see all available commands.

### Using `shardctl`

```bash
poetry run shardctl up f1r3node-rust
poetry run shardctl wait

# Scala shard (deprecated — will be removed in a future release)
poetry run shardctl up f1r3node
```

Genesis takes ~2-3 minutes. `shardctl wait` blocks until all nodes report Running.

### Verify

```bash
just status
# or: poetry run shardctl status
```

Once all nodes show Running, the HTTP API is available on each node's port 40403 (bootstrap), 40413 (validator1), etc. See [COMPOSE_STRUCTURE.md](COMPOSE_STRUCTURE.md) for the full port map.

### Stop

```bash
just down                            # Stop containers
just reset                           # Stop and wipe data volumes
# or:
poetry run shardctl down             # Stop containers
poetry run shardctl reset -y         # Stop and wipe data volumes
```

> **No Poetry or just?** Shards can be run directly with Docker Compose:
> ```bash
> docker compose --env-file .env.node -f compose/f1r3node-rust.yml up -d
> docker compose --env-file .env.node -f compose/f1r3node-rust.yml logs -f | grep "Making a transition to Running state"
> docker compose --env-file .env.node -f compose/f1r3node-rust.yml down           # stop
> docker compose --env-file .env.node -f compose/f1r3node-rust.yml down -v        # stop and wipe data
> ```

## Benchmark and Demo

Run an automated benchmark that exercises the full consensus path across the shard with all supporting services:

```bash
just benchmark              # 10 rounds, 300s startup timeout
just benchmark 20           # 20 rounds
just benchmark 20 600       # 20 rounds, 600s timeout
```

The benchmark:
1. Starts the f1r3node-rust shard (genesis ceremony with bootstrap + 3 validators + observer)
2. Waits for all nodes to reach Running state
3. Starts supporting services (monitoring, embers, f1r3sky)
4. Runs deploy/propose cycles round-robin across validators via HTTP API
5. Collects per-round timing metrics (deploy, propose, finalize)
6. Generates a summary report with aggregate stats (min/max/avg/p95, throughput, pass/fail)

To tear down services and view the report separately:

```bash
just teardown               # Stop all services, print report
```

Use `--keep` to leave services running after the benchmark completes:

```bash
./scripts/benchmark.sh --keep       # Benchmark without teardown
just teardown                       # Tear down later
```

Ctrl-C during a benchmark triggers graceful teardown and prints a partial report.

## Node Topologies

| Command | Description |
|---------|-------------|
| `shardctl up f1r3node-rust` | Rust shard (boot + 3 validators + observer) |
| `shardctl up f1r3node-rust-standalone` | Rust standalone (single node) |
| `shardctl up f1r3node` | Scala shard |
| `shardctl up f1r3node-standalone` | Scala standalone |
| `shardctl up f1r3node-shard-light` | Scala light shard (boot + 2 validators, ~7.5 GB RAM) |

See [COMPOSE_STRUCTURE.md](COMPOSE_STRUCTURE.md) for details on each compose file.

All `shardctl` commands require `poetry run` prefix unless you activate the shell with `poetry shell`.

### `just` Commands

| Command | Description |
|---------|-------------|
| `just up` | Start Rust shard (boot + 3 validators + observer) |
| `just down` | Stop Rust shard |
| `just reset` | Stop and wipe data volumes |
| `just status` | Show container status |
| `just logs` | Follow shard logs |
| `just wait` | Wait for nodes to reach Running state |
| `just up-standalone` | Start Rust standalone (single node) |
| `just up-monitoring` | Start Prometheus + Grafana |
| `just up-embers` | Start Embers API + frontend |
| `just up-f1r3sky` | Start F1R3Sky AT Protocol stack |
| `just benchmark` | Run full shard benchmark (default: 10 rounds) |
| `just teardown` | Tear down all services, print report |
| `just down-all` | Stop all containers across all compose files |
| `just reset-all` | Stop all and remove all data volumes |
| `just ps` | Show all running F1R3FLY containers |
| `just clone` | Clone all service repositories |

### Native Services

Some services run natively on the host instead of in Docker. They are defined in `services.yml` with a `run_command` and orchestrated by `shardctl up` / `shardctl down`.

| Command | Description |
|---------|-------------|
| `shardctl up f1r3drive` | F1R3Drive FUSE filesystem (foreground, Ctrl-C to stop) |

F1R3Drive requires Java 17+ and a FUSE library:
- **macOS:** [macFUSE](https://github.com/macfuse/macfuse/wiki/Getting-Started)
- **Linux:** `libfuse-dev` / [jnr-fuse](https://github.com/SerCeMan/jnr-fuse?tab=readme-ov-file#installation)

See [docs/f1r3drive-guide.md](docs/f1r3drive-guide.md) for full setup and usage.

### Custom Docker Images

Override the default image with env vars:

```bash
F1R3FLY_RUST_IMAGE=f1r3flyindustries/f1r3fly-rust-node:dev poetry run shardctl up f1r3node-rust
F1R3FLY_SCALA_IMAGE=f1r3flyindustries/f1r3fly-scala-node:v1.2.3 poetry run shardctl up f1r3node
```

| Variable | Default | Used by |
|---|---|---|
| `F1R3FLY_IMAGE` | `f1r3flyindustries/f1r3node-rust:latest` | All Rust node compose files |
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
poetry run shardctl down monitoring     # Stop monitoring (shard stays running)
```

| Component | URL | Description |
|---|---|---|
| Prometheus | http://localhost:9090 | Metrics collection, recording rules, target health |
| Grafana | http://localhost:3000 | Dashboards (admin/admin) |
| cAdvisor | http://localhost:8080 | Container CPU/memory/IO metrics |

Prometheus uses DNS-based service discovery on the Docker network. Only nodes that are actually running get scraped — no false DOWN targets when running light shard or standalone.

**Dashboards** (auto-provisioned):
- **F1R3FLY Node** — block finalization, validator status, consensus metrics
- **Block Transfer** — block download/validation timing, transport metrics

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
├── scripts/                        # Benchmark and setup scripts
│   ├── benchmark.sh                # Shard benchmark orchestrator
│   └── setup-hooks.sh              # Git hooks installer
├── shardctl/                       # CLI tool package
├── .github/workflows/              # CI smoke test pipeline
├── integration-tests/              # Integration test suite
├── services/                       # Service repositories (git-ignored)
├── docs/                           # Prerequisites, troubleshooting, development guide
├── justfile                        # just command runner recipes
├── .env.node                       # Node environment variables
├── services.yml                    # Service repository URLs and branches
└── pyproject.toml                  # Python package config
```



### Blockchain Issues

#### F1R3node won't accept deployments (Casper not ready)

**Symptom:** Embers API crashes with "casper instance was not available yet"

**Cause:** Blockchain needs time to initialize after genesis

**Solution:**
1. Wait 2-3 minutes after `shardctl up` for Casper to fully initialize
2. Check logs for "Making a transition to Running state":
   ```bash
   poetry run shardctl logs rnode.bootstrap | grep "Running state"
   ```
3. Restart Embers after blockchain is ready:
   ```bash
   poetry run shardctl restart embers-api
   ```

#### Blockchain stuck or won't start properly

**Symptom:** Nodes stay unhealthy, or blockchain doesn't complete genesis

**Cause:** Corrupted data from previous run

**Solution:**
```bash
# Stop all services
poetry run shardctl down

# Clean blockchain data
sudo rm -rf services/f1r3node/docker/data

# Restart (will trigger fresh genesis)
poetry run shardctl up
```

**Note:** This is a fresh private blockchain, so cleaning data is safe for development.

### Container Issues

#### Permission denied removing files

**Symptom:** Cannot delete `services/f1r3node/docker/data` files

**Cause:** Docker containers created files as root

**Solution:**
```bash
sudo rm -rf services/f1r3node/docker/data
```

#### Services Won't Start

```bash
# Check compose configuration
poetry run shardctl compose config

# View service logs
poetry run shardctl logs service-name

# Check if ports are already in use
poetry run shardctl ps
```

#### Permission Issues

```bash
# Shell into container to check
poetry run shardctl shell service-name

# Check file ownership
poetry run shardctl exec service-name ls -la /app
```

### Network Issues

```bash
# Restart with fresh network
poetry run shardctl down
poetry run shardctl up

# For advanced network diagnostics, you can use docker directly:
docker network inspect system-integration_f1r3fly
```

### Complete Clean Slate

If nothing else works, start completely fresh:

```bash
# Stop everything
poetry run shardctl down

# Remove all containers and data volumes
poetry run shardctl reset -y

# Remove and re-clone services
rm -rf services/*
poetry run shardctl clone

# Rebuild all Docker images
poetry run shardctl build-service -a

# Start fresh
poetry run shardctl up
# Wait 2-3 minutes for blockchain initialization
poetry run shardctl logs --follow rnode.bootstrap
```

## Advanced Usage

### Custom Compose Files

Add additional compose files to `config.py`:

```python
def get_compose_files_for_profile(self, profile: Optional[str] = None) -> List[Path]:
    files = [self.compose_file]

    if profile == "staging":
        files.append(self.root_dir / "docker-compose.staging.yml")

    return [f for f in files if f.exists()]
```

### Environment Variables

Create `.env` file in repository root:

```env
# Environment-specific settings
DATABASE_URL=postgresql://user:pass@postgres:5432/db
REDIS_URL=redis://redis:6379
API_KEY=your-api-key
```

Docker Compose automatically loads this file.

### Custom Scripts

Add convenience scripts that use shardctl:

```bash
#!/bin/bash
# scripts/dev-up.sh

poetry run shardctl up --profile dev --build
poetry run shardctl logs --follow
```

### Poetry Development Commands

```bash
# Install dependencies
poetry install

# Add a new dependency
poetry add package-name

# Add a dev dependency
poetry add --group dev package-name

# Update dependencies
poetry update

# Show installed packages
poetry show

# Run unit tests (fast, no Docker required)
poetry run pytest integration-tests/test/test_internal.py -v --tb=short

# Run full integration tests (requires Docker, 10-30+ min)
poetry run shardctl test

# Format code with ruff
poetry run ruff format shardctl/

# Lint with ruff
poetry run ruff check shardctl/

# Activate virtual environment
poetry shell
```

## Best Practices

1. **Never commit service directories**: They're git-ignored for a reason
2. **Use profiles**: Keep prod and dev configurations separate
3. **Document service dependencies**: Update compose files with proper `depends_on`
4. **Pin image versions**: Use specific tags, not `latest`
5. **Use volume mounts in dev**: Enable hot reload for faster development
6. **Run builds explicitly**: Use `--build` when you've changed dependencies
7. **Monitor logs**: Use `--follow` during development
8. **Clean up regularly**: Run `down --volumes` to free space

## Contributing

1. Only commit changes to integration tooling (compose files, shardctl code)
2. Never commit service code (it belongs in service repos)
3. CI runs automatically on PRs (compose validation, topology health, integration tests)
4. Update documentation for new features

For development workflow and best practices, see [docs/development.md](docs/development.md).

## License

MIT License - See LICENSE file for details

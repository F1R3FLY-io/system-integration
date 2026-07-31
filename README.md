# F1R3FLY System Integration

Orchestration tooling for the F1R3FLY blockchain ecosystem. Manages multiple service repositories with Docker Compose and the `shardctl` CLI.

## Prerequisites

- **Python 3.10+** ([pyenv setup](docs/setup.md#python-310-pyenv) if your system Python is newer)
- **Poetry** — `pipx install poetry` or `pip install --user poetry`
- **Docker & Docker Compose**

```bash
poetry install   # Installs shardctl
```

For per-service build toolchains (Rust, SBT, Node), see [docs/setup.md](docs/setup.md).

## Quick Start

### 1. Start a shard

```bash
poetry run shardctl up f1r3node-rust
poetry run shardctl wait
```

Genesis takes ~2-3 minutes. `shardctl wait` blocks until all nodes report Running.

### 2. Verify

```bash
poetry run shardctl status
```

HTTP API endpoints once Running: bootstrap on port 40403, validator1 on 40413, etc. Full port map in [COMPOSE_STRUCTURE.md](COMPOSE_STRUCTURE.md#port-map).

### 3. Stop

```bash
poetry run shardctl down             # Stop containers
poetry run shardctl reset -y         # Stop and wipe data volumes
```

> **No Poetry?** You can run shards directly with Docker Compose:
> ```bash
> docker compose --env-file .env.node -f compose/f1r3node-rust.yml up -d
> docker compose --env-file .env.node -f compose/f1r3node-rust.yml logs -f
> docker compose --env-file .env.node -f compose/f1r3node-rust.yml down -v   # stop + wipe
> ```

## Where to go next

| Goal | Doc |
|---|---|
| Different topology (standalone, light shard, observer, validator4) | [COMPOSE_STRUCTURE.md](COMPOSE_STRUCTURE.md) |
| Custom node Docker image | [COMPOSE_STRUCTURE.md#image-selection](COMPOSE_STRUCTURE.md#image-selection) |
| Full multi-service setup (clone all repos, build images, start everything) | [docs/setup.md#full-multi-service-setup](docs/setup.md#full-multi-service-setup) |
| Every `shardctl` command + flag | [docs/cli-reference.md](docs/cli-reference.md) |
| Node configs + env files | [docs/configuration.md](docs/configuration.md) |
| Consensus parameters (FTT, synchrony) | [docs/consensus-configuration.md](docs/consensus-configuration.md) |
| Monitoring (Prometheus + Grafana) | [COMPOSE_STRUCTURE.md#monitoring-stack](COMPOSE_STRUCTURE.md#monitoring-stack) |
| Run integration tests | [integration-tests/README.md](integration-tests/README.md) |
| Native services (F1R3Drive FUSE) | [docs/f1r3drive-guide.md](docs/f1r3drive-guide.md) |
| Slashing | [docs/slashing-mechanism.md](docs/slashing-mechanism.md) |
| Troubleshooting | [docs/troubleshooting.md](docs/troubleshooting.md) |
| Development workflow | [docs/development.md](docs/development.md) |

## Repository structure

See [CLAUDE.md](CLAUDE.md#repository-structure) for the full directory layout.



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

1. Only commit changes to integration tooling (compose files, shardctl code, docs)
2. Never commit service code (it belongs in service repos under `services/`)
3. CI runs automatically on PRs (compose validation, topology health, integration tests)
4. Update relevant docs when adding features

For development workflow and best practices, see [docs/development.md](docs/development.md).

## License

MIT License — see LICENSE file for details

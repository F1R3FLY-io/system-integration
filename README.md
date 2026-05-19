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
| Different topology (Scala, standalone, observer, validator4, light shard) | [COMPOSE_STRUCTURE.md](COMPOSE_STRUCTURE.md) |
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

## Contributing

1. Only commit changes to integration tooling (compose files, shardctl code, docs)
2. Never commit service code (it belongs in service repos under `services/`)
3. CI runs automatically on PRs (compose validation, topology health, integration tests)
4. Update relevant docs when adding features

For development workflow and best practices, see [docs/development.md](docs/development.md).

## License

MIT License — see LICENSE file for details

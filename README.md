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

### Which node do I talk to?

The nodes in a shard are not interchangeable, and sending a request to the wrong one usually fails silently rather than with an error.

| I want to... | Send it to | Ports (default shard) |
|---|---|---|
| Submit a deploy (transfer, bond, contract) | **A bonded validator** — `rnode.validator1/2/3` | gRPC 40412, HTTP 40413 |
| Run an exploratory query (balance, bonds, PoS state) | **The read-only node** — `rnode.readonly` | gRPC 40452, HTTP 40453 |
| Read chain state (status, blocks, a deploy) | Any node | HTTP 40403 / 40413 / 40453 |

Two rules are worth knowing before the first deploy:

- **`rnode.bootstrap` is not a validator.** It runs the genesis ceremony and is absent from `genesis/bonds.txt`, and it starts with `--heartbeat-disabled`. It accepts a deploy, returns a deploy ID, and never includes it in a block — deploy queues are node-local and deploys do not gossip. Nothing reports an error; the deploy simply never lands.
- **Only `rnode.readonly` serves exploratory deploys.** Balance, bonds and PoS queries are built on them, and every other node answers `Exploratory deploy can only be executed on read-only node`.

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
| Add a validator to a running shard | [docs/adding-a-validator.md](docs/adding-a-validator.md) |
| Attach a node from another machine | [docs/attaching-an-external-node.md](docs/attaching-an-external-node.md) |
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

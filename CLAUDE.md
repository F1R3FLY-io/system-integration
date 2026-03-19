# F1R3FLY System Integration

## Project Overview

This is a microservices integration repository for the F1R3FLY blockchain ecosystem. It provides tooling and orchestration for managing multiple service repositories (f1r3node, f1r3sky, embers, etc.) as independent nested git repositories with docker-compose coordination.

## Key Concepts

- **Services are git-ignored**: Each service repository (f1r3node, embers, f1r3sky-backend, etc.) is cloned into `services/` and completely ignored by the parent system-integration repository
- **Independent development**: Work in each service directory normally with full git functionality - changes are isolated to each service repo
- **Docker Compose orchestration**: Each service has its own compose file in `compose/` directory
- **shardctl CLI**: Python CLI tool (installed via Poetry) that wraps docker-compose operations and service management

## Getting Started

**IMPORTANT: Read [README.md](./README.md) first** - it contains complete documentation on:
- Installation and setup
- How to clone service repositories
- Using the shardctl CLI tool
- Docker compose configuration
- Development workflow
- Troubleshooting

## Quick Reference

```bash
# Install shardctl
poetry install

# Clone all service repositories with correct branches
poetry run shardctl clone

# Start services
poetry run shardctl up

# View status
poetry run shardctl status

# View logs
poetry run shardctl logs --follow

# Stop services
poetry run shardctl down
```

## Git Hooks

Pre-commit and pre-push hooks enforce code quality. Install with:

```bash
./scripts/setup-hooks.sh           # Uses core.hooksPath (recommended)
./scripts/setup-hooks.sh --copy    # Copies to .git/hooks/ (alternative)
./scripts/setup-hooks.sh --status  # Show current configuration
```

- **pre-commit**: ruff lint, black format check, YAML validation on staged files
- **pre-push**: ruff + black on full codebase, `test_internal.py` unit tests

Bypass with `--no-verify` (not recommended). Skip individual checks with env vars:
`SKIP_LINT=1`, `SKIP_RUFF=1`, `SKIP_BLACK=1`, `SKIP_YAML=1`, `SKIP_TESTS=1`, `QUICK=1`.

## CI

GitHub Actions runs the same checks as the git hooks on pushes and PRs to `dev` and `main`:
- **Lint job**: ruff + black + YAML validation (runs on `ubuntu-latest`)
- **Test job**: `test_internal.py` unit tests with full integration deps

Workflow: `.github/workflows/ci.yml`

## Repository Structure

```
.
├── services/                    # Service repos (git-ignored)
│   ├── f1r3node/               # F1R3FLY blockchain node (Scala + Rust)
│   ├── embers/                 # Embers API (Rust)
│   ├── embers-frontend/        # Embers UI (React 19)
│   ├── f1r3sky-backend/        # AT Protocol backend (Node.js)
│   └── rust-client/            # Rust CLI client
├── .github/workflows/          # CI (GitHub Actions)
├── hooks/                      # Git hooks (pre-commit, pre-push)
├── scripts/                    # Setup scripts (setup-hooks.sh)
├── shardctl/                   # CLI tool package
├── compose/                    # Docker Compose files (one per service)
│   ├── f1r3node.yml            # Scala shard (default)
│   ├── f1r3node-rust.yml       # Rust shard
│   ├── embers.yml              # Embers API + frontend
│   ├── f1r3sky.yml             # F1R3Sky AT Protocol services
│   └── monitoring.yml          # Prometheus + Grafana
├── services.yml                # Service repository URLs and branches
├── .env.embers                 # Embers configuration
└── README.md                   # Full documentation
```

## Service Repositories

Services are defined in `services.yml` with their git URLs and branches:
- **f1r3node**: Blockchain node (main + rust-dev branches)
- **embers**: Blockchain API bridge (main branch)
- **embers-frontend**: Web UI for embers (main branch)
- **f1r3sky-backend**: AT Protocol services (main branch)
- **rust-client**: CLI tool for blockchain interaction (main branch)

## Important Notes

1. **Never commit service directories** - they're independent git repos
2. **Use `shardctl clone`** to set up service repositories with the correct branches
3. **Each compose file is independent** - start only what you need:
   - `compose/f1r3node.yml` - F1R3node Scala shard (default)
   - `compose/f1r3node-rust.yml` - F1R3node Rust shard
   - `compose/embers.yml` - Embers API and frontend
   - `compose/f1r3sky.yml` - F1R3Sky AT Protocol services
   - `compose/monitoring.yml` - Prometheus + Grafana
4. **Services communicate via Docker network** - `f1r3fly` network
5. **Always use shardctl commands** - Don't run builds manually (cargo, sbt, etc.). Use:
   - `poetry run shardctl build-service <service>` for regular builds
   - `poetry run shardctl build-service <service> --docker` for Docker image builds
   - `poetry run shardctl build-service --list` to see available services
6. **Read README.md** for complete documentation and best practices

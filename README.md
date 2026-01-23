# System Integration Repository

A microservices integration repository for managing multiple services with docker-compose and the `shardctl` CLI tool.

## Overview

This repository provides a clean structure for managing multiple microservice repositories as nested git repos, with docker-compose orchestration and a convenient CLI tool for common operations.

### Key Features

- **Nested Git Repositories**: Service repos are cloned into `services/` and fully git-ignored by the parent repo
- **Independent Development**: Work in each service directory normally with full git functionality
- **Docker Compose Orchestration**: Layer base and profile-specific compose configurations
- **Convenient CLI**: `shardctl` wraps docker-compose with user-friendly commands
- **Profile Support**: Switch between dev and prod configurations easily
- **Rich Terminal Output**: Colorized, formatted output for better readability

## Repository Structure

```
.
├── services/                    # Service repositories (git-ignored)
│   ├── .gitkeep                # Tracked to maintain directory
│   ├── service-1/              # Independent git repo (ignored)
│   └── service-2/              # Independent git repo (ignored)
├── shardctl/                   # CLI tool package
│   ├── __init__.py
│   ├── cli.py                  # Typer CLI commands
│   ├── compose.py              # ComposeManager class
│   ├── config.py               # Configuration management
│   └── utils.py                # Helper functions
├── docker-compose.yml          # Base compose configuration
├── docker-compose.dev.yml      # Development overrides
├── services.yml                # Service repository URLs (optional)
├── pyproject.toml              # Python package configuration
├── .gitignore                  # Ignores services/*/ subdirectories
└── README.md                   # This file
```

## Installation

### Prerequisites

#### Core Requirements

- **Python 3.10** - Required for shardctl CLI and service builds

  On many recent Linux distributions, the latest Python is 3.13, which is not compatible with some services. At the moment, you need to use 3.10 or the build will likely fail. You can get the right python version any way you like, but `pyenv` is recommended.

  - **Recommended:** Use [pyenv](https://github.com/pyenv/pyenv) to manage Python versions
  ```bash
  # Install pyenv (Linux)
  curl https://pyenv.run | bash

  # Add to ~/.bashrc or ~/.zshrc:
  export PYENV_ROOT="$HOME/.pyenv"
  [[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"
  eval "$(pyenv init -)"

  # Restart shell, then install Python 3.10
  pyenv install 3.10
  pyenv local 3.10  # Sets Python 3.10 for this project
  ```
  - Alternatively, use [asdf](https://asdf-vm.com/) with the python plugin, or your system package manager
- **Docker & Docker Compose** - Container orchestration
- **Git** - For cloning service repositories
- **Poetry** - Python dependency management for shardctl

#### Service-Specific Build Dependencies

Different services require specific build tools. Install based on which services you'll be building:

**For F1R3node (Scala blockchain node):**
- **Nix** (recommended) - Provides complete dev environment via `nix develop`
  ```bash
  # Install Nix (Linux/macOS)
  sh <(curl -L https://nixos.org/nix/install) --daemon
  ```
- **OR manually install:**
  - **SBT (Scala Build Tool)** - Version 1.5+
  - **Rust toolchain** - For native libraries (rspace, rholang)
    ```bash
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
    ```
  - **Cargo** - Comes with Rust installation
  - **Java 11** - OpenJDK 11 or higher

**For F1R3Sky services (AT Protocol - Node.js/TypeScript):**
- **Node.js 18+** - JavaScript runtime (version 20.11 recommended for Docker builds)

  This project assumes you have `nvm` installed and working and the current version of node is 20.11.

- **pnpm 8.15.9+** - Fast, disk-efficient package manager
  ```bash
  curl -fsSL https://get.pnpm.io/install.sh | sh -
  ```
- **node-gyp** - For compiling native Node.js modules
  ```bash
  # After installing pnpm, set up global bin directory and install node-gyp
  pnpm setup
  export PNPM_HOME="$HOME/.local/share/pnpm"
  export PATH="$PNPM_HOME:$PATH"
  pnpm add -g node-gyp
  ```

**For Embers (Rust API service):**
- **Rust 1.91+** - Rust toolchain
  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
  ```
- **Cargo** - Comes with Rust
- **pkg-config** - Build configuration tool
- **protobuf-compiler** - Protocol buffers compiler
- **clang** - C/C++ compiler for some Rust dependencies

**For Rust Client:**
- **Rust 1.85.0+** - Latest stable Rust
- **Cargo** - Package manager (comes with Rust)

### Install Poetry

If you don't have Poetry installed:

```bash
# Using pipx (recommended)
pipx install poetry

# Or using pip
pip install --user poetry

# Or using the official installer
curl -sSL https://install.python-poetry.org | python3 -
```

### Install shardctl

From the repository root:

```bash
# Install dependencies and create virtual environment
poetry install

# Run shardctl commands using poetry run
poetry run shardctl --help

# Or activate the virtual environment
poetry shell
shardctl --help
```

Poetry automatically manages a virtual environment and installs all dependencies.

## Quick Start

### Complete Setup from Scratch

This guide walks through setting up the entire F1R3FLY stack from scratch:

#### 1. Install Dependencies

First, ensure you have the core requirements:

```bash
# Install Poetry (if not already installed)
curl -sSL https://install.python-poetry.org | python3 -

# Install shardctl
poetry install

# Verify installation
poetry run shardctl --help
```

#### 2. Install Service Build Tools

Install the build dependencies for the services you want to work with:

```bash
# For F1R3Sky services (Node.js/TypeScript)
curl -fsSL https://get.pnpm.io/install.sh | sh -
pnpm setup
export PNPM_HOME="$HOME/.local/share/pnpm"
export PATH="$PNPM_HOME:$PATH"
pnpm add -g node-gyp

# For Rust services (Embers, rust-client)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source $HOME/.cargo/env

# For F1R3node (Scala) - Option 1: Use Nix (recommended)
sh <(curl -L https://nixos.org/nix/install) --daemon

# For F1R3node (Scala) - Option 2: Install manually
# Install SBT, Java 11, and Rust (see Prerequisites section)
```

#### 3. Clone Service Repositories

Clone all service repositories with correct branches:

```bash
# This clones all enabled services defined in services.yml
poetry run shardctl clone
```

This will clone:
- `f1r3node` (main branch) - Scala blockchain node
- `rust-client` (main branch) - Rust CLI client
- `f1r3sky-backend` (main branch) - AT Protocol services
- `embers` (main branch) - Rust API bridge

Each service directory becomes an independent git repository.

#### 4. Build Services

Build all services from source (optional - you can skip to Docker builds):

```bash
# Build all enabled services from source
poetry run shardctl build --no-docker

# Or build specific services
poetry run shardctl build f1r3node --no-docker
poetry run shardctl build embers --no-docker
```

**Note:** Source builds are useful for development. For production, you only need Docker images.

#### 5. Build Docker Images

Build Docker images for all services:

```bash
# Build all Docker images (source + Docker build)
poetry run shardctl build

# This builds images for:
# - f1r3flyindustries/f1r3fly-scala-node:latest (f1r3node)
# - f1r3flyindustries/embers:latest
# - f1r3flyindustries/f1r3sky-bsky:latest
# - f1r3flyindustries/f1r3sky-pds:latest
# - f1r3flyindustries/f1r3sky-bsync:latest
# - f1r3flyindustries/f1r3sky-ozone:latest
```

**Expected build times:**
- F1R3node: ~10-15 minutes (first build)
- F1R3Sky services: ~2-3 minutes each
- Embers: ~3-5 minutes

#### 6. Start the Stack

Start all services using shardctl (which automatically orchestrates all configured services):

```bash
# Start all services (F1R3node, F1R3Sky, and Embers)
poetry run shardctl up
```

**Important:** F1R3node blockchain needs 2-3 minutes after startup to:
1. Complete genesis ceremony (validators signing genesis block)
2. Transition to "Running" state
3. Initialize Casper consensus (ready to accept deployments)

Wait for blockchain initialization before using Embers API. You can monitor the blockchain startup progress with:

```bash
poetry run shardctl logs --follow rnode.bootstrap
```

#### 7. Verify All Services Running

Check that all containers are healthy:

```bash
# View formatted status
poetry run shardctl status

# Or list all containers
poetry run shardctl ps
```

You should see:
- **F1R3node**: 5 nodes (bootstrap, validator1-3, readonly) - all healthy
- **F1R3Sky**: postgres, redis (healthy), bsky, pds, bsync, ozone
- **Embers**: embers-api
- **Monitoring**: prometheus, grafana

#### 8. Access Services

Services are now accessible:

- **F1R3node RPC** (validator1): http://localhost:40411
- **F1R3node API** (validator1): http://localhost:40413
- **F1R3node Read-only**: http://localhost:40453
- **Embers API**: http://localhost:8080
- **F1R3Sky PDS**: http://localhost:2583
- **F1R3Sky BSKY (AppView)**: http://localhost:2584
- **F1R3Sky Ozone (Moderation)**: http://localhost:3101
- **Grafana**: http://localhost:3000
- **Prometheus**: http://localhost:9090

#### 9. View Logs

Monitor service logs using shardctl:

```bash
# F1R3node blockchain logs
poetry run shardctl logs --follow rnode.bootstrap

# Embers API logs
poetry run shardctl logs --follow embers-api

# F1R3Sky service logs
poetry run shardctl logs --follow f1r3sky-pds
poetry run shardctl logs --follow f1r3sky-bsky

# View all logs at once
poetry run shardctl logs --follow
```

#### 10. Stop Services

When done:

```bash
# Stop all services
poetry run shardctl down
```

### Quick Commands Reference

```bash
# List available services
poetry run shardctl build --list

# Build specific service (source only)
poetry run shardctl build f1r3node --no-docker

# Build Docker image only
poetry run shardctl build embers

# View service status
poetry run shardctl status

# Follow all logs
poetry run shardctl logs --follow

# Restart blockchain after data cleanup
sudo rm -rf services/f1r3node/docker/data
poetry run shardctl up
```

**Tip:** Activate Poetry shell to avoid typing `poetry run` every time:
```bash
poetry shell
shardctl up
shardctl status
```

## CLI Commands

**Note:** All commands below assume you're either using `poetry run shardctl` or have activated the Poetry shell with `poetry shell`. Examples show commands without the `poetry run` prefix for brevity.

### Service Management

```bash
# Start services (detached by default)
shardctl up [SERVICES...] [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)
  --foreground, -f      Run in foreground
  --build, -b           Build images first

# Stop services
shardctl down [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)
  --volumes, -v         Remove volumes
  --keep-orphans        Keep orphan containers

# Restart services
shardctl restart [SERVICES...] [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)

# View status in formatted table
shardctl status [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)

# List containers
shardctl ps [SERVICES...] [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)

# View logs
shardctl logs [SERVICES...] [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)
  --follow, -f          Follow output
  --tail, -n INTEGER    Number of lines
```

### Build and Images

```bash
# Build services from their configured build commands in services.yml
shardctl build [SERVICE] [OPTIONS]
  --no-docker                  Skip Docker image building (source build only)
  --list, -l                   List services with build configurations
  --include-disabled            Include disabled services (default: enabled only)

# Pull service images
shardctl pull [SERVICES...] [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)

# Bring up only the shard (blockchain stack)
shardctl up --shard-only
```

### Container Interaction

```bash
# Execute command in service
shardctl exec SERVICE COMMAND... [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)
  --no-tty, -T         Disable TTY

# Open interactive shell
shardctl shell SERVICE [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)
  --shell, -s TEXT     Shell to use (default: /bin/bash)

# Examples
shardctl exec service-1 ls -la /app
shardctl shell service-1
shardctl shell postgres --shell /bin/sh
```

### Setup and Configuration

```bash
# Create example services.yml
shardctl setup --create-config

# Clone all service repositories
shardctl setup [OPTIONS]
  --force, -f           Remove existing before cloning

# Run custom docker-compose command
shardctl compose ARGS... [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)

# Examples
shardctl compose config --services
shardctl compose images
shardctl compose top service-1
```

## Docker Compose Configuration

### Base Configuration (docker-compose.yml)

The base configuration defines production-ready service definitions:

- Service build contexts pointing to `./services/service-name`
- Production environment variables
- Port mappings
- Volume mounts
- Networks
- Service dependencies

### Development Overrides (docker-compose.dev.yml)

The development configuration extends the base with:

- Development-specific environment variables (DEBUG=true, etc.)
- Source code volume mounts for hot reload
- Development command overrides
- Development tools (Adminer, Redis Commander, etc.)
- Different port mappings to avoid conflicts

### Profiles

Compose profiles allow selective service activation:

- **prod**: Production services (databases, caches, etc.)
- **dev**: Development services and tools

```bash
# Start only base services
shardctl up

# Start with prod profile (includes postgres, redis)
shardctl up --profile prod

# Start with dev profile (includes dev tools)
shardctl up --profile dev
```

## Working with Services

### Developing in Service Directories

Each service in `services/` is an independent git repository:

```bash
cd services/service-1

# Work normally with git
git status
git checkout -b feature/new-feature
git add .
git commit -m "Add new feature"
git push origin feature/new-feature

# Changes are isolated to the service repo
# Integration repo doesn't track these changes
```

### Adding a New Service

1. Add the service to `services.yml`:

```yaml
repositories:
  new-service: https://github.com/your-org/new-service.git
```

2. Clone the service:

```bash
shardctl setup
```

3. Add service definition to `docker-compose.yml`:

```yaml
services:
  new-service:
    build:
      context: ./services/new-service
      dockerfile: Dockerfile
    container_name: new-service
    networks:
      - app-network
    ports:
      - "8003:8000"
```

4. Start the new service:

```bash
shardctl up new-service --build
```

### Removing a Service

1. Stop and remove containers:

```bash
shardctl down
```

2. Remove service directory:

```bash
rm -rf services/service-name
```

3. Remove service definition from compose files

4. Update `services.yml` if needed

## Development Workflow

### Typical Development Session

```bash
# 1. Start development environment
shardctl up --profile dev --build

# 2. View status
shardctl status

# 3. Watch logs
shardctl logs --follow

# 4. Make changes in service directories
cd services/service-1
# ... edit code ...
# (Hot reload should pick up changes)

# 5. Run commands in containers
shardctl exec service-1 npm test
shardctl shell service-1

# 6. Restart specific service if needed
shardctl restart service-1

# 7. Stop when done
shardctl down
```

### Rebuilding After Changes

```bash
# Rebuild a specific docker-compose service image
shardctl compose build service-1

# Rebuild and restart the container
shardctl compose build service-1 && shardctl restart service-1

# Or rebuild and start
shardctl up service-1 --build
```

## Troubleshooting

### Common Build Issues

#### F1R3Sky services fail with "better-sqlite3" errors

**Symptom:** Build fails with compilation errors for `better-sqlite3` module

**Cause:** Node.js 24.x has compatibility issues with better-sqlite3

**Solution:**
1. Ensure you have node-gyp installed globally:
   ```bash
   pnpm add -g node-gyp
   ```
2. Use Docker builds instead of source builds (Docker uses Node 20.11):
   ```bash
   poetry run shardctl build f1r3sky-backend-bsky  # Builds Docker image (when configured)
   ```
3. The Docker build will succeed even if source build fails

#### Missing pnpm or node-gyp

**Symptom:** `pnpm: not found` or `node-gyp: not found`

**Solution:**
```bash
# Install pnpm
curl -fsSL https://get.pnpm.io/install.sh | sh -

# Setup pnpm paths
pnpm setup
export PNPM_HOME="$HOME/.local/share/pnpm"
export PATH="$PNPM_HOME:$PATH"

# Add to ~/.bashrc for persistence
echo 'export PNPM_HOME="$HOME/.local/share/pnpm"' >> ~/.bashrc
echo 'export PATH="$PNPM_HOME:$PATH"' >> ~/.bashrc

# Install node-gyp globally
pnpm add -g node-gyp
```

#### Rust compilation errors

**Symptom:** Cargo build fails with linker errors or missing dependencies

**Solution:**
```bash
# Ensure Rust is up to date
rustup update stable

# Install system dependencies (Ubuntu/Debian)
sudo apt-get install pkg-config libssl-dev protobuf-compiler clang

# Or on macOS
brew install protobuf
```

#### PNPM fails in Docker build

**Symptom:** The `pnpm` command fails in docker build for f1r3sky builds.

**Solution:**

The `pnpm` command will use IPv6 if it appears to be available and has no option for fall-back to IPv4. The `services.yml` file is configured to run the `f1r3sky` builds using host networking, but if your host interface has IPv6 configured *and* if it doesn't work, then `pnpm` can fail. Disable IPv6 on your host interface to resolve the issue.


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

# Remove all volumes and data
poetry run shardctl down --volumes
sudo rm -rf services/f1r3node/docker/data

# Remove and re-clone services
rm -rf services/*
poetry run shardctl clone

# Rebuild all Docker images
poetry run shardctl build

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

# Run tests
poetry run pytest

# Format code with black
poetry run black shardctl/

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

When contributing to this repository:

1. Only commit changes to integration tooling (compose files, shardctl code)
2. Never commit service code (it belongs in service repos)
3. Test changes with both dev and prod profiles
4. Update documentation for new features

## License

MIT License - See LICENSE file for details

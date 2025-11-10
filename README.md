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

- Python 3.8+
- Docker and docker-compose
- Git (for cloning service repos)
- Poetry (Python dependency management)

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

### 1. Configure Service Repositories

Create a `services.yml` file to define your service repositories:

```bash
poetry run shardctl setup --create-config
```

Edit `services.yml` to add your service repository URLs:

```yaml
repositories:
  service-1: https://github.com/your-org/service-1.git
  service-2: https://github.com/your-org/service-2.git
  api-gateway: https://github.com/your-org/api-gateway.git
```

### 2. Clone Service Repositories

Clone all configured services into the `services/` directory:

```bash
poetry run shardctl setup
```

Each service becomes an independent git repository that you can work with normally.

### 3. Start Services

```bash
# Start all services in detached mode
poetry run shardctl up

# Start with development profile
poetry run shardctl up --profile dev

# Start specific services
poetry run shardctl up service-1 service-2

# Start in foreground with build
poetry run shardctl up --foreground --build
```

### 4. View Status

```bash
# Show formatted status table
poetry run shardctl status

# List containers (raw docker-compose output)
poetry run shardctl ps
```

### 5. View Logs

```bash
# View logs for all services
poetry run shardctl logs

# Follow logs in real-time
poetry run shardctl logs --follow

# Show last 100 lines for specific service
poetry run shardctl logs service-1 --tail 100
```

### 6. Stop Services

```bash
# Stop and remove containers
poetry run shardctl down

# Also remove volumes
poetry run shardctl down --volumes
```

**Tip:** To avoid typing `poetry run` for every command, activate the virtual environment:
```bash
poetry shell
# Now you can use shardctl directly
shardctl up
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
# Build services
shardctl build [SERVICES...] [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)
  --no-cache           Build without cache

# Pull service images
shardctl pull [SERVICES...] [OPTIONS]
  --profile, -p TEXT    Profile (dev/prod)
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
# Rebuild specific service
shardctl build service-1

# Rebuild without cache
shardctl build service-1 --no-cache

# Rebuild and restart
shardctl build service-1 && shardctl restart service-1

# Or rebuild and start
shardctl up service-1 --build
```

## Troubleshooting

### Services Won't Start

```bash
# Check compose configuration
shardctl compose config

# View service logs
shardctl logs service-name

# Check if ports are already in use
shardctl compose ps
docker ps  # Check for conflicts
```

### Permission Issues

```bash
# Shell into container to check
shardctl shell service-name

# Check file ownership
shardctl exec service-name ls -la /app
```

### Network Issues

```bash
# Inspect network
docker network inspect system-integration_app-network

# Restart with fresh network
shardctl down
shardctl up
```

### Clean Slate

```bash
# Stop everything and remove volumes
shardctl down --volumes

# Remove all containers and networks
shardctl compose down --remove-orphans

# Rebuild everything
shardctl build --no-cache
shardctl up --build
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
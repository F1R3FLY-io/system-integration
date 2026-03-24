# Development Guide

## Working with Service Repositories

Each service in `services/` is an independent git repository, fully ignored by the parent system-integration repo:

```bash
cd services/f1r3node-rust

# Work normally with git
git status
git checkout -b feature/new-feature
git add .
git commit -m "Add new feature"
git push origin feature/new-feature

# Changes are isolated to the service repo
# system-integration does not track these changes
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

3. Add a compose file at `compose/<service>.yml`:
   ```yaml
   services:
     new-service:
       build:
         context: ./services/new-service
         dockerfile: Dockerfile
       container_name: new-service
       networks:
         - f1r3fly
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

3. Remove the compose file from `compose/`

4. Update `services.yml`

## Development Workflow

### Typical Session

```bash
# 1. Start the stack
shardctl up f1r3node-rust
shardctl wait

# 2. View status
shardctl status

# 3. Watch logs
shardctl logs --follow

# 4. Make changes in service directories
cd services/embers
# ... edit code ...
# Hot reload picks up changes if configured

# 5. Run commands in containers
shardctl exec embers-api ls -la /app
shardctl shell embers-api

# 6. Restart specific service if needed
shardctl restart embers-api

# 7. Stop when done
shardctl down
```

### CI Pipeline

The CI workflow (`.github/workflows/smoke-test.yml`) runs automatically on PRs to `main`. It validates compose files, starts all topologies (checking for 10 finalized blocks), and runs representative integration tests — all in parallel across 12 runners.

### Rebuilding After Changes

```bash
# Rebuild specific service
shardctl build embers

# Rebuild without cache
shardctl build embers --no-cache

# Rebuild and restart
shardctl build embers && shardctl restart embers

# Or rebuild and start in one step
shardctl up embers --build
```

## Docker Compose Configuration

### Compose Directory

Each service has its own compose file in `compose/`. Files are managed via shardctl:

```bash
shardctl up <service>       # Start compose/<service>.yml
shardctl down <service>     # Stop compose/<service>.yml
shardctl up                 # Start all (startup_order from services.yml)
```

See [COMPOSE_STRUCTURE.md](../COMPOSE_STRUCTURE.md) for details on each compose file.

### Development Overrides

`docker-compose.dev.yml` is a template for development-specific configuration:

- Development environment variables (DEBUG=true, etc.)
- Source code volume mounts for hot reload
- Development command overrides
- Development tools (Adminer, Redis Commander, etc.)
- Different port mappings to avoid conflicts

### Profiles

Compose profiles allow selective service activation:

```bash
# Start only base services
shardctl up

# Start with prod profile (includes postgres, redis)
shardctl up --profile prod

# Start with dev profile (includes dev tools)
shardctl up --profile dev
```

## Advanced Usage

### Custom Compose Files

Add additional compose files by extending `config.py`:

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

### Poetry Commands

```bash
poetry install              # Install dependencies
poetry add package-name     # Add a new dependency
poetry add --group dev pkg  # Add a dev dependency
poetry update               # Update dependencies
poetry show                 # Show installed packages
poetry run pytest           # Run tests
poetry run black shardctl/  # Format code
poetry run ruff check shardctl/  # Lint
poetry shell                # Activate virtual environment
```

## Best Practices

1. **Never commit service directories** — they're git-ignored for a reason
2. **Use profiles** — keep prod and dev configurations separate
3. **Document service dependencies** — update compose files with proper `depends_on`
4. **Pin image versions** — use specific tags, not `latest`
5. **Use volume mounts in dev** — enable hot reload for faster development
6. **Run builds explicitly** — use `--build` when you've changed dependencies
7. **Monitor logs** — use `--follow` during development
8. **Clean up regularly** — run `down --volumes` to free space

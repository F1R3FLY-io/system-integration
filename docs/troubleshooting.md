# Troubleshooting

## macOS Specific Issues

### Docker "Outside of rootfs" Error

**Symptom:**
Services fail to start with:
```text
Error: failed to create task for container: ... error mounting "..." to rootfs at "...": mountpoint "..." is outside of rootfs
```

**Cause:**
Known issue with the **VirtioFS** file sharing implementation in Docker Desktop for macOS. Occurs when mounting files inside directories that are also Docker named volumes.

**Solution:**
Switch Docker's file sharing implementation to **gRPC FUSE**:

1. Open **Docker Desktop Dashboard**
2. Go to **Settings** (gear icon) -> **General**
3. Scroll to "Choose file sharing implementation for your containers"
4. Select **gRPC FUSE**
5. Click **Apply & Restart**
6. After Docker restarts:
   ```bash
   poetry run shardctl reset -y
   poetry run shardctl up
   ```

## Build Issues

### F1R3Sky: "better-sqlite3" compilation errors

**Symptom:** Build fails with compilation errors for `better-sqlite3` module

**Cause:** Node.js 24.x has compatibility issues with better-sqlite3

**Solution:**
1. Ensure node-gyp is installed globally:
   ```bash
   pnpm add -g node-gyp
   ```
2. Use Docker builds instead of source builds (Docker uses Node 20.11):
   ```bash
   poetry run shardctl build-service f1r3sky-backend-bsky
   ```

### Missing pnpm or node-gyp

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

### Rust compilation errors

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

### PNPM fails in Docker build

**Symptom:** `pnpm` command fails during Docker build for f1r3sky services

**Cause:** `pnpm` uses IPv6 if it appears available and has no fallback to IPv4. The `services.yml` file is configured to run f1r3sky builds using host networking, but if your host interface has IPv6 configured and it doesn't work, pnpm can fail.

**Solution:** Disable IPv6 on your host interface.

## Blockchain Issues

### F1R3node won't accept deployments (Casper not ready)

**Symptom:** Embers API crashes with "casper instance was not available yet"

**Cause:** Blockchain needs 2-3 minutes to initialize after genesis

**Solution:**
1. Wait for all nodes to reach Running state:
   ```bash
   poetry run shardctl wait
   ```
2. Verify in logs:
   ```bash
   poetry run shardctl logs rnode.bootstrap | grep "Running state"
   ```
3. Restart Embers after blockchain is ready:
   ```bash
   poetry run shardctl restart embers-api
   ```

### Blockchain stuck or won't start properly

**Symptom:** Nodes stay unhealthy, or blockchain doesn't complete genesis

**Cause:** Corrupted data from previous run

**Solution:**
```bash
# Stop all services and remove data volumes (triggers fresh genesis on next start)
poetry run shardctl reset -y

# Restart
poetry run shardctl up
```

## Container Issues

### Permission denied removing files

**Symptom:** Cannot remove blockchain data

**Cause:** Docker containers created files as root inside named volumes

**Solution:**
```bash
poetry run shardctl reset -y
```

### Services won't start

```bash
# Check compose configuration
poetry run shardctl compose config

# View service logs
poetry run shardctl logs <service-name>

# Check if ports are already in use
poetry run shardctl ps
```

### Permission issues inside containers

```bash
# Shell into container to check
poetry run shardctl shell <service-name>

# Check file ownership
poetry run shardctl exec <service-name> ls -la /app
```

## Network Issues

```bash
# Restart with fresh network
poetry run shardctl down
poetry run shardctl up

# For advanced network diagnostics:
docker network inspect f1r3fly
```

## Complete Clean Slate

If nothing else works, start completely fresh:

```bash
# Stop everything and remove data volumes
poetry run shardctl reset -y

# Remove and re-clone services
rm -rf services/*
poetry run shardctl clone

# Rebuild all Docker images
poetry run shardctl build-service -a

# Start fresh
poetry run shardctl up

# Wait for blockchain initialization
poetry run shardctl wait
```

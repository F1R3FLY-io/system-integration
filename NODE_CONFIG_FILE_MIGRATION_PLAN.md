# File Migration Plan: node-config/ into system-integration/

## Overview

Migrate `node-config/` contents into the parent `system-integration/` folder to unify node configuration with the rest of the project.

## Current State

### Problem: Two Separate Config Locations

**Location 1: `docker-compose.yml`** (existing - for full stack with embers/f1r3sky)
```yaml
volumes:
  - ./services/f1r3node/docker/conf/bootstrap-ceremony.conf:/var/lib/rnode/rnode.conf
  - ./services/f1r3node/docker/genesis/wallets.txt:/var/lib/rnode/genesis/wallets.txt
  - ./services/f1r3node/docker/certs/bootstrap/node.certificate.pem:/var/lib/rnode/node.certificate.pem
```

**Location 2: `node-config/compose/*.yml`** (new - standalone node configs)
```yaml
volumes:
  - ../conf/standalone-dev.conf:/var/lib/rnode/rnode.conf
  - ../genesis/standalone-wallets.txt:/var/lib/rnode/genesis/wallets.txt
  - ../certs/bootstrap/node.certificate.pem:/var/lib/rnode/node.certificate.pem
```

### Current Directory Structure
```
system-integration/
├── docker-compose.yml           # Full stack (expects services/f1r3node/docker/...)
├── docker-compose.embers.yml
├── docker-compose.f1r3sky.yml
├── .env.embers
├── .env.f1r3sky
├── services/                    # Cloned repos (f1r3node, embers, etc.)
│   └── f1r3node/
│       └── docker/
│           ├── conf/            # EXPECTED by docker-compose.yml
│           ├── genesis/         # (but doesn't exist until cloned)
│           └── certs/
├── node-config/                 # ISOLATED node configs (to be merged)
│   ├── compose/                 # 8 compose files
│   ├── conf/                    # Node config files
│   ├── genesis/                 # Genesis files
│   ├── certs/                   # Node certificates
│   ├── .env                     # Node credentials
│   └── run.sh
└── shardctl/                    # CLI tool
```

## Target State

### New Directory Structure
```
system-integration/
├── compose/                     # ALL compose files (moved from node-config/)
│   ├── scala-standalone.yml
│   ├── scala-shard.yml
│   ├── rust-standalone.yml
│   ├── rust-shard.yml
│   ├── scala-observer.yml
│   ├── rust-observer.yml
│   ├── scala-validator4.yml
│   ├── rust-validator4.yml
│   ├── embers.yml               # (renamed from docker-compose.embers.yml)
│   └── f1r3sky.yml              # (renamed from docker-compose.f1r3sky.yml)
├── conf/                        # ALL node config files
│   ├── bootstrap-ceremony.conf
│   ├── shared-rnode.conf
│   ├── shared-rnode-runtime.conf
│   ├── standalone-dev.conf
│   ├── observer.conf
│   └── logback.xml
├── genesis/                     # ALL genesis files
│   ├── bonds.txt
│   ├── wallets.txt
│   ├── standalone-bonds.txt
│   └── standalone-wallets.txt
├── certs/                       # ALL certificates
│   ├── bootstrap/
│   │   ├── node.certificate.pem
│   │   └── node.key.pem
│   ├── validator1/
│   ├── validator2/
│   └── validator3/
├── data/                        # Blockchain data (created at runtime)
├── .env                         # UNIFIED - merge node-config/.env + existing
├── docker-compose.yml           # Base stack (updated paths)
├── services/                    # Cloned repos (unchanged)
├── shardctl/                    # CLI tool
├── run.sh                       # Wrapper script (moved from node-config/)
├── services.yml                 # Config file
└── README.md
```

---

## Migration Steps

### Step 1: Move Static Files

```bash
# From system-integration/ directory

# 1. Move compose files
mkdir -p compose
mv node-config/compose/*.yml compose/

# 2. Move config files
mv node-config/conf conf

# 3. Move genesis files
mv node-config/genesis genesis

# 4. Move certificates
mv node-config/certs certs

# 5. Move run.sh
mv node-config/run.sh run.sh

# 6. Merge .env files (manual step - see below)
```

### Step 2: Merge .env Files

**Current .env files:**
- `.env.embers` - Embers API config
- `.env.f1r3sky` - F1R3Sky config
- `node-config/.env` - Node credentials

**Target:** Single `.env` file with all variables

```bash
# Create unified .env
cat node-config/.env > .env
echo "" >> .env
echo "# === EMBERS CONFIG ===" >> .env
cat .env.embers >> .env
echo "" >> .env
echo "# === F1R3SKY CONFIG ===" >> .env
cat .env.f1r3sky >> .env

# Remove old files
rm .env.embers .env.f1r3sky node-config/.env
```

### Step 3: Update Compose File Paths

All compose files need path updates:

| Old Path (in node-config/compose/) | New Path (in compose/) |
| ---------------------------------- | ---------------------- |
| `../conf/`                         | `./conf/`              |
| `../genesis/`                      | `./genesis/`           |
| `../certs/`                        | `./certs/`             |
| `../data/`                         | `./data/`              |

**Example change in `compose/scala-shard.yml`:**
```yaml
# BEFORE (relative to node-config/compose/)
volumes:
  - ../conf/bootstrap-ceremony.conf:/var/lib/rnode/rnode.conf
  - ../genesis/wallets.txt:/var/lib/rnode/genesis/wallets.txt
  - ../certs/bootstrap/node.certificate.pem:/var/lib/rnode/node.certificate.pem

# AFTER (relative to system-integration/)
volumes:
  - ./conf/bootstrap-ceremony.conf:/var/lib/rnode/rnode.conf
  - ./genesis/wallets.txt:/var/lib/rnode/genesis/wallets.txt
  - ./certs/bootstrap/node.certificate.pem:/var/lib/rnode/node.certificate.pem
```

### Step 4: Update docker-compose.yml (Base Stack)

The existing `docker-compose.yml` expects `./services/f1r3node/docker/...`. Update to use new paths:

```yaml
# BEFORE
volumes:
  - ./services/f1r3node/docker/conf/bootstrap-ceremony.conf:/var/lib/rnode/rnode.conf
  - ./services/f1r3node/docker/genesis/wallets.txt:/var/lib/rnode/genesis/wallets.txt
  - ./services/f1r3node/docker/certs/bootstrap/node.certificate.pem:/var/lib/rnode/node.certificate.pem

# AFTER
volumes:
  - ./conf/bootstrap-ceremony.conf:/var/lib/rnode/rnode.conf
  - ./genesis/wallets.txt:/var/lib/rnode/genesis/wallets.txt
  - ./certs/bootstrap/node.certificate.pem:/var/lib/rnode/node.certificate.pem
```

### Step 5: Update run.sh

Update paths in `run.sh`:

```bash
# BEFORE (when in node-config/)
COMPOSE_FILE="compose/scala-standalone.yml"
docker-compose --env-file .env -f "$COMPOSE_FILE" up -d

# AFTER (when in system-integration/)
COMPOSE_FILE="compose/scala-standalone.yml"
docker-compose --env-file .env -f "$COMPOSE_FILE" up -d
# No change needed if already using relative paths!
```

**Key change:** Update the `determine_compose_file` function path prefix:
```bash
# BEFORE
echo "compose/scala-standalone.yml"

# AFTER - same! (just need to ensure working directory is correct)
```

### Step 6: Rename Legacy Compose Files (Optional)

For consistency, rename old compose files:

```bash
mv docker-compose.yml docker-compose.yml.bak  # Keep as backup initially
mv docker-compose.embers.yml compose/embers.yml
mv docker-compose.f1r3sky.yml compose/f1r3sky.yml
```

Or keep them in root and update paths only.

### Step 7: Update services.yml

Update `startup_order` to reference new locations:

```yaml
# BEFORE
startup_order:
  - docker-compose.yml
  - docker-compose.embers.yml
  - docker-compose.f1r3sky.yml

# AFTER (if moved to compose/)
startup_order:
  - compose/docker-compose.yml
  - compose/embers.yml
  - compose/f1r3sky.yml

# OR keep in root (less change)
startup_order:
  - docker-compose.yml
  - docker-compose.embers.yml
  - docker-compose.f1r3sky.yml
```

### Step 8: Clean Up

```bash
# Remove empty node-config directory
rm -r node-config/

# Update .gitignore
echo "data/" >> .gitignore  # Blockchain data should not be committed
```

---

## Files to Modify

| File                           | Change                                      |
| ------------------------------ | ------------------------------------------- |
| `compose/scala-standalone.yml` | Change `../conf/` → `./conf/` etc.          |
| `compose/scala-shard.yml`      | Change `../conf/` → `./conf/` etc.          |
| `compose/rust-standalone.yml`  | Change `../conf/` → `./conf/` etc.          |
| `compose/rust-shard.yml`       | Change `../conf/` → `./conf/` etc.          |
| `compose/scala-observer.yml`   | Change `../conf/` → `./conf/` etc.          |
| `compose/rust-observer.yml`    | Change `../conf/` → `./conf/` etc.          |
| `compose/scala-validator4.yml` | Change `../conf/` → `./conf/` etc.          |
| `compose/rust-validator4.yml`  | Change `../conf/` → `./conf/` etc.          |
| `docker-compose.yml`           | Change `./services/f1r3node/docker/` → `./` |
| `docker-compose.embers.yml`    | No path changes (uses Docker Hub images)    |
| `docker-compose.f1r3sky.yml`   | No path changes (uses Docker Hub images)    |
| `run.sh`                       | Update `SCRIPT_DIR` handling                |
| `services.yml`                 | Update `startup_order` if needed            |
| `.gitignore`                   | Add `data/`                                 |
| `README.md`                    | Update documentation                        |

---

## Verification Checklist

After migration, verify:

- [ ] `./run.sh --scala --standalone` starts correctly
- [ ] `./run.sh --scala --shard` starts correctly
- [ ] `./run.sh --rust --standalone` starts correctly
- [ ] `./run.sh --rust --shard` starts correctly
- [ ] `docker-compose up` (base stack) starts correctly
- [ ] `shardctl up` works with updated paths
- [ ] All volume mounts resolve correctly
- [ ] `.env` variables are loaded properly
- [ ] `data/` directory is created at runtime
- [ ] `./run.sh reset -y` cleans data correctly

---

## Rollback Plan

Keep backups during migration:

```bash
# Before starting
cp -r node-config node-config.bak
cp docker-compose.yml docker-compose.yml.bak
cp .env.embers .env.embers.bak
cp .env.f1r3sky .env.f1r3sky.bak
```

If issues occur:
```bash
# Restore
mv node-config.bak node-config
mv docker-compose.yml.bak docker-compose.yml
mv .env.embers.bak .env.embers
mv .env.f1r3sky.bak .env.f1r3sky
```

---

## Decision Points

1. **Keep root compose files or move to compose/?**
   - Option A: Keep `docker-compose.yml`, `docker-compose.embers.yml`, `docker-compose.f1r3sky.yml` in root
   - Option B: Move all to `compose/` for consistency
   - **Recommendation:** Keep in root for now (less disruption), move later if desired

2. **Merge .env or keep separate?**
   - Option A: Single `.env` with all variables
   - Option B: Keep `.env`, `.env.embers`, `.env.f1r3sky` separate
   - **Recommendation:** Merge into single `.env` (simpler, less confusion)

3. **Keep monitoring configs?**
   - Current `docker-compose.yml` has prometheus/grafana with paths to `./services/f1r3node/docker/monitoring/`
   - Either: Move monitoring configs too, OR remove from base compose (use separate monitoring compose)
   - **Recommendation:** Move monitoring configs to `./monitoring/` or keep them in services repo

# Docker Compose Structure

This repository uses a modular compose file structure, with services separated by logical stacks.

## Files

### docker-compose.yml (F1R3node Stack)
**Purpose:** Core blockchain infrastructure
**Services:**
- boot, validator1-3, readonly (F1R3FLY blockchain nodes)
- prometheus, grafana (monitoring)

**Creates:** `f1r3fly` network

### docker-compose.f1r3sky.yml (AT Protocol Stack)  
**Purpose:** AT Protocol social media services
**Services:**
- postgres, redis (infrastructure)
- bsky, pds, bsync, ozone (AT Protocol services)
- f1r3sky (frontend web app)

**Requires:** `f1r3fly` network (external)
**Volumes:** postgres_data, redis_data, pds_blocks, pds_tmp, pds_data

### docker-compose.embers.yml (Embers Stack)
**Purpose:** Blockchain API bridge and UI
**Services:**
- embers-api (Rust API bridging f1r3sky to f1r3node)
- embers-frontend (React 19 web UI)

**Requires:** `f1r3fly` network (external)

## Usage

```bash
# Start everything (via shardctl)
poetry run shardctl up

# Start specific stacks
docker-compose up                                          # Just blockchain
docker-compose -f docker-compose.f1r3sky.yml up           # Just AT Protocol
docker-compose -f docker-compose.embers.yml up            # Just Embers

# Combine stacks manually
docker-compose -f docker-compose.yml -f docker-compose.embers.yml up
```

## Network
All services communicate via the `f1r3fly` bridge network, created by docker-compose.yml.

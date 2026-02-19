# Docker Compose Structure

This repository uses a modular compose file structure. Each service has its own compose file in the `compose/` directory and is managed by `shardctl`.

## Files

### compose/f1r3node.yml (F1R3node Scala Shard)
**Purpose:** Core blockchain infrastructure (Scala, multi-node)
**Services:** boot, validator1-3, readonly (F1R3FLY blockchain nodes)
**Creates:** `f1r3fly` network

### compose/f1r3node-rust.yml (F1R3node Rust Shard)
**Purpose:** Core blockchain infrastructure (Rust, multi-node)
**Services:** boot, validator1-3, readonly

### compose/f1r3sky.yml (AT Protocol Stack)
**Purpose:** AT Protocol social media services
**Services:**
- postgres, redis (infrastructure)
- bsky, pds, bsync, ozone (AT Protocol services)
- f1r3sky (frontend web app)

**Requires:** `f1r3fly` network (external)
**Volumes:** postgres_data, redis_data, pds_blocks, pds_tmp, pds_data

### compose/embers.yml (Embers Stack)
**Purpose:** Blockchain API bridge and UI
**Services:**
- embers (Rust API bridging f1r3sky to f1r3node)
- embers-frontend (React 19 web UI)

**Requires:** `f1r3fly` network (external)

### compose/monitoring.yml (Monitoring Stack)
**Purpose:** Metrics and dashboards
**Services:** prometheus, grafana

### Other node configurations
| File | Description |
|------|-------------|
| `compose/f1r3node-standalone.yml` | Single Scala node |
| `compose/f1r3node-rust-standalone.yml` | Single Rust node |
| `compose/f1r3node-observer.yml` | Scala observer node |
| `compose/f1r3node-rust-observer.yml` | Rust observer node |
| `compose/f1r3node-validator4.yml` | 4th Scala validator |
| `compose/f1r3node-rust-validator4.yml` | 4th Rust validator |

## Usage

```bash
# Start everything (via shardctl, uses startup_order from services.yml)
poetry run shardctl up

# Start specific services
poetry run shardctl up f1r3node           # Scala shard (default)
poetry run shardctl up f1r3node-rust      # Rust shard
poetry run shardctl up f1r3node-standalone # Single Scala node
poetry run shardctl up embers             # Embers API + frontend
poetry run shardctl up f1r3sky            # AT Protocol services
poetry run shardctl up monitoring         # Prometheus + Grafana

# Multiple services
poetry run shardctl up f1r3node embers

# Stop services
poetry run shardctl down                  # Stop all (reverse order)
poetry run shardctl down f1r3node         # Stop specific service
```

## Network
All services communicate via the `f1r3fly` bridge network, created by the f1r3node compose file.

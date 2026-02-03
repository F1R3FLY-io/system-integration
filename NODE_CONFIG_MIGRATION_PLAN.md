# Migration Plan: `node-config/run.sh` into `shardctl`

## Overview

Migrate the F1R3FLY node-specific Docker management from `node-config/run.sh` into `shardctl` as a new subcommand group `shardctl node`.

## Current State

### `node-config/` Structure
```
node-config/
  run.sh              # Main wrapper script (to be migrated)
  compose/            # 8 compose files (scala/rust x standalone/shard/observer/validator4)
  conf/               # Node configuration files
  certs/              # Node certificates (bootstrap, validator1-3)
  genesis/            # Genesis files (bonds.txt, wallets.txt)
  .env                # Node keys and credentials
  README.md           # Documentation
```

### `run.sh` Commands
| Command  | Description                                            |
| -------- | ------------------------------------------------------ |
| `up`     | Start containers (with node type + topology selection) |
| `down`   | Stop containers (auto-detects running config)          |
| `logs`   | Follow container logs                                  |
| `status` | Show container status                                  |
| `reset`  | Stop + delete blockchain data                          |
| `wait`   | Poll for "Running state" readiness                     |
| `pull`   | Pull latest images                                     |

### `shardctl` Commands (existing)
| Command           | Description                       |
| ----------------- | --------------------------------- |
| `up`              | Start services from compose files |
| `down`            | Stop services                     |
| `logs`            | View logs                         |
| `ps` / `status`   | List/show status                  |
| `pull`            | Pull images                       |
| `build`           | Build images                      |
| `clone` / `setup` | Clone service repos               |
| `build-service`   | Build from source                 |
| `exec` / `shell`  | Run commands in containers        |

---

## Migration Approach

### Option A: Subcommand Group `shardctl node`
Create dedicated node management commands under a `node` subcommand:

```bash
shardctl node up --scala --standalone
shardctl node up --rust --shard
shardctl node down
shardctl node wait
shardctl node reset [-y]
shardctl node logs [service]
shardctl node status
shardctl node pull
```

**Pros:** Clean separation, node-specific logic isolated
**Cons:** Different from existing `shardctl up/down` pattern

### Option B: Integrate into Existing Commands with Flags
Add node-specific flags to existing commands:

```bash
shardctl up --node-type scala --topology standalone
shardctl down                    # Auto-detect or --node-type
shardctl wait                    # New command (generic)
shardctl reset [-y]              # New command (generic)
```

**Pros:** Unified interface, reusable reset/wait for all services
**Cons:** More complex flag handling

### Recommendation: **Hybrid Approach**
1. Add `reset` and `wait` as **generic** shardctl commands
2. Add **node-specific compose file selection** to existing commands via `--node-type` and `--topology` flags
3. Keep node configs in `node-config/` but reference them from shardctl

---

## Implementation Plan

### Phase 1: Add Generic Commands (reset, wait)

#### 1.1 Add `shardctl reset` command
```python
@app.command()
def reset(
    volumes: bool = typer.Option(False, "--volumes", "-v", help="Also remove named volumes"),
    data_dirs: Optional[List[str]] = typer.Option(None, "--data-dir", "-d", help="Data directories to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Stop services and delete persistent data."""
    # 1. Run `down --volumes` if volumes flag
    # 2. Delete specified data directories (with confirmation unless -y)
```

#### 1.2 Add `shardctl wait` command
```python
@app.command()
def wait(
    services: Optional[List[str]] = typer.Argument(None, help="Services to wait for"),
    timeout: int = typer.Option(300, "--timeout", "-t", help="Timeout in seconds"),
    ready_pattern: str = typer.Option("healthy", "--pattern", help="Log pattern indicating readiness"),
):
    """Wait for services to be ready by polling logs."""
    # 1. Determine containers to check
    # 2. Poll logs for ready_pattern
    # 3. Report timing per service
```

### Phase 2: Add Node Configuration Support

#### 2.1 Update `services.yml` with node configurations
```yaml
# Add new section for node configurations
node_configs:
  scala-standalone:
    compose_file: node-config/compose/scala-standalone.yml
    env_file: node-config/.env
    data_dir: node-config/data
    ready_pattern: "Making a transition to Running state"
    
  scala-shard:
    compose_file: node-config/compose/scala-shard.yml
    env_file: node-config/.env
    data_dir: node-config/data
    ready_pattern: "Making a transition to Running state"
    services:
      - boot
      - validator1
      - validator2
      - validator3
      - readonly
    
  rust-standalone:
    compose_file: node-config/compose/rust-standalone.yml
    env_file: node-config/.env
    data_dir: node-config/data
    ready_pattern: "Making a transition to Running state"
    
  rust-shard:
    compose_file: node-config/compose/rust-shard.yml
    env_file: node-config/.env
    data_dir: node-config/data
    ready_pattern: "Making a transition to Running state"
    services:
      - boot
      - validator1
      - validator2
      - validator3
      - readonly
```

#### 2.2 Add node flags to commands
```python
# In cli.py, add to up/down/logs/status/reset/wait:

node_type: Optional[str] = typer.Option(None, "--node-type", help="Node type: scala or rust")
topology: Optional[str] = typer.Option(None, "--topology", help="Topology: standalone or shard")
node_config: Optional[str] = typer.Option(None, "--node-config", help="Node config name from services.yml")
```

#### 2.3 Update `Config` class
```python
# In config.py
def get_node_config(self, name: str) -> Optional[dict]:
    """Get node configuration by name."""
    return self.config.get("node_configs", {}).get(name)

def get_node_compose_file(self, node_type: str, topology: str) -> Path:
    """Get compose file path for node type and topology."""
    config_name = f"{node_type}-{topology}"
    node_config = self.get_node_config(config_name)
    if node_config:
        return self.root_dir / node_config["compose_file"]
    raise ValueError(f"Unknown node configuration: {config_name}")
```

### Phase 3: Interactive Mode

#### 3.1 Add interactive selection
```python
@app.command()
def node(
    action: str = typer.Argument("up", help="Action: up, down, logs, status, wait, reset"),
):
    """Interactive node management (prompts for node type and topology)."""
    # If no flags provided, prompt interactively
    # Then delegate to appropriate command
```

Or add `--interactive` flag to `up`:
```bash
shardctl up --interactive  # Prompts for node type and topology
```

### Phase 4: Auto-Detection for down/logs/status

#### 4.1 Implement running config detection
```python
def detect_running_node_config() -> Optional[str]:
    """Detect which node configuration is currently running."""
    # Check for container names: rnode.standalone, rnode.bootstrap
    # Inspect image to determine scala vs rust
    # Return config name like "scala-shard" or None
```

---

## File Changes Summary

### New Files
- None (all changes in existing files)

### Modified Files
| File                  | Changes                                                                 |
| --------------------- | ----------------------------------------------------------------------- |
| `shardctl/cli.py`     | Add `reset`, `wait` commands; add node flags to existing commands       |
| `shardctl/config.py`  | Add `get_node_config()`, `get_node_compose_file()` methods              |
| `shardctl/compose.py` | Add support for env_file, data_dir from node configs                    |
| `shardctl/utils.py`   | Add `wait_for_ready()`, `delete_data_dirs()`, `detect_running_config()` |
| `services.yml`        | Add `node_configs` section                                              |

### Files to Remove (after migration)
- `node-config/run.sh` (functionality moved to shardctl)

### Files to Keep
- `node-config/compose/*.yml` - Referenced by shardctl
- `node-config/conf/*` - Node configurations
- `node-config/certs/*` - Certificates
- `node-config/genesis/*` - Genesis files
- `node-config/.env` - Node credentials
- `node-config/README.md` - Update to reference shardctl commands

---

## Command Mapping (After Migration)

| Old (`run.sh`)                  | New (`shardctl`)                                      |
| ------------------------------- | ----------------------------------------------------- |
| `./run.sh`                      | `shardctl up --interactive` or `shardctl node`        |
| `./run.sh --default`            | `shardctl up --node-config scala-shard`               |
| `./run.sh --scala --standalone` | `shardctl up --node-type scala --topology standalone` |
| `./run.sh --rust --shard`       | `shardctl up --node-type rust --topology shard`       |
| `./run.sh down`                 | `shardctl down` (auto-detect)                         |
| `./run.sh logs`                 | `shardctl logs`                                       |
| `./run.sh status`               | `shardctl status`                                     |
| `./run.sh wait`                 | `shardctl wait`                                       |
| `./run.sh reset`                | `shardctl reset --data-dir node-config/data`          |
| `./run.sh reset -y`             | `shardctl reset --data-dir node-config/data -y`       |
| `./run.sh pull`                 | `shardctl pull`                                       |

---

## Testing Plan

1. **Unit tests** for new functions in utils.py
2. **Integration tests**:
   - `shardctl up --node-type scala --topology standalone` starts correct container
   - `shardctl wait` correctly detects readiness
   - `shardctl reset -y` deletes data directory
   - Auto-detection works for down/logs/status
3. **Manual testing**:
   - Full workflow: up -> wait -> deploy -> reset
   - Both Scala and Rust variants
   - Both standalone and shard topologies

---

## Timeline Estimate

| Phase     | Tasks                     | Effort          |
| --------- | ------------------------- | --------------- |
| Phase 1   | Add reset + wait commands | 2-3 hours       |
| Phase 2   | Node config support       | 3-4 hours       |
| Phase 3   | Interactive mode          | 1-2 hours       |
| Phase 4   | Auto-detection            | 2-3 hours       |
| Testing   | Manual + automated        | 2-3 hours       |
| **Total** |                           | **10-15 hours** |

---

## Questions to Resolve

1. Should `--node-type`/`--topology` flags work with all compose commands, or only a subset?
2. Should we support multiple node networks running simultaneously (different ports)?
3. Should `reset` require explicit data-dir or auto-detect from running config?
4. Keep `.env` separate or merge into main project `.env` files?

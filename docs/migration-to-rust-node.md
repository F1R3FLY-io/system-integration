# Migration: Scala Node to Rust-Only f1r3node

## Overview

This document describes the migration from the dual Scala/Rust f1r3node setup to a Rust-only implementation using the standalone `f1r3node-rust` repository.

### Current State

- **f1r3node** (Scala): `github.com:F1R3FLY-io/f1r3node.git` branch `dev`
  - Built with SBT + Nix, produces `f1r3flyindustries/f1r3fly-scala-node:latest`
  - Default node type in shardctl (`--scala` is implicit)
- **f1r3node-rust** (Rust branch of Scala repo): `github.com:F1R3FLY-io/f1r3node.git` branch `rust/dev`
  - Built with Cargo inside Nix shell, produces `f1r3flyindustries/f1r3fly-rust-node:latest`
  - Selected via `--rust` flag in shardctl

### Target State

- **f1r3node** points to the standalone Rust repo: `github.com:F1R3FLY-io/f1r3node-rust.git` branch `dev`
  - Built with Cargo (no Nix required), produces `f1r3flyindustries/f1r3fly-rust-node:latest`
  - Default and only node type — `--scala`/`--rust` flags removed
  - Compose files sourced from the upstream f1r3node-rust repository
  - Scala node support fully removed from shardctl

## Compatibility Assessment

### Verified Compatible

| Component | Status | Notes |
|-----------|--------|-------|
| TLS certificates | Identical | Same certs in both repos |
| bonds.txt | Identical | Same 3 validators with 1000 bonds each |
| Private keys | Match | Same validator identities across repos |
| Network ID | Match | Both use `testnet` |
| Port layout | Match | 40400-40405 per node (6 ports) |
| gRPC API | Compatible | Same protobuf service definitions |
| HTTP API | Compatible | Same endpoints on port 40403 |
| Config format | Compatible | Both use HOCON |

### Must Fix Before Migration

| Issue | Severity | Action |
|-------|----------|--------|
| wallets.txt mismatch | **Critical** | f1r3node-rust has 8 lines (50T for validator3), system-integration has 20 lines (500T for validator3 + 12 additional wallets). Must sync to system-integration version. |
| Compose env var defaults | High | f1r3node-rust shard.yml uses bare `$VAR` without defaults. system-integration uses `${VAR:-default}`. Need to add defaults or ensure .env is always present. |
| Network name | Medium | f1r3node-rust uses `f1r3fly`, system-integration uses `f1r3fly-shard`. Must standardize. |

## Migration Phases

### Phase 1: Align Genesis State (f1r3node-rust repo)

**Goal:** Fix the wallets.txt discrepancy in the upstream f1r3node-rust repo.

**Changes (in f1r3node-rust repo):**
1. Replace `docker/genesis/wallets.txt` with the 20-line version from system-integration
2. Verify standalone genesis files also match
3. PR and merge to f1r3node-rust `dev` branch

**Validation:**
- Start a shard with the updated wallets.txt
- Confirm genesis ceremony completes successfully
- Verify all 20 wallet addresses have correct balances

### Phase 2: Switch Repository Source (system-integration)

**Goal:** Point `services.yml` at the standalone f1r3node-rust repo and update build config.

**Changes to `services.yml`:**
```yaml
repositories:
  f1r3node:
    url: git@github.com:F1R3FLY-io/f1r3node-rust.git
    branch: dev

builds:
  f1r3node:
    build_command: "cargo build --release -p node"
    docker_build_command: "./node/docker-commands.sh build-local"
    docker_image: "f1r3flyindustries/f1r3fly-rust-node:latest"
    dependencies:
      - "cargo"
      - "rustc"
      - "protobuf-compiler"
      - "libssl-dev"
      - "pkg-config"
      - "libclang-dev"
```

**Key differences from current config:**
- `environment: "nix"` removed (f1r3node-rust builds with standard Cargo)
- `docker_pre_build_steps` removed (no `rustup default stable` needed)
- Dependencies updated to reflect actual system requirements
- No separate `f1r3node-rust` entry — just one `f1r3node`

**Removed entries:**
- `f1r3node-rust` repository entry (merged into `f1r3node`)
- `f1r3node` Scala build config
- `f1r3node-rust` build config (replaced by new `f1r3node`)

### Phase 3: Replace Compose Files (system-integration)

**Goal:** Use f1r3node-rust's compose files as the source of truth.

**Compose file mapping:**

| Current file | Source from f1r3node-rust | New name |
|---|---|---|
| `compose/f1r3node.yml` (Scala shard) | `docker/shard.yml` | `compose/f1r3node.yml` |
| `compose/f1r3node-standalone.yml` (Scala) | `docker/standalone.yml` | `compose/f1r3node-standalone.yml` |
| `compose/f1r3node-observer.yml` (Scala) | `docker/observer.yml` | `compose/f1r3node-observer.yml` |
| `compose/f1r3node-validator4.yml` (Scala) | `docker/validator4.yml` | `compose/f1r3node-validator4.yml` |
| `compose/f1r3node-rust.yml` | Removed | — |
| `compose/f1r3node-rust-standalone.yml` | Removed | — |
| `compose/f1r3node-rust-observer.yml` | Removed | — |
| `compose/f1r3node-rust-validator4.yml` | Removed | — |
| `compose/f1r3node-shard-light.yml` (Scala) | Create from shard.yml | `compose/f1r3node-shard-light.yml` |

**Adaptations required when copying from upstream:**
1. Change relative paths from `./conf/` to `../conf/` (or copy upstream conf into system-integration)
2. Add `${VAR:-default}` env var defaults to compose files
3. Standardize network name to `f1r3fly-shard` (matching existing convention)
4. Image variable: `${F1R3FLY_IMAGE:-f1r3flyindustries/f1r3fly-rust-node:latest}` (rename from `F1R3FLY_RUST_IMAGE`)
5. Remove monitoring services from shard.yml (system-integration has `compose/monitoring.yml`)

**Config file updates:**
- Replace monolithic `conf/bootstrap-ceremony.conf` and `conf/shared-rnode.conf` with the DRY structure from f1r3node-rust (`default.conf` + `bootstrap.conf`)
- Remove `conf/logback.xml` (Scala-only, Rust doesn't use it)
- Keep `conf/observer.conf` and `conf/standalone-dev.conf`

### Phase 4: Simplify shardctl (system-integration)

**Goal:** Remove Scala/Rust duality from the CLI.

**Changes to `shardctl/node.py`:**
- Remove `NodeType` enum entirely (only one node type now)
- Remove `--scala`/`--rust`/`--node-type` flags from `up()` command
- Remove `_detect_node_type_from_container()` method
- Simplify `get_compose_file()` to take only `Topology`
- Rename `F1R3FLY_RUST_IMAGE` to `F1R3FLY_IMAGE` across compose files and env

**Changes to `shardctl/cli.py`:**
- Remove `--rust`/`--scala` options from test command
- Remove `DEFAULT_IMAGE` env var logic (only one image)
- Remove interactive node type selection prompts

**Changes to integration tests:**
- Remove `docker-compose.rust.yml` / `docker-compose.standalone-rust.yml` (keep Scala-named ones, update contents to Rust)
- Or rename to just `docker-compose.yml` / `docker-compose.standalone.yml`
- Update `conftest.py` `DEFAULT_IMAGE` to `f1r3flyindustries/f1r3fly-rust-node:latest`

### Phase 5: Update Documentation (system-integration)

**Files to update:**
- `README.md` — Remove all Scala references, simplify node commands table
- `CLAUDE.md` — Update repository structure, remove Scala mentions
- `COMPOSE_STRUCTURE.md` — Remove Rust-prefixed compose file entries
- `integration-tests/README.md` — Update image references and test commands
- `ci/CLAUDE.md` — Update runner descriptions (Scala runners may be repurposed)

### Phase 6: Clean Up

**Remove:**
- All `compose/f1r3node-rust-*.yml` files
- All Scala-specific compose files (`compose/f1r3node.yml` before replacement)
- `conf/logback.xml`
- Scala CI runner setup script (`ci/setup-f1r3node-scala-runner.sh`)
- Any Scala-specific build configs

**Verify:**
- `shardctl clone` clones from f1r3node-rust repo
- `shardctl build-service f1r3node --docker-only` builds the Rust image
- `shardctl up` starts the Rust shard
- `shardctl test` runs integration tests against Rust image
- `git push` pre-push hook passes

## Rollback Plan

If the Rust node has blocking issues during migration:

1. Revert `services.yml` to point back to Scala repo
2. Restore Scala compose files from git history
3. Re-add `NodeType` enum to shardctl

The migration is fully reversible at any phase since each phase is a separate commit/PR.

## Timeline Recommendation

| Phase | Effort | Dependency |
|-------|--------|------------|
| Phase 1: Align genesis | Small | Must complete first (upstream PR to f1r3node-rust) |
| Phase 2: Switch repo | Small | Phase 1 merged |
| Phase 3: Replace compose | Medium | Phase 2 |
| Phase 4: Simplify shardctl | Medium | Phase 3 |
| Phase 5: Update docs | Small | Phase 4 |
| Phase 6: Clean up | Small | Phase 5 |

Phases 2-6 can be done in a single PR or split across multiple PRs depending on review preference.

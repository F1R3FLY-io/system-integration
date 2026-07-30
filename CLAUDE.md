# F1R3FLY System Integration

## Project Overview

This is a microservices integration repository for the F1R3FLY blockchain ecosystem. It provides tooling and orchestration for managing multiple service repositories (f1r3node, f1r3sky, embers, etc.) as independent nested git repositories with docker-compose coordination.

## Key Concepts

- **Services are git-ignored**: Each service repository (f1r3node, embers, f1r3sky-backend, etc.) is cloned into `services/` and completely ignored by the parent system-integration repository
- **Independent development**: Work in each service directory normally with full git functionality - changes are isolated to each service repo
- **Docker Compose orchestration**: Each service has its own compose file in `compose/` directory
- **shardctl CLI**: Python CLI tool (installed via Poetry) that wraps docker-compose operations and service management

## Getting Started

**IMPORTANT: Read [README.md](./README.md) first** for prerequisites + Quick Start. Deeper docs live in dedicated files:

- [docs/setup.md](docs/setup.md) — full multi-service setup + per-service build deps
- [COMPOSE_STRUCTURE.md](COMPOSE_STRUCTURE.md) — topologies, image selection, project naming, network model, port map, monitoring
- [docs/cli-reference.md](docs/cli-reference.md) — every `shardctl` command + flag
- [docs/configuration.md](docs/configuration.md) — node configs (`conf/`) + env files
- [docs/consensus-configuration.md](docs/consensus-configuration.md) — FTT, synchrony, finalization semantics
- [docs/troubleshooting.md](docs/troubleshooting.md) — common issues
- [docs/development.md](docs/development.md) — development workflow

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

## Repository Structure

```
.
├── services/                    # Service repos (git-ignored)
│   ├── f1r3node/               # F1R3FLY blockchain node (Scala)
│   ├── f1r3node-rust/          # F1R3FLY blockchain node (Rust)
│   ├── rust-client/            # Rust CLI client
│   ├── f1r3drive/              # F1r3Drive FUSE app (Java)
│   ├── embers/                 # Embers API (Rust, opt-in)
│   ├── embers-frontend/        # Embers UI (React 19, opt-in)
│   └── f1r3sky-backend/        # AT Protocol backend (Node.js, opt-in)
├── shardctl/                   # CLI tool package
├── compose/                    # Docker Compose files (one per service)
│   ├── f1r3node.yml            # Scala shard
│   ├── f1r3node-rust.yml       # Rust shard (default)
│   ├── embers.yml              # Embers API + frontend
│   ├── f1r3sky.yml             # F1R3Sky AT Protocol services
│   └── monitoring.yml          # Prometheus + Grafana
├── docs/                       # Documentation
│   ├── setup.md                # Full setup walkthrough + per-service build deps
│   ├── cli-reference.md        # Every shardctl command + flag
│   ├── configuration.md        # Node configs + env files
│   ├── consensus-configuration.md  # FTT / synchrony / finalization semantics
│   ├── troubleshooting.md      # Common issues
│   ├── development.md          # Development workflow + advanced usage
│   ├── slashing-mechanism.md   # Slashing summary
│   ├── slashing-test-plan.md   # Slashing test rewrite plan
│   └── f1r3drive-guide.md      # F1R3Drive FUSE app
├── COMPOSE_STRUCTURE.md        # Canonical compose reference
├── services.yml                # Service repository URLs and branches
├── .env.node                   # Node container hostnames + validator keys
├── .env.embers                 # Embers configuration
├── .env.f1r3sky                # F1R3Sky configuration
└── README.md                   # Welcome + Quick Start + pointers
```

## Service Repositories

Services are defined in `services.yml` with their git URLs and branches.
Default-enabled (cloned by `shardctl clone`):
- **f1r3node**: Scala blockchain node (`dev` branch)
- **f1r3node-rust**: Rust blockchain node (`staging` branch)
- **rust-client**: CLI tool for blockchain interaction (`dev` branch)
- **f1r3drive**: F1r3Drive FUSE app (`dev` branch)

Opt-in (`enabled: false`; clone with `--include-disabled` or by name):
- **embers**: Blockchain API bridge (`main` branch)
- **embers-frontend**: Web UI for embers (`main` branch)
- **f1r3sky-backend**: AT Protocol services (`main` branch)
- **f1r3sky**: F1R3Sky frontend (`main` branch)

## Important Notes

1. **Never commit service directories** - they're independent git repos
2. **Use `shardctl clone`** to set up service repositories with the correct branches
3. **Each compose file is independent** - start only what you need:
   - `compose/f1r3node.yml` - F1R3node Scala shard
   - `compose/f1r3node-rust.yml` - F1R3node Rust shard (default)
   - `compose/f1r3node-shard-light.yml` - Lightweight 2-validator Scala shard (~7.5 GB RAM)
   - `compose/embers.yml` - Embers API and frontend
   - `compose/f1r3sky.yml` - F1R3Sky AT Protocol services
   - `compose/monitoring.yml` - Prometheus + Grafana

   See [COMPOSE_STRUCTURE.md](COMPOSE_STRUCTURE.md) for the full list (standalone, observer, validator4 variants).
4. **Services communicate via Docker network** - `f1r3fly` network
5. **Always use shardctl commands** - Don't run builds manually (cargo, sbt, etc.). Use:
   - `poetry run shardctl build-service <service>` for full builds (source + Docker)
   - `poetry run shardctl build-service <service> --docker-only` for Docker image only
   - `poetry run shardctl build-service <service> --no-docker` for source build only
   - `poetry run shardctl build-service --list` to see available services
6. **README.md is a thin entry point** — for any specific topic, follow the link from the "Where to go next" table to the dedicated doc
7. **CWD discipline for service-tree commands** — every `cargo`/`sbt`/`pytest` command must be run from inside the right service worktree (e.g. `services/f1r3node-rust-pr3/`). The Bash-tool cwd does NOT reliably persist between calls, so:
   - **Always start the command with `cd <absolute path> && <cmd>`**, even when the previous call appeared to land you there.
   - When working in a git worktree (e.g. `f1r3node-rust-pr1`, `f1r3node-rust-pr2`, `f1r3node-rust-pr3`), use the worktree's absolute path, not `services/f1r3node-rust/`.
   - If `cargo` errors with `could not find Cargo.toml in [WORKSPACE_ROOT]/system-integration`, the cwd silently reverted — re-issue the command with explicit `cd`.
   - For background commands launched with `run_in_background: true`, the cwd reset bites especially hard because the process inherits the parent shell's cwd. Always prefix with `cd`.

## Integration Tests

Test framework lives at [integration-tests/](integration-tests/). Canonical invocation is `poetry run pytest`; `shardctl test` is a convenience wrapper that sets `F1R3FLY_NODE_IMAGE` and forwards flags.

- **Run one test:**
  `poetry run pytest integration-tests/test/tests/shared/test_wallets.py::test_validator1_pay_validator2`
- **Iterative debug loop** (skip ~60s shard bring-up between runs):
  ```bash
  poetry run shardctl test --keep-running <suite>
  # Note the "Session <id>" line in output
  poetry run shardctl test --skip-setup --session-id <id> <suite>   # ~2s per iteration
  poetry run shardctl test-reset                                      # when done
  ```
- **Debugging a flake-under-load** (preserve only the failing shard): use `--keep-on-failure` with `-x` instead of `--keep-running`. Passing tests tear down (no host-load accumulation, which is itself a flake source on a constrained host); the first failing test's shard is left up for inspection. `poetry run pytest <suite> -x --keep-on-failure --provider=subprocess` (or `shardctl test --keep-on-failure -a -x`). `shardctl test-reset` when done.
- **Image selection:** `F1R3FLY_NODE_IMAGE` env var (single source of truth). Default `f1r3flyindustries/f1r3fly-rust:latest`.
- **Cleanup:** `shardctl test-reset` force-removes every `rnode.test.*` / `f1r3fly-test-*` / `test-*` resource, running or stopped. Add `--session-id <id>` to scope cleanup to one session — useful when another agent owns concurrent sessions.
- **Docs layout:**
  - [integration-tests/README.md](integration-tests/README.md) — running tests
  - [integration-tests/test/docs/ARCHITECTURE.md](integration-tests/test/docs/ARCHITECTURE.md) — framework internals (fixtures, Provider protocol, cleanup, ports, timeouts)
  - [integration-tests/test/docs/WRITING_TESTS.md](integration-tests/test/docs/WRITING_TESTS.md) — recipes for adding a test
  - [integration-tests/test/docs/INDEX.md](integration-tests/test/docs/INDEX.md) — catalog of all 22 test files

## Git Hooks

Pre-commit and pre-push hooks enforce code quality. Install with:

```bash
./scripts/setup-hooks.sh           # Uses core.hooksPath (recommended)
./scripts/setup-hooks.sh --copy    # Copies to .git/hooks/ (alternative)
./scripts/setup-hooks.sh --status  # Show current configuration
```

- **pre-commit**: ruff lint + format check on staged Python files
- **pre-push**: `unit-tests/` unit tests (no Docker, no shard, sub-second)

Bypass with `--no-verify` (not recommended).

## Collaboration and Standards

### Key Principles

1. **Stigmergic Collaboration**: Coordinate with other agents through shared `.md` files
2. **Document-First**: Create design docs and specifications BEFORE implementation
3. **Signal vs. Slop**: Maximize code that solves problems; avoid over-engineering
4. **Acceptance Criteria**: Define measurable success criteria in task definitions

### Standard Document Structure

| Document | Purpose | Location |
|----------|---------|----------|
| User Stories | Business needs and acceptance criteria | `docs/UserStories.md` |
| Tasks/Epochs | Implementation tracking | `docs/ToDos.md` |
| Completed Work | Historical reference | `docs/CompletedTasks.md` |
| Backlog | Deferred items | `docs/Backlog.md` |
| Work Logs | Session progress | `docs/work-logs/*.md` |
| Discoveries | Shared findings | `docs/discoveries/*.md` |

### Before Starting Work

1. **Read `docs/ToDos.md`** to check task status and claims
2. **Check `docs/work-logs/`** for existing progress on related tasks
3. **Review `docs/discoveries/`** for relevant context from other agents

### When Claiming a Task

Update the task in `docs/ToDos.md`:

```yaml
---
id: TASK-001
status: in_progress          # Changed from 'pending'
claimed_by: claude-session-a1b2c3  # See Implementer Identification format
claimed_at: 2025-01-15T10:00:00Z
# Other valid claimed_by formats:
#   human-jeff@example.com        # Human (git config --get user.email)
#   design-sprint/researcher      # Agent team member ({team}/{name})
---
```

### During Work

1. **Create work log** at `docs/work-logs/task-{id}-{timestamp}.md`
2. **Document discoveries** in `docs/discoveries/` for other agents
3. **Update blockers** if you encounter dependencies

### Before Pausing/Completing

Update your work log with handoff notes:

```yaml
---
handoff_status: ready | paused | blocked
next_steps:
  - What remains to be done
---
```

### Configuration File Conventions

When creating or modifying configuration files, follow these conventions to respect existing project preferences:

**JSON Format Preference Order:**

1. **Check for existing files first**: Before creating any `.json` file, check if `.jsonc` or `.json5` variants exist
2. **Prefer existing format**: If `config.jsonc` or `config.json5` exists, use that format instead of creating `config.json`
3. **Default to JSONC**: When creating new config files, prefer `.jsonc` (JSON with Comments) for better maintainability

**Why This Matters:**
- Projects may have established preferences for comment-supporting JSON formats
- Creating duplicate configs (e.g., both `biome.json` and `biome.jsonc`) causes confusion
- JSONC allows inline documentation which improves maintainability

**Examples:**

| If exists... | Don't create... | Instead... |
|--------------|-----------------|------------|
| `biome.jsonc` | `biome.json` | Edit the existing `biome.jsonc` |
| `tsconfig.json5` | `tsconfig.json` | Edit the existing `tsconfig.json5` |
| `eslint.config.jsonc` | `eslint.config.json` | Edit the existing file |
| Nothing | - | Create new file as `.jsonc` when comments are useful |

**File Discovery Pattern:**

Before creating any config file, check for variants:
```bash
# Check for config variants (example for biome)
ls biome.json biome.jsonc biome.json5 2>/dev/null
```

This applies to all slash commands and scripts that create configuration files.

#### Git Operations
- `/quick-commit` - Stage and commit changes (required in safe mode)
- `/recursive-push` - Push across repositories

#### Task Management
- `/nextTask` - Find and select next task to work on
- `/implement` - Begin implementation of a task
- `/epoch-review` - Preview and summarize epochs
- `/epoch-hygiene` - Archive completed epochs

#### Workspace Sync
- `/harmonize` - Sync workspace policies into this repo
- `/multi-repo-sync` - Workspace-wide sync orchestration

[OPTIONAL_COMMANDS]

### PII Guidelines for Contributors

**CRITICAL - Before submitting any contribution:**

Contributors MUST ensure their code, commits, and documentation do NOT contain PII:

**Check before committing:**
- [ ] No absolute file paths with usernames in code or documentation
- [ ] No personal email addresses in code (use generic examples like `user@example.com`)
- [ ] No real user data in tests or examples (use synthetic/fake data only)
- [ ] No PII in log statements (sanitize or use user IDs instead)
- [ ] No PII in error messages or stack traces
- [ ] No PII in code comments or documentation
- [ ] No credentials, tokens, or secrets in code (use environment variables)
- [ ] No IP addresses, MAC addresses, or device identifiers in examples

**If you accidentally committed PII:**
1. **DO NOT** push to remote repository
2. Use `git reset` to remove the commit
3. If already pushed, contact maintainers immediately
4. Repository history may need to be rewritten to remove PII

**Use these instead:**
- File paths: Use relative paths or generic placeholders (`[WORKSPACE_ROOT]/project/`)
- Email addresses: Use `user@example.com`, `admin@example.com`
- Names: Use `John Doe`, `Jane Smith`, `User123`
- Phone numbers: Use `+1-555-0100` (officially reserved for examples)
- IP addresses: Use reserved ranges (`192.0.2.1`, `198.51.100.1`, `203.0.113.1`)
- Dates: Use recent but generic dates, not specific personal dates

**For test data:**
- Use test data generators that create realistic but fake data
- Use well-known test fixtures (e.g., `test@example.com`)
- Never use production or real user data in development/testing

# important-instruction-reminders
Do what has been asked; nothing more, nothing less.
NEVER create files unless they're absolutely necessary for achieving your goal.
ALWAYS prefer editing an existing file to creating a new one.
NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.
Before making any code changes, first state: (1) which files you plan to modify, (2) what approach you'll take, (3) any assumptions you're making. Wait for my confirmation before proceeding. For simple single-file edits, a one-line summary is sufficient.

## Project Overview

This is a microservices integration repository for the F1R3FLY blockchain ecosystem. It provides tooling and orchestration for managing multiple service repositories (f1r3node, f1r3sky, embers, etc.) as independent nested git repositories with docker-compose coordination.

## Repository Structure

```
.
├── services/                    # Service repos (git-ignored)
│   ├── f1r3node/               # F1R3FLY blockchain node (Scala + Rust)
│   ├── embers/                 # Embers API (Rust)
│   ├── embers-frontend/        # Embers UI (React 19)
│   ├── f1r3sky-backend/        # AT Protocol backend (Node.js)
│   └── rust-client/            # Rust CLI client
├── .github/workflows/          # CI (GitHub Actions)
├── hooks/                      # Git hooks (pre-commit, pre-push)
├── scripts/                    # Setup scripts (setup-hooks.sh)
├── shardctl/                   # CLI tool package
├── compose/                    # Docker Compose files (one per service)
│   ├── f1r3node.yml            # Scala shard (default)
│   ├── f1r3node-rust.yml       # Rust shard
│   ├── embers.yml              # Embers API + frontend
│   ├── f1r3sky.yml             # F1R3Sky AT Protocol services
│   └── monitoring.yml          # Prometheus + Grafana
├── services.yml                # Service repository URLs and branches
├── .env.embers                 # Embers configuration
└── README.md                   # Full documentation
```

# F1R3FLY System Integration

## Key Concepts

- **Services are git-ignored**: Each service repository (f1r3node, embers, f1r3sky-backend, etc.) is cloned into `services/` and completely ignored by the parent system-integration repository
- **Independent development**: Work in each service directory normally with full git functionality - changes are isolated to each service repo
- **Docker Compose orchestration**: Each service has its own compose file in `compose/` directory
- **shardctl CLI**: Python CLI tool (installed via Poetry) that wraps docker-compose operations and service management

## Getting Started

**IMPORTANT: Read [README.md](./README.md) first** - it contains complete documentation on:
- Installation and setup
- How to clone service repositories
- Using the shardctl CLI tool
- Docker compose configuration
- Development workflow
- Troubleshooting

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

## Git Hooks

Pre-commit and pre-push hooks enforce code quality. Install with:

```bash
./scripts/setup-hooks.sh           # Uses core.hooksPath (recommended)
./scripts/setup-hooks.sh --copy    # Copies to .git/hooks/ (alternative)
./scripts/setup-hooks.sh --status  # Show current configuration
```

- **pre-commit**: ruff lint + format, YAML validation on staged files
- **pre-push**: ruff lint + format on full codebase, `test_internal.py` unit tests

Bypass with `--no-verify` (not recommended). Skip individual checks with env vars:
`SKIP_LINT=1`, `SKIP_RUFF=1`, `SKIP_FORMAT=1`, `SKIP_YAML=1`, `SKIP_TESTS=1`, `QUICK=1`.

## CI

GitHub Actions runs the same checks as the git hooks on pushes and PRs to `dev` and `main`:
- **Lint job**: ruff lint + format + YAML validation (runs on `ubuntu-latest`)
- **Test job**: `test_internal.py` unit tests with full integration deps

Workflow: `.github/workflows/ci.yml`

## Service Repositories

Services are defined in `services.yml` with their git URLs and branches:
- **f1r3node**: Blockchain node (main + rust-dev branches)
- **embers**: Blockchain API bridge (main branch)
- **embers-frontend**: Web UI for embers (main branch)
- **f1r3sky-backend**: AT Protocol services (main branch)
- **rust-client**: CLI tool for blockchain interaction (main branch)

## Important Notes

1. **Never commit service directories** - they're independent git repos
2. **Use `shardctl clone`** to set up service repositories with the correct branches
3. **Each compose file is independent** - start only what you need:
   - `compose/f1r3node.yml` - F1R3node Scala shard (default)
   - `compose/f1r3node-rust.yml` - F1R3node Rust shard
   - `compose/embers.yml` - Embers API and frontend
   - `compose/f1r3sky.yml` - F1R3Sky AT Protocol services
   - `compose/monitoring.yml` - Prometheus + Grafana
4. **Services communicate via Docker network** - `f1r3fly` network
5. **Always use shardctl commands** - Don't run builds manually (cargo, sbt, etc.). Use:
   - `poetry run shardctl build-service <service>` for regular builds
   - `poetry run shardctl build-service <service> --docker` for Docker image builds
   - `poetry run shardctl build-service --list` to see available services
6. **Read README.md** for complete documentation and best practices

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

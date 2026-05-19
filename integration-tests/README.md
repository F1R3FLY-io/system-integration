# Integration Tests

HTTP + gRPC integration tests for F1R3FLY nodes, running against Docker-managed node clusters.

Three layers of documentation:
- **This file** — running tests
- [test/docs/ARCHITECTURE.md](test/docs/ARCHITECTURE.md) — framework internals (fixtures, Provider protocol, cleanup, ports, timeouts)
- [test/docs/WRITING_TESTS.md](test/docs/WRITING_TESTS.md) — how to add a test
- [test/docs/INDEX.md](test/docs/INDEX.md) — catalog of all 25 test files

---

## Prerequisites

- **Docker + Docker Compose** (containers managed by the test fixtures)
- **Python 3.10** (see the [main README](../README.md) for pyenv setup)
- **Poetry** (Python dependency manager)
- **Memory** — 12 GB RAM minimum for Rust node tests (3-validator shards)

```bash
# From the repo root
poetry install --with integration
```

---

## Quick start

```bash
# Run one fast test (brings up a shard, takes ~90s first time)
poetry run pytest integration-tests/test/tests/shared/test_wallets.py::test_validator1_pay_validator2 -v

# Run all shared-shard tests (reuses the session shard)
poetry run pytest integration-tests/test/tests/shared/

# Run everything
poetry run pytest
```

`poetry run pytest` is the canonical way to run tests. `shardctl test` is a convenience wrapper — see below.

---

## Running tests

### Sequential

```bash
# All tests
poetry run pytest

# By directory
poetry run pytest integration-tests/test/tests/shared/       # 69 tests, one shard
poetry run pytest integration-tests/test/tests/custom/        # 20 tests, one shard per test
poetry run pytest integration-tests/test/tests/standalone/    # 14 tests, standalone nodes

# Single file
poetry run pytest integration-tests/test/tests/shared/test_deployment.py -v -s

# Single test
poetry run pytest integration-tests/test/tests/shared/test_wallets.py::test_validator1_pay_validator2

# Stop on first failure (useful with --keep-running for post-mortem)
poetry run pytest integration-tests/test/tests/shared/ -x -v -s
```

### Parallel

pytest-xdist with `--dist=loadgroup` respects the `xdist_group` markers so session-scoped fixtures don't collide:

```bash
# Auto-detect worker count
poetry run pytest -n auto --dist=loadgroup \
  integration-tests/test/tests/shared/ \
  integration-tests/test/tests/custom/ \
  integration-tests/test/tests/standalone/ \
  --monitor

# Conservative — 3 workers (one per directory)
poetry run pytest -n 3 --dist=loadgroup \
  integration-tests/test/tests/shared/ \
  integration-tests/test/tests/custom/ \
  integration-tests/test/tests/standalone/
```

How it works:
- **Shared** tests (all marked `xdist_group("shared")`) run on one worker sequentially — they share a session-scoped shard
- **Custom** tests (`xdist_group("custom")`) run on one worker (can be parallelized further later)
- **Standalone** tests have no xdist group — each test runs on its own worker, fully parallel

Each worker gets a non-overlapping host port range automatically. No coordination needed.

---

## Flags

| Flag | Effect |
|---|---|
| `--provider={docker,subprocess}` | Infrastructure backend. `docker` (default) spawns nodes as containers using the `F1R3FLY_NODE_IMAGE` tag; `subprocess` spawns the locally-built node binary directly on the host. Set `F1R3FLY_NODE_BINARY=/abs/path/to/target/release/node` to override the default lookup at `services/f1r3node-rust/target/release/node`. **`F1R3FLY_NODE_BINARY` is ignored under the Docker provider** — to exercise a feature-branch binary, pass `--provider=subprocess`. |
| `--instafail` | Print each test's traceback the moment it errors or fails, instead of buffering until session end. Strongly recommended under `-n auto` (xdist) — without it, a session-fixture failure on the `@shared` worker can hide behind 30+ identical ERROR markers for the duration of the run. Provided by `pytest-instafail`. |
| `--maxfail=N` | Abort the session after N total failures/errors. Pair with `--instafail` for diagnostic runs. Useful as a "storm-stop" against fixture-failure cascades — e.g. `--maxfail=10` aborts a runaway session in ~10s instead of grinding through every dependent test. |
| `--keep-running` | Start shard, run tests, **don't tear down** after. Prints the session ID for reuse. |
| `--skip-setup --session-id <id>` | Adopt a shard from a previous `--keep-running` run. Skip bring-up (~2s vs ~60s fresh). |
| `--monitor` | Sample Docker resource usage (peak memory, CPU) across all framework containers. Report embedded in `report.json`. (Docker provider only.) |
| `--timeout-scale <f>` | Multiplier for every derived timeout. Use `1.5`–`2.0` on slow CI runners. |

---

## Image selection

Every provider-created node reads `F1R3FLY_NODE_IMAGE` (env var, single source of truth):

```bash
# Explicit env var
F1R3FLY_NODE_IMAGE=f1r3flyindustries/f1r3fly-rust:dev poetry run pytest ...

# Via shardctl shortcuts (all set F1R3FLY_NODE_IMAGE internally)
poetry run shardctl test --rust   # f1r3flyindustries/f1r3fly-rust:latest
poetry run shardctl test --scala  # f1r3flyindustries/f1r3fly-scala-node:latest
poetry run shardctl test --image myrepo/custom:tag
```

Default (no flags, no env): `f1r3flyindustries/f1r3fly-rust:latest`.

---

## Iterative debugging workflow

When you're iterating on a single test and want to skip the ~60s shard bring-up cost:

```bash
# 1. First run — bring up shard, run test, leave shard alive
poetry run shardctl test --keep-running test_wallets
# Output will include:
#   WARNING  conftest:xx Session a3f7b2c1 started with --keep-running.
#            To reuse this shard: `pytest --skip-setup --session-id a3f7b2c1`

# 2. Subsequent runs — reuse the shard, test runs in ~2s
poetry run shardctl test --skip-setup --session-id a3f7b2c1 test_wallets

# 3. Done iterating — wipe everything
poetry run shardctl test-reset
```

`shardctl test-reset` force-removes every framework container/network/volume — including containers kept alive by `--keep-running` and stale state from crashed sessions.

---

## Cleanup

Test fixtures clean up after themselves on normal exit via four layers (fixture teardown, `atexit`, session hooks, next-session stale scan — see ARCHITECTURE.md § 4). In practice you'll rarely need to clean up manually.

When you do need manual cleanup (after a crashed run or when you're done with `--keep-running`):

```bash
poetry run shardctl test-reset
```

This force-removes every Docker resource matching `rnode.test.*` / `f1r3fly-test-*` / `test-*` AND every subprocess node + session data dir under `integration-tests/.subprocess-data/`. Aggressive — it will clobber a `--keep-running` shard you forgot about. Safe on CI (each job runs in an isolated VM) and on local dev (you almost never have two framework shards running simultaneously).

When two agents share the repo and you must not disturb the other's session, scope the cleanup with `--session-id`:

```bash
poetry run shardctl test-reset --session-id <hex-id>
```

The session ID is the one printed by `--keep-running` (and recorded in subprocess-data dir names / Docker container names). Containers, networks, volumes, processes, and data dirs whose names don't match that ID are left untouched. Idempotent — running it for an unknown ID is a no-op.

---

## What's next?

- **Writing a test?** → [test/docs/WRITING_TESTS.md](test/docs/WRITING_TESTS.md) (recipes for shared/custom/standalone)
- **Understanding the framework?** → [test/docs/ARCHITECTURE.md](test/docs/ARCHITECTURE.md) (fixtures, Provider protocol, cleanup, ports, timeouts)
- **Looking for a specific test?** → [test/docs/INDEX.md](test/docs/INDEX.md) (all 22 files, one-line summaries, links)
- **Adding a new backend (K8s, local processes)?** → [test/docs/ARCHITECTURE.md § 9](test/docs/ARCHITECTURE.md#9-how-to-add-a-new-provider)

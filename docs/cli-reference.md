# `shardctl` CLI Reference

Every `shardctl` command, grouped by purpose. For starting points, see the [main README](../README.md). For compose-file details, see [../COMPOSE_STRUCTURE.md](../COMPOSE_STRUCTURE.md). For node and env-file configuration, see [configuration.md](configuration.md).

All commands run via `poetry run shardctl ...` (or activate the shell once with `poetry shell`).

---

## Service lifecycle

```
shardctl up [SERVICES...]         Start services (detached by default)
  --build, -b                     Build images before starting
  --foreground, -f                Run in foreground
  --profile, -p TEXT              Compose profile (dev/prod)
shardctl down [SERVICES...]       Stop and remove containers
  --volumes, -v                   Also remove named volumes
  --keep-orphans                  Keep orphan containers
shardctl restart [SERVICES...]    Restart services
shardctl wait                     Block until nodes report Running
  --timeout, -t INTEGER           Seconds before giving up (default 300)
```

When no services are specified, `up` / `down` / `restart` operate on the `startup_order` defined in `services.yml`.

---

## Cleanup

```
shardctl reset                    Stop F1R3FLY containers and remove blockchain data volumes
  --yes, -y                       Skip confirmation prompt
  --force                         Skip compose-down detection; force-remove every
                                  container matching production names + every volume
                                  matching f1r3fly-* + the f1r3fly network.
                                  Use when compose files have moved or detection isn't
                                  finding state.
shardctl test-reset               Force-remove every integration-test resource
                                  (rnode.test.* / f1r3fly-test-* / test-*),
                                  including running containers
shardctl clean [SERVICES...]      Delete cloned service repositories under services/
```

`reset` and `test-reset` are scope-disjoint: production prefixes (`rnode.{bootstrap,validator,…}`, `f1r3fly-*`) vs. integration-test prefixes (`rnode.test.*`, `f1r3fly-test-*`, `test-*`). One never touches the other's resources.

---

## Observability

```
shardctl status [SERVICES...]     Show container status (one compose project at a time)
shardctl ps [SERVICES...]         List running containers
shardctl logs [SERVICES...]       View logs
  --follow, -f                    Follow output
  --tail, -n INTEGER              Number of lines from the end
```

---

## Images and builds

```
shardctl pull [SERVICES...]       Pull service images
shardctl build [SERVICES...]      Build services from services.yml
  --no-cache                      Build without cache
shardctl build-service [SERVICE]  Build one service's Docker image
  --docker-only                   Skip native build, Docker only
  --sync                          Sync branch from services.yml first
  --list                          List available services
```

For image selection (`F1R3FLY_NODE_IMAGE`), see [../COMPOSE_STRUCTURE.md#image-selection](../COMPOSE_STRUCTURE.md#image-selection).

---

## Repository setup

```
shardctl clone [SERVICES...]      Clone service repos from services.yml
  --force, -f                     Remove existing dirs before cloning
  --include-disabled              Include disabled services
shardctl setup [--force]          Clone all service repositories
```

---

## Container interaction

```
shardctl exec SERVICE COMMAND...  Execute command in a container
  --no-tty, -T                    Disable TTY
shardctl shell SERVICE            Open an interactive shell
  --shell, -s TEXT                Shell to use (default /bin/bash)
shardctl compose ARGS...          Generic passthrough to docker compose
```

---

## Integration tests

```
shardctl test [SUITE]             Run integration tests via pytest
  --image, -i TEXT                Custom Docker image (sets F1R3FLY_NODE_IMAGE)
  --rust                          Shortcut: resolve to default Rust node image
  --scala                         Shortcut: resolve to default Scala node image
  --skip-setup                    Adopt an existing shard (requires --session-id)
  --session-id TEXT               Session ID of an existing shard (printed by
                                  --keep-running runs)
  --keep-running                  Start shard, skip teardown — leaves the shard up
                                  and prints its session ID for reuse
  --verbose, -v                   Verbose pytest output
  --pytest-args, -a TEXT          Additional pytest arguments
  Image priority: F1R3FLY_NODE_IMAGE env > --image > --rust/--scala > default
shardctl test-report              Parse and display report.json from the last run
  --failures                      Show failed tests only
shardctl test-reset               (See "Cleanup" section above)
```

`shardctl test` is a convenience wrapper. The canonical invocation is `poetry run pytest`. See [../integration-tests/README.md](../integration-tests/README.md) for the full test framework.

### Iterative debug loop

Skip the ~60s shard bring-up between iterations:

```bash
poetry run shardctl test --keep-running test_wallets
# Output: "Session <id> started with --keep-running. To reuse: --skip-setup --session-id <id>"
poetry run shardctl test --skip-setup --session-id <id> test_wallets   # ~2s per run
poetry run shardctl test-reset                                          # done
```

---

## Help

```
shardctl --help                   Top-level help
shardctl COMMAND --help           Per-command help
```

For environment variables, image selection, and compose conventions, see [../COMPOSE_STRUCTURE.md](../COMPOSE_STRUCTURE.md).

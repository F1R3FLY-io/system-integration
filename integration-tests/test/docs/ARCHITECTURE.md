# Integration Test Framework — Architecture

Framework internals. For **running** tests, see [../../README.md](../../README.md). For **writing** tests, see [WRITING_TESTS.md](WRITING_TESTS.md). For the **test catalog**, see [INDEX.md](INDEX.md).

---

## 1. Fixture hierarchy

Tests interact with `Node` and `Shard`. Everything else is plumbing that supports those two.

```
session (pytest session)
  │
  ├── session_id            8-char hex, fresh per pytest invocation
  ├── port_allocator        socket-verified, worker-partitioned ranges
  ├── cleanup_registry      tracks resources + orchestrates teardown
  ├── timeouts              one scale factor derives every timeout
  ├── resource_paths        resolves conf/, genesis/, certs/
  ├── node_conf             HOCON-parsed defaults.conf + rust.conf
  │
  └── provider  (scope=session)          DockerProvider | K8sProvider (stub)
        │
        ├── shared_shard  (scope=session)  session-wide 3-validator shard
        │     └── Node wrappers: boot / validator1..N / readonly
        │
        └── per-test fixtures (tests create their own shards via provider.*)
              ├── custom_shard           ShardConfig differing from shared
              └── standalone_node        single-node, no peers
```

Session-scoped fixtures are built once; tests sharing a fixture run on one pytest worker by xdist group.

---

## 2. Provider / NodeHandle protocol

`infra/providers/base.py` declares two `typing_extensions.Protocol` classes. Tests never import these — they interact with `Node`, which wraps a `NodeHandle`. Providers create handles; tests consume them.

### NodeHandle — per-node operations

| Method / property | Purpose |
|---|---|
| `name: str` | Container/pod name (`rnode.test.{session}.{role}`) |
| `ports: PortMapping` | Host-accessible ports (protocol/grpc_ext/grpc_int/http/discovery/admin) |
| `grpc_host: str` | Hostname for gRPC connections — `localhost` for Docker, FQDN for K8s |
| `network_name: str` | Docker network or K8s namespace |
| `logs(tail=None) -> str` | Fetch stdout/stderr |
| `is_running() -> bool` | Liveness check |
| `restart()` | Restart the node (Docker restart / K8s pod delete) |
| `pause()` / `unpause()` | Simulate network partition (`docker pause` / K8s NetworkPolicy) |
| `exit_code() -> Optional[int]` | Return exit code, or None if still running |
| `wait_for_exit(timeout=180)` | Block until exit; return exit code or None on timeout |
| `resource_usage() -> dict` | `{memory_mb, cpu_percent, memory_limit_mb}` |
| `stop()` | Stop without removing |
| `remove()` | Force-remove resources |

### Provider — shard/session operations

| Method / property | Purpose |
|---|---|
| `keep_running: bool` | Skip teardown on session end (via `--keep-running`) |
| `active_handles: List[NodeHandle]` | Every handle this provider created; used by log scanner |
| `create_shard(config) -> List[NodeHandle]` | Spin up bootstrap + validators + optional readonly |
| `add_node(network, node_config, bootstrap) -> NodeHandle` | Attach a joiner |
| `remove_node(handle)` | Tear down one joiner |
| `destroy_shard(handles)` | Tear down a full shard |
| `create_standalone(config) -> NodeHandle` | Single-node shard for isolated tests |
| `destroy_standalone(handle)` | Tear down standalone |
| `cleanup_all()` | Session-scoped teardown (called from fixture teardown + atexit) |
| `force_cleanup_all_test_resources()` **classmethod** | **User-invoked only.** Aggressive force-remove of every framework resource on this backend, regardless of status. Backs `shardctl test-reset`. Never called from pytest hooks. |
| `adopt_session(session_id) -> List[NodeHandle]` | Reuse a shard from a previous `--keep-running` run. Backs `pytest --skip-setup --session-id <id>`. |

Two provider impls today:
- **`DockerProvider`** (`infra/providers/docker.py`) — full impl, shells out to `docker` + `docker compose`.
- **`K8sProvider`** (`infra/providers/kubernetes.py`) — stub. Every Protocol method raises `NotImplementedError` with a one-line implementation hint (kubectl/helm equivalents).

---

## 3. Resource naming conventions

All framework-created resources carry one of three session-prefixed patterns:

| Resource type | Pattern | Example |
|---|---|---|
| Container | `rnode.test.{session_id}.{role}` | `rnode.test.b86b2dd6.validator1` |
| Network | `f1r3fly-test-{session_id}` or `f1r3fly-test-{session_id}-{role}` | `f1r3fly-test-b86b2dd6` |
| Volume | `test-{session_id}-{name}-data` | `test-b86b2dd6-validator1-data` |

**Why:**
- Parallel pytest workers (xdist) can't collide — each invocation gets its own `session_id`.
- Crashed sessions leave behind deterministically-named zombies that stale-scan logic can identify and remove.
- `shardctl test-reset` can find every framework resource without requiring a metadata store.

---

## 4. `CleanupRegistry` — defense-in-depth teardown

Defined in `infra/cleanup.py`. Four independent cleanup paths ensure resources don't leak even on abnormal termination:

| Layer | Trigger | Scope |
|---|---|---|
| 1. Fixture teardown | Normal pytest `yield`/`finally` | This session's registered resources |
| 2. `atexit` handler | Python interpreter shutdown (incl. `SIGTERM`, pytest-timeout `SIGALRM`) | This session's registered resources |
| 3. `pytest_sessionfinish` hook | End of pytest run | `cleanup_stale_sessions()` — any crashed-session zombies |
| 4. `pytest_sessionstart` hook (next run) | Start of next pytest run | `cleanup_stale_sessions()` — catches SIGKILL/OOM survivors where no Python cleanup fired |

`SIGKILL` / OOM kills bypass 1 + 2. Layer 4 is the backstop.

Two entry points:
- `cleanup_stale_sessions()` — **conservative.** Only removes containers in sessions where *every* container has exited. Safe to call concurrently from parallel pytest workers. Used by hooks.
- `force_cleanup_all_test_resources()` — **aggressive.** Removes everything matching the prefixes regardless of status. Used only by `shardctl test-reset`.

---

## 5. `PortAllocator` — xdist-friendly port ranges

`infra/ports.py`. Each pytest worker gets a non-overlapping range carved from a base pool:

| Worker | Range | Notes |
|---|---|---|
| `gw0` / master | 41000–41499 | Main worker |
| `gw1` | 41500–41999 | |
| `gw2` | 42000–42499 | |
| ... | 500 per worker | Socket-verified before allocation |

The allocator binds a test socket to each candidate port and releases it immediately — catches transient conflicts before the real container tries to bind.

A shard needs 6 ports per node (the 40400-series internal ports mapped to host), so one shard consumes ~30-36 host ports; a 500-port range handles ~15 concurrent shards per worker.

---

## 6. `TimeoutHierarchy` — one scale, many timeouts

`infra/timeouts.py`. A single `scale` factor (from `--timeout-scale`) multiplies every derived timeout: node startup, deploy inclusion, finalization, command. Base values live in `TimeoutConfig`.

CI runners are slower than laptops — `--timeout-scale=1.5` (or `2.0`) bumps every deadline uniformly.

---

## 7. Log scanning

`infra/log_events.py` + autouse fixture in `conftest.py` (`check_node_logs_after_test`).

After **every test**, the fixture pulls logs from every active node via `handle.logs()` and runs two scans:

1. **`scan_for_errors()`** — fails the test on any `PANIC` line. WARN/ERROR detection is disabled until `ACCEPTABLE_PATTERNS` is populated (whitelist work tracked in root `docs/TODO.md`).
2. **`scan_for_forbidden()`** — fails the test on any line matching `FORBIDDEN_PATTERNS` (e.g. `InvalidBondsCache`, `BondsCacheMismatch`, `Recording invalid block`, `DAG storage is missing hash`). These are strict invariants; no test should produce them silently.

A test can opt out of one or more forbidden patterns when it legitimately produces them as part of its verification:

```python
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_validator_failure_recovery(...): ...
```

The marker takes one or more pattern keys from `FORBIDDEN_PATTERNS` (defined in `infra/log_events.py`). Adding a pattern is a hard tightening — run the full suite to confirm no untagged test trips it. Existing opt-outs are listed in the source comment next to each pattern.

Per-test (not per-session) so the failing test name is the one that surfaces, not whatever ran last.

---

## 8. Image flow end-to-end

```
┌──────────────────────────────┐
│ F1R3FLY_NODE_IMAGE env var   │
└──────────────┬───────────────┘
               ▼
  infra/config.py:resolve_node_image()      single source of truth
               │
               ▼
  NodeConfig.effective_image                property; used by compose
               │
               ▼
  infra/compose.py:generate_compose()       dynamic YAML
               │
               ▼
  docker compose up -d                      DockerProvider.create_shard
```

All three use sites read `resolve_node_image()`; nothing hardcodes image names (except the fallback default). `shardctl test --rust` / `--scala` / `--image` are ergonomic shortcuts that all end up exporting `F1R3FLY_NODE_IMAGE` to the pytest subprocess.

Fallback default: `f1r3flyindustries/f1r3fly-rust-node:latest` (in `infra/config.py`).

---

## 9. How to add a new Provider

Goal: implement the Protocol so existing tests work without modification.

1. Create `infra/providers/<name>.py`.
2. Implement `<Name>NodeHandle` — match every method/property on `NodeHandle`. Map to your backend's primitives (pod/VM/process).
3. Implement `<Name>Provider` — match every method on `Provider`.
   - `create_shard` must block until every node reports healthy (HTTP 200 on `/api/status`).
   - Handles returned in `[bootstrap, validator1..N, readonly]` order.
   - Register every container/network/volume/pod/pvc you create so teardown finds them.
4. Teach `cleanup_all()` about this backend's resources (prefix-scan by session, labels, or similar).
5. Implement `force_cleanup_all_test_resources()` — aggressive, ignores status, spans all sessions. User-invoked only.
6. Implement `adopt_session(session_id)` — scan by session prefix/label, build handles for each pod/container, return in canonical order.
7. Update `conftest.py`'s `provider` fixture (or add a `--provider` flag) so tests can opt in.

Design for `NotImplementedError` with clear guidance as a first pass — `K8sProvider` is a working example of this pattern. A partial provider that raises on unused methods is better than a missing one.

---

## 10. What the framework doesn't do

- **No compose-file templating.** `infra/compose.py` generates YAML programmatically from `ShardConfig`. There are no static `docker-compose.*.yml` files.
- **No cross-session coordination.** Each pytest invocation is sovereign. Multiple invocations can coexist (parallel xdist workers, or independent `shardctl test` runs) because of session-prefixed resource names.
- **No ambient state.** Every test asserts its own preconditions; there's no "start state" assumption beyond what the session-scoped `shared_shard` fixture provides.
- **No auto-retry.** A flaky test is a bug to fix, not to paper over. Retry loops inside fixtures are there for genuinely asynchronous conditions (node reaching Running state) with hard deadlines.

---

## Glossary of infra modules

| Module | Role |
|---|---|
| `types.py` | Pure data — `NodeRole`, `ValidatorIdentity`, `PortMapping`. No I/O. |
| `keys.py` | Pre-defined validator keys (single source of truth for genesis + signing) |
| `config.py` | `TimeoutConfig`, `ShardConfig`, `NodeConfig`, `ResourcePaths`, `NodeConf` (HOCON parser), `resolve_node_image()` |
| `timeouts.py` | `TimeoutHierarchy` |
| `ports.py` | `PortAllocator` |
| `cleanup.py` | `CleanupRegistry` (Docker-specific today; see Section 4) |
| `polling.py` | Node-aware wrappers around `f1r3fly.polling` |
| `assertions.py` | Deploy/shard assertions re-exported from `f1r3fly.deploy` + `f1r3fly.par` |
| `log_events.py` | Structured log event parsing + `scan_for_errors` |
| `token_metadata.py` | HTTP `/api/status` token helper (on-chain queries via pyf1r3fly) |
| `genesis.py` | Custom genesis file generation |
| `compose.py` | Dynamic Docker Compose YAML generation |
| `node.py` | `Node` — wraps handle + pyf1r3fly clients + HTTP helpers |
| `shard.py` | `Shard` — collection of `Node`s + joiner lifecycle + adoption |
| `resource_monitor.py` | `--monitor` flag implementation (peak memory/CPU via `docker stats`) |
| `metrics.py` | Prometheus metric helpers |
| `providers/base.py` | `Provider` + `NodeHandle` Protocols |
| `providers/docker.py` | `DockerProvider` + `DockerNodeHandle` |
| `providers/kubernetes.py` | `K8sProvider` + `K8sNodeHandle` (stub) |

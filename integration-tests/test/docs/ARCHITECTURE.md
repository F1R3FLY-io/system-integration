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
  ├── timeouts              one scale factor derives every timeout
  ├── resource_paths        resolves conf/, genesis/, certs/, repo_root
  ├── node_conf             HOCON-parsed defaults.conf + rust.conf
  │
  └── provider  (scope=session)          chosen by --provider flag:
                                          DockerProvider | SubprocessProvider | K8sProvider (stub)
        │  (each provider owns its own resource lifetime — Docker uses
        │   DockerCleanupRegistry internally; Subprocess tracks PIDs +
        │   data dirs internally)
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
| `logs(tail=None) -> str` | Fetch structured log content (Docker: reads `/var/lib/rnode/logs/node.log` via `docker exec cat`; subprocess: reads captured log file) |
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
| `keep_running: bool` | Skip teardown on session end (via `--keep-running`) — keeps *every* shard |
| `keep_on_failure: bool` | Via `--keep-on-failure`: `destroy_shard()` preserves the shard **only when its test failed**, otherwise tears down normally. The "did the current test fail?" signal comes from `infra/run_outcome.py` — `sys.exc_info()` for inline `try/finally: shard.destroy()` teardown (the failing exception is still propagating), or the `pytest_runtest_makereport` hook's recorded `call` outcome for fixture-finalization teardown. Pairs with `-x`. |
| `active_handles: List[NodeHandle]` | Every handle this provider created; used by log scanner |
| `create_shard(config) -> List[NodeHandle]` | Spin up bootstrap + validators + optional readonly |
| `add_node(network, node_config, bootstrap) -> NodeHandle` | Attach a joiner or observer (role taken from `node_config.role` — `JOINER` or `READONLY`). READONLY adds `--heartbeat-disabled` and skips validator-identity flags. |
| `remove_node(handle)` | Tear down one joiner or observer |
| `destroy_shard(handles)` | Tear down a full shard |
| `create_standalone(config) -> NodeHandle` | Single-node shard for isolated tests |
| `destroy_standalone(handle)` | Tear down standalone |
| `cleanup_all()` | Session-scoped teardown (called from fixture teardown + atexit) |
| `force_cleanup_all_test_resources()` **classmethod** | **User-invoked only.** Aggressive force-remove of every framework resource on this backend, regardless of status. Backs `shardctl test-reset`. Never called from pytest hooks. |
| `cleanup_session(session_id)` **classmethod** | **User-invoked only.** Same aggressiveness as the above, but scoped to one `session_id`. Other sessions are untouched. Backs `shardctl test-reset --session-id <id>` for the multi-agent-on-one-repo case. Idempotent. |
| `adopt_session(session_id) -> List[NodeHandle]` | Reuse a shard from a previous `--keep-running` run. Backs `pytest --skip-setup --session-id <id>`. |

Shard startup is transactional. Providers register every spawned handle before
readiness polling so log scanning and teardown can see partial starts. A failed
or interrupted readiness check archives the node logs and force-removes that
partial shard even when preservation flags are enabled; those flags apply only
to successfully returned resources. Docker and subprocess providers both use
the same lifecycle helper.

Each shard also owns an immutable generated `rnode.conf`. The generator copies
the base Rust configuration byte-for-byte when no client fuel is requested, or
adds a canonical, validated `client-fuel-allocations` list for cost-accounting
tests. Bootstrap nodes, validators, readonly nodes, and later joiners all use
that same shard-specific file. Subprocess shard directories are numbered so a
second shard cannot overwrite the configuration of the first.

Three provider impls today (selected via `--provider={docker,subprocess}`; default is `docker`):
- **`DockerProvider`** (`infra/providers/docker.py`) — full impl, shells out to `docker` + `docker compose`.
- **`SubprocessProvider`** (`infra/providers/subprocess.py`) — full impl, spawns the locally-built `services/f1r3node-rust/target/release/node` binary directly as `subprocess.Popen` instances on `localhost`. No Docker, no image build. Per-session data dirs under `integration-tests/.subprocess-data/<session_id>/`. Pre-built binary required (set `F1R3FLY_NODE_BINARY` to override path).
- **`K8sProvider`** (`infra/providers/kubernetes.py`) — stub. Every Protocol method raises `NotImplementedError` with a one-line implementation hint (kubectl/helm equivalents).

---

## 3. Resource naming conventions

All framework-created resources carry session-prefixed patterns. Docker provider uses three Docker-resource patterns; subprocess provider uses a single per-session data-dir tree:

| Resource type | Pattern | Example | Provider |
|---|---|---|---|
| Container | `rnode.test.{session_id}.{role}` | `rnode.test.b86b2dd6.validator1` | Docker |
| Network | `f1r3fly-test-{session_id}` or `f1r3fly-test-{session_id}-{role}` | `f1r3fly-test-b86b2dd6` | Docker |
| Volume | `test-{session_id}-{name}-data` | `test-b86b2dd6-validator1-data` | Docker |
| Data dir | `integration-tests/.subprocess-data/{session_id}/{role}/` | `.subprocess-data/b86b2dd6/validator1/` | Subprocess |
| Log file | `integration-tests/.subprocess-data/{session_id}/{role}.log` | `.subprocess-data/b86b2dd6/boot.log` | Subprocess |

**Why:**
- Parallel pytest workers (xdist) can't collide — each invocation gets its own `session_id`.
- Crashed sessions leave behind deterministically-named zombies that stale-scan logic can identify and remove.
- `shardctl test-reset` can find every framework resource (Docker + subprocess) without requiring a metadata store.

---

## 4. `DockerCleanupRegistry` — defense-in-depth teardown (Docker provider)

Defined in `infra/cleanup.py`. Tracks resources created by the Docker provider (containers, volumes, networks, temp dirs); other providers (e.g. Subprocess, K8s) own their own resource lifetime via the `Provider` trait's `cleanup_all` / `force_cleanup_all_test_resources` methods. Four independent cleanup paths ensure resources don't leak even on abnormal termination:

| Layer | Trigger | Scope |
|---|---|---|
| 1. Fixture teardown | Normal pytest `yield`/`finally` | This session's registered resources |
| 2. `atexit` handler | Python interpreter shutdown (incl. `SIGTERM`, pytest-timeout `SIGALRM`) | This session's registered resources |
| 3. `pytest_sessionfinish` hook | End of pytest run | `cleanup_stale_sessions()` — any crashed-session zombies |
| 4. `pytest_sessionstart` hook (next run) | Start of next pytest run | `cleanup_stale_sessions()` — catches SIGKILL/OOM survivors where no Python cleanup fired |

`SIGKILL` / OOM kills bypass 1 + 2. Layer 4 is the backstop.

Three entry points:
- `cleanup_stale_sessions()` — **conservative.** Only removes containers in sessions where *every* container has exited. Safe to call concurrently from parallel pytest workers. Used by hooks.
- `force_cleanup_all_test_resources()` — **aggressive, broad.** Removes everything matching the prefixes regardless of status. Used by `shardctl test-reset` (no flag).
- `cleanup_session(session_id)` — **aggressive, scoped.** Same force as above but only resources whose names match the given session ID — anchored regex on container/network/volume names prevents prefix collisions with sibling sessions. Used by `shardctl test-reset --session-id <id>`.

---

## 5. `PortAllocator` — xdist-friendly port ranges

`infra/ports.py`. Each pytest worker gets a non-overlapping range carved from a base pool:

| Worker | Range | Notes |
|---|---|---|
| master (no xdist) | 12000–31999 | Entire pool |
| `gw0` | 12000–12499 | First parallel worker |
| `gw1` | 12500–12999 | |
| `gw2` | 13000–13499 | |
| ... | 500 per worker | Socket-verified before allocation |

The listener pool ends below Linux's default ephemeral source-port range (`32768–60999`). This is required because a bind probe followed by process or container startup cannot reserve the released port: an outbound connection could otherwise acquire it as an ephemeral source port during that handoff and make the node fail with `EADDRINUSE`. On Linux, the allocator reads `/proc/sys/net/ipv4/ip_local_port_range` and fails before shard startup if the configured listener pool overlaps the active kernel range. It also binds a test socket to every candidate and skips blocks with an existing explicit listener or `TIME_WAIT` socket.

A shard needs 6 ports per node (the 40400-series internal ports mapped to host), so one shard consumes ~30-36 host ports; a 500-port range handles ~15 concurrent shards per worker.

---

## 6. `TimeoutHierarchy` — one scale, many timeouts

`infra/timeouts.py`. A single `scale` factor (from `--timeout-scale`) multiplies every derived timeout: node startup, deploy inclusion, finalization, command. Base values live in `TimeoutConfig`.

CI runners are slower than laptops — `--timeout-scale=1.5` (or `2.0`) bumps every deadline uniformly.

---

## 7. Log scanning

`infra/log_events.py` + autouse fixture in `conftest.py` (`check_node_logs_after_test`).

After **every test**, the fixture pulls logs from every active node via `handle.logs()` — which reads from the structured log file written by the node's `--log-sink=both` flag — and runs `scan_for_forbidden` against a single `FORBIDDEN_PATTERNS` dict. Any unmatched-by-opt-out hit fails the test before teardown destroys the evidence.

`FORBIDDEN_PATTERNS` covers panics, KvStore failures, bonds-cache mismatches, missing DAG hashes, replay-rig divergence, structural self-validation failures, `FATAL` keyword, and similar consensus/runtime bug signatures — see [`infra/log_events.py`](../../test/infra/log_events.py) for the canonical list with per-pattern comments naming the bug class and known opt-outs.

Tests that legitimately exercise a known bug class opt out per-pattern:

```python
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_validator_failure_recovery(...): ...

# Multiple keys: opts out of every named pattern.
@pytest.mark.allow_forbidden_patterns("DAGStorageMissingHash", "KvStoreError")
def test_bonding_validators(...): ...
```

The marker takes one or more pattern keys from `FORBIDDEN_PATTERNS`. A log line matching multiple patterns fires on the first non-opted-out match (dict iteration order) — tests producing log lines that match several patterns must opt out of every applicable key.

Adding a pattern is a hard tightening — run the full suite to confirm no untagged test trips it. Existing opt-outs are listed in the source comment next to each pattern.

Per-test (not per-session) so the failing test name is the one that surfaces, not whatever ran last.

The model is **allowlist-of-fatal**, not blacklist-of-acceptable. An earlier iteration scanned for any unexpected ERROR/WARN/PANIC and filtered through an `ACCEPTABLE_PATTERNS` whitelist; that proved unmaintainable as every new test surfaced new normal-operation log lines that needed triage and added to the whitelist. The current model gates on known consensus/runtime bug signatures only — anything not in `FORBIDDEN_PATTERNS` is by definition not a fatal. (A prior iteration split this into `FATAL_PATTERNS` (no opt-out) + `FORBIDDEN_PATTERNS` (with opt-out); the split was collapsed because broad FATAL patterns shadowed FORBIDDEN opt-outs for sibling bug classes that share log-line shape, e.g. `KvStore error` matching both the parent-child race signature and the missing-DAG-hash signature.)

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

All three use sites read `resolve_node_image()`; nothing hardcodes image names (except the fallback default). `shardctl test --rust` / `--image` are ergonomic shortcuts that all end up exporting `F1R3FLY_NODE_IMAGE` to the pytest subprocess.

Fallback default: `f1r3flyindustries/f1r3fly-rust:latest` (in `infra/config.py`).

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
| `cleanup.py` | `DockerCleanupRegistry` (see Section 4); other providers own their own resource lifetime |
| `polling.py` | Node-aware wrappers around `f1r3fly.polling` (`deploy_and_read`, `wait_for_deploy_finalized` for canonical-state per-deploy tracking, `wait_for_finalized` for block-height advancement, `deploy_with_fallback`, `poll_until`) |
| `assertions.py` | Deploy/shard assertions re-exported from `f1r3fly.deploy` + `f1r3fly.par`; cross-node helpers (`assert_block_finalized_on_all_nodes`, `assert_all_deploys_finalized_on_all_nodes`, `assert_bonds_map_consistent_across_nodes`, `assert_all_nodes_agree_on_block`, `assert_all_nodes_agree_on_lfb`, `assert_contracts_consistent_across_nodes`). **Deploy- vs block-level finalization:** `assert_all_deploys_finalized_on_all_nodes` is the bg-load/orphan-regression helper — it polls each node's `deploy_finalization_status` (re-homing-aware: a deploy whose first block loses a merge and is re-included into a finalized descendant counts as finalized), where `assert_block_finalized_on_all_nodes` checks one fixed block hash (correct only when the block itself is the one that finalizes, e.g. a specific PoS/foreground block). |
| `log_events.py` | Structured log event parsing + `scan_for_forbidden` (single unified `FORBIDDEN_PATTERNS` dict with per-pattern marker opt-out) |
| `token_metadata.py` | HTTP `/api/status` token helper (on-chain queries via pyf1r3fly) |
| `genesis.py` | Custom genesis file generation |
| `compose.py` | Dynamic Docker Compose YAML generation |
| `node.py` | `Node` — wraps handle + pyf1r3fly clients + HTTP helpers |
| `shard.py` | `Shard` — collection of `Node`s + joiner/observer attach + adoption (`add_joiner` transient context-manager; `attach_joiner` persistent with identity; `attach_observer` persistent readonly) |
| `resource_monitor.py` | `--monitor` flag implementation (peak memory/CPU via `docker stats`) |
| `metrics.py` | Prometheus metric helpers |
| `providers/base.py` | `Provider` + `NodeHandle` Protocols |
| `providers/docker.py` | `DockerProvider` + `DockerNodeHandle` |
| `providers/subprocess.py` | `SubprocessProvider` + `SubprocessNodeHandle` (host-process backend) |
| `providers/kubernetes.py` | `K8sProvider` + `K8sNodeHandle` (stub) |

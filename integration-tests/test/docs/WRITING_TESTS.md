# Writing Integration Tests — Recipes

Practical patterns for adding a test. For framework internals, see [ARCHITECTURE.md](ARCHITECTURE.md). For running tests, see [../../README.md](../../README.md).

---

## Pick the right test directory

| Directory | When to use | Shard lifecycle |
|---|---|---|
| `tests/shared/` | You just need a running 3-validator shard — don't crash nodes, don't wipe state, don't need special config | Session-scoped `shared_shard` (one shard per pytest run, reused across all shared tests) |
| `tests/custom/` | You need a custom `ShardConfig` — asymmetric bonds, specific validator counts, heartbeat off, custom FTT, etc. | Module-scoped or per-test fixture that calls `provider.create_shard(...)` |
| `tests/standalone/` | You need a single node with no peers — testing isolated node behavior, heartbeat config, propose mechanics | Per-test fixture that calls `provider.create_standalone(...)` |

Heuristic: if your test starts with "the shard is already running and I just want to...", it's shared. If it starts with "I need a shard where...", it's custom. If it starts with "given a single node...", it's standalone.

---

## Recipe 1 — Shared-shard test

**Goal:** a read-only or non-invasive test against the session shard.

```python
# integration-tests/test/tests/shared/test_example.py

import pytest

from ...infra.keys import VALIDATOR1_ID
from ...infra.polling import wait_for_deploy_included, wait_for_finalized

pytestmark = pytest.mark.xdist_group("shared")  # pin to one xdist worker


def test_something(validator1_node, readonly_node, timeouts):
    """Short description of what this verifies."""
    deploy_id = validator1_node.deploy_string(
        '@1!(42)',
        VALIDATOR1_ID.private_key(),
    )
    block_info = wait_for_deploy_included(
        validator1_node, deploy_id, timeouts.deploy_inclusion
    )
    wait_for_finalized(validator1_node, block_info.blockNumber, timeouts.finalization)

    # Now read from the readonly observer
    ...
```

**Available fixtures** (all session-scoped, from `conftest.py`):
- `boot_node`, `validator1_node`, `validator2_node`, `validator3_node`, `readonly_node`
- `validator_nodes` (list), `all_nodes` (list)
- `timeouts` — TimeoutHierarchy (use `timeouts.deploy_inclusion`, `timeouts.finalization`, etc.)
- `node_conf` — effective conf from `defaults.conf + rust.conf` (HOCON-parsed)

**Don't:** crash/restart nodes, call `provider.destroy_shard`, deplete wallets. Any test that mutates shared state should move to `tests/custom/`.

---

## Recipe 2 — Custom-shard test

**Goal:** a shard with non-default bonds, disabled heartbeat, or unusual topology.

```python
# integration-tests/test/tests/custom/test_example.py

import pytest

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")


@pytest.fixture(scope="module")
def asymmetric_shard(provider, timeouts):
    """Module-scoped shard — shared across all tests in this module."""
    config = ShardConfig(
        bonds=[(VALIDATOR1_ID, 60), (VALIDATOR2_ID, 20), (VALIDATOR3_ID, 15)],
        ftt=0.33,
        heartbeat=True,
        include_readonly=True,
    )
    shard = Shard.create(provider, config, timeouts)
    yield shard
    shard.destroy()


def test_asymmetric_finalization(asymmetric_shard, timeouts):
    v1 = asymmetric_shard.node("validator1")
    ...
```

**Fixture scope choice:**
- `scope="function"` — fresh shard per test (safest, slowest)
- `scope="module"` — reused across tests in one file (most common)
- `scope="session"` — conflicts with `shared_shard`; avoid

---

## Recipe 3 — Standalone-node test

**Goal:** single-node behavior (heartbeat timing, standalone propose, isolated execution).

```python
# integration-tests/test/tests/standalone/test_example.py

import pytest

from ...infra.config import NodeConfig
from ...infra.node import Node
from ...infra.types import NodeRole

# No xdist_group — each test gets its own node, safe to parallelize


def test_something(provider, timeouts):
    """Short description."""
    config = NodeConfig(
        role=NodeRole.STANDALONE,
        cli_flags=frozenset({"--heartbeat-enabled"}),
        cli_options={"--heartbeat-check-interval": "5seconds"},
    )
    handle = provider.create_standalone(config)
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    try:
        # test logic here
        ...
    finally:
        node.close()
        provider.destroy_standalone(handle)
```

Or extract the setup into a fixture when multiple tests share it — pattern is the same as Recipe 2 but with `create_standalone` / `destroy_standalone`.

---

## Common deploy + wait patterns

### Submit a deploy, wait for inclusion, wait for finalization

```python
from ...infra.polling import wait_for_deploy_included, wait_for_finalized

deploy_id = node.deploy_string('@1!(42)', key.private_key())
block_info = wait_for_deploy_included(node, deploy_id, timeouts.deploy_inclusion)
wait_for_finalized(node, block_info.blockNumber, timeouts.finalization)
```

### Deploy + read result in one helper

```python
from ...infra.polling import deploy_and_read

# Submit, wait for inclusion, read data at name on the resulting block
result = deploy_and_read(node, term, channel_name, timeouts)
```

### Poll with a deadline

```python
from ...infra.polling import poll_until

# Wait until condition is met or deadline expires
poll_until(
    lambda: node.last_finalized_block().blockInfo.blockNumber >= 10,
    timeout=timeouts.finalization,
    description="LFB reaches block 10",
)
```

---

## Per-test documentation convention

Every test file has a sibling `.md` in `test/docs/` with the same stem (`test_foo.py` → `test_foo.md`). Keep the structure consistent:

```markdown
# test_foo

## Purpose
What this file verifies at a high level. One paragraph.

## Tests (N)
- `test_bar` — one-line description
- `test_baz` — one-line description

## Setup
Fixture dependencies, custom shard config (if any), timeouts.

## Key assertions
Bullet list of the non-obvious invariants tested.

## Infrastructure used
Which `Node` methods, which `provider` methods, which polling helpers.
```

See any file in `test/docs/test_*.md` for concrete examples. Add your new doc to [INDEX.md](INDEX.md).

---

## Parallel-safety checklist

Parallel mode (`pytest -n auto --dist=loadgroup`) assigns whole xdist groups to one worker. Shared tests stay on one worker (session fixture). Custom/standalone tests can spread across workers.

Things that break parallel mode:
- Hard-coded host ports → use `port_allocator` (automatic via providers)
- Hard-coded container names → use framework-generated session-prefixed names (automatic)
- Cross-test state in module-globals → don't
- File writes outside a fixture-owned tempdir → don't

Things that are safe:
- Logging (one log stream per worker)
- Session-scoped fixtures — xdist scopes them per-worker, so `shared_shard` is isolated per worker
- Port allocation — each worker's `PortAllocator` gets a non-overlapping range

---

## When to `deselect` vs `skip` vs delete

- **`pytest.skip()` inside a test** — runtime condition, environment-dependent (e.g., "skip on standalone where validator and readonly are the same node")
- **`@pytest.mark.skip(reason=...)`** — class- or function-level skip. Prefer this over bare `skip()` if the condition is static.
- **`--deselect path::test` in CLI or CI** — known-broken test you want out of a specific run without touching source. See `tests/shared/test_convergence.py::test_network_converges_after_slow_deploy` as an example (triggers #437 shard stall).
- **Delete the test** — feature removed or superseded.

Don't skip silently without a clear reason string. Don't deselect indefinitely — put an issue link + a triage date.

---

## Adding a dependency to pyf1r3fly

Most framework helpers (`VaultAPI`, `crypto`, `par`) are from the `pyf1r3fly` package at `services/pyf1r3fly/`. If you need a new helper:

1. Add it to `services/pyf1r3fly/f1r3fly/`.
2. Reinstall locally: `poetry run pip install -e services/pyf1r3fly`.
3. Import in your test: `from f1r3fly.vault import ...`.

Don't duplicate client logic in the framework — keep it in pyf1r3fly so both tests and other clients benefit.

---

## Iterating fast while writing a test

```bash
# First run: start fresh shard, run one test, keep shard alive
poetry run shardctl test --keep-running <suite-pattern>
# → look at output: "Session <id> started with --keep-running..."

# Subsequent runs: reuse the shard, ~2s per iteration
poetry run shardctl test --skip-setup --session-id <id> <suite-pattern>

# Done: clean up
poetry run shardctl test-reset
```

See `../../README.md` for more on the debug loop.

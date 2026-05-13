# Slashing — Production Readiness & Test Plan

## Rust Node: Slashing Pipeline (Pure Rust)

The full slashing pipeline has been ported to Rust. Key source files:

| Component | File | What It Does |
|-----------|------|-------------|
| Block validation (17+ checks) | `casper/src/rust/validate.rs` | `block_number()`, `sequence_number()`, `parents()`, `block_hash()`, `shard_identifier()`, `neglected_invalid_block()`, `bonds_cache()` |
| InvalidBlock enum | `casper/src/rust/block_status.rs` | All 17 slashable offense types + `is_slashable()` method |
| Equivocation detection | `casper/src/rust/equivocation_detector.rs` | `check_equivocations()`, `check_neglected_equivocations_with_update()`, recursive detection |
| Recording invalid blocks | `casper/src/rust/multi_parent_casper_impl.rs` (line ~1066) | Logs `"Recording invalid block {hash} for {InvalidBlockType:?}"` and stores in DAG |
| SlashDeploy generation | `casper/src/rust/blocks/proposer/block_creator.rs` (line ~291) | `prepare_slashing_deploys()` reads `invalid_latest_messages` from DAG, creates `SlashDeploy` per offender |
| SlashDeploy execution | `casper/src/rust/util/rholang/costacc/slash_deploy.rs` | Rholang source calls `@PoS!("slash", ...)` |
| PoS contract | `PoS.rhox` (Rholang, shared with Scala) | Sets bond to 0, confiscates funds, removes from active set |

## Known Gaps (Node-Side)

### 1. IgnorableEquivocation DOS Vector

`block_status.rs` line 39 has a TODO:
> "Make IgnorableEquivocation slashable again... The above will become a DOS vector if we don't fix."

Currently, equivocations that aren't pulled in as dependencies are ignored rather than slashed. An attacker could create equivocating blocks that other validators can safely ignore, flooding the network without penalty. This was a known issue inherited from Scala.

### 2. No Configurable Slashing Parameters

Everything is hardcoded:
- No threshold (one strike = full forfeiture)
- No graduated penalties
- No cool-down between slashes
- Parameters locked at genesis in the PoS contract

Whether this needs to change depends on production requirements. Configurable thresholds would require PoS contract changes (Rholang), not just node config.

### 3. No Slashing Documentation

No formal documentation exists beyond source code. See `docs/slashing-mechanism.md` for the summary created from code analysis.

### 4. Log Format Difference from Scala

Rust node logs: `"Recording invalid block {hash} for {InvalidBlockType:?}"`

The `:?` debug format prints the Rust enum variant name (e.g., `InvalidBlockHash`). The Scala node used a slightly different format. Tests need to match the Rust format when checking logs.

## Test Infrastructure: What Needs to Change

### Current State of test_slash.py

The existing `test_slash.py` is **completely broken** — it was written for the legacy Scala test infrastructure and has never been ported:

- Imports `ready_bootstrap_with_network` and `bootstrap_connected_peer` — functions that no longer exist in `rnode.py`
- Calls `conftest.testing_context()` with parameters it doesn't accept (bootstrap_key, peers_keys, etc.)
- `Node.get_peer_node_ip()` method doesn't exist on current Node class
- Not in `pyproject.toml` test list or any CI workflow
- References `/opt/docker/examples/tut-hello.rho` (container path that may not exist in Rust image)

### What Works

- `node_client.py` — the transport layer client (`NodeClient`) is intact
- `routing.proto` — identical between Scala and Rust nodes
- The Rust node handles `BlockMessage` and `BlockRequest` packets in `engine/running.rs` (lines ~161, ~214)
- The Rust node streams blocks back via `stream_message_to_peer` in response to `BlockRequest`

### Required Changes

#### 1. Add `get_peer_node_ip()` to Node class (`rnode.py`)

The `NodeClient` needs to resolve container IPs on the Docker network. Add a method to the current Node class that extracts the container's IP from Docker attributes:

```python
def get_peer_node_ip(self, network_name: str) -> str:
    """Get this container's IP on the given Docker network."""
    self.container.reload()
    networks = self.container.attrs['NetworkSettings']['Networks']
    for name, config in networks.items():
        if network_name in name:
            return config['IPAddress']
    raise ValueError(f"Container {self.name} not connected to network {network_name}")
```

#### 2. Rewrite test_slash.py to use `start_custom_shard`

Replace legacy infrastructure with current custom shard pattern:

- Use `start_custom_shard()` from conftest (same pattern as `test_asymmetric_bonds.py`)
- Use unique port base (e.g., 40900) to avoid conflicts with other custom shard tests
- Use `pytestmark = pytest.mark.xdist_group("custom")` for proper test isolation
- 2 validators minimum (3 for justification test), heartbeat disabled, FTT=-1
- Replace `/opt/docker/examples/tut-hello.rho` with `deploy_string('new x in { x!(1) }', key)`

#### 3. Update `node_protocol_client` usage

- Pass custom shard's network name (`f1r3fly-test-custom`) instead of session shard network
- The `NodeClient` needs the Docker network for bidirectional transport layer communication
- Keep `@pytest.mark.skipif(sys.platform in ('win32', 'cygwin', 'darwin'))` — transport layer requires native Linux Docker networking (container-to-host routing)
- **WSL2 limitation**: Tests won't run locally on WSL2 — transport port (40400) isn't host-mapped and Docker bridge routing doesn't work. Must run on native Linux / CI.

#### 4. Expose transport port in custom shard compose (if needed)

The custom shard compose generator in `conftest.py` may need to map the transport port (40400 internal) to a host port. Check whether `NodeClient` connects via container IP (works on native Linux) or via localhost (needs port mapping).

#### 5. Match Rust log format in assertions

Update `wait_for_log_match` regex patterns:
- Scala: `Recording invalid block {hash[:10]}... for InvalidBlockHash`
- Rust: `Recording invalid block {hash[:10]}... for InvalidBlockHash.` (10-char
  truncation matching Scala — `PrettyPrinter::build_string_bytes` at
  `models/src/rust/casper/pretty_printer.rs:74` calls `Self::limit(..., 10)`;
  trailing period comes from the `{:?}.` format string at
  `casper/src/rust/casper_engine/validation_dispatcher.rs:481, :564`)

The regex must therefore (a) escape the literal `...` ellipsis (`\.\.\.`) and
(b) include the trailing `\.` after the variant name. Example:
```python
re.compile(rf"Recording invalid block {h.hex()[:10]}\.\.\. for InvalidBlockHash\.")
```

Verify exact format by checking
`casper/src/rust/casper_engine/validation_dispatcher.rs:481` (the line
moved from `multi_parent_casper_impl.rs:~1066` during the Phase 3
god-object decomposition).

#### 6. Add to pyproject.toml and CI

- Add `test_slash.py` to `python_files` in `pyproject.toml`
- Add to CI workflow test matrix (both `build-test-and-deploy.yml` in f1r3node-rust and system-integration CI)
- Update `TEST_CASES.md` with slash test documentation

#### 7. Mirror Rust's block-hash byte layout for forged blocks

The upstream Python `pyf1r3fly.crypto.gen_block_hash_from_block` wraps
`sigAlgorithm` / `seqNum` / `shardId` in protobuf `StringValue` /
`Int32Value` boxes before hashing. The Rust node
(`casper/src/rust/util/proto_util.rs:380 hash_block`) hashes the raw UTF-8
bytes for the string fields and 4 little-endian bytes for `seqNum`. The
two hashes therefore diverge for every non-genesis block. Because
`Validate::block_hash` is the **first** check in `block_summary`
(`casper/src/rust/validate.rs:291`), any forged block re-hashed with the
pyf1r3fly helper would be rejected as `InvalidBlockHash` before the
test's intended offense (InvalidBlockNumber / InvalidSequenceNumber /
InvalidFollows / InvalidParents) is reached.

Add a module-local helper in `test_slash.py` that mirrors the Rust byte
layout exactly:

```python
def rust_block_hash(block: BlockMessage) -> bytes:
    buf = b''.join([
        block.header.SerializeToString(),
        block.body.SerializeToString(),
        block.sender,
        block.sigAlgorithm.encode('utf-8'),
        block.seqNum.to_bytes(4, 'little', signed=True),
        block.shardId.encode('utf-8'),
        block.extraBytes,
    ])
    return hashlib.blake2b(buf, digest_size=32).digest()
```

Replace every `gen_block_hash_from_block(invalid_*)` call with
`rust_block_hash(invalid_*)`. Anchor the invariant at the start of each
active test with `assert rust_block_hash(b) == b.blockHash` for the
unmodified `b` returned by `client.block_request(...)` — so future
upstream drift surfaces immediately rather than silently breaking the
tests. Filing a follow-up against `pyf1r3fly` to upstream the byte
layout as a `rust_compat=True` flag is recommended but not required.

### Test Coverage Plan

#### Tests to Port (6 active from Scala)

| Test | Offense Type | Complexity |
|------|-------------|-----------|
| `test_slash_invalid_block_hash` | InvalidBlockHash | Low — tamper hash, re-sign |
| `test_slash_invalid_block_number` | InvalidBlockNumber | Low — set block number to 1000 |
| `test_slash_invalid_block_seq` | InvalidSequenceNumber | Low — set seq to 1000 |
| `test_slash_justification_not_correct` | InvalidFollows | Medium — needs extra validator key in bonds |
| `test_slash_GHOST_disobeyed` | InvalidParents | Medium — requires 3-block sequence |
| `test_node_working_right_after_slashing` | InvalidBlockHash | Low — extends invalid_block_hash test |

#### Skipped Test to Evaluate

| Test | Offense Type | Notes |
|------|-------------|-------|
| `test_slash_invalid_validator_approve_evil_block` | NeglectedEquivocation | Was skipped in Scala. Needs 3 validators. Tests Level 2 enforcement. Evaluate if it can work with current infrastructure. |

#### Tests NOT Currently Covered (potential additions)

These offense types have no integration test coverage:

- AdmissibleEquivocation
- DeployNotSigned
- InvalidRepeatDeploy
- InvalidShardId
- JustificationRegression
- InvalidTransaction
- InvalidBondsCache
- ContainsExpiredDeploy / ContainsTimeExpiredDeploy / ContainsFutureDeploy

Priority for new tests should be based on attack surface and production risk.

### Execution Order

1. Verify transport layer compatibility (send a valid block via `NodeClient`, confirm Rust node accepts it)
2. Port `test_slash_invalid_block_hash` first (simplest, validates full pipeline)
3. Port remaining 5 active tests
4. Evaluate and potentially enable `test_slash_invalid_validator_approve_evil_block`
5. Consider new tests for uncovered offense types based on priority

---

## Status (2026-05-12) — All planned tests implemented + expansion landed

This plan is historical. The 6-test revival landed first, then an expansion phase added 11 more tests covering the H1–H5 / M1–M6 threat coverage gaps. The canonical list of tests, their coverage scope, port_base allocations, and per-test docstrings live in:

- `integration-tests/TEST_CASES.md` — `test_slash.py` section (18 tests)
- `integration-tests/test/test_slash.py` — module docstring + per-test docstrings
- `f1r3node-rust/docs/theory/slashing/slashing-threat-model.md` §3.1 — Docker / wire-protocol coverage table mapping each threat class to its Docker test
- `f1r3node-rust/docs/theory/slashing/slashing-specification.md` §13 — `End-to-end shard reproduction of equivocation` row flipped from "Out" → "In" with cross-references

M7 (`test_slash_censoring_proposer_eventually_slashes`, T-37 / T-12PF) is deferred — the Rust binary has no runtime proposer-fairness detector and the threat model classifies T-12PF as a *boundary assumption* (per `slashing-traceability.md` finding 88 = `model_boundary`, "No source bug confirmed"). The deferral is documented in the `test_slash.py` module docstring.

Implementation log: see `f1r3node-rust/docs/work-logs/` for the work-log entries that capture forge mechanics, post-state self-consistency analysis, and per-test debug iterations.

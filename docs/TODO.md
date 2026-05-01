# F1R3FLY Integration TODO

Living document. Strategic priorities and asi-notes triage live in [`roadmap.md`](roadmap.md).

This file holds **integration-side work** (test framework, compose config, docs)
and **bug observations not yet filed as GitHub issues**. Cross-repo bugs are
tracked in [F1R3FLY-io/f1r3node Issues](https://github.com/F1R3FLY-io/f1r3node/issues)
— this file does not duplicate them, only links.

---

## 1. Test Suite Baseline

Last full run: **2026-04-28 (Gate 1.1)** on `refactor/integration-test-framework`
working tree — 92 passed / 2 failed in 34m36s, no forbidden-pattern trips.

Spot-check 2026-04-30 on `rust/staging` HEAD (`bfaa2c89` self-contained binary)
via subprocess provider:

- `test_shard_degradation` — 3/3 runs pass at 646.7s (was failing at 710s in
  §1.1). Stable across runs. Graduated out of the deselect list (§ 2.5).
- `test_finalization_asymmetric_bonds` — still fails (PR #484 fixed the
  validator side; readonly observer's FT cache path still broken). § 2.1 +
  smoking-gun evidence dump confirms.

**Image:** `f1r3flyindustries/f1r3fly-rust-node:local` built from
`fix/genesis-validator-late-join-recovery@66a0ed42` (PR #489 — late-joiner fix
+ flaky-test serialization).

**Canonical run command:**

```bash
poetry run pytest \
  integration-tests/test/tests/shared/ \
  integration-tests/test/tests/custom/ \
  integration-tests/test/tests/standalone/ \
  --deselect integration-tests/test/tests/custom/test_load.py \
  --deselect integration-tests/test/tests/shared/test_convergence.py::test_network_converges_after_slow_deploy \
  --deselect integration-tests/test/tests/custom/test_asymmetric_bonds.py::test_finalization_asymmetric_bonds \
  --deselect integration-tests/test/tests/shared/test_convergence.py::test_ft_convergence \
  --deselect integration-tests/test/tests/shared/test_bonding_validators.py \
  --deselect integration-tests/test/tests/shared/test_contract_lifecycle.py \
  -v --tb=short -n auto --dist=loadgroup --timeout=1200
```

Effective parallelism is ~2.05× (4255s of work in 2076s wall-clock). `loadgroup`
pins all `@shared` tests to one worker and all `@custom` to one worker; xdist
workers idle while the long `@shared`/`@custom` tails finish sequentially.

### 1.1 Baseline status — single source of truth

Status legend:
- ✅ **Passing** — counted toward green baseline; regressions surface here first
- 🐢 **Slow pass** — passing but >150s; watch for regressions
- ❌ **Deselected** — known failure with documented root cause; re-include when fixed
- ⏳ **Blocked** — added since last run, cannot run until upstream fix lands
- 🚫 **Permanent exclude** — design issue, never run as part of baseline

| Status | Test / file | Tests | Last run (s) | Notes |
|---|---|---:|---:|---|
| ❌ | `custom/test_asymmetric_bonds::test_finalization_asymmetric_bonds` | 1 | 349.4 | § 2.1 — observer's per-block FT cache frozen at V1 stake (0.2632). PR #484 fixed validator side; observer's `propagate_ft_to_finalized_blocks` path still broken. |
| ❌ | `shared/test_convergence::test_ft_convergence` | 1 | 270 (timeout) | § 2.2 — orphan-branch propagation gap; all 5 nodes' FT cache frozen at FTT=0.3333. Was flaky in §1.1 baseline, now a hard fail on `rust/staging` against subprocess provider. |
| ❌ | `shared/test_bonding_validators` | 1 | — | Pre-existing bonding instability; user has fix in progress on a separate branch. |
| 🚫 | `custom/test_load::test_deploy_throughput_and_finalization` | 1 | — | Propose-pipeline bottlenecked. [f1r3node#474](https://github.com/F1R3FLY-io/f1r3node/issues/474). Memory: `feedback_load_test_scope.md`. |
| 🚫 | `shared/test_convergence::test_network_converges_after_slow_deploy` | 1 | — | Triggers shard stall. [f1r3node#224](https://github.com/F1R3FLY-io/f1r3node/issues/224). |
| 🐢 | `custom/test_shard_degradation::test_shard_degradation` | 1 | 646.7 | § 2.5 — long by design (150 deploys × 30s waits). Subprocess-stable 3/3 on `rust/staging`. Re-verify on Docker provider before declaring §2.5 closed. |
| 🐢 | `custom/test_websocket::test_block_events` | 1 | 208.7 | 104s on shard setup. |
| 🐢 | `custom/test_consensus_safety::test_epoch_transition_under_heartbeat` | 1 | 179.2 | |
| 🐢 | `shared/test_bridge_admin::test_bridge_api_exploratory` | 1 | 162.4 | 69s on shard setup. |
| ✅ | `shared/test_web_api.py` | 23 | — | |
| ✅ | `standalone/test_token_metadata.py` | 13 | — | Includes `test_genesis_validator_with_wrong_token_blocks_ceremony` (was ❌ §2.10 — fixed by SubprocessProvider applying global/per-node CLI options). |
| ✅ | `shared/test_query_endpoints.py` | 12 | — | |
| ✅ | `custom/test_websocket.py` (excl. 1 🐢) | 5 | — | |
| ✅ | `shared/test_wallets.py` | 5 | — | |
| ✅ | `shared/test_token_metadata.py` | 5 | — | |
| ✅ | `custom/test_consensus_safety.py` (excl. 1 🐢) | 4 | — | |
| ✅ | `shared/test_deployment.py` | 4 | — | |
| ✅ | `custom/test_asymmetric_bonds.py` (excl. 1 ❌) | 3 | — | |
| ✅ | `standalone/test_heartbeat.py` | 2 | — | |
| ✅ | `shared/test_storage.py` | 2 | — | |
| ✅ | `shared/test_heartbeat.py` | 2 | — | |
| ✅ | `shared/test_bridge_admin.py` (excl. 1 🐢) | 1 | — | |
| ✅ | `standalone/test_propose.py` | 1 | — | |
| ✅ | `shared/test_genesis_ceremony.py` | 1 | — | |
| ✅ | `shared/test_dag_correctness.py` | 1 | — | |
| ✅ | `custom/test_trim_state.py` | 1 | — | |
| ✅ | `custom/test_synchrony_constraint.py` | 1 | — | |
| ✅ | `custom/test_bonding_validators.py` | 1 | — | |
| ⏳ | `shared/test_contract_lifecycle.py` | 7 | — | New (`bcaef31`). Needs additional fixes only on `feat/bitmask-or-mergeable-channels`; `rust/staging` (#488 merged) is not enough. Re-enable when those land on staging. |

**Roll-up:** 88 passing fast ✅ + 4 passing slow 🐢 + 3 deselected ❌ + 7 blocked ⏳ + 2 permanent exclude 🚫 = 104 tests across 22 files.

### 1.2 Notes

#### `shared/test_convergence::test_ft_convergence`

Was flagged flaky in earlier TODOs; passed cleanly in §1.1.

The test picks the **parent of LFB** as the target — a deliberately conservative
block whose initial cached FT is small (often 0.33-0.68). It then polls until
every node reports `FT == 1.0` for that block, with budget `finalization × 6`.
Convergence to 1.0 requires that later finalization rounds visit the block on
every node and that `propagate_ft_to_finalized_blocks` lifts the cache.

The flakiness has the same root cause family as § 2.2 and the
`test_finalization_asymmetric_bonds` failure: if the chosen target ends up on
an orphaned multi-parent branch (no later merge block includes its branch as a
parent), no later finalization round visits it, the propagation path skips it,
and FT stays frozen below 1.0.

**Why short-lived shards are riskier**: less DAG depth at poll time, fewer
descendants of the target, higher chance the target sits on a branch that
wasn't rebuilt-upon yet. Long-running shards mask this because every block
eventually gets built upon. Watch for re-flake when the shared shard is short
(few preceding tests on it) or under contention.

#### `custom/test_synchrony_constraint::test_synchrony_constraint`

Was flagged broken (first-proposal exemption); passed in §1.1. Custom shard
with FTT=-1, heartbeat off, per-validator thresholds: V1=0.67, V2=0.33, V3=0.99.
Walks 7 phases of explicit `propose()` calls, asserting each succeeds when its
synchrony math says it should be allowed.

**Why the test passes despite known node bugs**: the test only exercises the
*positive* case (proposal succeeds when threshold met). It doesn't (and can't,
in default config) exercise the *rejection* case. Two underlying gaps remain:

1. **First-proposal exemption is timing-sensitive** (§ 2.3 #1).
   `synchrony_constraint_checker.rs` checks `last_proposed_block_meta.block_number
   == 0` to grant the genesis exemption. There's a race between block visibility
   and DAG-state processing: a validator can propose its first block before the
   DAG state has fully processed the parent chain, so the field temporarily
   evaluates against a non-genesis state. The test sequences phases with
   `wait_for_block_visible` between each `propose()` call, which closes the race
   window. Reproducible at higher load (concurrent propose + many parents
   arriving), where the exemption check fires against the wrong state.
2. **`--synchrony-finalized-baseline-enabled` not exposed as a CLI flag** (§ 2.3 #2).
   The synchrony-constraint code has a fallback: if a proposer fails the threshold
   check, the finalized-baseline rescue path lets it propose anyway. The HOCON
   config key exists, but there's no clap CLI argument, so the test can't disable
   the rescue. Result: the test can never observe a rejection — the rescue always
   saves it. Half the contract goes untested.

Fix needed for either to surface in the test: (1) is a node-side correctness
fix for proper synchronization on `last_proposed_block_meta`; (2) needs either
a node-side CLI flag for the baseline-enabled config, or framework support for
per-test HOCON files.

#### `test_finalization_asymmetric_bonds` cleanup

New helper `wait_for_lfb_with_ft(node, target, ftt, timeout)` in
`infra/polling.py` collapses the inlined 3-call lambda + redundant post-poll
assertion. Single gRPC call per iteration, no torn reads. Reusable across any
test asserting the joint LFB-number + FT-cache invariant.

---

## 2. Bug Observations Not Yet Filed

These are observations from local runs / framework debugging that aren't captured
as GitHub issues yet. Move to f1r3node Issues when ready to action.

> **Bonding bug (Stacy 2026-04-23, v0.4.13).** Test plan and code-level analysis
> in [`bonding-bug-test-plan.md`](bonding-bug-test-plan.md). Distinct from the
> already-filed [f1r3node#373](https://github.com/F1R3FLY-io/f1r3node/issues/373)
> ("bonded but never runs a node → finalization stall"); same symptom family,
> different root cause. Both must remain open with separate coverage.

### 2.1 FT propagation race in `test_finalization_asymmetric_bonds`

The v2 test fails consistently. Validator side finalizes (FT=0.684) but the
readonly node's local FT never propagates above the threshold within the poll
budget. May be a real propagation gap rather than a test bug — same family as
the `propagate_ft_to_finalized_blocks` issue noted below.

**Update 2026-04-28 (Gate 1.1 run):** This session combined the LFB + FT poll
into a single predicate (was two separate polls). The combined predicate
re-failed at 298s against a `finalization*6 ≈ 270s` budget — close to but not
within. This rules out the "asserting too soon between two polls" theory and
points more strongly at a real cross-node FT propagation gap. Either the test
budget needs to grow, or the underlying propagation is slow enough to warrant a
node-side investigation of `propagate_ft_to_finalized_blocks` reaching the
readonly observer.

**Update 2026-04-30 (rust/staging baseline clone, `--keep-running` post-mortem):**
Confirmed this is **not** a slow propagation. It is a permanent divergence on
observers. Live state of the same shard, queried via HTTP `last-finalized-block`
~6 minutes after the test failed:

| Node       | LFB # | `faultTolerance` | `isFinalized` |
|------------|------:|------------------|---------------|
| boot       |    34 | 0.6842           | true          |
| validator1 |    34 | 0.6842           | true          |
| validator2 |    34 | 0.6842           | true          |
| validator3 |    34 | 0.6842           | true          |
| **readonly** | **36** | **0.2632**     | **true**      |

Readonly's LFB is **two ahead** of every validator, yet its cached FT is V1's
stake alone (60/95 = 0.2632). It also reports `isFinalized=true` on a block
whose cached FT is **below** FTT=0.33 — so the LFB pointer and the per-block FT
field are written by independent code paths and have desynced.

Per-block scan via `/api/blocks/40` (same shard), looking at the **same block
hashes** on readonly vs validator1:

| Block (hash prefix)         | Readonly FT | V1 FT  |
|-----------------------------|-------------|--------|
| #30 `220427c1238817cc`      | 0.2632      | 1.0000 |
| #34 `ba9b7cdecc9a9d72`      | 0.2632      | 1.0000 |
| #36 `3c87a162b3832dfa`      | 0.2632      | 0.6842 |
| #38 `636b023bf6aae5c7`      | 0.2632      | 0.6842 |

Validators correctly walk every finalized block from FT=0.263 (V1 alone) up
through 0.684 (V1+V2) to 1.0 (all three) as later blocks build on them. **Every
finalized block on readonly stays frozen at 0.2632**, regardless of how many
later validators sign on top.

Diagnosis: `propagate_ft_to_finalized_blocks` runs on validators but is either
not running on the readonly observer, or is running but failing to update the
cache. The cached FT reflects only the observer's first-finalization view (V1's
single signature visible via gossip), and never gets a second pass.

**Important context:** PR
[#484](https://github.com/F1R3FLY-io/f1r3node/pull/484)
("fix: cache fault tolerance at finalization time", commit `7d6b0fe4`) is
**already on `rust/staging`** and is included in the build that produced the
table above. That PR fixed the symptom we initially feared (per-query oracle
recomputation returning DAG-state-dependent values) on the validator side.
What we're seeing now is a **second, distinct bug** in the same code area —
the observer's propagation path doesn't update the cache once the initial
finalization-time write happens.

**Where to fix (node):** trace `propagate_ft_to_finalized_blocks` on observer
nodes. Likely candidates: the observer's `finalized_block_set` is empty/stale,
or the propagation path is gated on a validator-only condition (e.g. the
observer doesn't run the finalizer cycle that triggers the propagation pass).
Local branch `fix/cache-fault-tolerance` is the dev branch for #484 and is
already merged — not a candidate for further work.

**Test status:** `test_finalization_asymmetric_bonds` deselected from the
baseline run pending node fix. Validators (boot, V1, V2, V3) consistently
satisfy the combined predicate within 1 finalization budget — once the observer
FT propagation lands, this test should pass and re-enter the baseline.

**Test cleanup landed alongside the deselect:**
- New helper `wait_for_lfb_with_ft(node, target, ftt, timeout)` in
  `infra/polling.py` — single gRPC call per iteration, no torn reads between
  blockNumber and faultTolerance fields.
- `test_finalization_asymmetric_bonds` rewritten to use the helper. Inlined
  3-call lambda + redundant post-poll assertion gone.

### 2.2 Finalized blocks on orphaned DAG branches stay at FT=0.3333

In a multi-parent DAG, a finalized block can become unreachable from future LFBs
if no later merge block includes its branch as a parent.
`propagate_ft_to_finalized_blocks` updates blocks in `finalized_block_set`, but
when the block was only finalized on ONE node and not yet on others (different
nodes finalize different LFB chains), the other nodes' `finalized_block_set`
doesn't contain it, so propagation skips it.

**Test:** [`shared/test_convergence.py::test_ft_convergence`](../integration-tests/test/tests/shared/test_convergence.py).

**Update 2026-04-30 (rust/staging full baseline run, subprocess provider):**
Hard-failed at the 270s timeout. End-of-test FT values across all 5 nodes:

```
boot:        0.3333
validator1:  0.3333
validator2:  0.3333
validator3:  0.3333
readonly:    0.3333
```

All nodes are stuck at the original cached FT (FTT) — no propagation to 1.0
ever happened. This is **not** the §2.1 observer-only divergence; here all
validators agree but the cache stays frozen. The target block (parent of LFB
at test start) likely landed on a multi-parent branch that no later merge
block included as a parent, so no subsequent finalization round visited it
and the propagation pass skipped it across the board.

**Fix branch consideration:** the work on `fix/finalizer-cross-run-cache` (in `services/f1r3node-rust`) does **not** address this — that branch fixes the death-spiral mechanism, not cross-node propagation. Separate work needed.

### 2.3 `test_synchrony_constraint` documented gaps

Two issues, currently masked by the test passing in the latest baseline because
the first-proposal exemption happens to evaluate correctly:

1. **First-proposal exemption can fail.** `synchrony_constraint_checker.rs` checks
   `last_proposed_block_meta.block_number == 0`, but timing between block
   visibility and DAG processing can make the check evaluate against a non-genesis
   state. Reproducible at higher load.
2. **`synchrony-finalized-baseline-enabled` not exposed as a CLI flag.** Config
   key exists in HOCON but no clap argument. Cannot test the pure rejection path
   because the finalized-baseline fallback always rescues the proposer.

**Fix needed (node-side):** expose the flag, or have the test framework generate
per-test HOCON.

### 2.4 OPENAI_API_KEY not passed to containers via `shard.yml`

`docker/shard.yml` declares `OPENAI_ENABLED=${OPENAI_ENABLED:-false}` but does
not declare `OPENAI_API_KEY`. When `OPENAI_ENABLED=true` is set in `docker/.env`,
the node panics at `openai_service.rs:92` because docker-compose only forwards
declared env vars. Fix: add `OPENAI_API_KEY=${OPENAI_API_KEY:-}` to the `x-rnode`
anchor's `environment:` in `shard.yml` and `standalone.yml`. (Node-side
alternative: gracefully disable OpenAI when key missing instead of panic.)

### 2.5 `test_shard_degradation` finalizer-stall after sustained load

**Symptom (Gate 1.1 run, 2026-04-28):**

```
Production readiness FAILED:
  - LFB stalled: 6 consecutive batches with no advancement (max allowed: 1)
  - Deploy inclusion: 4/10 sampled deploys not included within 10s:
      #91 (bridge), #106 (bridge), #121 (bridge), #136 (bridge)
```

The shard advances LFB normally for the first ~50–80 deploys, then enters a
batch where no new block becomes finalized for the rest of the run. Same
symptom family as Stacy/Alexander's v0.4.9 production reports — see project
memory `project_shard_degradation.md`.

The deploys themselves keep being submitted, but inclusion latency on the
bridge channel grows past 10s as the shard degrades, indicating the
proposer/replay path is bottlenecked rather than the network layer.

**Update 2026-04-30 (rust/staging spot-check, subprocess provider):**
3/3 runs pass at ~647s with **0 stalls and 0 desync**:

| Run | Init rate | Final rate | Stalls | Desync | Time |
|---:|---:|---:|---:|---:|---:|
| 1 | 20.9 blk/min | 18.0 | 0 | 0 | 646.7s |
| 2 | 13.4 | 16.4 | 0 | 0 | 646.7s |
| 3 | 13.4 | 16.8 | 0 | 0 | 647.0s |

LFB rate stays stable or even improves over the test (final ≥ initial in 2/3
runs, 14% drop in run 1). All 8 strict assertions clear. Runtime is extremely
consistent (~647s ±0.4s).

Two commits landed on `rust/staging` since the §1.1 baseline (`fb59611f`
late-joiner fix, `bfaa2c89` self-contained binary), neither directly targets
the finalizer hot path. The improvement is most likely from running on
**subprocess provider** instead of Docker — no daemon overhead, no
container/network contention. Worth re-verifying on Docker provider before
declaring §2.5 closed; the failure may still reproduce when there's external
resource contention.

**Status:** Graduated out of the deselect list. Re-enabled in baseline
canonical run command. Watch for re-flake on Docker provider or under load.

### 2.6 gRPC client builds malformed URI when peer hostname resolves to IPv6

**Symptom (2026-04-29, surfaced via SubprocessProvider):**
With `--host localhost` and a bootstrap URL `rnode://...@localhost?protocol=PORT...`,
peer dial logs:

```
ERROR Failed to create gRPC endpoint: invalid authority http://::1:41042/ localhost
```

`localhost` resolves to `::1` (IPv6) but the resulting URI is unbracketed —
should be `http://[::1]:41042/`, not `http://::1:41042/`. RFC 3986 §3.2.2
requires brackets around an IPv6 host in URI authority.

**Reproduction:** Any peer connection where `peer.host` is a name that resolves
to IPv6. Doesn't surface in Docker because container DNS gives IPv4.

**Where to fix:** `comm/src/rust/transport/grpc_transport_client.rs` (URI
construction path that emits the "Failed to create gRPC endpoint" log). Same
fix needs to apply to discovery/Kademlia client URI construction.

**Workaround in test framework:** SubprocessProvider uses `127.0.0.1` (not
`localhost`) for `--host` and bootstrap URLs.

### 2.7 Phantom default bootstrap peer in `defaults.conf`

`/api/status` peerList includes an unreachable phantom entry:

```json
{ "address": "rnode://0000000000000000000000000000000000000000@127.0.0.1?protocol=40400&discovery=40404",
  "isConnected": false }
```

Sourced from a default placeholder bootstrap in
`node/src/main/resources/defaults.conf`. Discovery repeatedly attempts to
contact it and logs `Connection refused`. Cosmetic but produces persistent
ERROR-level log noise on every shard.

**Where to fix:** `node/src/main/resources/defaults.conf` — remove or empty the
default bootstrap entry; let the user supply `--bootstrap` explicitly. Update
the `ceremony-master-mode` path so it never attempts to dial a default peer.

### 2.9 `getBlock` returns `error` (not `PENDING`) for received-but-not-added blocks

**Symptom (2026-04-29, surfaced via `test_bonding_validators` Phase 8):**

A block proposed by a peer is gossiped to validators and announced; another
node calls `getBlock(hash)` immediately and gets:

```
gRPC response: error {
  messages: "Error: Block with hash 72d0e2a96b... received but not added yet"
}
```

The block exists at the protocol level (announced via gossip) but the
block-processor hasn't yet added it to that node's metadata store. There is a
brief window where `getBlock` cannot serve a block the node knows exists.

**Producer:** `casper/src/rust/api/block_api.rs:1288`.

**Why it's wrong:** The error path is essentially a 404 for a resource the node
*knows exists but can't yet serve*. That's a typed transient condition, not a
client error. Collapsing it onto the generic `error` channel forces clients to
string-match the message to distinguish "not yet ready" from "actually
missing".

**Two reasonable production fixes:**

1. **Block until processed**, with a server-side timeout — `getBlock` waits
   up to N seconds for the block-processor to finish, then returns the block
   or a clear `processing-timeout` status.
2. **Return a structured `PENDING` status** in the protobuf (separate from
   `error`) — typed signal lets clients poll without string matching.

Option 1 matches typical RPC semantics for "the resource exists, give me a
moment". Option 2 is more explicit but requires schema changes; the same
treatment likely applies to other endpoints that look up by block hash
(`isFinalized`, `findDeploy`, `getBlock` variants).

**Workaround in test framework:** `wait_for_block_visible_on_all_nodes`
helper in `polling.py` polls every node until `getBlock` succeeds before any
finalization assertion. Phase 8 of `test_bonding_validators` uses it.

### 2.10 Genesis ceremony master proceeds despite mismatched validator token configs

**Symptom (2026-04-30 baseline run, subprocess provider):**
`standalone/test_token_metadata::test_genesis_validator_with_wrong_token_blocks_ceremony`
fails with:

```
AssertionError: Ceremony master reached Running state despite two genesis
validators having mismatched token configs.
assert not True
```

The test starts a ceremony master expecting 3 genesis validators to sign, two
of which are configured with wrong `--native-token-name` / `--native-token-symbol`
/ `--native-token-decimals`. The contract is that the master should refuse to
finalize the genesis block when any validator's bonded token config disagrees
with the master's. In the run, the master reached Running anyway.

**Two possibilities:**
1. **Node bug** — the genesis ceremony's token-verification path doesn't actually
   gate the transition to Running. Either the verification isn't performed, or
   the master proceeds when the mismatched validators sign with their (wrong)
   configs because the signature itself is valid.
2. **Test bug** — the test waits for the master to reach Running, but the
   master may be reaching Running on a different code path (e.g. it timed out
   waiting for signatures and self-approved, or the validator processes never
   actually started). Need to inspect the `--required-signatures` value the
   test passes, and check master + validator logs.

**Where to dig:**
- Test source: `integration-tests/test/tests/standalone/test_token_metadata.py:484`
- Genesis ceremony master path:
  `casper/src/rust/engine/approve_block_protocol.rs::complete_genesis_ceremony`
- Token verification: search for `native_token_name` / `TokenMetadata` cross-checks
  in `casper/src/rust/engine/casper_launch.rs` and the genesis approver code.

**Status:** Deselected from baseline pending investigation. Likely candidate
for a small standalone bug repro before deciding test vs. node fix.

---

`node run --help` (and likely other subcommands) emits:

```
Run subcommand - Start RNode server
```

Stale rebrand. Should say "F1r3node server".

**Where to fix:** `node/src/main.rs` (or wherever the `run` subcommand's
clap `about`/`long_about` strings live). Audit all subcommand help strings
in the same pass.

---

## 3. Test Framework Tasks

### 3.1 Working-tree fixes (uncommitted)

One fix remains in the working tree on `refactor/integration-test-framework`:

| Path | Fix |
|------|-----|
| `integration-tests/test/tests/custom/test_asymmetric_bonds.py` | Combined LFB+FT poll predicate (closes the FT propagation race that surfaces in baseline §1) |

**Already committed** on the same branch:
- `e5b037b` — drop unused `bridge.rho`, harden `DockerProvider` (`docker stop -t 30`, network-race retry).

### 3.2 Open framework work

- **Background traffic generator** — opt-in conftest fixture (`active_traffic`) that
  sends deploys to all validators on a loop with unique channels per session.
  Realistic load for convergence/heartbeat/degradation tests.
- **Log scanner whitelist** — `infra/log_events.py` (`scan_for_errors`,
  `ACCEPTABLE_PATTERNS`) is built but disabled because the whitelist is empty.
  Run all tests with scanning enabled, triage WARN/ERROR/PANIC, populate
  `ACCEPTABLE_PATTERNS`, enable as autouse fixture.
- **Optimize `test_shard_degradation`** — currently ~11 min. Main cost is
  `BATCH_PROPAGATION_SECS = 30` × 15 batches = 450s of fixed sleeps. Replace
  with poll on per-node LFB advancement.
- **Implement `test_validator_expulsion_continued_finalization`** — V3 produces
  invalid state, V1+V2 reject V3's blocks, verify V1+V2 still finalize at
  FTT=0.1. Trigger options: deploy with wrong key, corrupt block data.
- **Add structured-error-code matching to tests** — currently regex on error
  text. Cross-repo: requires node-side ErrorCode enum (see §4).

### 3.3 Testing roadmap (deployment levels)

| Level | Setup | Status |
|-------|-------|--------|
| **1** Local dev | 3 validators, Docker Compose, FTT=0.1 sync=0 equal stake | **Current** — baseline run (§1) is on Level 1 |
| **2** Local multi-node | 7+ validators, FTT=0.67 sync=0, can lose 1-2 and still finalize | Pending — requires expanded compose, bonds.txt, test adaptation. Three node-side fixes for sync>0 stashed on `fix/convergence-after-divergence` |
| **3** Kubernetes staging | Validators on separate nodes, real network, real failure domains | Pending |
| **4** Production canary | Subset of real validators alongside stable | Pending |

Level 2 should add production-config (`ftt=0.67, sync=0, heartbeat=enabled`)
variants of: bonding at epoch boundary, trim-state under finalization lag, validator
loss with continued finalization, convergence after stall.

---

## 4. Cross-Repo Architecture Tasks

### 4.1 Structured gRPC error codes

Tests and clients currently match gRPC error messages with ad-hoc string patterns
(e.g. `(?i)pars` for parse errors, `"NoNewDeploys"` for propose contention).

**Scope:**
1. Define `ErrorCode` enum in `DeployServiceV1.proto` — `PARSE_ERROR`,
   `INSUFFICIENT_PHLO`, `NO_NEW_DEPLOYS`, `PROPOSE_CONTENTION`,
   `INVALID_PHLO_PRICE`, etc.
2. Update ~17 gRPC error handlers in f1r3node-rust (`deploy_grpc_service_v1.rs`,
   `block_api.rs`).
3. Update `F1r3flyClientException` in pyf1r3fly to expose the code.
4. Update tests to match on codes instead of strings.

**Context:** [f1r3node#472](https://github.com/F1R3FLY-io/f1r3node/pull/472)
improved error logging but did not add codes. Docs: `docs/node/README.md`,
`docs/rnode-api/index.md`.

### 4.2 Node API improvements for client robustness

| # | Concern | Impact |
|---|---------|--------|
| 1 | `valid_after_block_number` is a minimum, not a state guarantee. Multi-parent blocks may not include block N in their lineage. | Flaky sequential deploys (create → save → deploy). Document or add `requires_block_hash`. |
| 2 | `explore-deploy` always evaluates against latest tip. `/api/explore-deploy-by-block-hash` exists but observer behavior unverified. | Clients can't read consistent state. |
| 3 | Observer `explore-deploy` can return stale state even when `last-finalized-block` reports a newer block. | Read-after-write window bug. |

### 4.3 Monitoring follow-ups

- Rust metric dashboard queries
  ([system-integration#22](https://github.com/F1R3FLY-io/system-integration/pull/22)):
  `f1r3node.json` panels still use Scala Kamon names (`rchain_*`); Rust uses
  `metric_name{source="f1r3fly.*"}`. Blocked on
  [f1r3node#405](https://github.com/F1R3FLY-io/f1r3node/pull/405).

---

## 5. Reference Material

### 5.1 Rust/Scala node incompatibilities (snapshot 2026-03-26)

Affects embers and scoped test scripts. Not actively worked.

**Missing system contracts on Rust node:**
- `rho:registry:insertRandom` — no value set; not on Rust standalone or shard.
- `rho:crypto:secp256k1Sign` — no value set; cannot do Rholang-native signing.

**Missing on Scala `dev` node** (same two contracts).

**Peek (`<<-`) operator:** works on both Rust and Scala standalone (prior reports
of breakage were misdiagnosed insufficient-phlo failures).

**Consume+resend pattern hangs** on both Scala and Rust standalone — worse than
peek. Not a viable workaround.

**`node_cli deploy-and-wait` finalization detection** — works on Rust shard,
intermittently hangs on Rust standalone. Doesn't affect embers (uses HTTP
polling).

**`rho:deploy:data` format on Scala dev** — `for(@timestamp, @deployerId,
@deployId <- deployDataCh)` doesn't fire on Scala dev. No error. Needs
investigation.

**`rho:registry:insertSigned:secp256k1` on Scala dev** — `rs!()` finalizes but
`rl!()` returns empty. Possible explore-deploy state visibility issue.


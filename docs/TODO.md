# F1R3FLY Integration TODO

Living document. Strategic priorities and asi-notes triage live in [`roadmap.md`](roadmap.md).

This file holds **integration-side work** (test framework, compose config, docs)
and **bug observations not yet filed as GitHub issues**. Cross-repo bugs are
tracked in [F1R3FLY-io/f1r3node Issues](https://github.com/F1R3FLY-io/f1r3node/issues)
— this file does not duplicate them, only links.

---

## 1. Test Suite Baseline

Last full runs: **2026-05-01** on `refactor/integration-test-framework@32ed01c`
against `rust/staging@96d81971` (post-#488 merge) via subprocess provider — 3
clean wall-clock runs at **91 passed / 0 failed** in 26:36 / 27:04 / 26:58.

Earlier reference points retained for context:

- §1.1 (Gate 1.1) on 2026-04-28: 92 passed / 2 failed in 34:36 (Docker provider).
- Subprocess+`rust/staging@bfaa2c89` spot-check 2026-04-30: `test_shard_degradation`
  graduated out of deselect (3/3 stable at ~647s, § 2.5);
  `test_finalization_asymmetric_bonds` confirmed as a real observer FT-cache
  divergence bug (§ 2.1).

**Binary:** built from `services/f1r3node-rust-baseline` checkout pinned to
`rust/staging@96d81971`. Path passed via `F1R3FLY_NODE_BINARY` env var when
the default build (`services/f1r3node-rust/target/release/node`) is on a
different branch.

**Canonical run command:**

```bash
poetry run pytest \
  --provider=subprocess \
  integration-tests/test/tests/shared/ \
  integration-tests/test/tests/custom/ \
  integration-tests/test/tests/standalone/ \
  --deselect integration-tests/test/tests/custom/test_load.py \
  --deselect integration-tests/test/tests/shared/test_convergence.py::test_network_converges_after_slow_deploy \
  --deselect integration-tests/test/tests/custom/test_asymmetric_bonds.py::test_finalization_asymmetric_bonds \
  --deselect integration-tests/test/tests/shared/test_convergence.py::test_ft_convergence \
  --deselect integration-tests/test/tests/shared/test_bonding_validators.py \
  --deselect integration-tests/test/tests/custom/test_joiner_self_proposes_at_epoch_boundary.py \
  --deselect integration-tests/test/tests/shared/test_observer_lfs_sync.py \
  -v --tb=short --instafail --maxfail=10 -n auto --dist=loadgroup --timeout=1200
```

`--instafail` (from `pytest-instafail`) prints each traceback the moment a test
errors or fails, instead of buffering until session end — under `pytest-xdist`
the controller normally serializes all tracebacks to the final summary, so a
session-scoped fixture failure on the `@shared` worker can hide for 25+ minutes
while every dependent test errors with the same message. `--maxfail=10` is a
storm-stop: a fresh fixture-failure cascade aborts the run after the 10th
ERROR (~10s) instead of grinding through all 39 `@shared` tests.

> **`--provider=subprocess` is required.** The pytest default is `docker`,
> which ignores `F1R3FLY_NODE_BINARY` and spawns the
> `f1r3flyindustries/f1r3fly-rust:latest` image instead. To exercise a
> locally-built binary on a feature branch, you must pass `--provider=subprocess`
> AND set `F1R3FLY_NODE_BINARY=/abs/path/to/target/release/node` (or rely on
> the default lookup at `services/f1r3node-rust/target/release/node`).
> The 91/0 baselines above are all subprocess runs.

Wall-clock is dominated by the `@shared` and `@custom` tails: `loadgroup` pins
all `@shared` tests to one worker and all `@custom` to one worker, so xdist
workers idle once the standalone fan-out finishes. Today's runs land in the
26-27 min band consistently.

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
| ❌ | `custom/test_joiner_self_proposes_at_epoch_boundary` | 1 | — | WIP test — added in `7ae0c9c`, not yet expected to pass. Re-include when the underlying joiner self-propose flow is finished. |
| ❌ | `shared/test_observer_lfs_sync::test_observer_lfs_sync_against_active_shard` | 1 | — | WIP test — added in `7ae0c9c`, not yet expected to pass. Re-include when the LFS observer-sync flow is finished. |
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
| ✅ | `shared/test_contract_lifecycle.py` | 7 | — | Re-enabled 2026-05-10: PR #491 (bitmask-OR mergeable channels for registry concurrency) landed on `rust/staging` (`915eac58`). |

**Roll-up:** 95 passing fast ✅ + 4 passing slow 🐢 + 5 deselected ❌ + 0 blocked ⏳ + 2 permanent exclude 🚫 = 106 tests across 24 files.

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

**See also:** [`real-flakes-tracker.md`](real-flakes-tracker.md) #4c — `test_validator1_pay_validator2` hits the same FT=±0.3333 stuck pattern on the validator side. Same `propagate_ft_to_finalized_blocks` gap as §2.1; resolves together when the FT-propagation fix lands.

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

**See also:** [`real-flakes-tracker.md`](real-flakes-tracker.md) #3 — different failure mode in the same test (validator3 exits before reaching Running during custom-shard startup). Tracked separately under §2.19.

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

**See also:** [`real-flakes-tracker.md`](real-flakes-tracker.md) #1 — CI Docker provider reproduces the load-driven finalizer-stall at 30-50% on arm64, 10-20% on amd64 across attempts 4-6. Related family: tracker #4a (`test_trim_state` LFB=0), #4b (`test_transfers_interleaved_with_queries` deploy finalization timeout). The throughput keystone fix (§2.18 #9 `has_new_parents` on deploy-trigger) plausibly closes all of them by removing the propose-rate amplifier under sustained load.

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

### 2.11 `shared_shard` fixture readonly-startup race causes baseline cascade failures

The session-scoped `shared_shard` fixture in `conftest.py` calls `provider.create_shard(config)` which spins up boot + 3 validators + readonly. Intermittently, the **readonly node** fails to transition `Initializing → Running` during the ApprovedBlock handshake — `wait_for_node_running` raises, the fixture errors, and **every** `@shared`-grouped test that runs in that pytest session ERRORs at setup.

**Observed (2026-05-02 baseline runs):**

- Baseline 1 (no `-x`): all **65 @shared tests errored** at setup with the same root cause
- Baseline 2 (`-x` flag): `-x` halted at first 2 failures so only 1 cascaded ERROR was visible, but the underlying fixture failure was identical

**Last log lines from the broken readonly:**

```
"Starting to request ApprovedBlockRequest"
"Creating new F1r3fly channel to peer rnode://...@127.0.0.1?protocol=41000&discovery=41004"
[exit before next expected event]
```

The readonly process exits during the ApprovedBlock receive, before logging `"ApprovedBlock is signed by …"` or transitioning to `Running`. Same family as §2.12 (validator-startup-stalls under `RUST_LOG=debug`) but distinct trigger — this happens at default `info` level under no contention.

**Frequency:** ~10-20% of fresh `shared_shard` setups in our environment. Not deterministic; user's reference baseline runs on the same hardware achieved 91/0 cleanly multiple times.

**Cascade impact:** because `shared_shard` is session-scoped, one fixture failure invalidates all 60+ `@shared` tests in the run. Wall-clock cost is full ~26 min if `-n auto` is in flight (xdist workers complete in-flight tests before honoring `-x`).

**Suggested investigation paths:**

- Add retry logic in `provider.create_shard()`: if any node fails `wait_for_node_running`, tear down + retry once (most flakes resolve on second attempt). Cheap fix that absorbs the noise.
- Investigate `comm/src/rust/transport/grpc_transport_receiver.rs` subscription handler for a race with the `restore_approved_state` path — same suspect as §2.12.
- Increase `node_startup` timeout in heavy-parallel runs.

**Impact:** PR #491 baseline runs intermittently fail to verify the @shared group, blocking confidence in the integration suite. Workaround until fixed: retry the baseline run on cascade failure (the readonly boots cleanly on subsequent attempts ~80% of the time).

**See also:** [`real-flakes-tracker.md`](real-flakes-tracker.md) #3 — same "node exits before Running" family in a different fixture (validator3 in custom shards). Tracked under §2.19. Likely the same race in the comm/transport subscription path.

---

### 2.12 Open question: Is the `BlockProcessor` deferred-validation path still needed after Layer G?

**Background.** The prior session (2026-05-05) introduced a `MissingMergeableEntry` typed routing chain in the `BlockProcessor`: when `validate_with_effects` encountered a block whose parent's mergeable entry wasn't local, it would fire a `MergeableEntryRequest` to peers and keep the block in the casper buffer for retry. This required threading a shared `pending_blocks_by_missing_entry` map through the engine, adding `Running::handle_mergeable_entry_response`, retry budget + cooldown logic, NodeDiscovery unicast (Bug Fix C), and ~360 LOC of supporting infrastructure across `block_processor.rs`, `running.rs`, `engine.rs`, `initializing.rs`, and node setup wiring.

**The hypothesis at the time** was that validators were returning empty `serialized_entry` bytes (Layer G symptom) so the LFS-sync mergeable-entry path was dropping data — joiners then needed a Running-engine retry path to recover. The deferred path was the proposed safety net.

**This session (2026-05-06) found and fixed Layer G's true root cause** — a key-encoding mismatch in `KeyValueTypedStoreImpl::raw_get`/`raw_put` (since fixed by removing those methods and rerouting through the typed path). With Layer G fixed, validators now serve real entries on every request (verified ~190–500 entries per B5 run, 100% non-empty).

**The open question:** does the deferred-validation path still solve a real problem post-Layer-G, or is it now dead code? Across **eight** B5 runs since Layer G fix (4 isolation, 4 stability, 0 in-suite), the deferred path **never fired**: `validation deferred = 0` on every joiner, every run. Joiners imported entries cleanly via the LFS path (Phase 4 of the LFS sync flow) and never encountered a Running-state missing-entry condition.

**Two possible reads:**

1. **Layer G alone is sufficient.** With validators serving real entries, the LFS-sync path delivers everything joiners need; the BlockProcessor deferred path is solving a problem that no longer exists. Action: revert the deferred-path infrastructure (Layers A–F from prior session), keeping just Layer G + its supporting `_bytes` accessors.

2. **The deferred path is a real safety net for a rare race.** Even with Layer G fixed, there's a theoretical window where a block can arrive in Running before its parent's mergeable entry is fetched (e.g., gossip outpaces LFS, or a peer comes online late and joins mid-stream). The deferred path catches this; the fact that B5 doesn't trigger it means our test coverage is incomplete, not that the path is unnecessary. Action: keep the path, write a test that exercises it deterministically.

**Suggested investigation:** instrument the deferred-path code with metrics counters (`validation_deferred_total`, `mergeable_entry_request_sent_total`, `mergeable_entry_response_imported_total`) and run a longer/heavier test scenario that's more likely to trigger gossip-vs-LFS races. If counters stay zero across realistic load, lean toward read #1 (revert). If even one event fires, lean toward read #2 (keep + add coverage).

**Where to find the code:** `casper/src/rust/blocks/block_processor.rs` (`request_missing_mergeable_entry`, `register_pending_for_mergeable_entry`, `validate_with_effects` `MissingMergeableEntry` arm), `casper/src/rust/engine/running.rs` (`handle_mergeable_entry_response`), `casper/src/rust/engine/engine.rs` (`pending_blocks_by_missing_entry` plumbing).

**Reference:** [services/f1r3node-rust subrepo working tree](services/f1r3node-rust/) — both prior session and current session work uncommitted; see [docs/session-context-2026-05-05-mergeable-entry-running-engine.md](session-context-2026-05-05-mergeable-entry-running-engine.md) for the original Layers A–F implementation rationale.

**Deferred fault-injection plan:** [docs/missing-mergeable-entry-recovery-test-plan.md](missing-mergeable-entry-recovery-test-plan.md) — full 5-phase scope (Rust test-tools binary + Python helper + integration test + doc + experiment) for empirically answering the keep-vs-drop question via LMDB-level fault injection. ~2 days. Deferred 2026-05-08 in favor of bonding-validator fix; resume when PR #3 stabilizes or production observes a missing-entry-class failure.

### Update 2026-05-09: PR #2 dropped the deferred-validation infrastructure

PR #2 ([f1r3node#508](https://github.com/F1R3FLY-io/f1r3node/pull/508)) cherry-picked only the cascade-invalidate + ReplayCache mergeable-entry safety check from the prior `feat/d-thin-mutex-state` work, **explicitly excluding** the ~360 LOC of deferred-validation infrastructure (typed `BlockError::MissingMergeableEntry`, `pending_blocks_by_missing_entry`, retry budget + cooldown, NodeDiscovery unicast, Running engine handlers).

Rationale: 8 B5 runs since Layer G fix observed `validation deferred = 0` on every joiner, every run. Empirical YAGNI — read #1 ("Layer G alone is sufficient") chosen.

The fault-injection plan in [docs/missing-mergeable-entry-recovery-test-plan.md](missing-mergeable-entry-recovery-test-plan.md) remains valid as a reactivation lever — if production observes a missing-entry-class failure, run the plan to validate that re-introducing the deferred path actually catches it.

---

### 2.15 Joiner silently dropped from bonds map after first block (proposes once, then dead)

**Symptom (B5 v19, 2026-05-06, session `1731ed06`, subprocess provider, `feat/d-thin-mutex-state` head):**

V4 (joiner1) bonds successfully and finalizes cross-node — bond block `#4` (`85f5030d619b2294`) carries `bonds=4` including V4 (`04d26c6103d72697`), `isFinalized=True` on every node. V4 then produces its first block `#8` (`8f11667eac858363`) post-activation. Test sub-phase 6 logs "Joiner validator4 proposed block #8; finalized on all nodes". Sub-phase 7 logs "V1 block #12 justifies validator4". Then sub-phase 8 (post-bond liveness V1 deploy) times out at 50s.

Live shard inspection after the failure (LFB#115, ~2 min after timeout):

```
v1 LFB#82 bonds=3:    V1, V2, V3 — V4 missing
V4 produced 0 of last 85 blocks — block #8 was V4's first AND last
Block #8 (V4-proposed, isFinalized=true): bonds=3, V4 NOT in its own block's bonds
```

V4 was bonded at `#4`, produced one block at `#8`, then was silently dropped from the active validator set. No `InvalidBondsCache`, no `Recording invalid block`, no forbidden-pattern signals. V4's process is alive and reachable (gRPC responds), but it stops appearing in bonds maps and stops proposing.

**Distinct from the V5 second-bond `InvalidBondsCache` failure** documented in memory `project_bonding_bug.md` and `bonding-bug-test-plan.md`. That failure is a *hard rejection* with explicit cascade signals; this one is a *silent drift* with no error logs. Same bug family (PoS bonds-cache state divergence) but different surface manifestation.

**Why this is the first time it's surfacing:**
- Pre-enhancement B5 was 4/4 PASS (per session report) — the enhanced test's bg load adds timing variance that tickles this case at higher rate.
- The original test's only post-bond-block bonds check is at V5's bond block (assertion `expected_bonds_after=5`). When V4 silently disappears mid-Phase-A, V5's bond block ends up with `bonds=4` (V5 + V1 + V2 + V3, no V4) — the V5 bond would FAIL on count assertion before reaching the cross-node check. But here the failure was earlier (sub-phase 8 deploy timeout) so we never reached V5 bonding.
- Three runs of the enhanced test: v17 PASS, v18 PASS, v19 FAIL → ~67% pass rate. v17/v18 had V4's first block at #10/#11 (post-epoch); v19 had it at #8 (right at epoch boundary). Epoch-boundary timing appears load-bearing.

**Suggested investigation areas (node-side):**
- PoS contract's bond-application path during epoch transition. If V4's bond is applied to the active set at the epoch boundary, but V4 also produces a block AT the epoch boundary, there may be a race where the block is built against pre-epoch state (V4 not yet active per local view) but accepted by peers (who see V4 as active in their view).
- `quarantine-length` interaction with `epoch-length` — the test config uses `epoch-length=4, quarantine-length=10`. V4's bond at #4 has quarantine until ~#14. If V4 is producing at #8 it's producing IN quarantine — that may itself be the bug.
- Compare v17/v18 (V4 first block post-epoch, stable) to v19 (V4 first block AT epoch, dropped). Whatever activation logic differs at the boundary is the mechanism.

**Repro recipe:**
```bash
poetry run shardctl test-reset
F1R3FLY_NODE_BINARY=services/f1r3node-rust/target/release/node \
  poetry run pytest integration-tests/test/tests/shared/test_bonding_validators.py::test_bonding_validators \
  --provider=subprocess -v --keep-running
```
Failure rate ~33%. After failure, query v1's `/api/last-finalized-block` and the bond block by hash — bond block carries 4 bonds, current LFB carries 3. Query `/api/blocks/30` and count V4-prefixed senders — usually 0 or 1.

**Current test workaround:** Marker opts out of `DAGStorageMissingHash`, `KvStoreError`, `RootRepositoryDivergence` (the §2.14 noise). With this marker, B5 is 5/5 stable in this session's verification sweep — but only because §2.15 didn't fire (V4 didn't land on epoch boundary in those 5 runs). Statistical fire rate ~25% under heartbeat-driven scheduling.

**Status:** Reproducible but not deterministic. Different surface from §2.13 (sustained-load fork-choice divergence) and `project_bonding_bug.md` (V5 second-bond hard rejection); related family.

### Update 2026-05-07: Reproduction attempts exhausted

12+ attempts in this session to deterministically reproduce §2.15:

| Attempt | Conditions | Result |
|---|---|---|
| Stability 1-5 | Default `test_bonding_validators`, bg interval 5.0s | All 5 PASS, V4 first block at #10/#11/#12 (post-epoch) |
| Hunt 1-2 | Same defaults | PASS, V4 at #10 both times |
| Hunt 3 | bg interval 2.0s (heavier) | PASS until Phase B sub-phase 1 timeout |
| Focused test, epoch_length=1, V4 heartbeat-on | V4 at #3 (immediately) | PASS — V4 producing every block, no idle window |
| Focused test, epoch_length=1, V4 heartbeat-off, idle window without bg | V4 at #9 | PASS — V4 in bonds even with idle period + multi-parent |
| Focused test, epoch_length=1, V4 idle + bg load on V1/V2/V3 | LFB stalled at #10 (§2.13 fired instead) | Couldn't reach §2.15 conditions |

**Conclusion:** §2.15's exact trigger is more subtle than the obvious shape ("V4 idle + first block at epoch boundary + multi-parent merge"). The conditions involve specific gossip ordering / heartbeat-check race timing that manual control can't replicate, and that even heartbeat-driven repeats don't reliably hit.

**Why v19 fired but my repro attempts don't:** v19 had `bg_load = 7 deploys` (light) + heartbeat-driven proposes by all 4 validators, and V4's first heartbeat-fire happened to land at the next-up-proposer slot for height #8 specifically. Pure timing chance under heartbeat scheduler.

**The deeper investigation already exists.** Branch `services/f1r3node-rust@fix/bonding-stability` (commit `cd9405d3`) has [D-*]/[F-*]/[G-*]/[H]/[I]/[GP-*] diagnostic tracing across `validate.rs::bonds_cache`, `runtime_manager`, `runtime.rs`, `rspace.rs`. That branch is parked WIP from the prior bonding-bug investigation (project memory `project_bonding_bug.md`). Resume work there to dig into asymmetric mergeable-channel state-loading.

**Decision:** §2.15 is **deferred to a follow-up PR**. Reasons:
1. Reproduction requires multi-hour investigation beyond this PR's scope
2. The same bug class is already documented in `project_bonding_bug.md` (V5 second-bond)
3. The PR's load-bearing commits (Shape A + D-thin + LFS mergeable sync) DO address the bug class structurally — they just don't fully close every surface variant
4. Current B5 marker absorbs the §2.14 noise; §2.15's silent variant is rare under realistic test conditions
5. Completing the §2.14 multi-layer fix (extend LFS to fetch BlockMetadata + mergeable entries for side-branch blocks) likely also closes §2.15 — they're the same root cause from different angles

**Next-PR work:**
- Resume `fix/bonding-stability` branch tracing under reproducible conditions
- Implement §2.14 layers 1+2 (DAG metadata + mergeable channel fetching for side-branches)
- Verify by running enhanced B5 with old vs new binary across 10+ runs to compare statistical fire rates

### Update 2026-05-07: deterministic repro attempted, conditions narrowed

A new test [`tests/custom/test_joiner_self_proposes_at_epoch_boundary.py`](../integration-tests/test/tests/custom/test_joiner_self_proposes_at_epoch_boundary.py) was built to deterministically trigger §2.15 via manual propose control (heartbeat disabled). Six variants attempted, all PASS:

1. Linear single-parent propose, V4's first block at #8 epoch boundary.
2. Concurrent multi-parent rounds (3 forks per height at #5/#6/#7).
3. V4 lagging (skipped intra-round visibility waits).
4. Continuous bg proposers on V1/V2/V3 (40+ deploys, varying timing).
5. (4) + V4 multi-iteration scan: 12 sequential V4-proposed blocks, 4 of them on epoch boundaries (#16, #20, #24, #28). Bug fires on zero.

**Conditions ruled OUT as sufficient:**
- Joiner producing its first block at an epoch boundary alone.
- Multi-parent merge structure in the joiner's parent set at the boundary block.
- Bg-load chaos in V1/V2/V3's mempools/proposes.
- V4's local view being stale at propose time.

**Conditions still suspected as necessary:**
- Heartbeat-driven proposing specifically (the actor-message timing race in the heartbeat-check → propose pipeline).
- FTT-based finalization (FTT=0.1 vs FTT=-1 may interact with bonds-cache reads in non-obvious ways — not yet tested in isolation).

**Implication:** The bug surface is narrower than the broad architectural condition I initially hypothesized. It's **most likely a real consensus-runtime race** between heartbeat's propose-decision and rspace state mutations during epoch transition — the same family as the V5 second-bond bug ("requires asymmetric state-loading that only manifests in real cross-process production"). Manual propose elides whatever race condition heartbeat creates.

The negative-control test is preserved as a forward-regression: when §2.15 is fixed, this test continues to pass, confirming the deterministic shape stays correct. For the actual flake repro, `test_bonding_validators` (heartbeat=True + bg load) remains the only path; surfaces §2.15 ~33% of the time.

### Update 2026-05-10: fixed by PR #3 (on branch; not yet on staging)

PR #3 (`feat/bonding-additiveset` on f1r3node) introduces a new `MergeType::AdditiveSet` merge primitive whose combine function unions two Datums' Map payloads key-wise and breaks per-key conflicts via lowest-bincode LWW. PoS.rhox is rewired to hold the canonical bonds map on a dedicated `bondsCh` channel tagged with `additiveSetMergeableTag`; the `bond` handler dual-writes (legacy `state.allBonds` + new `bondsCh`), and `getBonds` + `pickActiveValidators` read from `bondsCh`. Multi-parent merge of concurrent bond writes now key-unions through AdditiveSet instead of LWW-dropping all but one branch's contribution through MutexState — which was the exact mechanism by which V4 silently disappeared.

Verification: B5 5/5 stable on PR #3 binary. In every run V4 bonded successfully (4-entry cross-node bonds map at the V4 bond block), then V5 bonded successfully (5-entry cross-node bonds map at the V5 bond block) — neither validator ever silently disappears. Mean run time 379s, range 367-391s, zero panics, zero `InvalidBondsCache`.

The §2.15 mark can be cleared once PR #3 lands on `rust/staging`. Slash and withdraw paths still read the legacy `state.allBonds` mirror — out of scope for this fix; their bonded-state reads will see stale views post-merge until those handlers are migrated to read `bondsCh` too. Operationally those paths are already broken in multi-parent merge regardless.

---

### 2.14 LFS only syncs DAG metadata for LFB-ancestor blocks; side-branch blocks within horizon are missing

**Symptom (B5 v7 isolation run, 2026-05-06, session `60b2dbc2`, subprocess provider, `feat/d-thin-mutex-state` head):**

A fresh readonly observer attached via `Shard.attach_observer()` after both V4 and V5 are bonded reaches `Running`, syncs LFB cleanly, and serves reads. But within seconds it begins emitting `DAGStorageMissingHash` errors at high rate (3550 in the test window):

```
[FATAL] KvStore failure: Error processing block 961765144e...:
  KvStore error: Invalid argument: DAG storage is missing hash 2760ab77bc...
```

Hash `2760ab77bc...` = block #10, V4's first proposed block from Phase A. V4 produced it during the V4 activation phase, before V5 bonded; subsequent fork-choice picked a different chain that didn't include #10 as a direct ancestor of any LFB. The observer LFS-synced LFB-ancestor blocks correctly, then received gossip for newer blocks whose justifications point at descendants of #10 — validation walks DAG metadata back through justifications, hits missing #10, fails.

**Diagnosis.** `lfs_block_requester` (in `services/f1r3node-rust/casper/src/rust/engine/lfs_block_requester.rs`) walks **back from LFB via parent links** to populate the joining node's block store + DAG metadata. It only fetches blocks reachable from LFB's parent chain. Side-branch blocks within `max_parent_depth + depth_buffer` of LFB — blocks that an honest proposer might still reference as a parent of an upcoming block, and that newer gossip blocks may justify — are **not fetched** during LFS. When the joining node enters `Running` and starts validating gossip-arriving blocks, those blocks' justifications can point at side-branch ancestors the joiner doesn't have.

**Sibling to the rspace forward-horizon gap fixed this session.** This session's `lfs_horizon_requester.rs` (committed in working tree on `feat/d-thin-mutex-state`) addresses the **rspace state** version of this problem: rspace history for every block in the forward horizon. The DAG metadata case (`DAGStorageMissingHash`) is the same shape but for a different storage layer:

| Storage | Symptom | Fix landed? |
|---|---|---|
| Rspace history (`RootRepository`) | `UnknownRootError` on sibling-of-LFB validation | ✅ This session: `lfs_horizon_requester` + `compute_forward_horizon_roots` |
| DAG metadata (`KeyValueDagStorage`) | `DAGStorageMissingHash` on gossip validation | ❌ Sibling gap, not addressed |

**Why this hadn't surfaced before:** No prior test attached a fresh observer **after** the shard had built up multi-bond fork history. V4 and V5 (joiners with validator identities) attach when the shard is small and quiet — LFB ancestry covers everything they need. The Phase C `attach_observer()` added in this session is the first test that exercises a production-shape sync against a busy shard.

**Same family as:**
- §2.13 (sustained-load fork-choice divergence) — both surface only with realistic load + multi-validator activity
- The rspace forward-horizon work this session — same architectural fix shape needed (proactive sync of horizon-window storage)

**Suggested fix shape (mirror this session's rspace work):**
1. New reachability calc: `compute_forward_horizon_block_hashes(dag, lfb, casper_shard_conf)` — every block hash within `max_parent_depth + depth_buffer` of LFB, including side branches (use `dag.topo_sort(min_height, lfb_height)` like `compute_forward_horizon_roots`).
2. New orchestrator step in `lfs_block_requester.rs` (or a sibling `lfs_horizon_block_requester.rs`): for each horizon block hash, ensure block + metadata are in local store; fetch from peers if missing.
3. Wire into `request_approved_state` between `populate_dag` and `sync_forward_horizon`.
4. Validator-side: extend the `validate::parents` depth check (this session's work) to also reject blocks whose **justifications** include hashes outside the horizon — symmetric to the parents check.

**Test workaround applied (2026-05-06, in `tests/shared/test_bonding_validators.py`):** marker opt-out for the `DAGStorageMissingHash` forbidden pattern on this test, with `# Reason: TODO §2.14` reference. Phase C's actual semantic assertions (observer LFB visible, bonds map consistent on observer, LFB lag ≤ 1 block) still execute and verify what the observer **can** do correctly. **The workaround masks the underlying bug — anyone running a fresh observer in production against a busy multi-bonded shard will hit this.**

**Repro path:**
1. Bring up a 3-validator shard (V1/V2/V3 + readonly).
2. Bond V4 via V1, run V4's first block + post-bond liveness (creates side-branch DAG history).
3. Bond V5 via V2, run V5's first block + post-bond liveness.
4. Attach a fresh readonly observer.
5. Observe: observer reaches `Running`, then emits `DAGStorageMissingHash` on every gossip block whose justifications reference V4's #10 (or any other side-branch block).

**Status:** Confirmed reproducible. Test workaround in place. Node-side fix not started. Likely candidate for a follow-up session as a direct extension of the LFS forward-horizon work landed this session.

### Update 2026-05-07: bug surface is broader than DAG metadata alone

Investigation of a `RootRepositoryDivergence` error during a Phase C observer attach (session `4c06dea7`) revealed the §2.14 bug class spans **three storage layers**, not just DAG metadata:

```
ERROR validateAndSetCurrentRoot FAILED: a9cd631af310... not in roots store
```

Tracing the missing hash: it's NOT any block's post-state hash. It's the **computed multi-parent merged pre-state** for V5-proposed block #63, which had 5 parents (4 at height-62 + 1 at height-60). Multi-parent merge requires mergeable channel data for EACH parent to compute the merged result. Observer's LFS sync didn't cover all mergeable entries for side-branch parents → merge fails to produce the expected `a9cd631af310...` → not in roots store.

**Three layers affected by the LFS main-chain-only gap:**

1. **BlockMetadata (DAG storage):** `DAGStorageMissingHash` — justification walks fail when they reference side-branch blocks
2. **Mergeable channel entries:** `RootRepositoryDivergence` — multi-parent merge fails when it needs side-branch mergeable data
3. **Rspace history roots:** partially closed by this session's `lfs_horizon_requester` (covers ancestor roots below LFB) but the merge-pre-state case in (2) still leaks through

**Implication for the fix:** A complete §2.14 fix needs to extend LFS to cover the same forward-horizon for **all three** storage layers. This session covered storage layer 3 only. Layers 1 and 2 remain.

The `compute_forward_horizon_roots` reachability calc this session built is the right starting point — it identifies every block reachable within `max_parent_depth + depth_buffer` of LFB. Extension work:
- For each horizon block, also fetch BlockMetadata into local DAG storage (closes layer 1)
- For each horizon block, also fetch the mergeable entries keyed by `(post_state_hash, creator, seq_num)` (closes layer 2)

Both extensions mirror the `lfs_horizon_requester` orchestrator pattern, just routing fetches through different storage backends.

**Test marker:** `tests/shared/test_bonding_validators.py` opts out of all three patterns (`DAGStorageMissingHash`, `KvStoreError`, `RootRepositoryDivergence`) until §2.14 is fully fixed.

### Update 2026-05-10: all three layers addressed by PR #1 + PR #3 (on branches; not yet on staging)

| Layer | Fix | Branch |
|---|---|---|
| 1 — DAG metadata (`DAGStorageMissingHash`) | `lfs_block_requester` lower_bound extended to `min(LFB - deploy_lifespan, LFB - max_parent_depth - depth_buffer)` so all horizon blocks are fetched, not just LFB-ancestor chain | `feat/lfs-coverage` ([f1r3node#507](https://github.com/F1R3FLY-io/f1r3node/pull/507)) |
| 2 — Mergeable channel entries | Per-block mergeable-channels sync during LFS (cherry-pick of `f33f553e` from prior d-thin work) + horizon-mergeable phase in PR #3 | PR #1 + PR #3 |
| 3 — Rspace history roots (`RootRepositoryDivergence`) | `lfs_horizon_requester` orchestrator + `compute_forward_horizon_roots` reachability calc; PR #3 fixed a multi-root pagination collision (`HashMap<Path, HashSet<Hash>>` so shared cursors satisfy all roots, not just last writer) | PR #1 + PR #3 |

Verified end-to-end against PR #3 binary: B5 5/5 stable, zero `RootRepositoryDivergence` / `DAGStorageMissingHash` / `KvStoreError` patterns across all five runs (mean 379s, range 367–391s). The `tests/shared/test_bonding_validators.py` marker for these three patterns can be removed once PR #1 + PR #3 land on `rust/staging`.

**See also:** [`real-flakes-tracker.md`](real-flakes-tracker.md) #2 — joiner emitting `RootRepositoryDivergence` mid-sync on `test_joiner_matching_config_succeeds` is the same Layer-3 symptom. CI Docker reproduces at ~10% on arm64 against current `rust/staging`. Resolves automatically when PR #1 + PR #3 land on staging.

---

### 2.13 Sustained concurrent producer load → fork-choice divergence prevents per-block finalization

**Symptom (B5 v5 isolation run, 2026-05-06, session `1bcc3235`, subprocess provider, `feat/d-thin-mutex-state` head with `max-parent-depth=100`):**

While `test_bonding_validators` runs a background-load thread issuing round-robin deploys at V1/V2/V3 (interval 2.0s per producer ≈ 1.5 deploys/sec total) throughout Phases A and B, the joiner V4's first proposed block (#15, sub-phase 6) cannot finalize cluster-wide:

```
Block ef93ec... not finalized on 3 node(s) after 225s:
  validator1: block_number=15, faultTolerance=-1.0
  joiner1:    block_number=15, faultTolerance=-1.0
  readonly:   block_number=15, faultTolerance=-1.0
```

V2 and V3 do finalize #15 (FT > FTT). V1, the joiner itself, and the readonly observer cannot — `faultTolerance=-1.0` (uncomputable). `wait_for_finalized` succeeded against the joiner (LFB advanced past 15), but per-block `isFinalized` never propagated to the V1-side gossip subgraph.

**Diagnosis.** Under sustained concurrent producer load, V1/V2/V3 each produce side-branch tips after V4's #15 lands. Because each producer's round-robin propose preferentially extends its own recent tip (and bg deploys keep mempools full so heartbeat fires every cycle), the DAG splits into two effective sub-clusters — V2/V3 build chains that include V4#15 in their justifications, V1 builds chains that don't. V1's `Estimator` keeps electing tips that side-step #15, so #15 never accumulates V1-stake-weighted FT from V1's perspective. The joiner and readonly inherit V1's view (they're upstream of V1's gossip).

**Same symptom family as:**
- §2.2 (orphan-branch FT propagation: finalized block becomes unreachable from future LFBs)
- §2.5 (`test_shard_degradation` finalizer-stall after sustained load — Stacy/Alexander v0.4.9 production reports)
- Project memory `project_shard_degradation.md`

**This is the root cause that 2.5 hinted at.** §2.5 noted "subprocess provider runs cleaner than Docker — re-verify under load before declaring closed." The B5 v5 finding confirms the underlying bug is real even on subprocess: the finalizer doesn't degrade gracefully under sustained multi-producer load. §2.5's `test_shard_degradation` hits it via single-producer high deploy rate; §2.13 hits it via multi-producer concurrent rate. Same fundamental issue.

**Test workaround applied (B5 v6, in `tests/shared/test_bonding_validators.py:_bond_lifecycle`):** stop the background-load thread between sub-phase 5 (epoch boundary advancement) and sub-phase 6 (joiner first-block finalization). Documented in code with `# Background load's purpose was to stress the joiner's LFS sync...` comment block. **The workaround is correct for the test's intent (LFS-sync stress, not finalizer-load stress) but masks the underlying bug — anyone running this test under sustained background load will hit divergence.**

**Repro path:**
1. Bring up a 4-validator shard (3 genesis + 1 joiner via `attach_joiner`).
2. Run a thread sending round-robin deploys at all 3 genesis validators at ≥1 deploy/sec total.
3. Wait for the joiner to activate at the next epoch boundary and propose its first block.
4. Observe: V2/V3 finalize the joiner's block, but V1 (and any node downstream of V1's view) do not.

**Suggested investigation areas (node-side):**
- `Estimator::filterDeepParents` and tip-selection logic — does it bias toward own-recent tips under heavy mempool load?
- `propagate_ft_to_finalized_blocks` — does it visit blocks that are reachable only from peer gossip, not from local LFB walk?
- `fork-choice-stale-threshold` (`1 minutes` in `rust.conf`) — interaction with rapid tip churn under load
- Whether the synchrony constraint check is silently rejecting V1's proposes when V1's justifications stale relative to V2/V3's

**Status:** Confirmed reproducible. Test workaround in place. Node-side fix not started.

---

### 2.16 Reporter (`block_report` API) replay diverges on LFS-synced READONLY observers

**Symptom (test_observer_lfs_sync + B5 Phase C, 2026-05-09 / 2026-05-10, subprocess provider, observed across rust/staging, PR #1, PR #3):**

A fresh readonly observer attached mid-shard via LFS sync emits panic clusters in `tokio-rt-worker` whenever the gRPC API server's transfer-enrichment path triggers `block_report.trace(block)`:

```
WARN Deploy replay failed, returning empty events
  deploy_index=0  error="System runtime error: Unable to consume results of system deploy"
... (one per deploy in the block)
WARN Discarded 0 replay events during precharge error path

thread 'tokio-rt-worker' panicked at rholang/src/rust/interpreter/rho_runtime.rs:386:14:
called `Result::unwrap()` on an `Err` value:
  BugFoundError("Unused COMM event: replayData multimap has 471 elements left")
```

The reporter creates a fresh `RhoReportingRspace` over the same store, resets to the block's `pre_state_hash`, then for each deploy replays the precharge system deploy. `consume_system_result` returns `None` (no result on the precharge return channel), each deploy's replay aborts, the rspace's `replayData` multimap stays full of unconsumed COMM events, and `create_checkpoint()` panics on the dirty trace.

**Bisect (with the framework's transient-handle scanner gap fix in place):**

| Binary | observer1 panics | Notes |
|---|---|---|
| `rust/staging` (`bf6a03d2`) | 2 (per fresh test_observer_lfs_sync) | **Pre-existing on staging baseline** |
| `feat/lfs-coverage` (PR #1) | 2-4 | Same bug, hidden by transient-observer scanner gap before today's fix |
| `feat/bonding-additiveset` (PR #3) | 2 (in test_observer_lfs_sync); 4 (in B5 Phase C) | Same bug — NOT introduced by PR #2 or PR #3 |

**Why nobody noticed before:** the `add_observer()` context manager removed the observer's handle from `provider.active_handles` BEFORE the autouse scanner ran, so transient observers' panics escaped scanning. Only `attach_observer()` (PERSISTENT) attached observers were ever scanned. B5 Phase C uses `attach_observer()` and DID surface this pattern in earlier B5 runs; that surface was masked by adjacent §2.14 patterns (`RootRepositoryDivergence` / `DAGStorageMissingHash` / `KvStoreError`) until those landed in the same opt-out marker. After PR #1 fixed §2.14 layers 1+2+3, the only remaining marker-needed pattern was the reporter panic — flagged as the §2.14-layer-2 candidate, eventually traced to a separate bug class.

**Bug class (narrowed):**
- Validators don't trigger the reporter (rejected by `validator_opt.is_some() && !dev_mode → ReadOnlyRequired` at [`block_report_api.rs:188`](services/f1r3node-rust/casper/src/rust/api/block_report_api.rs#L188))
- Genesis-attached READONLY (e.g. `shared_shard.readonly`) does NOT panic — has played every block from genesis, full rspace state on disk
- LFS-synced READONLY (e.g. observer attached mid-test) DOES panic — rspace state was reconstructed via the LFS exporter/importer pair
- LFS-synced JOINER (which becomes a validator after bonding) also doesn't panic — once bonded, its reporter is rejected by the validator-check

So divergence is **specific to the (LFS-imported rspace state) × (READONLY = reporter still invoked)** combination. The reporter's per-block precharge replay needs some piece of state that LFS sync doesn't faithfully reproduce.

**What we ruled out at the unit-test level (today, PR #3 worktree):**
- Reporter logic in isolation — `reporting_casper_should_behave_the_same_way_as_multi_parent_casper` PASSES on freshly-played single-node setup
- Basic LFS-import roundtrip — `reporting_casper_works_against_lfs_imported_rspace` PASSES (play one trivial deploy, export trie, import to fresh rspace, run reporter against fresh rspace, post-state hash matches)

So the bug needs more than the simple LFS-import path to trigger. Production conditions that the unit tests don't yet capture: hundreds of blocks of state churn, multi-block heartbeat cadence, multiple deployer keys, mergeable-channel store interactions across many blocks, concurrent reporter invocations.

**Workaround (in place for B5 Phase C):** Pass `--api-enable-reporting=false` when attaching the observer:

```python
observer = shared_shard.attach_observer(
    cli_options={"--api-enable-reporting": "false"},
)
```

This requires PR #3's CLI change — `--api-enable-reporting` now accepts a value (`true`/`false`); bare `--api-enable-reporting` keeps backwards-compatibility as `true`. With reporting disabled, `block_report_api` is constructed with `NoopReportingCasper`, every `trace()` returns empty results, no replay, no panic. B5 5/5 stable with this workaround.

**`test_observer_lfs_sync.py` does NOT apply the workaround** — it stays as the canary surface for whoever fixes the bug. Test body PASSES (observer LFS-syncs cleanly to LFB, drift=0, bonds map consistent), autouse scanner catches the reporter panics and reports `1 passed, 1 error`.

**Where to investigate (node-side):**
- `casper/src/rust/reporting_casper.rs::RhoReporterCasper::trace` — flow that constructs reporter runtime, resets to pre_state, replays deploys
- `casper/src/rust/rholang/replay_runtime.rs::process_deploy_with_cost_accounting` — precharge phase that hits ConsumeFailed
- `casper/src/rust/rholang/runtime.rs::eval_system_deploy` → `consume_system_result` → returns None
- LFS exporter/importer pair: `rspace++/src/rspace/state/exporters/rspace_exporter_items.rs` and `rspace++/src/rspace/state/instances/rspace_importer_store.rs` — does the exporter walk every trie node the reporter needs?
- Compare LFS-synced observer's LMDB contents vs genesis-attached readonly's LMDB at the same root for missing keys

**Suggested next investigation (recorded for whoever picks this up):**
1. Add `tracing::error!` instrumentation in `consume_system_result` printing the channel/pattern on `Ok(None)` to identify exactly which channel observer1 fails to consume from
2. Restart observer1 with the instrumented binary (data dir survives), capture the failing channel
3. Query both observer1 and a genesis-attached readonly for the same channel via gRPC to diff the state
4. The diff identifies the missing/mismatched state → fix at exporter, importer, or post-import reset path

**Status:** Pre-existing on `rust/staging` baseline. Test workaround in place for B5; canary preserved in `test_observer_lfs_sync`. Node-side fix not started.

### 2.17 Cross-crate "dead constants" in `casper::metrics_constants`

Surfaced by [f1r3node PR #502 review](pr-502-review-notes.md) §4.

`casper/src/rust/metrics_constants.rs` defines metric-name constants that are never imported anywhere because the emission site lives in a crate that doesn't depend on `casper`. Verified cases:

- `IS_MERGEABLE_CHANNEL_CALLS_METRIC` — defined in casper, emitted from `rholang/src/rust/interpreter/reduce.rs` as a hardcoded `"is-mergeable-channel.calls"` literal. `rholang` doesn't depend on `casper`, so direct import is impossible.
- `DAG_INSERT_TIME_METRIC` — defined in casper, emitted from `block-storage/src/rust/dag/block_dag_key_value_storage.rs` as a hardcoded `"dag.insert.time"` literal. `block-storage` doesn't depend on `casper` (dependency goes the other way).

**Drift risk:** rename a casper-side constant → emission site keeps the old name silently. Likely more cases beyond the 2 verified — `casper::metrics_constants` has 35 entries, any cross-crate emission is at risk.

**Fix (when picked up):** move each cross-crate constant to the emission crate (or a shared module), keeping the canonical definition co-located with the call site:

- `IS_MERGEABLE_CHANNEL_CALLS_METRIC` → `rholang/src/rust/interpreter/metrics_constants.rs` (already exists for rholang's own metrics).
- `DAG_INSERT_TIME_METRIC` → file-local `const` in `block-storage/src/rust/dag/block_dag_key_value_storage.rs` (block-storage has no other custom metrics; a new constants module is overkill).

If casper ever needs to reference these constants, `pub use` re-export from the owning crate.

**Repo:** f1r3node. **Branch:** `perf/runtime-manager-lock-free` (PR #502).

### 2.18 Throughput optimization levers (load-test driven, 2026-05-07)

After test_load tuning hit 0 unfinalized but high-phase finalization p50 sat at
46.8s (1s margin under the 45s budget), per-block metric tracing identified the
levers below. The merging-logic pair-dedup change (originally items #2 + #3 in
the list) was implemented and reverted in the same session — equivalence-correct
but a small regression on test_load's low-duplication workload (branches +14% in
clean re-run). Remaining items are deferred and tracked here. See session report
[`session-report-2026-05-07-proposer-cadence-and-config-tuning.md`](session-report-2026-05-07-proposer-cadence-and-config-tuning.md).

#### Throughput-tuning settings: confirmed UNSAFE for `defaults.conf` promotion

The four CLI overrides in `tests/custom/test_load.py::global_cli_options` (3s
self-propose-cooldown, frontier-chase-max-lag=20, 3s stale-recovery-min-interval,
max-user-deploys-per-block=128) were trialled in `conf/rust.conf` against the
full baseline suite on 2026-05-07. The baseline run produced four hard
regressions on previously-✅ tests:

| Test | Failure mode |
|------|--------------|
| `shared/test_wallets::test_transfer_failed_with_invalid_key` | `assert_block_finalized_on_all_nodes(blockHash)` returns FT=-1.0 on all 5 nodes |
| `shared/test_wallets::test_transfer_failed_with_insufficient_funds` | Same |
| `shared/test_web_api::test_get_deploy_detail` | `deploy should be finalized` → False |
| `shared/test_web_api::test_is_finalized_http` | `is-finalized` returns `false` on the deploy's blockHash |

All four share one root cause: each test submits a deploy, `wait_for_finalized
(blockNumber)` succeeds, then asserts finalization on the *specific* blockHash
that contained the deploy — but a **sibling block at the same blockNumber
finalized instead**. The chain's LFB advanced via a non-canonical-from-the-
deploy's-perspective branch; the deploy's containing block stays at FT=-1.0.

The two heartbeat overrides (3s cooldown + frontier-chase=20) raise the sibling
rate, which is fine for `test_load` (which tracks deploy IDs, doesn't care
which sibling wins) but breaks any realistic flow that follows a specific
blockHash through finalization. `max-user-deploys-per-block = 128` and the
3s `stale-recovery-min-interval` are unlikely contributors (single-deploy
tests, recovery path not exercised) but the suite was killed before isolating
them — keep all four bundled as test-shard-only.

**Status:** overrides remain in `tests/custom/test_load.py::global_cli_options`
where they enable the load test to drive 0 unfinalized; **NOT** in
`conf/rust.conf` (would apply to every test) or `defaults.conf` (would apply
to mainnet). The `has_new_parents`-on-deploy-trigger graft (item #9 below) is
the precondition for safely raising propose cadence in production.



#### #4 — Read parent timestamps from BlockMetadata

[`casper/src/rust/validate.rs:531-566`](../services/f1r3node-rust-pr488/casper/src/rust/validate.rs) (`Validate::timestamp`) calls
`block_store.get_unsafe(parent_hash)` once per parent — a full BlockMessage disk
read just for one `i64` timestamp field. With wide DAGs (`max-number-of-parents
= 100`) this scales linearly; observed cost is **26.8ms per validated block**
(vs sub-ms for the sibling `block_number` validator next door, which uses
`s.dag.lookup` against in-memory BlockMetadata).

**Cost:** ~860ms saved per high-phase phase (32 blocks × 27ms).

**Why deferred:** the in-memory `BlockMetadata` struct and its proto
`BlockMetadataInternal` (`models/src/main/protobuf/CasperMessage.proto:110-126`)
have no `timestamp` field. Adding one is wire-additive (field 12 unused) but
needs a self-healing fallback for legacy storage rows where the field decodes
as 0.

**Fix shape:**
- Add `int64 timestamp = 12;` to `BlockMetadataInternal`.
- Add `pub timestamp: i64` to the `BlockMetadata` struct + update
  `from_proto`, `to_proto`, `from_block`, `PartialEq`, `Hash`.
- Switch `Validate::timestamp` signature to take `&mut CasperSnapshot` (matches
  `block_number` next door); fast path reads `s.dag.lookup(parent_hash)?.timestamp`;
  fallback when `metadata.timestamp == 0 && parent_block_number > 0` reads
  `block_store.get_unsafe` for legacy rows.
- Update tests in `casper/tests/batch2/validate_test.rs:348+` for the new
  signature; add coverage for both fast path and fallback.

**Risk:** medium — proto-additive change, but PartialEq/Hash on BlockMetadata
extends to a new field which may surface in any test fixture comparing metadata
by struct equality (limited to fixtures that build BlockMetadata literally
rather than via `from_proto`/`from_block`).

**Repo:** f1r3node. **Effort:** ~2 hours including proto regen cascade.

#### #5 — bonds_cache by epoch instead of post-state-hash

[`casper/src/rust/util/rholang/runtime_manager.rs:744-762`](../services/f1r3node-rust-pr488/casper/src/rust/util/rholang/runtime_manager.rs)
caches bonds keyed by `post_state_hash`. Every block has a unique post-state,
so the cache misses on every block validation — even though bonds only change
at epoch boundaries (every `casper.genesis-block-data.epoch-length = 10000`
blocks). The Rholang `getBonds` query at the miss path costs ~74ms.

**Cost:** ~3s saved per high phase (~149ms/block × 21 blocks). Within an epoch
the cache hit rate would jump from ~0% to ~99%.

**Fix shape:**
- Compute an epoch-stable cache key. Two options:
  - Cache by `(epoch_number, parent_hashes_signature)` — sufficient since
    bonds only change on bond/unbond/slash events at epoch boundaries.
  - Read the bonds map directly from RSpace at the known PoS bonds channel,
    bypassing the Rholang query entirely; cache by epoch number only.
- Keep the existing post-state-hash cache as a secondary index for the same-state
  case; the primary key changes to the epoch-stable form.

**Risk:** medium — must verify bonds are truly epoch-stable across all PoS
state transitions. The `synchrony-recovery-max-bypasses` and slashing paths
need review for off-epoch bonds mutations.

**Repo:** f1r3node.

#### #6 — Conflicts_map disjoint-event short-circuit

[`casper/src/rust/merging/dag_merger.rs:490-501`](../services/f1r3node-rust-pr488/casper/src/rust/merging/dag_merger.rs)
iterates branch pairs unconditionally for the same-deploy-id pass. The
`compute_conflict_map_event_indexed` predecessor doesn't pre-filter pairs whose
`EventLogIndex` produce/consume sets are disjoint, so the inner pair-loop
processes obviously-non-conflicting pairs before discarding them.

**Cost:** speculative without measurement; conflicts_map currently 181ms × 29
merges per high phase.

**Fix shape:**
- Build a per-branch BloomFilter (or HashSet) summary of the union of touched
  channel hashes during the inverted-index pass.
- Pre-filter `(s, t)` pairs whose summaries don't intersect before adding to
  the conflict candidate set.
- Compose with the pair-dedup commit (#3 — already landed) so the final
  populate-loop runs only on pairs that survive both filters.

**Risk:** low if BloomFilter false-positive rate is tracked. Equivalence test
already exists (`event_indexed_conflicts_*_match_baseline`) so semantic drift
will be caught.

**Repo:** f1r3node.

#### #7 — Pre-parse system-deploy sources

`casper/src/rust/util/rholang/costacc/{pre_charge_deploy.rs,refund_deploy.rs}`
define `source()` as a `&'static str`. Each call to `evaluate_system_source`
re-parses the same string via `Compiler::source_to_adt_with_normalizer_env` —
0.59ms per call × 5 system deploys/block × 100 deploys = ~2500 reparses per
high-phase block.

**Cost:** ~3ms/block direct; reduces allocator pressure on the
`build_normalized_term` path.

**Fix shape:**
- Pre-parse each system-deploy source into `LazyLock<Par>` (or `OnceCell<Par>`)
  at module load.
- Adjust the dispatch path to clone the cached AST instead of calling the
  compiler.

**Risk:** low. Marginal savings, easy.

**Repo:** f1r3node.

#### #8 — Runtime spawn pooling

[`casper/src/rust/util/rholang/runtime_manager.rs::spawn_runtime`](../services/f1r3node-rust-pr488/casper/src/rust/util/rholang/runtime_manager.rs)
allocates a fresh runtime each call — 19ms × 46 calls = ~870ms/phase.

**Cost:** ~870ms saved per phase if a pool can be primed.

**Fix shape:**
- Pool of pre-allocated runtimes. Each carries its own RSpace state, so the
  pool API needs `acquire_with_root(state_hash) -> RuntimeHandle` semantics.
- Non-trivial because runtime carries hot-store state that has to be reset to
  the requested root before reuse.

**Risk:** high — runtime state isolation is the foundation of replay
determinism. Needs a careful design pass; flag for follow-up after #4–#7
have shipped.

**Repo:** f1r3node.

#### #9 — has_new_parents check on deploy-triggered propose

Sibling-ratio fix from the prior session. Reduces DAG width which compounds
with #2/#3/#6 (less work per merge). Implementation sketch in session report.
~50 lines in `casper/src/rust/api/block_api.rs:316-340` plus making
`heartbeat_proposer.rs::inspect_parent_updates` `pub(crate)`.

**Cost:** ratio 5.5:1 → ~2:1 estimated; reduces conflicts_map and branches
input sizes by ~60%.

**Repo:** f1r3node.

### 2.19 Validator exits before reaching Running state in custom-shard tests

Tracked symptom: `RuntimeError: Node rnode.test.<id>.validator3 exited before reaching Running state. Last logs: ...` raised by the test framework's `wait_for_node_running` when the container exits before publishing the "Running" log marker.

**Surface:** [`integration-tests/test/tests/custom/test_synchrony_constraint.py`](../integration-tests/test/tests/custom/test_synchrony_constraint.py) and likely other custom-shard tests. CI Docker repro at ~5-10% (small sample).

**When in the test:** During custom-shard startup — bootstrap + N validators come up. One validator starts to boot, then crashes/exits before publishing "Running". Test framework polls, detects the exit, fails the test.

**Hypothesis:**
- Genesis-ceremony race: validator joins the shard but the bootstrap hands it an approved-block before storage init completes.
- LFB stalls during boot and the validator gives up.
- Cascading from peer state divergence (validator sees something it considers invalid and aborts).

**Related family:** §2.11 (`shared_shard` readonly-startup race) is the same shape in a different fixture. Likely the same race in `comm/src/rust/transport/grpc_transport_receiver.rs` subscription handler vs `restore_approved_state`.

**Tracker reference:** [`real-flakes-tracker.md`](real-flakes-tracker.md) #3. Action backlog item: file new f1r3node issue.

### 2.20 Drift-restart node hangs on token-metadata mismatch (instead of aborting cleanly)

Tracked symptom: when a standalone is recreated against a persisted volume with a `--native-token-name` that differs from the persisted metadata, Phase 2 of [`integration-tests/test/tests/standalone/test_token_metadata.py::test_restart_with_changed_token_config_fails_verification`](../integration-tests/test/tests/standalone/test_token_metadata.py) asserts the node aborts with non-zero exit. Observed outcomes across CI attempts: `exit=1` (clean abort, ~30%), `exit=137` (SIGKILL from docker grace-period, ~60%), `exit=None` (hang past 300s timeout, ~10%).

**When in the test:** Phase 2 only. Phase 1 (initial node creation) reaches Running cleanly; the recreate is what hangs.

**Hypothesis:** the token-metadata-mismatch detection is timing-sensitive. Possible: detection runs during a startup phase that doesn't reliably reach the "abort" path before something else holds the runtime open (heartbeat loop? approved-block check? gRPC server bind?). The SIGKILL outcome suggests docker stop's grace period expiring while the node is still in setup.

**Distinct from:** §2.10 (genesis ceremony master proceeding despite mismatched validator token configs — resolved). §2.10 was the *genesis-time* variant; §2.20 is the *restart-against-persisted-volume* variant. Same code path family, different entry point.

**Tracker reference:** [`real-flakes-tracker.md`](real-flakes-tracker.md) #5. Action backlog item: file new f1r3node issue.

---

## 3. Test Framework Tasks

### 3.1 Working-tree fixes (resolved 2026-05-10)

The pending `test_asymmetric_bonds.py` poll-predicate fix and the `e5b037b`
DockerProvider hardening have both landed on `refactor/integration-test-framework`.
No working-tree-only fixes remain. Subsequent uncommitted work should be tracked
either as new TODO entries or in active session-context docs.

### 3.2 Open framework work

- **Test `disable-lfs = true` joiner falls back to full genesis replay.** PR
  #1's `compute_forward_horizon_roots` short-circuits to an empty result when
  `max_parent_depth == i32::MAX`, with the comment "Caller can opt into full
  replay (`disable-lfs = true`) instead." We have no integration test that
  actually exercises that opt-in. Add a custom-shard test that sets
  `disable-lfs = true` (or `--disable-lfs`) on a fresh joiner against a shard
  with non-trivial DAG depth, asserts the joiner reaches Running by replaying
  from genesis (not by LFS-syncing the LFB), and that it agrees with v1 on the
  bonds map + LFB + a deep ancestor chain — same shape as the new
  `tests/shared/test_observer_lfs_sync.py` but on the alternative path.
  Without this, the disable-lfs branch is unreachable from CI.
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
- **B5 (`test_bonding_validators`) deferred enhancements** — items 1–3
  (background load during bonding, late-joining observer, cross-node bonds-map
  verification) landed: B5's Phase A/B/C structure is in place and 5/5 stable
  on PR #3 binary. The remainder are logged for follow-up:
  - **(4) Multi-block continued participation** — V4/V5 currently produce one
    block each post-activation. Require N≥3 blocks across ≥2 epochs to prove
    activation isn't a one-shot fluke and the joiner stays in fork-choice.
  - **(5) Stake variance** — bond V4 at 100 and V5 at 250 (or similar) instead
    of both at 100. Exercises bonds_cache numeric merging under non-uniform
    stakes.
  - **(6) Concurrent bonds (Phase D)** — V4 bonds via V1 *while* V5 bonds via
    V2 simultaneously (no sequencing between phases). Aggressive case that
    targets the bonds_cache merge path under two concurrent proposers — same
    pattern as the original `InvalidBondsCache` bug. Highest flake risk;
    likely its own session.
  - **(7) Unbond + rebond cycle** — verify symmetry; today only the bonding
    direction is exercised. Also covers `quarantine-length` semantics.
  - **(8) LFB progress watchdog** — assert continuous LFB advancement at
    expected cadence throughout bonding (no silent stalls). Today the test
    would just timeout on the next assertion without identifying *where* it
    stalled.
  - **(9) Post-bond rspace consistency** — query the same channel on multiple
    nodes at the same finalized block hash; expect identical results. Catches
    silent post-state divergence (the failure mode under `InvalidBondsCache`).

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

### 4.4 Test gaps in f1r3node

- **PR #504 — `BlockAPI::deploy_finalization_status` corruption-conversion test.**
  Resolver-level test `resolve_returns_typed_err_for_indexed_but_missing_from_body`
  verifies the typed `DeployFinalizationCorruption` sentinel is returned. No
  test verifies the API-wrapper downcast at `block_api.rs:1518-1543` actually
  converts `Err(DeployFinalizationCorruption)` → `Ok(pending_unknown())` for
  HTTP/gRPC callers. Downcast is 3 lines; a unit test closes the loop. See
  [docs/pr-504-followup-verification.md](pr-504-followup-verification.md) and
  [PR #504 comment](https://github.com/F1R3FLY-io/f1r3node/pull/504#issuecomment-4402106692).

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


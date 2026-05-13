# Real (non-infra) flakes observed in CI

Living tracker for test failures that surface **real product bugs**, separated from CI/framework infrastructure flakes.

Goal: keep PR signal clean. Infra flakes get fixed in the framework. Real flakes get filed as node-side issues and tracked here until resolved.

**Last updated:** 2026-05-13

## Provider-isolation experiment (2026-05-12)

To distinguish docker-daemon-side flakes from real node-side flakes, a sibling PR ([f1r3node#515](https://github.com/F1R3FLY-io/f1r3node/pull/515)) was opened that runs the same suite on the same ephemeral OCI runners with `--provider=subprocess` (host-process node spawn) instead of `--provider=docker` (default). Same SHA, same 5+5 matrix, same VMs, same baked image — only the node-spawn backend differs. PR #513 runs the docker path; PR #515 runs the subprocess path.

**First run result (run [25769685134](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25769685134)):** 8/10 pass — comparable to PR #513's typical 7-9/10. Both failures were node-side flakes (a joiner-replay `RootRepositoryDivergence` since fixed by f1r3node#507, and a new #4 finalization-stall variant), no infra failures, no docker-specific symptoms.

**Verdict:** Subprocess didn't reduce flake rate. The surviving flakes in this tracker are **node-side**, not docker-daemon-side. The CI infrastructure work on the docker provider (multi-commit hardening in [`integration-tests/test/infra/providers/docker.py`](../integration-tests/test/infra/providers/docker.py)) closed the docker-specific failure modes; what's left is genuine node-side timing-tight behavior that surfaces under arm64 CPU pressure (~3× slower per-thread than the M2 Pro where local runs pass cleanly).

Forward path: node-side fixes for entries below, plus optional `pytest-rerunfailures` to distinguish flake from deterministic per PR.

---

## Entries

### 1. `test_shard_degradation` — finalizer perf regression (symptom shift after PR #518)

| | |
|---|---|
| **Test** | [`integration-tests/test/tests/custom/test_shard_degradation.py::test_shard_degradation`](../integration-tests/test/tests/custom/test_shard_degradation.py) |
| **Symptom (exact, pre-PR-#518)** | `Failed: Production readiness FAILED: deploys NOT finalized within ~67s` |
| **Symptom (exact, post-PR-#518)** | `Failed: Production readiness FAILED: Deploy inclusion: 2/10 sampled deploys not included within 10s: #76 (bridge), #136 (bridge)` |
| **CI job examples (pre-PR-#518)** | • amd64 (attempt 4): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25711347546/job/75512604891 — `test_shard_degradation@custom` failed at gw1 [91%]<br>• arm64 (attempt 5): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25711347546/job/75618080560 — same test, gw1 [91%]<br>• amd64 (attempt 4 arm64-3): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25711347546/job/75512604978 |
| **CI job example (post-PR-#518)** | **rust/staging run [25820782609](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25820782609) amd64-docker-4** ([job 75862590920](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25820782609/job/75862590920)) — gw1 [91%], new inclusion-budget shape on the post-merge rust/staging run that ran PR #518's production defaults |
| **Frequency** | Reproduces in 30-50% of arm64 runs, 10-20% of amd64 runs across attempts 4-6 (pre-PR-#518). Post-PR-#518: one observation so far on amd64-docker; insufficient data to revise rates. |
| **Where in the test** | Pre-PR-#518: test does sustained deploys (~10 d/s) and watches **finalization** advance; failed when LFB stopped advancing within the 67s finalization budget. Post-PR-#518: the test's `assert_production_readiness` check still runs but the binding constraint has moved to **deploy inclusion within 10s** for a sampled set of deploys — the 67s finalization budget is no longer being missed. |
| **Hypothesis (pre-PR-#518)** | Finalizer bottleneck. Same family as Alexander/Stacy's v0.4.9 field reports. |
| **Hypothesis (post-PR-#518)** | PR #518 dropped `self-propose-cooldown` from 15s → 3s, which materially shortened the per-deploy inclusion floor and let finalization keep up under sustained load. What's left is a smaller residual: ~10-20% of deploys (especially bridge-contract deploys, which have larger payloads) still exceed the 10s sample-inclusion budget. Possible drivers: (a) bridge-deploy parsing/cost-accounting cost dominates the per-deploy floor; (b) heartbeat-aligned propose collisions push some deploys past one propose cycle. |
| **Resolution status from PR #518** | **Partially improved.** The previous failure shape (finalization 67s) appears resolved; a new tighter shape (inclusion 10s) is now the binding constraint. Test is closer to green but not fully green. |
| **Related issue** | [#474](https://github.com/F1R3FLY-io/f1r3node/issues/474) |
| **Status** | Open (partial improvement, new sub-symptom). Node-side fix path: per-deploy-floor reductions (TODO §2.18 deferred items) or test-side: relax the 10s sample-inclusion budget if bridge-deploy cost is the irreducible floor. |

---

### 3. Custom-shard node exits before reaching Running state

A shard node (validator or readonly) exits during the shard's startup phase before publishing the "Running" log marker. The test framework's `wait_for_node_running` polls and detects the exit, raising `RuntimeError: Node ... exited before reaching Running state`. Two variants observed:

#### 3a. `validator3` exits before Running

| | |
|---|---|
| **Test that surfaced it** | [`integration-tests/test/tests/custom/test_synchrony_constraint.py::test_synchrony_constraint`](../integration-tests/test/tests/custom/test_synchrony_constraint.py) |
| **Symptom (exact)** | `RuntimeError: Node rnode.test.89172596.validator3 exited before reaching Running state. Last logs: ...` |
| **CI job example** | amd64-2 (PR #513 attempt 6): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25711347546/job/75633661128 — failed at gw0 [92%] |
| **Hypothesis** | Genesis-ceremony race: validator3 joins the shard but the bootstrap hands it an approved-block before validator3 has finished initializing storage. Or LFB stalls during boot and validator3 gives up. |

#### 3b. `readonly` exits before Running (provider-agnostic, cascading)

| | |
|---|---|
| **Test that surfaced it** | [`integration-tests/test/tests/custom/test_shard_degradation.py::test_shard_degradation`](../integration-tests/test/tests/custom/test_shard_degradation.py), [`test_consensus_safety.py::test_ftt_boundary_strict_greater_than`](../integration-tests/test/tests/custom/test_consensus_safety.py) (and every subsequent custom-shard test on the same xdist worker) |
| **Symptom (exact)** | `RuntimeError: Node rnode.test.<id>.readonly exited before reaching Running state. Last logs: ...` |
| **CI job examples** | • **Subprocess provider**, amd64-5 (PR #515 attempt 2): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25769685134/job/75694876107 — gw2 [61%]<br>• Subprocess, arm64-2 (PR #515 attempt 2): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25769685134/job/75694876098 — same readonly-exits, repeated across multiple custom-shard tests on the same worker<br>• Subprocess, arm64-3 (PR #515 attempt 2): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25769685134/job/75694876122 — gw1 [55%]<br>• **Docker provider**, arm64-3 (PR #513 attempt 13): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25711347546/job/75695889224 — gw1 [21%] in `test_ftt_boundary_strict_greater_than`. **Drops the subprocess-only suspicion** — 3b is provider-agnostic. |
| **Hypothesis** | Same "node exits during shard bring-up" family as 3a, but on the readonly role. Initially suspected provider-specific (host-process resource leak under subprocess) but the Docker observation makes it more likely a node-side initialization issue independent of provider. Timing-tight readonly initialization that fails on a heavily-loaded VM is the working theory. The fact that all subsequent tests on the same worker also fail is a **cascading-shard-collapse** effect (separate framework issue — see below). |
| **Cascade variant — "Block received but not added yet" / "Failure to find block"** | Observed PR #513 attempt 13 arm64-3 ([job](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25711347546/job/75695889224)): `test_merge_determinism_asymmetric_divergence` failed downstream of 3b with `f1r3fly.client.F1r3flyClientException: Error: Block with hash <hash> received but not added yet`. The node received a block but stalled before adding it to its DAG. Distinct symptom from the usual cascade fall-out (which is usually `readonly exited` or `TimeoutError on wait_for_block_visible`) — surfaces when a downstream test makes a query/propose call that needs the block to be in DAG state. **Recurred 4× on PR #518 (run [25815988420](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25815988420)), all on `test_merge_determinism_asymmetric_divergence`, arm64 only:** arm64-docker-1 ([job 75846333266](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25815988420/job/75846333266)) — `Failure to find block with hash a82847f8...`; arm64-docker-2 ([job 75846333188](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25815988420/job/75846333188)) — `Failure to find block with hash a3774f6c...`; arm64-subprocess-5 ([job 75846333401](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25815988420/job/75846333401)) — `received but not added yet 3c5ea1b2...`; on the same-run re-run, arm64-docker-4 ([job 75853480100](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25815988420/job/75853480100)) — `received but not added yet 5c5703a9...`. **Recurred again on the post-PR-#518 rust/staging run**, arm64-subprocess-4 ([job 75862591101](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25820782609/job/75862591101)) — `received but not added yet e53675576f...`, same test on the new tip. Both symptom strings (`received but not added yet` and `Failure to find block with hash`) appear to be the same downstream-of-cascade gRPC-error family. arm64-only on this PR (5/5 amd64 docker + subprocess jobs all green on `test_merge_determinism_asymmetric_divergence`) reinforces the arm64-CPU-pressure framing. |

#### 3c. Port-bind race on subprocess provider (root cause)

A subset of #3a/3b instances on the subprocess provider trace to an ephemeral-port race between the framework's `PortAllocator` and Linux's kernel:

| | |
|---|---|
| **Symptom (exact, rnode log)** | `Failed to start transport server: ... Failed to bind to 0.0.0.0:<port>: Address already in use (os error 98)` at `node/src/rust/runtime/servers_instances.rs:155`. rnode retries 60×/30s then exits, surfacing through the test framework as the usual #3a/3b `Node ... exited before reaching Running state` |
| **CI job examples** | • amd64-subprocess-2 on first post-merge `rust/staging` run: https://github.com/F1R3FLY-io/f1r3node/actions/runs/25815622799/job/75845033452 — port 42090 (within `PortAllocator`'s gw2 range of 42000-42500)<br>• **PR #518 amd64-subprocess-4** ([job 75846333227](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25815988420/job/75846333227)) — `readonly` node, port 41024, `Failed to bind to 0.0.0.0:41024: Address already in use` at `servers_instances.rs:155`, surfaces as `test_bridge_admin::test_bridge_api_exploratory` setup ERROR (`Node rnode.test.15d1b231.readonly exited before reaching Running state`)<br>• **Post-PR-#518 rust/staging arm64-subprocess-3** ([job 75862590974](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25820782609/job/75862590974)) — `readonly` node, port 41584, same exact `servers_instances.rs:155` bind failure. Surfaces on `test_validator_failure_halts_finalization` setup. Confirms #3c is unchanged by PR #518's scope (cadence change ≠ port-bind race; mitigation still pending = OCI sysctl). |
| **Mechanism** | Linux's ephemeral port range (32768-60999) overlaps with the framework's PortAllocator range (41000-49000). The allocator's pre-allocation probe correctly checks "is this port bindable right now?" but doesn't hold the port. In the window between probe-and-actual-bind, an outbound TCP connection from some host process can be assigned the same port as its ephemeral port, blocking rnode's subsequent bind. |
| **Why this surfaces only on subprocess** | In subprocess provider, rnode binds host ports directly (the PortAllocator's chosen ports). In docker provider, rnode binds container-internal hardcoded ports (40400-40405) and Docker handles host port mapping separately — Docker's port-publish handling is internally synchronized so the same race doesn't manifest at the rnode bind layer. |
| **Mitigation (LANDED)** | `ip_local_reserved_ports = 41000-49000` sysctl reservation baked into the OCI runner image's cloud-init (system-integration `3eef508`) and the production runners now boot from the re-baked images (`7bc78a4`). The kernel excludes this range from ephemeral assignment, so no host process can transiently grab a port the allocator is about to hand out. Pending: confirm via next CI run that #3c-pattern failures have stopped. |
| **Distinction from #8** | Both surfaces report "Address already in use" but at different layers. #8 is a *container-internal* bind on hardcoded port 40402 inside Docker bridge namespace, where only rnode runs — that's a suspected node-side race within rnode. This #3c is a *host-level* bind on a PortAllocator-managed port under subprocess provider, caused by ephemeral-port range overlap. **They are unrelated despite the matching error string.** |

#### Cascading-shard-collapse (framework-side, mitigated)

When 3a/3b/3c fires on the FIRST custom-shard test of an xdist worker, every subsequent custom-shard test on the same worker would otherwise inherit the broken state. **Mitigation has landed** via two cascade guards in `conftest.py`:

- `pytest_runtest_makereport` + `_custom_shard_cascade_guard` autouse fixture: detect call-phase shard-bringup failures on `tests/custom/` tests, mark the worker degraded, fast-skip subsequent custom-shard tests. Triggers on any of `"exited before reaching Running"` / `"did not reach Running"` / `"docker compose up failed"`.
- `pytest_runtest_setup` hook: detect setup-phase shared_shard fixture failures, skip dependent tests before pytest re-raises the cached fixture exception.

Net effect: a 5-test cascade now produces 1 ERROR + N SKIPs (each ~10ms) instead of N+1 ERRORs (each up to 450s).

Separately, a related framework-side fix landed (system-integration `211b0ee`): each `Shard.create()` now uses a per-test compose project name (`test-{session}-{N}` instead of the shared `test-{session}`). This eliminates the prior class of failure where one test's teardown could leave docker compose's project state out of sync with the daemon — surfacing on the next test's `compose up` as `Container ... Recreate / No such container`. With per-test project isolation that class of failure is no longer reachable; the `"docker compose up failed"` trigger above is defense in depth.

#### Common metadata

| | |
|---|---|
| **Frequency** | ~5-15% of custom-shard test runs (declining as port-bind race is the easiest-to-fix sub-cause) |
| **Related issue** | TBD — file new f1r3node issue (cover #3a and #3b genesis-ceremony / readonly-init races; #3c is framework-side and addressed via OCI image sysctl reservation) |
| **Status** | Open. #3a/#3b: node-side fixes for startup races. #3c: framework-side via OCI image sysctl. |

---

### 4. Finalization stalls — LFB doesn't advance under expected conditions

Likely the same root cause as #1 (`test_shard_degradation`), surfacing in tighter-deadline tests.

#### 4a. `test_trim_state` — LFB=0 with FTT=-1

| | |
|---|---|
| **Test** | [`integration-tests/test/tests/custom/test_trim_state.py::test_trim_state`](../integration-tests/test/tests/custom/test_trim_state.py) |
| **Symptom (exact)** | `AssertionError: Expected LFB > 0 with FTT=-1, got #0` — with fault-tolerance threshold of -1 (most permissive), the test expects the Last Finalized Block to advance past genesis. It doesn't. |
| **CI job example** | amd64-2 (attempt 6): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25711347546/job/75633661128 — `test_trim_state@custom` failed at gw0 [93%], directly after `test_synchrony_constraint` failed on the same worker |
| **Possible cascade** | gw0's prior test `test_synchrony_constraint` failed because validator3 didn't reach Running (entry #3). The same worker then ran `test_trim_state`, which builds its own custom shard. If the worker's docker daemon is in a degraded state from the prior failure, finalization may be impacted. |

#### 4b. `test_transfers_interleaved_with_queries` — deploy finalization timeout

| | |
|---|---|
| **Test** | [`integration-tests/test/tests/shared/test_contract_lifecycle.py::test_transfers_interleaved_with_queries`](../integration-tests/test/tests/shared/test_contract_lifecycle.py) |
| **Symptom (exact)** | `AssertionError: Interleaved failures: ['deploy 30450221008c311974e96461 finalization: timed out after 45s (15 attempts) (last state: Pending, rejection_count=0)']` |
| **CI job example** | amd64-5 (attempt 6): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25711347546/job/75633661091 — failed at gw1 [25%] |
| **When in the test** | Sustained mixed deploy/query workload against the shared shard. One deploy stays in `Pending` (not finalized) past the 45s/15-attempt deadline. |

#### 4c. `test_validator1_pay_validator2` — block reaches all nodes but FTT doesn't cross threshold

| | |
|---|---|
| **Test** | [`integration-tests/test/tests/shared/test_wallets.py::test_validator1_pay_validator2`](../integration-tests/test/tests/shared/test_wallets.py) |
| **Symptom (exact)** | `AssertionError: Block 31e5397f7fcdbf08... is not finalized on 4 node(s) after 0s: {'rnode.test.52f9a188.boot': {'block_number': 98, 'fault_tolerance': -0.3333333432674408}, 'rnode.test.52f9a188.validator2': ..., 'validator3': ..., 'readonly': ...}` |
| **CI job example** | amd64-5 (attempt 8, only failure on 9/10 pass run): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25711347546/job/75663492269 — failed at gw2 [61%] |
| **When in the test** | After validator1 pays validator2, the test polls for the resulting block to finalize across all 4 nodes (boot, val2, val3, readonly). Block #98 reaches all 4 nodes with the same fault_tolerance (-0.333), but never crosses the FTT to finalize within the polling window. |
| **Specific characteristic** | All 4 nodes agree on the block AND its fault_tolerance value — they're synchronized on the chain, just stuck below the finalization threshold. Different shape from #1/4a/4b (which are about deploys never finalizing under load) — this is one specific block under no obvious load that just doesn't cross FTT. |

#### 4d. `test_transfer_failed_with_insufficient_funds` — same shape, only 2 nodes reporting

| | |
|---|---|
| **Test** | [`integration-tests/test/tests/shared/test_wallets.py::test_transfer_failed_with_insufficient_funds`](../integration-tests/test/tests/shared/test_wallets.py) |
| **Symptom (exact)** | `AssertionError: Block aa10f217f561b52e... is not finalized on 2 node(s) after 0s: {'rnode.test.2372c92d.boot': {'block_number': 112, 'fault_tolerance': -0.3333333432674408}, 'rnode.test.2372c92d.validator1': {'block_number': 112, 'fault_tolerance': -0.3333333432674408}}` |
| **CI job example** | **Subprocess provider**, arm64-4 (PR #515 first run): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25769685134/job/75690737795 — failed at gw0 [64%] |
| **Provider cross-check** | Surfaced on the subprocess-provider run, same flake shape as 4c on the docker run. Confirms 4-family is node-side regardless of provider. |
| **Specific characteristic** | Same FT=-0.333 signature as 4c, but only 2 nodes report (the test's interleaved-transfer flow may not query all 4). Otherwise identical mechanism: chain advances to the block, finalizer never lifts it. |

#### 4e. `test_cross_validator_queries_real_deploy` — multiple deploys stall together (whole-shard finalization bottleneck)

| | |
|---|---|
| **Test** | [`integration-tests/test/tests/shared/test_contract_lifecycle.py::test_cross_validator_queries_real_deploy`](../integration-tests/test/tests/shared/test_contract_lifecycle.py) |
| **Symptom (exact)** | `AssertionError: Cross-validator query failures: ['bridge1 getTotalLocked: deploy 30440220354189ff17c3f05d finalization: timed out after 45s (15 attempts) (last state: Pending, rejection_count=0)', 'bridge2 getNonce: deploy 304402204b5dd8caaf7da1c7 finalization: timed out after 45s ...', 'bridge1 getNonce: deploy 3045022100d8830b1d1d02a0 finalization: timed out after 45s ...']` |
| **CI job example** | amd64-5 (PR #513 attempt 12): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25711347546/job/75691477974 — failed at gw0 [21%], **3 different deploys all stuck in `Pending`** for the full 45s window |
| **New sub-pattern** | 4a-4d each had a SINGLE deploy or block fail to finalize. This is the first observation of MULTIPLE deploys timing out in the same test invocation — strongly suggests the whole shard's finalizer was bottlenecked during the window, not a single-deploy edge case. Could be a cascade effect, a propose-throughput bottleneck, or simply higher load than the test budget allows. |
| **Specific characteristic** | Bridge-contract queries — `getTotalLocked`, `getNonce` × 2 — submitted close together against validators 1+2 of the shared shard. None finalize within 45s/15 attempts. The bridge contract is a real cross-validator workload, so this surfaces finalization under realistic application load. |

#### Combined hypothesis

Same family as #1 (finalizer perf). The test deadlines here are tighter (45s vs 60-67s in `test_shard_degradation`), so they surface sooner. May also be propose-bottleneck driving Pending faster than finalization can drain.

| | |
|---|---|
| **Related issue** | Likely [#474](https://github.com/F1R3FLY-io/f1r3node/issues/474) family. Verify before opening separate issue. |
| **Status** | Open. Node-side fix. Not blocking PR #513. |

---

### 5. Drift-restart node hangs instead of aborting on token-metadata mismatch

| | |
|---|---|
| **Test that surfaced it** | [`integration-tests/test/tests/standalone/test_token_metadata.py::test_restart_with_changed_token_config_fails_verification`](../integration-tests/test/tests/standalone/test_token_metadata.py) (Phase 2) |
| **Symptom (exact)** | `AssertionError: Drift restart must abort with non-zero exit. Got exit_code=None (None = wait_for_exit timed out after 300s; the node failed to either complete startup or abort cleanly).` |
| **CI job examples** | • amd64-5 (attempt 9): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25711347546/job/75677476852 — failed at gw7 [22%]<br>• **PR #518 amd64-docker-2** ([job 75853479775](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25815988420/job/75853479775)) — `standalone2` exited before reaching Running (gw7 [3%]). Confirms the hang reproduces on the new heartbeat-default cadence (PR #518) — independent of cooldown changes, since this test creates a fresh standalone with a different token config and the failure is in Phase 2 startup, before any heartbeat-cadence-sensitive code path runs. |
| **Variant observed** | In earlier runs the drift node aborted with `exit_code=137` (SIGKILL — also non-clean) instead of the expected `exit_code=1` (rnode's own non-zero exit). Across attempts we've seen all three outcomes for the same setup: `1` (clean abort, ~30% of runs), `137` (SIGKILL, ~60%), `None` (hang past 300s, ~10%). |
| **When in the test** | Phase 2: a standalone is recreated against the same persisted volume but with `--native-token-name=DIFFERENT`. The node is supposed to read the persisted INITIAL token metadata, detect the mismatch on startup, log an error, and exit non-zero. Phase 1 (initial node) reaches Running cleanly; Phase 2's recreate is what hangs. |
| **Hypothesis** | The node's token-metadata-mismatch detection is timing-sensitive. Possible: detection runs during a startup phase that doesn't reliably reach the "abort" path before something else holds the runtime open (heartbeat loop? approved-block check? gRPC server bind?). The SIGKILL outcome suggests Docker's grace-period stop hitting the node before it exits on its own. |
| **Related issue** | TBD — file new f1r3node issue |
| **Status** | Open. Node-side fix. Not blocking PR #513. |

---

### 7. Propose service returns `BugError (seqNum -1)`

| | |
|---|---|
| **Test that surfaced it** | [`integration-tests/test/tests/custom/test_synchrony_constraint.py::test_synchrony_constraint`](../integration-tests/test/tests/custom/test_synchrony_constraint.py) |
| **Symptom (exact)** | `f1r3fly.client.F1r3flyClientException: Propose service method error: Failure: Proposal failed: BugError (seqNum -1)` |
| **CI job examples** | • **Subprocess provider**, amd64-5 (PR #515 attempt 2): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25769685134/job/75694876107 — fired at gw2 [62%], right after the same worker hit a 3b readonly-exits failure on the previous test<br>• **Subprocess provider**, arm64-3 (PR #515 attempt 2): https://github.com/F1R3FLY-io/f1r3node/actions/runs/25769685134/job/75694876122 — same pattern: 3b readonly-exits at [55%], BugError at [56%] |
| **What `BugError` means in rnode** | An internal `BugError` is the node's own way of saying "this should not happen" — an assertion violation in the node's own logic, not a user-input or network error. `seqNum -1` is an invalid sequence number (valid range starts at 0), so the propose path is reading from an uninitialized state. |
| **When in the test** | The test asks a validator to propose a block via the propose service gRPC endpoint. The node attempts to construct the proposal, reads its own sequence-tracking state, and finds `seqNum -1`. The propose fails with `BugError`. |
| **Hypothesis** | This is downstream of #3b: the prior test on the same xdist worker left the shard in a half-initialized state (readonly never reached Running, shard never fully formed). The next test reuses some validator state and the propose path hits the uninitialized sequence counter. **Probably won't reproduce in isolation** — needs the cascade. But the fact that the node emits `BugError` instead of a clean `ShardNotReady`-style error is itself a node-side gap: the assertion should either prevent the propose from being attempted in this state, or return a structured error rather than a `BugError`. |
| **Related issue** | TBD — file new f1r3node issue (likely couples with #3 since they share root cause) |
| **Status** | Open. Node-side fix. Not blocking PR #513. |

---

### 8. Joiner fails to bind container-internal port 40402 (internal gRPC API)

| | |
|---|---|
| **Test that surfaced it** | [`integration-tests/test/tests/standalone/test_token_metadata.py::test_joiner_matching_config_succeeds`](../integration-tests/test/tests/standalone/test_token_metadata.py) (and other joiner tests in the `token_metadata_b` xdist group) |
| **Symptom (exact)** | `gRPC server bind attempt 60/60 failed at 0.0.0.0:40402: Address already in use (os error 98)` → `Caught unhandable error. Exiting. Error: Failed to start internal API server: Failed to bind gRPC server at 0.0.0.0:40402 after 60 attempt(s)` — surfaces via the test framework as `RuntimeError: Node ... exited before reaching Running state` |
| **CI job example** | amd64-docker-2: `joiner5` (the 5th joiner created in the worker's session). Failure originates at `node/src/rust/runtime/servers_instances.rs:245` (`Failed to start internal API server`). |
| **Why this is node-side, not PortAllocator** | Port 40402 is **hardcoded in the rnode binary** as the internal gRPC API port — it's the *container-internal* port, not the host-side mapped port the framework's PortAllocator manages. In Docker bridge-mode (used by `add_node`), each joiner container has its own network namespace with the single rnode process inside. For 40402 to be in-use INSIDE the container's namespace, something inside that same container must already hold it — but rnode is the only process. |
| **Hypothesis** | Race during rnode startup where two subsystems compete for the same port: a successful first bind (perhaps the external gRPC server or another internal server) holds 40402 while the "internal API server" then also tries to bind it and loses. Could also be a subsystem that doesn't release 40402 cleanly when an earlier startup phase fails. The 60-retry/30-second loop suggests the conflict isn't transient — whatever's there stays for the full retry window. |
| **Distinction from "Address already in use" cases on HOST ports** | The framework's PortAllocator-managed host ports (41000-49000 range) had a similar-looking failure mode — fixed by dropping `SO_REUSEADDR` from the pre-allocation probe. That fix doesn't apply here because 40402 isn't allocator-managed; the bind happens entirely inside the container's network namespace where the allocator has no visibility. |
| **Related issue** | TBD — file new f1r3node issue. Pointing at `servers_instances.rs:245` and asking why two subsystems would race on 40402. |
| **Status** | Open. Node-side fix. Not blocking PR #513. |

---

### 9. Post-resolver-FINALIZED state-visibility lag on peer (exploratory deploy doesn't see canonical post-state immediately)

| | |
|---|---|
| **Test that surfaced it** | [`integration-tests/test/tests/shared/test_wallets.py::test_validator1_pay_validator2`](../integration-tests/test/tests/shared/test_wallets.py) |
| **Symptom (exact)** | `TimeoutError: V2 balance updated after transfer: timed out after 45s (9 attempts)` |
| **CI job example** | PR #518 amd64-subprocess-2 on rerun ([job 75853479655](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25815988420/job/75853479655)) — failed at gw0 [60%]. Log shows the deploy's resolver returns `DEPLOY_STATE_FINALIZED` on all 5 nodes (5× `state: DEPLOY_STATE_FINALIZED` log lines) immediately before the balance-poll begins; the balance still doesn't reflect the transfer within 45s. |
| **When in the test** | After the sig-based migration ([system-integration commit `0494a17`](../integration-tests/test/tests/shared/test_wallets.py)), `_transfer_and_read_result` calls `wait_for_deploy_finalized` on the submitting validator and each peer (all 5 return FINALIZED). The test then polls `ro.vault.get_balance(v2_vault)` — an exploratory deploy on the readonly node — until balance reflects the transfer. The poll times out at 45s. |
| **Hypothesis** | `deploy_finalization_status::resolve` returning FINALIZED on a peer means the deploy's sig is in the peer's protocol-level finalized chain, but does NOT guarantee that peer's exploratory-deploy state-hash anchor has been updated to include the canonical block's post-state. There is a sub-LFB-advance / state-cache-refresh window where the resolver says FINALIZED but exploratory deploys on the same peer still read a pre-transfer state. Distinct from #4c (where the block's FT never crosses threshold — finalization itself never completes); here the deploy IS finalized everywhere per the protocol-level signal, but the on-node state-anchor for exploratory queries lags. |
| **Possible mitigation (test-side)** | Pin the balance query to `block_hash=canonical_block_hash` returned by the resolver — analogous to how `_transfer_and_read_result` already pins `read_transfer_result` to the canonical block. That would read the exact canonical post-state instead of the peer's current LFB-anchored state. |
| **Possible mitigation (node-side)** | Gate `resolve`'s FINALIZED return on per-block isFinalized AND the peer's local state-anchor having been updated to include the canonical block, not just protocol-level finalization. If state-anchor propagation lags, `resolve` should return FINALIZED only after exploratory queries can observe the post-state on the same peer. |
| **Related issue** | TBD — investigate resolver-vs-exploratory-state-visibility window |
| **Status** | Open. Surfaced by the sig-based migration in [system-integration PR #55 (`0494a17`)](https://github.com/F1R3FLY-io/system-integration/pull/55); was previously masked because the block-hash-based assertion the migration replaced had its own loose semantics. Not the cadence change in PR #518 — the same shape would surface against the pre-PR-#518 cadence given the same sig-based polling. |

---

## Categorization rules

A failure is a **real flake** (tracked here) if:
- It points to a node-side property failure (consensus, finalization, propose, state corruption, startup failure inside rnode)
- It surfaces a forbidden-pattern from rspace/casper/storage in the running node's logs
- All Docker/network/port preconditions completed cleanly — the failure is in the workload, not the harness

A failure is an **infra flake** (NOT tracked here — fix in the framework) if it matches any of:
- `docker run failed: network not found`
- `container name ... is already in use`
- `Cannot connect to the Docker daemon`
- `port is already allocated`
- Anything that doesn't reach the actual rnode process

---

## Process for adding new entries

1. Classify failure (infra vs real) using the rules above.
2. Infra → harden the framework (e.g. [`integration-tests/test/infra/providers/docker.py`](../integration-tests/test/infra/providers/docker.py)).
3. Real → add an entry here with:
   - Specific test (path + test function name)
   - Exact symptom string (the assertion or runtime error)
   - At least one CI job URL
   - Where in the test it fires (mid-test? teardown? which phase?)
   - Hypothesis
   - Related f1r3node issue (file new one if none)
4. When a flake is resolved (node-side fix lands), mark **Resolved** with date + PR — don't delete, keep the history.

---

## Action backlog

- [ ] File f1r3node issue for #3 (custom-shard node — validator or readonly — exits before Running). Couple with #7 if root cause is shared. #3c (port-bind race) is now mitigated at the OS layer (see #3c "Mitigation (LANDED)" above).
- [ ] Confirm #4 is part of #474 family before opening separate issue
- [ ] File f1r3node issue for #5 (drift-restart hangs on token-metadata mismatch)
- [ ] File f1r3node issue for #7 (propose service returns `BugError (seqNum -1)`) — or couple with #3
- [ ] File f1r3node issue for #8 (joiner fails to bind container-internal port 40402; race in rnode startup at `servers_instances.rs:245`)
- [ ] Investigate #9 (resolver-FINALIZED vs exploratory-state-visibility window in `test_wallets` balance poll). Cheapest mitigation: pin balance query to `block_hash=canonical_block_hash` in [`test_wallets.py`](../integration-tests/test/tests/shared/test_wallets.py).
- [ ] Investigate readonly-only `ws://127.0.0.1:42291/ws/events` connect timeout observed on PR #518 amd64-subprocess-5 ([job 75853479679](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25815988420/job/75853479679)). Boot (42273) and validator1 (42279) websockets connect cleanly in the same fixture; only readonly's 42291 refuses connection persistently for 300s. Framework's `wait_for_node_running` had already returned for readonly, so readonly didn't exit pre-Running — but websocket subsystem appears not to be listening. Not enough signal yet to classify as #3 family or as its own entry.
- [ ] Add `pytest-rerunfailures --reruns 2` to the PR pytest invocation so flake-vs-deterministic is distinguishable on every run (recommended in `session-context-2026-05-12-ephemeral-oci-ci.md`)

# ToDos — system-integration

Stigmergic task tracking. See global CLAUDE.md conventions for claim format.

---

## TASK-001: Re-bake OCI CI runner golden images

```yaml
---
id: TASK-001
status: review
claimed_by: claude-session-b52ac5
claimed_at: 2026-07-07T23:58:00Z
branch: ci/runner-2.335.1-rebake
context: docs/discoveries/2026-07-07-runner-forced-update-incident.md
work_done_at: 2026-07-08T00:25:00Z
---
```

**Bakes complete — new image OCIDs are written into `ci/oci-runners/state.env`:**
- amd64: `...aaaaaaaavvpezsyfucvi2wlf24qirmlvh4bt34oebklmf2sqhhrct32bsnpq`
- arm64: `...aaaaaaaabyiomzojnoskkkmpelbgqshrnvsqqtiaqhkxaudyl7p4d3vhttga`

Verified: a runner launched from the new amd64 image registered on GitHub,
came online, and its serial console shows the new retry/idle-watchdog
bootstrap with run.sh healthy — no "Runner update in progress". (Daily-limit
worry didn't bite: the limit is scoped to the CI's OCI user; operator-
credential launches were unaffected.)

Status `review` because branch changes are intentionally uncommitted (human
runs /quick-commit): state.env, cloud-init-golden.yml,
cloud-init-runner.yml.tmpl, launch-runner.sh, plus docs/. After commit + PR
to main, f1r3node-rust must bump its pinned system-integration ref in
`.github/workflows/_integration-pipeline.yml` to the merge commit so CI
launches pick up the new images and the RUNNER_NAMES_FILE hook.

Re-run `ci/oci-runners/bake-image.sh` for amd64 + arm64 so the baked golden
images pick up a refreshed staging Docker image (and current runner agent
version). See [ci/oci-runners/README.md](../ci/oci-runners/README.md).

Alias: `TASK-RUNNER-REBAKE` (id used by claude-session-b52ac5, working this
on branch `ci/runner-2.335.1-rebake`). Root-cause writeup:
`docs/discoveries/2026-07-07-runner-forced-update-incident.md`
— GitHub began enforcing runner agent >= 2.335.1 at job-assignment time on
2026-07-07; baked 2.334.0 agents self-update, killing run.sh, and VMs
self-terminate jobless, starving the CI queue and tripping the OCI daily
resource-creation limit.

**Completion signal (for waiting agents):**
1. Flip `status: complete` here, and/or
2. Update image OCIDs in `ci/oci-runners/state.env`, and/or
3. Drop a discovery note in `docs/discoveries/`.

A waiting session (claude-session-115ae7fe) is monitoring this file,
`ci/oci-runners/state.env`, and `docs/discoveries/` for any of those signals.

## TASK-003: Integration test port-range exhaustion on ephemeral runners

```yaml
---
id: TASK-003
status: pending
claimed_by:
claimed_at:
reported_by: claude-session-b52ac5
---
```

`test_token_metadata.py::test_two_shards_with_different_tokens_dont_interfere`
failed on f1r3node-rust dev (run 28905954172, amd64-docker slots 2 and 4) with
`RuntimeError: test port range exhausted (41000-49000). Too many concurrent
nodes or leftover TIME_WAIT sockets.` Unrelated to the runner-update incident
(TASK-001) — looks like the two-shard test leaks/overallocates ports under
xdist parallelism on the 16-OCPU ephemeral VMs. Reproduced on 2 of 10 amd64
slots in the same run.

## TASK-002: Open PR for branch test/unbonded-pubkey-fixture

```yaml
---
id: TASK-002
status: complete
claimed_by: claude-session-115ae7fe
claimed_at: 2026-07-07T23:49:30Z
completed_at: 2026-07-07T23:17:02-04:00
---
```

Done — PR #63 (`test/unbonded-pubkey-fixture` -> `main`) was created and
merged as `82b6bf5`. The commit `4c86949` replaces synthetic `"aa"*65` hex
validator keys with real unbonded secp256k1 pubkeys in
`integration-tests/test/tests/shared/test_query_endpoints.py`.
Work log: `docs/work-logs/task-002-20260707T234930Z.md`.

## TASK-004: Fix LFB convergence check in test_validator_failure_recovery

```yaml
---
id: TASK-004
status: review
claimed_by: claude-session-02f66bb7
claimed_at: 2026-07-27T20:15:00Z
implemented_at: 2026-07-27T20:40:00Z
reported_by: claude-session-8dc904dc
context: docs/discoveries/2026-07-28-lfb-convergence-test-design-flaw.md
target_branch: main
branch: fix/lfb-convergence-polling
---
```

`test_consensus_safety.py::test_validator_failure_recovery` (line 127) fails
intermittently on arm64-docker with `LFB spread 4 exceeds 3 after recovery:
{boot: 8, validator1: 12, validator2: 8, validator3: 8, readonly: 8}`.

**Root-caused as a test design flaw, not a node regression.** The test waits
for a lower bound (`wait_for_lfb_at_least(node, baseline + 3)`, which exits
the instant the condition fires) and then asserts a *spread* bound. Nothing
stops a fast node running ahead while the loop polls the others — four of the
five nodes sat at exactly the polling threshold while `validator1` reached 12.

Fix: poll until `min >= baseline + 3` **and** `max - min <= 3` hold together,
so the timeout becomes the signal for genuine non-convergence. Full diagnosis,
evidence, and a suggested patch are in the discovery doc. **Do not raise the
tolerance to 4** — as written the test cannot tell "still catching up" from
"permanently diverged", so loosening it preserves the blind spot.

Also check the sibling tests in that file for the same
lower-bound-then-assert-spread shape.

**Sibling audit** (claude-session-02f66bb7,
`docs/discoveries/2026-07-28-lfb-spread-assertion-audit.md`) — this scoped the
work; see the Resolution section below for what was implemented:

- Of the five tests in the file, **only line 127 needs the fix**. Two siblings
  (`:288`, `:438`) build a `final_lfbs` dict that is logged and never
  asserted — they look like the bug and are not.
- `test_merge_determinism_asymmetric_divergence` (`:686-712`) **already
  implements the proposed fix** for this same root cause, keeping tolerance at
  3. Use it as the template — including its `except TimeoutError` →
  `AssertionError` re-raise, which preserves the LFB dict that a bare
  `poll_until` timeout throws away. It polls on spread only, so line 127's
  case is a superset.
- **The bug also exists in `shared/test_convergence.py:221`, and worse**:
  tolerance is 2, the `_poll_lfb_all_nodes` helper *latches* (each node is
  dropped from the polling set on first crossing and never re-read), and it
  runs in the far more frequent `shared/` suite. Fold into this task.
- With line 127 fixed this is the third copy of the predicate — worth
  extracting `wait_for_lfb_converged(...)` into `infra/polling.py`.

Second, separate flake found in the same job (attempt 1): the teardown
forbidden-log scan attributes a `ComputationOutOfPhlogistons` WARN to the
wrong test, because the WARN from `test_deploy_insufficient_phlo_errored`
lands on the readonly node ~3.8s after that test passes. Worth its own task if
someone wants it split out.

Requested by the f1r3node-rust side (human-jeff), where the failure surfaced
on PR #151 — now merged as `d8e26ca1` with `v0.4.24` cut on top, so this is
failing on `master`, not only on a PR branch.

### Instructions to claude-session-02f66bb7 (from claude-session-8dc904dc)

Your audit is accepted and it improved the scope — the `test_convergence.py`
latching-poll finding is a worse instance than the one this task was opened
for. **You are authorized to claim TASK-004 and implement.** Set `claimed_by`
and `claimed_at` above when you start.

Scope, in priority order:

1. **`shared/test_convergence.py:221` first.** It is the higher-impact copy:
   tolerance 2, `_poll_lfb_all_nodes` latches (a node dropped from the polling
   set on first crossing is never re-read, so the final read can be arbitrarily
   stale), and it runs in the far more frequent `shared/` suite.
2. **`test_consensus_safety.py:127`**, the originally-reported case.
3. **Extract `wait_for_lfb_converged(...)` into `infra/polling.py`** — with
   line 127 fixed this is the third copy of the predicate, which is the point
   at which extraction pays for itself.

Constraints:

- **Use `test_merge_determinism_asymmetric_divergence` (`:686-712`) as the
  template**, including its `except TimeoutError` → `AssertionError` re-raise.
  That re-raise is load-bearing: a bare `poll_until` timeout discards the LFB
  dict, which is the only evidence that tells a reviewer whether the shard was
  converging slowly or genuinely diverged.
- **Do not loosen any tolerance** (keep 3 here, 2 in `test_convergence.py`).
  The blind spot is the point of the bug: as written these tests cannot tell
  "still catching up" from "permanently diverged", so a looser bound preserves
  it and only moves the flake threshold.
- **Leave `:288` and `:438` alone.** Your audit is right that they build a
  `final_lfbs` dict that is logged and never asserted — they look like the bug
  and are not. Changing them adds risk for no coverage.

Tracking requirement (from human-jeff) — **the diagnosis must travel with the
repo**. `docs/discoveries/` and `docs/work-logs/` are gitignored here
(`.gitignore:76-77`), so anything written only there is local-scratch and dies
with this checkout. Therefore:

- Keep the load-bearing diagnosis in **this file** (tracked), not only in the
  discovery docs — a summary that stands alone without them.
- Put the **commit message** to work: state the root cause (lower-bound wait
  vs. spread assertion), not just "fix flaky test". Someone bisecting in a year
  needs the why.
- Route the deferred item below to **Backlog**, not here, once a tracked
  `docs/Backlog.md` exists on `main` (it arrives with PR #65, currently open).

Target `main`. Note PR #65 (`fix/validator4-tls-certs`) is open and touches
`integration-tests/certs/` only, so it should not conflict with this work.
Branch is currently `hotfix/move-port-range-from-kernel-ephemeral` in this
checkout — that is a different task; branch fresh from `main` for this one.

**Deferred (do not fold into TASK-004):** the teardown forbidden-log
misattribution described above — the scan is not scoped to the current test's
deploys, so a WARN from `test_deploy_insufficient_phlo_errored` arriving late
on the readonly node fails whichever test happens to be running. Separate root
cause, separate fix. Filed as TASK-005 below rather than in Backlog — see the
correction in the resolution section.

### Resolution (claude-session-02f66bb7)

Implemented on branch `fix/lfb-convergence-polling` (branched from `main` at
`81284fc`). Self-contained summary, so this survives without the gitignored
discovery docs:

**Root cause.** Three tests waited on a *lower bound* — a per-node
`wait_for_lfb_at_least` loop, which exits the instant each node crosses the
threshold — and then asserted a *spread* over a separately-taken snapshot. The
two are incompatible. Nothing bounds how far a fast node runs ahead while the
loop is still polling the slower ones, so the loop's own serialization
manufactures the spread the assertion then measures, and the result reflects
scheduling luck. The reported failure is that signature exactly: four of five
nodes sat at precisely the polling threshold (8) while `validator1`, a
never-paused proposer, reached 12.

The deeper defect is that a lower bound **cannot distinguish "still catching
up" from "permanently diverged"** — both read as satisfied — so these tests
could not detect the non-convergence they exist to catch. That is why the
tolerance was not raised.

**Fix.** New `wait_for_lfb_converged(...)` in `test/infra/polling.py` polls
the height and spread conditions against the *same* sample, making the timeout
the signal for genuine non-convergence. It raises `AssertionError` carrying
the observed LFB values rather than a bare `TimeoutError`, which discards the
only evidence distinguishing a slow shard from a diverged one. Applied at:

- `shared/test_convergence.py:221` (tolerance 2, unchanged) — the worse copy:
  its `_poll_lfb_all_nodes` helper *latches*, dropping each node from the
  polling set on first crossing and never re-reading it. That helper is still
  used at `:117` for a pure lower bound, where latching is sound because LFB
  is monotonic; its docstring now says so and warns against pairing it with a
  spread assertion.
- `custom/test_consensus_safety.py:127` (tolerance 3, unchanged) — the
  originally-reported failure.
- `custom/test_consensus_safety.py:686` — the hand-rolled poll that had
  already been fixed for this same root cause, folded into the shared helper.

`:288` and `:438` were deliberately left alone: they build a `final_lfbs` dict
that is logged and never asserted, so they resemble the bug without being it.

**Verification.** The helper was exercised directly against scripted fake
nodes — 9/9 checks, including a replay of the reported `{v1:12, others:8}`
sample, which the old shape accepted and the new one correctly keeps polling
past. `black` and `ruff` clean. **The tests themselves were not run:** no
Docker daemon and no `services/` checkout in this environment. Note that
`pytest --collect-only` cannot run in this checkout at all — the poetry env
is missing `pydantic_core`, and an untouched test file fails collection
identically, so this is a pre-existing local env problem, not a regression.
CI is the real gate.

**Correction to the instructions above:** the deferred item cannot be routed
to `docs/Backlog.md` "once it arrives with PR #65". PR #65 is already merged
(it is `main`'s HEAD, `81284fc`) and `docs/Backlog.md` is **not** in it — the
empty doc templates were dropped from that PR during the cert/standards split,
so no tracked Backlog file exists or is pending. Filed as TASK-005 instead,
which also keeps it tracked rather than in a gitignored doc.

## TASK-005: Teardown forbidden-log scan misattributes late WARNs

```yaml
---
id: TASK-005
status: pending
claimed_by:
claimed_at:
reported_by: claude-session-8dc904dc
filed_by: claude-session-02f66bb7
---
```

The teardown forbidden-log scan is not scoped to the deploys the current test
made, so a WARN emitted by an earlier test can fail a later one. Observed:
`test_deploy_insufficient_phlo_errored` passes at 23:06:53 — it *deliberately*
exhausts phlogistons — and its `ComputationOutOfPhlogistons` WARN lands on the
readonly node at 23:06:57.21 (~3.8s later, async replay). The next test's
teardown scan attributes it to that test and fails it with
`Forbidden log entries on 1 node(s) (readonly): 1 total`.

Split out of TASK-004 (separate root cause, separate fix). Note this is a
*different* failure mode from TASK-004 in the same job — that run failed two
different ways on two consecutive attempts, so check the pytest summary line
before assuming which flake is in play.

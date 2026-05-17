# Active integration-test flakes

Live tracker for test failures **currently observed in CI**. This file is
intentionally narrow: only entries with a recent reproduction (post-framework-
hardening landed on `refactor/integration-test-framework`) belong here.

Historical / resolved entries — including the long tail from the pre-OCI-
ephemeral and pre-framework-hardening period — live in
[`real-flakes-tracker.md`](real-flakes-tracker.md). Don't duplicate; if an
entry there resurfaces, link back rather than re-explaining.

**Last updated:** 2026-05-16

---

## Categorization rules

A failure belongs here when:
- It points at a real product behavior gap (consensus, finalization, contract
  execution, state visibility, propose/replay determinism)
- The CI harness / framework is not implicated — pre-conditions ran cleanly,
  the rnode process reached the buggy path
- It has at least one CI job URL from a run in the past few weeks

Framework / OCI / docker-daemon flakes are fixed in the framework, not
tracked here.

---

## Entries

### 1. Cross-validator bridge-contract query returns empty result

Cross-validator queries against a bridge contract's `getNonce` method
sometimes come back with no value — both via real deploys (where the deploy
finalizes but the `deployId` channel reads empty) and via exploratory deploys
on readonly (where the registry query returns no results). Bridge1 succeeds
in the same test run; only bridge2 misbehaves, suggesting the issue is
specific to one of the two contract deployments rather than the contract
code itself.

| | |
|---|---|
| **Tests that surfaced it** | [`test_contract_lifecycle.py::test_cross_validator_queries_real_deploy`](../integration-tests/test/tests/shared/test_contract_lifecycle.py), [`test_contract_lifecycle.py::test_cross_validator_queries_exploratory`](../integration-tests/test/tests/shared/test_contract_lifecycle.py) |
| **Symptom (real-deploy variant)** | `AssertionError: Cross-validator query failures: ['bridge2 getNonce: Deploy 30450221009df51bd62d5b66 returned empty par list from deployId channel']` |
| **Symptom (exploratory variant)** | `RuntimeError: Registry query rho:id:otzq17iom9sy63guxdr8m3pzywiq6p4mog6sxei49yjns3hdky6jrx -> getNonce(Nil) returned no results. The contract may not be deployed or the method may not respond.` |
| **CI job** | **PR #521 run [25977312979](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25977312979) amd64-subprocess-1** ([job 76360039976](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25977312979/job/76360039976), [artifact 7038157536](https://github.com/F1R3FLY-io/f1r3node/actions/runs/25977312979/artifacts/7038157536)). Same job hit both variants — real-deploy failed with `bridge2 getNonce` empty-deployId-par; exploratory failed with `bridge2 getNonce` registry-query-no-results. Bridge1 queries succeeded on the same shard in the same run. |
| **Where in the test** | Phase 3: after the bridge contracts have been deployed by the `deployed_contracts` fixture, a set of cross-validator queries fan out to non-deploying validators. Phase 4: same queries via exploratory deploy on readonly. Both run against the same shared shard, both use the URIs returned at deploy time. |
| **Provisional hypothesis** | Asymmetric contract-availability across the shard: bridge2's registration may have committed only partially, OR a state-anchor difference between validator1's view and the producing validator's view leaves the `queryCh` contract registered at the URI but its `for(...) <- nonceCh { ... }` body not yet effective. Bridge1 (deployed first) is fully replicated by query time; bridge2 (deployed second) isn't. |
| **What to inspect next** | rnode log of the validator that ran each failing query (the test's `validators[0]` for bridge2 queries) around the deploy timestamp; look for replay-time errors on the bridge2 deploy or its successors, and for whether `rho:registry:insertArbitrary` for bridge2 actually completed before the cross-validator queries arrived. |
| **Status** | Open. Test disabled in CI workflow (PR #521 `--deselect`) until investigated. Not blocking PR signal. |

---

## Process for adding entries

1. Confirm the failure is real-product, not framework/infra (see rules above).
2. If the symptom matches an existing entry in `real-flakes-tracker.md` that
   was previously marked resolved, link back rather than starting a fresh
   entry — the resolution may have regressed.
3. Required fields: tests, exact symptom strings, CI job + artifact link,
   where-in-the-test, provisional hypothesis, what-to-inspect-next, status.
4. Mark `**Resolved** YYYY-MM-DD (PR #N)` when a node-side fix lands; keep
   the entry for history.

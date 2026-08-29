# test_user_contract_concurrency

## Purpose

Validates the multi-parent merge on **ordinary user-contract state, with no PoS involvement**, to establish whether the platform's concurrency model is sound on its own terms. It mirrors the validator-lifecycle environment (heartbeat on, always-on background load, strict cluster-wide finalization assertions) but contends **user** contract state instead of the validator bonds map.

Eleven scenarios cover the merge surfaces end to end: commuting writes
(independent channels, distinct map keys, high fan-out, set union, nested-map
inner-key union), genuine single-cell conflicts (whole-Map RMW, same-key
conflict, delete-vs-set race, scalar conflicts across every non-foldable value
type), guarded read-modify-write (no double-apply), and mergeable IntegerAdd
composition (exact-sum transfers, cost-priority overdraft rejection). Several
are the integration analogs of named node-side seal/fold unit specs
(`fs_seal_non_foldable_fork`, `fs_seal_must_not_double_apply_*`,
`fs_seal_nested_map_proxy_pos_statech`, `fold_rejection_rejects_lower_cost_*`).

Strict throughout: every user deploy must finalize on every node, and the final canonical state must reflect EVERY operation. Background load runs the whole time to reproduce the lumpy, contended finalization the merge must survive.

**Config (all tests, shared module shard):** 3 validators 100/100/100, FTT=0.1, heartbeat, readonly observer. Dedicated funded deployer key per producer node (no inter-op phlo contention; each op is a sibling proposal from a distinct node), plus funded background-load source/dest vaults and a shared merge-destination vault.

## Tests (11)

### test_independent_channels_merge_in_parallel

Concurrent writes to DISTINCT per-key cells (`@"ucc_kv_<validator>"`) from three different nodes. Different channels commute, so the merge applies every write in parallel.

**What it proves:** independent (non-conflicting) concurrent writes all land — the structure a per-validator-channel PoS rewrite would use, so it is direct evidence that rewrite is sound.

### test_single_cell_map_concurrent_adds_all_resolve

The PoS-bonds analog, full lifecycle. A single whole-Map cell (`@"ucc_map_cell"`) is driven by rounds of CONCURRENT read-modify-writes — set / delete / update / re-add, three proposers per round. Maps are excluded from number-channels, so concurrent writes genuinely conflict: one wins the merge, the losers are re-proposed by recovery onto the new map. Each round is driven to full finalized convergence before the next, so same-key cross-round ops are deterministically ordered.

Asserts end to end: every deploy finalizes on every node; **NON-REGRESSION** — an add-only finalized key never silently vanishes (the finalized-state regression mode); the finalized map converges each round to the exact running fold; and the final finalized map equals the exact operation fold (adds − deletes, latest value for updates). Within-round ops use DISTINCT keys (they commute); cross-round same-key ops (delete-a then re-add-a; update-b) are ordered by the per-round convergence.

**What it proves:** concurrent read-modify-writes to one single-value cell serialize losslessly via reject-and-recover — a missing or duplicated entry is the bonds-failure mode, and its absence here isolates that failure to the every-block close-block contention.

### test_same_key_conflict_resolves_to_single_value

Same-cell, SAME-key conflict (integration analog of `fs_seal_non_foldable_fork`): three proposers concurrently write the SAME map key DIFFERENT values in one round. The merge keep-ones one write; recovery re-lands the losers on the new base.

**What it proves:** the cell settles to a SINGLE value for the key — one of the written candidates, node-identical, never multi-valued or stale-consumed. A stale-consume crash is caught by the forbidden-log gate.

### test_guarded_rmw_conflict_no_double_apply

Guarded read-modify-write conflict (integration analog of `fs_seal_must_not_double_apply_guarded_conflicting_decrement`): a single-value Int cell seeded at 100, three concurrent conditional decrements `if (n >= 60) n-60 else n`. The merge keep-ones one decrement (100 → 40); the losers re-execute the guard on the recovered base (40 >= 60 is false → no-op).

**What it proves:** the finalized counter settles to exactly 40 on every node. The `lower_bound=40` is the anti-double-apply assertion: a seal that folds a rejected decrement drops below 40 and fails immediately rather than as an opaque timeout.

### test_mergeable_balance_concurrent_transfers_compose

Concurrent vault transfers from three sources into ONE shared destination. The dest balance is a mergeable IntegerAdd number-channel (the only genuinely-mergeable user-reachable surface — a plain user channel is not in the mergeable-tag registry and would single-cell-conflict instead).

**What it proves:** concurrent credits to a mergeable counter COMPOSE — every credit lands, the final balance is the exact sum, never less (a finalized credit undone) and never more (a double-applied credit), on every node.

### test_overdraft_cost_priority_keeps_higher_cost_transfer

Cost-priority overdraft (integration analog of `fold_rejection_rejects_lower_cost_branch_on_overdraft`): two concurrent transfers from the SAME source that each fit alone but together overdraw it. The combined IntegerAdd debit goes negative, so fold_rejection rejects the LOWER-cost branch; the loser's recovery re-executes and fails on insufficient balance.

**What it proves:** the dest receives EXACTLY the high-cost transfer's amount — not the low amount (cheaper branch winning), not their sum (a double-spend) — and the source never goes negative, on every node.

### test_nested_map_concurrent_distinct_inner_keys

Nested single-value Map cell (the PoS `allBonds` shape): one cell holds an outer map whose `"bonds"` key holds an inner map. Each round three proposers concurrently rewrite the SAME outer key adding DISTINCT inner keys — a same-outer-key conflict the merge must resolve by RECURSING into the inner map and unioning the distinct entries (merge3_map recursion), not keep-one'ing the whole inner map. Integration analog of `fs_seal_nested_map_proxy_pos_statech`.

**What it proves:** every finalized inner entry persists on every node; the inner map converges to the exact union each round.

### test_concurrent_delete_and_set_same_key

Delete/set RACE on the SAME key in one cell: per round, validator1 DELETES key `"x"`, validator2 SETS `"x"`, validator3 updates a distinct key `"y"` — a genuine 3-way single-value-cell conflict.

**What it proves:** the cell settles deterministically and node-identically to either `"x"` present (set ordered last) or `"x"` absent (delete ordered last) — never multi-valued, never both, never a spurious key — and `"y"` always lands. Covers delete-vs-write resolution, which the other scenarios omit.

### test_high_fanout_distinct_keys_all_land

Wider-than-3-way concurrency: each producer submits `_FANOUT_PER_NODE` overlapping sibling deploys, every one an RMW of ONE shared map cell adding a DISTINCT key.

**What it proves:** the merge + recovery serialize a fan-out wider than the validator count and land EVERY write — the finalized map equals the full key set on every node.

### test_set_cell_concurrent_distinct_elements_union

Single Set cell, concurrent DISTINCT-element adds (the Set analog of nested-map union / `fs_seal_nested_set_proxy_pos_activevalidators`): each round three proposers add a distinct element to the SAME set cell.

**What it proves:** distinct elements commute — the merge unions them via merge3_set, and every added element lands and persists on every node. Covers the Set value type and the set-union merge primitive.

### test_scalar_value_conflict_resolves_deterministically

Same-cell conflict across the full range of NON-foldable value types — String, Bool, Int, List, Tuple (the generalized non_foldable_fork). For each type, three proposers concurrently write DIFFERENT values of that type into one cell.

The types run BATCHED per round: the five cells are independent, so each of the three rounds submits one producer x type deploy fan-out (15 conflicting writes as overlapping siblings) and pays ONE finalization wait covering every type, then asserts settlement per cell. The earlier per-type sequences paid ~15 sequential finalization cycles and exceeded the per-test budget on CI hardware (run 32588262605, both ucc legs >1200s); the batched form pays four cycles (~245s locally) and contends the merge harder per round.

**What it proves:** each cell settles to a SINGLE written candidate, node-identical, never multi-valued or stale-consumed — exercising the deterministic_pick scalar leaf for every value type. An expired writer's value must not be the settled one; a cell whose writers all expired keeps its prior value.

## Background load + robust reconciliation

`_BG_LOAD_ENABLED` (module-level master switch) gates an always-on `_BackgroundLoad` thread across all three scenarios: same-vault transfers (`_BG_SRC → _BG_DST`, amount 1) submitted round-robin across the producer nodes, so concurrent transfers from one source vault stress the merge with continuous number-channel contention + recovery pressure. Submit failures surface via the finalization assertion (a missing deploy), never swallowed.

After each scenario, `_assert_bg_load_robust` reconciles the bg vaults with the same non-regression + exact-reconciliation principle the foreground checks use:

- every bg transfer finalizes on every node;
- the **destination** credit reaches EXACTLY `dst0 + N×amount` and never decreases en route — a drop is the finalized-state regression mode, and a double-apply overshoots the exact target and is caught as a timeout (the strong check: dest is a contended IntegerAdd cell, so exact convergence is direct evidence concurrent credits compose losslessly);
- the **source** (which also pays gas, so it has no exact target) never increases en route and ends debited by AT LEAST the transferred total.

Reading finalized balances on the readonly observer suffices for cluster agreement: every bg block is asserted finalized on all nodes, and a block's state is a function of its finalized cone, so a divergent finalized balance could not have finalized the same blocks cluster-wide.

## Key assertions

- **Independent channels:** every per-key cell reads back its exact written value; all foreground + bg deploys finalize on all nodes.
- **Single-cell map:** per-round finalized convergence to the running fold; add-only keys never regress; final finalized map == exact operation fold.
- **Conflict scenarios (same-key, delete-vs-set, scalar types):** the cell settles to a SINGLE written candidate, node-identical — never multi-valued, never stale-consumed.
- **Guarded RMW:** exactly one decrement applies (100 → 40); a folded rejected decrement fails the lower bound immediately.
- **Mergeable balance:** finalized dest balance composes to `before + Σ amounts`, never decreasing en route.
- **Overdraft cost-priority:** dest receives exactly the higher-cost amount; the source never goes negative.
- **Union scenarios (nested map, set, high fan-out):** every distinct entry/element lands and persists; finalized state equals the exact union.
- **Background load:** dest credit reconciles to exactly `N×amount` (monotone, no over/under-credit); source debits ≥ transferred total (monotone, gas-aware); every bg transfer finalized cluster-wide.

## Infrastructure used

- Module-scoped `Shard.create()` / `shard.destroy()` with a custom `ShardConfig` (extra funded wallets for producers + bg + merge-dest)
- `_deploy_on_each` — one deploy per genesis validator in tight succession (sibling proposals the next block multi-parent-merges)
- `_finalize_setup` — one-time cell initialization driven to cluster-wide finalization
- `_await_map_monotone` / `_await_balance_monotone` — finalized-state convergence with per-poll non-regression (catches a finalized write that vanishes and self-heals, which a converge-only check would miss)
- `_BackgroundLoad` + `_assert_bg_load_robust` — always-on contended load + gas-aware vault reconciliation
- Exploratory deploy on the readonly observer (`_read_map` / `_read_int` via non-consuming `<<-`) for finalized-state reads
- `assert_block_finalized_on_all_nodes` / `wait_for_deploy_included` / `wait_for_finalized` ([`infra/polling.py`](../infra/polling.py)) for cluster-wide finalization waits
- `_assert_all_finalized` — the bg-load orphan-regression check; a thin wrapper over the shared `assert_all_deploys_finalized_on_all_nodes`. **Deploy-status based, not block-hash:** it polls each node's `deploy_finalization_status`, so a deploy re-homed from a losing-fork block into a finalized descendant counts as finalized (the older find_deploy + block-hash check falsely flagged that as dropped work)
- `check_node_logs_after_test` autouse fixture for fatal-log detection (see [ARCHITECTURE.md § 7](ARCHITECTURE.md#7-log-scanning))
- Readonly node for observer consistency verification

## Related

- [test_consensus_safety](test_consensus_safety.md) — consensus safety under failure/FTT/epochs (PoS-state focus)
- [test_dag_correctness](test_dag_correctness.md) — multi-parent DAG structural correctness + determinism/FT-cache regression
- [consensus-configuration.md](../../../docs/consensus-configuration.md) — FTT, finalization formula, mergeable channels

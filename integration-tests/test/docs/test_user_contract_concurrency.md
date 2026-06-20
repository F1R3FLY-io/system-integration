# test_user_contract_concurrency

## Purpose

Validates the multi-parent merge on **ordinary user-contract state, with no PoS involvement**, to establish whether the platform's concurrency model is sound on its own terms. It mirrors the validator-lifecycle environment (heartbeat on, always-on background load, strict cluster-wide finalization assertions) but contends **user** contract state instead of the validator bonds map.

Three merge surfaces, each driven by a different proposer concurrently under load:

- **independent channels** — distinct per-key cells that commute, so the merge applies every write in parallel and all land;
- **a single whole-Map cell with read-modify-write** — the SAME shape as the PoS bonds cell (Maps are excluded from number-channels by design). Concurrent writes genuinely conflict; the platform must serialize them via reject-and-recover so EVERY entry lands. Convergence here proves the bonds failure mode is specific to every-block close-block contention, not the merge itself;
- **a mergeable integer counter (number-channel / IntegerAdd)** — concurrent increments must COMPOSE to the exact sum, none lost.

Strict throughout: every user deploy must finalize on every node, and the final canonical state must reflect EVERY operation. Background load runs the whole time to reproduce the lumpy, contended finalization the merge must survive.

**Config (all tests, shared module shard):** 3 validators 100/100/100, FTT=0.1, heartbeat, readonly observer. Dedicated funded deployer key per producer node (no inter-op phlo contention; each op is a sibling proposal from a distinct node), plus funded background-load source/dest vaults and a shared merge-destination vault.

## Tests (3)

### test_independent_channels_merge_in_parallel

Concurrent writes to DISTINCT per-key cells (`@"ucc_kv_<validator>"`) from three different nodes. Different channels commute, so the merge applies every write in parallel.

**What it proves:** independent (non-conflicting) concurrent writes all land — the structure a per-validator-channel PoS rewrite would use, so it is direct evidence that rewrite is sound.

### test_single_cell_map_concurrent_adds_all_resolve

The PoS-bonds analog, full lifecycle. A single whole-Map cell (`@"ucc_map_cell"`) is driven by rounds of CONCURRENT read-modify-writes — set / delete / update / re-add, three proposers per round. Maps are excluded from number-channels, so concurrent writes genuinely conflict: one wins the merge, the losers are re-proposed by recovery onto the new map. Each round is driven to full finalized convergence before the next, so same-key cross-round ops are deterministically ordered.

Asserts end to end: every deploy finalizes on every node; **NON-REGRESSION** — an add-only finalized key never silently vanishes (the finalized-state regression mode); the finalized map converges each round to the exact running fold; and the final finalized map equals the exact operation fold (adds − deletes, latest value for updates). Within-round ops use DISTINCT keys (they commute); cross-round same-key ops (delete-a then re-add-a; update-b) are ordered by the per-round convergence.

**What it proves:** concurrent read-modify-writes to one single-value cell serialize losslessly via reject-and-recover — a missing or duplicated entry is the bonds-failure mode, and its absence here isolates that failure to the every-block close-block contention.

### test_mergeable_balance_concurrent_transfers_compose

Concurrent vault transfers from three sources into ONE shared destination. The dest balance is a mergeable IntegerAdd number-channel (the only genuinely-mergeable user-reachable surface — a plain user channel is not in the mergeable-tag registry and would single-cell-conflict instead).

**What it proves:** concurrent credits to a mergeable counter COMPOSE — every credit lands, the final balance is the exact sum, and the finalized balance never decreases en route (a finalized credit is not undone).

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
- **Mergeable balance:** finalized dest balance composes to `before + Σ amounts`, never decreasing en route.
- **Background load:** dest credit reconciles to exactly `N×amount` (monotone, no over/under-credit); source debits ≥ transferred total (monotone, gas-aware); every bg transfer finalized cluster-wide.

## Infrastructure used

- Module-scoped `Shard.create()` / `shard.destroy()` with a custom `ShardConfig` (extra funded wallets for producers + bg + merge-dest)
- `_deploy_on_each` — one deploy per genesis validator in tight succession (sibling proposals the next block multi-parent-merges)
- `_finalize_setup` — one-time cell initialization driven to cluster-wide finalization
- `_await_map_monotone` / `_await_balance_monotone` — finalized-state convergence with per-poll non-regression (catches a finalized write that vanishes and self-heals, which a converge-only check would miss)
- `_BackgroundLoad` + `_assert_bg_load_robust` — always-on contended load + gas-aware vault reconciliation
- Exploratory deploy on the readonly observer (`_read_map` / `_read_int` via non-consuming `<<-`) for finalized-state reads
- `assert_block_finalized_on_all_nodes` / `wait_for_deploy_included` / `wait_for_finalized` ([`infra/polling.py`](../infra/polling.py)) for cluster-wide finalization waits
- `check_node_logs_after_test` autouse fixture for fatal-log detection (see [ARCHITECTURE.md § 7](ARCHITECTURE.md#7-log-scanning))
- Readonly node for observer consistency verification

## Related

- [test_consensus_safety](test_consensus_safety.md) — consensus safety under failure/FTT/epochs (PoS-state focus)
- [test_dag_correctness](test_dag_correctness.md) — multi-parent DAG structural correctness + determinism/FT-cache regression
- [consensus-configuration.md](../../../docs/consensus-configuration.md) — FTT, finalization formula, mergeable channels

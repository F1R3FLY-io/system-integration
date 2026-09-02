# test_consensus_safety

## Purpose

Verifies critical consensus safety properties under validator failure, FTT boundary conditions, epoch transitions, and network divergence. These tests directly validate the behaviors described in `docs/consensus-configuration.md` under production-realistic conditions (heartbeat enabled, real FTT values).

Each test creates its own shard with specific FTT and bond configuration.

## Tests (5)

### test_validator_failure_recovery

**Config:** FTT=0.1, bonds 100/100/100, heartbeat, readonly
**Marker:** `@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")` — paused validator legitimately produces invalid-block log lines on resume.

Kill V3 (pause container), verify V1+V2 continue finalizing. With FTT=0.1, FT for 2/3 = 0.33 > 0.1 — finalization continues. Deploy on V1+V2 during failure, verify LFB advances by 3+ blocks. Verify FT >= 0.1 on finalized blocks. Restart V3, deploy on all 3, verify all nodes (including readonly) converge with LFB spread <= 3.

**What it proves:** A 3-validator network with FTT=0.1 survives one validator failure without halting finalization.

### test_validator_failure_halts_finalization

**Config:** FTT=0.67, bonds 100/100/100, heartbeat, readonly
**Marker:** `@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")` — paused validator legitimately produces invalid-block log lines on resume.

Pause V3, wait for V3's process to actually halt, then deploy fresh blocks on V1+V2 and verify those specific blocks do NOT finalize. With FTT=0.67, FT for 2/3 = 0.33 which is NOT > 0.67. Restart V3, verify finalization resumes on all nodes.

After `v3.pause()`, the test calls `wait_for_node_quiet(v3)` which polls V3's HTTP API until it stops responding. This is required because SIGSTOP delivery is not instantaneous — V3's block-creation thread can keep producing blocks for 10+ seconds after `pause()` returns (observed in CI run 26122442592). Only once V3 is confirmed quiet does the test deploy V1+V2 strings; those deploys land in blocks V3 had no chance to vote on.

The assertion tracks SPECIFIC post-pause block hashes (returned from `try_find_deploy`) and polls `is_finalized()` on each for a scaled 30s observation window (`timeouts.custom(30)`). LFB number advancement is allowed and irrelevant — pre-pause blocks whose finalization was already in flight can legitimately advance the LFB without violating safety. Only the post-pause blocks reflect the steady-state property.

**What it proves:** FTT=0.67 (production default) requires all 3 equal-stake validators. Once V3 is dead, new V1+V2-only blocks cannot finalize — the safety margin is enforced.

### test_ftt_boundary_is_inclusive

**Config:** FTT=0.5, bonds 75/75/50, heartbeat, readonly

Kill V3 (50 stake). V1+V2 (150 stake) have FT = (150\*2 - 200) / 200 = 0.5. The comparison is inclusive >=, so 0.5 IS >= 0.5 and the post-pause blocks finalize on V1+V2 stake alone. Each (node, block) pair is polled independently so a partial result names which one never crossed. Restart V3, verify finalization continues.

The bond split exists to land FT EXACTLY on the threshold — the only point where >= and > disagree. `ft_decides_exact` evaluates `2*q*den >= S*(den+num)` and every production caller passes `strict=false` (four sites in `floor.rs`); the strict arm is reachable only from unit tests.

**What it proves:** FTT is the minimum tolerable margin, so meeting it exactly is sufficient to finalize. Finalization BELOW the threshold is a separate property, covered by `test_validator_failure_halts_finalization`.

### test_epoch_transition_under_heartbeat

**Config:** FTT=0.1, bonds 100/100, epoch-length=4, heartbeat, readonly, joiner wallet seeded

Bond VALIDATOR4 through `bond.rho` with a deliberately dominant stake of 10,000,000. Resolve its canonical finalized block before the joiner starts. Advance the LFB beyond that checkpoint, and then start the joiner. Finalization cannot continue after activation unless the joiner participates. Verify the active bonds map and a block from V4.

**What it proves:** Epoch-based validator activation works under production conditions (heartbeat, real FTT). The epoch transition doesn't stall finalization, and the activated joiner actually participates.

### test_merge_determinism_asymmetric_divergence

**Config:** FTT=0.1, bonds 60/20/15, heartbeat, readonly

Pause V1 (heaviest validator, 60 stake) for 30 seconds. V2+V3 produce independent blocks during pause. Unpause V1 — it receives diverged tips and must merge. Deploy on all validators to stimulate convergence. Verify all nodes (including readonly) agree on post-state hash for the LFB. Verify FT >= 0.1 on all validators. Verify LFB spread <= 3.

**What it proves:** With unequal stakes, the DAG merge and LCA computations produce identical results across all validators even when the heaviest validator has a different DAG view. Regression test for the InvalidBondsCache bug (Phase 1) and ConflictSetMerger (Phase 3).

## Key assertions

- **Recovery (FTT=0.1):** V1+V2 LFB advances by 3+ with V3 dead; FT >= 0.1; all nodes converge after restart
- **Halt (FTT=0.67):** after V3 is paused and confirmed quiet, the specific post-pause V1+V2 blocks remain non-finalized for 30s; resumes after V3 restart
- **Boundary (FTT=0.5):** LFB does NOT advance for 30s (FT=0.5 is not > 0.5); resumes after restart
- **Epoch:** LFB reaches target past epoch boundary; all nodes within 3 of target
- **Merge:** `assert_all_nodes_agree_on_block` on LFB; FT >= 0.1; spread <= 3

## Infrastructure used

- Per-test `Shard.create()` / `shard.destroy()` with custom configs
- `shard.add_joiner()` for epoch transition test
- `Node.pause()` / `Node.unpause()` for validator failure simulation
- `wait_for_lfb_at_least` / `lfb_number` ([`infra/polling.py`](../infra/polling.py)) for causal LFB-based waits; `wait_for_node_quiet` for confirming pause has taken effect; `try_find_deploy` + per-block `is_finalized()` polling for the steady-state safety assertion
- `assert_all_nodes_agree_on_block()` for post-state agreement
- `wait_for_block_visible()` / `wait_for_deploy_included()` for synchronization
- `check_node_logs_after_test` autouse fixture for fatal-log detection (panics + `FATAL_PATTERNS`; see [ARCHITECTURE.md § 7](ARCHITECTURE.md#7-log-scanning))
- Readonly node included in all tests for observer consistency verification

## Related

- [consensus-configuration.md](../../../docs/consensus-configuration.md) -- FTT values, finalization formula, configuration guide
- [test_asymmetric_bonds](test_asymmetric_bonds.md) -- FT monotonicity and agreement with unequal stakes
- [test_convergence](test_convergence.md) -- DAG divergence recovery (shared shard, different focus)
- [test_bonding_validators](test_bonding_validators.md) -- epoch-based activation with manual propose

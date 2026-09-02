# test_validator_lifecycle

## Purpose

Walks three joiner validators (V4/V5/V6) through the **entire PoS validator lifecycle** on a dedicated shard, under **always-on background load**, exercising the multi-parent merge of PoS state end-to-end: concurrent bond, activation, rewards, concurrent bond+unbond, epoch-boundary shrink+grow, quarantine payout, and re-bond. It is the integration regression test for the seal-base / fresh-joiner-latest-message consensus fixes — the surfaces where the PoS bonds map previously diverged across nodes.

One comprehensive test on its own `provider.create_shard` shard (destroyed at the end), so it can mutate shard-wide bonded state freely. **Slashing is out of scope** (covered separately); everything else non-slashing is exercised.

**Config (module-scoped shard):** The shard has three genesis validators, three joiners, and one readonly observer.

The shard sets bond, epoch, and quarantine limits through `global_cli_options`. One additional purse has less than the minimum complete bond cost.

**Background load:** a `_BackgroundLoad` thread runs **the whole test, never paused** — same-vault transfers (`_BG_SRC → _BG_DST`) round-robin across the genesis producers, contending the SAME two IntegerAdd balance channels (the production merge surface) and generating netPhlo so rewards accrue. (The contract's standing-pool reward model has **no idle-no-accrual** property — see Phase 3 — so there is nothing to demonstrate by pausing.)

**Reads:** all PoS state goes through the **readonly** node (validators reject exploratory deploy in non-dev mode), via the **no-hash / FS(LFB)** form so every assertion lands on the sealed finalized state, not a speculative tip.

## The test (`test_validator_lifecycle`) — phases

1. **Concurrent bond V4+V5 + activation.** Both joiners brought online (LFS-synced, can't-propose pre-bond), then BOTH bonds submitted in one window (sibling blocks — the concurrent-multi-bond merge surface). Each finalizes on all nodes; the FS bonded set converges to the exact 5-validator set with **non-regression** (`_await_bonds_monotone`); each joiner activates, proposes a finalized block, is justified, and the full 5-node active set is live.
2. **Rejection branches:** Contract checks reject invalid bond and withdraw requests without changing bonds.

   State-bound admission rejects an underfunded bond. Every node records one rejection block, and the payer balance stays unchanged.
3. **Reward window 1:** under bg-on, FS rewards accrue **proportionally** to stake — hard gate `Δgenesis < ΔV4 < ΔV5` (weights 1:2:3) plus positive accrual (cases 1, 5). The contract distributes the **standing** posVault pool every epoch, so there is **no idle-no-accrual** property to assert (the earlier "case 2" was removed once that was understood).
4. **Concurrent bond V6 + withdraw V4 + withdraw V5** (#1) in one window — allBonds grows (V6) while pendingWithdrawers grows (V4,V5) across overlapping blocks. **Double-withdraw edge**: a 2nd withdraw of V4 is contract-clean either way — idempotent pending overwrite if still in allBonds, else `(false, "not bonded")` — assert no corruption.
5. **Epoch-move shrink + grow:** the next boundary runs `movePendingWithdrawer({V4,V5})` (allBonds shrinks) and keeps V6 active. V4/V5 leave `/validators` into `getWithdrawers`; V6 active. The post-shrink FS set converges (non-regression, V4/V5 volatile) and the boundary block's bonds field is **node-identical across all nodes** (the multi-element move fold — the seal-base surface).
6. **Reward window 2:** withdrawn V4/V5 are **frozen** (not in the active set → no accrual) while V6/genesis accrue (case 3).
7. **Post-unbond can't-propose:** withdrawn V4's `propose()` raises; active V6 proposes a finalized block.
8. **Multi-element quarantine payout** (case 4): wait the multi-epoch quarantine; `payWithdraw` credits each vault `bond + committed reward`; `getWithdrawers` empties; a **withdraw-during-quarantine** is rejected `(false, "not bonded")`; the post-payout bonds field is node-identical across nodes.
9. **Re-bond after payout:** V4 re-bonds (committed-rewards row was deleted at payout) and starts at **net-0 rewards** (not re-initialized to a stale value).
10. **Read sanity:** `getCoopVault` / `getInitialPosVault` decode to sane tuples, and `getEpochLength` / `getQuarantineLength` match the shard's own CLI-pinned values (4 / 10 — the values the poll budgets assume).
11. **Commit-reveal randomness:** V1 commits a keccak256 image (`commitRandomImage`), then walks every `revealRandom` branch — commit happy, commit **already-committed**, reveal **not-found** (a validator that never committed), reveal **mismatch** (wrong preimage), and reveal **success** (the keccak preimage matches). Exercises the `randomImages` / `randomNumbers` state (`eth_hash.keccak` matches the contract's `rho:crypto:keccak256Hash`).
12. **`posVaultTransfer` permission guard:** a transfer from a non-PoS deployer key is denied `(false, "You have not permission to transfer.")` — the success path needs the PoS contract key and is not reachable from a test.
13. **Auth-token-gated system methods reject a bogus token:** The three system methods reject `Nil` before work and preserve state.

## Key assertions

- **Every PoS deploy finalizes on every node** (bond/withdraw/re-bond blocks via `assert_block_finalized_on_all_nodes`); all PoS reads are FS(LFB)-backed.
- **Cross-node FS bonds identity** at the bond, epoch-move, and quarantine-payout boundaries (`assert_bonds_map_consistent_across_nodes`) — the multi-element fold must be node-identical (the seal-base regression surface).
- **Bonds non-regression** (`_await_bonds_monotone`): a finalized bond never silently vanishes/changes except via an explicit unbond (volatile keys).
- **Rewards:** accrue under traffic, proportional by stake (1:2:3), frozen when idle (case 2) and when withdrawn (case 3), paid `bond + reward` at quarantine (case 4), net-0 on re-bond.
- **Background load** reconciled exactly (`_assert_bg_load_robust`): every transfer finalized on all nodes; the contended dst credit composes to exactly `N×amount` (no dropped/double-applied work); the gas-paying src debited by at least the total.

## Out of scope
**Slashing** is covered separately. The **active-validator cap** uses [test_active_validator_cap](test_active_validator_cap.md) because it requires a capped genesis.

The random beacon, `posVaultTransfer`, underfunded admission, and authentication-token rejection are in scope.

## Infrastructure used

- Module-scoped `Shard.create()` / `shard.destroy()` with custom bonds, funded purses, an underfunded purse, and explicit bond bounds
- `shard.attach_joiner()` + `_attach_prebond` (LFS-sync + pre-bond can't-propose)
- `_BackgroundLoad` (same-vault IntegerAdd contention, always-on for the whole test) + `_assert_bg_load_robust`
- `PosAPI` (`bond` / `withdraw` / `read_result` / `get_bonds` / `get_rewards` / `get_pending_withdrawer` / `get_withdrawers` / `get_coop_vault` / `get_initial_pos_vault` / `get_epoch_length` / `get_quarantine_length` / `commit_random_image` / `reveal_random` / `pos_vault_transfer` / `call_auth_gated_invalid_token`) and `VaultAPI.get_balance` — all FS-backed via the readonly node
- `_submit_bonds` / `_submit_withdraw` / `_pos_call_result` / `_await_pending` / `_await_withdrawer` / `_wait_for_active` / `_await_bonds_monotone` / `_advance_lfb`
- `assert_block_finalized_on_all_nodes` (per-block, for specific PoS/bond blocks) / `assert_bonds_map_consistent_across_nodes` / `wait_for_finalized` / `poll_until` ([`infra/polling.py`](../infra/polling.py))
- `_assert_bg_load_deploys_finalized` — bg-load orphan-regression check; a thin wrapper over the shared `assert_all_deploys_finalized_on_all_nodes`. **Deploy-status based, re-homing-aware:** polls each node's `deploy_finalization_status`, so a transfer re-homed from a losing-fork block into a finalized descendant counts as finalized (vs the old find_deploy + block-hash check, which falsely flagged it as dropped work)
- `check_node_logs_after_test` autouse fixture for fatal-log detection (see [ARCHITECTURE.md § 7](ARCHITECTURE.md#7-log-scanning))

## Related

- [test_active_validator_cap](test_active_validator_cap.md) — the active-validator cap (`take(N)`), split into its own capped-genesis shard; shares helpers with this test
- [test_user_contract_concurrency](test_user_contract_concurrency.md) — the same merge-stress discipline on user-contract state (no PoS)
- [test_consensus_safety](test_consensus_safety.md) — consensus safety under failure / FTT / epochs
- [test_asymmetric_bonds](test_asymmetric_bonds.md) — FT/agreement under unequal stakes
- [consensus-configuration.md](../../../docs/consensus-configuration.md) — FTT, epoch/quarantine, finalization semantics

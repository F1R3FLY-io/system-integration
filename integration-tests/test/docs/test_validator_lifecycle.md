# test_validator_lifecycle

## Purpose

Walks three joiner validators (V4/V5/V6) through the **entire PoS validator lifecycle** on a dedicated shard, under **always-on background load**, exercising the multi-parent merge of PoS state end-to-end: concurrent bond, activation, rewards, concurrent bond+unbond, epoch-boundary shrink+grow, quarantine payout, and re-bond. It is the integration regression test for the seal-base / fresh-joiner-latest-message consensus fixes — the surfaces where the PoS bonds map previously diverged across nodes.

One comprehensive test on its own `provider.create_shard` shard (destroyed at the end), so it can mutate shard-wide bonded state freely. **Slashing is out of scope** (covered separately); everything else non-slashing is exercised.

**Config (module-scoped shard):** 3 genesis validators (v1/v2/v3 @ stake 100) + 3 joiners (V4=200, V5=300, V6=400) + readonly observer. `--bond-minimum=100 --bond-maximum=1000` (CLI; makes below-min/above-max/Mode-B reachable); `epoch-length=4`, `quarantine-length=10` (from `conf/rust.conf`). Distinct joiner stakes give reward weights (= bond / bond-minimum) genesis:V4:V5:V6 = 1:2:3:4, driving the reward-proportionality case.

**Background load:** a `_BackgroundLoad` thread runs **the whole test, never paused** — same-vault transfers (`_BG_SRC → _BG_DST`) round-robin across the genesis producers, contending the SAME two IntegerAdd balance channels (the production merge surface) and generating netPhlo so rewards accrue. (The contract's standing-pool reward model has **no idle-no-accrual** property — see Phase 3 — so there is nothing to demonstrate by pausing.)

**Reads:** all PoS state goes through the **readonly** node (validators reject exploratory deploy in non-dev mode), via the **no-hash / FS(LFB)** form so every assertion lands on the sealed finalized state, not a speculative tip.

## The test (`test_validator_lifecycle`) — phases

1. **Concurrent bond V4+V5 + activation.** Both joiners brought online (LFS-synced, can't-propose pre-bond), then BOTH bonds submitted in one window (sibling blocks — the concurrent-multi-bond merge surface). Each finalizes on all nodes; the FS bonded set converges to the exact 5-validator set with **non-regression** (`_await_bonds_monotone`); each joiner activates, proposes a finalized block, is justified, and the full 5-node active set is live.
2. **Rejection branches** (non-mutating): already-bonded, below-minimum, above-maximum, withdraw-not-bonded, and **Mode-B deposit-fail** (wallet funded just over the phlo precharge but under the bond amount → `(false, "Bond deposit failed")`). Each is a SUCCESSFUL deploy whose contract returns `(false, reason)`; the bonds map is asserted unchanged.
3. **Reward window 1:** under bg-on, FS rewards accrue **proportionally** to stake — hard gate `Δgenesis < ΔV4 < ΔV5` (weights 1:2:3) plus positive accrual (cases 1, 5). The contract distributes the **standing** posVault pool every epoch, so there is **no idle-no-accrual** property to assert (the earlier "case 2" was removed once that was understood).
4. **Concurrent bond V6 + withdraw V4 + withdraw V5** (#1) in one window — allBonds grows (V6) while pendingWithdrawers grows (V4,V5) across overlapping blocks. **Double-withdraw edge**: a 2nd withdraw of V4 is contract-clean either way — idempotent pending overwrite if still in allBonds, else `(false, "not bonded")` — assert no corruption.
5. **Epoch-move shrink + grow:** the next boundary runs `movePendingWithdrawer({V4,V5})` (allBonds shrinks) and keeps V6 active. V4/V5 leave `/validators` into `getWithdrawers`; V6 active. The post-shrink FS set converges (non-regression, V4/V5 volatile) and the boundary block's bonds field is **node-identical across all nodes** (the multi-element move fold — the seal-base surface).
6. **Reward window 2:** withdrawn V4/V5 are **frozen** (not in the active set → no accrual) while V6/genesis accrue (case 3).
7. **Post-unbond can't-propose:** withdrawn V4's `propose()` raises; active V6 proposes a finalized block.
8. **Multi-element quarantine payout** (case 4): wait the multi-epoch quarantine; `payWithdraw` credits each vault `bond + committed reward`; `getWithdrawers` empties; a **withdraw-during-quarantine** is rejected `(false, "not bonded")`; the post-payout bonds field is node-identical across nodes.
9. **Re-bond after payout:** V4 re-bonds (committed-rewards row was deleted at payout) and starts at **net-0 rewards** (not re-initialized to a stale value).
10. **Read sanity:** `getCoopVault` / `getInitialPosVault` decode to sane tuples, and `getEpochLength` / `getQuarantineLength` match `conf/rust.conf` (4 / 10 — the values the poll budgets assume).
11. **Commit-reveal randomness:** V1 commits a keccak256 image (`commitRandomImage`), then walks every `revealRandom` branch — commit happy, commit **already-committed**, reveal **not-found** (a validator that never committed), reveal **mismatch** (wrong preimage), and reveal **success** (the keccak preimage matches). Exercises the `randomImages` / `randomNumbers` state (`eth_hash.keccak` matches the contract's `rho:crypto:keccak256Hash`).
12. **`posVaultTransfer` permission guard:** a transfer from a non-PoS deployer key is denied `(false, "You have not permission to transfer.")` — the success path needs the PoS contract key and is not reachable from a test.
13. **Mode-A out-of-phlo bond:** a bond with a phlo limit too small to complete runs out of phlo → the deploy **errors** (full phlo charged, bond not applied), distinct from Mode-B's clean `(false, msg)`. The block must still finalize on all nodes; if it does not, the assertion surfaces it **as Issue A** ([f1r3node-rust#47](https://github.com/F1R3FLY-io/f1r3node-rust/issues/47) — out-of-phlo play/replay divergence) rather than silently skipping the case.
14. **Auth-token-gated system methods reject a bogus token:** `chargeDeploy` / `refundDeploy` / `closeBlock` are user-callable (single write-enabled PoS bundle) but reject a `Nil` token with `(false, "Invalid system auth token")` **before any work** — asserted with no state change.

## Key assertions

- **Every PoS deploy finalizes on every node** (bond/withdraw/re-bond blocks via `assert_block_finalized_on_all_nodes`); all PoS reads are FS(LFB)-backed.
- **Cross-node FS bonds identity** at the bond, epoch-move, and quarantine-payout boundaries (`assert_bonds_map_consistent_across_nodes`) — the multi-element fold must be node-identical (the seal-base regression surface).
- **Bonds non-regression** (`_await_bonds_monotone`): a finalized bond never silently vanishes/changes except via an explicit unbond (volatile keys).
- **Rewards:** accrue under traffic, proportional by stake (1:2:3), frozen when idle (case 2) and when withdrawn (case 3), paid `bond + reward` at quarantine (case 4), net-0 on re-bond.
- **Background load** reconciled exactly (`_assert_bg_load_robust`): every transfer finalized on all nodes; the contended dst credit composes to exactly `N×amount` (no dropped/double-applied work); the gas-paying src debited by at least the total.

## Out of scope
**Slashing** (covered on the dev branch) and the **active-validator cap** — split into [test_active_validator_cap](test_active_validator_cap.md) because it needs a contradictory *capped* genesis. Also `refundDeploy`'s internal `(Bug found)` transfer-failure branch (a should-never-happen, not user-reachable) and reward numeric-formula exactness (behavioral assertions only). The random-beacon (`commit/revealRandom`), `posVaultTransfer`, Mode-A out-of-phlo, and the auth-token reject branches are **now in scope** (Phases 11-14).

## Infrastructure used

- Module-scoped `Shard.create()` / `shard.destroy()` with a custom `ShardConfig` (genesis bonds + extra funded wallets for joiners, bg pair, throwaways, and the Mode-B wallet) and `global_cli_options` for bond bounds
- `shard.attach_joiner()` + `_attach_prebond` (LFS-sync + pre-bond can't-propose)
- `_BackgroundLoad` (same-vault IntegerAdd contention, always-on for the whole test) + `_assert_bg_load_robust`
- `PosAPI` (`bond` / `withdraw` / `read_result` / `get_bonds` / `get_rewards` / `get_pending_withdrawer` / `get_withdrawers` / `get_coop_vault` / `get_initial_pos_vault` / `get_epoch_length` / `get_quarantine_length` / `commit_random_image` / `reveal_random` / `pos_vault_transfer` / `call_auth_gated_invalid_token`) and `VaultAPI.get_balance` — all FS-backed via the readonly node
- `_submit_bonds` / `_submit_withdraw` / `_pos_call_result` / `_await_pending` / `_await_withdrawer` / `_wait_for_active` / `_await_bonds_monotone` / `_advance_lfb`
- `assert_block_finalized_on_all_nodes` / `assert_bonds_map_consistent_across_nodes` / `wait_for_finalized` / `poll_until` ([`infra/polling.py`](../infra/polling.py))
- `check_node_logs_after_test` autouse fixture for fatal-log detection (see [ARCHITECTURE.md § 7](ARCHITECTURE.md#7-log-scanning))

## Related

- [test_active_validator_cap](test_active_validator_cap.md) — the active-validator cap (`take(N)`), split into its own capped-genesis shard; shares helpers with this test
- [test_user_contract_concurrency](test_user_contract_concurrency.md) — the same merge-stress discipline on user-contract state (no PoS)
- [test_consensus_safety](test_consensus_safety.md) — consensus safety under failure / FTT / epochs
- [test_asymmetric_bonds](test_asymmetric_bonds.md) — FT/agreement under unequal stakes
- [consensus-configuration.md](../../../docs/consensus-configuration.md) — FTT, epoch/quarantine, finalization semantics

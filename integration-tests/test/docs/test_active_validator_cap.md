# test_active_validator_cap

## Purpose

Verifies the PoS **active-validator cap**. `pickActiveValidators` does `pks.take($$numberOfActiveValidators$$).toSet()`, so even when more validators are bonded than the cap allows, only `numberOfActiveValidators` are promoted into the **active** set (the consensus committee). The over-cap bonded validators stay **bonded-but-inactive**.

Lives in its **own file** rather than woven into [test_validator_lifecycle](test_validator_lifecycle.md) for two reasons:

- the cap is a **genesis-time parameter** (`--number-of-active-validators`, immutable post-boot), and the lifecycle shard must boot **uncapped** so its joiners can all activate (it peaks at 6 active) — contradictory genesis requirements; and
- the subprocess provider uses **fixed per-session data dirs** (`<session>/<role>`), so two live module-scoped shards in one file collide on them. As a separate module, this shard is created only after the previous module tears its shard down, reusing the dirs sequentially. **Caveat:** do **not** run under `--keep-running`, which keeps the prior shard's nodes alive and re-introduces the collision (`DAG storage is missing hash`).

**Config (module-scoped shard):** 3 genesis validators (v1/v2/v3 @ stake 100) + readonly, booted with `--number-of-active-validators=3` (cap == genesis count) and `--bond-minimum=100 --bond-maximum=1000`. Two joiner wallets (V4=200, V5=300) funded so they can bond over the cap.

**Reads:** the active set via the readonly node's HTTP `/api/validators` (FS-backed); the full bonded map via `pos.get_bonds()` (FS-backed exploratory read on readonly).

## The test (`test_active_validator_cap`)

1. **Cap full at genesis** — poll `/validators` until the active set == 3 (genesis fills the cap exactly).
2. **Bond over the cap** — bring V4 and V5 online (`_attach_prebond`) and bond both **directly** (not via `_submit_bonds`, which would wait for active-set membership the cap denies). Five validators now bonded.
3. **Re-pick on the grown set** — wait until `allBonds == 5`, then cross an epoch boundary (`_advance_lfb`) so `pickActiveValidators` re-runs on the 5-bonded set.
4. **Assert the cap holds** — exactly **3 active** (`/validators`) despite **5 bonded** (`getBonds`), and `active ⊆ bonded` (the 2 over-cap joiners are bonded-but-inactive).
5. **Liveness under the cap** — a deploy from an **active** genesis finalizes on all nodes; the capped shard still makes progress.

## Key assertions

- **`len(active) == numberOfActiveValidators`** (3) while **`len(bonded) == 5`** — the `take(N)` cap is enforced.
- **`active ⊆ bonded`** — the active set is always a subset of the bonded set.
- **Liveness preserved** — the capped committee still finalizes on every node (`assert_block_finalized_on_all_nodes`).

**Count-based by design:** *which* 3 validators are active depends on public-key sort order (`take` of the sorted bond map), so the test asserts the active *count* == cap, not *which* members.

## Out of scope / caveats

- Exact active-set membership (pubkey-order-dependent) is not asserted — only the count.
- This deliberately creates **bonded-but-inactive running validators**, an unusual state; how those heartbeat nodes behave (e.g. whether they propose blocks peers reject) is a separate stability question this test does not deeply probe. For a purely deterministic check of the `take(N)` logic, a Rust unit test of `pickActiveValidators` is the lighter-weight complement.

## Infrastructure used

- Module-scoped `Shard.create()` / `shard.destroy()` with a capped `ShardConfig` (`global_cli_options` includes `--number-of-active-validators`)
- Shared lifecycle helpers imported from [test_validator_lifecycle](test_validator_lifecycle.md): `_attach_prebond`, `_validators_on`, `_vault_addr`, `_advance_lfb`, plus the stake / phlo constants (no duplication)
- `PosAPI` (`bond` / `read_result` / `get_bonds`); `assert_block_finalized_on_all_nodes` / `wait_for_deploy_included` / `wait_for_finalized` / `poll_until` ([`infra/polling.py`](../infra/polling.py))
- `check_node_logs_after_test` autouse fixture for fatal-log detection (see [ARCHITECTURE.md § 7](ARCHITECTURE.md#7-log-scanning))

## Related

- [test_validator_lifecycle](test_validator_lifecycle.md) — the full PoS lifecycle (uncapped); shares helpers with this test
- [consensus-configuration.md](../../../docs/consensus-configuration.md) — active validators, epoch / finalization semantics

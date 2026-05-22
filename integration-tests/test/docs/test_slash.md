# test_slash

## Purpose

End-to-end Docker integration tests for the slashing pipeline. Each test spawns a custom 2- or 3-validator shard with heartbeat disabled and FTT=-1 via `Shard.create`, then uses `infra/p2p_client.NodeClient` to inject crafted malicious blocks over the TLS-encrypted P2P transport (`routing.proto`). The receiving validator's logs are regex-scraped via `wait_for_log_match`, and post-slash bonds are verified via gRPC `block_info`.

**Linux-only.** `NodeClient` requires native Linux Docker bridge routing for host-to-container traffic; every test is `@pytest.mark.skipif`'d on macOS, Windows, and WSL2.

`rust_block_hash()` in [`infra/p2p_client.py`](../infra/p2p_client.py) mirrors Rust's `casper/src/rust/util/proto_util.rs:380 hash_block` byte layout so forged blocks pass `Validate::block_hash` and reach the intended offense. The upstream `pyf1r3fly` helper `gen_block_hash_from_block` boxes `sigAlgorithm` / `seqNum` / `shardId` in protobuf `StringValue` / `Int32Value` wrappers (2-byte tag-length prefixes) and diverges from the Rust node; each active test re-asserts the shim/Rust equivalence on its first `block_request` so an upstream divergence surfaces immediately.

Every test is marked with `@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")` so the autouse log scanner in `check_node_logs_after_test` does not fail the test on the very `"Recording invalid block ... for <Variant>."` line the test is asserting against.

## Tests (19)

| Test | What it does |
|------|-------------|
| `test_slash_invalid_block_hash` | V1 re-signs a valid block with an `evil` blake2b digest and ships it to V2. V2 records `InvalidBlockHash`, proposes a new block, and V1's bond drops to 0 |
| `test_slash_invalid_block_number` | V1 ships a block with `blockNumber = 5` (not `max(parents)+1 = 2`). V2 records `InvalidBlockNumber` and slashes V1. (Stays inside `epoch_length=10` so `slash_evidence_epoch_matches_target` accepts the evidence.) |
| `test_slash_invalid_block_seq` | V1 ships a block with `seqNum = 1000` (not previous+1). V2 records `InvalidSequenceNumber` and slashes V1 |
| `test_slash_justification_not_correct` | 3-validator shard. V1 ships a block with an extra `Justification` entry from an unknown random key. V2 records `InvalidFollows` and slashes V1 |
| `test_slash_unauthorized_slash_deploy` | **H3 (T-11, attack-tree A2)** — 3-validator shard. V2 proposes a block carrying a forged `SlashSystemDeploy` that cites a non-existent invalid_block_hash. V3 records V2's block as `UnauthorizedSlashDeploy` (rule #3 of `validate_received_slash_deploys` — referenced block unknown to DAG) and slashes V2. V1 (alleged target) remains bonded |
| `test_slash_references_valid_block` | **H4 (T-Auth wire-level sibling)** — 3-validator shard. V2 proposes a block whose `SlashSystemDeploy` references V1's legitimate first block as if it were slashable evidence. V3 records V2's block as `UnauthorizedSlashDeploy` (rule #4 — referenced block known but not flagged invalid) and slashes V2. T-Auth proper (spoofed-token) is in-tree only via `uc_21_auth_token_check.rs` because auth tokens are unforgeable Rholang names |
| `test_slash_self_regression` | **M1 (T-7, bug #6)** — V1 proposes block A; V1 forges a successor B whose creator-self justification points behind A (regressing V1's latest-message). V2 records `JustificationRegression` and slashes V1 |
| `test_slash_invalid_bonds_cache` | **M3 (T-26)** — V1 ships a block whose `body.state.bonds` differs from the post-state replay's computed bonds. V2 records `InvalidBondsCache` and slashes V1 |
| `test_slash_invalid_repeat_deploy` | **M4 (T-29)** — V1 ships two successive blocks containing the same `deployId`. V2 records the second block as `InvalidRepeatDeploy` and slashes V1 |
| `test_slash_GHOST_disobeyed` | 3-validator shard. V1 ships an off-GHOST block (parents mutated away from the GHOST winner + deploy replaced). V2 records the block as `InvalidTransaction` (Rust's `validate_block_checkpoint` runs before `parents`, so the wire-level variant is `InvalidTransaction`, not the formal `InvalidParents` — both are slashable; V1's bond drops to 0). Formal `InvalidParents` semantics are covered in-process by `casper/tests/slashing/integration_t_invalid_parents.rs` via `propose_with_block_mutation` which sidesteps the wire-level replay constraint |
| `test_node_working_right_after_slashing` | Same flow as `test_slash_invalid_block_hash`, but additionally verifies the slashing block contains exactly one `slashSystemDeploy` and the next normal block contains zero (slash deploy emitted once per offense) |
| `test_slash_invalid_validator_approve_evil_block` | Level-2 closure ("neglect of an invalid block"). 3-validator shard: V1 ships a hash-tampered block to V2; V2 (heartbeat off) crafts an "approve" block that cites V1's invalid block in justifications and ships to V3; V3 records V2's block as `InvalidTransaction` (post-state replay diverges from V1's copied post-state) and emits TWO slash deploys in one propose round — both V1 and V2 bonds drop to 0. Exercises `prepare_slashing_deploys`'s uncapped emission of `authorized_slash_candidates` |
| `test_slash_ignorable_equivocation` | **H2 (T-2, Bug #1 wire-level regression)** — V1 honestly proposes b1; the test forges a sibling `b1p` via the timestamp-+1 trick (same body, +1ms timestamp, re-hashed, re-signed). V2 receives both. The detector returns `IgnorableEquivocation` (the receiver did not request b1p by hash, so `requested_as_dependency == false`). After bug-#1 fix this variant is slashable; the test pins that post-fix behaviour against the deployed binary |
| `test_slash_admissible_equivocation` | **H1 (T-1)** — Same sibling-forge as H2, but the test additionally ships a V2-signed "child of b1p" block FIRST so V2's block processor buffers it pending the missing dependency b1p (flipping `requested_as_dependency(b1p)` to true). After waiting for the `"waiting on missing dependencies"` log line, the test ships b1p; the detector returns `AdmissibleEquivocation` |
| `test_slash_neglected_equivocation` | **H5 (T-33)** — 3-validator shard. V1 equivocates (b1/b1p). V2 builds a block citing b1p in justifications WITHOUT a SlashDeploy (forged on the wire because V2 wouldn't do this naturally). V3 records V2's block as `NeglectedInvalidBlock` or `InvalidTransaction` (variant alternates because `validate_block_checkpoint` runs before `neglected_invalid_block`; semantic outcome — V1=0 AND V2=0 in one V3 propose — is preserved). Wire-level pure-`NeglectedEquivocation` is unreachable; in-tree analog `uc_04_neglect_two_level.rs` covers it via test-only helpers |
| `test_slash_late_released_equivocation` | **M6 (T-36 §5.A.5)** — V1 proposes b1, withholds the equivocating sibling b1p. V2 proposes 4 blocks atop b1 (advancing the chain inside epoch 0 so the slash authorization predicate remains satisfied). V1 then releases b1p; V2 detects equivocation (V1's latest-message in V2's DAG is still b1 throughout, so `JustificationRegression` does not interfere) and slashes V1 |
| `test_slash_stale_evidence_rebond` | **M2 (T-12, bug #15)** — 3-validator shard with `--epoch-length=2`. V1 forges an `InvalidBlockNumber` block at epoch 0. V2 and V3 propose naturally into epoch ≥1 (proposer-side filter drops V1's stale evidence so V1 stays bonded). V2 then forges a slash deploy citing V1's epoch-0 evidence with `targetActivationEpoch=0` onto its current-epoch propose. V3 records V2's block as `UnauthorizedSlashDeploy` (rule #2 — `EpochMismatch`) and slashes V2. V1 remains bonded — proposer-side and receive-side stale-evidence filters are mutually consistent |
| `test_slash_self_correcting_block_admitted` | **M5 (T-34, bug #9)** — 3-validator shard. V1 forges an `InvalidBlockHash` block; V2's natural propose response carries a SlashDeploy against V1. V3 admits V2's self-correcting block via the bug-#9 widening at `validate.rs:1080-1092` (`neglected_invalid_justification && !has_slash_system_deploys` short-circuits to false when V2's block carries a slash). V1 is slashed inside V2's block; V2 remains bonded. Reinterprets the task's literal "same (sender, seqNum)" framing per spec §10.9 — that framing is equivocation, not self-correction; the bug-#9 widening is for a different-validator slasher |
| `test_no_false_positive_slash_on_propose_imbalance` | **B1 (T-12PF safety arm)** — 3-validator shard. Pins the ABSENCE of over-eager behavioral-pattern detectors. V1 dominates the propose chain (5 proposes in a row); V2 stays silent (no deploys, no proposes); V3 proposes once at the end. Asserts all bonds remain at 100. Test PASSES iff no over-eager fairness detector exists; would FAIL the moment a regression introduces one. The protocol's *liveness* arm of T-12PF cannot be wire-tested (no detector to fire), but its *safety* arm (no wrongful slashing under proposer unfairness) is testable, and this test is it |

## Carve-out — M7 (T-37 / T-12PF liveness arm)

`test_slash_censoring_proposer_eventually_slashes` is intentionally NOT present. The reason is stronger than "no runtime detector exists": **the conventional censorship threat is structurally undefined in this protocol's wire semantics.** Deploys are not gossiped — `casper/src/rust/casper_engine/block_admission.rs::admit_deploy` stores them in the local node's `KeyValueDeployStorage`, the block creator reads from that same local storage, and no code path broadcasts a deploy to peers. A deploy submitted to V2 stays on V2 until V2 proposes it; V1 cannot "censor" V2's deploys because V1 never has them. T-12PF is therefore correctly a *boundary assumption* — a positive design finding about the protocol's author-local-mempool semantics, not a deferred TODO.

In-tree property tests cover the boundary classification. The *safety* arm of T-12PF is covered by `test_no_false_positive_slash_on_propose_imbalance` above; the wire-level withholding theme is covered by `test_slash_late_released_equivocation`.

## Key assertions

- **Slash variants:** `wait_for_log_match` matches `"Recording invalid block <hash[:10]>... for <Variant>."` on the receiving validator
- **Bond zeroing:** post-slash `block_info.bonds[offender] == 0`; non-offenders remain bonded
- **Slash-deploy emission:** exactly one `slashSystemDeploy` per offense (verified by `is_exist_slash_deploy`)
- **Hash compatibility:** every test's first `block_request` re-asserts `rust_block_hash(block_msg) == block_msg.blockHash`

## Infrastructure used

- Per-test `Shard.create()` / `shard.destroy()` via the `_slash_shard` context manager
- `infra/p2p_client.py` — `NodeClient`, `p2p_protocol_client`, `rust_block_hash`, `generate_block_hash`, `is_exist_slash_deploy`
- `DockerNodeHandle.container_ip` / `.peer_cert` / `.peer_key` (exposed via `Node.peer_ip` / `.peer_cert` / `.peer_key`)
- `wait_for_block_visible` / `wait_for_log_match` ([`infra/polling.py`](../infra/polling.py))
- `@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")` on every test
- `@_SKIP_ON_NON_LINUX` on every test (`sys.platform in ('win32', 'cygwin', 'darwin')`)

## Related

- [slashing-test-plan.md](../../../docs/slashing-test-plan.md) — full coverage matrix and design rationale
- [slashing-mechanism.md](../../../docs/slashing-mechanism.md) — protocol-level slashing design
- [ARCHITECTURE.md § 7](ARCHITECTURE.md#7-log-scanning) — autouse log scanning and `allow_forbidden_patterns` opt-out
- f1r3node-rust `casper/tests/slashing/` — in-process complement to the wire-level tests here

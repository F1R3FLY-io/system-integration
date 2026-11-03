# test_slash

## Purpose

Wire-level tests of how nodes judge and punish misbehaving validators. Each test spawns a custom shard (two or three validators of stake 100 plus a read-only node, heartbeat off, `ftt=-1`), takes a real block, forges a variant a validator must not produce, signs it with that validator's key and sends it to a node over the P2P transport with `infra/p2p_client.NodeClient`. It then checks the node's verdict and its consequence.

Only equivocation is slashable. Every other offence is judged against the receiving node's own state, so the node rejects the block and its sender keeps its stake.

Runs under the subprocess and Docker providers. Under Docker the nodes reply to the client through the network gateway, which works only with native Linux networking, so the suite skips on other hosts there.

## Tests (19)

### Rejected, not slashed

| Test | Forgery | Expected |
|------|---------|----------|
| `test_slash_invalid_block_hash` | declared hash does not match the content | v2 ignores it without a verdict (`Ignoring block … does not match`) |
| `test_slash_invalid_block_number` | block number not one above its parents' | v2 records `InvalidBlockNumber` |
| `test_slash_invalid_block_seq` | sequence number not one above the previous block's | v2 records `InvalidSequenceNumber` |
| `test_slash_justification_not_correct` | extra justification from an unbonded key | v2 records `InvalidFollows` |
| `test_slash_unauthorized_slash_deploy` | slash of v1 citing a block no node holds | v3 records `UnauthorizedSlashDeploy` |
| `test_slash_references_valid_block` | slash of v1 citing v1's valid block | v3 records `UnauthorizedSlashDeploy` |
| `test_slash_self_regression` | own justification points behind its latest block | v2 records `JustificationRegression` |
| `test_slash_invalid_bonds_cache` | bonds field halves the sender's stake | v2 records `InvalidBondsCache` |
| `test_slash_invalid_repeat_deploy` | successor block repeats its deploy | v2 records `InvalidRepeatDeploy` |
| `test_slash_ghost_disobeyed` | parents skip the fork-choice winner, deploy replaced | v2 records `InvalidTransaction` (replay fails before the parent check) |
| `test_slash_invalid_validator_approve_evil_block` | v2's block cites v1's rejected block without slashing | v3 parks v2's block on the missing dependency and never admits it |

Each then has the judging node propose: its block carries no slash, and every validator's stake is unchanged.

### Equivocation: slashed

| Test | Scenario | Expected |
|------|----------|----------|
| `test_slash_ignorable_equivocation` | v2 receives a sibling of a block it holds | `IgnorableEquivocation`; v2's next block slashes v1 |
| `test_slash_admissible_equivocation` | v2 first asks for the sibling as a dependency | `AdmissibleEquivocation`; v2's next block slashes v1 |
| `test_slash_neglected_equivocation` | v2's block cites the sibling without slashing | v3 rejects v2's block; v3's next block slashes v1, not v2 |
| `test_slash_self_correcting_block_admitted` | v2's block cites the sibling and carries the slash | v3 admits it; v1 slashed, v2 and v3 keep their stake |
| `test_node_working_right_after_slashing` | v2 proposes again after slashing | **expected failure**: the slash is re-issued |
| `test_slash_late_released_equivocation` | sibling released after v2 built four blocks | **expected failure**: node defect |
| `test_slash_stale_evidence_rebond` | sibling arrives in a later epoch; v2 forges a slash on it | **expected failure**: node defect |

### Safety of uneven proposing

`test_no_false_positive_slash_on_propose_imbalance`: v1 proposes five blocks in a row, v2 stays silent, v3 proposes once; every stake is unchanged.

## Expected failures

- **Late-arriving equivocation** (`test_slash_late_released_equivocation`, `test_slash_stale_evidence_rebond`): once an equivocation reaches an honest validator after the chain has built past it, that validator's next block fails its own validation as `NeglectedEquivocation`, and it cannot propose.
- **Repeated slash** (`test_node_working_right_after_slashing`): proposers decide a target is still bonded from the parent's `bonds` field, which records the committee at the finalized floor, so the same slash is issued in later blocks until the floor passes it.

Both are strict: they fail the suite once the behaviour changes.

## Key assertions

- **Verdict:** the receiving node logs `Recording invalid block <hash[:10]>... for <Variant>.`, or `Ignoring block` for a hash mismatch.
- **Stake:** read from PoS state after a block (`readonly.pos.get_bonds(block_hash)`). A block's `bonds` field is the committee at its finalized floor and does not show its own slash.
- **Slash deploy:** whether a block carries one (`is_exist_slash_deploy`).
- **Hash:** every fetched block must hash with pyf1r3fly's `gen_block_hash_from_block` to its own `blockHash`.

## Infrastructure used

- `infra/p2p_client.py`: `NodeClient` handshakes for the node's network and chain identity, fetches blocks and pushes forged ones.
- `Node.p2p_address()`, `Node.tls_certificate()`, `Node.p2p_reply_host`, implemented by each provider.
- `@pytest.mark.allow_forbidden_patterns(...)` per test, allowing exactly the log patterns its forgery provokes.

## Related

- [slashing-test-plan.md](../../../docs/slashing-test-plan.md): the original plan and its coverage mapping
- [slashing-mechanism.md](../../../docs/slashing-mechanism.md): protocol-level slashing design

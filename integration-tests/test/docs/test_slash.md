# test_slash

## Purpose

How a live shard judges and punishes misbehaving validators. Each test runs a shard as production does: heartbeat on, the shard's fault-tolerance threshold and epoch, four validators of stake 100/200/300/400 and a read-only observer. Misbehaviour is injected over the P2P transport with `infra/p2p_client.NodeClient`, as blocks signed with a validator's key and built from blocks that validator really produced.

Only equivocation is slashable. Every other offence is judged against the receiving node's own state, so the node rejects the block and its sender keeps its stake.

Each test checks the outcome the way the network must agree on it: every receiving node reaches the verdict, a slash finalizes on every node, finalized stake and the committee are identical everywhere, and the chain keeps finalizing.

Runs under the subprocess and Docker providers. Under Docker the nodes reply to the client through the network gateway, which works only with native Linux networking, so the suite skips on other hosts there.

## Tests (3)

### `test_rejected_offences_do_not_punish`

v1 and v2 are attackers: they propose only when the test tells them to (`--heartbeat-disabled` on those two nodes), so a block one of them forges from is still its latest. Each offence is sent to every other node, and every one of them must reach the verdict.

| Offence | Verdict |
|---------|---------|
| declared hash does not match the content | ignored without a verdict (`Ignoring block … because block hash`) |
| block number not one above its parents' | `InvalidBlockNumber` |
| sequence number not one above the previous block's | `InvalidSequenceNumber` |
| justification from an unbonded key | `InvalidFollows` |
| bonds field halves the sender's stake | `InvalidBondsCache` |
| successor block repeats its deploy | `InvalidRepeatDeploy` |
| parents skip the fork-choice winner | `InvalidTransaction` (replay fails before the parent check) |
| slash citing a block no node holds | `UnauthorizedSlashDeploy` |
| slash citing a valid block | `UnauthorizedSlashDeploy` |
| block built on a rejected block | parked on the missing dependency, never admitted |
| own justification behind its latest block | `JustificationRegression` |

Then: the chain keeps finalizing, no forged block and no slash finalizes, finalized stake and the committee are unchanged and identical on every node, and all four validators still get blocks finalized.

### `test_equivocation_is_slashed_network_wide`

1. v1 signs a sibling of its latest block and goes quiet; only v2 receives the sibling (`IgnorableEquivocation`).
2. v2 does the same; v3 first receives a block built on v2's sibling, asks for the sibling as a dependency, then receives it (`AdmissibleEquivocation`).
3. Once the epoch of v1's evidence has passed, a block re-citing it carries a slash for the old epoch; v4 and the observer reject it (`UnauthorizedSlashDeploy`) and it never finalizes.

A slash is authorized only in its evidence's epoch, so each equivocation starts with room left in the shard's epoch. After each slash: it finalizes on every running node; finalized stake and the committee agree everywhere; every honest validator still gets blocks finalized. Honest proposers that see the evidence first may each slash in parallel blocks, and the merge keeps one; no block may slash again on top of a block that already did.

### `test_uneven_proposing_is_not_punished`

v2 stays silent for 40 blocks while the others propose, then returns. Nobody is slashed, stake and the committee are unchanged on every node, and v2 gets blocks finalized again.

## Key assertions

- **Verdict:** each receiving node logs `Recording invalid block <hash[:10]>... for <Variant>.`, or `Ignoring block` for a hash mismatch.
- **Finalized slash:** slashes are read from blocks every node has finalized (`slash_evidence`), keyed by the evidence they cite.
- **Stake:** read from the finalized state (`readonly.pos.get_bonds()`). A block's `bonds` field is the committee at its finalized floor and does not show its own slash.
- **Committee:** the bonds field of a block every node has finalized, identical on every node.
- **Hash:** every fetched block must hash with pyf1r3fly's `gen_block_hash_from_block` to its own `blockHash`.
- **Forensics:** a failure carries every node's LFB, whether the chain is advancing, and the forbidden log patterns each node hit.

## Infrastructure used

- `infra/p2p_client.py`: `NodeClient` handshakes for the node's network and chain identity, fetches blocks and pushes forged ones; `slash_evidence` lists the evidence a block's slashes cite.
- `Node.p2p_address()`, `Node.tls_certificate()`, `Node.p2p_reply_host`, implemented by each provider.
- `ShardConfig.extra_wallets` funds validator 4's vault, which the default genesis wallets leave empty.
- `@pytest.mark.allow_forbidden_patterns(...)` per test, allowing exactly the log patterns its forgeries provoke.

## Related

- [slashing-test-plan.md](../../../docs/slashing-test-plan.md): the original plan and its coverage mapping
- [slashing-mechanism.md](../../../docs/slashing-mechanism.md): protocol-level slashing design

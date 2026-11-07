# test_slash

## Purpose

How a live shard judges and punishes misbehaving validators. Each test runs a shard as production does: heartbeat on, the shard's fault-tolerance threshold and epoch, four validators of stake 100/200/300/400 and a read-only observer. Misbehaviour is injected over the P2P transport with `infra/p2p_client.NodeClient`, as blocks signed with a validator's key and built from blocks that validator really produced.

Only equivocation is slashable: two blocks one key signed at one sequence number, in any order and however late the second arrives. Every other offence is judged against the receiving node's own state, so the node rejects the block and its sender keeps its stake. A slash carries both blocks' signed commitments, so every node checks the evidence the same way, whatever it holds.

Each test checks the outcome the way the network must agree on it: every receiving node reaches the verdict, a slash finalizes on every node, finalized stake and the committee are identical everywhere, and the chain keeps finalizing.

Runs under the subprocess and Docker providers. Under Docker the nodes reply to the client through the network gateway, which works only with native Linux networking, so the suite skips on other hosts there.

## Tests (6)

### `test_rejected_offences_do_not_punish` (4 shards)

v1 and v2 are attackers: they propose only when the test tells them to (`--heartbeat-disabled` on those two nodes). Each offence is the attacker's next block, built on its latest, with one thing wrong. A sender that has signed a block at a sequence number cannot sign another there without equivocating, so each attacker commits one such offence per shard and then stays silent; v3 and v4 hold 70% of the stake and keep finalizing. The test is parametrized over four pairs of offences, one shard each. Each offence is sent to every other node, and every one of them must reach the verdict.

| Offence | Sender | Verdict |
|---------|--------|---------|
| declared hash does not match the content | v1 | ignored without a verdict (`Ignoring block … because block hash`) |
| sequence number 1000 above the previous block's | v1 | `InvalidSequenceNumber` |
| block number not one above its parents' | v1 | `InvalidBlockNumber` |
| block built on v1's rejected block | v2 | parked on the missing dependency, never admitted |
| justification from an unbonded key | v1 | `InvalidFollows` |
| slash whose second block v1 never signed | v2 | `UnauthorizedSlashDeploy` |
| bonds field halves the sender's stake | v1 | `InvalidBondsCache` |
| slash citing two of v1's blocks at different sequence numbers | v2 | `UnauthorizedSlashDeploy` |
| block repeats its predecessor's deploy | v1 | `InvalidRepeatDeploy` |
| parents skip the fork-choice winner | v2 | `InvalidRejectedDeploy` when the merge facts the block records no longer match its parents, otherwise `InvalidTransaction` when its replay on those parents fails; which check notices first depends on the chain's shape |

Then: the chain keeps finalizing, no forged block and no slash finalizes, finalized stake and the committee are unchanged and identical on every node, and v3 and v4 still get blocks finalized.

### `test_equivocation_is_slashed_network_wide`

1. v1 moves on, then signs a sibling of its previous block and goes quiet; only v2 receives the sibling (`IgnorableEquivocation`).
2. v2 signs a sibling of its latest block and goes quiet; v3 first receives a block built on v2's sibling, asks for the sibling as a dependency, then receives it (`AdmissibleEquivocation`).
3. v3's next block slashes v1 again; v4 and the observer reject it (`UnauthorizedSlashDeploy`, v1 has no stake left) and it never finalizes. v3 then stays silent; v4 holds the stake that keeps finalizing.

After each slash: it finalizes on every running node; finalized stake and the committee agree everywhere; every honest validator still gets blocks finalized. Honest proposers that see the evidence first may each slash in parallel blocks, and the merge keeps one; no block may slash again on top of a block that already did.

### `test_uneven_proposing_is_not_punished`

v2 stays silent for 40 blocks while the others propose, then returns. Nobody is slashed, stake and the committee are unchanged on every node, and v2 gets blocks finalized again.

## Key assertions

- **Verdict:** each receiving node logs `Recording invalid block <hash[:10]>... for <Variant>.`, or `Ignoring block` for a hash mismatch.
- **Finalized slash:** slashes are read from blocks every node has finalized (`slash_evidence`), keyed by the hashes of the two blocks their evidence names, which pyf1r3fly recomputes from the commitments.
- **Stake:** read from the finalized state (`readonly.pos.get_bonds()`). A block's `bonds` field is the committee at its finalized floor and does not show its own slash.
- **Committee:** the bonds field of a block every node has finalized, identical on every node.
- **Hash:** every fetched block must hash with pyf1r3fly's `gen_block_hash_from_block` to its own `blockHash`.
- **Forensics:** a failure carries every node's LFB, whether the chain is advancing, and the forbidden log patterns each node hit.

## Infrastructure used

- `infra/p2p_client.py`: `NodeClient` handshakes for the node's network and chain identity, fetches blocks and pushes forged ones; `slash_evidence` lists the block-hash pair each of a block's slashes proves.
- `Node.p2p_address()`, `Node.tls_certificate()`, `Node.p2p_reply_host`, implemented by each provider.
- `ShardConfig.extra_wallets` funds validator 4's vault, which the default genesis wallets leave empty.
- `@pytest.mark.allow_forbidden_patterns(...)` per test, allowing exactly the log patterns its forgeries provoke.

## Related

- [slashing-test-plan.md](../../../docs/slashing-test-plan.md): the original plan and its coverage mapping
- [slashing-mechanism.md](../../../docs/slashing-mechanism.md): protocol-level slashing design

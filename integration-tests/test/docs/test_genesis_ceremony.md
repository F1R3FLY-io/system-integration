# test_genesis_ceremony

## Purpose

Validates that the genesis ceremony completed successfully across all nodes in a shard. The genesis ceremony is the bootstrap protocol that creates the initial block (block #0) with no parents, establishes the validator set, and transitions all nodes to the Running state.

Expected values are derived from `shared_shard.config` (bonds) and the `node_conf` fixture (shard_id from parsed config).

## How the genesis ceremony works

1. **Bootstrap** starts in `--ceremony-master-mode` and waits for `--required-signatures` validators to connect and sign the genesis block
2. **Validators** start with `--genesis-validator` and connect to the bootstrap node
3. Each validator signs the proposed genesis block and sends its signature back
4. Once enough signatures are collected, the bootstrap finalizes the genesis block
5. All nodes transition to Running state and begin accepting deploys

The ceremony is performed implicitly by the session-scoped `shared_shard` fixture at startup, which generates the genesis configuration and waits for all nodes to reach Running state.

## Tests (1)

### test_successful_genesis_ceremony

Post-startup verification of ceremony results:

1. **Genesis hash agreement**: Fetch block #0 from all nodes via gRPC. All nodes must report the same genesis block hash.
2. **Genesis block structure**: Full genesis block has no parents (empty `parentsHashList`).
3. **shardId matches config**: Genesis block's `shardId` matches `node_conf.shard_id` (parsed from `defaults.conf` + `conf/rust.conf`).
4. **Bonds match shard config**: Genesis block's bonds match `shared_shard.config.bonds` — correct validator count and per-validator stakes.

## Setup

- **Topology**: Session-scoped `shared_shard` (3 validators + readonly)
- **FTT**: From `conf/rust.conf`
- **Heartbeat**: Enabled

## What it proves

- The genesis ceremony protocol completes successfully with 3 validators
- All nodes (validators + readonly observer) agree on the same genesis block hash
- The genesis block has the correct structure (block #0, no parents)
- The genesis block's shardId matches the node configuration
- The genesis block's bonds match the shard config (validator identities and stakes)
- The readonly observer receives the correct genesis block without participating in signing

## Key assertions

- `len(unique_hashes) == 1` -- all nodes share the same genesis hash
- `len(parentsHashList) == 0` -- genesis block has no parents
- `genesis_info.shardId == node_conf.shard_id` -- shardId matches config
- `len(actual_bonds) == len(expected_bonds)` -- correct bond count
- `actual_bonds[pubkey] == expected_stake` -- per-validator stake matches

## Infrastructure used

- Session-scoped `shared_shard` fixture (3 validators + readonly)
- `node_conf` fixture for shard_id (parsed from config via pyhocon)
- `shared_shard.config.bonds` for expected validator identities and stakes
- `Node.get_blocks()`, `Node.get_block()` for genesis block retrieval

## Related

- [test_bonding_validators](test_bonding_validators.md) -- dynamic validator addition after genesis

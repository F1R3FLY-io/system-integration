# Slashing Mechanism — Current State

The slashing mechanism in the current Rust node is a direct 1:1 port from the original Scala implementation. No new design work or modifications were made — it inherits the exact same semantics.

## How It Works

1. A receiving validator detects an invalid block and records it in the DAG
2. The next block proposer who observes the invalid block automatically includes a `SlashDeploy` system deploy
3. The PoS Rholang contract executes the slash

A single invalid block triggers immediate, full slashing:
- Bond set to 0 — entire stake forfeited
- Funds confiscated — transferred from PoS vault to the Coop multi-sig vault
- Validator removed from active set — can no longer propose blocks
- Pending rewards deleted
- No quarantine period — unlike normal unbonding, removal is immediate

This is not configurable. There is no threshold parameter — one invalid block = full forfeiture.

## What Triggers Slashing (17 Offense Types)

| Offense                   | Description                                  |
| ------------------------- | -------------------------------------------- |
| InvalidBlockHash          | Tampered block hash                          |
| InvalidBlockNumber        | Wrong block number progression               |
| InvalidSequenceNumber     | Wrong sequence number                        |
| InvalidParents            | Parent selection violates GHOST              |
| InvalidFollows            | Justifications don't match bonded validators |
| InvalidShardId            | Wrong shard identifier                       |
| InvalidRepeatDeploy       | Repeating a deploy signature                 |
| DeployNotSigned           | Including unsigned deploys                   |
| JustificationRegression   | Regressions in justifications                |
| NeglectedInvalidBlock     | Failing to slash a known invalid block       |
| NeglectedEquivocation     | Failing to slash a known equivocation        |
| InvalidTransaction        | Invalid on-chain transactions                |
| InvalidBondsCache         | Incorrect bonds cache                        |
| ContainsExpiredDeploy     | Expired deploys in block                     |
| ContainsTimeExpiredDeploy | Time-expired deploys                         |
| ContainsFutureDeploy      | Future-dated deploys                         |
| AdmissibleEquivocation    | Conflicting blocks pulled in as dependency   |

## Two-Level Enforcement

- **Level 1 — Direct offenders**: Validators who create invalid blocks are slashed immediately
- **Level 2 — Collusion prevention**: Validators who fail to slash a known equivocator are also slashed (`NeglectedEquivocation`, `NeglectedInvalidBlock`)

## Current State of Testing

The existing slash integration tests (`test_slash.py`) were written for the Scala node's test infrastructure and have not been ported to the current Rust test infrastructure yet. Porting these tests is on our roadmap.

## Documentation

There is no standalone slashing documentation beyond the source code and this file. Formal documentation will be created soon.

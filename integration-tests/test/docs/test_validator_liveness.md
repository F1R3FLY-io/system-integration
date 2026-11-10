# test_validator_liveness

## Purpose

How the leaky bucket ejects a validator that stops proposing, and spares the validators that keep producing, join or leave, on one live shard. Each validator keeps a bucket that every block drains and that the validator's own blocks refill. A validator whose bucket stays below the ejection threshold for long enough is ejected by a vote of the others. If it does not rejoin before its grace ends, it is slashed.

The test reads each step from the honest validators' liveness reports, `GET /api/validator-liveness` on the admin port (`Node.validator_liveness()`). The report holds two kinds of data:

- `localView`: the node's own buckets and its next decision. These depend on which blocks the node accepted, so nodes can disagree.
- `finalizedState`: the claims, ejections and stake that the node's last finalized block records. Nodes that hold the same finalized block report the same values.

The test does not trust the reports alone. At each outcome it reads the PoS contract on the readonly observer at the same finalized block (`PosAPI` getters), and requires the bonds, the active set and the ejected set to agree with the report.

The shard runs under `LIVENESS_PROFILE_CLI_OPTIONS` (`infra/config.py`):

| Parameter | Value | Effect |
|-----------|-------|--------|
| `--leaky-bucket-lambda-base-ppm` | 5000 | A silent validator drains below the threshold in 20 blocks |
| `--leaky-bucket-theta-eject-ppm` | 900000 | The threshold is 90% of capacity |
| `--leaky-bucket-eject-observation-blocks` | 10 | Ejection opens 10 blocks after the drain |
| `--leaky-bucket-reward-rate-ppm` | 50000 | A validator that produces its share of blocks stays above the threshold |
| `--epoch-length` | 20 | A claim must resolve within 20 blocks, and grace lasts 40 |
| `--quarantine-length` | 135 | The shortest quarantine the node accepts for this epoch |

Runs under the subprocess provider.

## The shard

Five genesis validators hold stakes 500, 400, 300, 200 and 100, with heartbeat on and a readonly observer. Validator 6 attaches later as a joiner. The genesis funds the vaults of validators 4, 5 and 6 and the two load vaults.

| Validator | Stake | Role |
|-----------|-------|------|
| v1 | 500 | Producer and reporter. Submits the joiner's bond and A's rejoins |
| v2 | 400 | Producer and reporter |
| v3 | 300 | The leaver |
| v4 | 200 | A: silenced, ejected, refused once, then reinstated |
| v5 | 100 | B: reporter until it is silenced, then ejected and slashed |
| v6 | 100 | The joiner |

Background deploy load runs for the whole test (`infra/background_load.py`): a vault transfer every 2 s, round-robin across v1 and v2.

## The test (1)

### `test_ejection_rejoin_and_slashing_while_validators_join_and_leave`

On every poll, no reporter may see a producing validator drained. No reporter may offer, claim or eject a protected validator. The producing and protected sets change as the phases run.

1. `held`: the shard runs 30 heights at mixed stakes. Every producer's bucket must be reported.
2. `joiner_synced`, `epoch_end`, `epoch_start`: v6 attaches as a joiner, and the test waits until its tip is within 2 heights of the reporters' tips. A validator bonded before its node catches up cannot propose, and its bucket drains under this profile. A is paused 17 or 18 heights into an epoch. When the tip is 1 to 4 heights into the next epoch, v1 submits v6's bond of 100 and v3 submits its withdrawal. Both changes take effect at the boundary after that, so the ejection claim against A opens while v3 is still seated.
3. `a_drained`, `a_claim_open`, `a_ejected`: A drains and is ejected. The claim's electorate holds the leaver, v3. The honest nodes agree on one grace end, and the contract's ejection record holds the same grace end and the leaver's stake. Then A is unpaused, so it catches up while it is ejected.
4. `joined`, `left`: v6 holds stake 100 and is active. v3 holds no bonded stake and is in withdrawal quarantine. The contract agrees.
5. `a_rejoin_refused`: A asks to rejoin with a topup of 1, below its shortfall. The rejoin fails, A stays ejected, and its freeze stays open. A's vault loses only the deploy's phlo.
6. `a_caught_up`, `a_reinstated`: A's tip is within 2 heights of the reporters' tips. Then B is paused, and A asks to rejoin with a topup of 200. A is reinstated. Its freeze closes with the topup, and its bond stays 200. A's vault loses the phlo of both deploys and the topup, and nothing more. From this step, no reporter may offer, claim or eject A.
7. `b_ejected`, `b_slashed`: B is ejected, and its grace ends. B holds no stake, and the coop vault grows by exactly B's stake of 100.
8. `a_holds`: one epoch after B's slash, no reporter sees A drained.

At the end, every background transfer must reach the same verdict on every live node. The destination vault must equal its start balance plus one per finalized transfer (`assert_vault_transfers_reconciled`). Every live node must hold the same bonds map at a common finalized block: v1 500, v2 400, A 200 and v6 100.

Step 6 depends on the node's tally rule for a member that left. A's rejoin electorate is the electorate that ejected A, so it holds v3. By then v3 has left without voting on the rejoin, and B is silent. A member that holds no seat and has not voted does not count, so the yeas of v1 and v2 pass the rejoin. Without that rule, v3's stake holds the yeas below two thirds, and A is slashed although it paid.

The test logs the time and tip height of each step, and each producer's lowest bucket level as a multiple of its threshold.

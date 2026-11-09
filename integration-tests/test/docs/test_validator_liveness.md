# test_validator_liveness

## Purpose

How the leaky bucket ejects a validator that stops proposing, on a live shard. Each validator keeps a bucket that every block drains and that the validator's own blocks refill. A validator whose bucket stays below the ejection threshold for long enough is ejected by a vote of the others. If it does not rejoin before its grace ends, it is slashed.

The suite reads each step from the honest validators' liveness reports, `GET /api/validator-liveness` on the admin port (`Node.validator_liveness()`). The report holds two kinds of data:

- `localView`: the node's own buckets and its next decision. These depend on which blocks the node accepted, so nodes can disagree.
- `finalizedState`: the claims, ejections and stake that the node's last finalized block records. Nodes that hold the same finalized block report the same values.

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

## Tests (1)

### `test_a_silent_validator_is_ejected_and_slashed_when_its_grace_ends`

Four validators of stake 100 with heartbeat on. The test pauses v4 and then waits for each step on v1, v2 and v3:

1. `drained`: every honest node sees v4's bucket below the threshold.
2. `offered`: an honest node offers v4 for ejection in its next block.
3. `claim_open`: a claim to eject v4 is open.
4. `ejected`: every honest node's finalized state shows v4 ejected, with one grace end.
5. `slashed`: every honest node's finalized state shows v4's stake at 0.

On every poll, no honest node may see a producing validator drained.

The test logs the time and tip height of each step. One run measured these values:

| Step | Seconds | Tip height |
|------|---------|------------|
| `drained` | 46 | 9 |
| `offered` | 67 | 12 |
| `claim_open` | 77 | 14 |
| `ejected` | 105 | 24 |
| `slashed` | 344 | 62 |

Each height holds several blocks, one from each producer, and the drain counts blocks. The grace takes most of the run.

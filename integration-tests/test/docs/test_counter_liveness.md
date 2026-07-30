# test_counter_liveness

## Purpose

This manual soak test checks node stability and finalized Rholang state over
1,000 strictly sequential counter updates. It is intentionally not a throughput
test: every increment must finalize everywhere and be observable through the
readonly node before the next increment is submitted.

## Topology

- Bootstrap
- Three equal-stake validators with heartbeat enabled
- One readonly observer
- One dedicated genesis-funded deployer

The test creates its own isolated shard and always requests cleanup. Use
`--keep-on-failure` to preserve a failing shard for inspection.

## Sequence

1. Deploy `resources/counter/counter.rho`.
2. Wait for the setup deploy to finalize on every node.
3. Extract the fresh `rho:id:` URI and verify the initial value is `0`.
4. Repeat 1,000 times:
   - submit one `inc` deploy to validator1;
   - wait for inclusion and canonical finalization on every node;
   - run an exploratory `get` query through readonly, pinned to readonly's
     canonical deploy block;
   - require the result to equal the current round number.
5. Confirm all nodes remain running and the final value is exactly `1000`.

The test logs every round and emits node/LFB checkpoints every 100 rounds.
Latency is reported as telemetry only and does not have a performance gate.

## Running

The full test is outside normal pytest discovery:

```bash
F1R3FLY_NODE_BINARY=/absolute/path/to/f1r3node-rust/target/release/node \
F1R3FLY_NODE_DEFAULTS_CONF=/absolute/path/to/f1r3node-rust/node/src/main/resources/defaults.conf \
poetry run pytest integration-tests/test/soak/test_counter_liveness.py \
  --provider=subprocess -v -s --keep-on-failure
```

Use a reduced count only to validate the harness:

```bash
F1R3FLY_COUNTER_ITERATIONS=3 \
poetry run pytest integration-tests/test/soak/test_counter_liveness.py \
  --provider=docker -v -s
```

A run with fewer than 1,000 iterations is not an acceptance result.

## Failure Signals

The test fails at the exact round when any of these occurs:

- inclusion exceeds 120 seconds;
- canonical finalization exceeds 600 seconds on any node;
- exploratory query returns no value, multiple values, a non-integer, or a
  stale/skipped/duplicated counter value;
- any node exits;
- the integration framework's post-test log scanner finds a prohibited fatal
  node error.

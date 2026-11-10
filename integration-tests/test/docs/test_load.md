# test_load

## Purpose

Loads a shard through deploy queues at its sustained rate and above it. The shard must keep working, and every deploy a queue accepts must get an outcome. Latency, refusals and expiries are measured and reported, not judged.

## Setup

- **Topology**: 4 validators (100/100/100/100 bonds), boot and readonly: 6 nodes. `ShardConfig(deploy_queue=True)` puts a deploy queue in front of each validator. The queue serves the validator's client ports, so the test reaches the validator through it.
- **Senders**: 16 generated keys funded at genesis. Each sender submits to one validator.
- **Workload**: the mix in `DEFAULT_MIX` (`infra/workloads.py`): light, compute, data and heavy terms, transfers, contract installs and calls, contention on shared cells, and nonce sequences. Each part checks the state its finalized deploys produced.
- **Node conf**: `conf/rust.conf`; the test sets no node flags of its own.

## Steps

| Step | Rate | Duration |
|------|------|----------|
| baseline | 1/s | 60 s |
| sustained | `SUSTAINED_RATE` | 240 s |
| overload | 2 × `SUSTAINED_RATE` | 60 s |

The baseline's finality pace is the reference for the loaded steps. In the overload step the queues refuse what the shard cannot settle in time. A refused deploy is signed again and resubmitted after its retry-after, as a client that honors a refusal does, and is abandoned after `REFUSAL_PATIENCE_SECONDS`. After the steps, load stops and the shard drains until finality passes every window the run signed.

`infra/load_run.py` runs the steps; `test_capacity` uses the same engine for its ramp.

## Failures

- A submission fails other than by a queue's refusal.
- A node logs a finalizer run that exceeded its timeout.
- A loaded step finalizes heights at less than half the baseline's pace.
- The LFB spread across nodes exceeds 5 while a step runs.
- Finality does not advance for 60 s while a step runs.
- A node takes longer than 2 s to answer its status.
- An accepted deploy has no outcome after the drain, the nodes do not converge, or the finalized deploys left inconsistent state.
- A node is not running at the end.

## Measurement

`LifecycleTracker` (`infra/metrics.py`) polls every node for every deploy. A deploy counts as finalized only when every node reports it Finalized; the first node's time is kept too. A background sampler reads every node's LFB, the tip, the block-ingest backlog and the queues' held deploys every 10 s. Each step scrapes `/metrics` on every validator before and after.

## Output

Per step, the `PHASE_LINE_FORMAT` line (parsed by f1r3node-rust `summarize-issue24-phases.sh`), a first-node line, `Unfinalized` lines for deploys that did not finalize, and `Node internals` per validator. Then the `LOAD TEST RESULTS` table, the run table (refused, resubmitted, client p95, expired, abandoned, not-in share, queue depth, spread, merge rejections), and `All-node LFBs at drain: … (spread N blocks)`. The soak reads the results table, the phase lines and the drain line.

## Settings

| Variable | Effect |
|---|---|
| `LOAD_TEST_SUSTAINED_RATE` | The sustained step's rate; the overload step is twice it |
| `LOAD_TEST_SUSTAINED_DURATION` | The sustained step's length in seconds |
| `LOAD_TEST_DEPLOY_MIX` | Mix weights, in `DEFAULT_MIX`'s form |
| `LOAD_TEST_NODE_CPUS` | `readonly:2` starts named nodes on fewer CPUs |
| `LOAD_TEST_OUT` | JSON lines: each step, the summary, and every deploy beside it |

With `--provider=remote --inventory <file> --skip-setup --session-id <shard>` the test adopts an existing shard, signs with the inventory's deployer keys, and skips the node log check.

## Related

- [test_heartbeat](test_heartbeat.md)
- [test_bridge_admin](test_bridge_admin.md): sustained load with complex contract interactions

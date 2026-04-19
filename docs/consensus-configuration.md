# Consensus Configuration Guide

How configuration values affect consensus behavior, finalization, and shard resilience.

## Multi-Parent DAG vs Linear Chain

Most blockchains (Bitcoin, Ethereum) produce a **linear chain** — each block has exactly 1 parent. Forks are resolved by choosing the longest/heaviest chain and discarding the other.

This node uses a **multi-parent DAG** — each block can have multiple parents, one per validator. Instead of choosing between forks, validators **merge** them by creating a block that references all known tips as parents. There is no fork-choice in the traditional sense — there's fork-merge.

This is why the system has:
- `max-number-of-parents = 100` — a single block can reference many parents
- A merger/LCA (Lowest Common Ancestor) calculation during block creation
- Justifications that track each validator's latest block

## Fault Tolerance Threshold

`fault-tolerance-threshold` is the fraction of total stake you tolerate being malicious or equivocating. It is a **safety margin**, not a voting percentage.

The finalization formula (from `casper/src/rust/finality/finalizer.rs`):

```
normalized_fault_tolerance = (agreeing_weight * 2 - total_weight) / total_weight
```

A block is finalized when `normalized_fault_tolerance > fault-tolerance-threshold`. The value ranges from -1.0 (no agreement) to 1.0 (unanimous).

From the original design documentation (SafetyOracle.scala):
> "The fault tolerance threshold is a subjective value that the user sets to 'secretly' state that they tolerate up to fault_tolerance_threshold fraction of the total weight to equivocate."
>
> "In the extreme case when your normalized fault tolerance threshold is 1, all validators must be part of the clique that supports the candidate in order to state that it is finalized."

### What each value means

| FTT | Equivocation tolerance | Requires to finalize | Use case |
|-----|----------------------|---------------------|----------|
| -1.0 | 100% | Any single validator | Completely unsafe |
| 0.0 | 0% | >50% of weight | Standalone/dev, trusted validator set |
| 0.1 | 10% | ~60% of weight | Trusted testnet |
| 0.33 | 33% (BFT classic) | >2/3 of weight | Standard BFT — tolerates <1/3 byzantine |
| 0.67 | 67% | >5/6 of weight | Very conservative (both Scala and Rust default) |
| 0.99 | 99% | Near-unanimity | Effectively broken for small validator sets |

### Effect on validator count (equal stake)

The normalized FT value for a given agreement ratio:

| Agreeing / Total | FT value | Finalized at FTT=0.0? | FTT=0.1? | FTT=0.33? | FTT=0.67? |
|-----------------|----------|----------------------|----------|-----------|-----------|
| 2/3 (67%) | 0.3333... (1/3) | Yes | Yes | Yes (0.333 > 0.33) | No |
| 3/4 (75%) | 0.50 | Yes | Yes | Yes | No |
| 5/7 (71%) | 0.4286... (3/7) | Yes | Yes | Yes | No |
| 5/6 (83%) | 0.6667... (2/3) | Yes | Yes | Yes | No (0.667 < 0.67) |
| 6/7 (86%) | 0.7143... (5/7) | Yes | Yes | Yes | Yes |
| 7/10 (70%) | 0.40 | Yes | Yes | Yes | No |
| 9/10 (90%) | 0.80 | Yes | Yes | Yes | Yes |

Key observations:
- **FTT=0.67** (default) with 3 equal-stake validators requires **all 3** to agree for finalization. Losing 1 validator halts finalization.
- **FTT=0.33** with 3 validators allows 2/3 to finalize (FT for 2/3 = 1/3 ≈ 0.3333, which IS > 0.33)
- **FTT=0.1** with 3 validators allows 2/3 to finalize (FT 0.33 > 0.1)
- **FTT=0.67** with 7+ validators starts being practical (can lose 1 of 7)

### Choosing FTT for production

The right value depends on your trust model and validator set size:

**Trusted validator set (known operators, small set):** FTT = 0.0 to 0.1
- You trust validators not to equivocate
- Finalization needs >50-60% agreement
- Can tolerate validator failures/expulsions in a 3-validator set
- Appropriate for: private networks, development, testnets

**Semi-trusted set (BFT standard):** FTT ≈ 0.3
- Classic BFT tolerance — up to 1/3 byzantine
- Requires >2/3 weight to finalize
- With 3 validators: all 3 must agree (FT for 2/3 = 0.33 is NOT > 0.3... barely passes at 0.29)
- With 7+ validators: can lose up to 2
- Appropriate for: consortium networks, public testnets

**Paranoid (default):** FTT = 0.67
- Very high equivocation tolerance
- Requires >5/6 weight to finalize
- Only practical with 7+ validators
- With 3 validators: all 3 must agree — any single failure halts finalization
- Appropriate for: large public networks with many validators

### What happens when a validator is expelled

If a validator's blocks are rejected by the majority (e.g., due to a replay mismatch), it's effectively expelled from consensus. With 3 equal-stake validators (V1 expelled):

- V2+V3 agreeing weight = 2000, total = 3000
- Normalized FT = (2000*2 - 3000) / 3000 = **0.33**
- Finalized at FTT=0.0? **Yes** (0.33 > 0.0)
- Finalized at FTT=0.1? **Yes** (0.33 > 0.1)
- Finalized at FTT=0.33? **No** (0.33 is NOT > 0.33, strict greater-than)
- Finalized at FTT=0.67? **No**

The expelled validator has no mechanism to detect it's on a minority fork and rejoin (see f1r3node#459).

## Synchrony Constraint Threshold

`synchrony-constraint-threshold` controls when a validator is allowed to propose a new block.

| Value | Meaning | Effect |
|-------|---------|--------|
| `0.67` | Must see 2/3 of validators active before proposing | Prevents premature proposals during partitions, but can deadlock |
| `0` | Always allowed to propose | Maximizes liveness, forks are merged by the DAG |

### Why 0 is correct for a multi-parent DAG

In a linear chain, the synchrony constraint prevents validators from proposing when they haven't seen recent blocks from peers. This avoids creating competing forks that waste work.

In a multi-parent DAG, forks are **expected and desirable** — they get merged. The synchrony constraint was designed for linear chains where forks are wasteful. In a DAG, "forks" are just parallel progress that gets merged into the next block.

With `synchrony-constraint-threshold = 0.67`:
- A validator must see 2/3 of other validators' recent blocks before proposing
- If 2 validators are waiting on each other, neither can propose → deadlock (see f1r3node#437)
- The deadlock is specific to multi-parent DAGs because there's no "longest chain" tiebreaker

With `synchrony-constraint-threshold = 0`:
- Validators always propose, creating independent blocks freely
- Forks are expected — the DAG merges them via multi-parent blocks
- No deadlock possible from synchrony
- Trade-off: more empty/recovery blocks and the DAG grows wider

## Configuration Interactions

The combination of these values determines shard behavior:

| FTT | Sync | Behavior |
|-----|------|----------|
| 0.67 + 0 | Easy to fork, hard to finalize | Requires near-unanimity. Only works with 7+ validators. |
| 0.67 + 0.67 | Hard to fork, hard to finalize | Deadlock-prone (#437). Only works with 7+ validators. |
| 0.1 + 0 | Easy to fork, easy to finalize | Tolerates validator failures in small sets. **Recommended for 3-validator dev/test.** |
| 0.3 + 0 | Easy to fork, BFT finalization | Classic BFT. Needs 4+ validators to tolerate 1 failure. |

## Recommended Values

**3-validator dev/test shard:**
```
fault-tolerance-threshold = 0.1
synchrony-constraint-threshold = 0
```
Allows 2/3 finalization. Tolerates 1 expelled/offline validator.

**7+ validator production shard:**
```
fault-tolerance-threshold = 0.67
synchrony-constraint-threshold = 0
```
Default values. Tolerates 1 failure at 7 validators, 2 at 10+.

**Integration tests (3 validators, must test recovery):**
```
fault-tolerance-threshold = 0.1
synchrony-constraint-threshold = 0
```
Same as dev — tests need 2/3 finalization to verify shard recovery from validator expulsion.

## Related

- `conf/rust.conf` / `conf/scala.conf` — where these values are set for docker shard
- `node/src/main/resources/defaults.conf` — hardcoded defaults (FTT=0.67 for both Scala and Rust)
- `casper/src/rust/finality/finalizer.rs` — finalization algorithm and FT formula
- `casper/src/rust/safety/clique_oracle.rs` — weight map calculation
- f1r3node#437 — network deadlock at `synchrony-constraint-threshold = 0.67`
- f1r3node#459 — fork abandonment mechanism (expelled validators can't rejoin)

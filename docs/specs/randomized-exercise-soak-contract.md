---
doc_type: spec
status: draft
version: "0.2"
last_modified_by: pi-session-019fbb2a
last_modified_at: 2026-08-01T17:25:43Z
implements_tasks: [TODO-011-001, TODO-011-002, TODO-011-003, TODO-011-004, TODO-011-005]
consumed_by_tasks: [TODO-012-001, TODO-012-002, TODO-012-003, TODO-012-004, TODO-012-005, TODO-012-006, TODO-012-007, TODO-012-008, TODO-012-009, TODO-012-010]
breaking_changes: false
---

# Randomized Exercise Soak Contract

## Canonical cross-repository linkage

This document is canonical for the system-integration catalog, executor, result, and replay interface. [`F1R3FLY-io/f1r3node-rust/docs/ci-pins.md`](https://github.com/F1R3FLY-io/f1r3node-rust/blob/dev/docs/ci-pins.md) is canonical for split runner/catalog pins, JSONC resolution, trigger trust, and pin-bump automation. The [orchestrator value stream](https://github.com/F1R3FLY-io/f1r3node-rust/blob/dev/docs/randomized-exercise-soak.md) is canonical for scheduling and failure policy.

These repository URLs are reciprocal canonical links; local relative workspace paths are conveniences rather than authoritative identifiers.

## Purpose

This document is the shared compatibility contract between the executable soak catalogue in `system-integration` and the scheduler/orchestrator in `f1r3node-rust`. The first delivery covers valid transaction workloads on the existing six-node single shard with Docker and subprocess providers.

Validator lifecycle, fault injection, invalid-input fuzzing, network disruption, and multi-shard workloads are deferred until transaction-only epochs reset and replay reliably.

## Ownership Boundary

| Concern | Owning repository |
| --- | --- |
| Epoch schema and compatibility contract | Shared, with mirrored contract tests |
| Executable catalogue, generators, invariants, and shard reset | `F1R3FLY-io/system-integration` |
| Seeded selection, coverage policy, segment deadlines, and workflow integration | `F1R3FLY-io/f1r3node-rust` |
| Node image and subprocess binary under test | `F1R3FLY-io/f1r3node-rust` |
| Per-epoch execution result | Produced here and aggregated by `f1r3node-rust` |
| Run artifacts, dashboard, and release verdict | `F1R3FLY-io/f1r3node-rust` |

The orchestrator's `.github/ci-pins.jsonc` defines separate immutable boundaries. `systemIntegration.runnerRef` selects privileged launcher/cloud-init code; `systemIntegration.catalogRef` selects this executor and catalog. Compatibility validation must finish before an OCI runner or shard is launched.

A backward-compatible experimental epoch normally requires only a one-line `catalogRef` bump in f1r3node-rust after this branch merges. Runner changes require a separate `runnerRef` review. Incompatible schema or scheduler-capability changes require coordinated branches. Required and release-gating promotion remains an explicit policy decision in f1r3node-rust and is never inherited silently from catalog metadata.

## Pin publication contract

Catalog changes must expose enough machine-readable evidence for a reviewed pin bump:

- merged 40-character catalog SHA;
- catalog schema version;
- added or revised epoch IDs and semantic revisions;
- definition digests;
- compatibility-test command and result; and
- whether scheduler capabilities or local gating policy must change.

Future automation may propose a f1r3node-rust PR changing only `systemIntegration.catalogRef`, but it cannot merge, change `runnerRef`, or promote gating policy. Initial pin bumps remain manual while the interface accumulates soak evidence.

## Version Identity

Every catalogue definition and result must carry:

```yaml
catalog_schema_version: 1
epoch_id: SOAK-EPOCH-NNN
epoch_revision: 1
definition_repository: F1R3FLY-io/system-integration
definition_sha: <40-character commit SHA>
definition_digest: <sha256 of normalized definition and fixtures>
orchestrator_repository: F1R3FLY-io/f1r3node-rust
orchestrator_sha: <40-character commit SHA>
seed: <unsigned integer>
provider: docker | subprocess
topology: six-node-single-shard
effective_limits: <orchestrator-supplied host and workload limits>
```

An `epoch_id` is permanent and is never reused. `epoch_revision` changes only when setup, generated operations, invariants, limits, or expected outcomes change semantically. Editorial changes do not increment the revision. `catalog_schema_version` changes only for incompatible catalogue or manifest changes.

Replay must use the recorded definition SHA, digest, seed, provider, topology, and effective limits. Missing or incompatible historical inputs fail closed rather than silently substituting newer behavior.

## Initial Catalogue

| ID | Workload | Operational shape | Initial policy |
| --- | --- | --- | --- |
| `SOAK-EPOCH-001` | Steady valid deploy stream | Sustained bounded rate with finalization drain | Required, gating |
| `SOAK-EPOCH-002` | Burst and cooldown | Valid bursts followed by quiescent convergence checks | Required, experimental |
| `SOAK-EPOCH-003` | Concurrent channel contention | Concurrent valid contracts competing over shared channels or state | Required, experimental |
| `SOAK-EPOCH-004` | Large valid deploys | Deploys near approved phlo and payload bounds | Required, experimental |
| `SOAK-EPOCH-005` | Dependent transaction chains | Each step waits for finalized prerequisite state | Required, experimental |
| `SOAK-EPOCH-006` | Mixed contract workload | Weighted interleave of independent valid contract families | Required, experimental |

## Valid-Operation Requirements

Every generator must prove or enforce that:

- deploy signatures, keys, balances, phlo limits, and payload structure are valid;
- dependencies, nonces, routing, and state transitions are legal;
- dependent operations wait for prerequisite finalization;
- offered load stays within declared rate and concurrency bounds;
- success is evaluated through finalized state, not submission acceptance alone;
- safety and convergence invariants run after the active phase; and
- epoch limits may tighten but never increase orchestrator-supplied host protections.

## Executor Interface Requirements

The stable executor entry point may be a CLI or pytest entry point, but it must accept:

- epoch ID and revision;
- deterministic seed;
- provider and topology;
- segment or epoch deadline;
- output directory; and
- effective workload and host-protection limits.

The exact command path and argument names remain to be frozen by `TODO-011-001`. The entry point must execute one bounded epoch, leave a structured manifest in the requested output directory, and return a machine-readable result classification.

## Result and Evidence Requirements

Each result must include:

- the complete version identity tuple;
- planned and actual start and finish times;
- effective topology, provider, image, and safety limits;
- submitted, accepted, rejected, included, and finalized counts;
- expected and observed finalized-state invariants;
- finalization latency, throughput, RSS, CPU, and convergence metrics;
- failure classification and first failing operation;
- evidence paths and checksums;
- shard-reset outcome; and
- a replay command or manifest reference.

Failure classes must distinguish workload/assertion failure, safety breach, host breach, reset failure, and infrastructure failure. Workload or finalization failures preserve evidence, reset, and permit orchestration to continue. Safety, host, or reset failures preserve evidence and stop the segment.

## Compatibility and Replay

Contract tests must be consumable by `f1r3node-rust` before OCI launch and must reject:

- unknown catalogue schemas;
- unknown epoch revisions;
- definition digest mismatches;
- missing executor capabilities;
- attempts to raise effective safety limits; and
- replay manifests incompatible with the pinned implementation.

## Open Interface Questions

- [ ] What command path and argument names form the stable executor entry point?
- [ ] What canonical serialization is hashed for `definition_digest`?
- [ ] Which deadline representation is shared across shell, Python, and workflow callers?
- [ ] Which result-schema fields are mandatory for version 1 versus optional provider extensions?
- [ ] How does the executor expose a pre-launch capabilities/compatibility probe without starting a shard?

## Change Log

- v0.2 (2026-08-01): Cross-linked the canonical pin specification, split privileged runner and catalog refs, and defined catalog pin-bump evidence and automation limits.
- v0.1 (2026-08-01): Recorded the initial cross-repository ownership, identity, catalogue, executor, result, safety, and replay contract.

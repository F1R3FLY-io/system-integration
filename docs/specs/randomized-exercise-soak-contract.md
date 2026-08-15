---
doc_type: spec
status: accepted
version: "1.0"
last_modified_by: pi-session-019fa4ad
last_modified_at: 2026-08-01T18:38:23Z
implements_tasks: [TODO-011-001]
consumed_by_tasks: [TODO-011-002, TODO-011-003, TODO-011-004, TODO-011-005, TODO-011-006, TODO-012-001, TODO-012-002, TODO-012-003, TODO-012-004, TODO-012-005, TODO-012-006, TODO-012-007, TODO-012-008, TODO-012-009, TODO-012-010]
breaking_changes: false
---

# Randomized Exercise Soak Contract

## Canonical cross-repository linkage

This document is canonical for the system-integration catalog, executor, result, and replay interface. [`F1R3FLY-io/f1r3node-rust/docs/ci-pins.md`](https://github.com/F1R3FLY-io/f1r3node-rust/blob/6d1120ce8fb179dee3a80517254f9fbcd1485a70/docs/ci-pins.md) is canonical for split runner/catalog pins, JSONC resolution, trigger trust, and pin-bump automation. The [orchestrator value stream](https://github.com/F1R3FLY-io/f1r3node-rust/blob/6d1120ce8fb179dee3a80517254f9fbcd1485a70/docs/randomized-exercise-soak.md) is canonical for scheduling and failure policy.

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
seed: <unsigned I-JSON safe integer, 0 through 2^53-1>
provider: docker | subprocess
topology: six-node-single-shard
effective_limits: <orchestrator-supplied host and workload limits>
```

An `epoch_id` is permanent and is never reused. `epoch_revision` changes only when setup, generated operations, invariants, limits, or expected outcomes change semantically. Editorial changes do not increment the revision. `catalog_schema_version` changes only for incompatible catalogue or manifest changes.

Replay must use the recorded definition SHA, digest, seed, provider, topology, and effective limits. Missing or incompatible historical inputs fail closed rather than silently substituting newer behavior.

### Canonical definition digest

`definition_digest` is lowercase SHA-256 over the UTF-8 bytes of a canonical JSON payload. Version 1 definitions use only JSON strings, I-JSON safe integers from `-(2^53-1)` through `2^53-1`, booleans, nulls, arrays, and objects. Object keys are restricted to ASCII so RFC 8785 UTF-16 key ordering and Python code-point ordering are identical. Floating-point or larger integers, duplicate keys, byte-order marks, absolute fixture paths, and non-UTF-8 fixture names are rejected.

The digest payload is:

```json
{
  "catalog_schema_version": 1,
  "epoch_id": "SOAK-EPOCH-NNN",
  "epoch_revision": 1,
  "definition": {},
  "fixtures": [
    {"path": "relative/posix/path", "sha256": "<lowercase sha256>"}
  ]
}
```

The `definition` value contains every schema-defined semantic field except `annotations`; source formatting, comments, `definition_sha`, and any previously calculated digest are excluded. Fixture entries are sorted by Unicode code-point order of their normalized repository-relative POSIX paths. Each fixture checksum is calculated over the fixture's exact bytes. The complete payload is serialized using RFC 8785 JSON Canonicalization Scheme with no BOM or trailing newline, then hashed. Because version 1 rejects floating point values, conforming implementations may use compact UTF-8 JSON with recursively sorted keys (`ensure_ascii=false`, separators `,` and `:`) and obtain the same bytes. Compatibility fixtures must publish both these bytes and the expected digest.

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

## Stable Executor Interface

The version 1 command family is a `shardctl` subcommand group. Commands write one JSON document to stdout, reserve stderr for diagnostics, and do not emit secrets or private keys.

```text
poetry run shardctl soak capabilities
poetry run shardctl soak validate --catalog PATH --expected-schema 1 [--previous-catalog PATH] [--epoch ID --revision N --definition-digest SHA256]
poetry run shardctl soak run --catalog PATH --epoch ID --revision N --definition-sha GIT_SHA --definition-digest SHA256 --orchestrator-sha GIT_SHA --seed SAFE_UINT53 --provider docker|subprocess --topology six-node-single-shard --deadline-epoch UNIX_SECONDS --output-dir PATH --limits-file PATH
poetry run shardctl soak replay --manifest PATH --deadline-epoch UNIX_SECONDS --output-dir PATH
```

`capabilities` and `validate` are pure pre-launch probes: they must not invoke Docker, start a subprocess node, reserve ports, create OCI resources, or mutate shard state. `run` executes exactly one epoch. `replay` accepts no identity override other than a new deadline and output directory; it obtains all reproducibility inputs from the manifest and fails if the recorded implementation, definition, fixture, provider, topology, or effective limits are unavailable.

The shared deadline is `deadline_epoch`, a positive integer Unix timestamp in UTC seconds. The executor captures a monotonic deadline at process start and never extends it after wall-clock changes. It must refuse launch when the deadline has passed or the definition's declared worst-case cleanup cannot fit. Results also record the corresponding RFC 3339 UTC instant for human inspection.

Exit codes are stable: `0` means compatible or passed; `2` means invalid input or incompatibility detected before resource launch; `10` means a workload or assertion failure with a manifest; `20` means a safety, host, or reset stop with a manifest; and `30` means an infrastructure failure with a manifest. The manifest's `failure_class`, not prose or exit-code parsing, is authoritative.

### Pre-launch capability document

`capabilities` returns at least:

```json
{
  "executor_protocol_version": 1,
  "catalog_schema_versions": [1],
  "result_schema_versions": [1],
  "providers": ["docker", "subprocess"],
  "topologies": ["six-node-single-shard"],
  "commands": ["capabilities", "validate", "run", "replay"],
  "failure_classes": ["none", "workload", "assertion", "safety", "host", "reset", "infrastructure"],
  "limit_schema_version": 1
}
```

Unknown required capabilities, schemas, providers, topologies, epoch revisions, or failure classes make compatibility validation fail with exit code `2` before resource launch. Candidate catalogue CI passes the previously pinned catalogue through `--previous-catalog`; removing a permanent epoch ID, changing a semantic digest without incrementing its revision, incrementing by more than one, regressing a revision, or incrementing a revision without a semantic change fails closed.

## Effective Safety Limits

The orchestrator supplies a version 1 limits document with all keys present:

```json
{
  "limit_schema_version": 1,
  "max_operations": 1000,
  "max_concurrency": 8,
  "max_payload_bytes": 1048576,
  "max_phlo_limit": 10000000,
  "max_submit_rate_per_second": 10,
  "max_active_phase_seconds": 600,
  "max_drain_seconds": 300,
  "max_node_rss_bytes": 4294967296,
  "min_host_available_bytes": 2147483648
}
```

All values are positive I-JSON safe integers no greater than `2^53-1`. Epoch definitions may omit an override and inherit the orchestrator value. A supplied epoch override tightens a maximum only when it is less than or equal to the orchestrator value; it tightens `min_host_available_bytes` only when it is greater than or equal to the orchestrator value. Missing orchestrator keys, unknown keys, non-integer or out-of-range values, or an attempted relaxation fail before resource launch. The result records both supplied and effective limits.

## Result and Evidence Schema

Every attempted `run` that passes pre-launch validation creates `result.json` atomically in `--output-dir`. Version 1 requires these top-level fields:

- `result_schema_version`, `executor_protocol_version`, `status`, `failure_class`, `recommended_action`, and `message`;
- the complete version identity tuple, including topology and both supplied and effective limits;
- `planned_start_at`, `started_at`, `finished_at`, `deadline_epoch`, and `deadline_at`, with timestamps in RFC 3339 UTC;
- `effective_image` and executor version;
- integer operation counts for `planned`, `submitted`, `accepted`, `rejected`, `included`, and `finalized`;
- arrays for expected and observed finalized-state invariants;
- metrics containing finalization latency, throughput, per-node RSS, host available bytes, CPU, and convergence observations; unavailable measurements are explicit nulls with a reason, never omitted;
- `first_failing_operation`, which is null on success and otherwise contains plan index, operation ID, phase, and sanitized diagnostic;
- evidence entries with repository-relative output paths, media types, byte counts, and lowercase SHA-256 checksums;
- reset fields `attempted`, `succeeded`, and sanitized diagnostic; and
- an executable replay command that references the manifest without embedding secrets.

Provider extensions are allowed only under `extensions.<provider>` and cannot replace or weaken required fields. Evidence paths must resolve beneath the output directory, must not be symlinks escaping it, and must not contain credentials, private keys, tokens, or unredacted environment dumps.

### Authoritative failure classes

| Class | Meaning | Recommended action |
| --- | --- | --- |
| `none` | All required finalized outcomes and cleanup passed | `continue` |
| `workload` | The catalog implementation could not generate or submit its declared valid operation plan | `continue_after_reset` |
| `assertion` | Valid submitted work was rejected, not included/finalized by its bound, or violated a non-safety expected outcome | `continue_after_reset` |
| `safety` | Conflicting finalized state, divergence, or another safety invariant was observed | `stop_segment` |
| `host` | An RSS or host-resource protection limit was crossed | `stop_segment` |
| `reset` | Clean shard teardown/reset could not be proved | `stop_segment` |
| `infrastructure` | Provider, runner, network, image, or external control-plane failure prevented a trustworthy workload verdict | `retry_infrastructure` |

Finalization rejection or timeout is `assertion` unless evidence proves a `safety`, `host`, or `infrastructure` cause. Classification precedence is `reset` after cleanup, then `safety`, `host`, `infrastructure`, `assertion`, and `workload`; a lower-priority original failure remains in evidence when cleanup produces a reset failure.

## Compatibility and Replay

Contract tests consumed by `f1r3node-rust` must execute `capabilities` and `validate` before OCI launch and reject:

- unknown catalogue, result, limit, or executor protocol schemas;
- unknown epoch revisions or definition digest mismatches;
- missing required executor capabilities;
- attempts to relax orchestrator-supplied limits;
- malformed or unavailable replay identity fields and historical fixtures; and
- provider or topology substitutions during replay.

## Resolved Interface Decisions

- [x] Stable commands and argument names are the `shardctl soak` family above.
- [x] Definition digests use the constrained RFC 8785 payload above with normative fixtures.
- [x] Cross-process deadlines use positive Unix UTC seconds and are enforced monotonically after process start.
- [x] Version 1 mandatory result fields and provider-extension boundaries are explicit.
- [x] `capabilities` and `validate` are resource-free, pre-launch compatibility probes.

## Change Log

- v1.0 (2026-08-01): Accepted the stable CLI/probe, canonical digest, deadline, safety-limit, result, evidence, exit-code, replay, and failure-class contracts.
- v0.2 (2026-08-01): Cross-linked the canonical pin specification, split privileged runner and catalog refs, and defined catalog pin-bump evidence and automation limits.
- v0.1 (2026-08-01): Recorded the initial cross-repository ownership, identity, catalogue, executor, result, safety, and replay contract.

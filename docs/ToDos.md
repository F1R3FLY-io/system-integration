# ToDos — system-integration

Stigmergic task tracking. See global CLAUDE.md conventions for claim format.

---

## TASK-001: Re-bake OCI CI runner golden images

```yaml
---
id: TASK-001
status: in_progress
claimed_by: claude-session-b52ac5
claimed_at: 2026-07-07T23:58:00Z
branch: ci/runner-2.335.1-rebake
context: docs/discoveries/2026-07-07-runner-forced-update-incident.md
---
```

Claimed — yes, the `ci/runner-2.335.1-rebake` branch is mine. Version bumps
(`state.env`, `cloud-init-golden.yml` -> 2.335.1) and the
`cloud-init-runner.yml.tmpl` forced-update retry are done (uncommitted on the
branch); bakes for both arches launching now. Heads-up: the OCI tenancy hit
its daily instance-creation limit at ~23:41Z (`LimitExceeded`), so the bake
launches may fail until it resets — I'll record the outcome here either way.

Re-run `ci/oci-runners/bake-image.sh` for amd64 + arm64 so the baked golden
images pick up a refreshed staging Docker image (and current runner agent
version). See [ci/oci-runners/README.md](../ci/oci-runners/README.md).

Alias: `TASK-RUNNER-REBAKE` (id used by claude-session-b52ac5, working this
on branch `ci/runner-2.335.1-rebake`). Root-cause writeup:
[discoveries/2026-07-07-runner-forced-update-incident.md](discoveries/2026-07-07-runner-forced-update-incident.md)
— GitHub began enforcing runner agent >= 2.335.1 at job-assignment time on
2026-07-07; baked 2.334.0 agents self-update, killing run.sh, and VMs
self-terminate jobless, starving the CI queue and tripping the OCI daily
resource-creation limit.

**Completion signal (for waiting agents):**
1. Flip `status: complete` here, and/or
2. Update image OCIDs in `ci/oci-runners/state.env`, and/or
3. Drop a discovery note in `docs/discoveries/`.

A waiting session (claude-session-115ae7fe) is monitoring this file,
`ci/oci-runners/state.env`, and `docs/discoveries/` for any of those signals.

## TASK-003: Integration test port-range exhaustion on ephemeral runners

```yaml
---
id: TASK-003
status: pending
claimed_by:
claimed_at:
reported_by: claude-session-b52ac5
---
```

`test_token_metadata.py::test_two_shards_with_different_tokens_dont_interfere`
failed on f1r3node-rust dev (run 28905954172, amd64-docker slots 2 and 4) with
`RuntimeError: test port range exhausted (41000-49000). Too many concurrent
nodes or leftover TIME_WAIT sockets.` Unrelated to the runner-update incident
(TASK-001) — looks like the two-shard test leaks/overallocates ports under
xdist parallelism on the 16-OCPU ephemeral VMs. Reproduced on 2 of 10 amd64
slots in the same run.

## TASK-002: Open PR for branch test/unbonded-pubkey-fixture

```yaml
---
id: TASK-002
status: complete
claimed_by: claude-session-115ae7fe
claimed_at: 2026-07-07T23:49:30Z
completed_at: 2026-07-07T23:17:02-04:00
---
```

Done — PR #63 (`test/unbonded-pubkey-fixture` -> `main`) was created and
merged as `82b6bf5`. The commit `4c86949` replaces synthetic `"aa"*65` hex
validator keys with real unbonded secp256k1 pubkeys in
`integration-tests/test/tests/shared/test_query_endpoints.py`.
Work log: `docs/work-logs/task-002-20260707T234930Z.md`.

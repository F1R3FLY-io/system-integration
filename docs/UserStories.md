---
doc_type: user_stories
version: "1.0"
last_updated: "2026-08-01"
---

# User Stories

<!--
TEMPLATE USAGE INSTRUCTIONS:
0. Update frontmatter: set last_updated to current date, increment version for structural changes
1. Add completed stories under "Completed Stories" section
2. Add planned stories under "Planned Stories" section
3. Move completed stories from "Planned" to "Completed" sections
4. Update epic links when implementation begins
5. Check acceptance criteria as features are verified
6. (Optional) Update reference URLs if using a fork with modified standards
7. Remove these usage instruction comments before committing
-->

This document captures user stories that drive feature development. User stories are reverse-engineered from completed epics and updated as new features are planned.

**Document Structure**

- Active stories: This file (`docs/UserStories.md`)
- Implementation tracking: `docs/ToDos.md` (epics and tasks)
- Completed work: `docs/CompletedTasks.md`

**Format:** Each story follows the standard template:
> As a [persona], I want [capability] so that [benefit].

**User Stories Standard Reference** (canonical):
[user-stories-standard.md](https://gitlab.com/smart-assets.io/gitlab-profile/-/blob/master/docs/common/user-stories-standard.md)

---

## Completed Stories

<!-- Add completed user stories here -->

---

## Planned Stories

Stories below are candidates for future epics. Move to "Completed Stories" when implemented.

#### US-001: Migrate to Rust-Only f1r3node

> As a **developer**, I want **the system-integration tooling to use the standalone f1r3node-rust repository as the sole node implementation** so that **the build system is simpler (no Nix/SBT), maintenance is reduced to one codebase, and the Scala/Rust duality is eliminated from shardctl and compose files**.

**Implemented in:** EPOCH-001

**Acceptance Criteria:**

- [x] `services.yml` points to `f1r3node-rust.git` (not `f1r3node.git rust/dev`) — the
      `f1r3node` repository entry and its `builds:` block are removed entirely
- [x] `shardctl up` starts Rust nodes by default without `--rust` flag
- [~] `--scala`/`--rust`/`--node-type` flags removed from shardctl CLI — `--scala`
      and `--node-type` are gone; **`--rust` is deliberately retained** as an
      accepted no-op so existing invocations keep working (see the naming
      decision below)
- [~] ~~All `compose/f1r3node-rust-*.yml` files removed; `compose/f1r3node*.yml`
      uses Rust image~~ — **decided the other way.** The `-rust` filenames are
      kept and the bare `f1r3node*.yml` files were deleted. Renaming would break
      any script or doc naming a compose file for no functional gain; the
      redundant infix is the cheaper price.
- [ ] Genesis files (wallets.txt, bonds.txt) are identical between repos — not
      verified as part of this work
- [ ] Integration tests pass against the Rust node image — requires a CI run
- [x] Scala-specific files removed (logback.xml, SBT build config, Scala CI runner scripts)
- [x] Documentation updated (README, CLAUDE.md) with no Scala references

**Note on the two deviations:** both were explicit calls, not oversights. Keeping
`--rust` and the `-rust` filenames trades a cosmetic inconsistency for zero
breakage in callers this repo cannot see.

**Completed:** 2026-07-31 (partial — two criteria deliberately superseded, two unverified)

**Migration plan:** [docs/migration-to-rust-node.md](migration-to-rust-node.md)

---

#### US-002: Reproducible Randomized Exercise Soaks

> As a **release operator**, I want **weekend soaks to execute a seeded catalogue of bounded, valid operational workloads** so that **broad behavior is exercised continuously while every failure remains exactly replayable and attributable**.

**Implemented in:** EPIC-011, EPIC-012

**Acceptance Criteria:**

- [ ] The pinned executor catalogue is schema-validated before shard or OCI resources launch
- [ ] Every execution records immutable definition, orchestrator, seed, provider, topology, and safety-limit identity
- [ ] The initial six required valid-operation epochs run deterministically on Docker and subprocess providers
- [ ] Success and failure are evaluated from finalized state with workload, safety, host, reset, and infrastructure failures classified separately
- [ ] A recorded manifest replays without silently substituting newer definitions or limits
- [ ] Required, experimental, and gating policy is machine-readable for the f1r3node-rust scheduler
- [ ] A compatible experimental epoch publishes evidence for a one-line `systemIntegration.catalogRef` bump without changing privileged `runnerRef`
- [ ] Incompatible schema/capability changes and gating promotion are identified as coordinated, explicitly reviewed changes

**Completed:** Planned

**Contract:** [Randomized Exercise Soak Contract](specs/randomized-exercise-soak-contract.md)

**Canonical pin contract:** [f1r3node-rust Trusted CI Pin Registry](https://github.com/F1R3FLY-io/f1r3node-rust/blob/6d1120ce8fb179dee3a80517254f9fbcd1485a70/docs/ci-pins.md)

---

## Story Template

Use this template when adding new user stories:

```markdown
#### US-XXX: [Short Title]

> As a **[persona]**, I want **[capability]** so that **[benefit]**.

**Implemented in:** [EPIC-ID or "Planned"]

**Acceptance Criteria:**
- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Criterion 3

**Completed:** [Date or "Planned"]
```

---

## Relationship to Epics

User stories capture the **why** (user need and benefit). Epics capture the **what** (technical implementation tasks).

| Artifact | Purpose | Location |
| ---------- | --------- | ---------- |
| User Story | Business/user need | `docs/UserStories.md` |
| Epic | Implementation scope | `docs/ToDos.md` |
| Task | Technical work item | Nested in epic YAML |
| Acceptance Criteria | Definition of done | In user story |

**Workflow:**

1. Identify user need -> Create user story
2. Design solution -> Create epic with tasks
3. Implement -> Work through tasks via `/nextTask` and `/implement`
4. Complete -> Mark epic complete, update story status

---

## References

- **Task Tracking:** `docs/ToDos.md`
- **Completed Work:** `docs/CompletedTasks.md`
- **User Stories Standard** (canonical): [user-stories-standard.md](https://gitlab.com/smart-assets.io/gitlab-profile/-/blob/master/docs/common/user-stories-standard.md)

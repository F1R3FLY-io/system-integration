---
doc_type: user_stories
version: "1.0"
last_updated: "[DATE]"
---

# User Stories

<!--
TEMPLATE USAGE INSTRUCTIONS:
0. Update frontmatter: set last_updated to current date, increment version for structural changes
1. Add completed stories under "Completed Stories" section
2. Add planned stories under "Planned Stories" section
3. Move completed stories from "Planned" to "Completed" sections
4. Update epoch links when implementation begins
5. Check acceptance criteria as features are verified
6. (Optional) Update reference URLs if using a fork with modified standards
7. Remove these usage instruction comments before committing
-->

This document captures user stories that drive feature development. User stories are reverse-engineered from completed epochs and updated as new features are planned.

**Document Structure**
- Active stories: This file (`docs/UserStories.md`)
- Implementation tracking: `docs/ToDos.md` (epochs and tasks)
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

Stories below are candidates for future epochs. Move to "Completed Stories" when implemented.

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

## Story Template

Use this template when adding new user stories:

```markdown
#### US-XXX: [Short Title]

> As a **[persona]**, I want **[capability]** so that **[benefit]**.

**Implemented in:** [EPOCH-ID or "Planned"]

**Acceptance Criteria:**
- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Criterion 3

**Completed:** [Date or "Planned"]
```

---

## Relationship to Epochs

User stories capture the **why** (user need and benefit). Epochs capture the **what** (technical implementation tasks).

| Artifact | Purpose | Location |
|----------|---------|----------|
| User Story | Business/user need | `docs/UserStories.md` |
| Epoch | Implementation scope | `docs/ToDos.md` |
| Task | Technical work item | Nested in epoch YAML |
| Acceptance Criteria | Definition of done | In user story |

**Workflow:**
1. Identify user need -> Create user story
2. Design solution -> Create epoch with tasks
3. Implement -> Work through tasks via `/nextTask` and `/implement`
4. Complete -> Mark epoch complete, update story status

---

## References

- **Task Tracking:** `docs/ToDos.md`
- **Completed Work:** `docs/CompletedTasks.md`
- **User Stories Standard** (canonical): [user-stories-standard.md](https://gitlab.com/smart-assets.io/gitlab-profile/-/blob/master/docs/common/user-stories-standard.md)

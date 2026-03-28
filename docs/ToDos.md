---
doc_type: todos
version: "1.0"
last_updated: [DATE]
mr_status:
  ready: false
  target_branch: main
---

# Tasks and Epochs

<!--
TEMPLATE USAGE INSTRUCTIONS:
0. Update the frontmatter date when modifying this file
   (Update version only for significant structural changes to template)
1. Replace all [PROJECT_NAME] and [PROJECT_SPECIFIC] markers
2. Add new epochs using the YAML frontmatter format below
3. Move completed epochs to docs/CompletedTasks.md
4. Use /nextTask to find the next task to work on
5. Use /implement to execute tasks with full context
6. Remove these usage instruction comments before committing
-->

This document tracks implementation work through **epochs** (logical groupings of related tasks).

**Document Structure**
- Active work: This file (`docs/ToDos.md`)
- User stories: `docs/UserStories.md`
- Completed work: `docs/CompletedTasks.md`
- Backlog: `docs/Backlog.md`

**For LLM assistance in multi-repo workspace:**
See [Task Tracking Standard]([RELATIVE_PATH]/top-level-gitlab-profile/docs/common/task-tracking-standard.md)

**For reference (GitLab):**
[Task Tracking Standard](https://gitlab.com/smart-assets.io/gitlab-profile/-/blob/master/docs/common/task-tracking-standard.md)

---

## MR/PR Tracking

When all tasks in this file are complete and ready for merge, update the frontmatter:

```yaml
mr_status:
  ready: true
  target_branch: main
  title: "feat: [PROJECT_SPECIFIC: MR title]"
  description: |
    ## Summary
    - [Completed items]

    ## Test plan
    - [x] All tests passing
  labels: ["feature", "enhancement"]
```

---

## Active Epochs

<!-- Epochs are ordered by priority. Work on the highest priority epoch first. -->

---

### EPOCH-001: Migrate to Rust-Only f1r3node

```yaml
---
epoch_id: EPOCH-001
title: "Migrate to Rust-Only f1r3node"
status: pending
priority: p1
user_story: US-001
blocked_by: []
created_at: 2026-03-19
claimed_by: null
claimed_at: null
tasks:
  - id: TASK-001-1
    title: "Align genesis state in f1r3node-rust repo"
    status: pending
    acceptance:
      - "wallets.txt in f1r3node-rust matches system-integration (20 lines, correct amounts)"
      - "Shard starts successfully with updated wallets.txt"

  - id: TASK-001-2
    title: "Switch services.yml to f1r3node-rust repo"
    status: pending
    blocked_by: [TASK-001-1]
    acceptance:
      - "services.yml points to f1r3node-rust.git branch dev"
      - "Nix environment requirement removed from build config"
      - "shardctl clone fetches from f1r3node-rust repo"

  - id: TASK-001-3
    title: "Replace compose files with upstream versions"
    status: pending
    blocked_by: [TASK-001-2]
    acceptance:
      - "compose/f1r3node.yml uses Rust node image"
      - "All compose/f1r3node-rust-*.yml files removed"
      - "Scala-specific compose files removed"
      - "Config files updated to f1r3node-rust DRY structure"

  - id: TASK-001-4
    title: "Simplify shardctl (remove Scala/Rust duality)"
    status: pending
    blocked_by: [TASK-001-3]
    acceptance:
      - "NodeType enum removed from shardctl/node.py"
      - "--scala/--rust/--node-type flags removed from CLI"
      - "shardctl up starts Rust nodes by default"

  - id: TASK-001-5
    title: "Update integration test infrastructure"
    status: pending
    blocked_by: [TASK-001-3]
    acceptance:
      - "Test compose files use Rust node image"
      - "Integration tests pass against Rust node"

  - id: TASK-001-6
    title: "Update documentation and clean up"
    status: pending
    blocked_by: [TASK-001-4, TASK-001-5]
    acceptance:
      - "README.md has no Scala references"
      - "CLAUDE.md updated with Rust-only structure"
      - "Scala CI runner scripts removed"
      - "logback.xml removed"
---
```

**Context:** The Scala and Rust node implementations are maintained in parallel, creating complexity in shardctl (dual NodeType enum, doubled compose files, conditional build configs). The standalone f1r3node-rust repo builds with standard Cargo (no Nix/SBT), is actively developed, and has feature parity.

**Scope:**
- Switch repository source from f1r3node `rust/dev` branch to standalone f1r3node-rust repo
- Remove all Scala node support from shardctl, compose files, and tests
- Align genesis files between repos (critical: wallets.txt mismatch)
- NOT in scope: changes to the f1r3node-rust repo itself (except genesis fix)

**Notes:**
- See [migration plan](migration-to-rust-node.md) for detailed phase breakdown
- wallets.txt in f1r3node-rust has 8 lines vs 20 in system-integration (critical fix)
- Compose files need path adjustments when copying from upstream

---

<!-- Add more epochs following the same format -->

---

## Epoch Template

Use this template when adding new epochs:

```yaml
---
epoch_id: EPOCH-XXX
title: "Short descriptive title"
status: pending
priority: p2
user_story: US-XXX
blocked_by: []
created_at: YYYY-MM-DD
claimed_by: null         # Implementer ID: human-{email}, {tool}-session[-{id}], or {team}/{role}
claimed_at: null
tasks:
  - id: TASK-XXX-1
    title: "Task description"
    status: pending
    acceptance:
      - "Measurable acceptance criterion"
---
```

---

## Task States

| Status | Meaning | Next Action |
|--------|---------|-------------|
| `pending` | Not started | Available to claim |
| `in_progress` | Being worked on | Continue or handoff |
| `blocked` | Waiting on dependency | Check `blocked_by` |
| `review` | Ready for review | Review and approve |
| `complete` | Done | Move to CompletedTasks.md |

---

## Workflow

1. **Find next task**: Use `/nextTask` to identify the highest priority unclaimed task
2. **Claim task**: Set `claimed_by` using [Implementer Identification](../common/stigmergic-collaboration.md#implementer-identification) format and `status: in_progress`
3. **Implement**: Use `/implement` to execute with full context
4. **Complete**: Mark `status: complete` when acceptance criteria met
5. **Move epoch**: When all tasks complete, move epoch to `docs/CompletedTasks.md`

---

## References

- **User Stories:** `docs/UserStories.md`
- **Completed Work:** `docs/CompletedTasks.md`
- **Backlog:** `docs/Backlog.md`
- **MR/PR Tracking Standard:** [docs/common/todos-mr_pr-tracking-standard.md]([RELATIVE_PATH]/top-level-gitlab-profile/docs/common/todos-mr_pr-tracking-standard.md)

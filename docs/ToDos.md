---
doc_type: todos
version: "1.0"
last_updated: 2026-03-25
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

### EPOCH-001: Rust Shard Integration via f1r3node-rust

```yaml
---
epoch_id: EPOCH-001
title: "Rust Shard Integration via f1r3node-rust"
status: in_progress
priority: p1
user_story: US-001
blocked_by: []
created_at: 2026-03-25
claimed_by: claude-session
claimed_at: 2026-03-25T00:00:00Z
tasks:
  - id: TASK-001-1
    title: "Add f1r3node-rust repo to services.yml"
    status: complete
    acceptance:
      - "f1r3node-rust entry exists in services.yml with correct git URL and branch"
      - "shardctl clone pulls the f1r3node-rust repo into services/"

  - id: TASK-001-2
    title: "Create compose/f1r3node-rust.yml for Rust shard deployment"
    status: complete
    blocked_by: [TASK-001-1]
    acceptance:
      - "compose/f1r3node-rust.yml builds and deploys the Rust shard from services/f1r3node-rust"
      - "Rust shard container connects to the f1r3fly Docker network"

  - id: TASK-001-3
    title: "Add shardctl support for Rust shard status and lifecycle"
    status: pending
    blocked_by: [TASK-001-2]
    acceptance:
      - "shardctl status shows Rust shard health"
      - "shardctl up/down manages Rust shard alongside other services"

  - id: TASK-001-4
    title: "Document Rust shard deployment option"
    status: pending
    blocked_by: [TASK-001-2]
    acceptance:
      - "README.md documents how to deploy the Rust shard as an alternative to Scala"
---
```

**Context:** The f1r3node currently runs a Scala-based shard by default. A Rust implementation exists in a separate `f1r3node-rust` repository. Node operators need the option to deploy either implementation, giving them flexibility in performance characteristics and operational preferences.

**Scope:**
- Add f1r3node-rust as a clonable service repo
- Create Docker Compose configuration for the Rust shard
- Integrate Rust shard lifecycle into shardctl
- Document the deployment option
- Excluded: modifying the Rust shard codebase itself

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

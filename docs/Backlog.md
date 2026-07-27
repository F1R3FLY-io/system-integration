---
doc_type: backlog
version: "1.0"
last_updated: "2026-07-27"
---

# Backlog

This document captures deferred work, future ideas, and low-priority items that aren't ready for active development.

**Document Structure**
- Active work: `docs/ToDos.md`
- User stories: `docs/UserStories.md`
- Completed work: `docs/CompletedTasks.md`
- Deferred work: This file (`docs/Backlog.md`)

**For reference (GitLab):**
[Task Tracking Standard](https://gitlab.com/smart-assets.io/gitlab-profile/-/blob/master/docs/common/task-tracking-standard.md)

---

## Backlog Categories

Items are organized by category and rough priority within each category.

---

### Technical Debt

Items that improve code quality, performance, or maintainability but aren't blocking active development.

_None yet._

---

### Feature Ideas

Future features that have been identified but aren't yet prioritized.

_None yet._

---

### Research & Exploration

Items that need investigation before they can become actionable tasks.

_None yet._

---

### Dependencies & Blockers

Items waiting on external factors (upstream releases, third-party APIs, etc.)

_None yet._

---

## Backlog Template

Use this template when adding new backlog items:

```yaml
---
backlog_id: BACKLOG-XX-XXX
title: "Short descriptive title"
category: technical_debt | feature_idea | research | blocked_external
priority: p3
added_at: YYYY-MM-DD
reason_deferred: "Why this isn't active yet"
promotion_criteria:
  - "Condition that would make this active"
---
```

---

## Promoting Items to Active Work

When a backlog item is ready for active development:

1. Create an epic in `docs/ToDos.md` based on the backlog item
2. Create or link a user story in `docs/UserStories.md` if needed
3. Remove the item from this backlog (or mark as `promoted: true`)
4. Add a note referencing the original backlog ID

---

## References

- **Active Work:** `docs/ToDos.md`
- **User Stories:** `docs/UserStories.md`
- **Completed Work:** `docs/CompletedTasks.md`

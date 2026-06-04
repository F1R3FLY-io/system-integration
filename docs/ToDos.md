---
doc_type: todos
version: "1.0"
last_updated: 2026-06-04
---

# ToDos

## F1R3node Rust OCI Validation SSOT Ref

```yaml
---
id: TASK-F1R3NODE-RUST-SSOT-TAG
status: complete
created_at: 2026-06-04T19:19:03Z
completed_at: 2026-06-04T19:36:00Z
participants:
  - pi-system-integration-session
  - f1r3node-rust-session
---
```

**Objective:** Establish a single-source-of-truth trusted `system-integration` Git ref for `f1r3node-rust` OCI validation workflows.

**Resolution:** The f1r3node-rust session clarified that "tag" means a **Git ref/tag in `F1R3FLY-io/system-integration`**, not a Docker image tag. The f1r3node-rust-side SSOT is `.github/oci-validation.env`.

**Final tag:**

```text
refs/tags/f1r3node-rust-oci-validation-20260604
```

**Verified target:**

```text
tag object: f3992b10d79bd9e8806cc93d5461aa17db9014fa
peeled commit: 322712fd672fbf6a76f843b97fa9c2d8365a73fd
```

**f1r3node-rust handoff value:**

```text
SYSTEM_INTEGRATION_REF=refs/tags/f1r3node-rust-oci-validation-20260604
```

**Notes from command review:**

- The first `git push origin "refs/tags/$TAG"` failed because `$TAG` was empty in that shell invocation.
- The later multiline command succeeded because `TAG` and `TARGET` were set in the same shell.
- `fatal: tag ... already exists` was harmless; the local annotated tag had already been created.
- The abbreviated target resolved to the intended commit, and the remote peeled tag confirms the correct target.

## External Follow-up

```yaml
---
id: TASK-F1R3NODE-RUST-VALIDATE-CI-WIRING
status: external
owner: f1r3node-rust-session
---
```

The f1r3node-rust session owns validation and commit of its CI workflow wiring:

- `.github/oci-validation.env`
- `.github/workflows/ci.yml`
- `.github/workflows/oci-validation.yml`
- `.github/workflows/reusable-oci-validation.yml`
- `CONTRIBUTING.md`

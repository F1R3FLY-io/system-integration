# OCI Ephemeral GitHub Actions Runners

Self-hosted, per-job ephemeral GitHub Actions runners on Oracle Cloud Infrastructure for the f1r3node integration test suite.

Each runner is a fresh VM that boots from a pre-baked image, registers with `F1R3FLY-io/f1r3node`, picks up exactly one queued workflow job, then self-terminates. **No state survives between jobs** — this eliminates the runner-state-leak class of CI flakiness that plagued the persistent runner setup (`../setup-f1r3node-rust-runner.sh`).

## How it's wired

The integration tests in `f1r3node`'s `build-test-and-deploy.yml` workflow include a `launch_ephemeral_runners` job that fires automatically on every `pull_request`, `push`, and tag event. That job:

1. Installs the OCI CLI (on the workflow's `ubuntu-latest` runner)
2. Authenticates to OCI via the `OCI_*` repository secrets
3. Clones this directory (`system-integration/ci/oci-runners/`)
4. Runs `launch-runner.sh amd64` × 5 + `launch-runner.sh arm64` × 5 in parallel

Each launched VM boots, registers with GitHub using a short-lived token, picks up its assigned matrix job (`required_rust_integration_tests`), runs the canonical integration baseline, uploads logs, and `oci compute instance terminate`s itself via instance-principal auth.

## Status

- ✓ amd64 baked image (`Canonical Ubuntu 22.04` + Docker, Python 3.10, Poetry 1.8.5, Rust, OCI CLI, GH runner agent 2.334.0, node test Docker image pre-pulled)
- ✓ arm64 baked image (same stack)
- ✓ Wired into `f1r3node`'s `build-test-and-deploy.yml` as the integration test pool
- ✓ OCI CLI secrets configured on `F1R3FLY-io/f1r3node` (5 secrets, `OCI_*`)
- ✓ Validated 8/10 pass on the canonical baseline (amd64 only); 2 known test issues, **zero infra flakes**

## OCI topology

| Layer | Resource | Notes |
|---|---|---|
| Tenancy | `f1r3fly` | Same tenancy as `f1r3fly-devops` (persistent runners) |
| Compartment | `ci-runner` | Dedicated, separate from `f1r3fly-devops` |
| Region | `us-sanjose-1` (AD `fnZP:US-SANJOSE-1-AD-1`) | |
| VCN | `ci-runner-vcn` (10.0.0.0/16) | Internet Gateway, default route table |
| Subnet | `ci-runner-public-subnet` (10.0.1.0/24, public) | Security list: SSH ingress (22), all egress |
| Image (amd64) | `ci-runner-image-amd64-<timestamp>` | Custom; ~5 GB |
| Image (arm64) | `ci-runner-image-arm64-<timestamp>` | Custom; ~5 GB |
| Service user | `ci-runner-launcher` (in group `ci-runner-launchers`) | Has policy to manage instances + read networking in `ci-runner` compartment only |
| Dynamic group | `ci-runner-ephemeral` (matches instances in `ci-runner`) | Used for self-terminate via instance principal |
| Policy | `ci-runner-ephemeral-policy` | Grants the dynamic group permission to terminate its own instances |
| SSH pub key | [`ssh-authorized-key.pub`](ssh-authorized-key.pub) | Committed in-repo so CI launches work; private key only on operator's laptop |

## Shape

Both arches use **16 OCPU / 32 GB / boot from baked image**:
- amd64: `VM.Standard.E6.Flex` (AMD EPYC Turin, Zen 5)
- arm64: `VM.Standard.A1.Flex` (Ampere Altra) — only Ampere shape available in `us-sanjose-1`; A2.Flex (newer AmpereOne) would require a region migration.

## File inventory

| File | Purpose |
|---|---|
| `state.env` | All OCIDs + config (image OCIDs, compartment, VCN, subnet, shapes, runner version, SSH key paths). Sourced by every script. |
| `ssh-authorized-key.pub` | SSH public key authorized for launched runner VMs. Committed in-repo (safe — public keys are designed to be public). The private key lives only on the operator's laptop. |
| `cloud-init-golden.yml` | Bake-target cloud-init. Installs every dependency, downloads the runner agent, pre-pulls the node test Docker image, symlinks tools into `/usr/local/bin`, then `shutdown -h`. Used by `bake-image.sh` only. |
| `cloud-init-runner.yml.tmpl` | Production cloud-init template. Assumes the baked image; runs only the runner-specific steps (config.sh, run.sh, self-terminate). An idle watchdog self-terminates the VM if no job arrives within 45 min, so over-provisioned/cancelled-run runners don't leak. Variables (`__REG_TOKEN__`, `__GH_REPO__`, etc.) substituted at launch time by `launch-runner.sh`. |
| `bake-image.sh` | One-shot bake. Launches a golden VM, waits for it to STOP, snapshots its boot volume to a custom image, terminates the golden VM. Re-run when the runner agent version is deprecated or the pre-pulled node test Docker image needs refreshing. |
| `launch-runner.sh` | Launches one ephemeral runner VM. Mints a short-lived (1-hour) registration token via `gh api`, renders cloud-init, calls `oci compute instance launch`. Called by the CI workflow + can be invoked manually for debugging. |
| `reap-stale-runners.sh` | Safety net: terminates `ci-runner` VMs older than `MAX_AGE_HOURS` (default 6h, well past the ~45-min pipeline) and deregisters offline `ci-eph-*` runner entries. **Manual / on-demand only from this repo** — the scheduled reaping runs in `f1r3node-rust` (`.github/workflows/ci-runner-reaper.yml`), which is where the `ci-eph-*` pool registers and where the OCI credentials are provisioned. Supersedes the old `cleanup-orphan-runners.sh`. |
| `destroy-all.sh` | Emergency: interactively force-terminates every instance in `ci-runner` compartment. |
| `README.md` | This file. |

## OCI secrets in F1R3FLY-io/f1r3node

The CI workflow authenticates to OCI via 5 secrets (set via `gh secret set` / repo Actions secrets UI):

| Secret | Source |
|---|---|
| `OCI_TENANCY_OCID` | Tenancy OCID from `~/.oci/config` |
| `OCI_USER_OCID` | OCID of the scoped `ci-runner-launcher` user (NOT a human user) |
| `OCI_REGION` | `us-sanjose-1` |
| `OCI_FINGERPRINT` | Fingerprint of the API key for `ci-runner-launcher` |
| `OCI_PRIVATE_KEY` | Full PEM contents of the `ci-runner-launcher` API key (multi-line) |

The `ci-runner-launcher` user has *only* the permission to manage instances in the `ci-runner` compartment — no admin rights, no access to other tenancy resources. Blast radius is limited.

## Operations

### Regular CI

No manual action. Every push to a PR targeting `rust/dev` / `rust/main` / `rust/staging` / `feature/**` fires `build-test-and-deploy.yml`, which includes the 10-job ephemeral integration matrix (5 amd64 + 5 arm64).

### Re-bake an image

```bash
cd ci/oci-runners
./bake-image.sh amd64
./bake-image.sh arm64
```

After baking, update `AMD64_BAKED_IMAGE_OCID` / `ARM64_BAKED_IMAGE_OCID` in `state.env` with the printed OCID, commit, and CI picks up the new image on the next run.

### Launch one runner manually (for debugging)

```bash
./launch-runner.sh amd64
# or
./launch-runner.sh arm64
```

Useful for triaging an issue without going through the full PR pipeline. The runner will sit idle waiting for a queued job that matches its labels; cancel by manually terminating the instance (`destroy-all.sh`) if no job arrives.

### Emergency cleanup

```bash
./reap-stale-runners.sh        # terminate VMs >6h old + deregister offline GH runners (manual; the scheduled reaper lives in f1r3node-rust)
./destroy-all.sh               # nuke EVERY instance in ci-runner now (interactive) — only when no run is active
```

### Bigger ad-hoc soaks (for flake measurement)

The current matrix is 5+5 = 10 samples per PR push. If you need more samples (e.g., 50× for tighter flake stats), either:

1. **Push more commits** — N pushes = N × 10 samples. Cheap and simple.
2. **Temporarily widen the matrix** in `build-test-and-deploy.yml` (add more `slot` entries), push, then revert. The launch step needs the count bumped to match.
3. **Run pytest locally** with the canonical baseline against subprocess provider (see `integration-tests/test/docs/ARCHITECTURE.md`).

## Relationship to persistent runners

The persistent runners (documented in [`../CLAUDE.md`](../CLAUDE.md)) live in compartment `f1r3fly-devops` with labels `[..., f1r3fly-rust-ci]` (no `-ephemeral` suffix). After this PR merges:

- **Persistent runners** still run: `build_rust_docker_image` (the Docker image build itself) + the smoke tests + Scala CI workflows.
- **Ephemeral runners** now run: the integration test matrix (was `required_rust_integration_tests`).
- The two pools coexist by distinct labels.

Once we have a few weeks of green ephemeral integration tests, persistent runners' rust-ci entries can be reduced (they're still needed for the Docker image build, but a smaller pool would suffice).

## Cost summary

| Item | Approx |
|---|---|
| VCN / IGW / subnet / IAM | $0 (free) |
| Custom image storage | ~$0.13/month per image (~$0.26 total for 2 arches) |
| Single CI run (10 jobs × ~30 min) | ~$0.50 |
| Bake | ~$0.02 per arch (~10 min × $0.10/hr) |

For comparison: the persistent integration test runners (4 VMs always-on) cost ~$50–100/month regardless of usage. Ephemeral is dramatically cheaper for typical PR volume.

## Image refresh cadence

The baked image freezes a snapshot of:
- The GH runner agent (currently `2.334.0`)
- Docker CE + Docker Compose v2
- Python 3.10 + Poetry 1.8.5
- Rust stable toolchain
- OCI CLI
- `f1r3flyindustries/f1r3fly-rust:dev` Docker image (images baked before the
  `staging` branch deprecation contain `:staging` instead — re-bake to refresh)
- `/etc/sysctl.d/99-ci-port-reservation.conf` reserving the test
  PortAllocator's range (41000-49000) from kernel ephemeral
  assignment (`net.ipv4.ip_local_reserved_ports`). Eliminates the
  ephemeral-port race that surfaces as `Address already in use` at
  rnode bind time under subprocess provider.

The runner agent self-updates at registration if newer (no `--disableupdate` flag), so a stale baked agent is auto-recovered. But the **node test image** is frozen as baked — re-bake when the node image is bumped or every quarter.

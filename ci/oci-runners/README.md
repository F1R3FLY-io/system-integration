# OCI Ephemeral GitHub Actions Runners

Self-hosted, per-job ephemeral GitHub Actions runners on Oracle Cloud Infrastructure.

Each runner is a fresh VM that boots from a pre-baked image, registers with `F1R3FLY-io/f1r3node`, picks up exactly one queued workflow job, then self-terminates. **No state survives between jobs** — this eliminates the runner-state-leak class of CI flakiness that plagued the persistent runner setup (`../setup-f1r3node-rust-runner.sh`).

## Status

- ✓ amd64 baked image (`Canonical Ubuntu 22.04` + Docker, Python 3.10, Poetry 1.8.5, Rust, OCI CLI, GH runner agent 2.334.0, staging Docker image pre-pulled)
- ✓ arm64 baked image (same stack)
- ✓ workflow `oci-ephemeral-tests.yml` on `ci/oci-ephemeral-smoke` branch in f1r3node
- ✓ validated: 8/10 pass on full canonical baseline (amd64 only); 2 known test issues, **zero infra flakes**

## Topology

| Layer | Resource | Notes |
|---|---|---|
| Tenancy | `f1r3fly` | Same tenancy as `f1r3fly-devops` (persistent runners) |
| Compartment | `ci-runner` | Dedicated, separate from `f1r3fly-devops` |
| Region | `us-sanjose-1` (AD `fnZP:US-SANJOSE-1-AD-1`) | Single-AD |
| VCN | `ci-runner-vcn` (10.0.0.0/16) | Internet Gateway, default route table |
| Subnet | `ci-runner-public-subnet` (10.0.1.0/24, public) | Security list: SSH ingress (22), all egress |
| Image (amd64) | `ci-runner-image-amd64-<timestamp>` | Custom; ~5 GB |
| Image (arm64) | `ci-runner-image-arm64-<timestamp>` | Custom; ~5 GB |
| IAM | Dynamic group `ci-runner-ephemeral` | Matches instances in `ci-runner` compartment |
| IAM | Policy `ci-runner-ephemeral-policy` | Allows the dynamic group to terminate its own instances (for self-destruct via instance principal) |
| SSH key | `~/.ssh/oci-ci-runner` | ed25519, locally generated |

## Shape

Both arches use **8 OCPU / 24 GB / boot volume from baked image**:
- amd64: `VM.Standard.E5.Flex` (AMD EPYC)
- arm64: `VM.Standard.A1.Flex` (Ampere Altra)

## File inventory

| File | Purpose |
|---|---|
| `state.env` | All OCIDs + config (image OCIDs, compartment, VCN, subnet, shapes, runner version). Sourced by every script. |
| `cloud-init-golden.yml` | Bake-target cloud-init. Installs every dependency, downloads the runner agent, pre-pulls the staging Docker image, then `shutdown -h`. Used by `bake-image.sh` only. |
| `cloud-init-runner.yml.tmpl` | Production cloud-init template. Assumes the baked image; runs only the runner-specific steps (config.sh, run.sh, self-terminate). Variables (`__REG_TOKEN__`, `__GH_REPO__`, etc.) are substituted at launch time by `launch-runner.sh`. |
| `bake-image.sh` | One-shot bake. Launches a golden VM, waits for it to STOP, snapshots its boot volume to a custom image, terminates the golden VM. Re-run when the runner agent version is deprecated or when the staging image needs refreshing. |
| `launch-runner.sh` | Launches one ephemeral runner VM. Mints a short-lived (1-hour) registration token via `gh api`, renders cloud-init, calls `oci compute instance launch`. |
| `run-soak.sh` | Triggers one `workflow_dispatch` of `oci-ephemeral-tests.yml` (mode=soak), then launches 6 amd64 + 4 arm64 ephemeral runners in parallel. Each runner picks up one matrix job. |
| `cleanup-orphan-runners.sh` | Removes offline `ci-eph-*` runner entries from GitHub. Useful after a run where a runner failed before completing its job (rare; happens with deprecated agent versions or cloud-init bugs). |
| `destroy-all.sh` | Emergency: interactively force-terminates every instance in `ci-runner` compartment. |
| `README.md` | This file. |

## Workflows on f1r3node

One workflow file: `.github/workflows/oci-ephemeral-tests.yml` on the `ci/oci-ephemeral-smoke` branch. Two modes via the `mode` input:

| Mode | Trigger | Jobs | Purpose |
|---|---|---|---|
| **smoke** (default) | `push` to `ci/oci-ephemeral-smoke`, or `workflow_dispatch` with `mode=smoke` | 1 amd64 job (~30s) | Validates one ephemeral runner end-to-end. |
| **soak** | `workflow_dispatch` with `mode=soak` (via `run-soak.sh`) | 10 parallel matrix jobs: 5 amd64 + 5 arm64 (~30 min each) | Full canonical integration baseline × N. Flake measurement. |

The matrix labels are `[self-hosted, linux, x64, f1r3fly-rust-ci-ephemeral, oracle-cloud]` (amd64) and `[self-hosted, linux, arm64, f1r3fly-rust-ci-ephemeral, oracle-cloud]` (arm64). These are **distinct from the persistent runners' `f1r3fly-rust-ci` label** — the two pools coexist without contention.

## Common operations

### Run a soak

```bash
cd ci/oci-runners
./run-soak.sh           # default: timeout-scale=1.0, image-tag=staging
./run-soak.sh 1.5       # bump timeout scale for slower runs
./run-soak.sh 1.0 v0.4.12   # test a specific image tag
```

Cost: ~10 × 30 min × $0.10/hr = **~$0.50 per soak run**.

### Validate one runner manually

```bash
# Trigger one workflow_dispatch (smoke mode)
gh workflow run oci-ephemeral-tests.yml \
  --repo F1R3FLY-io/f1r3node \
  --ref ci/oci-ephemeral-smoke

# Launch one runner to pick it up
./launch-runner.sh amd64
# (or arm64)
```

### Re-bake an image

```bash
./bake-image.sh amd64
./bake-image.sh arm64
```

After baking, update `AMD64_BAKED_IMAGE_OCID` / `ARM64_BAKED_IMAGE_OCID` in `state.env` with the printed OCID. (The script prints the line to copy.)

### Cleanup

```bash
# After a soak run, GitHub-side runner entries are auto-cleaned by --ephemeral
# unless the runner failed before completing its job. To garbage-collect those:
./cleanup-orphan-runners.sh

# Emergency: nuke every instance in the compartment (interactive prompt)
./destroy-all.sh
```

## Relationship to persistent runners

The persistent runners (documented in `../CLAUDE.md`) live in compartment `f1r3fly-devops` with labels `[..., f1r3fly-rust-ci]` (note: no `-ephemeral` suffix). They:
- Are 4 long-lived VMs (2 amd64 + 2 arm64) used by the existing `build-test-and-deploy.yml` workflow
- Persist state between jobs (Docker layers, /tmp, kernel TIME_WAIT sockets — the source of the flakes we're solving)
- Will be decommissioned once the ephemeral pool fully replaces them

The two pools are isolated:
- Different compartments (`ci-runner` vs `f1r3fly-devops`)
- Different labels (so workflows can opt into either pool by label choice)
- Different SSH keys, IAM identities

## Image refresh cadence

The baked image freezes a snapshot of:
- The GH runner agent (release version, currently `2.334.0`)
- Docker CE + Docker Compose v2
- Python 3.10 + Poetry 1.8.5
- Rust stable toolchain
- OCI CLI
- `f1r3flyindustries/f1r3fly-rust-node:staging` Docker image

The runner agent self-updates at registration if newer (no `--disableupdate` flag), so a stale baked agent is auto-recovered. But the **staging test image** stays as baked — re-bake when the node image is bumped or every ~quarter.

## Cost summary

| Item | Approx |
|---|---|
| VCN / IGW / subnet / IAM | $0 (free) |
| Custom image storage | ~$0.13/month per image |
| Single ephemeral runner job | ~$0.05 (~32 min × $0.10/hr) |
| 10× soak | ~$0.50 |
| Bake | ~$0.02 (~10 min × $0.10/hr) |

The persistent runners cost ~$50–100/month always-on regardless of usage. Once decommissioned, ephemeral runners are dramatically cheaper for the same throughput.

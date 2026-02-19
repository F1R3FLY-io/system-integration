# F1R3FLY CI Runner Instances

## Overview

Self-hosted GitHub Actions runners for the F1R3FLY blockchain CI pipeline, hosted on Oracle Cloud Infrastructure (OCI). Scala and Rust runners are separate instances with distinct labels to prevent cross-routing (both workflows live in the same repo).

## Instance Configuration

| Property | Value |
|----------|-------|
| OS | Ubuntu 22.04 |
| Shapes | VM.Standard.E4.Flex (amd64), VM.Standard.A1.Flex (arm64) |
| Spec | 2 OCPU / 16 GB RAM / 50 GB boot volume |
| Compartment | f1r3fly-devops |
| VCN | f1r3node-ci-vcn |
| Subnet | f1r3node-ci-subnet (10.0.0.0/24, public) |
| SSH key | `f1r3fly-ci-oracle` (ed25519) |

## Instances

| Name | Arch | Stack | Labels |
|------|------|-------|--------|
| ci-scala-amd64 | x64 | Scala | `self-hosted,Linux,X64,f1r3fly-scala-ci,oracle-cloud` |
| ci-scala-arm64 | arm64 | Scala | `self-hosted,Linux,ARM64,f1r3fly-scala-ci,oracle-cloud` |
| ci-rust-amd64 | x64 | Rust | `self-hosted,Linux,X64,f1r3fly-rust-ci,oracle-cloud` |
| ci-rust-arm64 | arm64 | Rust | `self-hosted,Linux,ARM64,f1r3fly-rust-ci,oracle-cloud` |

Instance IPs are managed in the OCI console. To list registered runners:
```bash
gh api repos/F1R3FLY-io/f1r3node/actions/runners --jq '.runners[] | {name, status, labels: [.labels[].name]}'
```

## Runner Details

- **User**: `runner` (passwordless sudo, docker group)
- **Agent**: `/opt/actions-runner/`
- **Service**: `actions-runner` (systemd, auto-restart)
- **Work dir**: `/opt/actions-runner/_work`

### Label Routing

Both Scala and Rust workflows live in `F1R3FLY-io/f1r3node`. Distinct labels prevent cross-routing:
- Scala workflow uses `runs-on: [self-hosted, Linux, X64, f1r3fly-scala-ci]`
- Rust workflow uses `runs-on: [self-hosted, Linux, X64, f1r3fly-rust-ci]`

## Installed Software

### Scala Runners
- Docker CE + docker-compose v2
- Java 17 (Eclipse Temurin) + SBT
- Python 3.10 + Poetry
- GHC + Cabal (alex, happy, BNFC)
- Build tools: make, cmake, autoconf, libtool, protobuf-compiler, jflex

### Rust Runners
- Docker CE + docker-compose v2
- Rust stable (via rustup)
- Python 3.10 + Poetry
- Build tools: make, cmake, autoconf, libtool, protobuf-compiler, libssl-dev

## Management

```bash
# Health check instances (IPs provided as arguments, never stored)
ci/healthcheck-runners.sh --type rust  <IP1> <IP2>
ci/healthcheck-runners.sh --type scala <IP1> <IP2>
ci/healthcheck-runners.sh <IP>   # auto-detects type

# SSH into an instance
ssh -i ~/.ssh/f1r3fly-ci-oracle ubuntu@<IP>

# Runner service (on instance)
sudo systemctl status actions-runner
sudo systemctl restart actions-runner
sudo journalctl -u actions-runner -f

# Docker cleanup (runs daily at 03:00 UTC via cron)
sudo /usr/local/bin/ci-cleanup.sh
```

## Setup / Re-provision

```bash
# Generate a registration token
gh api -X POST repos/F1R3FLY-io/f1r3node/actions/runners/registration-token --jq '.token'

# Scala runner
scp ci/setup-f1r3node-scala-runner.sh ubuntu@<IP>:/tmp/
ssh ubuntu@<IP> "sudo bash /tmp/setup-f1r3node-scala-runner.sh \
  --repo F1R3FLY-io/f1r3node \
  --name <ci-scala-amd64|ci-scala-arm64> \
  --labels 'self-hosted,linux,<x64|arm64>,f1r3fly-scala-ci,oracle-cloud' \
  --token <TOKEN>"

# Rust runner
scp ci/setup-f1r3node-rust-runner.sh ubuntu@<IP>:/tmp/
ssh ubuntu@<IP> "sudo bash /tmp/setup-f1r3node-rust-runner.sh \
  --repo F1R3FLY-io/f1r3node \
  --name <ci-rust-amd64|ci-rust-arm64> \
  --labels 'self-hosted,linux,<x64|arm64>,f1r3fly-rust-ci,oracle-cloud' \
  --token <TOKEN>"
```

## Teardown / Migration

```bash
# Generate a removal token
gh api -X POST repos/F1R3FLY-io/f1r3node/actions/runners/remove-token --jq '.token'

# Run teardown (same script for both Scala and Rust)
scp ci/teardown-f1r3node-<scala|rust>-runner.sh ubuntu@<IP>:/tmp/
ssh ubuntu@<IP> "sudo bash /tmp/teardown-f1r3node-<scala|rust>-runner.sh --token <TOKEN>"
```

## Networking

- **Ingress**: SSH (22) only
- **Egress**: All (GitHub API, Docker Hub, PyPI, apt repos, crates.io)
- Internet Gateway attached to VCN for outbound access

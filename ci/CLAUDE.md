# F1R3FLY CI Runner Instance

## Overview

This is a self-hosted GitHub Actions runner for the F1R3FLY blockchain CI pipeline, hosted on Oracle Cloud Infrastructure (OCI).

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

| Name | Arch | Repo | IP |
|------|------|------|----|
| f1r3node-ci-scala-amd64 | x64 | F1R3FLY-io/f1r3node | 64.181.236.94 |
| f1r3node-ci-scala-arm64 | arm64 | F1R3FLY-io/f1r3node | 192.9.138.56 |
| f1r3node-ci-rust-amd64 | x64 | (future) | TBD |
| f1r3node-ci-rust-arm64 | arm64 | (future) | TBD |

## Runner Details

- **User**: `runner` (passwordless sudo, docker group)
- **Agent**: `/opt/actions-runner/`
- **Service**: `actions-runner` (systemd, auto-restart)
- **Labels**: `self-hosted,linux,{x64|arm64},f1r3fly-ci`
- **Work dir**: `/opt/actions-runner/_work`

## Installed Software

- Docker CE + docker-compose v2
- Java 17 (Eclipse Temurin)
- SBT
- Python 3.10 + Poetry
- GHC + Cabal (via ghcup)
- Build tools: make, cmake, autoconf, libtool, protobuf-compiler, jflex

## Management

```bash
# SSH into an instance
ssh -i ~/.ssh/f1r3fly-ci-oracle ubuntu@<IP>

# Runner service
sudo systemctl status actions-runner
sudo systemctl restart actions-runner
sudo journalctl -u actions-runner -f

# Docker cleanup (runs daily at 03:00 UTC via cron)
sudo /usr/local/bin/ci-cleanup.sh
```

## Setup / Re-provision

```bash
# Generate a registration token
gh api -X POST repos/OWNER/REPO/actions/runners/registration-token --jq '.token'

# Run setup (from system-integration repo)
scp ci/setup-f1r3node-scala-runner.sh ubuntu@<IP>:/tmp/
ssh ubuntu@<IP> "sudo bash /tmp/setup-f1r3node-scala-runner.sh \
  --repo OWNER/REPO \
  --name <runner-name> \
  --labels 'self-hosted,linux,<x64|arm64>,f1r3fly-ci' \
  --token <TOKEN>"
```

## Teardown / Migration

```bash
# Generate a removal token
gh api -X POST repos/OWNER/REPO/actions/runners/remove-token --jq '.token'

# Run teardown
scp ci/teardown-f1r3node-scala-runner.sh ubuntu@<IP>:/tmp/
ssh ubuntu@<IP> "sudo bash /tmp/teardown-f1r3node-scala-runner.sh --token <TOKEN>"
```

## Networking

- **Ingress**: SSH (22) only
- **Egress**: All (GitHub API, Docker Hub, PyPI, apt repos)
- Internet Gateway attached to VCN for outbound access

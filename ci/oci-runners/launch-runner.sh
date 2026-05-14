#!/usr/bin/env bash
# Launch one ephemeral GitHub Actions runner VM on OCI.
#
# Usage:
#   ./launch-runner.sh amd64
#   ./launch-runner.sh arm64
#
# Mints a short-lived (1-hour) runner registration token via `gh api` and embeds
# it in cloud-init user-data. The launched VM:
#   1. Installs deps (Docker w/ containerd-snapshotter, Python, Poetry, Rust)
#   2. Creates `runner` user (matches setup-f1r3node-rust-runner.sh)
#   3. Registers as repo-level ephemeral runner with labels
#        self-hosted,linux,<arch>,f1r3fly-rust-ci-ephemeral,oracle-cloud
#   4. Runs exactly one queued job
#   5. Self-terminates via instance-principal auth
#
# Requires:
#   - `gh` CLI authenticated with admin on F1R3FLY-io/f1r3node (verify: gh auth status)
#   - OCI CLI configured with credentials that can launch instances in ci-runner compartment

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/state.env"

ARCH="${1:-amd64}"
if [[ "$ARCH" != "amd64" && "$ARCH" != "arm64" ]]; then
  echo "Usage: $0 <amd64|arm64>" >&2
  exit 1
fi

export SUPPRESS_LABEL_WARNING=True

# Resolve shape, OCPUs, memory, image, runner arch label
if [[ "$ARCH" == "amd64" ]]; then
  SHAPE="$AMD64_SHAPE"
  OCPUS="$AMD64_OCPUS"
  MEM_GB="$AMD64_MEM_GB"
  IMAGE_NAME="$AMD64_IMAGE_NAME"
  IMAGE_OCID="${AMD64_BAKED_IMAGE_OCID:-}"
  RUNNER_AGENT_ARCH="x64"
  LABEL_ARCH="x64"
else
  SHAPE="$ARM64_SHAPE"
  OCPUS="$ARM64_OCPUS"
  MEM_GB="$ARM64_MEM_GB"
  IMAGE_NAME="$ARM64_IMAGE_NAME"
  IMAGE_OCID="${ARM64_BAKED_IMAGE_OCID:-}"
  RUNNER_AGENT_ARCH="arm64"
  LABEL_ARCH="arm64"
fi

# Verify gh CLI is authenticated
if ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: gh CLI not authenticated. Run 'gh auth login' first." >&2
  exit 1
fi

# Prefer baked image OCID (from bake-image.sh) — fastest cold start. Fall back
# to looking up the stock Ubuntu image by name, in which case cloud-init does
# the full dependency install (~3.5 min instead of ~30s).
if [[ -z "$IMAGE_OCID" ]]; then
  echo "WARN: no baked image OCID for $ARCH in state.env; falling back to stock Ubuntu image $IMAGE_NAME" >&2
  IMAGE_OCID=$(oci compute image list \
    -c "$COMP" \
    --shape "$SHAPE" \
    --query "data[?\"display-name\"=='$IMAGE_NAME'].id|[0]" \
    --raw-output 2>/dev/null)
  if [[ -z "$IMAGE_OCID" || "$IMAGE_OCID" == "null" ]]; then
    echo "ERROR: could not resolve image $IMAGE_NAME for shape $SHAPE in compartment $COMP" >&2
    exit 1
  fi
fi

# Generate a unique runner/instance name. Repo slug is embedded so a glance at
# the OCI instance list (or `gh api .../actions/runners`) shows which repo each
# runner belongs to — both repos register into the same OCI compartment.
REPO_SLUG="${GH_REPO##*/}"
TS=$(date +%Y%m%d-%H%M%S)
RAND=$(openssl rand -hex 3)
RUNNER_NAME="ci-eph-$REPO_SLUG-$ARCH-$TS-$RAND"

# Mint a runner registration token via gh CLI (uses local gh auth, no PAT)
REG_TOKEN=$(gh api -X POST "repos/$GH_REPO/actions/runners/registration-token" --jq '.token')
if [[ -z "$REG_TOKEN" || "$REG_TOKEN" == "null" ]]; then
  echo "ERROR: failed to mint registration token via 'gh api'" >&2
  exit 1
fi

# Labels: distinct -ephemeral suffix so workflows opt in explicitly
LABELS="self-hosted,linux,$LABEL_ARCH,f1r3fly-rust-ci-ephemeral,oracle-cloud"

# Render cloud-init from template
CLOUD_INIT_TMPL="$SCRIPT_DIR/cloud-init-runner.yml.tmpl"
CLOUD_INIT_RENDERED=$(mktemp -t oci-cloud-init.XXXXXX.yml)
trap 'rm -f "$CLOUD_INIT_RENDERED"' EXIT

# sed-escape values that may contain &, /, or \
esc() { printf '%s' "$1" | sed 's/[&/\]/\\&/g'; }

sed \
  -e "s|__REG_TOKEN__|$(esc "$REG_TOKEN")|g" \
  -e "s|__GH_REPO__|$(esc "$GH_REPO")|g" \
  -e "s|__RUNNER_NAME__|$(esc "$RUNNER_NAME")|g" \
  -e "s|__RUNNER_LABELS__|$(esc "$LABELS")|g" \
  -e "s|__RUNNER_VERSION__|$(esc "$RUNNER_VERSION")|g" \
  -e "s|__RUNNER_ARCH__|$(esc "$RUNNER_AGENT_ARCH")|g" \
  "$CLOUD_INIT_TMPL" > "$CLOUD_INIT_RENDERED"

# Resolve SSH public key path: ~ expansion (operator path) OR repo-relative
# (CI / committed-in-repo path).
if [[ "$SSH_KEY_PUB" == /* ]]; then
  SSH_KEY_PUB_RESOLVED="$SSH_KEY_PUB"
elif [[ "$SSH_KEY_PUB" == ~* ]]; then
  SSH_KEY_PUB_RESOLVED="${SSH_KEY_PUB/#\~/$HOME}"
else
  SSH_KEY_PUB_RESOLVED="$SCRIPT_DIR/$SSH_KEY_PUB"
fi

echo "=== Launching $RUNNER_NAME ==="
echo "  Shape:       $SHAPE"
echo "  OCPUs:       $OCPUS"
echo "  Memory:      ${MEM_GB} GB"
echo "  Image:       $IMAGE_OCID"
echo "  Labels:      $LABELS"

INSTANCE_OCID=$(oci compute instance launch \
  -c "$COMP" \
  --availability-domain "$AD" \
  --shape "$SHAPE" \
  --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM_GB}" \
  --image-id "$IMAGE_OCID" \
  --subnet-id "$SUBNET_OCID" \
  --display-name "$RUNNER_NAME" \
  --assign-public-ip true \
  --ssh-authorized-keys-file "$SSH_KEY_PUB_RESOLVED" \
  --user-data-file "$CLOUD_INIT_RENDERED" \
  --query 'data.id' --raw-output 2>&1 | tail -1)

echo "  Instance:    $INSTANCE_OCID"
echo
echo "Cloud-init runs ~3–5 min before the runner registers on GitHub."
echo
echo "Inspect after launch:"
echo "  oci compute instance get --instance-id $INSTANCE_OCID --query 'data.{state:\"lifecycle-state\",ip:\"public-ip\"}'"
echo "  gh api repos/$GH_REPO/actions/runners --jq '.runners[] | select(.name==\"$RUNNER_NAME\")'"
echo
echo "SSH (for debugging the bootstrap):"
echo "  ssh -i $SSH_KEY_PRIV ubuntu@<public-ip> sudo tail -f /var/log/runner-bootstrap.log"

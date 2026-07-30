#!/usr/bin/env bash
# Bake a custom OCI image for ephemeral runners.
#
# Usage:
#   ./bake-image.sh amd64
#   ./bake-image.sh arm64
#
# Flow:
#   1. Launch a golden instance from the stock Ubuntu image
#   2. Cloud-init installs every dependency, downloads the runner agent,
#      pre-pulls the staging image, then `shutdown -h`
#   3. Wait for the instance to reach STOPPED
#   4. Create a custom image from its boot volume
#   5. Wait for the image to be AVAILABLE
#   6. Terminate the golden instance + boot volume
#   7. Print the new image OCID for state.env

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

if [[ "$ARCH" == "amd64" ]]; then
  SHAPE="$AMD64_SHAPE"
  OCPUS="$AMD64_OCPUS"
  MEM_GB="$AMD64_MEM_GB"
  BASE_IMAGE_NAME="$AMD64_IMAGE_NAME"
else
  SHAPE="$ARM64_SHAPE"
  OCPUS="$ARM64_OCPUS"
  MEM_GB="$ARM64_MEM_GB"
  BASE_IMAGE_NAME="$ARM64_IMAGE_NAME"
fi

# Resolve stock Ubuntu image
BASE_IMAGE_OCID=$(oci compute image list \
  -c "$COMP" \
  --shape "$SHAPE" \
  --query "data[?\"display-name\"=='$BASE_IMAGE_NAME'].id|[0]" \
  --raw-output 2>/dev/null)
if [[ -z "$BASE_IMAGE_OCID" || "$BASE_IMAGE_OCID" == "null" ]]; then
  echo "ERROR: could not resolve base image $BASE_IMAGE_NAME" >&2
  exit 1
fi

TS=$(date +%Y%m%d-%H%M%S)
GOLDEN_NAME="ci-runner-golden-$ARCH-$TS"
IMAGE_NAME="ci-runner-image-$ARCH-$TS"

SSH_KEY_PUB_RESOLVED="${SSH_KEY_PUB/#\~/$HOME}"
# Resolve relative paths against the script's own directory so the script
# can be invoked from any cwd (e.g. the repo root in CI / agent workflows).
if [[ "$SSH_KEY_PUB_RESOLVED" != /* ]]; then
  SSH_KEY_PUB_RESOLVED="$SCRIPT_DIR/$SSH_KEY_PUB_RESOLVED"
fi

# Cost guard. The golden VM is terminated in step [4/6], but every step between
# the launch below and that point can abort under `set -euo pipefail` — the
# step [2/6] bootstrap wait most of all, since it polls for minutes and is the
# step an operator is most likely to interrupt. On any such exit the instance
# leaks, and NOTHING reaps it:
#
#   * f1r3node-rust's ci-runner-reaper.yml is the only scheduled reaper with
#     credentials, and it filters on display-name `ci-eph-*` with an explicit
#     defense-in-depth SKIP for anything else. A `ci-runner-golden-*` VM is
#     skipped by design.
#   * this repo's reap-stale-runners.sh has no name filter and would have caught
#     it, but it is manual-only (its scheduled workflow was never provisioned
#     with OCI credentials and never once ran).
#
# A leaked instance bills indefinitely; a leaked STOPPED one still bills its boot
# volume. Hence: terminate on any unclean exit. Idempotent with step [4/6], which
# clears the trap on success.
GOLDEN_INSTANCE_OCID=""

_reap_golden_on_exit() {
  local rc=$?
  [ -n "$GOLDEN_INSTANCE_OCID" ] || exit "$rc"
  echo
  echo "!!! Bake did not complete (exit $rc). Terminating golden instance to avoid a billing leak."
  echo "    $GOLDEN_INSTANCE_OCID"
  if oci compute instance terminate \
    --instance-id "$GOLDEN_INSTANCE_OCID" \
    --force \
    --preserve-boot-volume false >/dev/null 2>&1; then
    echo "    Terminate call submitted."
  else
    # Never leave this silent: an unreaped VM is the expensive failure mode, so
    # the operator must be told exactly what to remove by hand.
    echo "    TERMINATE FAILED. Remove it manually or it will bill indefinitely:"
    echo "      oci compute instance terminate --instance-id $GOLDEN_INSTANCE_OCID \\"
    echo "        --force --preserve-boot-volume false"
    echo "    Or run ./destroy-all.sh to clear the whole ci-runner compartment."
  fi
  exit "$rc"
}
trap _reap_golden_on_exit EXIT INT TERM

echo "=== [1/6] Launching golden instance $GOLDEN_NAME ==="
INSTANCE_OCID=$(oci compute instance launch \
  -c "$COMP" \
  --availability-domain "$AD" \
  --shape "$SHAPE" \
  --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM_GB}" \
  --image-id "$BASE_IMAGE_OCID" \
  --subnet-id "$SUBNET_OCID" \
  --display-name "$GOLDEN_NAME" \
  --assign-public-ip true \
  --ssh-authorized-keys-file "$SSH_KEY_PUB_RESOLVED" \
  --user-data-file "$SCRIPT_DIR/cloud-init-golden.yml" \
  --query 'data.id' --raw-output 2>/dev/null)
echo "  Instance: $INSTANCE_OCID"
# Arm the cost guard as early as possible: from here on, any exit reaps this VM.
GOLDEN_INSTANCE_OCID="$INSTANCE_OCID"

echo "=== [2/6] Waiting for golden bootstrap to complete (instance reaches STOPPED) ==="
echo "    Expected ~6-10 min: apt installs, Docker, Python, Rust, OCI CLI, runner agent, staging image pull"
# Poll for STOPPED. cloud-init ends with `shutdown -h +1` (1-min grace).
# Two-stage wait:
#   * up to ~15 min for OCI to natively see STOPPED
#   * if still RUNNING by then, send SOFTSTOP — OS has almost certainly
#     completed shutdown by then; this nudges OCI's view to catch up.
#     SOFTSTOP is a no-op signal if the OS is already down.
#   * then poll another ~15 min for STOPPED.
deadline=$(($(date +%s) + 1500))     # 25 min total
softstop_after=$(($(date +%s) + 900))  # nudge OCI after 15 min
softstop_sent=0
while true; do
  state=$(oci compute instance get --instance-id "$INSTANCE_OCID" --query 'data."lifecycle-state"' --raw-output 2>/dev/null || echo "?")
  echo "  [$(date +%H:%M:%S)] state=$state"
  if [[ "$state" == "STOPPED" ]]; then
    break
  fi
  now=$(date +%s)
  if [[ $softstop_sent -eq 0 && $now -ge $softstop_after ]]; then
    echo "  [$(date +%H:%M:%S)] still RUNNING after 15 min — issuing SOFTSTOP to nudge OCI's view"
    oci compute instance action --instance-id "$INSTANCE_OCID" --action SOFTSTOP --query 'data."lifecycle-state"' --raw-output >/dev/null 2>&1 || true
    softstop_sent=1
  fi
  if [[ $now -ge $deadline ]]; then
    echo "ERROR: golden instance never reached STOPPED within 25 min (even after SOFTSTOP)" >&2
    exit 1
  fi
  sleep 20
done

echo "=== [3/6] Creating custom image $IMAGE_NAME from golden boot volume ==="
NEW_IMAGE_OCID=$(oci compute image create \
  -c "$COMP" \
  --instance-id "$INSTANCE_OCID" \
  --display-name "$IMAGE_NAME" \
  --wait-for-state AVAILABLE \
  --query 'data.id' --raw-output 2>&1 | tail -1)
echo "  Image: $NEW_IMAGE_OCID"

echo "=== [4/6] Terminating golden instance + boot volume ==="
set +e
oci compute instance terminate \
  --instance-id "$INSTANCE_OCID" \
  --force \
  --preserve-boot-volume false 2>&1 | tail -3
terminate_rc=${PIPESTATUS[0]}
set -e

if [ "$terminate_rc" -eq 0 ]; then
  # Confirmed gone — disarm the cost guard so the EXIT trap is a no-op.
  GOLDEN_INSTANCE_OCID=""
else
  # Deliberately leave the guard armed. This step used to end in `|| true`,
  # which printed the "Done" banner while the instance kept billing. Now the
  # exit trap retries the terminate and, failing that, prints the manual
  # command — a bake must never report success over a live VM.
  echo "  WARNING: terminate returned $terminate_rc; the exit guard will retry it."
fi

echo
echo "=== [5/6] Done. Update state.env ==="
echo
if [[ "$ARCH" == "amd64" ]]; then
  echo "AMD64_BAKED_IMAGE_OCID=$NEW_IMAGE_OCID"
else
  echo "ARM64_BAKED_IMAGE_OCID=$NEW_IMAGE_OCID"
fi
echo
echo "=== [6/6] Then point launch-runner.sh at it (next: slim cloud-init.yml.tmpl) ==="

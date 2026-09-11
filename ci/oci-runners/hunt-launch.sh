#!/usr/bin/env bash
# Launch personal flake-hunt VMs on OCI (no GitHub runner registration).
#
# Usage:
#   ./hunt-launch.sh <amd64|arm64> [count]
#
# Boots the baked CI image with a minimal cloud-init: no runner agent, no
# registration token — just a hard self-shutdown fuse as a cost backstop
# (stopped Flex instances stop OCPU billing). Instances are named
# flake-hunt-<arch>-<ts>-<rand>; drive them with hunt-run.sh, terminate with
# hunt-teardown.sh.
#
# WARNING: the scheduled CI reaper (reap-runners.yml -> reap-stale-runners.sh)
# terminates ANY RUNNING instance in the compartment older than its cutoff
# (6h), hunt VMs included. Plan hunts in <6h windows and collect forensics
# from a caught shard promptly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/state.env"

ARCH="${1:-amd64}"
COUNT="${2:-1}"
FUSE_MINUTES=360

if [[ "$ARCH" != "amd64" && "$ARCH" != "arm64" ]]; then
  echo "Usage: $0 <amd64|arm64> [count]" >&2
  exit 1
fi

if [[ "$ARCH" == "amd64" ]]; then
  SHAPE="$AMD64_SHAPE"
  OCPUS="$AMD64_OCPUS"
  MEM_GB="$AMD64_MEM_GB"
  IMAGE_OCID="${AMD64_BAKED_IMAGE_OCID:?baked amd64 image OCID missing from state.env}"
else
  SHAPE="$ARM64_SHAPE"
  OCPUS="$ARM64_OCPUS"
  MEM_GB="$ARM64_MEM_GB"
  IMAGE_OCID="${ARM64_BAKED_IMAGE_OCID:?baked arm64 image OCID missing from state.env}"
fi

# Per-launch shape overrides. A hunt that runs the node under heavy tracing
# needs materially more RAM than the pool default (the debug merge stream adds
# ~11 GB to a 5-node shard), and that is a property of the RUN, not of the
# pool — so it is an env override rather than a state.env edit.
OCPUS="${HUNT_OCPUS:-$OCPUS}"
MEM_GB="${HUNT_MEM_GB:-$MEM_GB}"
# Boot volume: the baked image's default is ~45 GB, which the debug merge
# stream fills in ~20 minutes (~2 GB/min across 5 nodes). Set HUNT_BOOT_GB for
# runs that hunt instrumented.
BOOT_GB="${HUNT_BOOT_GB:-}"

if [[ "$SSH_KEY_PUB" == /* ]]; then
  SSH_KEY_PUB_RESOLVED="$SSH_KEY_PUB"
elif [[ "$SSH_KEY_PUB" == ~* ]]; then
  SSH_KEY_PUB_RESOLVED="${SSH_KEY_PUB/#\~/$HOME}"
else
  SSH_KEY_PUB_RESOLVED="$SCRIPT_DIR/$SSH_KEY_PUB"
fi

CLOUD_INIT=$(mktemp -t hunt-cloud-init.XXXXXX.yml)
LAUNCH_ERR=$(mktemp -t hunt-launch-err.XXXXXX)
trap 'rm -f "$CLOUD_INIT" "$LAUNCH_ERR"' EXIT

# Reboot-proof cost fuse. A one-shot `shutdown -h +N` is cancelled by any
# reboot during provisioning (why four hunt VMs outlived their fuses by up
# to 48h), and the 6h reaper workflow only runs from the repo's default
# branch. This fuse survives both failure modes: the absolute deadline is
# written to disk at first boot and a cron watchdog compares wall-clock to
# it every 5 minutes — a STOPPED Flex instance stops OCPU/memory billing,
# and hunt-teardown.sh cleans the stopped husk.
cat > "$CLOUD_INIT" <<EOF
#cloud-config
write_files:
  - path: /usr/local/bin/hunt-fuse-check
    permissions: "0755"
    content: |
      #!/bin/bash
      # Power off once wall-clock passes the recorded absolute deadline.
      # Self-healing: if the deadline was never written (a reboot raced
      # cloud-init's runcmd), anchor it to this first check instead —
      # the fuse may start minutes late but can never be disarmed.
      deadline_file=/etc/hunt-fuse-deadline
      if [ ! -f "\$deadline_file" ]; then
        echo \$(( \$(date +%s) + ${FUSE_MINUTES} * 60 )) > "\$deadline_file"
        exit 0
      fi
      now=\$(date +%s)
      deadline=\$(cat "\$deadline_file")
      if [ "\$now" -ge "\$deadline" ]; then
        /usr/sbin/poweroff
      fi
  - path: /etc/cron.d/hunt-fuse
    permissions: "0644"
    content: |
      */5 * * * * root /usr/local/bin/hunt-fuse-check
runcmd:
  - bash -c 'echo \$(( \$(date +%s) + ${FUSE_MINUTES} * 60 )) > /etc/hunt-fuse-deadline'
EOF

for n in $(seq 1 "$COUNT"); do
  TS=$(date +%Y%m%d-%H%M%S)
  RAND=$(openssl rand -hex 3)
  NAME="flake-hunt-$ARCH-$TS-$RAND"

  echo "=== Launching $NAME ($n/$COUNT) ==="
  echo "  Shape:  $SHAPE ($OCPUS OCPU / ${MEM_GB} GB${BOOT_GB:+ / ${BOOT_GB} GB boot})"
  echo "  Fuse:   poweroff at launch + ${FUSE_MINUTES}m (reboot-proof cron watchdog)"

  BOOT_ARGS=()
  if [[ -n "$BOOT_GB" ]]; then
    BOOT_ARGS=(--boot-volume-size-in-gbs "$BOOT_GB")
  fi

  if ! INSTANCE_OCID=$(oci compute instance launch \
      -c "$COMP" \
      --availability-domain "$AD" \
      --shape "$SHAPE" \
      --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM_GB}" \
      "${BOOT_ARGS[@]}" \
      --image-id "$IMAGE_OCID" \
      --subnet-id "$SUBNET_OCID" \
      --display-name "$NAME" \
      --assign-public-ip true \
      --ssh-authorized-keys-file "$SSH_KEY_PUB_RESOLVED" \
      --user-data-file "$CLOUD_INIT" \
      --query 'data.id' --raw-output 2>"$LAUNCH_ERR"); then
    echo "  launch failed:" >&2
    sed 's/^/    /' "$LAUNCH_ERR" >&2
    exit 1
  fi
  echo "  Instance: $INSTANCE_OCID"

  # Public IP appears once the VNIC attaches; poll briefly.
  IP=""
  for _ in $(seq 1 30); do
    IP=$(oci compute instance list-vnics --instance-id "$INSTANCE_OCID" \
      --query 'data[0]."public-ip"' --raw-output 2>/dev/null || true)
    [[ -n "$IP" && "$IP" != "null" ]] && break
    sleep 5
  done
  if [[ -z "$IP" || "$IP" == "null" ]]; then
    echo "  WARN: no public IP after 150s; check the console for $INSTANCE_OCID" >&2
  else
    echo "  Public IP: $IP"
  fi
done

echo
echo "SSH:   ssh -i $SSH_KEY_PRIV ubuntu@<ip>"
echo "Drive: ./hunt-run.sh --ips \"<ip> [<ip> ...]\""

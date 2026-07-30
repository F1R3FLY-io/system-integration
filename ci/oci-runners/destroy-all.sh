#!/usr/bin/env bash
# Emergency cleanup: force-terminate every instance in the ci-runner compartment.
#
# Use when ephemeral runners get stuck and don't self-terminate. Skips any
# instance not in the runner compartment, so other compartments are untouched.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/state.env"
export SUPPRESS_LABEL_WARNING=True

echo "=== Instances in ci-runner compartment ==="
oci compute instance list -c "$COMP" \
  --query 'data[?"lifecycle-state"!=`TERMINATED`].{name:"display-name", id:id, state:"lifecycle-state"}' \
  --output table

echo
read -r -p "Terminate all of the above? [y/N] " ANSWER
if [[ "$ANSWER" != "y" && "$ANSWER" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

IDS=$(oci compute instance list -c "$COMP" \
  --query 'data[?"lifecycle-state"!=`TERMINATED`].id' \
  --raw-output 2>/dev/null | tr -d '[]," ' | grep -v '^$')

for ID in $IDS; do
  echo "Terminating $ID..."
  oci compute instance terminate \
    --instance-id "$ID" \
    --force \
    --preserve-boot-volume false &
done
wait
echo "All terminate calls submitted."

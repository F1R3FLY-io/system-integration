#!/usr/bin/env bash
# Terminate every flake-hunt-* instance in the CI compartment (any non-
# terminated lifecycle state, so stopped-by-fuse instances are cleaned too).
#
# Usage:
#   ./hunt-teardown.sh [--dry-run]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/state.env"

DRY_RUN="${1:-}"

MATCHES=$(oci compute instance list -c "$COMP" --all \
  --query "data[?starts_with(\"display-name\",'flake-hunt-') && \"lifecycle-state\"!='TERMINATED' && \"lifecycle-state\"!='TERMINATING'].[id,\"display-name\",\"lifecycle-state\"]" \
  --output table 2>/dev/null | tail -n +4 | grep -v '^+' || true)

if [[ -z "$MATCHES" ]]; then
  echo "No flake-hunt instances found."
  exit 0
fi

echo "$MATCHES"

IDS=$(oci compute instance list -c "$COMP" --all \
  --query "data[?starts_with(\"display-name\",'flake-hunt-') && \"lifecycle-state\"!='TERMINATED' && \"lifecycle-state\"!='TERMINATING'].id" \
  --raw-output | jq -r '.[]' 2>/dev/null || true)

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "(dry run — nothing terminated)"
  exit 0
fi

for id in $IDS; do
  echo "Terminating $id"
  oci compute instance terminate --instance-id "$id" --force
done
echo "Done."

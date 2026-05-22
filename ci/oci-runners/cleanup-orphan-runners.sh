#!/usr/bin/env bash
# Remove offline ephemeral runner entries from GitHub.
#
# `--ephemeral` runners auto-deregister after completing a job. They do NOT
# auto-deregister if the runner exits before completing a job (e.g. cloud-init
# failure, deprecated runner version, mid-bootstrap crash). This script
# garbage-collects those leftovers.
#
# Safe to run anytime; only removes runners with status=offline AND name
# matching ci-eph-*.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/state.env"

ORPHANS=$(gh api "repos/$GH_REPO/actions/runners" \
  --jq '.runners[] | select(.status == "offline" and (.name | startswith("ci-eph"))) | "\(.id) \(.name)"' \
  2>/dev/null)

if [ -z "$ORPHANS" ]; then
  echo "No offline ephemeral runners to clean up."
  exit 0
fi

echo "Removing offline ephemeral runners:"
while IFS= read -r line; do
  ID=$(echo "$line" | awk '{print $1}')
  NAME=$(echo "$line" | awk '{print $2}')
  echo "  $NAME (id=$ID)"
  gh api -X DELETE "repos/$GH_REPO/actions/runners/$ID" 2>&1 || \
    echo "    (delete failed)"
done <<< "$ORPHANS"
echo "Done."

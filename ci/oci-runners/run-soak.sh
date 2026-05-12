#!/usr/bin/env bash
# Run the integration soak: 1 workflow_dispatch run that fans out to 10
# parallel matrix jobs (6 amd64 + 4 arm64), each on its own ephemeral OCI
# runner.
#
# Usage:
#   ./run-soak.sh [timeout-scale] [image-tag]
#     timeout-scale  pytest --timeout-scale shared by all jobs (default: 1.0)
#     image-tag      F1R3FLY_NODE_IMAGE tag (default: staging)
#
# Flow:
#   1. POST one workflow_dispatch with mode=soak. GH creates 1 workflow run
#      with 10 queued jobs (6 amd64 + 4 arm64) per the matrix in
#      .github/workflows/oci-ephemeral-smoke.yml.
#   2. Launch 6 amd64 + 4 arm64 ephemeral runner VMs in parallel. Each VM
#      registers as a self-hosted runner; GH dispatches one job per runner
#      based on arch labels.
#   3. Each runner runs its one job (--ephemeral), uploads the
#      integration-tests artifact, self-terminates.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/state.env"

TIMEOUT_SCALE="${1:-1.0}"
IMAGE_TAG="${2:-staging}"
BRANCH=ci/oci-ephemeral-smoke

# Matrix size — keep in lockstep with the matrix in oci-ephemeral-tests.yml.
AMD64_COUNT=5
ARM64_COUNT=5

WORKFLOW_FILENAME=oci-ephemeral-tests.yml

echo "=== Resolve workflow ID from $GH_REPO ==="
WORKFLOW_ID=$(gh api "repos/$GH_REPO/actions/workflows" \
  --jq ".workflows[] | select(.path | endswith(\"$WORKFLOW_FILENAME\")) | .id" 2>/dev/null)
if [ -z "$WORKFLOW_ID" ]; then
  echo "ERROR: workflow $WORKFLOW_FILENAME not found / not yet registered." >&2
  exit 1
fi
echo "  Workflow ID: $WORKFLOW_ID"

echo
echo "=== Triggering 1 soak run (mode=soak; matrix fans out to $((AMD64_COUNT + ARM64_COUNT)) jobs) ==="
printf '%s\n' "{\"ref\":\"$BRANCH\",\"inputs\":{\"mode\":\"soak\",\"timeout-scale\":\"$TIMEOUT_SCALE\",\"image-tag\":\"$IMAGE_TAG\"}}" \
  | gh api -X POST "repos/$GH_REPO/actions/workflows/$WORKFLOW_ID/dispatches" --input -
echo "  Triggered."

echo
echo "=== Waiting 10s for matrix jobs to queue ==="
sleep 10

echo "=== Most-recent run + its $((AMD64_COUNT + ARM64_COUNT)) queued jobs ==="
RUN_ID=$(gh run list --repo "$GH_REPO" --workflow "$WORKFLOW_FILENAME" --limit 1 --json databaseId --jq '.[0].databaseId')
echo "  Run: $RUN_ID"
gh api "repos/$GH_REPO/actions/runs/$RUN_ID/jobs" \
  --jq '.jobs[] | "  \(.name): \(.status)"' 2>/dev/null | head -12

echo
echo "=== Launching $AMD64_COUNT amd64 + $ARM64_COUNT arm64 ephemeral runners in parallel ==="
for _ in $(seq 1 "$AMD64_COUNT"); do
  "$SCRIPT_DIR/launch-runner.sh" amd64 2>&1 | tail -1 &
done
for _ in $(seq 1 "$ARM64_COUNT"); do
  "$SCRIPT_DIR/launch-runner.sh" arm64 2>&1 | tail -1 &
done
wait

echo
echo "=== All $((AMD64_COUNT + ARM64_COUNT)) launches submitted ==="
echo "Each runner: ~30s cold start + ~26-30 min tests + ~30s self-terminate"
echo
echo "Watch progress:"
echo "  gh run view --repo $GH_REPO $RUN_ID"
echo "  gh api repos/$GH_REPO/actions/runs/$RUN_ID/jobs --jq '.jobs[] | \"\(.name): \(.status) \(.conclusion // \"\")\"' "
echo "  oci compute instance list -c $COMP --query 'data[?\"lifecycle-state\"!=\`TERMINATED\`].{name:\"display-name\", state:\"lifecycle-state\"}' --output table"

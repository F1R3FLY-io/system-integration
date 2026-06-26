#!/usr/bin/env bash
# Safety net for ephemeral runners that didn't self-terminate.
#
# Ephemeral runners are supposed to self-terminate after their job (and, with
# the idle watchdog in cloud-init-runner.yml.tmpl, after a no-job timeout). This
# reaper catches anything that slips through: a cloud-init failure, a hung job,
# or a watchdog that never ran. Run it on a schedule (see reap-runners.yml).
#
# Thresholds are deliberately generous so a live run is never reaped — the heavy
# pipeline is ~45 min, so a VM older than MAX_AGE_HOURS cannot be a live job.
#
#   1. OCI : terminate ci-runner-compartment instances older than MAX_AGE_HOURS.
#   2. GitHub: deregister offline ci-eph-* runners (dead VMs). Their VMs have
#      already been terminated by step 1 here or on a prior run, so they show
#      offline; this supersedes the old cleanup-orphan-runners.sh.
#
# Requires: oci CLI authenticated, gh authenticated (GH_TOKEN with repo-admin),
# and state.env alongside this script.

set -euo pipefail

MAX_AGE_HOURS="${MAX_AGE_HOURS:-6}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/state.env"
export SUPPRESS_LABEL_WARNING=True

# ISO8601 cutoff; OCI time-created sorts lexicographically by this, so a string
# comparison in JMESPath is a valid age filter. GNU date first, BSD/macOS fallback.
CUTOFF_ISO="$(date -u -d "${MAX_AGE_HOURS} hours ago" +%Y-%m-%dT%H:%M:%S 2>/dev/null \
  || date -u -v-"${MAX_AGE_HOURS}"H +%Y-%m-%dT%H:%M:%S)"

echo "=== 1. Terminating ci-runner VMs created before ${CUTOFF_ISO} (>${MAX_AGE_HOURS}h old) ==="
IDS="$(oci compute instance list -c "$COMP" --all \
  --query "data[?\"lifecycle-state\"=='RUNNING' && \"time-created\" < '${CUTOFF_ISO}'].id" \
  --raw-output 2>/dev/null | tr -d '[]," ' | grep -v '^$' || true)"

if [ -z "$IDS" ]; then
  echo "  No OCI instances older than ${MAX_AGE_HOURS}h."
else
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    echo "  terminating $id"
    oci compute instance terminate --instance-id "$id" --force --preserve-boot-volume false >/dev/null 2>&1 &
  done <<< "$IDS"
  wait
  echo "  Terminate calls submitted."
fi

echo "=== 2. Deregistering offline ci-eph-* GitHub runners ==="
ORPHANS="$(gh api "repos/$GH_REPO/actions/runners" --paginate \
  --jq '.runners[] | select(.status == "offline" and (.name | startswith("ci-eph"))) | "\(.id) \(.name)"' \
  2>/dev/null || true)"

if [ -z "$ORPHANS" ]; then
  echo "  No offline ci-eph runners."
else
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    id="${line%% *}"
    name="${line#* }"
    echo "  deregistering $name (id=$id)"
    gh api --silent -X DELETE "repos/$GH_REPO/actions/runners/$id" || echo "    (delete failed)"
  done <<< "$ORPHANS"
fi

echo "Reap complete."

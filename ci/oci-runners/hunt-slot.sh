#!/usr/bin/env bash
# One flake-hunt slot: loop the ucc suite until a failure is caught or the
# iteration budget is spent. Runs on a hunt VM as the runner user; started by
# hunt-run.sh from the system-integration checkout at ~/system-integration.
#
# Args / env:
#   $1     slot index on this VM (0-based); selects a disjoint port range via
#          F1R3FLY_PORT_WORKER=gw$1. Passed as an argument (not env) so the
#          slot number is visible in the process command line — hunt-run.sh's
#          already-running guard pgreps for "hunt-slot.sh <slot>".
#   IMAGE  node image to test against
#   ITER   iteration budget; 0 = loop until caught or killed
#   SLOTS_TOTAL  concurrent slots on this VM; divides the host's RSS budget
#
# A failing iteration leaves its shard up (--keep-on-failure), writes
# ~/hunt-logs/slot$SLOT.caught, and stops the slot so the shard can be
# inspected live.

set -uo pipefail

SLOT="${1:?slot index argument is required}"
IMAGE="${IMAGE:?IMAGE is required}"
ITER="${ITER:-0}"
SLOTS_TOTAL="${SLOTS_TOTAL:-1}"
PROVIDER="${PROVIDER:-docker}"
# ci = main leg then ucc leg per iteration; main = main leg only; ucc = ucc only
SUITE="${SUITE:-ci}"

# CI-simulation slots pass no explicit RSS ceiling — CI runs on the harness
# default, so the simulation does too.
HOST_MEM_MB=$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo)

cd "$HOME/system-integration"
mkdir -p "$HOME/hunt-logs"
echo "slot $SLOT: host ${HOST_MEM_MB}MB / $SLOTS_TOTAL slot(s), provider=$PROVIDER"

# Stagger slot starts so simultaneous compose bring-ups don't race the docker
# daemon (observed as "No such container" during create).
sleep $((SLOT * 30))

i=0
consecutive_infra=0
while [ "$ITER" -eq 0 ] || [ "$i" -lt "$ITER" ]; do
  i=$((i + 1))
  LOG="$HOME/hunt-logs/slot$SLOT-iter$i.log"
  # Full CI simulation, faithful to the arm64 pipeline legs: leg 1 is the
  # main integration leg exactly as _integration-pipeline.yml runs it on
  # arm64-docker (workers 8, timeout 1200, scale 1.5, capability args, ucc
  # deselected into its own leg); leg 2 is the ucc leg exactly as CI runs
  # it (no xdist, no scale, timeout 1200). The one deviation is
  # -x --keep-on-failure in place of CI's maxfail=10, so the first failing
  # shard is preserved live for forensics. No --rss-ceiling-mb: CI runs on
  # the harness default, so the simulation does too.
  rc=0
  # EXTRA_DESELECTS: space-separated test paths deferred by the base
  # branch's pipeline beyond the defaults below (empty = stack-shaped leg).
  extra_deselect_args=()
  for t in ${EXTRA_DESELECTS:-}; do extra_deselect_args+=(--deselect "$t"); done
  if [ "$SUITE" != "ucc" ]; then
    F1R3FLY_NODE_IMAGE="$IMAGE" F1R3FLY_PORT_WORKER="gw$SLOT" \
      poetry run pytest \
      integration-tests/test/tests/shared/ \
      integration-tests/test/tests/custom/ \
      integration-tests/test/tests/standalone/ \
      --deselect integration-tests/test/tests/custom/test_load.py \
      --deselect integration-tests/test/tests/custom/test_shard_degradation.py \
      --deselect integration-tests/test/tests/custom/test_deploy_carrier_starvation.py \
      --deselect integration-tests/test/tests/custom/test_user_contract_concurrency.py \
      "${extra_deselect_args[@]}" \
      --provider="$PROVIDER" \
      --node-capability=concurrent-bridge-lock-accounting \
      --node-capability=deploy-play-budget \
      --node-capability=duplicate-signed-deploy-race \
      --node-capability=expired-deploy-admission \
      --node-capability=finality-stall-recovery \
      --node-capability=observer-exploratory-backpressure \
      --node-capability=observer-missing-block-retry \
      --node-capability=readonly-observer-api-catchup \
      --node-capability=slow-peer-notification-quorum \
      --node-capability=transient-peer-liveness \
      -x --keep-on-failure -v --tb=short --instafail \
      -n 8 --dist=loadgroup --timeout=1200 --timeout-scale=1.5 \
      > "$LOG" 2>&1
    rc=$?
  fi
  if [ "$rc" -eq 0 ] && [ "$SUITE" != "main" ]; then
    F1R3FLY_NODE_IMAGE="$IMAGE" F1R3FLY_PORT_WORKER="gw$SLOT" \
      poetry run pytest \
      integration-tests/test/tests/custom/test_user_contract_concurrency.py \
      --provider="$PROVIDER" \
      -x --keep-on-failure -v --tb=short --instafail --timeout=1200 \
      >> "$LOG" 2>&1
    rc=$?
  fi
  echo "$(date -Iseconds) slot=$SLOT iter=$i rc=$rc" >> "$HOME/hunt-logs/slot$SLOT.status"
  if [ "$rc" -eq 0 ]; then
    consecutive_infra=0
    continue
  fi
  # A real specimen is a test-assertion failure: pytest's short summary lists
  # it as "FAILED <nodeid>". Fixture/collection/daemon problems surface as
  # "ERROR <nodeid>" (or nothing) — infra, not the flake.
  if grep -q '^FAILED ' "$LOG"; then
    echo "$(date -Iseconds) iter=$i rc=$rc log=slot$SLOT-iter$i.log" \
      > "$HOME/hunt-logs/slot$SLOT.caught"
    break
  fi
  consecutive_infra=$((consecutive_infra + 1))
  echo "$(date -Iseconds) iter=$i rc=$rc infra consecutive=$consecutive_infra" \
    >> "$HOME/hunt-logs/slot$SLOT.status"
  # Remove this iteration's own leftovers (containers/volumes/network of the
  # session named in the log) so the retry starts clean without touching the
  # other slots' shards.
  SESSION=$(grep -om1 'rnode\.test\.[0-9a-f]*' "$LOG" | cut -d. -f3)
  if [ -n "$SESSION" ]; then
    docker ps -aq --filter "name=rnode.test.$SESSION" | xargs -r docker rm -f
    docker volume ls -q --filter "name=test-$SESSION" | xargs -r docker volume rm -f 2>/dev/null
    docker network ls -q --filter "name=test-$SESSION" | xargs -r docker network rm 2>/dev/null
  fi
  if [ "$consecutive_infra" -ge 5 ]; then
    echo "$(date -Iseconds) iter=$i rc=$rc" > "$HOME/hunt-logs/slot$SLOT.infra-dead"
    break
  fi
  sleep $((15 + SLOT * 15))
done

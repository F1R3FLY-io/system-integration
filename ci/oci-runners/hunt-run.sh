#!/usr/bin/env bash
# Drive the flake hunt across launched VMs from the operator machine.
#
# Usage:
#   ./hunt-run.sh --ips "1.2.3.4 5.6.7.8" [options]
#
# Options:
#   --ips "<ip> [<ip> ...]"   hunt VM public IPs (required)
#   --slots N                 concurrent slots per VM            (default 1)
#   --iterations M            iteration budget per slot, 0=inf   (default 0)
#   --image IMG               node image under test              (default f1r3flyindustries/f1r3fly-rust:dev)
#   --image-tar PATH          local docker-save tarball (.tar.gz) rsynced to each
#                             VM and docker-loaded during setup — the delivery
#                             mode for a locally-built image whose branch is not
#                             pushed (pair with --image <its tag>)
#   --ref SHA                 system-integration ref to test     (default: HEAD of this checkout)
#   --results DIR             local dir for pulled forensics     (default ./hunt-results)
#   --rearm                   clear .caught / .infra-dead markers before starting,
#                             so halted slots resume hunting
#   --setup-only              prepare VMs, start no slots
#   --status                  print one tally snapshot and exit
#
# Phases:
#   1. setup   — per VM: clone/fetch system-integration at --ref, poetry
#                install, docker pull the image (parallel across VMs)
#   2. slots   — per VM: start N detached hunt-slot.sh loops
#   3. monitor — poll slot status; on a catch, pull the pytest log, the node
#                file-sink logs (integration-tests/data), and log-archive
#                into --results/<ip>-slot<k>/ and keep monitoring the rest
#
# A caught slot leaves its failing shard RUNNING on the VM (--keep-on-failure)
# for live inspection: ssh in and use `docker ps`, the readonly node's HTTP
# API, or `pytest --skip-setup --session-id <id>`. Mind the CI reaper's 6h
# instance horizon.
#
# Ctrl-C detaches the monitor only; slots keep hunting. Stop everything with:
#   ./hunt-run.sh --ips "..." --stop

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/state.env"

# Operator-checkout files under integration-tests/test/infra that must ride
# over the fetched ref while unpushed. Keep in sync with the chown list below.
OVERLAY_INFRA=(log_events.py assertions.py node.py run_outcome.py resource_monitor.py)
# Test modules (paths under integration-tests/test/tests/) that must ride over
# the fetched ref, same rationale as OVERLAY_INFRA.
OVERLAY_TESTS=(
  custom/test_user_contract_concurrency.py
  custom/test_joiner_self_proposes_at_epoch_boundary.py
  custom/test_deploy_carrier_starvation.py
)

IPS=""
SLOTS=1
ITERATIONS=0
IMAGE="f1r3flyindustries/f1r3fly-rust:dev"
IMAGE_TAR=""
REF="$(git -C "$SCRIPT_DIR" rev-parse HEAD)"
RESULTS="./hunt-results"
MODE="run"
REARM=0
PROVIDER="docker"
# ci = main leg then ucc leg per iteration; main = main leg only; ucc = ucc only
SUITE="ci"
# Space-separated extra --deselect targets appended to the main leg, for
# aligning the gate with a base branch whose pipeline defers more tests
# than the stack-shaped default (e.g. origin/dev).
EXTRA_DESELECTS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ips) IPS="$2"; shift 2 ;;
    --slots) SLOTS="$2"; shift 2 ;;
    --iterations) ITERATIONS="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --image-tar) IMAGE_TAR="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --results) RESULTS="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --suite) SUITE="$2"; shift 2 ;;
    --extra-deselects) EXTRA_DESELECTS="$2"; shift 2 ;;
    --rearm) REARM=1; shift ;;
    --setup-only) MODE="setup"; shift ;;
    --status) MODE="status"; shift ;;
    --stop) MODE="stop"; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

[[ -n "$IPS" ]] || { echo "ERROR: --ips is required" >&2; exit 1; }

SSH_KEY="${SSH_KEY_PRIV/#\~/$HOME}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -i "$SSH_KEY")
run_as_runner() { # $1=ip, stdin=script
  ssh "${SSH_OPTS[@]}" "ubuntu@$1" "sudo -H -u runner bash -s"
}

if [[ "$MODE" == "stop" ]]; then
  for ip in $IPS; do
    echo "=== $ip: stopping slots + shards ==="
    run_as_runner "$ip" <<'STOP' || true
pkill -f hunt-slot.sh || true
pkill -f "pytest integration-tests" || true
docker ps -aq --filter "name=rnode.test." | xargs -r docker rm -f
STOP
  done
  exit 0
fi

if [[ "$MODE" == "status" ]]; then
  for ip in $IPS; do
    echo "=== $ip ==="
    run_as_runner "$ip" <<'STATUS' || echo "  (unreachable)"
cd "$HOME/hunt-logs" 2>/dev/null || { echo "  (no hunt-logs yet)"; exit 0; }
for s in *.status; do
  [ -e "$s" ] || continue
  echo "  ${s%.status}: $(wc -l < "$s") iterations, last: $(tail -1 "$s")"
done
ls *.caught 2>/dev/null | sed 's/^/  CAUGHT: /' || true
STATUS
  done
  exit 0
fi

# ── Phase 1: setup ───────────────────────────────────────────────────────────
echo "=== Setup: ref=$REF image=$IMAGE ==="
pids=()
for ip in $IPS; do
  (
    if [[ -n "$IMAGE_TAR" ]]; then
      # The tarball is read by the runner user, and -a preserves the source
      # mode — a 0600 operator-side file would arrive unreadable.
      chmod 644 "$IMAGE_TAR"
      rsync -az -e "ssh ${SSH_OPTS[*]}" \
        "$IMAGE_TAR" "ubuntu@$ip:/tmp/hunt-image.tar.gz"
    fi
    run_as_runner "$ip" <<SETUP
set -euo pipefail
export PATH="\$HOME/.local/bin:\$PATH"
cd "\$HOME"
if [ ! -d system-integration/.git ]; then
  git clone https://github.com/F1R3FLY-io/system-integration.git
fi
cd system-integration
git fetch origin "$REF"
git checkout -q "$REF"
poetry lock --no-update
poetry install --with integration --no-interaction -q
# A transferred tarball loads first; then a registry image is pulled; a
# locally-built tag (e.g. from a PR-branch build on the VM) just has to exist.
if [ -f /tmp/hunt-image.tar.gz ]; then
  gunzip -c /tmp/hunt-image.tar.gz | docker load
  # Best-effort: the tarball arrives owned by ubuntu and /tmp is sticky, so
  # the runner user may not be able to remove it. Harmless on an ephemeral VM.
  rm -f /tmp/hunt-image.tar.gz 2>/dev/null || true
fi
docker pull -q "$IMAGE" 2>/dev/null || docker image inspect "$IMAGE" >/dev/null
mkdir -p "\$HOME/hunt-logs"
echo "setup done: \$(git rev-parse --short HEAD)"
SETUP
    # Overlay files that may differ from the fetched ref (pre-push iteration):
    # the slot loop itself and the conftest port-range override ride the
    # operator checkout, not origin.
    rsync -az --rsync-path="sudo rsync" -e "ssh ${SSH_OPTS[*]}" \
      "$SCRIPT_DIR/hunt-slot.sh" \
      "ubuntu@$ip:/home/runner/system-integration/ci/oci-runners/hunt-slot.sh"
    rsync -az --rsync-path="sudo rsync" -e "ssh ${SSH_OPTS[*]}" \
      "$SCRIPT_DIR/../../integration-tests/test/conftest.py" \
      "ubuntu@$ip:/home/runner/system-integration/integration-tests/test/conftest.py"
    rsync -az --rsync-path="sudo rsync" -e "ssh ${SSH_OPTS[*]}" \
      "$SCRIPT_DIR/../../conf/rust.conf" \
      "ubuntu@$ip:/home/runner/system-integration/conf/rust.conf"
    rsync -az --rsync-path="sudo rsync" -e "ssh ${SSH_OPTS[*]}" \
      "$SCRIPT_DIR/../../integration-tests/test/infra/compose.py" \
      "ubuntu@$ip:/home/runner/system-integration/integration-tests/test/infra/compose.py"
    # The docker provider resolves ROTATED node logs. Without it the classifier
    # reads a capped `docker logs` tail instead, so a caught specimen is judged
    # on a truncated log and the forbidden-log gate silently sees less than the
    # run produced. It lives under infra/providers/, so OVERLAY_INFRA's flat
    # glob does not reach it.
    rsync -az --rsync-path="sudo rsync" -e "ssh ${SSH_OPTS[*]}" \
      "$SCRIPT_DIR/../../integration-tests/test/infra/providers/docker.py" \
      "ubuntu@$ip:/home/runner/system-integration/integration-tests/test/infra/providers/docker.py"
    # The node's defaults.conf, from the operator's node checkout (the branch
    # under test). CI provides it from the runner's node checkout; hunt VMs
    # clone only system-integration, and the harness's NodeConf resolver
    # errors at fixture setup without it. mkdir first — the operator's rsync
    # predates --mkpath.
    run_as_runner "$ip" <<'MKDEFAULTS'
mkdir -p "$HOME/system-integration/services/f1r3node-rust/node/src/main/resources"
MKDEFAULTS
    rsync -az --rsync-path="sudo rsync" -e "ssh ${SSH_OPTS[*]}" \
      "$SCRIPT_DIR/../../services/f1r3node-rust/node/src/main/resources/defaults.conf" \
      "ubuntu@$ip:/home/runner/system-integration/services/f1r3node-rust/node/src/main/resources/defaults.conf"
    # The deploy-loss classifier and its plumbing. Without these a caught
    # specimen reports only "did not finalize", and telling refund-quarantine
    # from gate starvation from merge starvation means hand-scanning shard
    # logs on the VM — the thing the classifier exists to avoid.
    for f in "${OVERLAY_INFRA[@]}"; do
      rsync -az --rsync-path="sudo rsync" -e "ssh ${SSH_OPTS[*]}" \
        "$SCRIPT_DIR/../../integration-tests/test/infra/$f" \
        "ubuntu@$ip:/home/runner/system-integration/integration-tests/test/infra/$f"
    done
    # The suite under test itself. Without this the VM runs the --ref copy while
    # the overlaid infra is the operator's, so a policy change in the test (what
    # fails vs what is only recorded) silently does not apply to the hunt.
    for f in "${OVERLAY_TESTS[@]}"; do
      rsync -az --rsync-path="sudo rsync" -e "ssh ${SSH_OPTS[*]}" \
        "$SCRIPT_DIR/../../integration-tests/test/tests/$f" \
        "ubuntu@$ip:/home/runner/system-integration/integration-tests/test/tests/$f"
    done
    run_as_runner "$ip" <<'CHOWN'
sudo chown runner:runner \
  "$HOME/system-integration/ci/oci-runners/hunt-slot.sh" \
  "$HOME/system-integration/integration-tests/test/conftest.py" \
  "$HOME/system-integration/conf/rust.conf" \
  "$HOME/system-integration/integration-tests/test/infra/compose.py"
# Overlaid infra modules arrive owned by the operator's uid; chown by glob so
# the list here cannot drift out of sync with OVERLAY_INFRA.
sudo chown runner:runner "$HOME"/system-integration/integration-tests/test/infra/*.py
sudo chown runner:runner "$HOME"/system-integration/integration-tests/test/infra/providers/*.py
sudo chown runner:runner "$HOME"/system-integration/integration-tests/test/tests/custom/*.py
CHOWN
    echo "  [$ip] setup complete"
  ) &
  pids+=($!)
done
for pid in "${pids[@]}"; do wait "$pid"; done

[[ "$MODE" == "setup" ]] && exit 0

# ── Phase 2: start slots ─────────────────────────────────────────────────────
if [[ "$REARM" == "1" ]]; then
  echo "=== Rearming: clearing catch markers ==="
  for ip in $IPS; do
    run_as_runner "$ip" <<'REARM_EOF'
rm -f "$HOME/hunt-logs"/slot*.caught "$HOME/hunt-logs"/slot*.infra-dead
REARM_EOF
  done
fi

echo "=== Starting $SLOTS slot(s) per VM ==="
for ip in $IPS; do
  for slot in $(seq 0 $((SLOTS - 1))); do
    run_as_runner "$ip" <<START
if [ -e "\$HOME/hunt-logs/slot$slot.caught" ]; then
  echo "  [$ip] slot $slot caught; skipping (clear the marker to rearm)"
elif pgrep -f "hunt-slot\.sh $slot\$" >/dev/null 2>&1; then
  echo "  [$ip] slot $slot already running; skipping"
else
  nohup env IMAGE="$IMAGE" ITER=$ITERATIONS SLOTS_TOTAL=$SLOTS PROVIDER="$PROVIDER" SUITE="$SUITE" EXTRA_DESELECTS="$EXTRA_DESELECTS" PATH="\$HOME/.local/bin:\$PATH" \
    bash "\$HOME/system-integration/ci/oci-runners/hunt-slot.sh" $slot \
    > "\$HOME/hunt-logs/slot$slot.nohup" 2>&1 &
  echo "  [$ip] slot $slot started (pid \$!)"
fi
START
  done
done

# ── Phase 3: monitor ─────────────────────────────────────────────────────────
mkdir -p "$RESULTS"
echo "=== Monitoring (Ctrl-C detaches; slots keep hunting) ==="
declare -A COLLECTED
while :; do
  total=0 caught=0
  for ip in $IPS; do
    out=$(run_as_runner "$ip" <<'POLL' 2>/dev/null || true
cd "$HOME/hunt-logs" 2>/dev/null || exit 0
iters=$(cat ./*.status 2>/dev/null | wc -l)
echo "iters=$iters"
for c in *.caught; do [ -e "$c" ] && echo "caught=${c%.caught} $(cat "$c")"; done
POLL
)
    n=$(sed -n 's/^iters=//p' <<<"$out")
    total=$(( total + ${n:-0} ))
    while IFS= read -r line; do
      [[ "$line" == caught=* ]] || continue
      caught=$((caught + 1))
      slot_name="${line#caught=}"; slot_name="${slot_name%% *}"
      key="$ip-$slot_name"
      if [[ -z "${COLLECTED[$key]:-}" ]]; then
        COLLECTED[$key]=1
        dest="$RESULTS/$key"
        mkdir -p "$dest"
        echo ">>> CATCH on $ip $slot_name — pulling forensics to $dest"
        rsync -az --rsync-path="sudo rsync" -e "ssh ${SSH_OPTS[*]}" \
          "ubuntu@$ip:/home/runner/hunt-logs/" "$dest/hunt-logs/" || true
        # Subprocess-provider layout (host data dirs). Absent under the
        # docker provider — rsync then no-ops.
        rsync -az --rsync-path="sudo rsync" -e "ssh ${SSH_OPTS[*]}" \
          "ubuntu@$ip:/home/runner/system-integration/integration-tests/data/" \
          "$dest/data/" || true
        rsync -az --rsync-path="sudo rsync" -e "ssh ${SSH_OPTS[*]}" \
          "ubuntu@$ip:/home/runner/system-integration/integration-tests/log-archive/" \
          "$dest/log-archive/" || true
        # Docker-provider layout: each node keeps its file-sink log inside
        # the container at <data_dir>/logs/ (immune to docker-daemon log
        # rotation, complete from genesis). Snapshot the caught session's
        # containers and pull the copies.
        latest_log=$(ls "$dest/hunt-logs/$slot_name"-iter*.log 2>/dev/null | sort -V | tail -1)
        session=""
        [[ -n "$latest_log" ]] && session=$(sed -n 's/.*Session \([0-9a-f]\{8\}\).*/\1/p' "$latest_log" | head -1)
        if [[ -n "$session" ]]; then
          run_as_runner "$ip" <<NODELOGS || true
mkdir -p "/tmp/specimen-$session"
for c in \$(docker ps -a --format '{{.Names}}' | grep "$session"); do
  docker cp "\$c:/var/lib/rnode/logs/." "/tmp/specimen-$session/\$c/" 2>/dev/null || true
done
gzip -rf "/tmp/specimen-$session" || true
NODELOGS
          rsync -az -e "ssh ${SSH_OPTS[*]}" \
            "ubuntu@$ip:/tmp/specimen-$session/" "$dest/specimen-node-logs/" || true
        fi
        echo ">>> live shard preserved on $ip — ssh -i $SSH_KEY ubuntu@$ip"
      fi
    done <<<"$out"
  done
  echo "$(date +%H:%M:%S) total_iterations=$total catches=$caught"
  sleep 60
done

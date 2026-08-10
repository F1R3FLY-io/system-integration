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
#      Override the whole set with RUNNER_LABELS to get an exclusive runner:
#        RUNNER_LABELS="self-hosted,linux,x64,f1r3fly-rust-soak,oracle-cloud" \
#          ./launch-runner.sh amd64
#      Swap only the pool label; the rest are facts about the VM and are enforced.
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

# RUNNER_MEM_GB_OVERRIDE replaces the arch default from state.env for this
# launch only. This exists for the merge-recovery soak, whose 6-node shard
# needs ~26GB (19-20GB node RSS + 6-7GB host overhead) and dies on the host
# free-floor guard on the 32GB default (run 31390673884) — while a global
# AMD64_MEM_GB raise was rejected: every PR CI launch would pay for headroom
# only the soak uses. Launch-time only; bake-image.sh keeps the defaults.
if [[ -n "${RUNNER_MEM_GB_OVERRIDE+x}" ]]; then
  # Same `+x` + explicit-validation pattern as RUNNER_LABELS below: fail
  # closed on a blank or malformed value rather than handing OCI a
  # shape-config it rejects 90 seconds into the launch.
  if [[ ! "$RUNNER_MEM_GB_OVERRIDE" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: RUNNER_MEM_GB_OVERRIDE must be a positive integer (GB), got: '$RUNNER_MEM_GB_OVERRIDE'" >&2
    exit 1
  fi
  MEM_GB="$RUNNER_MEM_GB_OVERRIDE"
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

# Labels: distinct -ephemeral suffix so workflows opt in explicitly.
#
# RUNNER_LABELS overrides the whole set. This exists so a workflow that needs an
# *exclusive* runner can register the VM under a label nothing else requests,
# rather than registering it as shared CI capacity and relabelling afterwards.
# Relabelling leaves a real window: between the runner registering and the label
# removal landing, any queued job matching the shared label can claim the VM.
# That is how the merge-recovery soak lost run 30606130771 -- it ran on a VM a PR
# CI run had launched, and died when that run finished with the runner.
#
# The scheduling loss is the visible half. The quieter half: the soak then runs
# on whatever cloud-init the OTHER workflow pinned, so SYSTEM_INTEGRATION_REF
# stops describing the machine the soak actually runs on. Launching with the
# dedicated label from the start is what makes that pin binding.
DEFAULT_LABELS="self-hosted,linux,$LABEL_ARCH,f1r3fly-rust-ci-ephemeral,oracle-cloud"
if [[ -n "${RUNNER_LABELS+x}" ]]; then
  # Note the `+x` test above and the explicit blank check here: `${VAR:-default}`
  # does NOT substitute for a whitespace-only value, so the shorthand would hand
  # cloud-init an empty label set. GitHub accepts that registration, and the
  # result is a VM no `runs-on` can ever match -- a silent paid-for no-op.
  if [[ -z "${RUNNER_LABELS//[[:space:]]/}" ]]; then
    echo "ERROR: RUNNER_LABELS is set but empty/whitespace-only. Unset it to use the default:" >&2
    echo "       $DEFAULT_LABELS" >&2
    exit 1
  fi
  LABELS="$RUNNER_LABELS"

  # An override supplies the COMPLETE set, so a caller can omit a label the
  # consuming `runs-on` requires. Fail here, loudly, rather than 15 minutes later
  # with a healthy VM idling next to a job that will never be routed to it.
  # claude-session-9f68c6fa caught an earlier draft whose own header example
  # dropped `oracle-cloud`, which every `runs-on` in f1r3node-rust asks for:
  # copying the documented example would have produced a runner nothing matched,
  # with validation passing.
  #
  # Of the four, only `oracle-cloud` is ours. `gh api .../actions/runners` shows
  # every runner carrying self-hosted(read-only), Linux(read-only),
  # X64(read-only), oracle-cloud(custom), <pool>(custom) -- the `linux` and `x64`
  # passed to config.sh never become custom labels, because GitHub absorbs them
  # into its own auto-assigned read-only set.
  #
  # This is absorbed SILENTLY -- nothing warns about it. (An earlier version of
  # this comment blamed SUPPRESS_LABEL_WARNING above; that is wrong.
  # SUPPRESS_LABEL_WARNING is an OCI CLI variable, exported in the *launcher's*
  # environment on the GitHub-hosted runner. config.sh runs on the OCI VM, a
  # different process on a different machine, and never sees it -- it does not
  # appear in cloud-init-runner.yml.tmpl at all. claude-session-9f68c6fa checked
  # 2410 lines of console history: config.sh prints nothing between the
  # --labels invocation and "Runner successfully added". So there is no muted
  # warning to restore, and dropping these labels will not surface one.)
  #
  # So dropping self-hosted/linux/<arch> does NOT actually break routing; the
  # agent assigns them from the OS and CPU it detects. Dropping `oracle-cloud`
  # does, because nothing else supplies it. All four stay required -- a caller
  # who omits self-hosted has misunderstood something worth stopping on -- but
  # the messages must not claim a consequence that is not real. A right check
  # with a wrong reason is how the next person talks themselves into deleting it.
  REQUIRED_LABELS=("self-hosted" "linux" "$LABEL_ARCH" "oracle-cloud")
  for required in "${REQUIRED_LABELS[@]}"; do
    if [[ ",$LABELS," != *",$required,"* ]]; then
      echo "ERROR: RUNNER_LABELS ('$LABELS') is missing the required label '$required'." >&2
      if [[ "$required" == "oracle-cloud" ]]; then
        echo "       Nothing else supplies it, so no 'runs-on' asking for it can" >&2
        echo "       match this runner -- the job queues until it times out." >&2
      else
        echo "       GitHub assigns this one automatically, so routing would still" >&2
        echo "       work; it is required to keep the set honest about what the VM" >&2
        echo "       is. If you meant to drop it, you probably meant something else." >&2
      fi
      exit 1
    fi
  done

  # Presence of the invariants is not the same as exclusivity, and exclusivity is
  # the entire point of this override. Two ways to satisfy the check above and
  # still get a shared runner:
  #
  #   1. Keep the shared pool label alongside the exclusive one. The VM is then
  #      claimable by ordinary CI exactly as before -- the race is reopened while
  #      the config reads as if it were closed.
  #   2. Supply only the invariants and no pool label at all, which matches every
  #      `runs-on` that does not name a pool.
  #
  # The shared label is derived from DEFAULT_LABELS rather than hard-coded, so
  # this guard follows if the default pool is ever renamed.
  SHARED_POOL_LABEL=""
  IFS=',' read -r -a _default_parts <<< "$DEFAULT_LABELS"
  for _lbl in "${_default_parts[@]}"; do
    _is_invariant=0
    for required in "${REQUIRED_LABELS[@]}"; do
      [[ "$_lbl" == "$required" ]] && { _is_invariant=1; break; }
    done
    [[ "$_is_invariant" -eq 0 ]] && SHARED_POOL_LABEL="$_lbl"
  done

  if [[ -n "$SHARED_POOL_LABEL" && ",$LABELS," == *",$SHARED_POOL_LABEL,"* ]]; then
    echo "ERROR: RUNNER_LABELS ('$LABELS') still carries the shared pool label" >&2
    echo "       '$SHARED_POOL_LABEL'. Any queued CI job can then claim this VM," >&2
    echo "       which is the race the override exists to close. Drop it." >&2
    exit 1
  fi

  _pool_count=0
  IFS=',' read -r -a _parts <<< "$LABELS"
  for _lbl in "${_parts[@]}"; do
    _is_invariant=0
    for required in "${REQUIRED_LABELS[@]}"; do
      [[ "$_lbl" == "$required" ]] && { _is_invariant=1; break; }
    done
    [[ "$_is_invariant" -eq 0 ]] && _pool_count=$((_pool_count + 1))
  done

  if [[ "$_pool_count" -eq 0 ]]; then
    echo "ERROR: RUNNER_LABELS ('$LABELS') has no pool label -- only the labels" >&2
    echo "       every runner carries. This runner would match any 'runs-on'" >&2
    echo "       that does not name a pool, so it is not exclusive. Add one." >&2
    exit 1
  fi
else
  LABELS="$DEFAULT_LABELS"
fi

# Render cloud-init from template
CLOUD_INIT_TMPL="$SCRIPT_DIR/cloud-init-runner.yml.tmpl"
CLOUD_INIT_RENDERED=$(mktemp -t oci-cloud-init.XXXXXX.yml)
LAUNCH_ERR=$(mktemp -t oci-launch-err.XXXXXX)
trap 'rm -f "$CLOUD_INIT_RENDERED" "$LAUNCH_ERR"' EXIT

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
echo "  Memory:      ${MEM_GB} GB${RUNNER_MEM_GB_OVERRIDE:+ (via RUNNER_MEM_GB_OVERRIDE)}"
echo "  Image:       $IMAGE_OCID"
echo "  Labels:      $LABELS"

# Retry transient OCI errors (out-of-capacity, throttling, 5xx). Non-transient
# failures (auth, bad image, missing subnet) exit immediately so a doomed
# launch doesn't burn the runner registration token. Errors are captured to
# $LAUNCH_ERR so the actual OCI message is surfaced — the previous form
# (`2>&1 | tail -1`) swallowed everything but the last line.
INSTANCE_OCID=""
MAX_ATTEMPTS=3
for attempt in $(seq 1 $MAX_ATTEMPTS); do
  if INSTANCE_OCID=$(oci compute instance launch \
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
      --query 'data.id' --raw-output 2>"$LAUNCH_ERR"); then
    break
  fi

  echo "  [$RUNNER_NAME] attempt $attempt/$MAX_ATTEMPTS failed:" >&2
  sed 's/^/    /' "$LAUNCH_ERR" >&2

  if (( attempt < MAX_ATTEMPTS )) \
    && grep -qE 'Out of host capacity|InternalError|TooManyRequests|429|503|Throttle|connection reset|timed out' "$LAUNCH_ERR"; then
    sleep_s=$(( attempt * 10 ))
    echo "  [$RUNNER_NAME] transient OCI error; retrying in ${sleep_s}s" >&2
    sleep "$sleep_s"
    continue
  fi

  echo "  [$RUNNER_NAME] OCI launch failed permanently after $attempt attempt(s); giving up" >&2
  exit 1
done

echo "  Instance:    $INSTANCE_OCID"

# When set, append this launch's runner name for post-launch verification
# (CI watchdogs poll GitHub's runner API for these names to detect batches
# that boot but never register — see 2026-07-07 forced-update incident).
if [[ -n "${RUNNER_NAMES_FILE:-}" ]]; then
  echo "$RUNNER_NAME" >> "$RUNNER_NAMES_FILE"
fi
echo
echo "Cloud-init runs ~3–5 min before the runner registers on GitHub."
echo
echo "Inspect after launch:"
echo "  oci compute instance get --instance-id $INSTANCE_OCID --query 'data.{state:\"lifecycle-state\",ip:\"public-ip\"}'"
echo "  gh api repos/$GH_REPO/actions/runners --jq '.runners[] | select(.name==\"$RUNNER_NAME\")'"
echo
echo "SSH (for debugging the bootstrap):"
echo "  ssh -i $SSH_KEY_PRIV ubuntu@<public-ip> sudo tail -f /var/log/runner-bootstrap.log"

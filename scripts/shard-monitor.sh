#!/usr/bin/env bash
# Monitors a shard whose nodes run on separate hosts.
#
# Two scopes, because the nodes no longer share a host:
#   host checks  (always)  this host's container, CPU/log wedge, restarts,
#                          disk, host memory, container anon memory
#   chain checks (--chain) the whole shard over HTTP: finalized height, per-node
#                          lag, tip-vs-floor, production, network id, the
#                          in-flight set, and whether validation completes
#
# Run --chain on exactly one host, or every host alerts about the same chain.
#
# Modes:
#   (no args)   periodic check: alert on a condition appearing, re-alert with
#               exponential backoff while it persists, once on recovery
#   --nightly   digest posted regardless of state, so silence is never
#               ambiguous between "healthy" and "the monitor is dead"
#   --dry-run   print messages instead of posting
#
# Usage:
#   SHARD_NODES="boot=10.0.0.47:40403 ..." SHARD_NETWORK_ID=... ./shard-monitor.sh --chain
#
# The webhook is read from a mode-600 file, never embedded: on the previous
# shard it lived in a world-readable script.

set -uo pipefail

SHARD_NAME="${SHARD_NAME:-shard02}"
STATE_DIR="${STATE_DIR:-$HOME/.shard-monitor}"
WEBHOOK_FILE="${WEBHOOK_FILE:-$STATE_DIR/webhook}"
CURL_MAX_TIME="${CURL_MAX_TIME:-20}"

# --- thresholds -------------------------------------------------------------
# A finalized height that has not moved this long is a stalled chain.
LFB_STALL_SECS="${LFB_STALL_SECS:-600}"
# Nothing produced anywhere. Below this every node starts broadcasting
# fork-choice tip requests.
NO_BLOCK_SECS="${NO_BLOCK_SECS:-600}"
# One node's height is not the chain's; the chain is the max across nodes.
NODE_LAG_BLOCKS="${NODE_LAG_BLOCKS:-100}"
# Tip minus finalized, per node, from that SAME node. Production continuing
# while the floor freezes is the signature of the previous shard's failure and
# nothing watched it.
FINALIZATION_GAP_BLOCKS="${FINALIZATION_GAP_BLOCKS:-200}"
# The in-flight set filling toward its cap, or holding a marker for a long
# time, is the other half of that failure. These gauges did not exist then.
IN_FLIGHT_WARN_PCT="${IN_FLIGHT_WARN_PCT:-50}"
IN_FLIGHT_OLDEST_WARN_SECS="${IN_FLIGHT_OLDEST_WARN_SECS:-600}"
# A node livelocks with a core pinned and no log output while HTTP still
# answers 200 and docker still reports healthy. Either signal alone
# false-positives, so both must hold, on two consecutive checks.
WEDGE_CPU_PCT="${WEDGE_CPU_PCT:-90}"
WEDGE_LOG_WINDOW="${WEDGE_LOG_WINDOW:-5m}"
DISK_WARN_PCT="${DISK_WARN_PCT:-85}"
# Kept only as a floor. mem_available counts reclaimable cache so it barely
# moves as anon crowds cache out: it read HIGHER during the previous
# collapse than when healthy. Container anon is the real discriminator.
MEM_AVAIL_WARN_MB="${MEM_AVAIL_WARN_MB:-2048}"
CONTAINER_ANON_WARN_MB="${CONTAINER_ANON_WARN_MB:-9000}"
# Re-alert backoff: 30m doubling to a 24h cap. A standing failure stays
# visible without pinging the channel all day.
REALERT_BASE_SECS="${REALERT_BASE_SECS:-1800}"
REALERT_MAX_SECS="${REALERT_MAX_SECS:-86400}"

CHAIN=0; NIGHTLY=0; DRY_RUN=0
for a in "$@"; do
  case "$a" in
    --chain)   CHAIN=1 ;;
    --nightly) NIGHTLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
  esac
done

mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR" 2>/dev/null || true
# Suppression state goes on disk, NOT /dev/shm. logind defaults to
# RemoveIPC=yes, which deletes a user's /dev/shm entries when their last
# session ends; cron has no session, so tmpfs state is wiped almost
# immediately and every run looks like a condition first firing. That is the
# alert-every-interval behaviour this backoff exists to prevent. A full root
# disk is covered by the disk_low condition rather than by hiding state
# somewhere unreliable.
SUPPRESS_DIR="$STATE_DIR/cond"
mkdir -p "$SUPPRESS_DIR" 2>/dev/null || SUPPRESS_DIR="$STATE_DIR"

NOW=$(date -u +%s)
NOW_MS=$((NOW * 1000))
FAILED=no
# A real newline, not the two characters backslash-n. post() converts real
# newlines into the JSON escape; a literal backslash-n would be escaped again
# and arrive in Discord as visible text.
NL=$'\n'
DIGEST=""

# --- delivery ---------------------------------------------------------------
post() {
  local msg="$1" url code esc
  if [ "$DRY_RUN" = 1 ]; then printf 'DRY-RUN:\n%b\n---\n' "$msg"; return 0; fi
  undeliverable() {
    printf '%s %s: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" "$msg" \
      | tee -a "$STATE_DIR/post_failures.log" >&2
    return 1
  }
  [ -r "$WEBHOOK_FILE" ] || { undeliverable "no webhook file at $WEBHOOK_FILE"; return 1; }
  url=$(tr -d '[:space:]' < "$WEBHOOK_FILE")
  [ -n "$url" ] || { undeliverable "webhook file is empty"; return 1; }
  esc="${msg//\\/\\\\}"; esc="${esc//\"/\\\"}"; esc="${esc//$'\n'/\\n}"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
    -H 'Content-Type: application/json' -d "{\"content\":\"$esc\"}" "$url")
  case "$code" in 200|204) return 0 ;; esac
  printf '%s post failed http=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "${code:-000}" \
    >> "$STATE_DIR/post_failures.log"
  return 1
}

human() {
  local s=$1
  if   [ "$s" -lt 3600 ];  then printf '%dm' $((s/60))
  elif [ "$s" -lt 86400 ]; then printf '%dh' $((s/3600))
  else printf '%dd %dh' $((s/86400)) $(( (s%86400)/3600 )); fi
}

# Suppression is keyed on the condition NAME, never on the message. A live
# value in a message therefore cannot look like a new condition and reset the
# backoff — which is exactly how the previous monitor came to alert every two
# minutes during a stall.
signal() {
  local key="$1" firing="$2" msg="$3"
  local f="$SUPPRESS_DIR/cond.$key"
  local first last level interval
  if [ "$firing" = yes ]; then
    FAILED=yes
    printf 'FAIL %s: %s\n' "$key" "$msg"
    first=$NOW; last=0; level=0
    [ -f "$f" ] && read -r first last level < "$f"
    interval=$REALERT_BASE_SECS
    local n=0
    while [ "$n" -lt "$level" ] && [ "$interval" -lt "$REALERT_MAX_SECS" ]; do
      interval=$((interval*2)); n=$((n+1))
    done
    [ "$interval" -gt "$REALERT_MAX_SECS" ] && interval=$REALERT_MAX_SECS
    if [ "$last" = 0 ]; then
      post "**$SHARD_NAME** FAIL: $msg" && printf '%s %s %s' "$first" "$NOW" "0" > "$f"
    elif [ $((NOW - last)) -ge "$interval" ]; then
      post "**$SHARD_NAME** STILL FAILING ($(human $((NOW-first)))): $msg" \
        && printf '%s %s %s' "$first" "$NOW" "$((level+1))" > "$f"
    fi
  elif [ -f "$f" ]; then
    read -r first last level < "$f"
    post "**$SHARD_NAME** RECOVERED after $(human $((NOW-first))): $key" && rm -f "$f"
    printf 'RECOVERED %s\n' "$key"
  fi
}

yn() { [ "$1" -gt 0 ] 2>/dev/null && echo yes || echo no; }

# --- host checks ------------------------------------------------------------
# Name the container explicitly where possible. Discovery by pattern picks up
# any unrelated rnode container on the same machine, which is how a dry run on
# a developer box alerted about somebody else's standalone node.
C="${MONITOR_CONTAINER:-$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E '^rnode\.' | head -1)}"
if [ -n "$C" ]; then
  st=$(docker inspect -f '{{.State.Status}}' "$C" 2>/dev/null)
  hl=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$C" 2>/dev/null)
  rc=$(docker inspect -f '{{.RestartCount}}' "$C" 2>/dev/null || echo 0)

  signal container_down "$([ "$st" = running ] && echo no || echo yes)" \
    "$C is ${st:-missing} on $(hostname)"
  signal container_unhealthy "$([ "$hl" = unhealthy ] && echo yes || echo no)" \
    "$C reports unhealthy on $(hostname)"

  prev_rc=0; [ -f "$STATE_DIR/restart_count" ] && read -r prev_rc < "$STATE_DIR/restart_count"
  if [ "${rc:-0}" -gt "${prev_rc:-0}" ] 2>/dev/null; then
    post "**$SHARD_NAME** restart detected: $C on $(hostname) ($((rc-prev_rc))x, total $rc)" \
      && printf '%s' "$rc" > "$STATE_DIR/restart_count"
  else
    printf '%s' "${rc:-0}" > "$STATE_DIR/restart_count"
  fi

  # Wedge: pinned CPU together with log silence, two consecutive checks.
  cpu=$(docker stats --no-stream --format '{{.CPUPerc}}' "$C" 2>/dev/null | tr -d '%')
  cpu_int=${cpu%%.*}; [ -z "$cpu_int" ] && cpu_int=0
  lines=$(docker logs --since "$WEDGE_LOG_WINDOW" "$C" 2>&1 | wc -l | tr -d ' ')
  wedged_now=no
  [ "$cpu_int" -ge "$WEDGE_CPU_PCT" ] 2>/dev/null && [ "$lines" = 0 ] && wedged_now=yes
  prev_wedge=no; [ -f "$SUPPRESS_DIR/wedge_streak" ] && read -r prev_wedge < "$SUPPRESS_DIR/wedge_streak"
  signal node_wedged "$([ "$wedged_now" = yes ] && [ "$prev_wedge" = yes ] && echo yes || echo no)" \
    "$C on $(hostname): CPU >=${WEDGE_CPU_PCT}% and no log output for $WEDGE_LOG_WINDOW"
  printf '%s' "$wedged_now" > "$SUPPRESS_DIR/wedge_streak"

  anon=$(awk '/^anon /{printf "%d", $2/1048576}' \
    "/sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' "$C" 2>/dev/null).scope/memory.stat" 2>/dev/null)
  [ -n "${anon:-}" ] && signal container_anon_high \
    "$([ "$anon" -ge "$CONTAINER_ANON_WARN_MB" ] 2>/dev/null && echo yes || echo no)" \
    "$C anon memory ${anon}MB on $(hostname); a restart is the only reclaim"
  DIGEST="$DIGEST$NL- $(hostname): $C $st/$hl, CPU ${cpu:-?}%, anon ${anon:-?}MB, restarts ${rc:-?}"
fi

disk=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
signal disk_low "$([ -n "$disk" ] && [ "$disk" -ge "$DISK_WARN_PCT" ] 2>/dev/null && echo yes || echo no)" \
  "disk ${disk}% used on / at $(hostname)"
memav=$(free -m 2>/dev/null | awk '/^Mem:/{print $7}')
signal host_mem_low "$([ -n "$memav" ] && [ "$memav" -lt "$MEM_AVAIL_WARN_MB" ] 2>/dev/null && echo yes || echo no)" \
  "host memory ${memav}MB available at $(hostname)"
DIGEST="$DIGEST$NL- $(hostname): disk ${disk:-?}%, mem ${memav:-?}MB available"

# --- chain checks -----------------------------------------------------------
if [ "$CHAIN" = 1 ]; then
  : "${SHARD_NODES:?set to a space-separated list of name=host:port using each HTTP port}"
  : "${SHARD_NETWORK_ID:?set so a node on the wrong network is detected}"

  names=(); lfbs=(); gaps=(); max_lfb=""; newest=0
  down=(); notready=(); wrongnet=(); lagging=(); widegap=(); inflight=(); stuckmarker=()

  for spec in $SHARD_NODES; do
    n="${spec%%=*}"; addr="${spec#*=}"
    s=$(curl -s --max-time "$CURL_MAX_TIME" "http://$addr/api/status" 2>/dev/null)
    [ -z "$s" ] && { down+=("$n"); continue; }
    printf '%s' "$s" | grep -q '"isReady":true' || notready+=("$n")
    net=$(printf '%s' "$s" | grep -oE '"networkId":"[^"]*"' | sed 's/.*://; s/"//g')
    [ -n "$net" ] && [ "$net" != "$SHARD_NETWORK_ID" ] && wrongnet+=("$n:$net")
    lfb=$(printf '%s' "$s" | grep -oE '"lastFinalizedBlockNumber":-?[0-9]+' | sed 's/.*://')
    [ -z "$lfb" ] && { down+=("$n"); continue; }

    # tip and floor from the SAME node. Mixing a cross-node maximum with one
    # node's tip produced a negative floor distance on the previous shard, so
    # the alert could not fire during the condition it existed to catch.
    b=$(curl -s --max-time "$CURL_MAX_TIME" "http://$addr/api/blocks/1" 2>/dev/null)
    tip=$(printf '%s' "$b" | grep -oE '"blockNumber":[0-9]+' | sed 's/.*://' | sort -n | tail -1)
    ts=$(printf '%s' "$b" | grep -oE '"timestamp":[0-9]+' | sed 's/.*://' | sort -n | tail -1)
    [ -n "${ts:-}" ] && [ "$ts" -gt "$newest" ] && newest="$ts"
    gap=0; [ -n "${tip:-}" ] && gap=$((tip - lfb))
    [ "$gap" -ge "$FINALIZATION_GAP_BLOCKS" ] 2>/dev/null && widegap+=("$n:$gap")

    m=$(curl -s --max-time "$CURL_MAX_TIME" "http://$addr/metrics" 2>/dev/null)
    gm() { printf '%s' "$m" | grep -E "^$1\{" | head -1 | awk '{print $NF}'; }
    inf=$(gm block_processing_in_flight); cap=$(gm block_processing_parallel_limit)
    old=$(gm block_processing_in_flight_oldest_age_seconds)
    # The cap counts queued plus active; parallel_limit is the concurrency, so
    # use the documented 512 unless a gauge says otherwise.
    capn=512
    [ -n "${inf:-}" ] && [ "${inf%%.*}" -ge $((capn * IN_FLIGHT_WARN_PCT / 100)) ] 2>/dev/null \
      && inflight+=("$n:${inf%%.*}")
    [ -n "${old:-}" ] && [ "${old%%.*}" -ge "$IN_FLIGHT_OLDEST_WARN_SECS" ] 2>/dev/null \
      && stuckmarker+=("$n:${old%%.*}s")

    names+=("$n"); lfbs+=("$lfb"); gaps+=("$gap")
    if [ -z "$max_lfb" ] || [ "$lfb" -gt "$max_lfb" ]; then max_lfb="$lfb"; fi
    DIGEST="$DIGEST$NL- $n: finalized $lfb, tip ${tip:-?}, gap $gap, in-flight ${inf:-?}"
  done

  signal nodes_unreachable "$(yn ${#down[@]})"  "unreachable: ${down[*]:-}"
  signal nodes_not_ready   "$(yn ${#notready[@]})" "isReady false: ${notready[*]:-}"
  signal wrong_network     "$(yn ${#wrongnet[@]})" "wrong networkId, expected $SHARD_NETWORK_ID: ${wrongnet[*]:-}"
  signal finalization_gap  "$(yn ${#widegap[@]})" "tip minus finalized >=$FINALIZATION_GAP_BLOCKS, so blocks outrun finalization: ${widegap[*]:-}"
  signal in_flight_filling "$(yn ${#inflight[@]})" "in-flight set over ${IN_FLIGHT_WARN_PCT}% of the 512 cap: ${inflight[*]:-}"
  signal in_flight_stuck   "$(yn ${#stuckmarker[@]})" "a block has been in-flight longer than ${IN_FLIGHT_OLDEST_WARN_SECS}s: ${stuckmarker[*]:-}"

  if [ -n "$max_lfb" ]; then
    for i in "${!names[@]}"; do
      [ $((max_lfb - ${lfbs[$i]})) -ge "$NODE_LAG_BLOCKS" ] 2>/dev/null \
        && lagging+=("${names[$i]}:${lfbs[$i]}")
    done
    signal node_lagging "$(yn ${#lagging[@]})" \
      "behind the chain by >=$NODE_LAG_BLOCKS blocks: ${lagging[*]:-}"

    pf="$STATE_DIR/max_lfb"; stalled=no
    if [ -f "$pf" ]; then
      read -r pl pt < "$pf"
      if [ "$max_lfb" -gt "${pl:-0}" ] 2>/dev/null; then printf '%s %s' "$max_lfb" "$NOW" > "$pf"
      elif [ $((NOW - ${pt:-$NOW})) -ge "$LFB_STALL_SECS" ]; then stalled=yes; fi
    else printf '%s %s' "$max_lfb" "$NOW" > "$pf"; fi
    read -r _ since < "$pf"
    signal chain_stalled "$stalled" \
      "no node has finalized past $max_lfb for $(human $((NOW - ${since:-$NOW}))) "

    if [ "$newest" -gt 0 ]; then
      age=$(( (NOW_MS - newest) / 1000 )); [ "$age" -lt 0 ] && age=0
      signal no_recent_block "$([ "$age" -ge "$NO_BLOCK_SECS" ] && echo yes || echo no)" \
        "newest block anywhere is ${age}s old"
      DIGEST="$DIGEST$NL- chain: finalized $max_lfb, newest block ${age}s ago"
    fi
  else
    signal all_nodes_down yes "no node answered /api/status"
  fi
fi

# --- nightly digest ---------------------------------------------------------
if [ "$NIGHTLY" = 1 ]; then
  v=HEALTHY; [ "$FAILED" = yes ] && v=DEGRADED
  # Braces are required: $NL_ would parse as the variable NL_ and abort under set -u.
  post "**Nightly: $SHARD_NAME — $v**$DIGEST${NL}_$(date -u '+%Y-%m-%d %H:%M:%S UTC')_"
  exit 0
fi

[ "$FAILED" = yes ] && exit 1
exit 0

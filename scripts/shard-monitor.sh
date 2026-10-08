#!/usr/bin/env bash
# Monitors a shard whose nodes run on separate hosts, and alerts on the
# conditions that a container healthcheck cannot see.
#
# The previous shard reported every container "healthy" for four days while no
# node finalized anything, and the block-production alert never fired because
# the validators kept proposing. The checks below are the ones that would have
# caught that, in the order they would have fired.
#
# Usage:
#   SHARD_NODES="boot=10.0.0.4:40403 v1=10.0.0.5:40403 ..." \
#   SHARD_NETWORK_ID=<id> ./shard-monitor.sh
#
# Cron, every two minutes:
#   */2 * * * * SHARD_NODES="..." SHARD_NETWORK_ID=... /path/shard-monitor.sh
#
# The webhook is read from WEBHOOK_FILE, not passed on the command line and not
# embedded here: on the previous host it lived in a world-readable script.

set -uo pipefail

# Do not use apostrophes or quotes in a ${VAR:?word} message: bash parses the
# word specially and an unpaired quote mis-syncs the parser for the whole file.
SHARD_NODES="${SHARD_NODES:?set to a space-separated list of name=host:port using each HTTP port}"
EXPECTED_NETWORK_ID="${SHARD_NETWORK_ID:?SHARD_NETWORK_ID must be set so a node on the wrong network is detected}"

STATE_DIR="${STATE_DIR:-$HOME/.shard-monitor}"
WEBHOOK_FILE="${WEBHOOK_FILE:-$STATE_DIR/webhook}"
CURL_MAX_TIME="${CURL_MAX_TIME:-20}"

# A finalized height that has not moved for this long is a stalled chain.
LFB_STALL_SECS="${LFB_STALL_SECS:-900}"
# No block produced anywhere for this long. This is the condition that makes
# every node broadcast fork-choice tip requests, and it went unnoticed for
# 25+ minutes on the previous shard.
NO_BLOCK_SECS="${NO_BLOCK_SECS:-600}"
# One node's height is not the chain's: the chain is the max across nodes.
NODE_LAG_BLOCKS="${NODE_LAG_BLOCKS:-100}"
# Tip minus finalized height. Production continuing while finalization is
# frozen is the signature of the previous failure, and nothing watched it.
FINALIZATION_GAP_BLOCKS="${FINALIZATION_GAP_BLOCKS:-500}"
# A condition that persists must re-announce, or an operator who starts
# watching after the first alert reads silence as health.
REASSERT_SECS="${REASSERT_SECS:-21600}"

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true

NOW_S=$(date -u +%s)
NOW_MS=$((NOW_S * 1000))

# Set by signal() so the exit status reflects whether anything is failing,
# which lets this be wrapped by something other than cron.
FAILED=no

post() {
  local msg="$1" url code
  # Configuration failures are logged as well as printed: under cron, stderr
  # goes to mail that may not be configured, and a monitor whose webhook is
  # broken must not be the one thing that fails quietly.
  undeliverable() {
    printf '%s %s: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" "$msg" | tee -a "$STATE_DIR/post_failures.log" >&2
    return 1
  }
  [ -r "$WEBHOOK_FILE" ] || { undeliverable "no webhook file at $WEBHOOK_FILE"; return 1; }
  url=$(tr -d '[:space:]' < "$WEBHOOK_FILE")
  [ -n "$url" ] || { undeliverable "webhook file $WEBHOOK_FILE is empty"; return 1; }
  local esc="${msg//\\/\\\\}"
  esc="${esc//\"/\\\"}"
  esc="${esc//$'\n'/ }"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
    -H 'Content-Type: application/json' \
    -d "{\"content\":\"$esc\"}" \
    "$url")
  case "$code" in
    200|204) return 0 ;;
  esac
  printf '%s post failed http=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "${code:-000}" >> "$STATE_DIR/post_failures.log"
  return 1
}

# Alerts once per condition, re-asserts every REASSERT_SECS, and reports
# recovery exactly once. Delivery is verified before the state is cleared, so a
# failed send does not lose the message.
signal() {
  # Split: a single `local` declares every name before assigning, so a later
  # assignment in the same statement cannot reference an earlier one under set -u.
  local key="$1" firing="$2" msg="$3" since
  local f="$STATE_DIR/cond.$key"
  if [ "$firing" = yes ]; then
    if [ -f "$f" ]; then
      since=$(cat "$f" 2>/dev/null || echo "$NOW_S")
      if [ $((NOW_S - since)) -ge "$REASSERT_SECS" ]; then
        post "STILL FAILING ($(( (NOW_S - since) / 60 ))m): $msg" && printf '%s\n' "$NOW_S" > "$f"
      fi
    else
      # Record the condition as announced only once delivery is confirmed.
      # Writing first would mark an undelivered alert as sent and suppress it
      # until the re-assert interval.
      post "FAIL: $msg" && printf '%s\n' "$NOW_S" > "$f"
    fi
    printf 'FAIL %s: %s\n' "$key" "$msg"
    FAILED=yes
  elif [ -f "$f" ]; then
    post "RECOVERED: $key" && rm -f "$f"
    printf 'RECOVERED %s\n' "$key"
  fi
}

field() { # field <body> <json key> -> largest integer value for that key
  printf '%s' "$1" | grep -oE "\"$2\":-?[0-9]+" | sed 's/.*://' | sort -n | tail -1
}

names=() lfbs=() tips=() newest_ts=0 max_lfb="" unreachable=() wrong_net=() not_ready=()

for spec in $SHARD_NODES; do
  name="${spec%%=*}"
  addr="${spec#*=}"
  status=$(curl -s --max-time "$CURL_MAX_TIME" "http://$addr/api/status" 2>/dev/null)
  if [ -z "$status" ]; then
    unreachable+=("$name")
    continue
  fi

  net=$(printf '%s' "$status" | grep -oE '"networkId":"[^"]*"' | sed 's/.*://; s/"//g')
  [ -n "$net" ] && [ "$net" != "$EXPECTED_NETWORK_ID" ] && wrong_net+=("$name:$net")
  printf '%s' "$status" | grep -q '"isReady":true' || not_ready+=("$name")

  lfb=$(field "$status" lastFinalizedBlockNumber)
  [ -n "$lfb" ] || { unreachable+=("$name"); continue; }

  blocks=$(curl -s --max-time "$CURL_MAX_TIME" "http://$addr/api/blocks/1" 2>/dev/null)
  tip=$(field "$blocks" blockNumber)
  ts=$(field "$blocks" timestamp)
  [ -n "$ts" ] && [ "$ts" -gt "$newest_ts" ] && newest_ts="$ts"

  names+=("$name"); lfbs+=("$lfb"); tips+=("${tip:-}")
  if [ -z "$max_lfb" ] || [ "$lfb" -gt "$max_lfb" ]; then max_lfb="$lfb"; fi
done

if [ -z "$max_lfb" ]; then
  signal all_nodes_down yes "no node answered /api/status (${SHARD_NODES})"
  exit 1
fi

printf 'chain lfb=%s  newest_block_age=%ss\n' "$max_lfb" \
  "$([ "$newest_ts" -gt 0 ] && echo $(( (NOW_MS - newest_ts) / 1000 )) || echo unknown)"

signal nodes_unreachable "$([ ${#unreachable[@]} -gt 0 ] && echo yes || echo no)" \
  "unreachable: ${unreachable[*]:-}"
signal nodes_not_ready "$([ ${#not_ready[@]} -gt 0 ] && echo yes || echo no)" \
  "isReady false: ${not_ready[*]:-}"
signal wrong_network "$([ ${#wrong_net[@]} -gt 0 ] && echo yes || echo no)" \
  "wrong networkId (expected $EXPECTED_NETWORK_ID): ${wrong_net[*]:-}"

# Chain liveness, against the max height across nodes.
prev_file="$STATE_DIR/max_lfb"
stalled=no
if [ -f "$prev_file" ]; then
  read -r prev_lfb prev_at < "$prev_file"
  if [ "$max_lfb" -gt "${prev_lfb:-0}" ]; then
    printf '%s %s\n' "$max_lfb" "$NOW_S" > "$prev_file"
  elif [ $((NOW_S - ${prev_at:-$NOW_S})) -ge "$LFB_STALL_SECS" ]; then
    stalled=yes
  fi
else
  printf '%s %s\n' "$max_lfb" "$NOW_S" > "$prev_file"
fi
signal chain_stalled "$stalled" \
  "finalized height stuck at $max_lfb for $(( (NOW_S - $(cut -d' ' -f2 < "$prev_file")) / 60 ))m across all nodes"

# Nothing produced anywhere. Below this, every node starts broadcasting
# fork-choice tip requests.
if [ "$newest_ts" -gt 0 ]; then
  age=$(( (NOW_MS - newest_ts) / 1000 ))
  [ "$age" -lt 0 ] && age=0
  signal no_recent_block "$([ "$age" -ge "$NO_BLOCK_SECS" ] && echo yes || echo no)" \
    "newest block anywhere is ${age}s old"
fi

# Per-node lag, and production outrunning finalization.
lagging=() wide_gap=()
for i in "${!names[@]}"; do
  [ $(( max_lfb - ${lfbs[$i]} )) -ge "$NODE_LAG_BLOCKS" ] && lagging+=("${names[$i]}:${lfbs[$i]}")
  if [ -n "${tips[$i]}" ]; then
    gap=$(( ${tips[$i]} - ${lfbs[$i]} ))
    [ "$gap" -ge "$FINALIZATION_GAP_BLOCKS" ] && wide_gap+=("${names[$i]}:$gap")
  fi
done
signal node_lagging "$([ ${#lagging[@]} -gt 0 ] && echo yes || echo no)" \
  "behind chain height $max_lfb by >=$NODE_LAG_BLOCKS: ${lagging[*]:-}"
signal finalization_gap "$([ ${#wide_gap[@]} -gt 0 ] && echo yes || echo no)" \
  "tip minus finalized >=$FINALIZATION_GAP_BLOCKS, so blocks are produced faster than they finalize: ${wide_gap[*]:-}"

[ "$FAILED" = yes ] && exit 1
exit 0

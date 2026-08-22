#!/usr/bin/env bash
# Reserve the host ports the shard and the integration-test framework bind,
# so the kernel never hands them out as ephemeral source ports.
#
# Compose files publish 40400-40455; integration-tests' PortAllocator uses
# 41000-49000. Both sit inside Linux's default ephemeral range (32768-60999).
# On a fresh CI runner, `poetry install` / `docker pull` open outbound
# connections whose source ports can land in that span and linger in
# TIME_WAIT; `docker compose up` then fails with "address already in use"
# (surfaced by shardctl as "Error: Port conflict"). Reserving the span
# closes that window for every connection opened after this step.
#
# The default span 40400-49000 is one contiguous range covering both users
# (40456-40999 are reserved only to keep it contiguous). It removes ~30% of
# the ephemeral pool: fine for a CI runner, not for a long-lived host.
#
# Any reservation already present is kept: the new span is appended, not
# substituted, so self-hosted runners that reserve ports for other tooling
# are not clobbered.
set -euo pipefail

SYSCTL_KEY="net.ipv4.ip_local_reserved_ports"
RESERVED="${SHARD_RESERVED_PORTS:-40400-49000}"

if ! [[ "$RESERVED" =~ ^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$ ]]; then
    echo "reserve-shard-ports: SHARD_RESERVED_PORTS must look like 40400-49000 or 1,2-3; got '$RESERVED'" >&2
    exit 1
fi

if [ "$(uname -s)" != "Linux" ]; then
    echo "reserve-shard-ports: not Linux, nothing to do"
    exit 0
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

# covers LIST WANTED: true when every port in WANTED is inside LIST. The
# kernel normalises the value on read (merges overlaps), so a literal
# substring match is not reliable.
covers() {
    python3 - "$1" "$2" <<'PY'
import sys

def ports(spec):
    out = set()
    for part in filter(None, spec.split(",")):
        lo, _, hi = part.partition("-")
        out.update(range(int(lo), int(hi or lo) + 1))
    return out

sys.exit(0 if ports(sys.argv[2]) <= ports(sys.argv[1]) else 1)
PY
}

existing="$($SUDO sysctl -n "$SYSCTL_KEY" 2>/dev/null || true)"

# Idempotent: a second run on a persistent runner must not append again.
if covers "$existing" "$RESERVED"; then
    echo "reserve-shard-ports: ${SYSCTL_KEY} already covers '${RESERVED}' ('${existing}')"
    exit 0
fi

if [ -n "$existing" ]; then
    merged="${existing},${RESERVED}"
else
    merged="$RESERVED"
fi

$SUDO sysctl -q -w "${SYSCTL_KEY}=${merged}"

# Read back and require the span itself, not merely a non-empty value: on a
# runner that already had reservations, an ignored write would leave the
# old value in place and a non-empty check would pass anyway.
actual="$($SUDO sysctl -n "$SYSCTL_KEY")"
echo "reserve-shard-ports: ${SYSCTL_KEY} was '${existing:-<empty>}', now '${actual}'"
if ! covers "$actual" "$RESERVED"; then
    echo "reserve-shard-ports: value read back does not cover '${RESERVED}'; write did not take effect" >&2
    exit 1
fi

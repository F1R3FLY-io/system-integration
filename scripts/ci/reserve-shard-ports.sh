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

existing="$($SUDO sysctl -n "$SYSCTL_KEY" 2>/dev/null || true)"
if [ -n "$existing" ]; then
    merged="${existing},${RESERVED}"
else
    merged="$RESERVED"
fi

$SUDO sysctl -q -w "${SYSCTL_KEY}=${merged}"

# Read back: the kernel normalises the list, and a runner that silently
# ignored the write would otherwise look reserved in the log.
actual="$($SUDO sysctl -n "$SYSCTL_KEY")"
echo "reserve-shard-ports: ${SYSCTL_KEY} was '${existing:-<empty>}', now '${actual}'"
if [ -z "$actual" ]; then
    echo "reserve-shard-ports: write did not take effect" >&2
    exit 1
fi

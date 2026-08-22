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
# removes that race without touching the port map.
set -euo pipefail

RESERVED="${SHARD_RESERVED_PORTS:-40400-49000}"

if [ "$(uname -s)" != "Linux" ]; then
    echo "reserve-shard-ports: not Linux, nothing to do"
    exit 0
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    SUDO="sudo"
fi

$SUDO sysctl -w net.ipv4.ip_local_reserved_ports="$RESERVED"
echo "reserve-shard-ports: reserved $RESERVED"

#!/bin/bash
# status.sh — Show status of all services
# Usage: ./scripts/status.sh

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

echo "========================================="
echo "Service Status"
echo "========================================="

check() {
    local name="$1"
    local port="$2"
    local url="$3"

    local status=$(docker ps -a --filter "name=^${name}$" --format '{{.Status}}' 2>/dev/null)
    if [ -z "$status" ]; then
        echo -e "  ${RED}[DOWN]${NC} $name (no container)"
    elif echo "$status" | grep -q "Up"; then
        if curl -s --max-time 2 "$url" >/dev/null 2>&1; then
            echo -e "  ${GREEN}[ UP ]${NC} $name (port $port) — responding"
        else
            echo -e "  ${YELLOW}[ UP ]${NC} $name (port $port) — not responding yet"
        fi
    else
        echo -e "  ${RED}[DOWN]${NC} $name — $status"
    fi
}

echo ""
echo "Shard:"
for node in rnode.bootstrap rnode.validator1 rnode.validator2 rnode.validator3 rnode.readonly; do
    status=$(docker ps -a --filter "name=^${node}$" --format '{{.Status}}' 2>/dev/null)
    if echo "$status" | grep -q "Up"; then
        echo -e "  ${GREEN}[ UP ]${NC} $node"
    else
        echo -e "  ${RED}[DOWN]${NC} $node"
    fi
done

echo ""
echo "Embers:"
check embers 8080 "http://localhost:8080/api/service/ready"
check embers-frontend 8081 "http://localhost:8081"

echo ""
echo "F1R3Sky:"
# postgres and redis are internal-only (no host port), just check container status
for svc in f1r3sky-postgres f1r3sky-redis; do
    status=$(docker ps -a --filter "name=^${svc}$" --format '{{.Status}}' 2>/dev/null)
    if [ -z "$status" ]; then
        echo -e "  ${RED}[DOWN]${NC} $svc (no container)"
    elif echo "$status" | grep -q "Up"; then
        echo -e "  ${GREEN}[ UP ]${NC} $svc"
    else
        echo -e "  ${RED}[DOWN]${NC} $svc — $status"
    fi
done
check f1r3sky 2583 "http://localhost:2583/xrpc/_health"
check f1r3sky-frontend 8100 "http://localhost:8100"

echo ""

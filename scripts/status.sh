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

    # Match compose container names (compose-{name}-1) or plain names
    local status=$(docker ps -a --filter "name=${name}" --format '{{.Names}} {{.Status}}' 2>/dev/null | head -1)
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
echo "Services:"
check embers 8080 "http://localhost:8080/api/service/ready"
check embers-frontend 8081 "http://localhost:8081"
check f1r3sky-postgres 5432 "n/a"
check f1r3sky-redis 6379 "n/a"
check "f1r3sky-1\|f1r3sky$" 2583 "http://localhost:2583/xrpc/_health"
check f1r3sky-frontend 8100 "http://localhost:8100"

echo ""
echo "API Health:"
for endpoint in \
    "ai-agents-teams/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g" \
    "ai-agents/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g" \
    "oslfs/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g" \
    "wallets/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g/state"; do
    name=$(echo "$endpoint" | cut -d/ -f1)
    result=$(curl -s --max-time 5 "http://localhost:8080/api/$endpoint" 2>&1)
    if echo "$result" | grep -q 'contract did not return'; then
        echo -e "  ${RED}[FAIL]${NC} $name — contract did not return any value"
    elif echo "$result" | grep -q '"agents_teams"\|"agents"\|"oslfs"\|"balance"'; then
        echo -e "  ${GREEN}[ OK ]${NC} $name"
    else
        echo -e "  ${YELLOW}[????]${NC} $name — $(echo "$result" | head -c 100)"
    fi
done

echo ""

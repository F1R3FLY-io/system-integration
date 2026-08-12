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

# Check if a container is running by partial name match
container_status() {
    local pattern="$1"
    docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | grep "$pattern" | head -1
}

# Check a service with HTTP health endpoint
check_service() {
    local label="$1"
    local pattern="$2"
    local port="$3"
    local url="$4"

    local status=$(container_status "$pattern")
    if [ -z "$status" ]; then
        echo -e "  ${RED}[DOWN]${NC} $label (no container)"
        return 1
    fi

    local name=$(echo "$status" | awk '{print $1}')
    if echo "$status" | grep -q "Up"; then
        if [ "$url" = "internal" ]; then
            echo -e "  ${GREEN}[ UP ]${NC} $label ($name)"
        elif curl -s --max-time 2 "$url" >/dev/null 2>&1; then
            echo -e "  ${GREEN}[ UP ]${NC} $label ($name, port $port) — responding"
        else
            echo -e "  ${YELLOW}[ UP ]${NC} $label ($name, port $port) — not responding"
        fi
    else
        echo -e "  ${RED}[DOWN]${NC} $label ($name) — stopped"
        return 1
    fi
}

echo ""
echo "Nodes:"
for node in rnode.bootstrap rnode.validator1 rnode.validator2 rnode.validator3 rnode.readonly rnode.rust-standalone rnode.standalone; do
    status=$(docker ps --filter "name=^${node}$" --format '{{.Status}}' 2>/dev/null)
    if [ -n "$status" ] && echo "$status" | grep -q "Up"; then
        echo -e "  ${GREEN}[ UP ]${NC} $node"
    fi
done
# Check if any node is running
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "rnode"; then
    echo -e "  ${RED}[DOWN]${NC} No nodes running"
fi

echo ""
echo "Services:"
check_service "Embers API" "embers-1" 8080 "http://localhost:8080/api/service/ready"
check_service "Embers Frontend" "embers-frontend" 8081 "http://localhost:8081"
check_service "F1R3Sky PDS" "f1r3sky-1" 2583 "http://localhost:2583/xrpc/_health"
check_service "F1R3Sky Frontend" "f1r3sky-frontend" 8100 "http://localhost:8100"
check_service "F1R3Sky Postgres" "f1r3sky-postgres" 5432 "internal"
check_service "F1R3Sky Redis" "f1r3sky-redis" 6379 "internal"

echo ""
echo "Embers API Health:"
EMBERS_OK=true
for endpoint in \
    "ai-agents-teams/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g" \
    "ai-agents/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g" \
    "oslfs/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g" \
    "wallets/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g/state"; do
    name=$(echo "$endpoint" | cut -d/ -f1)
    result=$(curl -s --max-time 5 "http://localhost:8080/api/$endpoint" 2>&1)
    if echo "$result" | grep -q 'contract did not return'; then
        echo -e "  ${RED}[FAIL]${NC} $name — contract did not return any value"
        EMBERS_OK=false
    elif echo "$result" | grep -q '"agents_teams"\|"agents"\|"oslfs"\|"balance"'; then
        echo -e "  ${GREEN}[ OK ]${NC} $name"
    elif [ -z "$result" ]; then
        echo -e "  ${RED}[FAIL]${NC} $name — no response"
        EMBERS_OK=false
    else
        echo -e "  ${YELLOW}[????]${NC} $name — $(echo "$result" | head -c 100)"
        EMBERS_OK=false
    fi
done

echo ""
if $EMBERS_OK; then
    echo -e "${GREEN}All systems operational${NC}"
else
    echo -e "${YELLOW}Some services need attention${NC}"
fi
echo ""

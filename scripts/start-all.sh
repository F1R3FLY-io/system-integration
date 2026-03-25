#!/bin/bash
# start-all.sh — Start all services (embers, f1r3sky) against a running shard
# Usage: ./scripts/start-all.sh
#
# Prerequisites: Rust shard must already be running on the f1r3fly Docker network
# See: services/f1r3node-rust/docker/shard.yml

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

log() { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err() { echo -e "${RED}[x]${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/compose/services.yml"

# Check shard is running
if ! docker ps --format '{{.Names}}' | grep -q 'rnode.bootstrap'; then
    err "Shard is not running. Start it first:"
    echo "  cd services/f1r3node-rust/docker && docker compose -f shard.yml up -d"
    exit 1
fi

log "Shard is running"

# Start all services via compose (idempotent — reuses existing containers)
log "Starting services..."
docker compose -f "$COMPOSE_FILE" up -d 2>&1 | grep -v "^$" || true

# Wait for embers API to become healthy
# Bootstrap deploys 5 init contracts sequentially (~20-30s each on fresh shard)
log "Waiting for embers to bootstrap (this may take a few minutes on fresh shard)..."
for i in $(seq 1 180); do
    if curl -s http://localhost:8080/api/service/ready >/dev/null 2>&1; then
        log "Embers API is healthy"
        break
    fi
    if [ "$i" -eq 180 ]; then
        err "Embers API not responding after 6 minutes"
        docker logs compose-embers-1 2>&1 | grep -E "ERROR|WARN|finalized|errored" | tail -10
        exit 1
    fi
    sleep 2
done

# Create f1r3sky user account (idempotent — ignores if exists)
log "Creating f1r3sky user account (user1.test)..."
RESULT=$(curl -s -X POST http://localhost:2583/xrpc/com.atproto.server.createAccount \
  -H 'Content-Type: application/json' \
  -d '{"handle": "user1.test", "email": "user1@test.com", "password": "password123"}' 2>&1)

if echo "$RESULT" | grep -q '"handle"'; then
    log "Account created: user1.test"
else
    warn "Account creation failed (may already exist): $(echo "$RESULT" | head -1)"
fi

# Wait for init deploys to finalize (agents_teams endpoint returns 200)
log "Waiting for blockchain init deploys to finalize..."
for i in $(seq 1 60); do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/api/ai-agents-teams/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g 2>&1)
    BODY=$(curl -s http://localhost:8080/api/ai-agents-teams/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g 2>&1)
    if [ "$HTTP_CODE" = "200" ] && echo "$BODY" | grep -q 'agents_teams'; then
        log "Agents teams endpoint working"
        break
    fi
    if [ "$i" -eq 60 ]; then
        err "Init deploys did not finalize after 120s (status: $HTTP_CODE)"
        exit 1
    fi
    sleep 2
done

echo ""
echo "========================================="
echo "All services started"
echo "========================================="
echo ""
echo "  Embers Frontend:  http://localhost:8081"
echo "    Sign in key:    5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657"
echo ""
echo "  F1R3Sky Frontend: http://localhost:8100"
echo "    Hosting:        http://localhost:2583"
echo "    Username:       user1.test"
echo "    Password:       password123"
echo ""
echo "  Embers API:       http://localhost:8080"
echo "  F1R3Sky PDS:      http://localhost:2583"
echo "  Swagger UI:       http://localhost:8080/swagger-ui/index.html"
echo ""
echo "========================================="

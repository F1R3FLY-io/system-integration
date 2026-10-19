#!/bin/bash
# start-all.sh — Start all services (embers, f1r3sky) against a running node
# Usage: ./scripts/start-all.sh [--node shard|rust-standalone|scala-standalone]
#
# Modes:
#   shard            (default) Rust shard on f1r3fly network
#   rust-standalone  Rust standalone node
#   scala-standalone Scala standalone node

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

# Parse --node flag
NODE_MODE="shard"
while [[ $# -gt 0 ]]; do
    case $1 in
        --node) NODE_MODE="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# Configure based on node mode
case "$NODE_MODE" in
    shard)
        COMPOSE_FILE="$ROOT_DIR/compose/services.yml"
        EMBERS_ENV="embers.env"
        NODE_CHECK_CONTAINER="rnode.bootstrap"
        NODE_NETWORK="f1r3fly"
        COMPOSE_PREFIX="compose"
        log "Mode: Rust shard"
        ;;
    rust-standalone)
        COMPOSE_FILE="$ROOT_DIR/compose/services-standalone.yml"
        EMBERS_ENV="embers.rust-standalone.env"
        NODE_CHECK_CONTAINER="rnode.rust-standalone"
        export NODE_NETWORK="rust-standalone_f1r3fly-standalone"
        export EMBERS_ENV
        COMPOSE_PREFIX="compose"
        log "Mode: Rust standalone"
        ;;
    scala-standalone)
        COMPOSE_FILE="$ROOT_DIR/compose/services-standalone.yml"
        EMBERS_ENV="embers.scala-standalone.env"
        NODE_CHECK_CONTAINER="rnode.standalone"
        export NODE_NETWORK="scala-standalone_f1r3fly-standalone"
        export EMBERS_ENV
        COMPOSE_PREFIX="compose"
        log "Mode: Scala standalone"
        ;;
    *)
        err "Unknown node mode: $NODE_MODE"
        echo "  Valid modes: shard, rust-standalone, scala-standalone"
        exit 1
        ;;
esac

# Check node is running
if ! docker ps --format '{{.Names}}' | grep -q "$NODE_CHECK_CONTAINER"; then
    err "Node '$NODE_CHECK_CONTAINER' is not running"
    exit 1
fi

log "Node is running ($NODE_CHECK_CONTAINER)"

# Start infra first to get f1r3sky IP for embers localhost override
log "Starting infrastructure..."
docker compose -f "$COMPOSE_FILE" up -d f1r3sky-postgres f1r3sky-redis f1r3sky 2>&1 | grep -v "^$" || true

# Get f1r3sky container IP — try compose prefix patterns
F1R3SKY_CONTAINER=$(docker ps --format '{{.Names}}' | grep "f1r3sky-1" | grep -v "postgres\|redis\|frontend" | head -1)
if [ -z "$F1R3SKY_CONTAINER" ]; then
    err "Could not find f1r3sky container"
    exit 1
fi

export F1R3SKY_IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$F1R3SKY_CONTAINER" 2>/dev/null || echo "")
if [ -z "$F1R3SKY_IP" ]; then
    err "Could not resolve f1r3sky container IP"
    exit 1
fi
log "F1R3Sky IP: $F1R3SKY_IP"

# Start remaining services (embers gets localhost -> f1r3sky mapping)
log "Starting services..."
docker compose -f "$COMPOSE_FILE" up -d 2>&1 | grep -v "^$" || true

# Wait for embers API to become healthy
log "Waiting for embers to bootstrap (this may take a few minutes on fresh node)..."
for i in $(seq 1 180); do
    if curl -s http://localhost:8080/api/service/ready >/dev/null 2>&1; then
        log "Embers API is healthy"
        break
    fi
    if [ "$i" -eq 180 ]; then
        err "Embers API not responding after 6 minutes"
        EMBERS_CONTAINER=$(docker ps --format '{{.Names}}' | grep "embers-1" | grep -v "frontend" | head -1)
        docker logs "$EMBERS_CONTAINER" 2>&1 | grep -E "ERROR|WARN|finalized|errored" | tail -10
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
echo "All services started ($NODE_MODE)"
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

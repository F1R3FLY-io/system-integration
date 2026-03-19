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
EMBERS_DIR="$ROOT_DIR/services/embers"

# Docker image tags:
#   f1r3flyio/embers:local          — built from local source (docker build -f docker/embers.dockerfile)
#   f1r3flyio/embers-frontend:local — pre-built from Docker Hub
#   f1r3flyindustries/firesky-ts:local — built from f1r3sky-backend source (must match frontend)
#   f1r3flyio/firesky-frontend:local — built locally with EXPO_PUBLIC_EMBERS_API_URL
#   postgres:16-alpine, redis:7-alpine — standard images
#
# If you change embers code, rebuild first:
#   cd services/embers && docker build -f docker/embers.dockerfile -t f1r3flyio/embers:local .

# Check shard is running
if ! docker ps --format '{{.Names}}' | grep -q 'rnode.bootstrap'; then
    err "Shard is not running. Start it first:"
    echo "  cd services/f1r3node-rust/docker && docker compose -f shard.yml up -d"
    exit 1
fi

log "Shard is running"

# Clean up any existing containers
log "Cleaning up existing containers..."
docker rm -f f1r3sky-postgres f1r3sky-redis f1r3sky embers embers-frontend f1r3sky-frontend 2>/dev/null || true

# Start f1r3sky infrastructure
log "Starting f1r3sky PostgreSQL..."
docker run -d --name f1r3sky-postgres --network f1r3fly \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=atproto \
  postgres:16-alpine >/dev/null

log "Starting f1r3sky Redis..."
docker run -d --name f1r3sky-redis --network f1r3fly \
  redis:7-alpine >/dev/null

log "Waiting for PostgreSQL to be ready..."
sleep 5

# Start f1r3sky backend (AT Protocol dev-env)
log "Starting f1r3sky backend (PDS:2583, BSKY:2584, Ozone:2587)..."
docker run -d --name f1r3sky --network f1r3fly \
  -p 2581:2581 -p 2582:2582 -p 2583:2583 -p 2584:2584 -p 2587:2587 \
  -e ENABLE_PDS=1 \
  -e DB_POSTGRES_URL=postgresql://postgres:postgres@f1r3sky-postgres:5432/atproto \
  -e REDIS_HOST=f1r3sky-redis \
  -e PDS_HOSTNAME=f1r3sky \
  f1r3flyindustries/firesky-ts:local >/dev/null

log "Waiting for f1r3sky to start..."
sleep 10

# Get f1r3sky IP for localhost alias
F1R3SKY_IP=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' f1r3sky)

# Start embers
log "Starting embers (port 8080)..."
docker run -d --name embers \
  --env-file "$EMBERS_DIR/embers.env" \
  --network f1r3fly \
  -p 8080:3000 \
  --add-host "localhost:$F1R3SKY_IP" \
  f1r3flyio/embers:local >/dev/null

# Start embers frontend
log "Starting embers frontend (port 8081)..."
docker run -d --name embers-frontend \
  -p 8081:80 \
  -e API_URL="http://localhost:8080" \
  f1r3flyio/embers-frontend:local >/dev/null

# Start f1r3sky frontend
log "Starting f1r3sky frontend (port 8100)..."
docker run -d --name f1r3sky-frontend --network f1r3fly \
  -p 8100:8100 \
  -e HTTP_ADDRESS=:8100 \
  -e ATP_APPVIEW_HOST=http://f1r3sky:2583 \
  f1r3flyio/firesky-frontend:local \
  /usr/bin/bskyweb serve >/dev/null

# Wait for embers to bootstrap
log "Waiting for embers to bootstrap..."
sleep 10

# Verify embers is healthy
if curl -s http://localhost:8080/api/service/ready >/dev/null 2>&1; then
    log "Embers API is healthy"
else
    warn "Embers API not responding yet — may need more time for init deploys to finalize"
fi

# Create f1r3sky user account
log "Creating f1r3sky user account (user1.test)..."
RESULT=$(curl -s -X POST http://localhost:2583/xrpc/com.atproto.server.createAccount \
  -H 'Content-Type: application/json' \
  -d '{"handle": "user1.test", "email": "user1@test.com", "password": "password123"}' 2>&1)

if echo "$RESULT" | grep -q '"handle"'; then
    log "Account created: user1.test"
else
    warn "Account creation failed (may already exist): $(echo "$RESULT" | head -1)"
fi

# Wait for embers init deploys to finalize
log "Waiting for blockchain init deploys to finalize..."
sleep 15

# Verify agents_teams endpoint
RESPONSE=$(curl -s http://localhost:8080/api/ai-agents-teams/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g 2>&1)
if echo "$RESPONSE" | grep -q 'agents_teams'; then
    log "Agents teams endpoint working"
else
    warn "Agents teams endpoint not ready: $RESPONSE"
    warn "Init deploys may still be finalizing — wait 30s and try again"
fi

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

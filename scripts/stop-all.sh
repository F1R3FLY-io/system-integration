#!/bin/bash
# stop-all.sh — Stop all embers and f1r3sky services (leaves shard running)
# Usage: ./scripts/stop-all.sh [--clean]
#   --clean   Also remove Docker volumes (database, cache, PDS data)

set -euo pipefail

CLEAN=false
if [[ "${1:-}" == "--clean" ]]; then
    CLEAN=true
fi

echo "Stopping embers and f1r3sky services..."
docker rm -f embers embers-frontend f1r3sky-frontend f1r3sky f1r3sky-redis f1r3sky-postgres 2>/dev/null || true

if $CLEAN; then
    echo "Removing associated volumes..."
    docker volume ls -q --filter dangling=true | xargs -r docker volume rm 2>/dev/null || true
    echo "Pruning build cache..."
    docker builder prune -f 2>/dev/null | tail -1 || true
    echo "Done. Containers and volumes removed."
else
    echo "Done. Containers removed (volumes preserved)."
    echo "  Use --clean to also remove volumes"
fi

echo ""
echo "To also stop the shard:"
echo "  cd services/f1r3node-rust/docker && docker compose -f shard.yml down"
echo ""
echo "To stop shard and wipe blockchain data:"
echo "  cd services/f1r3node-rust/docker && docker compose -f shard.yml down -v"

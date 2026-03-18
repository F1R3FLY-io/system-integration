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
    echo "Pruning unused volumes..."
    docker volume prune -f 2>/dev/null || true
    echo "Done. Containers removed and volumes pruned."
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

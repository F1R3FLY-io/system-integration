#!/bin/bash
# stop-all.sh — Stop all embers and f1r3sky services (leaves shard running)
# Usage: ./scripts/stop-all.sh [--clean]
#   --clean   Also remove Docker volumes (database, cache, PDS data)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/compose/services.yml"

CLEAN=false
if [[ "${1:-}" == "--clean" ]]; then
    CLEAN=true
fi

echo "Stopping services..."
if $CLEAN; then
    docker compose -f "$COMPOSE_FILE" down -v 2>&1 | grep -v "^$" || true
    echo "Done. Containers and volumes removed."
else
    docker compose -f "$COMPOSE_FILE" down 2>&1 | grep -v "^$" || true
    echo "Done. Containers removed (volumes preserved)."
    echo "  Use --clean to also remove volumes"
fi

echo ""
echo "To also stop the shard:"
echo "  cd services/f1r3node-rust/docker && docker compose -f shard.yml down"
echo ""
echo "To stop shard and wipe blockchain data:"
echo "  cd services/f1r3node-rust/docker && docker compose -f shard.yml down -v"

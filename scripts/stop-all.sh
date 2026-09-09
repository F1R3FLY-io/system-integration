#!/bin/bash
# stop-all.sh — Stop all embers and f1r3sky services (leaves node running)
# Usage: ./scripts/stop-all.sh [--clean] [--node shard|rust-standalone|scala-standalone]
#   --clean   Also remove Docker volumes (database, cache, PDS data)
#   --node    Select which compose config to stop (default: shard)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CLEAN=false
NODE_MODE="shard"

while [[ $# -gt 0 ]]; do
    case $1 in
        --clean) CLEAN=true; shift ;;
        --node) NODE_MODE="$2"; shift 2 ;;
        *) shift ;;
    esac
done

case "$NODE_MODE" in
    shard)
        COMPOSE_FILE="$ROOT_DIR/compose/services.yml"
        ;;
    rust-standalone)
        COMPOSE_FILE="$ROOT_DIR/compose/services-standalone.yml"
        export NODE_NETWORK="rust-standalone_f1r3fly-standalone"
        export EMBERS_ENV="embers.rust-standalone.env"
        ;;
    scala-standalone)
        COMPOSE_FILE="$ROOT_DIR/compose/services-standalone.yml"
        export NODE_NETWORK="scala-standalone_f1r3fly-standalone"
        export EMBERS_ENV="embers.scala-standalone.env"
        ;;
    *)
        echo "Unknown node mode: $NODE_MODE"
        exit 1
        ;;
esac

echo "Stopping services ($NODE_MODE)..."
if $CLEAN; then
    docker compose -f "$COMPOSE_FILE" down -v 2>&1 | grep -v "^$" || true
    echo "Done. Containers and volumes removed."
else
    docker compose -f "$COMPOSE_FILE" down 2>&1 | grep -v "^$" || true
    echo "Done. Containers removed (volumes preserved)."
    echo "  Use --clean to also remove volumes"
fi

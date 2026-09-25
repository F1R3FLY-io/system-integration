#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
E2E_DIR="$SCRIPT_DIR/../e2e-demo"

# Load .env if present (for optional config overrides)
if [ -f "$E2E_DIR/.env" ]; then
  set -a
  source "$E2E_DIR/.env"
  set +a
fi

# Ensure local SDK is built
SDK_DIR="$SCRIPT_DIR/../services/embers-frontend/packages/client"
if [ ! -d "$SDK_DIR/dist" ]; then
  echo "Building embers-client-sdk locally..."
  cd "$SCRIPT_DIR/../services/embers-frontend"
  pnpm --filter @f1r3fly-io/embers-client-sdk build
fi

# Install deps if needed
if [ ! -d "$E2E_DIR/node_modules" ]; then
  echo "Installing e2e-demo dependencies..."
  cd "$E2E_DIR"
  npm install
fi

cd "$E2E_DIR"
exec npx tsx demo-test.ts "$@"

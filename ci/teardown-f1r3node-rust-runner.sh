#!/usr/bin/env bash
# =================================================================
# F1R3FLY CI Runner Teardown Script (f1r3node / Rust)
# =================================================================
# Cleanly stops, unregisters, and removes a GitHub Actions self-hosted
# runner. Run this before decommissioning or migrating an instance.
#
# Usage:
#   ./teardown-f1r3node-rust-runner.sh --token <REMOVAL_TOKEN>
#
# Obtain a removal token from:
#   gh api repos/OWNER/REPO/actions/runners/remove-token
# =================================================================

set -euo pipefail

RUNNER_HOME="/opt/actions-runner"
SERVICE_NAME="actions-runner"
TOKEN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --token) TOKEN="$2"; shift 2 ;;
        --help)
            echo "Usage: $0 --token <REMOVAL_TOKEN>"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ -z "$TOKEN" ]]; then
    echo "ERROR: --token is required."
    echo "Get one with: gh api repos/OWNER/REPO/actions/runners/remove-token"
    exit 1
fi

echo "=== Tearing down GitHub Actions runner ==="

# Stop the service
if systemctl is-active "$SERVICE_NAME" &>/dev/null; then
    echo "Stopping runner service..."
    sudo systemctl stop "$SERVICE_NAME"
    sudo systemctl disable "$SERVICE_NAME"
fi

# Unregister from GitHub
if [[ -f "$RUNNER_HOME/.runner" ]]; then
    echo "Unregistering runner..."
    cd "$RUNNER_HOME"
    sudo -u runner ./config.sh remove --token "$TOKEN"
fi

echo ""
echo "=== Runner removed ==="
echo "The runner software remains at $RUNNER_HOME."
echo "To fully clean up: sudo rm -rf $RUNNER_HOME"

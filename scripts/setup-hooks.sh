#!/usr/bin/env bash
#
# setup-hooks.sh - Install git hooks for this repository
#
# Two installation methods are supported:
#   1. core.hooksPath (recommended) - points git directly at hooks/
#   2. copy - copies hooks to .git/hooks/ (traditional approach)
#
# Usage:
#   ./scripts/setup-hooks.sh           # Uses core.hooksPath (default)
#   ./scripts/setup-hooks.sh --copy    # Copies to .git/hooks/
#   ./scripts/setup-hooks.sh --status  # Show current hook configuration
#   ./scripts/setup-hooks.sh --remove  # Remove hook configuration
#

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Get repository root
REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOKS_SOURCE="$REPO_ROOT/hooks"
HOOKS_TARGET="$REPO_ROOT/.git/hooks"

show_status() {
    echo -e "${CYAN}Git Hooks Status${NC}"
    echo "================"

    local hooks_path
    hooks_path=$(git config --local core.hooksPath 2>/dev/null || echo "")

    if [[ -n "$hooks_path" ]]; then
        echo -e "core.hooksPath: ${GREEN}$hooks_path${NC}"
        echo "Method: core.hooksPath (git-native)"
    elif [[ -f "$HOOKS_TARGET/pre-push" || -f "$HOOKS_TARGET/pre-commit" ]]; then
        echo -e "Hooks installed: ${GREEN}$HOOKS_TARGET${NC}"
        echo "Method: copied to .git/hooks/"
    else
        echo -e "${YELLOW}No hooks configured${NC}"
        echo ""
        echo "Run: $0 to install hooks"
    fi

    echo ""
    echo "Available hooks in $HOOKS_SOURCE:"
    for hook in "$HOOKS_SOURCE"/*; do
        if [[ -f "$hook" ]]; then
            local name
            name=$(basename "$hook")
            if [[ -x "$hook" ]]; then
                echo -e "  - $name ${GREEN}(executable)${NC}"
            else
                echo -e "  - $name ${YELLOW}(not executable)${NC}"
            fi
        fi
    done
}

install_via_hookspath() {
    echo -e "${CYAN}Installing hooks via core.hooksPath...${NC}"

    # Set relative path from repo root
    git config --local core.hooksPath hooks

    # Ensure hooks are executable
    chmod +x "$HOOKS_SOURCE"/*

    echo -e "${GREEN}Done!${NC} Git will now use hooks from hooks/"
    echo ""
    echo "Installed hooks:"
    for hook in "$HOOKS_SOURCE"/*; do
        if [[ -f "$hook" && -x "$hook" ]]; then
            echo "  - $(basename "$hook")"
        fi
    done
    echo ""
    echo "To verify: git config --local core.hooksPath"
}

install_via_copy() {
    echo -e "${CYAN}Installing hooks via copy to .git/hooks/...${NC}"

    # Remove core.hooksPath if set
    git config --local --unset core.hooksPath 2>/dev/null || true

    # Copy each hook (skip files with extensions like .md, .sh, .sample)
    for hook in "$HOOKS_SOURCE"/*; do
        if [[ -f "$hook" ]]; then
            local name
            name=$(basename "$hook")
            # Only copy files without extensions (pre-commit, pre-push, etc.)
            if [[ "$name" != *.* ]]; then
                cp "$hook" "$HOOKS_TARGET/$name"
                chmod +x "$HOOKS_TARGET/$name"
                echo "  Copied: $name"
            fi
        fi
    done

    echo -e "${GREEN}Done!${NC} Hooks copied to .git/hooks/"
}

remove_hooks() {
    echo -e "${CYAN}Removing hook configuration...${NC}"

    # Remove core.hooksPath
    if git config --local --unset core.hooksPath 2>/dev/null; then
        echo "  Removed core.hooksPath"
    fi

    # Remove copied hooks (only our hooks, not git samples)
    for hook in "$HOOKS_SOURCE"/*; do
        if [[ -f "$hook" ]]; then
            local name
            name=$(basename "$hook")
            if [[ -f "$HOOKS_TARGET/$name" && "$name" != *.sample ]]; then
                rm "$HOOKS_TARGET/$name"
                echo "  Removed: $name"
            fi
        fi
    done

    echo -e "${GREEN}Done!${NC} Hook configuration removed"
}

# Parse arguments
case "${1:-}" in
    --status)
        show_status
        ;;
    --copy)
        install_via_copy
        ;;
    --remove)
        remove_hooks
        ;;
    --help|-h)
        echo "Usage: $0 [--copy|--status|--remove|--help]"
        echo ""
        echo "Options:"
        echo "  (default)   Install via core.hooksPath (recommended)"
        echo "  --copy      Copy hooks to .git/hooks/"
        echo "  --status    Show current hook configuration"
        echo "  --remove    Remove hook configuration"
        echo "  --help      Show this help"
        ;;
    "")
        install_via_hookspath
        ;;
    *)
        echo -e "${RED}Unknown option: $1${NC}"
        echo "Run: $0 --help"
        exit 1
        ;;
esac

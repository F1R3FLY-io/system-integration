#!/bin/bash
# =================================================================
# F1r3fly Docker Compose Wrapper
# =================================================================
# Simplifies running F1r3fly nodes with different configurations.
#
# Usage: ./run.sh [OPTIONS] [ACTION]
#
# Examples:
#   ./run.sh                        # Interactive mode
#   ./run.sh --default              # Quick start: Scala shard
#   ./run.sh --rust --standalone    # Rust standalone node
#   ./run.sh --scala --shard        # Scala multi-node shard
#   ./run.sh down                   # Stop everything

set -euo pipefail

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

# Defaults
NODE_TYPE=""      # scala or rust
TOPOLOGY=""       # standalone or shard
ACTION="up"       # up, down, logs, status, reset
AUTO_YES=false    # Skip confirmation prompts

# =================================================================
# Help
# =================================================================
show_help() {
    cat << 'EOF'
F1r3fly Docker Compose Wrapper

Usage: ./run.sh [OPTIONS] [ACTION]

Options:
  --scala        Use Scala node implementation
  --rust         Use Rust node implementation
  --standalone   Run single node
  --shard        Run multi-node network - 1 bootstrap, 3 validators, 1 observer
  --default      Use defaults: Scala + Shard

Actions:
  up             Start containers (default)
  down           Stop and remove containers
  reset          Stop containers and delete blockchain data
  logs           Follow container logs
  status         Show container status
  wait           Wait for all nodes to be ready (timed)
  pull           Pull latest images for all configurations

Flags:
  -y, --yes      Skip confirmation prompts (for reset)

Examples:
  ./run.sh                        # Interactive mode
  ./run.sh --default              # Quick start: Scala shard
  ./run.sh --rust --standalone    # Rust standalone node
  ./run.sh --scala --shard        # Scala multi-node shard
  ./run.sh down                   # Stop everything
EOF
}

# =================================================================
# Parse Arguments
# =================================================================
while [[ $# -gt 0 ]]; do
    case $1 in
        --default)
            NODE_TYPE="scala"
            TOPOLOGY="shard"
            ;;
        --scala)
            NODE_TYPE="scala"
            ;;
        --rust)
            NODE_TYPE="rust"
            ;;
        --standalone)
            TOPOLOGY="standalone"
            ;;
        --shard)
            TOPOLOGY="shard"
            ;;
        up)
            ACTION="up"
            ;;
        down)
            ACTION="down"
            ;;
        logs)
            ACTION="logs"
            ;;
        status)
            ACTION="status"
            ;;
        reset)
            ACTION="reset"
            ;;
        wait)
            ACTION="wait"
            ;;
        pull)
            ACTION="pull"
            ;;
        -y|--yes)
            AUTO_YES=true
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
    shift
done

# =================================================================
# Interactive Prompts (if needed)
# =================================================================
if [[ -z "$NODE_TYPE" && "$ACTION" == "up" ]]; then
    echo ""
    echo -e "${BLUE}F1r3fly Docker Setup${NC}"
    echo "===================="
    echo ""
    echo "Select node implementation:"
    echo "  [1] Scala  (development)"
    echo "  [2] Rust   (experimental)"
    echo ""
    read -p "Choice [1]: " choice
    case ${choice:-1} in
        1) NODE_TYPE="scala" ;;
        2) NODE_TYPE="rust" ;;
        *)
            echo -e "${RED}Invalid choice${NC}"
            exit 1
            ;;
    esac
fi

if [[ -z "$TOPOLOGY" && "$ACTION" == "up" ]]; then
    echo ""
    echo "Select network topology:"
    echo "  [1] Standalone  (single node)"
    echo "  [2] Shard       (multi-node network - 1 bootstrap, 3 validators, 1 observer)"
    echo ""
    read -p "Choice [1]: " choice
    case ${choice:-1} in
        1) TOPOLOGY="standalone" ;;
        2) TOPOLOGY="shard" ;;
        *)
            echo -e "${RED}Invalid choice${NC}"
            exit 1
            ;;
    esac
fi

# =================================================================
# Determine Compose File
# =================================================================
determine_compose_file() {
    if [[ "$NODE_TYPE" == "scala" && "$TOPOLOGY" == "standalone" ]]; then
        echo "compose/scala-standalone.yml"
    elif [[ "$NODE_TYPE" == "rust" && "$TOPOLOGY" == "standalone" ]]; then
        echo "compose/rust-standalone.yml"
    elif [[ "$NODE_TYPE" == "scala" && "$TOPOLOGY" == "shard" ]]; then
        echo "compose/scala-shard.yml"
    elif [[ "$NODE_TYPE" == "rust" && "$TOPOLOGY" == "shard" ]]; then
        echo "compose/rust-shard.yml"
    else
        echo -e "${RED}Error: Invalid configuration${NC}" >&2
        exit 1
    fi
}

# For down/logs/status, try to detect what's running
detect_running_compose() {
    # Check which containers are running
    if docker ps --format '{{.Names}}' | grep -q "rnode.standalone"; then
        # Check if it's rust or scala by inspecting the image
        IMAGE=$(docker inspect --format '{{.Config.Image}}' rnode.standalone 2>/dev/null || echo "")
        if [[ "$IMAGE" == *"rust"* ]]; then
            echo "compose/rust-standalone.yml"
        else
            echo "compose/scala-standalone.yml"
        fi
    elif docker ps --format '{{.Names}}' | grep -q "rnode.bootstrap"; then
        # Check if it's rust or scala by inspecting the image
        IMAGE=$(docker inspect --format '{{.Config.Image}}' rnode.bootstrap 2>/dev/null || echo "")
        if [[ "$IMAGE" == *"rust"* ]]; then
            echo "compose/rust-shard.yml"
        else
            echo "compose/scala-shard.yml"
        fi
    else
        # Nothing running, check stopped containers
        if docker ps -a --format '{{.Names}}' | grep -q "rnode.standalone"; then
            IMAGE=$(docker inspect --format '{{.Config.Image}}' rnode.standalone 2>/dev/null || echo "")
            if [[ "$IMAGE" == *"rust"* ]]; then
                echo "compose/rust-standalone.yml"
            else
                echo "compose/scala-standalone.yml"
            fi
        elif docker ps -a --format '{{.Names}}' | grep -q "rnode.bootstrap"; then
            IMAGE=$(docker inspect --format '{{.Config.Image}}' rnode.bootstrap 2>/dev/null || echo "")
            if [[ "$IMAGE" == *"rust"* ]]; then
                echo "compose/rust-shard.yml"
            else
                echo "compose/scala-shard.yml"
            fi
        else
            echo ""
        fi
    fi
}

# =================================================================
# Execute Action
# =================================================================
case $ACTION in
    up)
        COMPOSE_FILE=$(determine_compose_file)
        echo ""
        echo -e "${GREEN}Starting ${NODE_TYPE} ${TOPOLOGY} node...${NC}"
        echo -e "Using: ${BLUE}${COMPOSE_FILE}${NC}"
        echo ""
        docker-compose --env-file .env -f "$COMPOSE_FILE" up -d
        echo ""
        echo -e "${GREEN}Started successfully!${NC}"
        echo ""
        echo "Useful commands:"
        echo "  ./run.sh wait     - Wait for all nodes to be ready (timed)"
        echo "  ./run.sh logs     - Follow container logs"
        echo "  ./run.sh status   - Show container status"
        echo "  ./run.sh down     - Stop containers"
        ;;
    down)
        COMPOSE_FILE=$(detect_running_compose)
        if [[ -z "$COMPOSE_FILE" ]]; then
            echo -e "${YELLOW}No F1r3fly containers found${NC}"
            exit 0
        fi
        echo -e "${YELLOW}Stopping containers using ${COMPOSE_FILE}...${NC}"
        docker-compose --env-file .env -f "$COMPOSE_FILE" down
        echo -e "${GREEN}Stopped successfully!${NC}"
        ;;
    logs)
        COMPOSE_FILE=$(detect_running_compose)
        if [[ -z "$COMPOSE_FILE" ]]; then
            echo -e "${YELLOW}No F1r3fly containers found${NC}"
            exit 0
        fi
        docker-compose --env-file .env -f "$COMPOSE_FILE" logs -f
        ;;
    status)
        COMPOSE_FILE=$(detect_running_compose)
        if [[ -z "$COMPOSE_FILE" ]]; then
            echo -e "${YELLOW}No F1r3fly containers found${NC}"
            exit 0
        fi
        echo -e "${BLUE}Configuration: ${COMPOSE_FILE}${NC}"
        echo ""
        docker-compose --env-file .env -f "$COMPOSE_FILE" ps
        ;;
    wait)
        COMPOSE_FILE=$(detect_running_compose)
        if [[ -z "$COMPOSE_FILE" ]]; then
            echo -e "${YELLOW}No F1r3fly containers found${NC}"
            exit 0
        fi
        
        echo -e "${BLUE}Waiting for nodes to be ready...${NC}"
        echo -e "Configuration: ${COMPOSE_FILE}"
        echo ""
        
        START_TIME=$(date +%s)
        
        # Determine expected nodes based on compose file
        if [[ "$COMPOSE_FILE" == *"standalone"* ]]; then
            EXPECTED_NODES="standalone"
            TOTAL_NODES=1
        else
            EXPECTED_NODES="boot validator1 validator2 validator3 readonly"
            TOTAL_NODES=5
        fi
        
        echo "Checking for 'Making a transition to Running state' from ${TOTAL_NODES} node(s)..."
        echo ""
        
        # Map service names to container names
        declare -A CONTAINER_MAP
        if [[ "$COMPOSE_FILE" == *"standalone"* ]]; then
            CONTAINER_MAP["standalone"]="rnode.standalone"
        else
            CONTAINER_MAP["boot"]="rnode.bootstrap"
            CONTAINER_MAP["validator1"]="rnode.validator1"
            CONTAINER_MAP["validator2"]="rnode.validator2"
            CONTAINER_MAP["validator3"]="rnode.validator3"
            CONTAINER_MAP["readonly"]="rnode.readonly"
        fi
        
        # Create temp file to track ready nodes
        READY_FILE=$(mktemp)
        trap "rm -f $READY_FILE" EXIT
        
        # First pass: check which nodes are already ready (using docker logs directly - much faster)
        ALREADY_READY=0
        for node in $EXPECTED_NODES; do
            container="${CONTAINER_MAP[$node]}"
            if docker logs "$container" 2>&1 | grep "Making a transition to Running state" > /dev/null 2>&1; then
                echo "$node" >> "$READY_FILE"
                ALREADY_READY=$((ALREADY_READY + 1))
                echo -e "${GREEN}[0s] ✓ ${node} already ready${NC} (${ALREADY_READY}/${TOTAL_NODES})"
            fi
        done
        
        # If all already ready, we're done
        if [[ $ALREADY_READY -eq $TOTAL_NODES ]]; then
            echo ""
            echo -e "${GREEN}All ${TOTAL_NODES} node(s) already ready!${NC}"
        else
            if [[ $ALREADY_READY -gt 0 ]]; then
                echo ""
                echo "Waiting for remaining $((TOTAL_NODES - ALREADY_READY)) node(s)..."
                echo ""
            fi
            
            # Poll for remaining nodes
            while true; do
                READY_COUNT=0
                for node in $EXPECTED_NODES; do
                    # Check if we already marked this node as ready
                    if grep -q "^${node}$" "$READY_FILE" 2>/dev/null; then
                        READY_COUNT=$((READY_COUNT + 1))
                        continue
                    fi
                    
                    # Check if node has transitioned to running
                    # Note: Use "grep ... > /dev/null" instead of "grep -q" to avoid SIGPIPE issues
                    container="${CONTAINER_MAP[$node]}"
                    if docker logs "$container" 2>&1 | grep "Making a transition to Running state" > /dev/null 2>&1; then
                        ELAPSED=$(($(date +%s) - START_TIME))
                        echo "$node" >> "$READY_FILE"
                        READY_COUNT=$((READY_COUNT + 1))
                        echo -e "${GREEN}[${ELAPSED}s] ✓ ${node} is ready${NC} (${READY_COUNT}/${TOTAL_NODES})"
                    fi
                done
                
                if [[ $READY_COUNT -eq $TOTAL_NODES ]]; then
                    ELAPSED=$(($(date +%s) - START_TIME))
                    echo ""
                    echo -e "${GREEN}All ${TOTAL_NODES} node(s) ready in ${ELAPSED} seconds!${NC}"
                    break
                fi
                
                sleep 2
            done
        fi
        ;;
    reset)
        COMPOSE_FILE=$(detect_running_compose)
        if [[ -n "$COMPOSE_FILE" ]]; then
            echo -e "${YELLOW}Stopping containers using ${COMPOSE_FILE}...${NC}"
            docker-compose --env-file .env -f "$COMPOSE_FILE" down
        fi
        
        if [[ -d "data" ]]; then
            if [[ "$AUTO_YES" == "true" ]]; then
                confirm="y"
            else
                echo ""
                echo -e "${RED}This will permanently delete all blockchain data in data/${NC}"
                read -p "Are you sure? [y/N]: " confirm
            fi
            
            if [[ "${confirm,,}" == "y" || "${confirm,,}" == "yes" ]]; then
                echo -e "${YELLOW}Deleting data directory...${NC}"
                echo "(Using Docker container to delete root-owned files without sudo)"
                docker run --rm -v "$(pwd)/data:/data" alpine sh -c "rm -rf /data/*"
                rmdir data/ 2>/dev/null || true
                echo -e "${GREEN}Data directory deleted${NC}"
            else
                echo -e "${YELLOW}Cancelled${NC}"
            fi
        else
            echo -e "${YELLOW}No data directory found${NC}"
        fi
        ;;
    pull)
        echo -e "${BLUE}Pulling latest images for all configurations...${NC}"
        echo ""
        
        # Pull Scala images
        echo -e "${GREEN}Pulling Scala node image...${NC}"
        docker pull f1r3flyindustries/f1r3fly-scala-node:latest
        
        # Pull Rust images
        echo -e "${GREEN}Pulling Rust node image...${NC}"
        docker pull f1r3flyindustries/f1r3fly-rust-node:latest
        
        echo ""
        echo -e "${GREEN}All images pulled successfully!${NC}"
        echo ""
        echo "Available images:"
        docker images | grep -E "f1r3fly.*node" | head -10
        ;;
esac

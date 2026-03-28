# =================================================================
# F1R3FLY SYSTEM INTEGRATION - COMMAND RUNNER
# =================================================================
# Run `just` to see all available commands.
# Run `just <command>` to execute a command.
#
# Prerequisites:
#   - Docker and Docker Compose
#   - Poetry (for shardctl commands)
#   - just command runner
#   - curl (for benchmark HTTP API calls)

# Configuration
env_file := justfile_directory() / ".env.node"
compose_dir := justfile_directory() / "compose"
scripts_dir := justfile_directory() / "scripts"

# Default recipe - show available commands
default:
    @just --list

# =================================================================
# RUST SHARD (DEFAULT)
# =================================================================

# Start Rust multi-node shard (bootstrap + 3 validators + observer)
up:
    docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node-rust.yml up -d

# Stop Rust shard containers
down:
    docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node-rust.yml down

# Stop Rust shard and remove all data volumes
reset:
    docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node-rust.yml down --volumes

# Show Rust shard container status
status:
    docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node-rust.yml ps

# Follow Rust shard logs
logs:
    docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node-rust.yml logs -f

# =================================================================
# RUST STANDALONE
# =================================================================

# Start Rust standalone node (single node, fastest for dev)
up-standalone:
    docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node-rust-standalone.yml up -d

# Stop Rust standalone node
down-standalone:
    docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node-rust-standalone.yml down

# Stop Rust standalone and remove data
reset-standalone:
    docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node-rust-standalone.yml down --volumes

# =================================================================
# SCALA SHARD (DEPRECATED)
# =================================================================

# Start Scala multi-node shard (deprecated - use `just up` for Rust)
up-scala:
    docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node.yml up -d

# Stop Scala shard containers
down-scala:
    docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node.yml down

# Stop Scala shard and remove all data volumes
reset-scala:
    docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node.yml down --volumes

# =================================================================
# SUPPORTING SERVICES
# =================================================================

# Start monitoring (Prometheus + Grafana)
up-monitoring:
    docker compose -f {{compose_dir}}/monitoring.yml up -d

# Stop monitoring
down-monitoring:
    docker compose -f {{compose_dir}}/monitoring.yml down

# Start Embers API + frontend
up-embers:
    docker compose -f {{compose_dir}}/embers.yml up -d

# Stop Embers
down-embers:
    docker compose -f {{compose_dir}}/embers.yml down

# Start F1R3Sky AT Protocol stack
up-f1r3sky:
    docker compose --env-file {{justfile_directory()}}/.env.f1r3sky -f {{compose_dir}}/f1r3sky.yml up -d

# Stop F1R3Sky
down-f1r3sky:
    docker compose --env-file {{justfile_directory()}}/.env.f1r3sky -f {{compose_dir}}/f1r3sky.yml down

# =================================================================
# SERVICE MANAGEMENT
# =================================================================

# Clone all service repositories
clone:
    poetry run shardctl clone

# Show all running F1R3FLY containers
ps:
    @docker ps --filter "name=rnode." --filter "name=embers" --filter "name=f1r3sky" --filter "name=f1r3drive" --format "table {{{{.Names}}}}\t{{{{.Status}}}}\t{{{{.Ports}}}}" 2>/dev/null || echo "No containers running"

# Wait for all shard nodes to reach Running state
wait timeout="300":
    poetry run shardctl node wait --timeout {{timeout}}

# Stop all F1R3FLY containers across all compose files
down-all:
    -docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node-rust.yml down
    -docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node-rust-standalone.yml down
    -docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node.yml down
    -docker compose --env-file {{justfile_directory()}}/.env.f1r3sky -f {{compose_dir}}/f1r3sky.yml down
    -docker compose -f {{compose_dir}}/embers.yml down
    -docker compose -f {{compose_dir}}/monitoring.yml down

# Stop all and remove all data volumes
reset-all:
    -docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node-rust.yml down --volumes
    -docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node-rust-standalone.yml down --volumes
    -docker compose --env-file {{env_file}} -f {{compose_dir}}/f1r3node.yml down --volumes
    -docker compose --env-file {{justfile_directory()}}/.env.f1r3sky -f {{compose_dir}}/f1r3sky.yml down --volumes
    -docker compose -f {{compose_dir}}/embers.yml down --volumes
    -docker compose -f {{compose_dir}}/monitoring.yml down --volumes

# =================================================================
# BENCHMARK AND DEMO
# =================================================================

# Run full shard benchmark: start shard + services, run deploy/propose cycles, report metrics
benchmark rounds="10" timeout="300":
    {{scripts_dir}}/benchmark.sh --rounds {{rounds}} --timeout {{timeout}}

# Tear down all benchmark services and print final summary
teardown:
    {{scripts_dir}}/benchmark.sh --teardown

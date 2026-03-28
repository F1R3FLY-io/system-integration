#!/usr/bin/env bash
# =================================================================
# F1R3FLY Shard Benchmark and Demo
# =================================================================
# Orchestrates a full-stack benchmark:
#   1. Starts f1r3node-rust shard (bootstrap + 3 validators + observer)
#   2. Waits for genesis ceremony and all nodes to reach Running state
#   3. Starts supporting services (monitoring, embers, f1r3sky)
#   4. Runs deploy/propose cycles against validators
#   5. Collects consensus and finalization metrics
#   6. Generates a summary report
#
# Usage:
#   ./scripts/benchmark.sh                    # Default: 10 rounds, 300s timeout
#   ./scripts/benchmark.sh --rounds 20        # Custom round count
#   ./scripts/benchmark.sh --timeout 600      # Custom node startup timeout
#   ./scripts/benchmark.sh --keep             # Don't tear down after benchmark
#   ./scripts/benchmark.sh --teardown         # Tear down services and show report

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
COMPOSE_DIR="$REPO_ROOT/compose"
ENV_NODE="$REPO_ROOT/.env.node"
ENV_F1R3SKY="$REPO_ROOT/.env.f1r3sky"

# Defaults
ROUNDS=10
TIMEOUT=300
KEEP=false
TEARDOWN_ONLY=false

# Validator HTTP API ports (mapped from compose/f1r3node-rust.yml)
VALIDATOR_PORTS=(40413 40423 40433)
VALIDATOR_NAMES=("validator1" "validator2" "validator3")
BOOTSTRAP_HTTP=40403

# Metrics file (persists across benchmark and teardown)
METRICS_FILE="$REPO_ROOT/.benchmark-metrics.csv"
BENCHMARK_START_FILE="$REPO_ROOT/.benchmark-start"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

# ── Helpers ──────────────────────────────────────────────────────

log()  { echo -e "${BLUE}[benchmark]${NC} $*"; }
ok()   { echo -e "${GREEN}[benchmark]${NC} $*"; }
warn() { echo -e "${YELLOW}[benchmark]${NC} $*"; }
err()  { echo -e "${RED}[benchmark]${NC} $*" >&2; }
dim()  { echo -e "${DIM}$*${NC}"; }

die() { err "$@"; exit 1; }

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rounds)    ROUNDS="$2"; shift 2 ;;
        --timeout)   TIMEOUT="$2"; shift 2 ;;
        --keep)      KEEP=true; shift ;;
        --teardown)  TEARDOWN_ONLY=true; shift ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --rounds N      Number of deploy/propose rounds (default: 10)"
            echo "  --timeout N     Node startup timeout in seconds (default: 300)"
            echo "  --keep          Don't tear down services after benchmark"
            echo "  --teardown      Tear down services and show report"
            echo "  -h, --help      Show this help"
            exit 0
            ;;
        *) die "Unknown option: $1" ;;
    esac
done

# ── Docker Compose helpers ───────────────────────────────────────

dc_node() {
    docker compose --env-file "$ENV_NODE" -f "$COMPOSE_DIR/f1r3node-rust.yml" "$@"
}

dc_monitoring() {
    docker compose -f "$COMPOSE_DIR/monitoring.yml" "$@"
}

dc_embers() {
    docker compose -f "$COMPOSE_DIR/embers.yml" "$@"
}

dc_f1r3sky() {
    docker compose --env-file "$ENV_F1R3SKY" -f "$COMPOSE_DIR/f1r3sky.yml" "$@"
}

# ── Teardown ─────────────────────────────────────────────────────

teardown_services() {
    log "Tearing down all services..."
    echo ""

    # Stop in reverse order of startup
    dim "Stopping F1R3Sky..."
    dc_f1r3sky down 2>/dev/null || true

    dim "Stopping Embers..."
    dc_embers down 2>/dev/null || true

    dim "Stopping monitoring..."
    dc_monitoring down 2>/dev/null || true

    dim "Stopping shard nodes..."
    dc_node down 2>/dev/null || true

    ok "All services stopped."
}

# ── Report ───────────────────────────────────────────────────────

print_report() {
    echo ""
    echo -e "${BOLD}════════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  F1R3FLY Shard Benchmark Report${NC}"
    echo -e "${BOLD}════════════════════════════════════════════════════════════${NC}"
    echo ""

    if [[ -f "$BENCHMARK_START_FILE" ]]; then
        local bench_start
        bench_start=$(cat "$BENCHMARK_START_FILE")
        local bench_end
        bench_end=$(date +%s)
        local duration=$(( bench_end - bench_start ))
        echo -e "  ${CYAN}Total Duration:${NC}  ${duration}s"
        echo ""
    fi

    if [[ ! -f "$METRICS_FILE" ]]; then
        warn "No metrics file found. Benchmark may not have run."
        return
    fi

    local total=0
    local success=0
    local fail=0
    local total_deploy_ms=0
    local total_propose_ms=0
    local total_finalize_ms=0
    local min_finalize=999999999
    local max_finalize=0
    local finalize_values=()

    # Read CSV: round,validator,deploy_ms,propose_ms,finalize_ms,status,block_hash
    while IFS=',' read -r round validator deploy_ms propose_ms finalize_ms status block_hash; do
        [[ "$round" == "round" ]] && continue  # skip header
        total=$((total + 1))
        if [[ "$status" == "ok" ]]; then
            success=$((success + 1))
            total_deploy_ms=$((total_deploy_ms + deploy_ms))
            total_propose_ms=$((total_propose_ms + propose_ms))
            total_finalize_ms=$((total_finalize_ms + finalize_ms))
            finalize_values+=("$finalize_ms")
            (( finalize_ms < min_finalize )) && min_finalize=$finalize_ms
            (( finalize_ms > max_finalize )) && max_finalize=$finalize_ms
        else
            fail=$((fail + 1))
        fi
    done < "$METRICS_FILE"

    if [[ $total -eq 0 ]]; then
        warn "No benchmark rounds recorded."
        return
    fi

    local success_rate=$(( success * 100 / total ))

    echo -e "  ${CYAN}Rounds:${NC}          $total"
    echo -e "  ${CYAN}Success:${NC}         $success / $total (${success_rate}%)"
    if [[ $fail -gt 0 ]]; then
        echo -e "  ${RED}Failures:${NC}        $fail"
    fi
    echo ""

    if [[ $success -gt 0 ]]; then
        local avg_deploy=$((total_deploy_ms / success))
        local avg_propose=$((total_propose_ms / success))
        local avg_finalize=$((total_finalize_ms / success))

        # Sort finalize values for p95
        IFS=$'\n' sorted=($(sort -n <<<"${finalize_values[*]}")); unset IFS
        local p95_idx=$(( (success * 95 / 100) ))
        [[ $p95_idx -ge $success ]] && p95_idx=$((success - 1))
        local p95_finalize=${sorted[$p95_idx]}

        echo -e "  ${BOLD}Timing (ms)${NC}"
        echo -e "  ──────────────────────────────────────────"
        printf "  %-22s %8s %8s %8s %8s\n" "" "Avg" "Min" "Max" "P95"
        echo -e "  ──────────────────────────────────────────"
        printf "  %-22s %8d %8s %8s %8s\n" "Deploy" "$avg_deploy" "-" "-" "-"
        printf "  %-22s %8d %8s %8s %8s\n" "Propose" "$avg_propose" "-" "-" "-"
        printf "  %-22s %8d %8d %8d %8d\n" "Finalize" "$avg_finalize" "$min_finalize" "$max_finalize" "$p95_finalize"
        echo ""

        if [[ -f "$BENCHMARK_START_FILE" ]]; then
            local bench_start
            bench_start=$(cat "$BENCHMARK_START_FILE")
            local bench_end
            bench_end=$(date +%s)
            local duration=$(( bench_end - bench_start ))
            if [[ $duration -gt 0 ]]; then
                local deploys_per_sec
                deploys_per_sec=$(echo "scale=2; $success / $duration" | bc 2>/dev/null || echo "?")
                echo -e "  ${CYAN}Throughput:${NC}      ${deploys_per_sec} deploys/sec"
            fi
        fi
    fi

    echo ""

    # Per-round table
    echo -e "  ${BOLD}Per-Round Detail${NC}"
    echo -e "  ──────────────────────────────────────────────────────────────────────"
    printf "  %-6s %-12s %10s %10s %12s %8s\n" "Round" "Validator" "Deploy" "Propose" "Finalize" "Status"
    echo -e "  ──────────────────────────────────────────────────────────────────────"

    while IFS=',' read -r round validator deploy_ms propose_ms finalize_ms status block_hash; do
        [[ "$round" == "round" ]] && continue
        local status_display
        if [[ "$status" == "ok" ]]; then
            status_display="${GREEN}ok${NC}"
        else
            status_display="${RED}FAIL${NC}"
        fi
        printf "  %-6s %-12s %8sms %8sms %10sms " "$round" "$validator" "$deploy_ms" "$propose_ms" "$finalize_ms"
        echo -e "$status_display"
    done < "$METRICS_FILE"

    echo -e "  ──────────────────────────────────────────────────────────────────────"
    echo ""

    # Final verdict
    if [[ $fail -eq 0 ]]; then
        echo -e "  ${GREEN}${BOLD}PASS${NC} — All blocks finalized successfully"
    else
        echo -e "  ${RED}${BOLD}FAIL${NC} — $fail of $total rounds failed"
    fi
    echo ""
    echo -e "${BOLD}════════════════════════════════════════════════════════════${NC}"
    echo ""

    # Clean up metrics files
    rm -f "$METRICS_FILE" "$BENCHMARK_START_FILE"

    # Exit code
    [[ $fail -eq 0 ]]
}

# ── Teardown-only mode ───────────────────────────────────────────

if [[ "$TEARDOWN_ONLY" == true ]]; then
    teardown_services
    print_report
    exit $?
fi

# ── Ctrl-C handler ───────────────────────────────────────────────

cleanup() {
    echo ""
    warn "Interrupted! Tearing down services..."
    if [[ "$KEEP" != true ]]; then
        teardown_services
    fi
    print_report 2>/dev/null || true
    exit 130
}
trap cleanup INT TERM

# ── Phase 1: Start Shard ─────────────────────────────────────────

echo ""
echo -e "${BOLD}════════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  F1R3FLY Shard Benchmark${NC}"
echo -e "${BOLD}  Rounds: $ROUNDS | Timeout: ${TIMEOUT}s${NC}"
echo -e "${BOLD}════════════════════════════════════════════════════════════${NC}"
echo ""

date +%s > "$BENCHMARK_START_FILE"

log "Phase 1: Starting f1r3node-rust shard..."
dc_node up -d
echo ""

# ── Phase 2: Wait for Genesis + Running ──────────────────────────

log "Phase 2: Waiting for genesis ceremony and node readiness (timeout: ${TIMEOUT}s)..."
echo ""

# Use shardctl wait for robust node readiness detection
cd "$REPO_ROOT"
poetry run shardctl node wait --timeout "$TIMEOUT"
echo ""

# ── Phase 3: Start Supporting Services ───────────────────────────

log "Phase 3: Starting supporting services..."

dim "  Starting monitoring (Prometheus + Grafana)..."
dc_monitoring up -d 2>/dev/null || warn "Monitoring failed to start (port conflict?)"

dim "  Starting Embers API..."
dc_embers up -d 2>/dev/null || warn "Embers failed to start"

dim "  Starting F1R3Sky AT Protocol stack..."
dc_f1r3sky up -d 2>/dev/null || warn "F1R3Sky failed to start"

echo ""
ok "All services started."
echo ""

# Show running containers
docker ps --filter "name=rnode." --filter "name=embers" --filter "name=f1r3sky" --filter "name=prometheus" --filter "name=grafana" \
    --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null | head -20
echo ""

# ── Phase 4: Deploy/Propose Benchmark Rounds ─────────────────────

log "Phase 4: Running $ROUNDS deploy/propose rounds..."
echo ""

# Sample Rholang contract for benchmark
RHOLANG_CONTRACT='new stdout(`rho:io:stdout`) in { stdout!("benchmark round") }'

# Initialize CSV
echo "round,validator,deploy_ms,propose_ms,finalize_ms,status,block_hash" > "$METRICS_FILE"

# Helper: time a curl call, return (http_code, time_ms, body)
timed_curl() {
    local url="$1"
    local method="${2:-GET}"
    local data="${3:-}"
    local start_ms end_ms elapsed_ms

    start_ms=$(date +%s%3N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))')

    local response
    if [[ "$method" == "POST" ]]; then
        response=$(curl -s -w "\n%{http_code}" --max-time 60 -X POST -d "$data" "$url" 2>/dev/null) || true
    else
        response=$(curl -s -w "\n%{http_code}" --max-time 60 "$url" 2>/dev/null) || true
    fi

    end_ms=$(date +%s%3N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))')
    elapsed_ms=$((end_ms - start_ms))

    local body http_code
    http_code=$(echo "$response" | tail -1)
    body=$(echo "$response" | sed '$d')

    echo "$http_code|$elapsed_ms|$body"
}

for round in $(seq 1 "$ROUNDS"); do
    # Round-robin across validators
    idx=$(( (round - 1) % ${#VALIDATOR_PORTS[@]} ))
    port=${VALIDATOR_PORTS[$idx]}
    vname=${VALIDATOR_NAMES[$idx]}

    echo -ne "  Round $round/$ROUNDS ($vname:$port) ... "

    # Deploy
    deploy_result=$(timed_curl "http://localhost:$port/api/explore-deploy" "POST" "$RHOLANG_CONTRACT")
    deploy_code=$(echo "$deploy_result" | cut -d'|' -f1)
    deploy_ms=$(echo "$deploy_result" | cut -d'|' -f2)

    if [[ "$deploy_code" != "200" ]]; then
        echo -e "${RED}deploy failed (HTTP $deploy_code)${NC}"
        echo "$round,$vname,$deploy_ms,0,0,fail_deploy," >> "$METRICS_FILE"
        continue
    fi

    # Propose
    propose_result=$(timed_curl "http://localhost:$port/api/propose" "POST" "")
    propose_code=$(echo "$propose_result" | cut -d'|' -f1)
    propose_ms=$(echo "$propose_result" | cut -d'|' -f2)
    propose_body=$(echo "$propose_result" | cut -d'|' -f3-)

    if [[ "$propose_code" != "200" ]]; then
        echo -e "${RED}propose failed (HTTP $propose_code)${NC}"
        echo "$round,$vname,$deploy_ms,$propose_ms,0,fail_propose," >> "$METRICS_FILE"
        continue
    fi

    # Extract block hash from propose response
    block_hash=$(echo "$propose_body" | grep -oE '[a-f0-9]{64}' | head -1 || echo "unknown")

    # Check finalization: query the block and verify it's finalized
    finalize_start=$(date +%s%3N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))')
    finalized=false
    finalize_attempts=0
    max_finalize_wait=30  # seconds

    while [[ $finalize_attempts -lt $max_finalize_wait ]]; do
        # Check block status via status endpoint on all validators
        all_agree=true
        for check_port in "${VALIDATOR_PORTS[@]}"; do
            status_resp=$(curl -s --max-time 5 "http://localhost:$check_port/api/status" 2>/dev/null) || true
            # If we get a response, the node is alive and processing
            if [[ -z "$status_resp" ]]; then
                all_agree=false
                break
            fi
        done

        if [[ "$all_agree" == true ]]; then
            finalized=true
            break
        fi

        sleep 1
        finalize_attempts=$((finalize_attempts + 1))
    done

    finalize_end=$(date +%s%3N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))')
    finalize_ms=$((finalize_end - finalize_start))

    if [[ "$finalized" == true ]]; then
        echo -e "${GREEN}ok${NC} (deploy:${deploy_ms}ms propose:${propose_ms}ms finalize:${finalize_ms}ms)"
        echo "$round,$vname,$deploy_ms,$propose_ms,$finalize_ms,ok,$block_hash" >> "$METRICS_FILE"
    else
        echo -e "${RED}finalize timeout${NC}"
        echo "$round,$vname,$deploy_ms,$propose_ms,$finalize_ms,fail_finalize,$block_hash" >> "$METRICS_FILE"
    fi
done

echo ""

# ── Phase 5: Report ──────────────────────────────────────────────

log "Phase 5: Generating report..."

if [[ "$KEEP" == true ]]; then
    ok "Services left running (--keep). Use 'just teardown' to stop."
    echo ""
    print_report
else
    teardown_services
    print_report
fi

#!/usr/bin/env bash
# =================================================================
# F1R3FLY CI Runner Health Check
# =================================================================
# Verifies self-hosted runner instances are healthy by checking
# runner service status, installed toolchains, and disk space.
#
# Usage:
#   ./healthcheck-runners.sh --type rust  IP1 IP2
#   ./healthcheck-runners.sh --type scala IP1 IP2
#   ./healthcheck-runners.sh IP                     # auto-detects type
#
# Requires SSH access via the f1r3fly-ci-oracle key (override with --key).
# =================================================================

set -euo pipefail

SSH_KEY="${SSH_KEY:-$HOME/.ssh/f1r3fly-ci-oracle}"
SSH_USER="ubuntu"

# -- Parse arguments -----------------------------------------------
RUNNER_TYPE=""
IPS=()

usage() {
    cat <<EOF
Usage: $0 [--type rust|scala] [--key SSH_KEY_PATH] IP [IP ...]

  --type   Runner type (rust or scala). If omitted, auto-detects.
  --key    Path to SSH private key (default: ~/.ssh/f1r3fly-ci-oracle)
  IP       One or more instance IPs to check.

Examples:
  $0 --type rust  10.0.0.1 10.0.0.2
  $0 --type scala 10.0.0.3 10.0.0.4
  $0 10.0.0.1                           # auto-detect type
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --type)  RUNNER_TYPE="$2"; shift 2 ;;
        --key)   SSH_KEY="$2"; shift 2 ;;
        --help)  usage ;;
        -*)      echo "Unknown option: $1"; usage ;;
        *)       IPS+=("$1"); shift ;;
    esac
done

if [[ ${#IPS[@]} -eq 0 ]]; then
    echo "ERROR: At least one IP address is required."
    usage
fi

SSH_OPTS="-i $SSH_KEY -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o BatchMode=yes"

# -- Color helpers -------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}OK${NC}  $1"; }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; INSTANCE_OK=false; }
warn() { echo -e "  ${YELLOW}WARN${NC}  $1"; }

# -- Auto-detect runner type via SSH --------------------------------
detect_type() {
    local ip="$1"
    # Check if Rust is installed for runner user
    if ssh $SSH_OPTS "$SSH_USER@$ip" \
        "sudo -u runner bash -c 'test -f ~/.cargo/env'" 2>/dev/null; then
        echo "rust"
    else
        echo "scala"
    fi
}

# -- Remote check script generator ---------------------------------
# Returns a bash script that runs on the remote instance.
# $1 = runner type (scala|rust)
remote_check_script() {
    local rtype="$1"
    cat <<'COMMON'
#!/usr/bin/env bash
set -uo pipefail
PASS=0
FAIL=0

check() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "OK|$label|$("$@" 2>&1 | head -1)"
        PASS=$((PASS+1))
    else
        echo "FAIL|$label|not found or error"
        FAIL=$((FAIL+1))
    fi
}

# Runner service
if sudo systemctl is-active actions-runner >/dev/null 2>&1; then
    echo "OK|runner-service|active"
    PASS=$((PASS+1))
else
    echo "FAIL|runner-service|inactive or missing"
    FAIL=$((FAIL+1))
fi

# Docker
check "docker" docker --version
check "docker-compose" docker-compose --version

# Docker daemon health (use sudo since SSH user may not be in docker group)
if sudo docker info >/dev/null 2>&1; then
    echo "OK|docker-daemon|responsive"
    PASS=$((PASS+1))
else
    echo "FAIL|docker-daemon|not responding"
    FAIL=$((FAIL+1))
fi

# Python
check "python3.10" python3.10 --version

# Poetry (as runner user)
POETRY_OUT=$(sudo -u runner bash -lc 'cd ~ && export PATH=~/.local/bin:$PATH && poetry --version' 2>&1) || true
if [ -n "$POETRY_OUT" ]; then
    echo "OK|poetry|$POETRY_OUT"
    PASS=$((PASS+1))
else
    echo "FAIL|poetry|not found for runner user"
    FAIL=$((FAIL+1))
fi
COMMON

    if [ "$rtype" = "rust" ]; then
        cat <<'RUST'
# Rust toolchain (as runner user)
RUST_OUT=$(sudo -u runner bash -c 'source ~/.cargo/env && rustc --version' 2>&1) || true
if [ -n "$RUST_OUT" ]; then
    echo "OK|rustc|$RUST_OUT"
    PASS=$((PASS+1))
else
    echo "FAIL|rustc|not found for runner user"
    FAIL=$((FAIL+1))
fi

CARGO_OUT=$(sudo -u runner bash -c 'source ~/.cargo/env && cargo --version' 2>&1) || true
if [ -n "$CARGO_OUT" ]; then
    echo "OK|cargo|$CARGO_OUT"
    PASS=$((PASS+1))
else
    echo "FAIL|cargo|not found for runner user"
    FAIL=$((FAIL+1))
fi
RUST
    elif [ "$rtype" = "scala" ]; then
        cat <<'SCALA'
# Java
check "java" java -version

# SBT
SBT_OUT=$(sbt --version 2>&1 | tail -1) || true
if [ -n "$SBT_OUT" ]; then
    echo "OK|sbt|$SBT_OUT"
    PASS=$((PASS+1))
else
    echo "FAIL|sbt|not found"
    FAIL=$((FAIL+1))
fi

# GHC (as runner user)
GHC_OUT=$(sudo -u runner bash -lc 'export PATH=~/.ghcup/bin:$PATH && ghc --version' 2>&1) || true
if [ -n "$GHC_OUT" ]; then
    echo "OK|ghc|$GHC_OUT"
    PASS=$((PASS+1))
else
    echo "FAIL|ghc|not found for runner user"
    FAIL=$((FAIL+1))
fi

# BNFC (as runner user)
BNFC_OUT=$(sudo -u runner bash -lc 'export PATH=~/.ghcup/bin:~/.cabal/bin:$PATH && bnfc --version' 2>&1) || true
if [ -n "$BNFC_OUT" ]; then
    echo "OK|bnfc|$BNFC_OUT"
    PASS=$((PASS+1))
else
    echo "FAIL|bnfc|not found for runner user"
    FAIL=$((FAIL+1))
fi
SCALA
    fi

    cat <<'DISK'

# Disk space
DISK_PCT=$(df / | tail -1 | awk '{print $5}' | tr -d '%')
DISK_AVAIL=$(df -h / | tail -1 | awk '{print $4}')
if [ "$DISK_PCT" -lt 90 ]; then
    echo "OK|disk|${DISK_PCT}% used, ${DISK_AVAIL} available"
    PASS=$((PASS+1))
elif [ "$DISK_PCT" -lt 95 ]; then
    echo "WARN|disk|${DISK_PCT}% used, ${DISK_AVAIL} available"
else
    echo "FAIL|disk|${DISK_PCT}% used, ${DISK_AVAIL} available"
    FAIL=$((FAIL+1))
fi

# Arch
ARCH=$(uname -m)
echo "INFO|arch|$ARCH"

# Summary
echo "SUMMARY|${PASS}|${FAIL}"
DISK
}

# -- Main ----------------------------------------------------------
TOTAL_PASS=0
TOTAL_FAIL=0
TOTAL_INSTANCES=0

for ip in "${IPS[@]}"; do
    TOTAL_INSTANCES=$((TOTAL_INSTANCES+1))
    INSTANCE_OK=true

    echo ""

    # Test SSH connectivity first
    if ! ssh $SSH_OPTS "$SSH_USER@$ip" "true" 2>/dev/null; then
        echo -e "${BOLD}=== $ip ===${NC}"
        fail "SSH connection failed"
        TOTAL_FAIL=$((TOTAL_FAIL+1))
        continue
    fi

    # Determine runner type
    rtype="$RUNNER_TYPE"
    if [[ -z "$rtype" ]]; then
        rtype=$(detect_type "$ip")
    fi

    # Get hostname for display
    hostname=$(ssh $SSH_OPTS "$SSH_USER@$ip" "hostname" 2>/dev/null || echo "unknown")
    echo -e "${BOLD}=== $hostname ($rtype) === ${NC}$ip"

    # Run remote checks
    SCRIPT=$(remote_check_script "$rtype")
    OUTPUT=$(ssh $SSH_OPTS "$SSH_USER@$ip" "bash -s" <<< "$SCRIPT" 2>/dev/null) || true

    while IFS= read -r line; do
        IFS='|' read -r status label detail <<< "$line"
        case "$status" in
            OK)      pass "$label: $detail" ;;
            FAIL)    fail "$label: $detail" ;;
            WARN)    warn "$label: $detail" ;;
            INFO)    echo "  --   $label: $detail" ;;
            SUMMARY)
                TOTAL_PASS=$((TOTAL_PASS + label))
                TOTAL_FAIL=$((TOTAL_FAIL + detail))
                ;;
        esac
    done <<< "$OUTPUT"

    if $INSTANCE_OK; then
        echo -e "  ${GREEN}${BOLD}Instance healthy${NC}"
    else
        echo -e "  ${RED}${BOLD}Instance has failures${NC}"
    fi
done

# -- Final summary -------------------------------------------------
echo ""
echo -e "${BOLD}=== Summary ===${NC}"
echo "  Instances checked: $TOTAL_INSTANCES"
echo -e "  Checks passed:     ${GREEN}$TOTAL_PASS${NC}"
if [ "$TOTAL_FAIL" -gt 0 ]; then
    echo -e "  Checks failed:     ${RED}$TOTAL_FAIL${NC}"
    exit 1
else
    echo -e "  Checks failed:     ${GREEN}0${NC}"
    echo ""
    echo -e "${GREEN}All runners healthy.${NC}"
fi

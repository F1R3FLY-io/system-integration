#!/bin/bash
# test_embers_deploys.sh - Test script for Embers deploy verification
# Run this script after shard restart to verify deploy state before manual debugging

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo "=============================================="
echo "  Embers Deploy Verification Test Suite"
echo "=============================================="
echo ""

# -----------------------------------------------------------------------------
# 1. Container Status
# -----------------------------------------------------------------------------
echo -e "${BLUE}[1/8] Container Status${NC}"
echo "----------------------------------------------"
for container in rnode.bootstrap rnode.validator1 rnode.validator2 rnode.validator3 rnode.readonly embers; do
    status=$(docker ps --filter "name=^${container}$" --format "{{.Status}}" 2>/dev/null || echo "NOT FOUND")
    if [[ "$status" == *"healthy"* ]] || [[ "$status" == *"Up"* ]]; then
        echo -e "  ${GREEN}✓${NC} $container: $status"
    elif [[ -z "$status" ]]; then
        echo -e "  ${RED}✗${NC} $container: NOT RUNNING"
    else
        echo -e "  ${YELLOW}?${NC} $container: $status"
    fi
done
echo ""

# -----------------------------------------------------------------------------
# 2. Port Mappings
# -----------------------------------------------------------------------------
echo -e "${BLUE}[2/8] Readonly Node Port Mappings${NC}"
echo "----------------------------------------------"
readonly_http_port=$(docker port rnode.readonly 40403 2>/dev/null | head -1 | cut -d: -f2 || echo "")
if [[ -n "$readonly_http_port" ]]; then
    echo -e "  ${GREEN}✓${NC} Readonly HTTP API: localhost:$readonly_http_port (internal 40403)"
else
    echo -e "  ${RED}✗${NC} Readonly HTTP port not mapped"
    readonly_http_port="40453"  # fallback
fi
echo ""

# -----------------------------------------------------------------------------
# 3. Block Progress
# -----------------------------------------------------------------------------
echo -e "${BLUE}[3/8] Block Progress${NC}"
echo "----------------------------------------------"
latest_block=$(docker logs rnode.validator1 2>&1 | grep "Block created:" | tail -1 || echo "")
if [[ -n "$latest_block" ]]; then
    block_num=$(echo "$latest_block" | grep -oP '#\d+' | head -1)
    echo -e "  Latest block created: ${GREEN}$block_num${NC}"
    echo "  $latest_block"
else
    echo -e "  ${YELLOW}No blocks created yet${NC}"
fi
echo ""

# -----------------------------------------------------------------------------
# 4. Blocks with Deploys
# -----------------------------------------------------------------------------
echo -e "${BLUE}[4/8] Blocks with User Deploys${NC}"
echo "----------------------------------------------"
deploy_blocks=$(docker logs rnode.validator1 2>&1 | grep -E "Block created:.*\([1-9][0-9]*d\)" | tail -10)
if [[ -n "$deploy_blocks" ]]; then
    echo -e "  ${GREEN}Found blocks with deploys:${NC}"
    echo "$deploy_blocks" | while read line; do
        echo "    $line"
    done
else
    echo -e "  ${RED}No blocks with user deploys found${NC}"
fi
echo ""

# -----------------------------------------------------------------------------
# 5. Deploy Selection Logs
# -----------------------------------------------------------------------------
echo -e "${BLUE}[5/8] Recent Deploy Selection (last 5)${NC}"
echo "----------------------------------------------"
deploy_selection=$(docker logs rnode.validator1 2>&1 | grep "Deploy selection" | tail -5)
if [[ -n "$deploy_selection" ]]; then
    echo "$deploy_selection" | while read line; do
        if [[ "$line" == *"selected=0"* ]]; then
            echo -e "  ${YELLOW}$line${NC}"
        else
            echo -e "  ${GREEN}$line${NC}"
        fi
    done
else
    echo -e "  ${YELLOW}No deploy selection logs found${NC}"
fi
echo ""

# -----------------------------------------------------------------------------
# 6. DagMerger Activity
# -----------------------------------------------------------------------------
echo -e "${BLUE}[6/8] DagMerger Activity (last 5 merges)${NC}"
echo "----------------------------------------------"
dag_merges=$(docker logs rnode.readonly 2>&1 | grep "DagMerger.merge:" | grep -v DEBUG | tail -5)
if [[ -n "$dag_merges" ]]; then
    echo "$dag_merges" | while read line; do
        echo "  $line"
    done
else
    echo -e "  ${YELLOW}No DagMerger logs found${NC}"
fi
echo ""

# -----------------------------------------------------------------------------
# 7. Exploratory Deploy Test
# -----------------------------------------------------------------------------
echo -e "${BLUE}[7/8] Exploratory Deploy Test${NC}"
echo "----------------------------------------------"
# Test with simple Nil to verify API works
explore_result=$(curl -s -X POST "http://localhost:$readonly_http_port/api/explore-deploy" \
    -H "Content-Type: text/plain" \
    -d 'Nil' 2>&1)

if [[ "$explore_result" == *"blockNumber"* ]]; then
    block_num=$(echo "$explore_result" | grep -oP '"blockNumber":\d+' | grep -oP '\d+')
    state_hash=$(echo "$explore_result" | grep -oP '"postStateHash":"[a-f0-9]+"' | cut -d'"' -f4 | head -c 16)
    echo -e "  ${GREEN}✓${NC} Exploratory deploy working"
    echo "    Block: #$block_num"
    echo "    State: ${state_hash}..."
elif [[ "$explore_result" == *"read-only"* ]]; then
    echo -e "  ${RED}✗${NC} Hit validator instead of readonly node"
    echo "    Error: $explore_result"
else
    echo -e "  ${RED}✗${NC} Exploratory deploy failed"
    echo "    Response: $explore_result"
fi
echo ""

# -----------------------------------------------------------------------------
# 8. Wallet API Test
# -----------------------------------------------------------------------------
echo -e "${BLUE}[8/8] Embers Wallet API Test${NC}"
echo "----------------------------------------------"
test_wallet="1111EjdAxnKb5zKUc8ikuxfdi3kwSGH7BJCHKWjnVzfAF3SjCBvjh"
wallet_result=$(curl -s "http://localhost:8080/api/wallets/$test_wallet/state?network=mainnet" 2>&1)

if [[ "$wallet_result" == *"balance"* ]]; then
    echo -e "  ${GREEN}✓${NC} Wallet API returned balance data"
    echo "    Response: ${wallet_result:0:200}..."
elif [[ "$wallet_result" == *"contract did not return any value"* ]]; then
    echo -e "  ${RED}✗${NC} Contract not found in state"
    echo "    This indicates the Embers contracts are not visible in the merged state"
else
    echo -e "  ${YELLOW}?${NC} Unexpected response"
    echo "    Response: $wallet_result"
fi
echo ""

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
echo "=============================================="
echo "  Exploratory Deploy Logging (if present)"
echo "=============================================="
explore_logs=$(docker logs rnode.readonly 2>&1 | grep -i "exploratoryDeploy" | tail -10)
if [[ -n "$explore_logs" ]]; then
    echo "$explore_logs"
else
    echo -e "${YELLOW}No exploratoryDeploy logging found - rebuild may be needed${NC}"
fi
echo ""

echo "=============================================="
echo "  Test Complete"
echo "=============================================="

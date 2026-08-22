#!/usr/bin/env bash
# Wait until the monitoring stack has real data, instead of sleeping a fixed
# time or trusting the servers' own readiness endpoints.
#
# `shardctl up monitoring` returns as soon as the containers exist. Two
# things then have to happen before "Verify monitoring stack" can pass:
#
#   1. Grafana finishes provisioning. For a few seconds it accepts TCP
#      connections but resets them mid-response (curl exit 56; run
#      32572443142).
#   2. Prometheus completes its first scrape. `/-/ready` turns true within
#      seconds of start, long before any target has been scraped: a wait on
#      `/-/ready` alone returned after 4s and the verify step then saw
#      UP=0 DOWN=0 (run 32573011415). With scrape_interval 15s the first
#      sample lands up to ~15s after start, which is why the old `sleep 20`
#      was marginal rather than wrong.
#
# Ready therefore means: Grafana /api/health reports database ok, and
# Prometheus /api/v1/targets lists at least one active target with none
# still in health "unknown" (unknown = discovered but never scraped).
set -euo pipefail

TIMEOUT="${MONITORING_READY_TIMEOUT:-120}"
PROMETHEUS="${PROMETHEUS_URL:-http://localhost:9090}"
GRAFANA="${GRAFANA_URL:-http://localhost:3000}"

grafana_ready() {
    curl -sf --max-time 5 "$GRAFANA/api/health" | python3 -c '
import json, sys
sys.exit(0 if json.load(sys.stdin).get("database") == "ok" else 1)' 2>/dev/null
}

prometheus_scraped() {
    curl -sf --max-time 5 "$PROMETHEUS/api/v1/targets" | python3 -c '
import json, sys
targets = json.load(sys.stdin)["data"]["activeTargets"]
sys.exit(0 if targets and all(t["health"] != "unknown" for t in targets) else 1)' 2>/dev/null
}

deadline=$((SECONDS + TIMEOUT))
until grafana_ready && prometheus_scraped; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "wait-for-monitoring: not ready after ${TIMEOUT}s" >&2
        echo "--- grafana /api/health ---" >&2
        curl -s --max-time 5 "$GRAFANA/api/health" >&2 || true
        echo "--- prometheus /api/v1/targets ---" >&2
        curl -s --max-time 5 "$PROMETHEUS/api/v1/targets" >&2 || true
        exit 1
    fi
    sleep 2
done

summary="$(curl -sf --max-time 5 "$PROMETHEUS/api/v1/targets" | python3 -c '
import json, sys
t = json.load(sys.stdin)["data"]["activeTargets"]
up = sum(1 for x in t if x["health"] == "up")
print(f"{len(t)} targets scraped, {up} up")')"
echo "wait-for-monitoring: grafana ready, prometheus ${summary}, after ${SECONDS}s"

#!/usr/bin/env bash
# Wait until the monitoring stack answers, instead of sleeping a fixed time.
#
# `shardctl up monitoring` returns as soon as the containers exist. Grafana
# then provisions datasources and dashboards, and for a few seconds accepts
# TCP connections but resets them mid-response; curl reports exit 56. A
# fixed `sleep 20` covered that window most of the time, not always
# (run 32572443142, Rust Standalone, "Verify monitoring stack").
set -euo pipefail

TIMEOUT="${MONITORING_READY_TIMEOUT:-120}"
PROMETHEUS="${PROMETHEUS_URL:-http://localhost:9090}"
GRAFANA="${GRAFANA_URL:-http://localhost:3000}"

ready() {
    curl -sf --max-time 5 "$PROMETHEUS/-/ready" >/dev/null \
        && curl -sf --max-time 5 "$GRAFANA/api/health" | grep -q '"database": *"ok"'
}

deadline=$((SECONDS + TIMEOUT))
until ready; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "wait-for-monitoring: not ready after ${TIMEOUT}s" >&2
        echo "--- prometheus ---" >&2; curl -s --max-time 5 "$PROMETHEUS/-/ready" >&2 || true
        echo "--- grafana ---" >&2; curl -s --max-time 5 "$GRAFANA/api/health" >&2 || true
        exit 1
    fi
    sleep 2
done
echo "wait-for-monitoring: prometheus and grafana ready after ${SECONDS}s"

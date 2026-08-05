#!/usr/bin/env bash
set -euo pipefail

probe=${1:-ready}
if [[ "$probe" != "live" && "$probe" != "ready" ]]; then
  echo "usage: health.sh [live|ready]" >&2
  exit 64
fi

business_bind=${HERMES_BUSINESS_API_BIND:-127.0.0.1}
business_port=${HERMES_BUSINESS_API_PORT:-8101}
connector_bind=${HERMES_CONNECTOR_GATEWAY_BIND:-127.0.0.1}
connector_port=${HERMES_CONNECTOR_GATEWAY_PORT:-8102}
file_bind=${HERMES_FILE_GATEWAY_BIND:-127.0.0.1}
file_port=${HERMES_FILE_GATEWAY_PORT:-8104}

check_http() {
  local name=$1
  local bind_address=$2
  local port=$3
  curl --fail --silent --show-error --max-time 3 \
    "http://${bind_address}:${port}/${probe}" >/dev/null
  echo "${name}_${probe}=PASS"
}

check_http business_api "$business_bind" "$business_port"
check_http connector_gateway "$connector_bind" "$connector_port"
check_http file_gateway "$file_bind" "$file_port"

if command -v systemctl >/dev/null 2>&1; then
  systemctl is-active --quiet hermes-cloud-worker.service
  echo "worker_active=PASS"
else
  echo "worker_active=SKIP systemctl_unavailable"
fi

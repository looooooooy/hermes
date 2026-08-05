#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: run_asgi.sh <package:app> <bind-address> <port>" >&2
  exit 64
fi

application=$1
bind_address=$2
port=$3

case "$application" in
  hermes_cloud.entrypoints.business_api.bootstrap:app|\
  hermes_cloud.entrypoints.connector_gateway.bootstrap:app|\
  hermes_cloud.entrypoints.file_gateway.bootstrap:app)
    ;;
  *)
    echo "refusing an unknown ASGI package path" >&2
    exit 64
    ;;
esac

: "${HERMES_CURRENT:?HERMES_CURRENT is required}"
: "${HERMES_VENV:?HERMES_VENV is required}"

if [[ "$bind_address" != "127.0.0.1" && "$bind_address" != "::1" ]]; then
  echo "test server services must bind to loopback" >&2
  exit 78
fi
if [[ ! "$port" =~ ^[0-9]{2,5}$ ]] || ((port < 1024 || port > 65535)); then
  echo "service port is invalid" >&2
  exit 78
fi

cd -- "$HERMES_CURRENT"
exec "$HERMES_VENV/bin/python" -m uvicorn "$application" \
  --host "$bind_address" \
  --port "$port" \
  --proxy-headers \
  --forwarded-allow-ips "127.0.0.1,::1" \
  --lifespan on \
  --log-level warning \
  --no-access-log \
  --timeout-keep-alive 5 \
  --ws-max-size 262144

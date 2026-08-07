#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 <rendered-config> <target-config> <require-backend-ready:true|false>" >&2
  exit 64
}

[[ $# -eq 3 ]] || usage
source_config=$1
target_config=$2
require_backend_ready=$3

case "$require_backend_ready" in
  true|false) ;;
  *) usage ;;
esac

if [[ $(id -u) -ne 0 ]]; then
  echo "deployment must run as root or through passwordless sudo" >&2
  exit 77
fi

if [[ ! -f "$source_config" || -L "$source_config" ]]; then
  echo "rendered Nginx config is unavailable or unsafe" >&2
  exit 66
fi
if [[ "$target_config" != /etc/nginx/conf.d/* || "$target_config" == *".."* ]]; then
  echo "target config must be a direct /etc/nginx/conf.d file" >&2
  exit 65
fi
target_name=${target_config#/etc/nginx/conf.d/}
if [[ -z "$target_name" || "$target_name" == */* ]]; then
  echo "target config must not be nested" >&2
  exit 65
fi

install_packages() {
  if command -v nginx >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
    return
  fi
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y nginx curl
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y nginx curl
  elif command -v yum >/dev/null 2>&1; then
    yum install -y nginx curl
  else
    echo "no supported package manager found for Nginx installation" >&2
    exit 69
  fi
}

check_backend() {
  curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8101/live >/dev/null
  curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8101/ready >/dev/null
  for port in 8102 8104; do
    timeout 3 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/$port"
  done
}

install_packages

if [[ "$require_backend_ready" == true ]]; then
  check_backend
fi

certificate=$(awk '$1 == "ssl_certificate" {gsub(/;/, "", $2); print $2; exit}' "$source_config")
certificate_key=$(awk '$1 == "ssl_certificate_key" {gsub(/;/, "", $2); print $2; exit}' "$source_config")
for item in "$certificate" "$certificate_key"; do
  if [[ -z "$item" || "$item" != /* || ! -f "$item" || -L "$item" ]]; then
    echo "TLS certificate material is missing or unsafe: $item" >&2
    exit 78
  fi
done

mkdir -p /etc/nginx/conf.d
backup=""
if [[ -e "$target_config" ]]; then
  if [[ -L "$target_config" || ! -f "$target_config" ]]; then
    echo "existing target is not a regular file" >&2
    exit 73
  fi
  backup="${target_config}.backup.$(date -u +%Y%m%dT%H%M%SZ)"
  cp --preserve=mode,ownership,timestamps "$target_config" "$backup"
fi

rollback() {
  status=$?
  if [[ $status -ne 0 ]]; then
    if [[ -n "$backup" && -f "$backup" ]]; then
      cp "$backup" "$target_config"
    else
      rm -f "$target_config"
    fi
    nginx -t >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap rollback EXIT

install -o root -g root -m 0644 "$source_config" "$target_config"
nginx -t

if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now nginx
  systemctl reload nginx
else
  if pgrep -x nginx >/dev/null 2>&1; then
    nginx -s reload
  else
    nginx
  fi
fi

server_name=$(awk '$1 == "server_name" {gsub(/;/, "", $2); print $2; exit}' "$target_config")
curl --fail --silent --show-error --max-time 3 \
  --resolve "$server_name:443:127.0.0.1" \
  --cacert "$certificate" \
  "https://$server_name/hermes/live" \
  >/dev/null || {
    if [[ "$require_backend_ready" == true ]]; then
      exit 1
    fi
  }

trap - EXIT
rm -f "$source_config"
echo "Hermes Nginx configuration applied: $target_config"

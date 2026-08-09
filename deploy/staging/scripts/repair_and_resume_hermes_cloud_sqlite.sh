#!/usr/bin/env bash
set -euo pipefail

SERVER_NAME=${HERMES_SERVER_NAME:-api.seaotter.wiki}
DEPLOY_ROOT=/opt/hermes-cloud
SOURCE_ROOT="$DEPLOY_ROOT/source-main"
RELEASES_ROOT="$DEPLOY_ROOT/releases"
CURRENT_LINK="$DEPLOY_ROOT/current"
CONFIG_ROOT=/etc/hermes-cloud/sqlite
SECRET_ROOT="$CONFIG_ROOT/secrets"
DATABASE_ROOT=/var/lib/hermes-cloud-sqlite
DATABASE="$DATABASE_ROOT/hermes-cloud.db"
NGINX_TARGET=/etc/nginx/conf.d/hermes-public.conf
LOGIN_RECEIPT=/root/hermes-staging-login.txt

log() { printf '[hermes-cloud-repair] %s\n' "$*"; }
die() { printf '[hermes-cloud-repair] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "repair must run as root"
[[ "$SERVER_NAME" =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid HERMES_SERVER_NAME"
getent group hermes-cloud >/dev/null || die "hermes-cloud group is missing"
id hermes-cloud >/dev/null 2>&1 || die "hermes-cloud service account is missing"
id hermes-cloud-migrate >/dev/null 2>&1 || die "hermes-cloud-migrate service account is missing"
[[ -L "$CURRENT_LINK" ]] || die "current release link is missing"
CURRENT_ROOT=$(readlink -f "$CURRENT_LINK")
[[ -d "$CURRENT_ROOT" && "$CURRENT_ROOT" == "$RELEASES_ROOT"/* ]] || die "current release target is unsafe"

repair_runtime_permissions() {
  log "repairing runtime traversal and group permissions"
  chown root:hermes-cloud "$DEPLOY_ROOT" "$RELEASES_ROOT" "$CURRENT_ROOT"
  chmod 0750 "$DEPLOY_ROOT" "$RELEASES_ROOT" "$CURRENT_ROOT"

  while IFS= read -r -d '' directory; do
    chown root:hermes-cloud "$directory"
    chmod 0750 "$directory"
  done < <(find "$CURRENT_ROOT" -xdev -type d -print0)

  while IFS= read -r -d '' file; do
    executable=false
    [[ -x "$file" ]] && executable=true
    chown root:hermes-cloud "$file"
    if [[ "$executable" == true ]]; then
      chmod 0750 "$file"
    else
      chmod 0640 "$file"
    fi
  done < <(find "$CURRENT_ROOT" -xdev -type f -print0)

  install -d -o hermes-cloud -g hermes-cloud -m 0770 "$DATABASE_ROOT"
  if [[ -e "$DATABASE" ]]; then
    [[ -f "$DATABASE" && ! -L "$DATABASE" ]] || die "unsafe SQLite database path"
    chown hermes-cloud:hermes-cloud "$DATABASE"
    chmod 0660 "$DATABASE"
  fi

  runuser -u hermes-cloud-migrate -- test -x "$CURRENT_ROOT/.venv/bin/python" || \
    die "migration user still cannot execute release Python"
  runuser -u hermes-cloud -- test -x "$CURRENT_ROOT/.venv/bin/python" || \
    die "runtime user still cannot execute release Python"
  log "runtime permission gate=PASS"
}

require_secret_contract() {
  local path=$1 owner=$2 mode=$3
  [[ -f "$path" && ! -L "$path" ]] || die "required secret is missing or unsafe: $path"
  [[ $(stat -c '%U' "$path") == "$owner" ]] || die "secret owner mismatch: $path"
  [[ $(stat -c '%a' "$path") == "$mode" ]] || die "secret mode mismatch: $path"
}

verify_secret_contracts() {
  require_secret_contract "$SECRET_ROOT/migration_database_dsn" hermes-cloud-migrate 600
  require_secret_contract "$SECRET_ROOT/bootstrap_database_dsn" hermes-cloud-migrate 600
  require_secret_contract "$SECRET_ROOT/initial_user_password" hermes-cloud-migrate 600
  require_secret_contract "$SECRET_ROOT/runtime_database_dsn" hermes-cloud 600
  require_secret_contract "$SECRET_ROOT/business_api_signing_secret" hermes-cloud 600
  require_secret_contract "$SECRET_ROOT/connector_signing_secret" hermes-cloud 600
  [[ -f "$SECRET_ROOT/observer_keyring.json" && ! -L "$SECRET_ROOT/observer_keyring.json" ]] || \
    die "observer keyring is missing or unsafe"
  [[ $(stat -c '%U:%G:%a' "$SECRET_ROOT/observer_keyring.json") == root:hermes-cloud:440 ]] || \
    die "observer keyring ownership/mode mismatch"
  log "secret contract gate=PASS"
}

start_unit_or_report() {
  local unit=$1
  if ! systemctl start "$unit"; then
    systemctl --no-pager -l status "$unit" || true
    journalctl -u "$unit" -n 120 --no-pager || true
    die "$unit failed"
  fi
}

run_database_setup() {
  systemctl stop hermes-cloud-sqlite-business-api.service 2>/dev/null || true
  systemctl stop hermes-cloud-sqlite-connector-gateway.service 2>/dev/null || true
  systemctl reset-failed hermes-cloud-sqlite-migrate.service hermes-cloud-sqlite-seed-test-data.service 2>/dev/null || true
  start_unit_or_report hermes-cloud-sqlite-migrate.service
  start_unit_or_report hermes-cloud-sqlite-seed-test-data.service
  [[ -f "$DATABASE" ]] || die "SQLite database was not created"
  chown hermes-cloud:hermes-cloud "$DATABASE"
  chmod 0660 "$DATABASE"
  log "migration+seed=PASS"
}

start_cloud() {
  systemctl enable hermes-cloud-sqlite-connector-gateway.service hermes-cloud-sqlite-business-api.service >/dev/null
  start_unit_or_report hermes-cloud-sqlite-connector-gateway.service
  start_unit_or_report hermes-cloud-sqlite-business-api.service
  local _attempt
  for _attempt in $(seq 1 60); do
    if curl -fsS --max-time 2 http://127.0.0.1:8101/live >/dev/null && \
       curl -fsS --max-time 2 http://127.0.0.1:8101/ready >/dev/null && \
       curl -fsS --max-time 2 http://127.0.0.1:8102/live >/dev/null && \
       curl -fsS --max-time 2 http://127.0.0.1:8102/ready >/dev/null; then
      log "local Cloud readiness=PASS"
      return
    fi
    sleep 1
  done
  systemctl --no-pager -l status hermes-cloud-sqlite-business-api.service || true
  systemctl --no-pager -l status hermes-cloud-sqlite-connector-gateway.service || true
  journalctl -u hermes-cloud-sqlite-business-api.service -n 100 --no-pager || true
  journalctl -u hermes-cloud-sqlite-connector-gateway.service -n 100 --no-pager || true
  die "Cloud services did not become ready"
}

find_tls_pair() {
  TLS_CERT=${HERMES_TLS_CERTIFICATE:-}
  TLS_KEY=${HERMES_TLS_CERTIFICATE_KEY:-}
  if [[ -n "$TLS_CERT" || -n "$TLS_KEY" ]]; then
    [[ -f "$TLS_CERT" && ! -L "$TLS_CERT" && -f "$TLS_KEY" && ! -L "$TLS_KEY" ]] || \
      die "configured TLS certificate/key is unavailable or unsafe"
    return
  fi
  local letsencrypt_cert="/etc/letsencrypt/live/$SERVER_NAME/fullchain.pem"
  local letsencrypt_key="/etc/letsencrypt/live/$SERVER_NAME/privkey.pem"
  if [[ -f "$letsencrypt_cert" && -f "$letsencrypt_key" ]]; then
    TLS_CERT=$letsencrypt_cert
    TLS_KEY=$letsencrypt_key
    return
  fi
  local config cert key
  while IFS= read -r config; do
    cert=$(awk '$1=="ssl_certificate" {gsub(/;/,"",$2); print $2; exit}' "$config")
    key=$(awk '$1=="ssl_certificate_key" {gsub(/;/,"",$2); print $2; exit}' "$config")
    [[ "$cert" = /* && "$key" = /* && -f "$cert" && -f "$key" ]] || continue
    if openssl x509 -in "$cert" -noout -checkhost "$SERVER_NAME" >/dev/null 2>&1; then
      TLS_CERT=$cert
      TLS_KEY=$key
      return
    fi
  done < <(find /etc/nginx -maxdepth 4 -type f 2>/dev/null | sort)
  die "no existing TLS certificate covers $SERVER_NAME"
}

apply_nginx_p0() {
  find_tls_pair
  openssl x509 -in "$TLS_CERT" -noout -checkhost "$SERVER_NAME" >/dev/null 2>&1 || \
    die "TLS certificate does not cover $SERVER_NAME"
  local template="$SOURCE_ROOT/deploy/staging/nginx/hermes-public-p0.conf.template"
  [[ -f "$template" ]] || die "P0 Nginx template is missing"
  local rendered backup conflict
  rendered=$(mktemp /tmp/hermes-public-p0.XXXXXX.conf)
  sed \
    -e "s|__SERVER_NAME__|$SERVER_NAME|g" \
    -e "s|__TLS_CERTIFICATE__|$TLS_CERT|g" \
    -e "s|__TLS_CERTIFICATE_KEY__|$TLS_KEY|g" \
    "$template" > "$rendered"
  mkdir -p /etc/nginx/conf.d /var/www/html
  conflict=$(grep -Rsl --include='*.conf' "server_name[[:space:]].*$SERVER_NAME" /etc/nginx 2>/dev/null \
    | grep -vFx "$NGINX_TARGET" | head -n1 || true)
  [[ -z "$conflict" ]] || die "existing Nginx config already owns $SERVER_NAME: $conflict"
  backup=""
  if [[ -e "$NGINX_TARGET" ]]; then
    [[ -f "$NGINX_TARGET" && ! -L "$NGINX_TARGET" ]] || die "unsafe existing Nginx target"
    backup="$NGINX_TARGET.backup.$(date -u +%Y%m%dT%H%M%SZ)"
    cp -a "$NGINX_TARGET" "$backup"
  fi
  install -o root -g root -m 0644 "$rendered" "$NGINX_TARGET"
  rm -f "$rendered"
  if ! nginx -t; then
    if [[ -n "$backup" ]]; then cp -a "$backup" "$NGINX_TARGET"; else rm -f "$NGINX_TARGET"; fi
    nginx -t >/dev/null 2>&1 || true
    die "Nginx configuration test failed; previous config restored"
  fi
  systemctl enable --now nginx >/dev/null
  systemctl reload nginx
  curl -fsS --max-time 5 --resolve "$SERVER_NAME:443:127.0.0.1" --cacert "$TLS_CERT" \
    "https://$SERVER_NAME/hermes/live" >/dev/null || die "local HTTPS live canary failed"
  curl -fsS --max-time 5 --resolve "$SERVER_NAME:443:127.0.0.1" --cacert "$TLS_CERT" \
    "https://$SERVER_NAME/hermes/ready" >/dev/null || die "local HTTPS ready canary failed"
  local challenge='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
  local authorize_url="https://$SERVER_NAME/hermes/auth/native/authorize?code_challenge=$challenge&code_challenge_method=S256&redirect_uri=http%3A%2F%2F127.0.0.1%3A54321%2Foauth%2Fcallback&state=state-0123456789abcdef"
  curl -fsS --max-time 5 --resolve "$SERVER_NAME:443:127.0.0.1" --cacert "$TLS_CERT" "$authorize_url" \
    | grep -q 'Connect Hermes' || die "native OAuth authorize canary failed"
  log "local HTTPS + native OAuth canary=PASS"
}

write_login_receipt() {
  local password release_id source_commit
  password=$(cat "$SECRET_ROOT/initial_user_password")
  release_id=$(basename "$CURRENT_ROOT")
  source_commit=$(git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null || printf 'unknown')
  cat > "$LOGIN_RECEIPT" <<EOF
Hermes Cloud staging
URL=https://$SERVER_NAME/hermes/
Username=android-user
Password=$password
Release=$release_id
Commit=$source_commit
EOF
  chmod 0600 "$LOGIN_RECEIPT"
}

repair_runtime_permissions
verify_secret_contracts
systemctl daemon-reload
run_database_setup
start_cloud
apply_nginx_p0
write_login_receipt
log "deployment=PASS release=$(basename "$CURRENT_ROOT")"
log "workspace_url=https://$SERVER_NAME/hermes/"
log "login credentials are stored root-only at $LOGIN_RECEIPT"

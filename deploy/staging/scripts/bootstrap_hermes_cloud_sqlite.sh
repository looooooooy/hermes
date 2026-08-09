#!/usr/bin/env bash
set -euo pipefail

# One-command, fail-closed bootstrap for the Hermes Cloud SQLite P0 staging profile.
# Intended for a dedicated Alibaba Linux test host. This is a staging integration
# path, not the immutable production release promotion runbook.

SERVER_NAME=${HERMES_SERVER_NAME:-api.seaotter.wiki}
GIT_REF=${HERMES_GIT_REF:-main}
REPOSITORY_URL=${HERMES_REPOSITORY_URL:-https://github.com/looooooooy/hermes.git}
DEPLOY_ROOT=/opt/hermes-cloud
SOURCE_ROOT="$DEPLOY_ROOT/source-main"
RELEASES_ROOT="$DEPLOY_ROOT/releases"
CURRENT_LINK="$DEPLOY_ROOT/current"
PREVIOUS_LINK="$DEPLOY_ROOT/previous"
CONFIG_ROOT=/etc/hermes-cloud/sqlite
SECRET_ROOT="$CONFIG_ROOT/secrets"
ENV_FILE="$CONFIG_ROOT/test-server.env"
DATABASE_ROOT=/var/lib/hermes-cloud-sqlite
DATABASE="$DATABASE_ROOT/hermes-cloud.db"
NGINX_TARGET=/etc/nginx/conf.d/hermes-public.conf
LOGIN_RECEIPT=/root/hermes-staging-login.txt
LOCK_FILE=/var/lock/hermes-cloud-sqlite-bootstrap.lock
SEED_TENANT_ID=a495873f-cc49-5e21-b9fd-a581e3159ec8

log() { printf '[hermes-cloud] %s\n' "$*"; }
die() { printf '[hermes-cloud] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "bootstrap must run as root"
[[ "$SERVER_NAME" =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid HERMES_SERVER_NAME"
[[ "$GIT_REF" =~ ^[A-Za-z0-9._/-]+$ && "$GIT_REF" != *..* ]] || die "invalid HERMES_GIT_REF"
umask 0077
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
flock -n 9 || die "another Hermes Cloud bootstrap is already running"

install_base_packages() {
  local manager=""
  if command -v dnf >/dev/null 2>&1; then manager=dnf
  elif command -v yum >/dev/null 2>&1; then manager=yum
  elif command -v apt-get >/dev/null 2>&1; then manager=apt-get
  else die "supported package manager not found"
  fi

  if [[ "$manager" == apt-get ]]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y git curl nginx openssl ca-certificates util-linux
  else
    "$manager" install -y git curl nginx openssl ca-certificates util-linux
  fi
}

find_python() {
  local candidate version
  for candidate in python3.13 python3.12 python3.11 python3; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    version=$($candidate -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)
    case "$version" in
      3.11|3.12|3.13) command -v "$candidate"; return 0 ;;
    esac
  done
  return 1
}

install_python_if_needed() {
  if find_python >/dev/null 2>&1; then return; fi
  local manager=""
  if command -v dnf >/dev/null 2>&1; then manager=dnf
  elif command -v yum >/dev/null 2>&1; then manager=yum
  elif command -v apt-get >/dev/null 2>&1; then manager=apt-get
  fi
  [[ -n "$manager" ]] || die "cannot install Python"

  if [[ "$manager" == apt-get ]]; then
    apt-get install -y python3.12 python3.12-venv python3-pip 2>/dev/null || \
      apt-get install -y python3.11 python3.11-venv python3-pip
  else
    "$manager" install -y python3.12 python3.12-pip 2>/dev/null || \
      "$manager" install -y python3.11 python3.11-pip 2>/dev/null || \
      "$manager" install -y python311 python311-pip 2>/dev/null || true
  fi
  find_python >/dev/null 2>&1 || die "CPython 3.11+ is required; package manager could not install it"
}

ensure_accounts() {
  getent group hermes-cloud >/dev/null || groupadd --system hermes-cloud
  local nologin_shell
  nologin_shell=$(command -v nologin 2>/dev/null || true)
  [[ -n "$nologin_shell" ]] || nologin_shell=/sbin/nologin
  id hermes-cloud >/dev/null 2>&1 || \
    useradd --system --gid hermes-cloud --home-dir /nonexistent --shell "$nologin_shell" hermes-cloud
  id hermes-cloud-migrate >/dev/null 2>&1 || \
    useradd --system --gid hermes-cloud --home-dir /nonexistent --shell "$nologin_shell" hermes-cloud-migrate
}

sync_source() {
  mkdir -p "$DEPLOY_ROOT" "$RELEASES_ROOT"
  if [[ -d "$SOURCE_ROOT/.git" ]]; then
    git -C "$SOURCE_ROOT" fetch --depth=1 origin "$GIT_REF"
    git -C "$SOURCE_ROOT" reset --hard FETCH_HEAD
    git -C "$SOURCE_ROOT" clean -fdx
  else
    rm -rf "$SOURCE_ROOT"
    git clone --depth=1 --branch "$GIT_REF" "$REPOSITORY_URL" "$SOURCE_ROOT" 2>/dev/null || {
      git clone --depth=1 "$REPOSITORY_URL" "$SOURCE_ROOT"
      git -C "$SOURCE_ROOT" fetch --depth=1 origin "$GIT_REF"
      git -C "$SOURCE_ROOT" checkout --detach FETCH_HEAD
    }
  fi
  SOURCE_COMMIT=$(git -C "$SOURCE_ROOT" rev-parse HEAD)
  [[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "source commit identity is invalid"
  RELEASE_ID="staging-main-${SOURCE_COMMIT:0:12}"
  RELEASE_ROOT="$RELEASES_ROOT/$RELEASE_ID"
}

prepare_release() {
  local python_bin=$1
  if [[ ! -d "$RELEASE_ROOT" ]]; then
    mkdir -p "$RELEASE_ROOT"
    cp -a "$SOURCE_ROOT/hermes-cloud/." "$RELEASE_ROOT/"
  fi
  if [[ ! -x "$RELEASE_ROOT/.venv/bin/python" ]]; then
    "$python_bin" -m venv "$RELEASE_ROOT/.venv"
  fi
  "$RELEASE_ROOT/.venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip >/dev/null
  "$RELEASE_ROOT/.venv/bin/python" -m pip install --disable-pip-version-check "$RELEASE_ROOT" >/dev/null
  "$RELEASE_ROOT/.venv/bin/python" - <<'PY'
import fastapi, sqlalchemy, uvicorn
import hermes_cloud
print("runtime-imports=PASS")
PY
}

write_secret_if_missing() {
  local path=$1 owner=$2 group=$3 mode=$4 value=$5
  if [[ ! -e "$path" ]]; then
    printf '%s\n' "$value" > "$path"
  fi
  [[ -f "$path" && ! -L "$path" ]] || die "unsafe secret path: $path"
  chown "$owner:$group" "$path"
  chmod "$mode" "$path"
}

prepare_state_and_secrets() {
  install -d -o root -g hermes-cloud -m 0750 "$CONFIG_ROOT"
  install -d -o root -g hermes-cloud -m 0750 "$SECRET_ROOT"
  install -d -o hermes-cloud -g hermes-cloud -m 0770 "$DATABASE_ROOT"

  local dsn='sqlite+pysqlite:////var/lib/hermes-cloud-sqlite/hermes-cloud.db'
  write_secret_if_missing "$SECRET_ROOT/runtime_database_dsn" hermes-cloud hermes-cloud 0600 "$dsn"
  write_secret_if_missing "$SECRET_ROOT/migration_database_dsn" hermes-cloud-migrate hermes-cloud 0600 "$dsn"
  write_secret_if_missing "$SECRET_ROOT/bootstrap_database_dsn" hermes-cloud-migrate hermes-cloud 0600 "$dsn"
  write_secret_if_missing "$SECRET_ROOT/business_api_signing_secret" hermes-cloud hermes-cloud 0600 "$(openssl rand -base64 48)"
  write_secret_if_missing "$SECRET_ROOT/connector_signing_secret" hermes-cloud hermes-cloud 0600 "$(openssl rand -base64 48)"
  write_secret_if_missing "$SECRET_ROOT/initial_user_password" hermes-cloud-migrate hermes-cloud 0600 "$(openssl rand -base64 24 | tr -d '\n')"

  if [[ ! -e "$SECRET_ROOT/observer_keyring.json" ]]; then
    local kek
    kek=$(openssl rand -base64 32 | tr -d '\n')
    printf '{"version":1,"tenants":{"%s":{"current":"v1","keys":{"v1":"%s"}}}}\n' \
      "$SEED_TENANT_ID" "$kek" > "$SECRET_ROOT/observer_keyring.json"
  fi
  [[ -f "$SECRET_ROOT/observer_keyring.json" && ! -L "$SECRET_ROOT/observer_keyring.json" ]] || \
    die "unsafe observer keyring"
  chown root:hermes-cloud "$SECRET_ROOT/observer_keyring.json"
  chmod 0440 "$SECRET_ROOT/observer_keyring.json"

  cat > "$ENV_FILE" <<'EOF'
HERMES_DEPLOY_ROOT=/opt/hermes-cloud
HERMES_RELEASES_DIR=/opt/hermes-cloud/releases
HERMES_CURRENT=/opt/hermes-cloud/current
HERMES_PREVIOUS=/opt/hermes-cloud/previous
HERMES_VENV=/opt/hermes-cloud/current/.venv
HERMES_BUSINESS_API_BIND=127.0.0.1
HERMES_BUSINESS_API_PORT=8101
HERMES_CONNECTOR_GATEWAY_BIND=127.0.0.1
HERMES_CONNECTOR_GATEWAY_PORT=8102
HERMES_CONNECTOR_TOKEN_TENANT_ID=a495873f-cc49-5e21-b9fd-a581e3159ec8
HERMES_CONNECTOR_TOKEN_DEVICE_ID=0059b49e-fb3e-5da1-9a7c-d5a1537b2210
HERMES_CONNECTOR_TOKEN_TTL_SECONDS=300
HERMES_CONNECTOR_TOKEN_OUTPUT=/var/lib/hermes-cloud-connector-token/connector.token
HERMES_SEED_TENANT_SLUG=android-test
HERMES_SEED_TENANT_DISPLAY_NAME=Android Test
HERMES_SEED_USERNAME=android-user
HERMES_SEED_USER_DISPLAY_NAME=Android User
HERMES_SEED_WORKSPACE_KEY=android
HERMES_SEED_WORKSPACE_DISPLAY_NAME=Android
HERMES_SEED_OWNER_CONTROL_ENABLED=true
HERMES_SEED_AGENT_KEY=android-agent
HERMES_SEED_DEVICE_KEY=android-device
EOF
  chown root:hermes-cloud "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
}

activate_release_link() {
  OLD_CURRENT=""
  if [[ -L "$CURRENT_LINK" ]]; then
    OLD_CURRENT=$(readlink -f "$CURRENT_LINK" || true)
  fi
  if [[ -n "$OLD_CURRENT" && -d "$OLD_CURRENT" && "$OLD_CURRENT" != "$RELEASE_ROOT" ]]; then
    ln -sfn "$OLD_CURRENT" "$PREVIOUS_LINK"
  fi
  ln -sfn "$RELEASE_ROOT" "$CURRENT_LINK"
}

install_units() {
  local unit_root="$RELEASE_ROOT/deploy/test_server/sqlite/systemd"
  for unit in \
    hermes-cloud-sqlite-business-api.service \
    hermes-cloud-sqlite-connector-gateway.service \
    hermes-cloud-sqlite-migrate.service \
    hermes-cloud-sqlite-seed-test-data.service \
    hermes-cloud-sqlite-mint-connector-token.service; do
    [[ -f "$unit_root/$unit" ]] || die "missing systemd unit: $unit"
    install -o root -g root -m 0644 "$unit_root/$unit" "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
}

run_database_setup() {
  systemctl stop hermes-cloud-sqlite-business-api.service 2>/dev/null || true
  systemctl stop hermes-cloud-sqlite-connector-gateway.service 2>/dev/null || true
  systemctl start hermes-cloud-sqlite-migrate.service
  systemctl start hermes-cloud-sqlite-seed-test-data.service
  [[ -f "$DATABASE" ]] || die "SQLite database was not created"
  chown hermes-cloud:hermes-cloud "$DATABASE"
  chmod 0660 "$DATABASE"
}

start_cloud() {
  systemctl enable --now hermes-cloud-sqlite-connector-gateway.service
  systemctl enable --now hermes-cloud-sqlite-business-api.service
  local attempt url
  for attempt in $(seq 1 60); do
    if curl -fsS --max-time 2 http://127.0.0.1:8101/live >/dev/null && \
       curl -fsS --max-time 2 http://127.0.0.1:8101/ready >/dev/null && \
       curl -fsS --max-time 2 http://127.0.0.1:8102/live >/dev/null && \
       curl -fsS --max-time 2 http://127.0.0.1:8102/ready >/dev/null; then
      log "local Cloud readiness=PASS"
      return 0
    fi
    sleep 1
  done
  systemctl --no-pager -l status hermes-cloud-sqlite-business-api.service || true
  systemctl --no-pager -l status hermes-cloud-sqlite-connector-gateway.service || true
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
  done < <(find /etc/nginx -type f -maxdepth 4 2>/dev/null | sort)

  die "no existing TLS certificate covers $SERVER_NAME; set HERMES_TLS_CERTIFICATE and HERMES_TLS_CERTIFICATE_KEY"
}

apply_nginx_p0() {
  find_tls_pair
  openssl x509 -in "$TLS_CERT" -noout -checkhost "$SERVER_NAME" >/dev/null 2>&1 || \
    die "TLS certificate does not cover $SERVER_NAME"

  local template="$SOURCE_ROOT/deploy/staging/nginx/hermes-public-p0.conf.template"
  [[ -f "$template" ]] || die "P0 Nginx template is missing"
  local rendered
  rendered=$(mktemp /tmp/hermes-public-p0.XXXXXX.conf)
  sed \
    -e "s|__SERVER_NAME__|$SERVER_NAME|g" \
    -e "s|__TLS_CERTIFICATE__|$TLS_CERT|g" \
    -e "s|__TLS_CERTIFICATE_KEY__|$TLS_KEY|g" \
    "$template" > "$rendered"

  mkdir -p /etc/nginx/conf.d /var/www/html
  local conflict
  conflict=$(grep -Rsl --include='*.conf' "server_name[[:space:]].*$SERVER_NAME" /etc/nginx 2>/dev/null \
    | grep -vFx "$NGINX_TARGET" | head -n1 || true)
  if [[ -n "$conflict" ]]; then
    die "existing Nginx config already owns $SERVER_NAME: $conflict"
  fi

  local backup=""
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
  systemctl enable --now nginx
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
  local password
  password=$(cat "$SECRET_ROOT/initial_user_password")
  cat > "$LOGIN_RECEIPT" <<EOF
Hermes Cloud staging
URL=https://$SERVER_NAME/hermes/
Username=android-user
Password=$password
Release=$RELEASE_ID
Commit=$SOURCE_COMMIT
EOF
  chmod 0600 "$LOGIN_RECEIPT"
}

install_base_packages
install_python_if_needed
PYTHON_BIN=$(find_python) || die "CPython 3.11+ unavailable"
log "python=$($PYTHON_BIN --version 2>&1)"
ensure_accounts
sync_source
log "source_commit=$SOURCE_COMMIT"
prepare_release "$PYTHON_BIN"
prepare_state_and_secrets
activate_release_link
install_units
run_database_setup
start_cloud
apply_nginx_p0
write_login_receipt

log "deployment=PASS release=$RELEASE_ID"
log "workspace_url=https://$SERVER_NAME/hermes/"
log "login credentials are stored root-only at $LOGIN_RECEIPT"
log "next: retry Hermes Desktop > Sign in with browser"

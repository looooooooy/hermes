#!/usr/bin/env bash
set -euo pipefail

GIT_REF=${HERMES_GIT_REF:-main}
DEPLOY_ROOT=/opt/hermes-cloud
SOURCE_ROOT="$DEPLOY_ROOT/source-main"
RELEASES_ROOT="$DEPLOY_ROOT/releases"
CURRENT_LINK="$DEPLOY_ROOT/current"
PREVIOUS_LINK="$DEPLOY_ROOT/previous"
CONFIG_ROOT=/etc/hermes-cloud/sqlite
DATABASE=/var/lib/hermes-cloud-sqlite/hermes-cloud.db
LOCK_FILE=/var/lock/hermes-cloud-sqlite-update.lock

log() { printf '[hermes-cloud-update] %s\n' "$*"; }
die() { printf '[hermes-cloud-update] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "update must run as root"
[[ "$GIT_REF" =~ ^[A-Za-z0-9._/-]+$ && "$GIT_REF" != *..* ]] || die "invalid HERMES_GIT_REF"
[[ -d "$SOURCE_ROOT/.git" ]] || die "Hermes Cloud source checkout is missing"
[[ -L "$CURRENT_LINK" ]] || die "current Cloud release link is missing"
[[ -f "$CONFIG_ROOT/test-server.env" ]] || die "Cloud staging configuration is missing"
getent group hermes-cloud >/dev/null || die "hermes-cloud group is missing"
id hermes-cloud >/dev/null 2>&1 || die "hermes-cloud runtime account is missing"
id hermes-cloud-migrate >/dev/null 2>&1 || die "hermes-cloud migration account is missing"

mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
flock -n 9 || die "another Hermes Cloud update is already running"

OLD_CURRENT=$(readlink -f "$CURRENT_LINK")
[[ -d "$OLD_CURRENT" && "$OLD_CURRENT" == "$RELEASES_ROOT"/* ]] || die "current release target is unsafe"

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

PYTHON_BIN=$(find_python) || die "CPython 3.11+ is unavailable"
log "python=$($PYTHON_BIN --version 2>&1)"

log "fetching source ref=$GIT_REF"
git -C "$SOURCE_ROOT" fetch --depth=1 origin "$GIT_REF"
git -C "$SOURCE_ROOT" reset --hard FETCH_HEAD
git -C "$SOURCE_ROOT" clean -fdx
SOURCE_COMMIT=$(git -C "$SOURCE_ROOT" rev-parse HEAD)
[[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || die "source commit identity is invalid"
RELEASE_ID="staging-main-${SOURCE_COMMIT:0:12}"
RELEASE_ROOT="$RELEASES_ROOT/$RELEASE_ID"
log "source_commit=$SOURCE_COMMIT"

prepare_release() {
  if [[ ! -d "$RELEASE_ROOT" ]]; then
    install -d -o root -g hermes-cloud -m 0750 "$RELEASE_ROOT"
    cp -a "$SOURCE_ROOT/hermes-cloud/." "$RELEASE_ROOT/"
  fi
  if [[ ! -x "$RELEASE_ROOT/.venv/bin/python" ]]; then
    "$PYTHON_BIN" -m venv "$RELEASE_ROOT/.venv"
  fi
  "$RELEASE_ROOT/.venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip >/dev/null
  "$RELEASE_ROOT/.venv/bin/python" -m pip install --disable-pip-version-check "$RELEASE_ROOT" >/dev/null

  while IFS= read -r -d '' directory; do
    chown root:hermes-cloud "$directory"
    chmod 0750 "$directory"
  done < <(find "$RELEASE_ROOT" -xdev -type d -print0)
  while IFS= read -r -d '' file; do
    executable=false
    [[ -x "$file" ]] && executable=true
    chown root:hermes-cloud "$file"
    if [[ "$executable" == true ]]; then chmod 0750 "$file"; else chmod 0640 "$file"; fi
  done < <(find "$RELEASE_ROOT" -xdev -type f -print0)

  runuser -u hermes-cloud -- test -x "$RELEASE_ROOT/.venv/bin/python" || die "runtime user cannot execute candidate Private Python"
  runuser -u hermes-cloud-migrate -- test -x "$RELEASE_ROOT/.venv/bin/python" || die "migration user cannot execute candidate Private Python"
  "$RELEASE_ROOT/.venv/bin/python" - <<'PY'
import fastapi, sqlalchemy, uvicorn
import hermes_cloud
print("candidate-imports=PASS")
PY
}

install_units() {
  local unit_root="$RELEASE_ROOT/deploy/test_server/sqlite/systemd"
  for unit in \
    hermes-cloud-sqlite-business-api.service \
    hermes-cloud-sqlite-connector-gateway.service \
    hermes-cloud-sqlite-migrate.service \
    hermes-cloud-sqlite-seed-test-data.service \
    hermes-cloud-sqlite-mint-connector-token.service; do
    [[ -f "$unit_root/$unit" ]] || die "candidate is missing systemd unit: $unit"
    install -o root -g root -m 0644 "$unit_root/$unit" "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
}

start_unit_or_report() {
  local unit=$1
  if ! systemctl start "$unit"; then
    systemctl --no-pager -l status "$unit" || true
    journalctl -u "$unit" -n 120 --no-pager || true
    return 1
  fi
}

rollback() {
  local status=$?
  if [[ $status -ne 0 && -n "${SWITCHED:-}" ]]; then
    log "candidate failed; restoring previous release"
    ln -sfn "$OLD_CURRENT" "$CURRENT_LINK"
    systemctl daemon-reload || true
    systemctl restart hermes-cloud-sqlite-connector-gateway.service || true
    systemctl restart hermes-cloud-sqlite-business-api.service || true
  fi
  exit "$status"
}
trap rollback EXIT

prepare_release
install_units

if [[ "$OLD_CURRENT" != "$RELEASE_ROOT" ]]; then
  ln -sfn "$OLD_CURRENT" "$PREVIOUS_LINK"
fi
systemctl stop hermes-cloud-sqlite-business-api.service 2>/dev/null || true
systemctl stop hermes-cloud-sqlite-connector-gateway.service 2>/dev/null || true
ln -sfn "$RELEASE_ROOT" "$CURRENT_LINK"
SWITCHED=1

systemctl reset-failed hermes-cloud-sqlite-migrate.service hermes-cloud-sqlite-seed-test-data.service 2>/dev/null || true
start_unit_or_report hermes-cloud-sqlite-migrate.service || die "candidate migration failed"
start_unit_or_report hermes-cloud-sqlite-seed-test-data.service || die "candidate seed verification failed"
[[ -f "$DATABASE" ]] || die "SQLite database is unavailable after candidate migration"
chown hermes-cloud:hermes-cloud "$DATABASE"
chmod 0660 "$DATABASE"

start_unit_or_report hermes-cloud-sqlite-connector-gateway.service || die "candidate Connector Gateway failed"
start_unit_or_report hermes-cloud-sqlite-business-api.service || die "candidate Business API failed"

for _attempt in $(seq 1 60); do
  if curl -fsS --max-time 2 http://127.0.0.1:8101/live >/dev/null && \
     curl -fsS --max-time 2 http://127.0.0.1:8101/ready >/dev/null && \
     curl -fsS --max-time 2 http://127.0.0.1:8102/live >/dev/null && \
     curl -fsS --max-time 2 http://127.0.0.1:8102/ready >/dev/null; then
    break
  fi
  sleep 1
  [[ $_attempt -lt 60 ]] || die "candidate Cloud services did not become ready"
done
log "local readiness=PASS"

PAIRING_CONTEXT_CODE=$(curl -sS --max-time 3 -o /tmp/hermes-pairing-context-unauthorized.json -w '%{http_code}' \
  http://127.0.0.1:8101/api/onboarding/pairing-context || true)
[[ "$PAIRING_CONTEXT_CODE" == 401 ]] || die "pairing-context route canary failed (HTTP $PAIRING_CONTEXT_CODE)"
log "pairing-context unauthenticated canary=PASS"

trap - EXIT
log "update=PASS release=$RELEASE_ID"
log "current_commit=$SOURCE_COMMIT"

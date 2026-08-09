#!/usr/bin/env bash
set -euo pipefail

# Destructive development-only reset for the Hermes Cloud SQLite staging profile.
# This intentionally discards staging database state while preserving the
# currently installed Cloud release, deployment secrets, TLS, Nginx and system
# accounts.  No source fetch or release switch is performed here.

SERVER_NAME=${HERMES_SERVER_NAME:-api.seaotter.wiki}
DATABASE_ROOT=/var/lib/hermes-cloud-sqlite
DATABASE="$DATABASE_ROOT/hermes-cloud.db"
CURRENT_LINK=/opt/hermes-cloud/current

log() { printf '[hermes-cloud-reset] %s\n' "$*"; }
die() { printf '[hermes-cloud-reset] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "reset must run as root"
[[ "$SERVER_NAME" =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid HERMES_SERVER_NAME"
[[ -L "$CURRENT_LINK" ]] || die "current release link is missing"
CURRENT_ROOT=$(readlink -f "$CURRENT_LINK")
[[ -d "$CURRENT_ROOT" ]] || die "current release target is missing"
[[ "$CURRENT_ROOT" == /opt/hermes-cloud/releases/* ]] || die "current release target is unsafe"
[[ -x "$CURRENT_ROOT/.venv/bin/python" ]] || die "current release Python is unavailable"
id hermes-cloud >/dev/null 2>&1 || die "hermes-cloud runtime account is missing"
id hermes-cloud-migrate >/dev/null 2>&1 || die "hermes-cloud migration account is missing"

log "current_release=$(basename "$CURRENT_ROOT")"

start_unit_or_report() {
  local unit=$1
  systemctl reset-failed "$unit" 2>/dev/null || true
  if ! systemctl start "$unit"; then
    systemctl --no-pager -l status "$unit" || true
    journalctl -u "$unit" -n 160 --no-pager || true
    die "$unit failed"
  fi
}

log "stopping Cloud runtime services"
systemctl stop hermes-cloud-sqlite-business-api.service 2>/dev/null || true
systemctl stop hermes-cloud-sqlite-connector-gateway.service 2>/dev/null || true

install -d -o hermes-cloud-migrate -g hermes-cloud -m 0770 "$DATABASE_ROOT"

log "discarding staging SQLite database state"
rm -f \
  "$DATABASE" \
  "$DATABASE-wal" \
  "$DATABASE-shm" \
  "$DATABASE-journal"
[[ ! -e "$DATABASE" ]] || die "SQLite database could not be removed"
log "sqlite reset=PASS"

log "rebuilding schema from current release"
start_unit_or_report hermes-cloud-sqlite-migrate.service
[[ -f "$DATABASE" && ! -L "$DATABASE" ]] || die "migration did not create SQLite database"
chown hermes-cloud:hermes-cloud "$DATABASE"
chmod 0660 "$DATABASE"
log "migration=PASS"

log "seeding canonical current staging identity graph"
start_unit_or_report hermes-cloud-sqlite-seed-test-data.service
log "seed=PASS"

log "starting current Cloud release"
start_unit_or_report hermes-cloud-sqlite-connector-gateway.service
start_unit_or_report hermes-cloud-sqlite-business-api.service

for attempt in $(seq 1 60); do
  if curl -fsS --max-time 2 http://127.0.0.1:8101/live >/dev/null && \
     curl -fsS --max-time 2 http://127.0.0.1:8101/ready >/dev/null && \
     curl -fsS --max-time 2 http://127.0.0.1:8102/live >/dev/null && \
     curl -fsS --max-time 2 http://127.0.0.1:8102/ready >/dev/null; then
    break
  fi
  sleep 1
  [[ $attempt -lt 60 ]] || die "Cloud services did not become ready after reset"
done
log "local readiness=PASS"

PAIRING_CONTEXT_CODE=$(curl -sS --max-time 3 \
  -o /tmp/hermes-reset-pairing-context.json \
  -w '%{http_code}' \
  http://127.0.0.1:8101/api/onboarding/pairing-context || true)
[[ "$PAIRING_CONTEXT_CODE" == 401 ]] || die "pairing-context canary failed (HTTP $PAIRING_CONTEXT_CODE)"
log "pairing-context unauthenticated canary=PASS"

NATIVE_AUTH_CODE=$(curl -sS --max-time 3 \
  -o /tmp/hermes-reset-native-auth.html \
  -w '%{http_code}' \
  "http://127.0.0.1:8101/auth/native/authorize?code_challenge=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&code_challenge_method=S256&redirect_uri=http%3A%2F%2F127.0.0.1%3A55407%2Foauth%2Fcallback&state=reset-canary-state-0123456789&provider=basic" || true)
[[ "$NATIVE_AUTH_CODE" == 200 ]] || die "native OAuth authorize canary failed (HTTP $NATIVE_AUTH_CODE)"
grep -q 'Connect Hermes' /tmp/hermes-reset-native-auth.html || die "native OAuth authorize page marker is missing"
log "native OAuth canary=PASS"

log "reset-and-resume=PASS current_release=$(basename "$CURRENT_ROOT")"

#!/usr/bin/env bash
set -euo pipefail

# Destructive development-only reset for the Hermes Cloud SQLite staging profile.
# This intentionally discards staging database state. It preserves deployment
# secrets, TLS material, Nginx configuration, system accounts, and release files.

SERVER_NAME=${HERMES_SERVER_NAME:-api.seaotter.wiki}
DATABASE_ROOT=/var/lib/hermes-cloud-sqlite
DATABASE="$DATABASE_ROOT/hermes-cloud.db"
CURRENT_LINK=/opt/hermes-cloud/current
REPAIR_SCRIPT=/root/repair_and_resume_hermes_cloud_sqlite.sh
REPAIR_COMMIT=4d6c0fc69aeb86b684ba4b1c3704ba11f6296518

log() { printf '[hermes-cloud-reset] %s\n' "$*"; }
die() { printf '[hermes-cloud-reset] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "reset must run as root"
[[ "$SERVER_NAME" =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid HERMES_SERVER_NAME"
[[ -L "$CURRENT_LINK" ]] || die "current release link is missing"
CURRENT_ROOT=$(readlink -f "$CURRENT_LINK")
[[ -d "$CURRENT_ROOT" ]] || die "current release target is missing"
[[ -x "$CURRENT_ROOT/.venv/bin/python" ]] || die "current release Python is unavailable"

start_unit_or_report() {
  local unit=$1
  systemctl reset-failed "$unit" 2>/dev/null || true
  if ! systemctl start "$unit"; then
    systemctl --no-pager -l status "$unit" || true
    journalctl -u "$unit" -n 120 --no-pager || true
    die "$unit failed"
  fi
}

log "stopping Cloud runtime services"
systemctl stop hermes-cloud-sqlite-business-api.service 2>/dev/null || true
systemctl stop hermes-cloud-sqlite-connector-gateway.service 2>/dev/null || true

install -d -o hermes-cloud -g hermes-cloud -m 0770 "$DATABASE_ROOT"

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

log "seeding current staging identity graph"
start_unit_or_report hermes-cloud-sqlite-seed-test-data.service
log "seed=PASS"

# Reuse the already CI-verified repair/resume control path at an immutable commit
# to finish runtime readiness, Nginx HTTPS, and native OAuth canaries. Pinning the
# commit avoids branch/CDN cache ambiguity during staging recovery.
curl -fsSL \
  "https://raw.githubusercontent.com/looooooooy/hermes/${REPAIR_COMMIT}/deploy/staging/scripts/repair_and_resume_hermes_cloud_sqlite.sh" \
  -o "$REPAIR_SCRIPT"
chmod 0700 "$REPAIR_SCRIPT"

log "continuing Cloud/HTTPS/OAuth closure"
HERMES_SERVER_NAME="$SERVER_NAME" "$REPAIR_SCRIPT"

log "reset-and-resume=PASS"

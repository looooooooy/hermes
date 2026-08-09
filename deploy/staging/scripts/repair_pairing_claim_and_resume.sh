#!/usr/bin/env bash
set -euo pipefail

# Development/staging-only repair for failed device-pairing scratch state.
# Preserves identity/workspace/password/refresh-session data, TLS, Nginx,
# deployment secrets, and the canonical seeded owner-control device.

CLOUD_REF=${HERMES_CLOUD_REF:-12b3b053612502aa4fb81877441df805f399f351}
SERVER_NAME=${HERMES_SERVER_NAME:-api.seaotter.wiki}
DATABASE=/var/lib/hermes-cloud-sqlite/hermes-cloud.db
CURRENT_LINK=/opt/hermes-cloud/current
UPDATE_SCRIPT=/root/update_hermes_cloud_sqlite_release.pairing-repair.sh
UPDATE_URL="https://raw.githubusercontent.com/looooooooy/hermes/${CLOUD_REF}/deploy/staging/scripts/update_hermes_cloud_sqlite_release.sh"
SEED_DEVICE_KEY=android-device

log() { printf '[hermes-pairing-repair] %s\n' "$*"; }
die() { printf '[hermes-pairing-repair] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "repair must run as root"
[[ "$CLOUD_REF" =~ ^[0-9a-f]{40}$ ]] || die "HERMES_CLOUD_REF must be one full commit SHA"
[[ "$SERVER_NAME" =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid HERMES_SERVER_NAME"
[[ -f "$DATABASE" ]] || die "staging SQLite database is unavailable"
[[ -L "$CURRENT_LINK" ]] || die "current Cloud release link is missing"
command -v curl >/dev/null || die "curl is unavailable"
command -v systemctl >/dev/null || die "systemctl is unavailable"

log "updating Cloud to verified pairing recovery release"
curl -fsSL "$UPDATE_URL" -o "$UPDATE_SCRIPT"
chmod 0700 "$UPDATE_SCRIPT"
HERMES_GIT_REF="$CLOUD_REF" "$UPDATE_SCRIPT"
log "cloud release update=PASS"

PYTHON="$CURRENT_LINK/.venv/bin/python"
[[ -x "$PYTHON" ]] || die "current Cloud Private Python is unavailable"

SERVICES_STOPPED=0
restore_services() {
  local status=$?
  if [[ $SERVICES_STOPPED -eq 1 ]]; then
    systemctl start hermes-cloud-sqlite-connector-gateway.service >/dev/null 2>&1 || true
    systemctl start hermes-cloud-sqlite-business-api.service >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap restore_services EXIT

log "stopping Cloud services for one atomic pairing scratch repair"
systemctl stop hermes-cloud-sqlite-business-api.service
systemctl stop hermes-cloud-sqlite-connector-gateway.service
SERVICES_STOPPED=1

HERMES_PAIRING_REPAIR_DATABASE="$DATABASE" \
HERMES_PAIRING_REPAIR_SEED_DEVICE_KEY="$SEED_DEVICE_KEY" \
"$PYTHON" - <<'PY'
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

path = Path(os.environ["HERMES_PAIRING_REPAIR_DATABASE"])
seed_device_key = os.environ["HERMES_PAIRING_REPAIR_SEED_DEVICE_KEY"]

required_tables = {
    "users",
    "workspaces",
    "workspace_memberships",
    "agents",
    "devices",
    "password_credentials",
    "refresh_sessions",
    "pairing_offers",
    "pairing_sessions",
    "pairing_enrollment_proofs",
    "pairing_idempotency_records",
    "pairing_claim_limits",
    "device_lifecycles",
    "device_credentials",
    "device_credential_public_keys",
    "device_authentication_challenges",
}
protected_tables = (
    "users",
    "workspaces",
    "workspace_memberships",
    "agents",
    "password_credentials",
    "refresh_sessions",
)

connection = sqlite3.connect(path, timeout=10.0)
connection.row_factory = sqlite3.Row
try:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("BEGIN IMMEDIATE")

    available = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = sorted(required_tables - available)
    if missing:
        raise RuntimeError(f"pairing repair schema is incomplete: {','.join(missing)}")

    before = {
        table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in protected_tables
    }

    seed_rows = connection.execute(
        "SELECT tenant_id, device_id, status FROM devices WHERE device_key = ?",
        (seed_device_key,),
    ).fetchall()
    if len(seed_rows) != 1:
        raise RuntimeError("canonical staging seed device is missing or ambiguous")

    non_seed = connection.execute(
        "SELECT tenant_id, device_id, device_key, status "
        "FROM devices WHERE device_key <> ? ORDER BY created_at, device_id",
        (seed_device_key,),
    ).fetchall()
    if any(row["status"] != "disabled" for row in non_seed):
        raise RuntimeError(
            "refusing to remove a non-seed device that is not disabled"
        )

    device_ids = [row["device_id"] for row in non_seed]
    if device_ids:
        placeholders = ",".join("?" for _ in device_ids)
        credential_ids = [
            row[0]
            for row in connection.execute(
                f"SELECT credential_id FROM device_credentials "
                f"WHERE device_id IN ({placeholders})",
                device_ids,
            ).fetchall()
        ]

        connection.execute(
            f"DELETE FROM device_authentication_challenges "
            f"WHERE device_id IN ({placeholders})",
            device_ids,
        )
        if credential_ids:
            credential_placeholders = ",".join("?" for _ in credential_ids)
            connection.execute(
                f"DELETE FROM device_credential_public_keys "
                f"WHERE credential_id IN ({credential_placeholders})",
                credential_ids,
            )
        connection.execute(
            f"DELETE FROM device_credentials WHERE device_id IN ({placeholders})",
            device_ids,
        )

    # These tables are pairing-only scratch/ledger state in this staging profile.
    # Clear them in FK-safe child-to-parent order. No account/workspace/session-auth
    # table is touched.
    for table in (
        "pairing_enrollment_proofs",
        "pairing_idempotency_records",
        "device_lifecycles",
        "pairing_sessions",
        "pairing_claim_limits",
        "pairing_offers",
    ):
        connection.execute(f'DELETE FROM "{table}"')

    if device_ids:
        placeholders = ",".join("?" for _ in device_ids)
        connection.execute(
            f"DELETE FROM devices WHERE device_id IN ({placeholders})",
            device_ids,
        )

    after = {
        table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in protected_tables
    }
    if after != before:
        raise RuntimeError("protected identity/workspace/auth table counts changed")

    remaining_non_seed = connection.execute(
        "SELECT COUNT(*) FROM devices WHERE device_key <> ?",
        (seed_device_key,),
    ).fetchone()[0]
    if remaining_non_seed != 0:
        raise RuntimeError("non-seed pairing devices remain after repair")

    for table in (
        "pairing_enrollment_proofs",
        "pairing_idempotency_records",
        "device_lifecycles",
        "pairing_sessions",
        "pairing_claim_limits",
        "pairing_offers",
    ):
        if connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] != 0:
            raise RuntimeError(f"pairing scratch table was not cleared: {table}")

    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError("foreign key check failed after pairing repair")

    connection.commit()
    print(f"pairing_scratch_devices_removed={len(device_ids)}")
    print("pairing_scratch_reset=PASS")
    print("identity_workspace_auth_preserved=PASS")
except BaseException:
    connection.rollback()
    raise
finally:
    connection.close()
PY

chown hermes-cloud:hermes-cloud "$DATABASE"
chmod 0660 "$DATABASE"
log "pairing scratch reset=PASS"

systemctl reset-failed hermes-cloud-sqlite-business-api.service \
  hermes-cloud-sqlite-connector-gateway.service >/dev/null 2>&1 || true
systemctl start hermes-cloud-sqlite-connector-gateway.service
systemctl start hermes-cloud-sqlite-business-api.service
SERVICES_STOPPED=0

for attempt in $(seq 1 60); do
  if curl -fsS --max-time 2 http://127.0.0.1:8101/live >/dev/null && \
     curl -fsS --max-time 2 http://127.0.0.1:8101/ready >/dev/null && \
     curl -fsS --max-time 2 http://127.0.0.1:8102/live >/dev/null && \
     curl -fsS --max-time 2 http://127.0.0.1:8102/ready >/dev/null; then
    break
  fi
  sleep 1
  [[ $attempt -lt 60 ]] || die "Cloud services did not become ready after pairing repair"
done
log "local readiness=PASS"

PAIRING_CONTEXT_CODE=$(curl -sS --max-time 3 -o /tmp/hermes-pairing-repair-context.json -w '%{http_code}' \
  http://127.0.0.1:8101/api/onboarding/pairing-context || true)
[[ "$PAIRING_CONTEXT_CODE" == 401 ]] || die "pairing-context canary failed (HTTP $PAIRING_CONTEXT_CODE)"
log "pairing-context canary=PASS"

trap - EXIT
log "repair=PASS cloud_ref=$CLOUD_REF server=$SERVER_NAME"

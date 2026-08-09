#!/usr/bin/env bash
set -euo pipefail

SERVER_NAME=${HERMES_SERVER_NAME:-api.seaotter.wiki}
CURRENT_LINK=/opt/hermes-cloud/current
ENV_FILE=/etc/hermes-cloud/sqlite/test-server.env
SECRET_ROOT=/etc/hermes-cloud/sqlite/secrets
DATABASE=/var/lib/hermes-cloud-sqlite/hermes-cloud.db
BACKUP_DIR=/root/hermes-cloud-backups
REPAIR_SCRIPT=/root/repair_and_resume_hermes_cloud_sqlite.sh

log() { printf '[hermes-cloud-workspace-reconcile] %s\n' "$*"; }
die() { printf '[hermes-cloud-workspace-reconcile] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "reconcile must run as root"
[[ "$SERVER_NAME" =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid HERMES_SERVER_NAME"
[[ -L "$CURRENT_LINK" ]] || die "current release link is missing"
CURRENT_ROOT=$(readlink -f "$CURRENT_LINK")
[[ -d "$CURRENT_ROOT" ]] || die "current release target is missing"
[[ -x "$CURRENT_ROOT/.venv/bin/python" ]] || die "current release Python is unavailable"
[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || die "staging environment file is unavailable"
[[ -f "$SECRET_ROOT/bootstrap_database_dsn" && ! -L "$SECRET_ROOT/bootstrap_database_dsn" ]] || die "bootstrap DSN is unavailable"
[[ -f "$DATABASE" && ! -L "$DATABASE" ]] || die "SQLite database is unavailable"

seed_runner="$CURRENT_ROOT/deploy/test_server/scripts/seed_test_data.py"
[[ -f "$seed_runner" && ! -L "$seed_runner" ]] || die "seed runner is unavailable"

systemctl stop hermes-cloud-sqlite-business-api.service 2>/dev/null || true
systemctl stop hermes-cloud-sqlite-connector-gateway.service 2>/dev/null || true

install -d -o root -g root -m 0700 "$BACKUP_DIR"
backup="$BACKUP_DIR/hermes-cloud-before-workspace-creator-$(date -u +%Y%m%dT%H%M%SZ).db"
"$CURRENT_ROOT/.venv/bin/python" - "$DATABASE" "$backup" <<'PY'
from __future__ import annotations

import sqlite3
import sys

source_path, backup_path = sys.argv[1:3]
source = sqlite3.connect(source_path)
target = sqlite3.connect(backup_path)
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY
chmod 0600 "$backup"
chown root:root "$backup"
log "sqlite backup=PASS"

env_args=()
while IFS= read -r line; do
  [[ -z "$line" || "$line" == \#* ]] && continue
  [[ "$line" =~ ^[A-Z][A-Z0-9_]*= ]] || die "invalid staging environment entry"
  env_args+=("$line")
done < "$ENV_FILE"
env_args+=("HERMES_BOOTSTRAP_DSN_FILE=$SECRET_ROOT/bootstrap_database_dsn")

set +e
runuser -u hermes-cloud-migrate -- env "${env_args[@]}" \
  "$CURRENT_ROOT/.venv/bin/python" - "$seed_runner" <<'PY'
from __future__ import annotations

import importlib.util
import os
import sys

runner_path = sys.argv[1]
spec = importlib.util.spec_from_file_location(
    "hermes_cloud_staging_workspace_creator_reconcile",
    runner_path,
)
if spec is None or spec.loader is None:
    raise SystemExit(90)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

config = module.SeedConfig.from_environment(os.environ)
database_url = module.read_secret_file(
    os.environ["HERMES_BOOTSTRAP_DSN_FILE"],
    name="bootstrap database",
)
engine = module.build_sqlite_engine(database_url)
factory = module.sessionmaker(bind=engine, expire_on_commit=False)

try:
    tenant_id = module._stable_id("tenant", config.tenant_slug)
    user_id = module._stable_id("user", config.tenant_slug, config.username)
    role_id = module._stable_id(
        "role", config.tenant_slug, config.workspace_key, "test-user"
    )
    workspace_id = module._stable_id(
        "workspace", config.tenant_slug, config.workspace_key
    )
    membership_id = module._stable_id(
        "workspace-membership",
        str(tenant_id),
        str(workspace_id),
        str(user_id),
        str(role_id),
    )
    credential_id = module._stable_id(
        "password-credential", config.tenant_slug, config.username
    )
    agent_id = module._stable_id(
        "agent", config.tenant_slug, config.workspace_key, config.agent_key
    )
    device_id = (
        module._stable_id("device", config.tenant_slug, config.device_key)
        if config.device_key is not None
        else None
    )

    workspace_expected = {
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "workspace_key": config.workspace_key,
        "display_name": config.workspace_display_name,
        "status": "active",
        "created_at": module._SEED_TIME,
    }
    user_expected = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "subject": config.username,
        "display_name": config.user_display_name,
        "email": None,
        "status": "active",
        "created_at": module._SEED_TIME,
    }
    membership_expected = {
        "tenant_id": tenant_id,
        "workspace_membership_id": membership_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "role_id": role_id,
        "status": "active",
        "joined_at": module._SEED_TIME,
        "revoked_at": None,
    }

    with factory.begin() as session:
        workspaces = (
            session.query(module.WorkspaceModel)
            .filter(
                module.and_(
                    module.WorkspaceModel.tenant_id == tenant_id,
                    module.or_(
                        module.WorkspaceModel.workspace_id == workspace_id,
                        module.WorkspaceModel.workspace_key == config.workspace_key,
                    ),
                )
            )
            .limit(2)
            .all()
        )
        if len(workspaces) != 1:
            print("reconcile_refused=workspace_identity_not_unique")
            raise SystemExit(10)
        workspace = workspaces[0]
        drift = sorted(
            field
            for field, expected in workspace_expected.items()
            if getattr(workspace, field) != expected
        )
        if drift:
            print("reconcile_refused=workspace_non_creator_drift")
            print("workspace_other_mismatched_fields=" + ",".join(drift))
            raise SystemExit(11)

        # Use exactly the same identity selection semantics as seed_test_data.py:
        # tenant + (stable user_id OR configured subject), then require every
        # seeded field to match. This prevents the reconcile safety gate from
        # disagreeing with the real seed path while remaining fail-closed.
        current_users = (
            session.query(module.UserModel)
            .filter(
                module.and_(
                    module.UserModel.tenant_id == tenant_id,
                    module.or_(
                        module.UserModel.user_id == user_id,
                        module.UserModel.subject == config.username,
                    ),
                )
            )
            .limit(2)
            .all()
        )
        if len(current_users) != 1:
            print("reconcile_refused=current_seed_user_identity_not_unique")
            print(f"current_seed_user_match_count={len(current_users)}")
            raise SystemExit(12)
        user_drift = sorted(
            field
            for field, expected in user_expected.items()
            if getattr(current_users[0], field) != expected
        )
        if user_drift:
            print("reconcile_refused=current_seed_user_not_exact")
            print("current_seed_user_mismatched_fields=" + ",".join(user_drift))
            raise SystemExit(12)

        memberships = (
            session.query(module.WorkspaceMembershipModel)
            .filter(
                module.WorkspaceMembershipModel.tenant_id == tenant_id,
                module.or_(
                    module.WorkspaceMembershipModel.workspace_membership_id
                    == membership_id,
                    module.and_(
                        module.WorkspaceMembershipModel.workspace_id == workspace_id,
                        module.WorkspaceMembershipModel.user_id == user_id,
                        module.WorkspaceMembershipModel.role_id == role_id,
                    ),
                ),
            )
            .limit(2)
            .all()
        )
        if len(memberships) != 1:
            print("reconcile_refused=current_seed_membership_identity_not_unique")
            print(f"current_seed_membership_match_count={len(memberships)}")
            raise SystemExit(13)
        membership_drift = sorted(
            field
            for field, expected in membership_expected.items()
            if getattr(memberships[0], field) != expected
        )
        if membership_drift:
            print("reconcile_refused=current_seed_membership_not_exact")
            print("current_seed_membership_mismatched_fields=" + ",".join(membership_drift))
            raise SystemExit(13)

        old_creator = workspace.created_by
        if old_creator == user_id:
            print("creator_reference_kind=current_seed_user")
            print("workspace_creator_reconcile=ALREADY_CURRENT")
            raise SystemExit(20)

        referenced_users = []
        if old_creator is not None:
            referenced_users = (
                session.query(module.UserModel)
                .filter(module.UserModel.user_id == old_creator)
                .limit(2)
                .all()
            )
        if referenced_users:
            same_tenant = any(user.tenant_id == tenant_id for user in referenced_users)
            print("creator_reference_kind=existing_user")
            print(f"creator_existing_user_same_tenant={str(same_tenant).lower()}")
            print("reconcile_refused=created_by_still_names_real_user")
            raise SystemExit(14)

        deterministic = {
            tenant_id: "tenant_id",
            workspace_id: "workspace_id",
            role_id: "role_id",
            membership_id: "workspace_membership_id",
            credential_id: "password_credential_id",
            agent_id: "agent_id",
        }
        if device_id is not None:
            deterministic[device_id] = "device_id"
        creator_kind = (
            "null_or_missing"
            if old_creator is None
            else deterministic.get(old_creator, "orphan_identity")
        )

        other_workspace_count = (
            session.query(module.WorkspaceModel)
            .filter(
                module.WorkspaceModel.tenant_id == tenant_id,
                module.WorkspaceModel.workspace_id != workspace_id,
                module.WorkspaceModel.created_by == old_creator,
            )
            .count()
        )
        print(f"creator_reference_kind={creator_kind}")
        print(f"creator_other_workspace_references={other_workspace_count}")
        if other_workspace_count != 0:
            print("reconcile_refused=legacy_creator_is_shared")
            raise SystemExit(15)

        workspace.created_by = user_id
        session.flush()
        print("workspace_creator_reconcile=UPDATED")
finally:
    engine.dispose()
PY
status=$?
set -e

case "$status" in
  0)
    log "workspace creator reconcile=PASS"
    ;;
  20)
    log "workspace creator already current"
    ;;
  10|11|12|13|14|15)
    die "workspace creator reconcile was refused by safety policy"
    ;;
  *)
    die "workspace creator reconcile failed unexpectedly (status=$status)"
    ;;
esac

systemctl reset-failed hermes-cloud-sqlite-seed-test-data.service 2>/dev/null || true
if ! systemctl start hermes-cloud-sqlite-seed-test-data.service; then
  systemctl --no-pager -l status hermes-cloud-sqlite-seed-test-data.service || true
  journalctl -u hermes-cloud-sqlite-seed-test-data.service -n 120 --no-pager || true
  die "seed still fails after workspace creator reconcile"
fi
log "seed after reconcile=PASS"

curl -fsSL \
  https://raw.githubusercontent.com/looooooooy/hermes/main/deploy/staging/scripts/repair_and_resume_hermes_cloud_sqlite.sh \
  -o "$REPAIR_SCRIPT"
chmod 0700 "$REPAIR_SCRIPT"
HERMES_SERVER_NAME="$SERVER_NAME" "$REPAIR_SCRIPT"

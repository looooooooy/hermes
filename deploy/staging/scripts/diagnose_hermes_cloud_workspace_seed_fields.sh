#!/usr/bin/env bash
set -euo pipefail

CURRENT_LINK=/opt/hermes-cloud/current
ENV_FILE=/etc/hermes-cloud/sqlite/test-server.env
SECRET_ROOT=/etc/hermes-cloud/sqlite/secrets

log() { printf '[hermes-cloud-workspace-diagnose] %s\n' "$*"; }
die() { printf '[hermes-cloud-workspace-diagnose] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "diagnostic must run as root"
[[ -L "$CURRENT_LINK" ]] || die "current release link is missing"
CURRENT_ROOT=$(readlink -f "$CURRENT_LINK")
[[ -d "$CURRENT_ROOT" ]] || die "current release target is missing"
[[ -x "$CURRENT_ROOT/.venv/bin/python" ]] || die "current release Python is unavailable"
[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || die "staging environment file is unavailable"
[[ -f "$SECRET_ROOT/bootstrap_database_dsn" && ! -L "$SECRET_ROOT/bootstrap_database_dsn" ]] || die "bootstrap DSN is unavailable"

seed_runner="$CURRENT_ROOT/deploy/test_server/scripts/seed_test_data.py"
[[ -f "$seed_runner" && ! -L "$seed_runner" ]] || die "seed runner is unavailable"

env_args=()
while IFS= read -r line; do
  [[ -z "$line" || "$line" == \#* ]] && continue
  [[ "$line" =~ ^[A-Z][A-Z0-9_]*= ]] || die "invalid staging environment entry"
  env_args+=("$line")
done < "$ENV_FILE"
env_args+=("HERMES_BOOTSTRAP_DSN_FILE=$SECRET_ROOT/bootstrap_database_dsn")

runuser -u hermes-cloud-migrate -- env "${env_args[@]}" \
  "$CURRENT_ROOT/.venv/bin/python" - "$seed_runner" <<'PY'
from __future__ import annotations

import importlib.util
import os
import sys

runner_path = sys.argv[1]
spec = importlib.util.spec_from_file_location("hermes_cloud_workspace_seed_diagnostic", runner_path)
if spec is None or spec.loader is None:
    raise SystemExit("diagnostic_error=seed_runner_load_failed")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

engine = None
try:
    config = module.SeedConfig.from_environment(os.environ)
    database_url = module.read_secret_file(
        os.environ["HERMES_BOOTSTRAP_DSN_FILE"],
        name="bootstrap database",
    )
    driver = module.make_url(database_url).drivername
    if driver in {"sqlite", "sqlite+pysqlite"}:
        engine = module.build_sqlite_engine(database_url)
    elif driver in {"postgresql", "postgresql+psycopg"}:
        engine = module.create_engine(database_url, pool_pre_ping=True, poolclass=module.NullPool)
    else:
        raise SystemExit("diagnostic_error=unsupported_database_provider")

    factory = module.sessionmaker(bind=engine, expire_on_commit=False)
    tenant_id = module._stable_id("tenant", config.tenant_slug)
    user_id = module._stable_id("user", config.tenant_slug, config.username)
    workspace_id = module._stable_id("workspace", config.tenant_slug, config.workspace_key)
    expected = {
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "workspace_key": config.workspace_key,
        "display_name": config.workspace_display_name,
        "status": "active",
        "created_by": user_id,
        "created_at": module._SEED_TIME,
    }
    with factory() as session:
        rows = session.query(module.WorkspaceModel).filter(
            module.and_(
                module.WorkspaceModel.tenant_id == tenant_id,
                module.or_(
                    module.WorkspaceModel.workspace_id == workspace_id,
                    module.WorkspaceModel.workspace_key == config.workspace_key,
                ),
            )
        ).limit(2).all()
        if len(rows) != 1:
            print(f"workspace_row_count={len(rows)}")
            raise SystemExit(2)
        workspace = rows[0]
        mismatched = sorted(
            field for field, value in expected.items()
            if getattr(workspace, field) != value
        )
        identity_fields = {"tenant_id", "workspace_id", "workspace_key", "created_by"}
        unsafe = sorted(field for field in mismatched if field in identity_fields)
        safe = sorted(field for field in mismatched if field not in identity_fields)
        print("workspace_mismatched_fields=" + (",".join(mismatched) if mismatched else "none"))
        print("workspace_identity_drift=" + (",".join(unsafe) if unsafe else "none"))
        print("workspace_reconcilable_fields=" + (",".join(safe) if safe else "none"))
        if mismatched:
            raise SystemExit(2)
        print("workspace_seed=PASS")
finally:
    if engine is not None:
        engine.dispose()
PY
status=$?
case "$status" in
  0) log "diagnostic=PASS" ;;
  2) log "diagnostic=DRIFT" ;;
  *) die "workspace diagnostic failed" ;;
esac
exit "$status"

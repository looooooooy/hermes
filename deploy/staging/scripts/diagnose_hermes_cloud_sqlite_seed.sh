#!/usr/bin/env bash
set -euo pipefail

CURRENT_LINK=/opt/hermes-cloud/current
ENV_FILE=/etc/hermes-cloud/sqlite/test-server.env
SECRET_ROOT=/etc/hermes-cloud/sqlite/secrets

log() { printf '[hermes-cloud-seed-diagnose] %s\n' "$*"; }
die() { printf '[hermes-cloud-seed-diagnose] ERROR: %s\n' "$*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "diagnostic must run as root"
[[ -L "$CURRENT_LINK" ]] || die "current release link is missing"
CURRENT_ROOT=$(readlink -f "$CURRENT_LINK")
[[ -d "$CURRENT_ROOT" ]] || die "current release target is missing"
[[ -x "$CURRENT_ROOT/.venv/bin/python" ]] || die "current release Python is unavailable"
[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || die "staging environment file is unavailable"
[[ -f "$SECRET_ROOT/bootstrap_database_dsn" && ! -L "$SECRET_ROOT/bootstrap_database_dsn" ]] || die "bootstrap DSN is unavailable"
[[ -f "$SECRET_ROOT/initial_user_password" && ! -L "$SECRET_ROOT/initial_user_password" ]] || die "initial password reference is unavailable"

seed_runner="$CURRENT_ROOT/deploy/test_server/scripts/seed_test_data.py"
[[ -f "$seed_runner" && ! -L "$seed_runner" ]] || die "seed runner is unavailable"

# Preserve each NAME=value line as one argv element so display names containing spaces
# remain intact. The environment file is root-owned deployment metadata and contains no
# credential material; secrets remain file references below.
env_args=()
while IFS= read -r line; do
  [[ -z "$line" || "$line" == \#* ]] && continue
  [[ "$line" =~ ^[A-Z][A-Z0-9_]*= ]] || die "invalid staging environment entry"
  env_args+=("$line")
done < "$ENV_FILE"
env_args+=(
  "HERMES_BOOTSTRAP_DSN_FILE=$SECRET_ROOT/bootstrap_database_dsn"
  "HERMES_INITIAL_USER_PASSWORD_FILE=$SECRET_ROOT/initial_user_password"
)

set +e
runuser -u hermes-cloud-migrate -- env "${env_args[@]}" \
  "$CURRENT_ROOT/.venv/bin/python" - "$seed_runner" <<'PY'
from __future__ import annotations

import importlib.util
import os
import sys

runner_path = sys.argv[1]
spec = importlib.util.spec_from_file_location("hermes_cloud_staging_seed_diagnostic", runner_path)
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
    initial_password = module.read_secret_file(
        os.environ["HERMES_INITIAL_USER_PASSWORD_FILE"],
        name="initial password",
    )
    driver = module.make_url(database_url).drivername
    if driver in {"sqlite", "sqlite+pysqlite"}:
        engine = module.build_sqlite_engine(database_url)
    elif driver in {"postgresql", "postgresql+psycopg"}:
        engine = module.create_engine(database_url, pool_pre_ping=True, poolclass=module.NullPool)
    else:
        raise SystemExit("diagnostic_error=unsupported_database_provider")
    factory = module.sessionmaker(bind=engine, expire_on_commit=False)
    try:
        result = module.seed_test_data(
            session_factory=factory,
            config=config,
            initial_password=initial_password,
            apply=False,
        )
    except module.SeedConflict as error:
        # SeedConflict messages are deliberately constructed only from fixed resource
        # labels; they never contain row values, password hashes, tokens, or DSNs.
        reason = str(error)
        print(f"seed_conflict={reason}")
        if reason == "password credential conflicts with seed":
            tenant_id = module._stable_id("tenant", config.tenant_slug)
            user_id = module._stable_id("user", config.tenant_slug, config.username)
            credential_id = module._stable_id(
                "password-credential",
                config.tenant_slug,
                config.username,
            )
            with factory() as session:
                rows = session.query(module.PasswordCredentialModel).filter(
                    module.or_(
                        module.PasswordCredentialModel.subject == config.username,
                        module.and_(
                            module.PasswordCredentialModel.tenant_id == tenant_id,
                            module.or_(
                                module.PasswordCredentialModel.credential_id == credential_id,
                                module.PasswordCredentialModel.user_id == user_id,
                            ),
                        ),
                    )
                ).limit(2).all()
                if len(rows) == 1:
                    credential = rows[0]
                    expected = {
                        "tenant_id": tenant_id,
                        "credential_id": credential_id,
                        "user_id": user_id,
                        "subject": config.username,
                        "status": "active",
                        "created_at": module._SEED_TIME,
                        "updated_at": module._SEED_TIME,
                    }
                    mismatched = sorted(
                        field for field, value in expected.items()
                        if getattr(credential, field) != value
                    )
                    print(
                        "credential_mismatched_fields="
                        + (",".join(mismatched) if mismatched else "none")
                    )
                    password_matches = module.Argon2PasswordHasher().verify(
                        credential.password_hash,
                        initial_password,
                    )
                    print(f"password_matches_current_secret={str(password_matches).lower()}")
        raise SystemExit(2)
    except Exception:
        # Keep the CLI boundary redacted. Unknown errors require a separate controlled
        # diagnostic rather than echoing arbitrary exception text into operator logs.
        print("diagnostic_error=seed_dry_run_failed")
        raise SystemExit(3)
    else:
        print(
            f"seed_plan=PASS created={result.created} existing={result.existing} updated={result.updated}"
        )
finally:
    if engine is not None:
        engine.dispose()
PY
status=$?
set -e

case "$status" in
  0) log "diagnostic=PASS no seed conflict detected" ;;
  2) log "diagnostic=CONFLICT" ;;
  *) die "seed diagnostic could not classify the failure" ;;
esac
exit "$status"

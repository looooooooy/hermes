#!/usr/bin/env bash
set -euo pipefail

mode=${1:-}
if [[ "$mode" != "--runtime" && "$mode" != "--migration" && "$mode" != "--seed" && "$mode" != "--connector" && "$mode" != "--connector-token" ]]; then
  echo "usage: preflight.sh --runtime|--migration|--seed|--connector|--connector-token" >&2
  exit 64
fi
if ((EUID == 0)); then
  echo "preflight must run as the configured non-root service user" >&2
  exit 77
fi

: "${HERMES_CURRENT:?HERMES_CURRENT is required}"
: "${HERMES_VENV:?HERMES_VENV is required}"

[[ -d "$HERMES_CURRENT" ]] || {
  echo "current release directory is missing" >&2
  exit 78
}
[[ -x "$HERMES_VENV/bin/python" ]] || {
  echo "release virtual environment is missing Python" >&2
  exit 78
}

validate_secret_file() {
  local credential_path=$1
  local credential_name=$2

  if ! "$HERMES_VENV/bin/python" - "$credential_path" "$credential_name" <<'PY'
import os
import stat
import sys

path, name = sys.argv[1:]
if not os.path.isabs(path):
    raise SystemExit(f"{name} credential path must be absolute")
try:
    metadata = os.lstat(path)
except OSError:
    raise SystemExit(f"{name} credential is unavailable") from None
if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
    raise SystemExit(f"{name} credential must be a regular non-symlink file")
if stat.S_IMODE(metadata.st_mode) & ~0o600:
    raise SystemExit(f"{name} credential permissions must not exceed 0600")
if metadata.st_size == 0:
    raise SystemExit(f"{name} credential must not be empty")
PY
  then
    exit 78
  fi
}

validate_connector_signing_secret() {
  local credential_path=$1
  validate_secret_file "$credential_path" "connector signing"
  if ! "$HERMES_VENV/bin/python" - "$credential_path" <<'PY'
import sys

from hermes_cloud.adapters.connector_auth import (
    ConnectorAuthenticationConfigurationError,
    read_connector_signing_secret,
)

try:
    read_connector_signing_secret(sys.argv[1])
except ConnectorAuthenticationConfigurationError:
    raise SystemExit("connector signing credential content is invalid") from None
PY
  then
    exit 78
  fi
}

if [[ "$mode" == "--migration" ]]; then
  : "${HERMES_MIGRATION_DSN_FILE:?HERMES_MIGRATION_DSN_FILE is required}"
  validate_secret_file "$HERMES_MIGRATION_DSN_FILE" "migration"
  migration_runner="$HERMES_CURRENT/deploy/test_server/scripts/run_migrations.py"
  [[ -f "$migration_runner" && ! -L "$migration_runner" ]] || {
    echo "migration runner is missing or unsafe" >&2
    exit 78
  }
  "$HERMES_VENV/bin/python" - "$migration_runner" <<'PY'
import runpy
import sys

runpy.run_path(sys.argv[1], run_name="hermes_cloud_migration_preflight")
PY
elif [[ "$mode" == "--seed" ]]; then
  : "${HERMES_BOOTSTRAP_DSN_FILE:?HERMES_BOOTSTRAP_DSN_FILE is required}"
  : "${HERMES_INITIAL_USER_PASSWORD_FILE:?HERMES_INITIAL_USER_PASSWORD_FILE is required}"
  validate_secret_file "$HERMES_BOOTSTRAP_DSN_FILE" "bootstrap database"
  validate_secret_file "$HERMES_INITIAL_USER_PASSWORD_FILE" "initial password"
  seed_runner="$HERMES_CURRENT/deploy/test_server/scripts/seed_test_data.py"
  [[ -f "$seed_runner" && ! -L "$seed_runner" ]] || {
    echo "seed runner is missing or unsafe" >&2
    exit 78
  }
  "$HERMES_VENV/bin/python" - "$seed_runner" <<'PY'
import runpy
import sys

runpy.run_path(sys.argv[1], run_name="hermes_cloud_seed_preflight")
PY
elif [[ "$mode" == "--connector-token" ]]; then
  : "${HERMES_CONNECTOR_SIGNING_SECRET_FILE:?HERMES_CONNECTOR_SIGNING_SECRET_FILE is required}"
  : "${HERMES_RUNTIME_DSN_FILE:?HERMES_RUNTIME_DSN_FILE is required}"
  : "${HERMES_CONNECTOR_TOKEN_OUTPUT:?HERMES_CONNECTOR_TOKEN_OUTPUT is required}"
  validate_connector_signing_secret "$HERMES_CONNECTOR_SIGNING_SECRET_FILE"
  validate_secret_file "$HERMES_RUNTIME_DSN_FILE" "runtime database"
  token_runner="$HERMES_CURRENT/deploy/test_server/scripts/mint_connector_token.py"
  [[ -f "$token_runner" && ! -L "$token_runner" ]] || {
    echo "connector token mint runner is missing or unsafe" >&2
    exit 78
  }
  "$HERMES_VENV/bin/python" - "$token_runner" <<'PY'
import os
import runpy
import sys

namespace = runpy.run_path(
    sys.argv[1],
    run_name="hermes_cloud_token_mint_preflight",
)
config = namespace["ConnectorTokenMintConfig"].from_environment(os.environ)
if not config.owner_control_enabled:
    raise SystemExit("owner-control token mint is required")
namespace["_validate_output"](os.environ["HERMES_CONNECTOR_TOKEN_OUTPUT"])
PY
  "$HERMES_VENV/bin/python" "$token_runner" >/dev/null
elif [[ "$mode" == "--connector" ]]; then
  : "${HERMES_RELEASES_DIR:?HERMES_RELEASES_DIR is required}"
  : "${HERMES_CONNECTOR_SIGNING_SECRET_FILE:?HERMES_CONNECTOR_SIGNING_SECRET_FILE is required}"
  [[ -d "$HERMES_RELEASES_DIR" ]] || {
    echo "release directory is missing" >&2
    exit 78
  }
  [[ -x "$HERMES_VENV/bin/uvicorn" ]] || {
    echo "release virtual environment is missing Uvicorn" >&2
    exit 78
  }
  validate_connector_signing_secret "$HERMES_CONNECTOR_SIGNING_SECRET_FILE"
  "$HERMES_VENV/bin/python" - <<'PY'
from importlib import import_module

module = import_module("hermes_cloud.entrypoints.connector_gateway.bootstrap")
if not module.app:
    raise SystemExit("connector gateway entrypoint is unavailable")
PY
else
  : "${HERMES_RELEASES_DIR:?HERMES_RELEASES_DIR is required}"
  : "${HERMES_RUNTIME_DSN_FILE:?HERMES_RUNTIME_DSN_FILE is required}"
  : "${HERMES_SIGNING_SECRET_FILE:?HERMES_SIGNING_SECRET_FILE is required}"
  [[ -d "$HERMES_RELEASES_DIR" ]] || {
    echo "release directory is missing" >&2
    exit 78
  }
  [[ -x "$HERMES_VENV/bin/uvicorn" ]] || {
    echo "release virtual environment is missing Uvicorn" >&2
    exit 78
  }
  validate_secret_file "$HERMES_RUNTIME_DSN_FILE" "runtime"
  validate_secret_file "$HERMES_SIGNING_SECRET_FILE" "signing"
  "$HERMES_VENV/bin/python" - <<'PY'
from importlib import import_module

for module_name in (
    "hermes_cloud.entrypoints.business_api.bootstrap",
    "hermes_cloud.entrypoints.connector_gateway.bootstrap",
    "hermes_cloud.entrypoints.worker",
    "hermes_cloud.entrypoints.file_gateway.bootstrap",
):
    import_module(module_name)
PY
fi

echo "preflight=PASS mode=${mode#--}"

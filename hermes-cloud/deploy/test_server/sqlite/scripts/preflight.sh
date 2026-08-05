#!/usr/bin/env bash
set -euo pipefail

mode=${1:-}
case "$mode" in
  --sqlite-business|--sqlite-connector|--sqlite-connector-token|--sqlite-migration|--sqlite-seed|--sqlite-seed-cleanup) ;;
  *)
    echo "usage: preflight.sh --sqlite-business|--sqlite-connector|--sqlite-connector-token|--sqlite-migration|--sqlite-seed|--sqlite-seed-cleanup" >&2
    exit 64
    ;;
esac

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

validate_reference() {
  "$HERMES_VENV/bin/python" - "$1" <<'PY'
import sys

from hermes_cloud.configuration import DsnFileReference

DsnFileReference(sys.argv[1]).read()
PY
}

validate_observer_keyring() {
  : "${HERMES_OBSERVER_KEYRING_FILE:?HERMES_OBSERVER_KEYRING_FILE is required}"
  [[ "$HERMES_OBSERVER_KEYRING_FILE" == /* ]] || {
    echo "observer keyring path must be absolute" >&2
    exit 78
  }
  "$HERMES_VENV/bin/python" - "$HERMES_OBSERVER_KEYRING_FILE" <<'PY'
import sys

from hermes_cloud.platform.sqlalchemy.observer_encryption import (
    read_tenant_kek_registry,
)

read_tenant_kek_registry(sys.argv[1])
PY
}

validate_asgi_runtime() {
  : "${HERMES_RELEASES_DIR:?HERMES_RELEASES_DIR is required}"
  [[ -d "$HERMES_RELEASES_DIR" ]] || {
    echo "release directory is missing" >&2
    exit 78
  }
  [[ -x "$HERMES_VENV/bin/uvicorn" ]] || {
    echo "release virtual environment is missing Uvicorn" >&2
    exit 78
  }
}

if [[ "$mode" == "--sqlite-business" ]]; then
  validate_asgi_runtime
  : "${HERMES_RUNTIME_DSN_FILE:?HERMES_RUNTIME_DSN_FILE is required}"
  : "${HERMES_SIGNING_SECRET_FILE:?HERMES_SIGNING_SECRET_FILE is required}"
  : "${HERMES_CONNECTOR_SIGNING_SECRET_FILE:?HERMES_CONNECTOR_SIGNING_SECRET_FILE is required}"
  validate_observer_keyring
  validate_reference "$HERMES_RUNTIME_DSN_FILE"
  validate_reference "$HERMES_SIGNING_SECRET_FILE"
  validate_reference "$HERMES_CONNECTOR_SIGNING_SECRET_FILE"
  "$HERMES_VENV/bin/python" - <<'PY'
import asyncio
import os

from hermes_cloud.adapters.connector_auth import read_connector_signing_secret
from hermes_cloud.configuration import DsnFileReference
from hermes_cloud.entrypoints.business_api import bootstrap
from hermes_cloud.platform.sqlite.engine import (
    require_sqlite_version,
    sqlite_database_path,
)

database_url = DsnFileReference(os.environ["HERMES_RUNTIME_DSN_FILE"]).read()
read_connector_signing_secret(
    os.environ["HERMES_CONNECTOR_SIGNING_SECRET_FILE"]
)
require_sqlite_version()
sqlite_database_path(database_url)
if not bootstrap.app:
    raise SystemExit("business API entrypoint is unavailable")
route_methods = {
    (method, route.path)
    for route in bootstrap.app.routes
    for method in getattr(route, "methods", ())
}
required_pairing_routes = {
    ("POST", "/api/device-pairing/offers"),
    ("GET", "/api/device-pairing/sessions/{pairing_session_id}"),
}
if not required_pairing_routes <= route_methods:
    raise SystemExit("business API device pairing route is unavailable")

async def verify_ready() -> None:
    await bootstrap.app.startup()
    try:
        if not bootstrap.app.snapshot().get("ready"):
            raise SystemExit("business API production composition is unready")
    finally:
        await bootstrap.app.shutdown()

asyncio.run(verify_ready())
PY
elif [[ "$mode" == "--sqlite-connector" ]]; then
  validate_asgi_runtime
  : "${HERMES_RUNTIME_DSN_FILE:?HERMES_RUNTIME_DSN_FILE is required}"
  : "${HERMES_CONNECTOR_SIGNING_SECRET_FILE:?HERMES_CONNECTOR_SIGNING_SECRET_FILE is required}"
  validate_observer_keyring
  validate_reference "$HERMES_RUNTIME_DSN_FILE"
  validate_reference "$HERMES_CONNECTOR_SIGNING_SECRET_FILE"
  "$HERMES_VENV/bin/python" - <<'PY'
import asyncio
import os

from hermes_cloud.configuration import DsnFileReference
from hermes_cloud.entrypoints.connector_gateway import bootstrap
from hermes_cloud.platform.sqlite.engine import (
    require_sqlite_version,
    sqlite_database_path,
)

database_url = DsnFileReference(os.environ["HERMES_RUNTIME_DSN_FILE"]).read()
require_sqlite_version()
sqlite_database_path(database_url)
if not bootstrap.app:
    raise SystemExit("connector gateway entrypoint is unavailable")

async def verify_ready() -> None:
    await bootstrap.app.startup()
    try:
        if not bootstrap.app.snapshot().get("ready"):
            raise SystemExit("connector gateway production composition is unready")
    finally:
        await bootstrap.app.shutdown()

asyncio.run(verify_ready())
PY
elif [[ "$mode" == "--sqlite-connector-token" ]]; then
  : "${HERMES_RUNTIME_DSN_FILE:?HERMES_RUNTIME_DSN_FILE is required}"
  : "${HERMES_CONNECTOR_SIGNING_SECRET_FILE:?HERMES_CONNECTOR_SIGNING_SECRET_FILE is required}"
  : "${HERMES_CONNECTOR_TOKEN_OUTPUT:?HERMES_CONNECTOR_TOKEN_OUTPUT is required}"
  validate_reference "$HERMES_RUNTIME_DSN_FILE"
  validate_reference "$HERMES_CONNECTOR_SIGNING_SECRET_FILE"
  token_runner="$HERMES_CURRENT/deploy/test_server/scripts/mint_connector_token.py"
  [[ -f "$token_runner" && ! -L "$token_runner" ]] || {
    echo "Connector token mint runner is missing or unsafe" >&2
    exit 78
  }
  "$HERMES_VENV/bin/python" - "$token_runner" <<'PY'
import os
import runpy
import sys

namespace = runpy.run_path(
    sys.argv[1],
    run_name="hermes_cloud_sqlite_connector_token_preflight",
)
config = namespace["ConnectorTokenMintConfig"].from_environment(os.environ)
if not config.owner_control_enabled:
    raise SystemExit("owner-control token mint is required")
namespace["_validate_output"](os.environ["HERMES_CONNECTOR_TOKEN_OUTPUT"])
namespace["read_connector_signing_secret"](
    os.environ["HERMES_CONNECTOR_SIGNING_SECRET_FILE"]
)
PY
  "$HERMES_VENV/bin/python" "$token_runner" >/dev/null
elif [[ "$mode" == "--sqlite-migration" ]]; then
  : "${HERMES_MIGRATION_DSN_FILE:?HERMES_MIGRATION_DSN_FILE is required}"
  validate_observer_keyring
  validate_reference "$HERMES_MIGRATION_DSN_FILE"
  migration_runner="$HERMES_CURRENT/deploy/test_server/scripts/migrate_sqlite.py"
  [[ -f "$migration_runner" && ! -L "$migration_runner" ]] || {
    echo "SQLite migration runner is missing or unsafe" >&2
    exit 78
  }
  "$HERMES_VENV/bin/python" - "$migration_runner" <<'PY'
import os
import runpy
import sys

from hermes_cloud.configuration import DsnFileReference
from hermes_cloud.platform.sqlite.engine import (
    require_sqlite_version,
    sqlite_database_path,
)

database_url = DsnFileReference(os.environ["HERMES_MIGRATION_DSN_FILE"]).read()
require_sqlite_version()
sqlite_database_path(database_url, allow_missing=True)
runpy.run_path(sys.argv[1], run_name="hermes_cloud_sqlite_migration_preflight")
PY
elif [[ "$mode" == "--sqlite-seed-cleanup" ]]; then
  : "${HERMES_BOOTSTRAP_DSN_FILE:?HERMES_BOOTSTRAP_DSN_FILE is required}"
  validate_reference "$HERMES_BOOTSTRAP_DSN_FILE"
  cleanup_runner="$HERMES_CURRENT/deploy/test_server/scripts/cleanup_test_seed_session.py"
  [[ -f "$cleanup_runner" && ! -L "$cleanup_runner" ]] || {
    echo "SQLite seed cleanup runner is missing or unsafe" >&2
    exit 78
  }
  "$HERMES_VENV/bin/python" - "$cleanup_runner" <<'PY'
import os
import runpy
import sys

from hermes_cloud.configuration import DsnFileReference
from hermes_cloud.platform.sqlite.engine import (
    require_sqlite_version,
    sqlite_database_path,
)

database_url = DsnFileReference(os.environ["HERMES_BOOTSTRAP_DSN_FILE"]).read()
require_sqlite_version()
sqlite_database_path(database_url)
namespace = runpy.run_path(
    sys.argv[1],
    run_name="hermes_cloud_sqlite_seed_cleanup_preflight",
)
namespace["CleanupConfig"].from_environment(os.environ)
PY
else
  : "${HERMES_BOOTSTRAP_DSN_FILE:?HERMES_BOOTSTRAP_DSN_FILE is required}"
  : "${HERMES_INITIAL_USER_PASSWORD_FILE:?HERMES_INITIAL_USER_PASSWORD_FILE is required}"
  validate_reference "$HERMES_BOOTSTRAP_DSN_FILE"
  validate_reference "$HERMES_INITIAL_USER_PASSWORD_FILE"
  seed_runner="$HERMES_CURRENT/deploy/test_server/scripts/seed_test_data.py"
  [[ -f "$seed_runner" && ! -L "$seed_runner" ]] || {
    echo "SQLite seed runner is missing or unsafe" >&2
    exit 78
  }
  "$HERMES_VENV/bin/python" - "$seed_runner" <<'PY'
import os
import runpy
import sys

from hermes_cloud.configuration import DsnFileReference
from hermes_cloud.platform.sqlite.engine import (
    require_sqlite_version,
    sqlite_database_path,
)

database_url = DsnFileReference(os.environ["HERMES_BOOTSTRAP_DSN_FILE"]).read()
require_sqlite_version()
sqlite_database_path(database_url)
runpy.run_path(sys.argv[1], run_name="hermes_cloud_sqlite_seed_preflight")
PY
fi

echo "preflight=PASS mode=${mode#--}"

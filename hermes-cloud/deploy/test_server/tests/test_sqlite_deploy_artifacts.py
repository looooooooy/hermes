from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session
from test_mint_connector_token import (
    CREDENTIAL_ID,
    DEVICE_ID,
    NOW,
    TENANT_ID,
    _active_owner_control_database,
)

from hermes_cloud.contracts.observer_v2 import require_cloud_frame
from hermes_cloud.platform.postgres.models import DeviceCredentialModel

ROOT = Path(__file__).parents[1]
CLOUD_ROOT = ROOT.parents[1]
SQLITE = ROOT / "sqlite"
SYSTEMD = SQLITE / "systemd"
PREFLIGHT = SQLITE / "scripts" / "preflight.sh"
ENVIRONMENT = SQLITE / "env" / "test-server.env.example"
README = SQLITE / "README.md"
NGINX = SQLITE / "nginx" / "hermes-test-server.conf"
TOKEN_UNIT = SYSTEMD / "hermes-cloud-sqlite-mint-connector-token.service"

UNITS = {
    "business": SYSTEMD / "hermes-cloud-sqlite-business-api.service",
    "connector": SYSTEMD / "hermes-cloud-sqlite-connector-gateway.service",
    "migration": SYSTEMD / "hermes-cloud-sqlite-migrate.service",
    "seed": SYSTEMD / "hermes-cloud-sqlite-seed-test-data.service",
}

SECRET_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:password|token|ticket|dsn|secret)\s*=\s*[^/$%\s]"
)


def _write_private(path: Path, value: str) -> str:
    path.write_text(value)
    path.chmod(0o600)
    return str(path)


def _observer_keyring(path: Path) -> str:
    return _write_private(
        path,
        json.dumps(
            {
                "version": 1,
                "tenants": {
                    "10000000-0000-4000-8000-000000000001": {
                        "current": "test-v1",
                        "keys": {
                            "test-v1": base64.b64encode(b"k" * 32).decode("ascii")
                        },
                    }
                },
            }
        ),
    )


def _preflight_environment(directory: Path) -> dict[str, str]:
    virtual_environment = directory / "venv"
    binaries = virtual_environment / "bin"
    binaries.mkdir(parents=True)
    python = binaries / "python"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o700)
    uvicorn = binaries / "uvicorn"
    uvicorn.write_text("#!/bin/sh\nexit 0\n")
    uvicorn.chmod(0o700)
    # Secret-file references installed by a deployment wrapper must never leak
    # into hermetic preflight cases; every case sets its own fixture paths.
    inherited = {
        name: value
        for name, value in os.environ.items()
        if name
        not in {
            "HERMES_OBSERVER_KEYRING_FILE",
            "HERMES_RUNTIME_DSN_FILE",
            "HERMES_MIGRATION_DSN_FILE",
            "HERMES_BOOTSTRAP_DSN_FILE",
            "HERMES_SIGNING_SECRET_FILE",
            "HERMES_CONNECTOR_SIGNING_SECRET_FILE",
        }
    }
    return {
        **inherited,
        "HERMES_CURRENT": str(CLOUD_ROOT),
        "HERMES_RELEASES_DIR": str(CLOUD_ROOT.parent),
        "HERMES_VENV": str(virtual_environment),
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                (
                    str(CLOUD_ROOT / "src"),
                    os.environ.get("PYTHONPATH"),
                ),
            )
        ),
    }


class SQLiteDeployArtifactTest(unittest.TestCase):
    def test_installed_runtime_can_validate_the_observer_v2_gateway_ready_frame(
        self,
    ) -> None:
        frame = {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "gateway.ready",
                "payload": {
                    "observer_contract": 2,
                    "connection_role": "observer",
                },
            },
        }

        self.assertIs(require_cloud_frame("gateway_ready", frame), frame)

    def test_sqlite_readme_publishes_the_v11_upgrade_contract(self) -> None:
        readme = README.read_text()
        normalized = " ".join(readme.split())

        self.assertIn("current local schema revision is 11", normalized)
        self.assertIn("`0011_session_projection_durable_identity`", readme)
        self.assertIn(
            "verified historical sources are revisions 1 through 10",
            normalized,
        )
        self.assertIn("historical_source_count=10", readme)
        self.assertIn("pair is `(9, 10)`", normalized)
        self.assertIn("v1-to-v11", readme)

    def test_sqlite_readme_uses_platform_independent_release_toolchain_identity(
        self,
    ) -> None:
        readme = README.read_text()
        normalized = " ".join(readme.split())

        self.assertIn("canonical uv version `0.9.25`", normalized)
        self.assertIn("platform-specific `uv --version` display", normalized)
        self.assertIn("out-of-band raw audit", normalized)
        self.assertIn("must not affect stable evidence or Release ID", normalized)
        self.assertIn("artifact SHA-256 is platform-specific acquisition evidence", normalized)
        self.assertIn("must not reuse a macOS artifact digest for Linux", normalized)
        self.assertIn("required-integration source identity", normalized)
        self.assertIn("integration-source-lock.json", normalized)
        self.assertIn("clean copy containing only declared Cloud release inputs", normalized)
        self.assertIn("208-file integration lock", normalized)

    def test_sqlite_readme_names_all_units_and_uses_p0_specific_health(self) -> None:
        readme = README.read_text()

        for unit in sorted(path.name for path in SYSTEMD.glob("*.service")):
            with self.subTest(unit=unit):
                self.assertIn(f"`{unit}`", readme)
        self.assertEqual(len(tuple(SYSTEMD.glob("*.service"))), 5)
        self.assertIn(
            "does not use `deploy/test_server/scripts/health.sh`",
            readme,
        )
        self.assertIn("127.0.0.1:8101", readme)
        self.assertIn("127.0.0.1:8102", readme)

    def test_owner_control_bridge_is_private_shared_and_gateway_ordered(
        self,
    ) -> None:
        business = UNITS["business"].read_text()
        connector = UNITS["connector"].read_text()
        socket = (
            "Environment=HERMES_OWNER_CONTROL_SOCKET="
            "/run/hermes-cloud-sqlite-owner-control/owner-control.sock"
        )
        for unit in (business, connector):
            self.assertIn(socket, unit)
            unit_lines = unit.splitlines()
            self.assertIn("User=hermes-cloud", unit_lines)
            self.assertIn("Group=hermes-cloud", unit_lines)
        self.assertIn(
            "RuntimeDirectory=hermes-cloud-sqlite-owner-control",
            connector,
        )
        self.assertIn("RuntimeDirectoryMode=0700", connector)
        self.assertNotIn("RuntimeDirectory=", business)
        self.assertNotIn("RuntimeDirectoryMode=", business)
        self.assertIn(
            "Wants=hermes-cloud-sqlite-connector-gateway.service",
            business,
        )
        self.assertIn(
            "After=hermes-cloud-sqlite-connector-gateway.service",
            business,
        )

    def test_legacy_units_use_absolute_secret_files_without_loadcredential(
        self,
    ) -> None:
        expected_references = {
            "business": (
                (
                    "Environment=HERMES_RUNTIME_DSN_FILE="
                    "/etc/hermes-cloud/sqlite/secrets/runtime_database_dsn"
                ),
                (
                    "Environment=HERMES_SIGNING_SECRET_FILE="
                    "/etc/hermes-cloud/sqlite/secrets/business_api_signing_secret"
                ),
                (
                    "Environment=HERMES_CONNECTOR_SIGNING_SECRET_FILE="
                    "/etc/hermes-cloud/sqlite/secrets/connector_signing_secret"
                ),
                (
                    "Environment=HERMES_OBSERVER_KEYRING_FILE="
                    "/etc/hermes-cloud/sqlite/secrets/observer_keyring.json"
                ),
            ),
            "connector": (
                (
                    "Environment=HERMES_RUNTIME_DSN_FILE="
                    "/etc/hermes-cloud/sqlite/secrets/runtime_database_dsn"
                ),
                (
                    "Environment=HERMES_CONNECTOR_SIGNING_SECRET_FILE="
                    "/etc/hermes-cloud/sqlite/secrets/connector_signing_secret"
                ),
                (
                    "Environment=HERMES_OBSERVER_KEYRING_FILE="
                    "/etc/hermes-cloud/sqlite/secrets/observer_keyring.json"
                ),
            ),
            "migration": (
                (
                    "Environment=HERMES_MIGRATION_DSN_FILE="
                    "/etc/hermes-cloud/sqlite/secrets/migration_database_dsn"
                ),
                (
                    "Environment=HERMES_OBSERVER_KEYRING_FILE="
                    "/etc/hermes-cloud/sqlite/secrets/observer_keyring.json"
                ),
            ),
            "seed": (
                (
                    "Environment=HERMES_BOOTSTRAP_DSN_FILE="
                    "/etc/hermes-cloud/sqlite/secrets/bootstrap_database_dsn"
                ),
                (
                    "Environment=HERMES_INITIAL_USER_PASSWORD_FILE="
                    "/etc/hermes-cloud/sqlite/secrets/initial_user_password"
                ),
            ),
        }

        for name, unit in UNITS.items():
            with self.subTest(unit=name):
                text = unit.read_text()
                self.assertNotIn("LoadCredential", text)
                self.assertNotIn("%d/", text)
                self.assertNotIn("User=root", text)
                self.assertIn("NoNewPrivileges=true", text)
                self.assertIn("PrivateTmp=true", text)
                self.assertIn("ProtectSystem=strict", text)
                self.assertIn("ProtectHome=true", text)
                self.assertIn("CapabilityBoundingSet=", text)
                self.assertIsNone(SECRET_ASSIGNMENT.search(text))
                for reference in expected_references[name]:
                    self.assertIn(reference, text)
        self.assertIn(
            "SupplementaryGroups=hermes-cloud",
            UNITS["migration"].read_text(),
        )

    def test_units_use_exact_sqlite_preflight_modes_and_shared_database_group(
        self,
    ) -> None:
        expected_modes = {
            "business": "--sqlite-business",
            "connector": "--sqlite-connector",
            "migration": "--sqlite-migration",
            "seed": "--sqlite-seed",
        }

        for name, unit in UNITS.items():
            with self.subTest(unit=name):
                text = unit.read_text()
                self.assertIn("Group=hermes-cloud", text)
                self.assertIn("UMask=0007", text)
                self.assertIn("StateDirectory=hermes-cloud-sqlite", text)
                self.assertIn("StateDirectoryMode=0770", text)
                self.assertIn(
                    f"scripts/preflight.sh {expected_modes[name]}",
                    text,
                )
                self.assertIn(
                    "EnvironmentFile=/etc/hermes-cloud/sqlite/test-server.env",
                    text,
                )

        self.assertIn(
            "scripts/migrate_sqlite.py --apply",
            UNITS["migration"].read_text(),
        )
        self.assertIn(
            "scripts/seed_test_data.py --apply",
            UNITS["seed"].read_text(),
        )

    def test_bundle_documents_systemd_239_secret_ownership_and_p0_scope(
        self,
    ) -> None:
        environment = ENVIRONMENT.read_text()
        readme = README.read_text()
        preflight = PREFLIGHT.read_text()

        self.assertNotIn("LoadCredential", preflight)
        self.assertIn("--sqlite-business", preflight)
        self.assertIn("--sqlite-connector", preflight)
        self.assertIn("--sqlite-connector-token", preflight)
        self.assertIn("--sqlite-migration", preflight)
        self.assertIn("--sqlite-seed", preflight)
        self.assertIn("--sqlite-seed-cleanup", preflight)
        self.assertIn("DsnFileReference", preflight)
        self.assertIn("sqlite_database_path", preflight)
        self.assertIn(
            '("GET", "/api/device-pairing/sessions/{pairing_session_id}")',
            preflight,
        )
        self.assertIn("HERMES_CURRENT=", environment)
        self.assertIn("HERMES_BUSINESS_API_PORT=", environment)
        self.assertIn("HERMES_CONNECTOR_GATEWAY_PORT=", environment)
        self.assertIsNone(SECRET_ASSIGNMENT.search(environment))

        self.assertIn("systemd 239", readme)
        self.assertIn("0600", readme)
        self.assertIn("0660", readme)
        self.assertIn("hermes-cloud-migrate", readme)
        self.assertIn("Business API", readme)
        self.assertIn("Connector Gateway", readme)
        self.assertIn("File Gateway", readme)
        self.assertIn("Worker", readme)
        self.assertIn("not enabled", readme)
        self.assertNotIn("LoadCredential", readme)
        self.assertIn("cleanup_test_seed_session.py", readme)
        self.assertIn("before revision 11", readme)
        self.assertIn(
            "Observer V2 state, lease, and intent rows require their parent rows",
            readme,
        )
        self.assertIn("Observer Inbox has no session dimension", readme)
        self.assertIn("SQLite writer ownership", readme)
        self.assertIn("does not block future writes after commit", readme)
        self.assertIn("services stopped", readme)
        self.assertIn("1,024", readme)
        self.assertIn(
            "does not add an index to immutable revision 10",
            readme,
        )

    def test_each_sqlite_preflight_mode_validates_only_its_service_inputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runtime_dsn, engine = _active_owner_control_database(directory)
            engine.dispose()
            database_url = Path(runtime_dsn).read_text()
            migration_dsn = _write_private(
                directory / "migration-dsn",
                database_url,
            )
            bootstrap_dsn = _write_private(
                directory / "bootstrap-dsn",
                database_url,
            )
            business_signing = _write_private(
                directory / "business-signing",
                "business-signing-key-material-at-least-32-bytes",
            )
            connector_signing = _write_private(
                directory / "connector-signing",
                "connector-signing-key-material-at-least-32-bytes",
            )
            initial_password = _write_private(
                directory / "initial-password",
                "initial-password-value",
            )
            observer_keyring = _observer_keyring(directory / "observer-keyring.json")
            token_output_directory = directory / "connector-token"
            token_output_directory.mkdir(mode=0o700)
            base = _preflight_environment(directory)
            cases = {
                "--sqlite-business": {
                    "HERMES_RUNTIME_DSN_FILE": runtime_dsn,
                    "HERMES_SIGNING_SECRET_FILE": business_signing,
                    "HERMES_CONNECTOR_SIGNING_SECRET_FILE": connector_signing,
                    "HERMES_OBSERVER_KEYRING_FILE": observer_keyring,
                },
                "--sqlite-connector": {
                    "HERMES_RUNTIME_DSN_FILE": runtime_dsn,
                    "HERMES_CONNECTOR_SIGNING_SECRET_FILE": connector_signing,
                    "HERMES_OBSERVER_KEYRING_FILE": observer_keyring,
                },
                "--sqlite-migration": {
                    "HERMES_MIGRATION_DSN_FILE": migration_dsn,
                    "HERMES_OBSERVER_KEYRING_FILE": observer_keyring,
                },
                "--sqlite-seed": {
                    "HERMES_BOOTSTRAP_DSN_FILE": bootstrap_dsn,
                    "HERMES_INITIAL_USER_PASSWORD_FILE": initial_password,
                },
                "--sqlite-seed-cleanup": {
                    "HERMES_BOOTSTRAP_DSN_FILE": bootstrap_dsn,
                    "HERMES_SEED_TENANT_SLUG": "android-test",
                    "HERMES_SEED_TENANT_DISPLAY_NAME": "Android Test",
                    "HERMES_SEED_USERNAME": "android-user",
                    "HERMES_SEED_USER_DISPLAY_NAME": "Android User",
                    "HERMES_SEED_WORKSPACE_KEY": "android",
                    "HERMES_SEED_WORKSPACE_DISPLAY_NAME": "Android",
                    "HERMES_SEED_AGENT_KEY": "android-agent",
                },
                "--sqlite-connector-token": {
                    "HERMES_RUNTIME_DSN_FILE": runtime_dsn,
                    "HERMES_CONNECTOR_SIGNING_SECRET_FILE": connector_signing,
                    "HERMES_CONNECTOR_TOKEN_TENANT_ID": (
                        "33333333-3333-4333-8333-333333333333"
                    ),
                    "HERMES_CONNECTOR_TOKEN_DEVICE_ID": (
                        "77777777-7777-4777-8777-777777777777"
                    ),
                    "HERMES_CONNECTOR_TOKEN_TTL_SECONDS": "300",
                    "HERMES_CONNECTOR_TOKEN_OUTPUT": str(
                        token_output_directory / "connector.token"
                    ),
                    "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
                    "HERMES_SEED_TENANT_SLUG": "android-test",
                    "HERMES_SEED_AGENT_KEY": "android-agent",
                    "HERMES_SEED_DEVICE_KEY": "android-device",
                },
            }

            for mode, values in cases.items():
                with self.subTest(mode=mode):
                    completed = subprocess.run(
                        ("bash", str(PREFLIGHT), mode),
                        cwd=CLOUD_ROOT,
                        env={**base, **values},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        completed.returncode,
                        0,
                        completed.stderr,
                    )
                    self.assertEqual(
                        completed.stdout.strip(),
                        f"preflight=PASS mode={mode[2:]}",
                    )

    def test_observer_modes_fail_closed_for_missing_or_invalid_keyring(self) -> None:
        from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
        from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database = directory / "hermes-cloud.db"
            database_url = f"sqlite+pysqlite:///{database}"
            engine = build_sqlite_engine(database_url, allow_missing=True)
            try:
                build_sqlite_metadata().create_all(engine)
            finally:
                engine.dispose()
            database.chmod(0o660)
            runtime_dsn = _write_private(directory / "runtime-dsn", database_url)
            migration_dsn = _write_private(directory / "migration-dsn", database_url)
            business_signing = _write_private(
                directory / "business-signing",
                "business-signing-key-material-at-least-32-bytes",
            )
            connector_signing = _write_private(
                directory / "connector-signing",
                "connector-signing-key-material-at-least-32-bytes",
            )
            invalid = _write_private(
                directory / "invalid-observer-keyring.json",
                '{"version":1,"tenants":{}}',
            )
            wide = _observer_keyring(directory / "wide-observer-keyring.json")
            Path(wide).chmod(0o640)
            valid = _observer_keyring(directory / "valid-observer-keyring.json")
            symlink = directory / "observer-keyring-link.json"
            symlink.symlink_to(valid)
            base = _preflight_environment(directory)
            service_inputs = {
                "--sqlite-business": {
                    "HERMES_RUNTIME_DSN_FILE": runtime_dsn,
                    "HERMES_SIGNING_SECRET_FILE": business_signing,
                    "HERMES_CONNECTOR_SIGNING_SECRET_FILE": connector_signing,
                },
                "--sqlite-connector": {
                    "HERMES_RUNTIME_DSN_FILE": runtime_dsn,
                    "HERMES_CONNECTOR_SIGNING_SECRET_FILE": connector_signing,
                },
                "--sqlite-migration": {
                    "HERMES_MIGRATION_DSN_FILE": migration_dsn,
                },
            }

            for mode, values in service_inputs.items():
                for keyring in (None, invalid, wide, str(symlink)):
                    with self.subTest(mode=mode, keyring=keyring):
                        environment = {**base, **values}
                        if keyring is not None:
                            environment["HERMES_OBSERVER_KEYRING_FILE"] = keyring
                        completed = subprocess.run(
                            ("bash", str(PREFLIGHT), mode),
                            cwd=CLOUD_ROOT,
                            env=environment,
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertNotEqual(completed.returncode, 0)
                        self.assertNotIn("test-v1", completed.stdout)
                        self.assertNotIn("test-v1", completed.stderr)

    def test_sqlite_business_preflight_requires_connector_signing_secret(
        self,
    ) -> None:
        from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
        from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database = directory / "hermes-cloud.db"
            database_url = f"sqlite+pysqlite:///{database}"
            engine = build_sqlite_engine(database_url, allow_missing=True)
            try:
                build_sqlite_metadata().create_all(engine)
            finally:
                engine.dispose()
            database.chmod(0o660)
            runtime_dsn = _write_private(directory / "runtime-dsn", database_url)
            business_signing = _write_private(
                directory / "business-signing",
                "business-signing-key-material-at-least-32-bytes",
            )
            empty = _write_private(directory / "connector-empty", "")
            short = _write_private(directory / "connector-short", "too-short")
            wide = _write_private(
                directory / "connector-wide",
                "connector-signing-key-material-at-least-32-bytes",
            )
            Path(wide).chmod(0o644)
            valid = _write_private(
                directory / "connector-valid",
                "connector-signing-key-material-at-least-32-bytes",
            )
            symlink = directory / "connector-symlink"
            symlink.symlink_to(valid)
            base = _preflight_environment(directory)

            cases = (
                {},
                {"HERMES_CONNECTOR_SIGNING_SECRET_FILE": empty},
                {"HERMES_CONNECTOR_SIGNING_SECRET_FILE": short},
                {"HERMES_CONNECTOR_SIGNING_SECRET_FILE": wide},
                {"HERMES_CONNECTOR_SIGNING_SECRET_FILE": str(symlink)},
            )
            for values in cases:
                with self.subTest(values=tuple(sorted(values))):
                    completed = subprocess.run(
                        ("bash", str(PREFLIGHT), "--sqlite-business"),
                        cwd=CLOUD_ROOT,
                        env={
                            **base,
                            "HERMES_RUNTIME_DSN_FILE": runtime_dsn,
                            "HERMES_SIGNING_SECRET_FILE": business_signing,
                            **values,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                    self.assertNotEqual(completed.returncode, 0)
                    self.assertNotIn(
                        "business-signing-key-material",
                        completed.stdout,
                    )
                    self.assertNotIn(
                        "business-signing-key-material",
                        completed.stderr,
                    )
                    self.assertNotIn(
                        "connector-signing-key-material",
                        completed.stdout,
                    )
                    self.assertNotIn(
                        "connector-signing-key-material",
                        completed.stderr,
                    )

    def test_sqlite_connector_preflight_rejects_missing_or_invalid_runtime_dsn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            connector_signing = _write_private(
                directory / "connector-signing",
                "connector-signing-key-material-at-least-32-bytes",
            )
            invalid_runtime_dsn = _write_private(
                directory / "invalid-runtime-dsn",
                "postgresql://runtime-provider-is-not-sqlite",
            )
            base = _preflight_environment(directory)
            cases = (
                {
                    "HERMES_CONNECTOR_SIGNING_SECRET_FILE": connector_signing,
                },
                {
                    "HERMES_CONNECTOR_SIGNING_SECRET_FILE": connector_signing,
                    "HERMES_RUNTIME_DSN_FILE": invalid_runtime_dsn,
                },
            )

            for values in cases:
                with self.subTest(values=tuple(sorted(values))):
                    completed = subprocess.run(
                        ("bash", str(PREFLIGHT), "--sqlite-connector"),
                        cwd=CLOUD_ROOT,
                        env={**base, **values},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(completed.returncode, 0)

    def test_sqlite_connector_token_preflight_requires_private_runtime_dsn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            connector_signing = _write_private(
                directory / "connector-signing",
                "connector-signing-key-material-at-least-32-bytes",
            )
            output_directory = directory / "token"
            output_directory.mkdir(mode=0o700)
            base_environment = _preflight_environment(directory)
            completed = subprocess.run(
                ("bash", str(PREFLIGHT), "--sqlite-connector-token"),
                cwd=CLOUD_ROOT,
                env={
                    **base_environment,
                    "HERMES_CONNECTOR_SIGNING_SECRET_FILE": connector_signing,
                    "HERMES_CONNECTOR_TOKEN_TENANT_ID": (
                        "33333333-3333-4333-8333-333333333333"
                    ),
                    "HERMES_CONNECTOR_TOKEN_DEVICE_ID": (
                        "77777777-7777-4777-8777-777777777777"
                    ),
                    "HERMES_CONNECTOR_TOKEN_TTL_SECONDS": "300",
                    "HERMES_CONNECTOR_TOKEN_OUTPUT": str(
                        output_directory / "connector.token"
                    ),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertNotIn("connector-signing-key-material", completed.stdout)
            self.assertNotIn("connector-signing-key-material", completed.stderr)

            runtime_dsn = _write_private(
                directory / "runtime-dsn",
                "sqlite+pysqlite:////private/runtime.sqlite3",
            )
            missing_database = subprocess.run(
                ("bash", str(PREFLIGHT), "--sqlite-connector-token"),
                cwd=CLOUD_ROOT,
                env={
                    **base_environment,
                    "HERMES_RUNTIME_DSN_FILE": runtime_dsn,
                    "HERMES_CONNECTOR_SIGNING_SECRET_FILE": connector_signing,
                    "HERMES_CONNECTOR_TOKEN_TENANT_ID": (
                        "33333333-3333-4333-8333-333333333333"
                    ),
                    "HERMES_CONNECTOR_TOKEN_DEVICE_ID": (
                        "77777777-7777-4777-8777-777777777777"
                    ),
                    "HERMES_CONNECTOR_TOKEN_TTL_SECONDS": "300",
                    "HERMES_CONNECTOR_TOKEN_OUTPUT": str(
                        output_directory / "connector.token"
                    ),
                    "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
                    "HERMES_SEED_TENANT_SLUG": "android-test",
                    "HERMES_SEED_AGENT_KEY": "android-agent",
                    "HERMES_SEED_DEVICE_KEY": "android-device",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(missing_database.returncode, 0)
            self.assertNotIn(
                "connector-signing-key-material",
                missing_database.stdout + missing_database.stderr,
            )

    def test_sqlite_connector_token_preflight_resolves_active_binding(self) -> None:
        for state in ("valid", "revoked", "expired", "ambiguous", "mismatched"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                connector_signing = _write_private(
                    directory / "connector-signing",
                    "connector-signing-key-material-at-least-32-bytes",
                )
                runtime_dsn, engine = _active_owner_control_database(directory)
                output_directory = directory / "token"
                output_directory.mkdir(mode=0o700)
                try:
                    if state in {"revoked", "expired"}:
                        with Session(engine) as session, session.begin():
                            credential = session.get(
                                DeviceCredentialModel,
                                (TENANT_ID, CREDENTIAL_ID),
                            )
                            self.assertIsNotNone(credential)
                            if state == "revoked":
                                credential.status = "revoked"
                                credential.revoked_at = NOW
                            else:
                                credential.expires_at = NOW - timedelta(seconds=1)
                    elif state == "ambiguous":
                        with Session(engine) as session, session.begin():
                            session.add(
                                DeviceCredentialModel(
                                    tenant_id=TENANT_ID,
                                    credential_id=UUID(
                                        "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
                                    ),
                                    device_id=DEVICE_ID,
                                    credential_type="public_key",
                                    key_id="e" * 64,
                                    credential_fingerprint="e" * 64,
                                    status="active",
                                    issued_at=NOW,
                                    expires_at=NOW + timedelta(days=1),
                                    revoked_at=None,
                                )
                            )

                    environment = {
                        **_preflight_environment(directory),
                        "HERMES_RUNTIME_DSN_FILE": runtime_dsn,
                        "HERMES_CONNECTOR_SIGNING_SECRET_FILE": connector_signing,
                        "HERMES_CONNECTOR_TOKEN_TENANT_ID": str(TENANT_ID),
                        "HERMES_CONNECTOR_TOKEN_DEVICE_ID": (
                            "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
                            if state == "mismatched"
                            else str(DEVICE_ID)
                        ),
                        "HERMES_CONNECTOR_TOKEN_TTL_SECONDS": "300",
                        "HERMES_CONNECTOR_TOKEN_OUTPUT": str(
                            output_directory / "connector.token"
                        ),
                        "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
                        "HERMES_SEED_TENANT_SLUG": "android-test",
                        "HERMES_SEED_AGENT_KEY": "android-agent",
                        "HERMES_SEED_DEVICE_KEY": "android-device",
                    }
                    completed = subprocess.run(
                        ("bash", str(PREFLIGHT), "--sqlite-connector-token"),
                        cwd=CLOUD_ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                    if state == "valid":
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                    else:
                        self.assertNotEqual(completed.returncode, 0)
                    self.assertFalse((output_directory / "connector.token").exists())
                    self.assertNotIn(
                        "connector-signing-key-material",
                        completed.stdout + completed.stderr,
                    )
                finally:
                    engine.dispose()

    def test_sqlite_nginx_include_exposes_only_p0_routes(self) -> None:
        nginx = NGINX.read_text()

        locations = {
            match.group("route"): match.group("body")
            for match in re.finditer(
                r"(?ms)^location (?P<route>(?:= )?/hermes/\S+) \{\n"
                r"(?P<body>.*?)^\}",
                nginx,
            )
        }
        expected_upstreams = {
            "/hermes/api/": "http://127.0.0.1:8101/api/",
            "/hermes/auth/": "http://127.0.0.1:8101/auth/",
            "= /hermes/api/ws": "http://127.0.0.1:8101/api/ws",
            "= /hermes/internal/connector/ws": "http://127.0.0.1:8102/api/ws",
            "= /hermes/live": "http://127.0.0.1:8101/live",
            "= /hermes/ready": "http://127.0.0.1:8101/ready",
        }

        self.assertNotRegex(nginx, r"(?m)^\s*(?:http|events|server)\s*\{")
        self.assertNotRegex(nginx, r"(?m)^\s*listen\s+")
        self.assertEqual(set(locations), set(expected_upstreams))
        for route, upstream in expected_upstreams.items():
            with self.subTest(route=route):
                body = locations[route]
                self.assertIn("access_log off;", body)
                self.assertIn("proxy_set_header X-Forwarded-Prefix /hermes;", body)
                self.assertIn(f"proxy_pass {upstream};", body)
                if route not in {"= /hermes/live", "= /hermes/ready"}:
                    self.assertIn("proxy_set_header Host $host;", body)
                    self.assertIn(
                        "proxy_set_header X-Forwarded-Proto $scheme;",
                        body,
                    )
                else:
                    self.assertNotIn("proxy_set_header Host", body)
                    self.assertNotIn("proxy_set_header X-Forwarded-Proto", body)
        self.assertNotIn("/hermes/files/", nginx)
        self.assertNotRegex(nginx, r"location\s+/hermes/\s*\{")
        self.assertEqual(
            nginx.count('proxy_set_header Upgrade "$http_upgrade";'),
            2,
        )
        self.assertEqual(
            nginx.count('proxy_set_header Connection "upgrade";'),
            2,
        )
        self.assertGreaterEqual(nginx.count("access_log off;"), 6)
        self.assertIn("client_max_body_size", nginx)
        self.assertIn("proxy_connect_timeout", nginx)
        self.assertIn("proxy_read_timeout", nginx)
        self.assertIn("proxy_set_header X-Forwarded-Prefix /hermes;", nginx)

    def test_optional_connector_token_unit_is_owner_control_safe_and_explicit(
        self,
    ) -> None:
        unit = TOKEN_UNIT.read_text()
        environment = ENVIRONMENT.read_text()

        self.assertNotIn("LoadCredential", unit)
        self.assertIn("User=hermes-cloud", unit)
        self.assertIn("Group=hermes-cloud", unit)
        self.assertIn("UMask=0077", unit)
        self.assertIn(
            "Environment=HERMES_CONNECTOR_SIGNING_SECRET_FILE="
            "/etc/hermes-cloud/sqlite/secrets/connector_signing_secret",
            unit,
        )
        self.assertIn(
            "Environment=HERMES_RUNTIME_DSN_FILE="
            "/etc/hermes-cloud/sqlite/secrets/runtime_database_dsn",
            unit,
        )
        self.assertIn("--sqlite-connector-token", unit)
        self.assertIn("mint_connector_token.py --apply --output", unit)
        self.assertIn("StateDirectory=hermes-cloud-connector-token", unit)
        self.assertIn("StateDirectoryMode=0700", unit)
        self.assertNotIn("WantedBy=", unit)
        self.assertNotIn("business-api.service", unit)
        self.assertNotIn("connector-gateway.service", unit)
        self.assertIsNone(SECRET_ASSIGNMENT.search(unit))

        self.assertIn("HERMES_CONNECTOR_TOKEN_TENANT_ID=", environment)
        self.assertIn("HERMES_CONNECTOR_TOKEN_DEVICE_ID=", environment)
        self.assertIn("HERMES_CONNECTOR_TOKEN_TTL_SECONDS=", environment)
        self.assertIn(
            "HERMES_SEED_OWNER_CONTROL_ENABLED=true",
            environment,
        )
        self.assertIn("HERMES_SEED_AGENT_KEY=android-agent", environment)
        self.assertIn("HERMES_SEED_DEVICE_KEY=android-device", environment)
        self.assertIn(
            "HERMES_CONNECTOR_TOKEN_TENANT_ID=a495873f-cc49-5e21-b9fd-a581e3159ec8",
            environment,
        )
        self.assertIn(
            "HERMES_CONNECTOR_TOKEN_DEVICE_ID=0059b49e-fb3e-5da1-9a7c-d5a1537b2210",
            environment,
        )
        self.assertIn(
            "HERMES_CONNECTOR_TOKEN_OUTPUT="
            "/var/lib/hermes-cloud-connector-token/connector.token",
            environment,
        )
        readme = README.read_text()
        self.assertIn("current V1 device-credential claim set", readme)
        self.assertIn("private runtime database reference", readme)
        self.assertIn("Direct CLI legacy mode still requires canonical UUID", readme)
        self.assertIn("`--inspect-binding`", readme)
        self.assertIn("custom seed", readme)
        self.assertIn("Do not copy the example UUIDs", readme)
        self.assertIn("prints only non-secret UUIDs and scopes", readme)
        self.assertIn("never prints the DSN, signing secret, or token", readme)
        self.assertIn("update `HERMES_CONNECTOR_TOKEN_TENANT_ID`", readme)
        self.assertIn("run the default dry-run", readme)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import re
import runpy
import select
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Self
from unittest import mock

from test_mint_connector_token import _active_owner_control_database

ROOT = Path(__file__).parents[1]
CLOUD_ROOT = ROOT.parents[1]
SYSTEMD = ROOT / "systemd"
NGINX = ROOT / "nginx" / "hermes-test-server.conf"
ENVIRONMENT = ROOT / "env" / "test-server.env.example"
README = ROOT / "README.md"
SCRIPTS = ROOT / "scripts"
PREFLIGHT = SCRIPTS / "preflight.sh"

SERVICE_UNITS = {
    "business-api": "hermes_cloud.entrypoints.business_api.bootstrap:app",
    "connector-gateway": ("hermes_cloud.entrypoints.connector_gateway.bootstrap:app"),
    "worker": "hermes_cloud.entrypoints.worker",
    "file-gateway": "hermes_cloud.entrypoints.file_gateway.bootstrap:app",
}

SECRET_VALUE_PATTERNS = (
    re.compile(r"postgres(?:ql)?://", re.IGNORECASE),
    re.compile(r"(?im)^\s*(?:password|token|ticket|dsn)\s*=\s*[^/$%\s]"),
    re.compile(r"(?i)bearer\s+[a-z0-9._-]+"),
)


def _preflight_environment(
    directory: Path,
    *,
    include_uvicorn: bool,
) -> dict[str, str]:
    virtual_environment = directory / "venv"
    binaries = virtual_environment / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    python = binaries / "python"
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o700)
    if include_uvicorn:
        uvicorn = binaries / "uvicorn"
        uvicorn.write_text("#!/bin/sh\nexit 0\n")
        uvicorn.chmod(0o700)
    python_path = os.pathsep.join(
        filter(
            None,
            (
                str(CLOUD_ROOT / "src"),
                os.environ.get("PYTHONPATH"),
            ),
        )
    )
    return {
        **os.environ,
        "HERMES_CURRENT": str(CLOUD_ROOT),
        "HERMES_RELEASES_DIR": str(CLOUD_ROOT.parent),
        "HERMES_VENV": str(virtual_environment),
        "PYTHONPATH": python_path,
    }


def _run_preflight(
    mode: str,
    environment: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("bash", str(PREFLIGHT), mode),
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_secret(path: Path, value: str, *, mode: int = 0o600) -> None:
    path.write_text(value)
    path.chmod(mode)


class _TrackingMigrationConnection:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.closed = True


class _TrackingMigrationEngine:
    def __init__(self, connection: _TrackingMigrationConnection) -> None:
        self.connection = connection
        self.disposed = False

    def connect(self) -> _TrackingMigrationConnection:
        return self.connection

    def dispose(self) -> None:
        self.disposed = True


class DeployArtifactTest(unittest.TestCase):
    def test_run_asgi_uses_release_python_module_not_console_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            current = directory / "current"
            virtualenv = directory / "venv"
            python = virtualenv / "bin/python"
            uvicorn = virtualenv / "bin/uvicorn"
            capture = directory / "arguments"
            current.mkdir()
            python.parent.mkdir(parents=True)
            python.write_text(
                '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$HERMES_CAPTURE_FILE"\n'
            )
            python.chmod(0o700)
            uvicorn.write_text("#!/usr/bin/env bash\nexit 91\n")
            uvicorn.chmod(0o700)
            environment = {
                **os.environ,
                "HERMES_CURRENT": str(current),
                "HERMES_VENV": str(virtualenv),
                "HERMES_CAPTURE_FILE": str(capture),
            }

            result = subprocess.run(
                (
                    "bash",
                    str(SCRIPTS / "run_asgi.sh"),
                    "hermes_cloud.entrypoints.business_api.bootstrap:app",
                    "127.0.0.1",
                    "8101",
                ),
                check=False,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = capture.read_text().splitlines()
            self.assertEqual(
                arguments[:3],
                [
                    "-m",
                    "uvicorn",
                    "hermes_cloud.entrypoints.business_api.bootstrap:app",
                ],
            )

    def test_run_asgi_requires_application_lifespan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            current = directory / "current"
            virtualenv = directory / "venv"
            executable = virtualenv / "bin/python"
            capture = directory / "arguments"
            current.mkdir()
            executable.parent.mkdir(parents=True)
            executable.write_text(
                '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$HERMES_CAPTURE_FILE"\n'
            )
            executable.chmod(0o700)
            environment = {
                **os.environ,
                "HERMES_CURRENT": str(current),
                "HERMES_VENV": str(virtualenv),
                "HERMES_CAPTURE_FILE": str(capture),
            }

            result = subprocess.run(
                (
                    "bash",
                    str(SCRIPTS / "run_asgi.sh"),
                    "hermes_cloud.entrypoints.connector_gateway.bootstrap:app",
                    "127.0.0.1",
                    "8102",
                ),
                check=False,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = capture.read_text().splitlines()
            lifespan_index = arguments.index("--lifespan")
            self.assertEqual(arguments[lifespan_index + 1], "on")
            self.assertIn("--proxy-headers", arguments)
            forwarded_index = arguments.index("--forwarded-allow-ips")
            self.assertEqual(arguments[forwarded_index + 1], "127.0.0.1,::1")
            log_level_index = arguments.index("--log-level")
            self.assertEqual(arguments[log_level_index + 1], "warning")
            self.assertIn("--no-access-log", arguments)

    def test_run_asgi_suppresses_info_websocket_ticket_but_keeps_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            current = directory / "current"
            virtualenv = directory / "venv"
            executable = virtualenv / "bin/python"
            current.mkdir()
            executable.parent.mkdir(parents=True)
            executable.write_text(
                "#!/usr/bin/env bash\n"
                "level=info\n"
                "while [[ $# -gt 0 ]]; do\n"
                "  if [[ $1 == --log-level ]]; then level=$2; shift 2; else shift; fi\n"
                "done\n"
                "if [[ $level == warning ]]; then\n"
                "  printf '%s\\n' 'WARNING retained-runtime-warning' >&2\n"
                "else\n"
                "  printf '%s\\n' 'INFO WebSocket /api/ws?ticket=single-use-secret' >&2\n"
                "fi\n"
            )
            executable.chmod(0o700)
            environment = {
                **os.environ,
                "HERMES_CURRENT": str(current),
                "HERMES_VENV": str(virtualenv),
            }

            result = subprocess.run(
                (
                    "bash",
                    str(SCRIPTS / "run_asgi.sh"),
                    "hermes_cloud.entrypoints.business_api.bootstrap:app",
                    "127.0.0.1",
                    "8101",
                ),
                check=False,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("retained-runtime-warning", result.stderr)
            self.assertNotIn("ticket", result.stderr.casefold())
            self.assertNotIn("single-use-secret", result.stderr)

    def test_migration_runner_binds_database_name_from_the_dsn(self) -> None:
        namespace = runpy.run_path(
            str(SCRIPTS / "run_migrations.py"),
            run_name="hermes_cloud_migration_test",
        )
        main = namespace["main"]
        connection = _TrackingMigrationConnection()
        engine = _TrackingMigrationEngine(connection)
        engine_factory = mock.Mock(return_value=engine)
        captured: dict[str, object] = {}

        class CapturingRunner:
            def apply_all(self, session, *, identifiers, deadline):
                captured["session"] = session
                captured["identifiers"] = identifiers
                captured["deadline"] = deadline
                return ()

        with tempfile.TemporaryDirectory() as temporary:
            dsn_file = Path(temporary) / "migration-dsn"
            _write_secret(
                dsn_file,
                "postgresql://migrate:secret@127.0.0.1/hermes_cloud",
            )
            environment = {
                "HERMES_MIGRATION_DSN_FILE": str(dsn_file),
                "HERMES_MIGRATION_ROLE": "hermes_cloud_migrate",
                "HERMES_RUNTIME_ROLE": "hermes_cloud_runtime",
            }
            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.dict(
                    main.__globals__,
                    {
                        "create_engine": engine_factory,
                        "PostgresMigrationRunner": CapturingRunner,
                    },
                ),
            ):
                main()

        self.assertTrue(connection.closed)
        self.assertTrue(engine.disposed)
        engine_url = engine_factory.call_args.args[0]
        self.assertEqual(engine_url.drivername, "postgresql+psycopg")
        self.assertEqual(engine_url.database, "hermes_cloud")
        self.assertEqual(engine_url.username, "migrate")
        self.assertEqual(engine_url.password, "secret")
        self.assertEqual(
            captured["identifiers"],
            {
                "database_name": "hermes_cloud",
                "migration_role": "hermes_cloud_migrate",
                "runtime_role": "hermes_cloud_runtime",
            },
        )

    def test_migration_main_preserves_explicit_psycopg_and_redacts_engine_failure(
        self,
    ) -> None:
        namespace = runpy.run_path(
            str(SCRIPTS / "run_migrations.py"),
            run_name="hermes_cloud_migration_driver_normalization_test",
        )
        main = namespace["main"]
        sentinel = "migration-secret-must-not-appear"

        class FailingEngineFactory:
            def __init__(self) -> None:
                self.database_url = None
                self.kwargs: dict[str, object] = {}

            def __call__(self, database_url, **kwargs):
                self.database_url = database_url
                self.kwargs = kwargs
                raise RuntimeError("migration engine initialization failed")

        for driver in ("postgresql", "postgresql+psycopg"):
            with (
                self.subTest(driver=driver),
                tempfile.TemporaryDirectory() as temporary,
            ):
                dsn_file = Path(temporary) / "migration-dsn"
                _write_secret(
                    dsn_file,
                    f"{driver}://migrate:{sentinel}@127.0.0.1/hermes_cloud",
                )
                engine_factory = FailingEngineFactory()

                with (
                    mock.patch.dict(
                        os.environ,
                        {"HERMES_MIGRATION_DSN_FILE": str(dsn_file)},
                        clear=True,
                    ),
                    mock.patch.dict(
                        main.__globals__,
                        {"create_engine": engine_factory},
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "^migration engine initialization failed$",
                    ) as raised,
                ):
                    main()

                engine_url = engine_factory.database_url
                self.assertIsNotNone(engine_url)
                self.assertEqual(engine_url.drivername, "postgresql+psycopg")
                self.assertEqual(engine_url.database, "hermes_cloud")
                self.assertEqual(engine_url.password, sentinel)
                self.assertNotIn(sentinel, str(raised.exception))

    def test_migration_main_closes_resources_when_runner_fails(self) -> None:
        namespace = runpy.run_path(
            str(SCRIPTS / "run_migrations.py"),
            run_name="hermes_cloud_migration_failure_test",
        )
        main = namespace["main"]
        connection = _TrackingMigrationConnection()
        engine = _TrackingMigrationEngine(connection)

        class FailingRunner:
            def apply_all(self, session, *, identifiers, deadline):
                raise RuntimeError("synthetic migration failure")

        with tempfile.TemporaryDirectory() as temporary:
            dsn_file = Path(temporary) / "migration-dsn"
            _write_secret(
                dsn_file,
                "postgresql+psycopg://migrate:secret@127.0.0.1/hermes_cloud",
            )
            environment = {"HERMES_MIGRATION_DSN_FILE": str(dsn_file)}
            with (
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.dict(
                    main.__globals__,
                    {
                        "create_engine": mock.Mock(return_value=engine),
                        "PostgresMigrationRunner": FailingRunner,
                    },
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "^synthetic migration failure$",
                ),
            ):
                main()

        self.assertTrue(connection.closed)
        self.assertTrue(engine.disposed)

    def test_migration_identifiers_accept_supported_postgresql_drivers(
        self,
    ) -> None:
        namespace = runpy.run_path(
            str(SCRIPTS / "run_migrations.py"),
            run_name="hermes_cloud_migration_driver_test",
        )

        for driver in ("postgresql", "postgresql+psycopg"):
            with self.subTest(driver=driver):
                identifiers = namespace["_migration_identifiers"](
                    f"{driver}://migrate:secret@127.0.0.1/hermes_cloud",
                    {},
                )
                self.assertEqual(identifiers["database_name"], "hermes_cloud")

    def test_migration_identifiers_reject_unknown_schemes_without_disclosure(
        self,
    ) -> None:
        namespace = runpy.run_path(
            str(SCRIPTS / "run_migrations.py"),
            run_name="hermes_cloud_migration_unknown_driver_test",
        )
        sentinel = "migration-secret-must-not-appear"

        for database_url in (
            f"mysql://migrate:{sentinel}@127.0.0.1/hermes_cloud",
            f"postgresql+unknown://migrate:{sentinel}@127.0.0.1/hermes_cloud",
        ):
            with self.subTest(database_url=database_url.split(":", 1)[0]):
                with self.assertRaisesRegex(
                    SystemExit,
                    "^migration database identity is invalid$",
                ) as raised:
                    namespace["_migration_identifiers"](database_url, {})
                self.assertNotIn(sentinel, str(raised.exception))

    def test_migration_identifiers_reject_missing_database_and_bad_dsn_safely(
        self,
    ) -> None:
        namespace = runpy.run_path(
            str(SCRIPTS / "run_migrations.py"),
            run_name="hermes_cloud_migration_invalid_dsn_test",
        )
        sentinel = "migration-secret-must-not-appear"

        for database_url in (
            f"postgresql+psycopg://migrate:{sentinel}@127.0.0.1/",
            f"not a url {sentinel}",
        ):
            with self.subTest(database_url=database_url.split(":", 1)[0]):
                with self.assertRaisesRegex(
                    SystemExit,
                    "^migration database identity is invalid$",
                ) as raised:
                    namespace["_migration_identifiers"](database_url, {})
                self.assertNotIn(sentinel, str(raised.exception))

    def test_four_runtime_units_are_non_root_hardened_and_package_specific(
        self,
    ) -> None:
        worker_helper = (SCRIPTS / "run_worker.py").read_text()
        for service, package_path in SERVICE_UNITS.items():
            with self.subTest(service=service):
                unit = (SYSTEMD / f"hermes-cloud-{service}.service").read_text()
                self.assertIn("User=hermes-cloud", unit)
                self.assertNotIn("User=root", unit)
                self.assertIn("EnvironmentFile=", unit)
                self.assertIn("LoadCredential=", unit)
                self.assertIn("NoNewPrivileges=true", unit)
                self.assertIn("PrivateTmp=true", unit)
                self.assertIn("ProtectSystem=strict", unit)
                self.assertIn("Restart=on-failure", unit)
                self.assertIn("TimeoutStartSec=", unit)
                self.assertIn("LimitNOFILE=", unit)
                self.assertNotRegex(unit, r"(?i)ExecStart(?:Pre)?=.*migrat")
                self.assertTrue(
                    package_path in unit or package_path in worker_helper,
                    package_path,
                )

    def test_migration_is_explicit_oneshot_and_not_a_runtime_dependency(self) -> None:
        migration = (SYSTEMD / "hermes-cloud-migrate.service").read_text()
        self.assertIn("Type=oneshot", migration)
        self.assertIn("User=hermes-cloud-migrate", migration)
        self.assertIn("preflight.sh --migration", migration)
        self.assertIn("run_migrations.py", migration)
        self.assertNotIn("WantedBy=", migration)
        for service in SERVICE_UNITS:
            unit = (SYSTEMD / f"hermes-cloud-{service}.service").read_text()
            self.assertNotIn("hermes-cloud-migrate.service", unit)

    def test_seed_is_explicit_apply_oneshot_and_not_a_runtime_dependency(
        self,
    ) -> None:
        seed = (SYSTEMD / "hermes-cloud-seed-test-data.service").read_text()
        self.assertIn("Type=oneshot", seed)
        self.assertIn("User=hermes-cloud-migrate", seed)
        self.assertIn("preflight.sh --seed", seed)
        self.assertIn("seed_test_data.py --apply", seed)
        self.assertIn("LoadCredential=bootstrap_dsn:", seed)
        self.assertIn("LoadCredential=initial_user_password:", seed)
        self.assertNotIn("WantedBy=", seed)
        for service in SERVICE_UNITS:
            unit = (SYSTEMD / f"hermes-cloud-{service}.service").read_text()
            self.assertNotIn("hermes-cloud-seed-test-data.service", unit)

    def test_seed_nonsecrets_are_environment_only(self) -> None:
        environment = ENVIRONMENT.read_text()
        credentials = (ROOT / "env" / "credential-files.example").read_text()
        for name in (
            "HERMES_SEED_TENANT_SLUG",
            "HERMES_SEED_TENANT_DISPLAY_NAME",
            "HERMES_SEED_USERNAME",
            "HERMES_SEED_USER_DISPLAY_NAME",
            "HERMES_SEED_WORKSPACE_KEY",
            "HERMES_SEED_WORKSPACE_DISPLAY_NAME",
        ):
            self.assertIn(f"{name}=", environment)
        self.assertNotIn("PASSWORD=", environment)
        self.assertIn("bootstrap_database_dsn_file=", credentials)
        self.assertIn("initial_user_password_file=", credentials)

    def test_connector_gateway_uses_signing_credential_and_bounded_preflight(
        self,
    ) -> None:
        unit = (SYSTEMD / "hermes-cloud-connector-gateway.service").read_text()
        self.assertIn("LoadCredential=connector_signing_secret:", unit)
        self.assertIn(
            "Environment=HERMES_CONNECTOR_SIGNING_SECRET_FILE="
            "%d/connector_signing_secret",
            unit,
        )
        self.assertIn("preflight.sh --connector", unit)
        self.assertNotIn("HERMES_RUNTIME_DSN_FILE", unit)

    def test_connector_token_mint_is_optional_explicit_apply_oneshot(
        self,
    ) -> None:
        unit = (SYSTEMD / "hermes-cloud-mint-connector-token.service").read_text()
        self.assertIn("Type=oneshot", unit)
        self.assertIn("preflight.sh --connector-token", unit)
        self.assertIn(
            "mint_connector_token.py --apply --output ${HERMES_CONNECTOR_TOKEN_OUTPUT}",
            unit,
        )
        self.assertIn("LoadCredential=connector_signing_secret:", unit)
        self.assertIn("LoadCredential=runtime_dsn:", unit)
        self.assertIn(
            "Environment=HERMES_RUNTIME_DSN_FILE=%d/runtime_dsn",
            unit,
        )
        self.assertNotIn("WantedBy=", unit)
        for service in SERVICE_UNITS:
            runtime = (SYSTEMD / f"hermes-cloud-{service}.service").read_text()
            self.assertNotIn(
                "hermes-cloud-mint-connector-token.service",
                runtime,
            )

    def test_connector_token_nonsecrets_are_environment_only(self) -> None:
        environment = ENVIRONMENT.read_text()
        credentials = (ROOT / "env" / "credential-files.example").read_text()
        readme = README.read_text()
        for name in (
            "HERMES_CONNECTOR_TOKEN_TENANT_ID",
            "HERMES_CONNECTOR_TOKEN_DEVICE_ID",
            "HERMES_CONNECTOR_TOKEN_TTL_SECONDS",
            "HERMES_CONNECTOR_TOKEN_OUTPUT",
        ):
            self.assertIn(f"{name}=", environment)
        self.assertIn(
            "HERMES_CONNECTOR_TOKEN_TENANT_ID=33333333-3333-4333-8333-333333333333",
            environment,
        )
        self.assertIn(
            "HERMES_CONNECTOR_TOKEN_DEVICE_ID=77777777-7777-4777-8777-777777777777",
            environment,
        )
        self.assertIn("connector_signing_secret_file=", credentials)
        self.assertIn("`--inspect-binding`", readme)
        self.assertIn("custom seed", readme)
        self.assertIn("Do not copy the example UUIDs", readme)
        self.assertIn("prints only non-secret UUIDs and scopes", readme)
        self.assertIn("never prints the DSN, signing secret, or token", readme)
        self.assertIn("update `HERMES_CONNECTOR_TOKEN_TENANT_ID`", readme)
        self.assertIn("run the default dry-run", readme)

    def test_connector_preflights_are_bounded_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            signing = directory / "connector-signing"
            sentinel = "s" * 32
            _write_secret(signing, sentinel)

            connector_environment = _preflight_environment(
                directory,
                include_uvicorn=True,
            )
            connector_environment["HERMES_CONNECTOR_SIGNING_SECRET_FILE"] = str(signing)
            connector = _run_preflight(
                "--connector",
                connector_environment,
                directory,
            )
            self.assertEqual(connector.returncode, 0, connector.stderr)
            self.assertIn("preflight=PASS mode=connector", connector.stdout)
            self.assertNotIn(sentinel, connector.stdout + connector.stderr)

            token_environment = _preflight_environment(
                directory,
                include_uvicorn=False,
            )
            token_environment.pop("HERMES_RELEASES_DIR")
            runtime_dsn, engine = _active_owner_control_database(directory)
            output_directory = directory / "token"
            output_directory.mkdir(mode=0o700)
            token_environment["HERMES_CONNECTOR_SIGNING_SECRET_FILE"] = str(signing)
            token_environment["HERMES_RUNTIME_DSN_FILE"] = runtime_dsn
            token_environment["HERMES_CONNECTOR_TOKEN_TENANT_ID"] = (
                "33333333-3333-4333-8333-333333333333"
            )
            token_environment["HERMES_CONNECTOR_TOKEN_DEVICE_ID"] = (
                "77777777-7777-4777-8777-777777777777"
            )
            token_environment["HERMES_CONNECTOR_TOKEN_TTL_SECONDS"] = "300"
            token_environment["HERMES_CONNECTOR_TOKEN_OUTPUT"] = str(
                output_directory / "connector.token"
            )
            token_environment["HERMES_SEED_OWNER_CONTROL_ENABLED"] = "true"
            token_environment["HERMES_SEED_TENANT_SLUG"] = "android-test"
            token_environment["HERMES_SEED_AGENT_KEY"] = "android-agent"
            token_environment["HERMES_SEED_DEVICE_KEY"] = "android-device"
            try:
                token = _run_preflight(
                    "--connector-token",
                    token_environment,
                    directory,
                )
                self.assertEqual(token.returncode, 0, token.stderr)
                self.assertIn(
                    "preflight=PASS mode=connector-token",
                    token.stdout,
                )
                self.assertFalse((output_directory / "connector.token").exists())
                self.assertNotIn(sentinel, token.stdout + token.stderr)

                signing.chmod(0o640)
                rejected = _run_preflight(
                    "--connector-token",
                    token_environment,
                    directory,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertNotIn(sentinel, rejected.stdout + rejected.stderr)
            finally:
                engine.dispose()

    def test_connector_token_preflight_rejects_legacy_slugs_and_owner_without_dsn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            signing = directory / "connector-signing"
            sentinel = "s" * 32
            _write_secret(signing, sentinel)
            base = _preflight_environment(directory, include_uvicorn=False)
            base.pop("HERMES_RELEASES_DIR")
            base.update(
                {
                    "HERMES_CONNECTOR_SIGNING_SECRET_FILE": str(signing),
                    "HERMES_CONNECTOR_TOKEN_TTL_SECONDS": "300",
                }
            )
            cases = (
                {
                    "HERMES_CONNECTOR_TOKEN_TENANT_ID": (
                        "33333333-3333-4333-8333-333333333333"
                    ),
                    "HERMES_CONNECTOR_TOKEN_DEVICE_ID": (
                        "77777777-7777-4777-8777-777777777777"
                    ),
                },
                {
                    "HERMES_CONNECTOR_TOKEN_TENANT_ID": "android-test",
                    "HERMES_CONNECTOR_TOKEN_DEVICE_ID": "android-device",
                },
                {
                    "HERMES_CONNECTOR_TOKEN_TENANT_ID": (
                        "33333333-3333-4333-8333-333333333333"
                    ),
                    "HERMES_CONNECTOR_TOKEN_DEVICE_ID": (
                        "77777777-7777-4777-8777-777777777777"
                    ),
                    "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
                    "HERMES_SEED_TENANT_SLUG": "custom-tenant",
                    "HERMES_SEED_AGENT_KEY": "custom-agent",
                    "HERMES_SEED_DEVICE_KEY": "custom-device",
                },
            )

            for values in cases:
                with self.subTest(values=values):
                    result = _run_preflight(
                        "--connector-token",
                        {**base, **values},
                        directory,
                    )

                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(sentinel, result.stdout + result.stderr)

    def test_migration_preflight_does_not_require_runtime_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = _preflight_environment(
                directory,
                include_uvicorn=False,
            )
            environment.pop("HERMES_RELEASES_DIR")
            migration_dsn = directory / "migration-dsn"
            _write_secret(migration_dsn, "postgresql://migration-secret")
            environment["HERMES_MIGRATION_DSN_FILE"] = str(migration_dsn)

            result = _run_preflight("--migration", environment, directory)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("preflight=PASS mode=migration", result.stdout)

    def test_migration_preflight_does_not_import_runtime_entrypoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = _preflight_environment(
                directory,
                include_uvicorn=True,
            )
            migration_dsn = directory / "migration-dsn"
            _write_secret(migration_dsn, "postgresql://migration-secret")
            environment["HERMES_MIGRATION_DSN_FILE"] = str(migration_dsn)
            (directory / "sitecustomize.py").write_text(
                "import importlib.abc\n"
                "import sys\n"
                "\n"
                "class RuntimeEntrypointBlocker(importlib.abc.MetaPathFinder):\n"
                "    def find_spec(self, fullname, path, target=None):\n"
                '        if fullname.startswith("hermes_cloud.entrypoints"):\n'
                '            raise ModuleNotFoundError("runtime entrypoint blocked")\n'
                "        return None\n"
                "\n"
                "sys.meta_path.insert(0, RuntimeEntrypointBlocker())\n"
            )
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(directory), environment["PYTHONPATH"])
            )

            result = _run_preflight("--migration", environment, directory)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("preflight=PASS mode=migration", result.stdout)

    def test_seed_preflight_is_bounded_and_redacts_unsafe_credentials(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = _preflight_environment(
                directory,
                include_uvicorn=False,
            )
            environment.pop("HERMES_RELEASES_DIR")
            dsn = directory / "bootstrap-dsn"
            password = directory / "initial-password"
            _write_secret(dsn, "postgresql://bootstrap-secret-must-not-appear")
            _write_secret(password, "password-secret-must-not-appear")
            environment["HERMES_BOOTSTRAP_DSN_FILE"] = str(dsn)
            environment["HERMES_INITIAL_USER_PASSWORD_FILE"] = str(password)

            result = _run_preflight("--seed", environment, directory)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("preflight=PASS mode=seed", result.stdout)
            self.assertNotIn("bootstrap-secret-must-not-appear", result.stdout)
            self.assertNotIn("password-secret-must-not-appear", result.stdout)

            password.chmod(0o640)
            rejected = _run_preflight("--seed", environment, directory)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertNotIn(
                "password-secret-must-not-appear",
                rejected.stdout + rejected.stderr,
            )

    def test_runtime_preflight_rejects_unsafe_dsn_files_without_disclosure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = _preflight_environment(
                directory,
                include_uvicorn=True,
            )
            sentinel = "postgresql://runtime-secret-must-not-appear"
            signing_secret = directory / "business-api-signing-secret"
            _write_secret(signing_secret, "s" * 48)
            environment["HERMES_SIGNING_SECRET_FILE"] = str(signing_secret)
            private_dsn = directory / "runtime-private-dsn"
            _write_secret(private_dsn, sentinel)

            cases: list[tuple[str, str | None]] = [
                ("missing", None),
                ("relative", private_dsn.name),
            ]
            empty_dsn = directory / "runtime-empty-dsn"
            _write_secret(empty_dsn, "")
            cases.append(("empty", str(empty_dsn)))
            wide_dsn = directory / "runtime-wide-dsn"
            _write_secret(wide_dsn, sentinel, mode=0o640)
            cases.append(("wide-permissions", str(wide_dsn)))
            symlink_dsn = directory / "runtime-symlink-dsn"
            symlink_dsn.symlink_to(private_dsn)
            cases.append(("symlink", str(symlink_dsn)))

            for name, dsn_path in cases:
                with self.subTest(name=name):
                    case_environment = dict(environment)
                    if dsn_path is not None:
                        case_environment["HERMES_RUNTIME_DSN_FILE"] = dsn_path
                    else:
                        case_environment.pop("HERMES_RUNTIME_DSN_FILE", None)

                    result = _run_preflight(
                        "--runtime",
                        case_environment,
                        directory,
                    )

                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(sentinel, result.stdout)
                    self.assertNotIn(sentinel, result.stderr)

            valid_environment = dict(environment)
            valid_environment["HERMES_RUNTIME_DSN_FILE"] = str(private_dsn)
            valid = _run_preflight("--runtime", valid_environment, directory)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertIn("preflight=PASS mode=runtime", valid.stdout)
            self.assertNotIn(sentinel, valid.stdout)
            self.assertNotIn(sentinel, valid.stderr)

    def test_nginx_is_include_safe_and_routes_rest_and_websockets_separately(
        self,
    ) -> None:
        nginx = NGINX.read_text()
        self.assertNotRegex(nginx, r"(?m)^\s*(?:http|events|server)\s*\{")
        self.assertNotRegex(nginx, r"(?m)^\s*listen\s+")
        self.assertIn("location /hermes/api/", nginx)
        self.assertIn("location /hermes/auth/", nginx)
        self.assertIn(
            "proxy_pass http://127.0.0.1:8101/auth/;",
            nginx,
        )
        self.assertIn("location = /hermes/api/ws", nginx)
        self.assertIn("location = /hermes/internal/connector/ws", nginx)
        self.assertIn("location /hermes/files/", nginx)
        self.assertIn("location = /hermes/live", nginx)
        self.assertIn("location = /hermes/ready", nginx)
        self.assertGreaterEqual(
            nginx.count('proxy_set_header Upgrade "$http_upgrade";'),
            2,
        )
        self.assertGreaterEqual(
            nginx.count('proxy_set_header Connection "upgrade";'),
            2,
        )
        self.assertIn("proxy_read_timeout", nginx)
        self.assertIn("client_max_body_size", nginx)

    def test_business_api_requires_database_and_signing_credentials(self) -> None:
        unit = (SYSTEMD / "hermes-cloud-business-api.service").read_text()
        preflight = PREFLIGHT.read_text()

        self.assertIn(
            "LoadCredential=database_dsn:",
            unit,
        )
        self.assertIn(
            "LoadCredential=signing_secret:",
            unit,
        )
        self.assertIn(
            "Environment=HERMES_RUNTIME_DSN_FILE=%d/database_dsn",
            unit,
        )
        self.assertIn(
            "Environment=HERMES_SIGNING_SECRET_FILE=%d/signing_secret",
            unit,
        )
        self.assertIn(
            "HERMES_SIGNING_SECRET_FILE:?HERMES_SIGNING_SECRET_FILE is required",
            preflight,
        )
        self.assertIn(
            'validate_secret_file "$HERMES_SIGNING_SECRET_FILE" "signing"',
            preflight,
        )

    def test_examples_and_units_contain_references_not_secret_values(self) -> None:
        candidates = [
            ENVIRONMENT,
            *SYSTEMD.glob("*.service"),
            *SCRIPTS.glob("*"),
        ]
        self.assertTrue(ENVIRONMENT.is_file())
        for path in candidates:
            if not path.is_file():
                continue
            content = path.read_text()
            with self.subTest(path=path.name):
                for pattern in SECRET_VALUE_PATTERNS:
                    self.assertIsNone(pattern.search(content), pattern.pattern)

    def test_helpers_default_to_no_sudo_and_no_state_change(self) -> None:
        validate = (SCRIPTS / "validate.sh").read_text()
        rollback = (SCRIPTS / "rollback.sh").read_text()
        for name in (
            "preflight.sh",
            "health.sh",
            "rollback.sh",
            "validate.sh",
        ):
            content = (SCRIPTS / name).read_text()
            with self.subTest(script=name):
                self.assertNotRegex(content, r"(?m)^\s*sudo\b")
        self.assertIn("--apply", rollback)
        self.assertIn("DRY RUN", rollback)
        self.assertIn("--systemd", validate)
        self.assertIn("--nginx", validate)
        self.assertNotIn("systemctl start", validate)
        self.assertNotIn("systemctl restart", validate)

    def test_validate_uses_final_release_venv_not_old_system_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            releases = directory / "releases"
            release = releases / "20260801T120000Z"
            scripts = release / "deploy/test_server/scripts"
            tests = release / "deploy/test_server/tests"
            scripts.mkdir(parents=True)
            tests.mkdir(parents=True)
            validate = scripts / "validate.sh"
            validate.write_text((SCRIPTS / "validate.sh").read_text())
            validate.chmod(0o700)
            expected_python = release / ".venv/bin/python"
            expected_python.parent.mkdir(parents=True)
            resolved_release = release.resolve()
            resolved_python = expected_python.resolve()
            expected_python.write_text(
                "#!/bin/sh\n"
                f"export HERMES_VALIDATION_INTERPRETER={shlex.quote(str(resolved_python))}\n"
                f'exec {shlex.quote(sys.executable)} "$@"\n'
            )
            expected_python.chmod(0o700)
            (tests / "test_release_python.py").write_text(
                "import os\n"
                "import unittest\n"
                "from pathlib import Path\n\n"
                "class ReleasePythonTest(unittest.TestCase):\n"
                "    def test_final_release_interpreter(self):\n"
                "        release = Path(__file__).resolve().parents[3]\n"
                "        self.assertEqual(str(release), os.environ['EXPECTED_RELEASE'])\n"
                "        self.assertEqual(\n"
                "            os.environ['HERMES_VALIDATION_INTERPRETER'],\n"
                "            os.environ['EXPECTED_RELEASE_PYTHON'],\n"
                "        )\n"
            )
            current = directory / "current"
            current.symlink_to(release, target_is_directory=True)
            fake_bin = directory / "fake-bin"
            fake_bin.mkdir()
            system_python_capture = directory / "system-python-used"
            system_python = fake_bin / "python3"
            system_python.write_text(
                "#!/bin/sh\n"
                f"touch {shlex.quote(str(system_python_capture))}\n"
                "echo 'SyntaxError: Python 3.6 cannot load release tests' >&2\n"
                "exit 1\n"
            )
            system_python.chmod(0o700)
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "HERMES_RELEASES_DIR": str(releases),
                "HERMES_CURRENT": str(current),
                "EXPECTED_RELEASE": str(resolved_release),
                "EXPECTED_RELEASE_PYTHON": str(resolved_python),
            }

            result = subprocess.run(
                ("bash", str(validate)),
                cwd=release,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("deployment_artifacts=PASS", result.stdout)
            self.assertFalse(system_python_capture.exists())

    def test_validate_isolates_both_python_invocations_from_host_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            releases = directory / "releases"
            release = releases / "20260801T120000Z"
            scripts = release / "deploy/test_server/scripts"
            tests = release / "deploy/test_server/tests"
            scripts.mkdir(parents=True)
            tests.mkdir(parents=True)
            validate = scripts / "validate.sh"
            validate.write_text((SCRIPTS / "validate.sh").read_text())
            validate.chmod(0o700)
            release_python = release / ".venv/bin/python"
            release_python.parent.mkdir(parents=True)
            release_python.write_text(
                f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'
            )
            release_python.chmod(0o700)
            genuine_capture = directory / "genuine-test-ran"
            malicious_capture = directory / "malicious-import-ran"
            (tests / "test_genuine_release.py").write_text(
                "import os\n"
                "import unittest\n"
                "from pathlib import Path\n\n"
                "class GenuineReleaseTest(unittest.TestCase):\n"
                "    def test_clean_environment(self):\n"
                "        for name in (\n"
                "            'PYTHONPATH', 'PYTHONHOME', 'PYTHONUSERBASE',\n"
                "            'PYTHONSTARTUP', 'PYTHONINSPECT', 'PYTHONBREAKPOINT',\n"
                "            'PYTHONWARNINGS', 'PYTHONSAFEPATH', 'PYTHONNOUSERSITE',\n"
                "        ):\n"
                "            self.assertNotIn(name, os.environ)\n"
                "        Path(os.environ['GENUINE_TEST_CAPTURE']).write_text('genuine')\n"
            )
            malicious = directory / "malicious-python"
            malicious.mkdir()
            (malicious / "sitecustomize.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "Path(os.environ['MALICIOUS_IMPORT_CAPTURE']).write_text('sitecustomize')\n"
            )
            (malicious / "unittest.py").write_text(
                "raise RuntimeError('fake unittest imported')\n"
            )
            current = directory / "current"
            current.symlink_to(release, target_is_directory=True)
            polluted_environment = {
                **os.environ,
                "HERMES_RELEASES_DIR": str(releases),
                "HERMES_CURRENT": str(current),
                "PYTHONPATH": str(malicious),
                "PYTHONHOME": str(malicious),
                "PYTHONUSERBASE": str(malicious),
                "PYTHONSTARTUP": str(malicious / "sitecustomize.py"),
                "PYTHONINSPECT": "1",
                "PYTHONBREAKPOINT": "malicious.breakpoint",
                "PYTHONWARNINGS": "error",
                "PYTHONSAFEPATH": "0",
                "PYTHONNOUSERSITE": "0",
                "GENUINE_TEST_CAPTURE": str(genuine_capture),
                "MALICIOUS_IMPORT_CAPTURE": str(malicious_capture),
            }

            result = subprocess.run(
                ("bash", str(validate)),
                cwd=release,
                env=polluted_environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(genuine_capture.read_text(), "genuine")
            self.assertFalse(malicious_capture.exists())

    def test_validate_fails_closed_without_supported_release_python(self) -> None:
        for case in ("missing", "too-old"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                releases = directory / "releases"
                release = releases / "20260801T120000Z"
                scripts = release / "deploy/test_server/scripts"
                tests = release / "deploy/test_server/tests"
                scripts.mkdir(parents=True)
                tests.mkdir(parents=True)
                validate = scripts / "validate.sh"
                validate.write_text((SCRIPTS / "validate.sh").read_text())
                validate.chmod(0o700)
                version_probe_capture = directory / "version-probe-capture"
                if case == "too-old":
                    release_python = release / ".venv/bin/python"
                    release_python.parent.mkdir(parents=True)
                    release_python.write_text(
                        "#!/bin/sh\n"
                        'if [ "$#" -ne 3 ] || [ "${1-}" != "-I" ] || '
                        '[ "${2-}" != "-c" ]; then\n'
                        "  exit 97\n"
                        "fi\n"
                        f'printf \'%s\\n\' "$1" "$2" "$3" > '
                        f"{shlex.quote(str(version_probe_capture))}\n"
                        f"exec {shlex.quote(sys.executable)} -I -c "
                        "'import sys; received_probe = sys.argv[1]; "
                        'sys.version_info = (3, 6, 15); exec(received_probe)\' "$3"\n'
                    )
                    release_python.chmod(0o700)
                current = directory / "current"
                current.symlink_to(release, target_is_directory=True)
                result = subprocess.run(
                    ("bash", str(validate)),
                    cwd=release,
                    env={
                        **os.environ,
                        "HERMES_RELEASES_DIR": str(releases),
                        "HERMES_CURRENT": str(current),
                    },
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertNotEqual(result.returncode, 0)
                expected = (
                    "release virtual environment is missing Python"
                    if case == "missing"
                    else "release virtual environment requires Python 3.11 or newer"
                )
                self.assertIn(expected, result.stderr)
                if case == "too-old":
                    self.assertEqual(
                        version_probe_capture.read_text().splitlines(),
                        [
                            "-I",
                            "-c",
                            "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)",
                        ],
                    )

    def test_validate_rejects_untrusted_resolved_release_paths(self) -> None:
        for case in ("outside-releases", "wrong-current"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                releases = directory / "releases"
                releases.mkdir()
                release = (
                    directory / "candidate"
                    if case == "outside-releases"
                    else releases / "20260801T120000Z"
                )
                scripts = release / "deploy/test_server/scripts"
                scripts.mkdir(parents=True)
                validate = scripts / "validate.sh"
                validate.write_text((SCRIPTS / "validate.sh").read_text())
                validate.chmod(0o700)
                current_target = release
                if case == "wrong-current":
                    current_target = releases / "20260801T110000Z"
                    current_target.mkdir()
                current = directory / "current"
                current.symlink_to(current_target, target_is_directory=True)

                result = subprocess.run(
                    ("bash", str(validate)),
                    cwd=release,
                    env={
                        **os.environ,
                        "HERMES_RELEASES_DIR": str(releases),
                        "HERMES_CURRENT": str(current),
                    },
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertNotEqual(result.returncode, 0)
                expected = (
                    "validation release is outside the trusted release directory"
                    if case == "outside-releases"
                    else "current release does not resolve to the validation release"
                )
                self.assertIn(expected, result.stderr)

    def test_validate_rechecks_current_after_python_tests_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            releases = directory / "releases"
            release = releases / "20260801T120000Z"
            replacement_release = releases / "20260801T130000Z"
            scripts = release / "deploy/test_server/scripts"
            tests = release / "deploy/test_server/tests"
            scripts.mkdir(parents=True)
            tests.mkdir(parents=True)
            replacement_release.mkdir(parents=True)
            validate = scripts / "validate.sh"
            validate.write_text((SCRIPTS / "validate.sh").read_text())
            validate.chmod(0o700)
            release_python = release / ".venv/bin/python"
            release_python.parent.mkdir(parents=True)
            release_python.write_text(
                f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'
            )
            release_python.chmod(0o700)
            ready_fifo = directory / "test-ready.fifo"
            release_fifo = directory / "test-release.fifo"
            os.mkfifo(ready_fifo)
            os.mkfifo(release_fifo)
            (tests / "test_current_race.py").write_text(
                "import os\n"
                "import unittest\n\n"
                "class CurrentRaceTest(unittest.TestCase):\n"
                "    def test_current_stays_pinned(self):\n"
                "        with open(os.environ['CURRENT_TEST_READY'], 'w') as ready:\n"
                "            ready.write('1')\n"
                "        with open(os.environ['CURRENT_TEST_RELEASE'], 'r') as release:\n"
                "            self.assertEqual(release.read(1), '1')\n"
            )
            current = directory / "current"
            current.symlink_to(release, target_is_directory=True)
            ready_descriptor = os.open(ready_fifo, os.O_RDWR | os.O_NONBLOCK)
            release_descriptor = os.open(release_fifo, os.O_RDWR | os.O_NONBLOCK)
            process = subprocess.Popen(
                ("bash", str(validate)),
                cwd=release,
                env={
                    **os.environ,
                    "HERMES_RELEASES_DIR": str(releases),
                    "HERMES_CURRENT": str(current),
                    "CURRENT_TEST_READY": str(ready_fifo),
                    "CURRENT_TEST_RELEASE": str(release_fifo),
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                readable, _, _ = select.select((ready_descriptor,), (), (), 5)
                self.assertEqual(readable, [ready_descriptor])
                self.assertEqual(os.read(ready_descriptor, 1), b"1")
                next_current = directory / "current.next"
                next_current.symlink_to(replacement_release, target_is_directory=True)
                os.replace(next_current, current)
                self.assertEqual(os.write(release_descriptor, b"1"), 1)
                stdout, stderr = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
                os.close(ready_descriptor)
                os.close(release_descriptor)

            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(
                "current release changed during validation",
                stderr,
            )
            self.assertNotIn("deployment_artifacts=PASS", stdout)

    def test_validate_dispatches_optional_host_checks_and_propagates_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            releases = directory / "releases"
            release = releases / "20260801T120000Z"
            deploy = release / "deploy/test_server"
            scripts = deploy / "scripts"
            tests = deploy / "tests"
            systemd = deploy / "systemd"
            scripts.mkdir(parents=True)
            tests.mkdir(parents=True)
            systemd.mkdir(parents=True)
            validate = scripts / "validate.sh"
            validate.write_text((SCRIPTS / "validate.sh").read_text())
            validate.chmod(0o700)
            release_python = release / ".venv/bin/python"
            release_python.parent.mkdir(parents=True)
            release_python.write_text(
                f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'
            )
            release_python.chmod(0o700)
            (tests / "test_real.py").write_text(
                "import unittest\n\n"
                "class RealTest(unittest.TestCase):\n"
                "    def test_real(self):\n"
                "        self.assertTrue(True)\n"
            )
            service = systemd / "hermes-cloud-test.service"
            service.write_text("[Service]\nExecStart=/bin/true\n")
            nginx_config = directory / "nginx.conf"
            nginx_config.write_text("events {}\n")
            fake_bin = directory / "fake-bin"
            fake_bin.mkdir()
            systemd_capture = directory / "systemd-capture"
            nginx_capture = directory / "nginx-capture"
            fake_systemd = fake_bin / "systemd-analyze"
            fake_systemd.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$SYSTEMD_CAPTURE"\n'
            )
            fake_systemd.chmod(0o700)
            fake_nginx = fake_bin / "nginx"
            fake_nginx.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$NGINX_CAPTURE"\nexit 23\n'
            )
            fake_nginx.chmod(0o700)
            current = directory / "current"
            current.symlink_to(release, target_is_directory=True)
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "HERMES_RELEASES_DIR": str(releases),
                "HERMES_CURRENT": str(current),
                "SYSTEMD_CAPTURE": str(systemd_capture),
                "NGINX_CAPTURE": str(nginx_capture),
            }

            systemd_result = subprocess.run(
                ("bash", str(validate), "--systemd"),
                cwd=release,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            nginx_result = subprocess.run(
                ("bash", str(validate), "--nginx", str(nginx_config)),
                cwd=release,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(systemd_result.returncode, 0, systemd_result.stderr)
            self.assertEqual(
                systemd_capture.read_text().splitlines(),
                ["verify", str(service.resolve())],
            )
            self.assertEqual(nginx_result.returncode, 23)
            self.assertEqual(
                nginx_capture.read_text().splitlines(),
                ["-t", "-c", str(nginx_config)],
            )
            self.assertNotIn("deployment_artifacts=PASS", nginx_result.stdout)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import ast
import asyncio
import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

ROOT = Path(__file__).parents[1]
CLOUD_ROOT = ROOT.parents[1]
RUNNER = ROOT / "scripts" / "seed_test_data.py"
sys.path.insert(0, str(CLOUD_ROOT / "src"))

spec = importlib.util.spec_from_file_location("hermes_cloud_test_seed", RUNNER)
if spec is None or spec.loader is None:
    raise RuntimeError("seed runner cannot be loaded")
seed_runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = seed_runner
spec.loader.exec_module(seed_runner)

from hermes_cloud.platform.postgres.models import (
    AgentModel,
    DeviceModel,
    SessionProjectionModel,
    TenantModel,
)
from hermes_cloud.platform.sqlite import migrations as sqlite_migrations
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata


class _ScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return list(self._rows)

    def filter(self, *_criteria: object) -> _ScalarResult:
        return self

    def limit(self, _limit: int) -> _ScalarResult:
        return self


class _Session:
    def __init__(self, rows: dict[type[object], list[object]]) -> None:
        self.rows = rows
        self.pending: list[object] = []
        self.statements: list[object] = []

    def scalars(self, statement: object) -> _ScalarResult:
        self.statements.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        return _ScalarResult(self.rows.get(entity, []))

    def query(self, model: type[object]) -> _ScalarResult:
        return _ScalarResult(self.rows.get(model, []))

    def add(self, model: object) -> None:
        self.pending.append(model)

    def get(self, model: type[object], identity: tuple[object, object]) -> object | None:
        for row in self.rows.get(model, []):
            if (row.tenant_id, row.session_id) == identity:
                return row
        return None


class _Transaction:
    def __init__(self, factory: _SessionFactory) -> None:
        self._factory = factory
        self._session = _Session(factory.rows)

    def __enter__(self) -> _Session:
        self._factory.begin_calls += 1
        self._factory.sessions.append(self._session)
        return self._session

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is None:
            for model in self._session.pending:
                self._factory.rows.setdefault(type(model), []).append(model)
            self._factory.add_calls += len(self._session.pending)
        return False


class _SessionFactory:
    def __init__(self) -> None:
        self.rows: dict[type[object], list[object]] = {}
        self.begin_calls = 0
        self.add_calls = 0
        self.sessions: list[_Session] = []

    def begin(self) -> _Transaction:
        return _Transaction(self)


class _Hasher:
    encoded = "$argon2id$test-seed-hash"

    def hash(self, password: str) -> str:
        if password != "correct-password":
            raise AssertionError("unexpected password")
        return self.encoded

    def verify(self, password_hash: str, password: str) -> bool:
        return password_hash == self.encoded and password == "correct-password"


class _FailingDisposeEngine:
    def __init__(self, detail: str) -> None:
        self._detail = detail
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1
        raise RuntimeError(self._detail)


class _Engine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


class _RaisingSessionFactory:
    def __init__(self, failure: BaseException) -> None:
        self._failure = failure

    def begin(self) -> None:
        raise self._failure


class _CommitThenRaiseTransaction(_Transaction):
    def __exit__(self, exc_type, exc, traceback) -> bool:
        outcome = super().__exit__(exc_type, exc, traceback)
        if exc_type is None:
            raise RuntimeError("commit acknowledgement was lost")
        return outcome


class _CommitThenRaiseFactory(_SessionFactory):
    def begin(self) -> _CommitThenRaiseTransaction:
        return _CommitThenRaiseTransaction(self)


class _ConcurrentWinnerTransaction(_Transaction):
    def __init__(
        self,
        factory: _ConcurrentCollisionFactory,
        winner_rows: dict[type[object], list[object]],
    ) -> None:
        super().__init__(factory)
        self._winner_rows = winner_rows

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None:
            return False
        self._factory.rows = {
            model: list(rows) for model, rows in self._winner_rows.items()
        }
        raise IntegrityError(
            "INSERT",
            {},
            RuntimeError("concurrent unique constraint collision"),
        )


class _ConcurrentCollisionFactory(_SessionFactory):
    def __init__(self, winner_rows: dict[type[object], list[object]]) -> None:
        super().__init__()
        self._winner_rows = winner_rows

    def begin(self) -> _Transaction:
        if self.begin_calls == 0:
            return _ConcurrentWinnerTransaction(self, self._winner_rows)
        return _Transaction(self)


def _config():
    return seed_runner.SeedConfig(
        tenant_slug="android-test",
        tenant_display_name="Android Test",
        username="android-user",
        user_display_name="Android User",
        workspace_key="android",
        workspace_display_name="Android",
        agent_key="seed-agent",
    )


def _owner_control_config():
    return seed_runner.SeedConfig(
        tenant_slug="android-test",
        tenant_display_name="Android Test",
        username="android-user",
        user_display_name="Android User",
        workspace_key="android",
        workspace_display_name="Android",
        owner_control_enabled=True,
        agent_key="seed-agent",
        device_key="android-device",
    )


def _write_secret(path: Path, value: str, *, mode: int = 0o600) -> None:
    path.write_text(value)
    path.chmod(mode)


class SeedTestDataTest(unittest.TestCase):
    def test_agent_identity_is_required_and_owner_control_only_gates_device(
        self,
    ) -> None:
        base = _config().as_environment()
        self.assertFalse(
            seed_runner.SeedConfig.from_environment(base).owner_control_enabled
        )

        missing_agent = dict(base)
        missing_agent.pop("HERMES_SEED_AGENT_KEY")
        for environment in (
            missing_agent,
            {**base, "HERMES_SEED_OWNER_CONTROL_ENABLED": "yes"},
            {
                **base,
                "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
            },
            {
                **base,
                "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
                "HERMES_SEED_DEVICE_KEY": "*",
            },
        ):
            with (
                self.subTest(environment=environment),
                self.assertRaises(seed_runner.SeedConfigurationError),
            ):
                seed_runner.SeedConfig.from_environment(environment)

        configured = seed_runner.SeedConfig.from_environment(
            {
                **base,
                "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
                "HERMES_SEED_DEVICE_KEY": "android-device",
            }
        )
        self.assertEqual(configured, _owner_control_config())

    def test_owner_control_seed_creates_exact_agent_device_identity_chain(
        self,
    ) -> None:
        factory = _SessionFactory()

        first = seed_runner.seed_test_data(
            session_factory=factory,
            config=_owner_control_config(),
            initial_password="correct-password",
            apply=True,
            password_hasher=_Hasher(),
        )
        second = seed_runner.seed_test_data(
            session_factory=factory,
            config=_owner_control_config(),
            initial_password="correct-password",
            apply=True,
            password_hasher=_Hasher(),
        )

        agent = factory.rows[seed_runner.AgentModel][0]
        device = factory.rows[seed_runner.DeviceModel][0]
        self.assertEqual((first.created, first.existing, first.updated), (8, 0, 0))
        self.assertEqual((second.created, second.existing, second.updated), (0, 8, 0))
        self.assertEqual(device.agent_id, agent.agent_id)
        self.assertEqual(device.device_key, "android-device")
        self.assertEqual(agent.workspace_id, device.workspace_id)

    def test_seed_never_mints_a_session_projection_or_catalog_identity(self) -> None:
        factory = _SessionFactory()

        seed_runner.seed_test_data(
            session_factory=factory,
            config=_owner_control_config(),
            initial_password="correct-password",
            apply=True,
            password_hasher=_Hasher(),
        )

        self.assertNotIn(SessionProjectionModel, factory.rows)

    def test_dry_run_plans_all_rows_without_writes(self) -> None:
        factory = _SessionFactory()

        result = seed_runner.seed_test_data(
            session_factory=factory,
            config=_config(),
            initial_password="correct-password",
            apply=False,
            password_hasher=_Hasher(),
        )

        self.assertEqual(result.mode, "plan")
        self.assertEqual(result.created, 7)
        self.assertEqual(result.existing, 0)
        self.assertEqual(factory.begin_calls, 1)
        self.assertEqual(factory.add_calls, 0)
        self.assertEqual(factory.rows, {})

    def test_apply_is_idempotent_and_uses_one_orm_transaction_per_run(self) -> None:
        factory = _SessionFactory()

        first = seed_runner.seed_test_data(
            session_factory=factory,
            config=_config(),
            initial_password="correct-password",
            apply=True,
            password_hasher=_Hasher(),
        )
        second = seed_runner.seed_test_data(
            session_factory=factory,
            config=_config(),
            initial_password="correct-password",
            apply=True,
            password_hasher=_Hasher(),
        )

        self.assertEqual((first.created, first.existing), (7, 0))
        self.assertEqual((second.created, second.existing), (0, 7))
        self.assertEqual(factory.begin_calls, 2)
        self.assertEqual(factory.add_calls, 7)
        agent = factory.rows[seed_runner.AgentModel][0]
        self.assertEqual(agent.agent_key, "seed-agent")

    def test_conflicting_existing_content_fails_closed_without_new_writes(
        self,
    ) -> None:
        factory = _SessionFactory()
        seed_runner.seed_test_data(
            session_factory=factory,
            config=_config(),
            initial_password="correct-password",
            apply=True,
            password_hasher=_Hasher(),
        )
        tenant = factory.rows[seed_runner.TenantModel][0]
        tenant.display_name = "Conflicting tenant"
        writes_before_conflict = factory.add_calls

        with self.assertRaises(seed_runner.SeedConflict):
            seed_runner.seed_test_data(
                session_factory=factory,
                config=_config(),
                initial_password="correct-password",
                apply=True,
                password_hasher=_Hasher(),
            )

        self.assertEqual(factory.add_calls, writes_before_conflict)

    def test_late_credential_conflict_rolls_back_all_pending_models(
        self,
    ) -> None:
        winner = _SessionFactory()
        seed_runner.seed_test_data(
            session_factory=winner,
            config=_config(),
            initial_password="correct-password",
            apply=True,
            password_hasher=_Hasher(),
        )
        credential = winner.rows[seed_runner.PasswordCredentialModel][0]
        credential.status = "revoked"
        factory = _SessionFactory()
        factory.rows = {
            seed_runner.PasswordCredentialModel: [credential],
        }

        with self.assertRaises(seed_runner.SeedConflict):
            seed_runner.seed_test_data(
                session_factory=factory,
                config=_config(),
                initial_password="correct-password",
                apply=True,
                password_hasher=_Hasher(),
            )

        self.assertEqual(factory.begin_calls, 1)
        self.assertEqual(len(factory.sessions[0].pending), 6)
        self.assertEqual(factory.add_calls, 0)
        self.assertEqual(
            set(factory.rows),
            {seed_runner.PasswordCredentialModel},
        )

    def test_concurrent_unique_collision_revalidates_exact_winner_read_only(
        self,
    ) -> None:
        winner = _SessionFactory()
        seed_runner.seed_test_data(
            session_factory=winner,
            config=_config(),
            initial_password="correct-password",
            apply=True,
            password_hasher=_Hasher(),
        )
        factory = _ConcurrentCollisionFactory(winner.rows)

        result = seed_runner.seed_test_data(
            session_factory=factory,
            config=_config(),
            initial_password="correct-password",
            apply=True,
            password_hasher=_Hasher(),
        )

        self.assertEqual(result.mode, "apply")
        self.assertEqual((result.created, result.existing), (0, 7))
        self.assertEqual(factory.begin_calls, 2)
        self.assertIsNot(factory.sessions[0], factory.sessions[1])
        self.assertEqual(len(factory.sessions[0].pending), 7)
        self.assertEqual(factory.sessions[1].pending, [])
        self.assertEqual(factory.add_calls, 0)

    def test_concurrent_unique_collision_with_different_winner_fails_closed(
        self,
    ) -> None:
        winner = _SessionFactory()
        seed_runner.seed_test_data(
            session_factory=winner,
            config=_config(),
            initial_password="correct-password",
            apply=True,
            password_hasher=_Hasher(),
        )
        winner.rows[seed_runner.TenantModel][0].display_name = "Other tenant"
        factory = _ConcurrentCollisionFactory(winner.rows)

        with self.assertRaises(seed_runner.SeedConflict):
            seed_runner.seed_test_data(
                session_factory=factory,
                config=_config(),
                initial_password="correct-password",
                apply=True,
                password_hasher=_Hasher(),
            )

        self.assertEqual(factory.begin_calls, 2)
        self.assertEqual(factory.add_calls, 0)

    def test_password_conflict_is_redacted(self) -> None:
        factory = _SessionFactory()
        seed_runner.seed_test_data(
            session_factory=factory,
            config=_config(),
            initial_password="correct-password",
            apply=True,
            password_hasher=_Hasher(),
        )
        credential = factory.rows[seed_runner.PasswordCredentialModel][0]
        credential.password_hash = "$argon2id$secret-hash-sentinel"

        with self.assertRaises(seed_runner.SeedConflict) as raised:
            seed_runner.seed_test_data(
                session_factory=factory,
                config=_config(),
                initial_password="different-password",
                apply=True,
                password_hasher=_Hasher(),
            )

        rendered = str(raised.exception)
        self.assertNotIn("different-password", rendered)
        self.assertNotIn("secret-hash-sentinel", rendered)

    def test_secret_files_are_absolute_private_regular_and_nonempty(self) -> None:
        sentinel = "secret-value-must-not-leak"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            valid = directory / "valid"
            _write_secret(valid, sentinel)
            self.assertEqual(
                seed_runner.read_secret_file(str(valid), name="test"),
                sentinel,
            )

            empty = directory / "empty"
            _write_secret(empty, "")
            wide = directory / "wide"
            _write_secret(wide, sentinel, mode=0o640)
            symlink = directory / "symlink"
            symlink.symlink_to(valid)
            cases = ("relative", str(empty), str(wide), str(symlink))
            for path in cases:
                with self.subTest(path=path):
                    with self.assertRaises(
                        seed_runner.SeedConfigurationError
                    ) as raised:
                        seed_runner.read_secret_file(path, name="test")
                    self.assertNotIn(sentinel, str(raised.exception))

    def test_main_redacts_database_and_password_failures(self) -> None:
        database_secret = "postgresql://seed-secret-must-not-leak"
        password_secret = "password-secret-must-not-leak"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            dsn = directory / "bootstrap-dsn"
            password = directory / "initial-password"
            _write_secret(dsn, database_secret)
            _write_secret(password, password_secret)
            environment = {
                **_config().as_environment(),
                "HERMES_BOOTSTRAP_DSN_FILE": str(dsn),
                "HERMES_INITIAL_USER_PASSWORD_FILE": str(password),
            }

            def fail_engine(database_url: str, **_: object) -> object:
                raise RuntimeError(f"cannot open {database_url}")

            with self.assertRaises(SystemExit) as raised:
                seed_runner.main(
                    [],
                    environment=environment,
                    engine_factory=fail_engine,
                )

            rendered = str(raised.exception)
            self.assertNotIn(database_secret, rendered)
            self.assertNotIn(password_secret, rendered)
            self.assertEqual(rendered, "seed failed; database unchanged")

    def test_main_redacts_business_and_dispose_failures_after_engine_creation(
        self,
    ) -> None:
        database_secret = "postgresql://seed-secret-must-not-leak"
        password_secret = "password-secret-must-not-leak"
        password_hash = "$argon2id$secret-hash-must-not-leak"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            dsn = directory / "bootstrap-dsn"
            password = directory / "initial-password"
            _write_secret(dsn, database_secret)
            _write_secret(password, password_secret)
            engine = _FailingDisposeEngine(
                f"{database_secret} {password_secret} {password_hash}"
            )

            with self.assertRaises(SystemExit) as raised:
                seed_runner.main(
                    [],
                    environment={
                        **_config().as_environment(),
                        "HERMES_BOOTSTRAP_DSN_FILE": str(dsn),
                        "HERMES_INITIAL_USER_PASSWORD_FILE": str(password),
                    },
                    engine_factory=lambda *_args, **_kwargs: engine,
                    session_factory_builder=lambda **_kwargs: _RaisingSessionFactory(
                        RuntimeError(
                            f"{database_secret} {password_secret} {password_hash}"
                        )
                    ),
                )

            rendered = str(raised.exception)
            self.assertEqual(rendered, "seed failed; database unchanged")
            self.assertNotIn(database_secret, rendered)
            self.assertNotIn(password_secret, rendered)
            self.assertNotIn(password_hash, rendered)
            self.assertEqual(engine.dispose_calls, 1)

    def test_main_redacts_dispose_failure_after_successful_seed(self) -> None:
        database_secret = "postgresql://seed-secret-must-not-leak"
        password_secret = "password-secret-must-not-leak"
        password_hash = "$argon2id$secret-hash-must-not-leak"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            dsn = directory / "bootstrap-dsn"
            password = directory / "initial-password"
            _write_secret(dsn, database_secret)
            _write_secret(password, password_secret)
            engine = _FailingDisposeEngine(
                f"{database_secret} {password_secret} {password_hash}"
            )

            with self.assertRaises(SystemExit) as raised:
                seed_runner.main(
                    [],
                    environment={
                        **_config().as_environment(),
                        "HERMES_BOOTSTRAP_DSN_FILE": str(dsn),
                        "HERMES_INITIAL_USER_PASSWORD_FILE": str(password),
                    },
                    engine_factory=lambda *_args, **_kwargs: engine,
                    session_factory_builder=lambda **_kwargs: _SessionFactory(),
                )

            rendered = str(raised.exception)
            self.assertEqual(rendered, "seed failed; database unchanged")
            self.assertNotIn(database_secret, rendered)
            self.assertNotIn(password_secret, rendered)
            self.assertNotIn(password_hash, rendered)
            self.assertEqual(engine.dispose_calls, 1)

    def test_main_reports_committed_apply_when_dispose_fails(self) -> None:
        database_secret = "postgresql://seed-secret-must-not-leak"
        password_secret = "password-secret-must-not-leak"
        password_hash = "$argon2id$secret-hash-must-not-leak"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            dsn = directory / "bootstrap-dsn"
            password = directory / "initial-password"
            _write_secret(dsn, database_secret)
            _write_secret(password, password_secret)
            engine = _FailingDisposeEngine(
                f"{database_secret} {password_secret} {password_hash}"
            )
            factory = _SessionFactory()

            with self.assertRaises(SystemExit) as raised:
                seed_runner.main(
                    ["--apply"],
                    environment={
                        **_config().as_environment(),
                        "HERMES_BOOTSTRAP_DSN_FILE": str(dsn),
                        "HERMES_INITIAL_USER_PASSWORD_FILE": str(password),
                    },
                    engine_factory=lambda *_args, **_kwargs: engine,
                    session_factory_builder=lambda **_kwargs: factory,
                )

            rendered = str(raised.exception)
            self.assertEqual(rendered, "seed committed; cleanup failed")
            self.assertNotIn("unchanged", rendered)
            self.assertNotIn(database_secret, rendered)
            self.assertNotIn(password_secret, rendered)
            self.assertNotIn(password_hash, rendered)
            self.assertEqual(factory.add_calls, 7)
            self.assertEqual(engine.dispose_calls, 1)

    def test_apply_commit_ack_loss_is_not_reported_as_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            dsn = directory / "bootstrap-dsn"
            password = directory / "initial-password"
            _write_secret(dsn, "postgresql://redacted")
            _write_secret(password, "redacted-password")
            engine = _Engine()
            factory = _CommitThenRaiseFactory()

            with self.assertRaises(SystemExit) as raised:
                seed_runner.main(
                    ["--apply"],
                    environment={
                        **_config().as_environment(),
                        "HERMES_BOOTSTRAP_DSN_FILE": str(dsn),
                        "HERMES_INITIAL_USER_PASSWORD_FILE": str(password),
                    },
                    engine_factory=lambda *_args, **_kwargs: engine,
                    session_factory_builder=lambda **_kwargs: factory,
                )

            self.assertEqual(
                str(raised.exception),
                "seed apply outcome unknown; rerun plan",
            )
            self.assertNotIn("unchanged", str(raised.exception))
            self.assertEqual(factory.add_calls, 7)
            self.assertEqual(engine.dispose_calls, 1)

    def test_apply_failure_before_commit_reports_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            dsn = directory / "bootstrap-dsn"
            password = directory / "initial-password"
            _write_secret(dsn, "postgresql://redacted")
            _write_secret(password, "redacted-password")
            engine = _Engine()

            with self.assertRaises(SystemExit) as raised:
                seed_runner.main(
                    ["--apply"],
                    environment={
                        **_config().as_environment(),
                        "HERMES_BOOTSTRAP_DSN_FILE": str(dsn),
                        "HERMES_INITIAL_USER_PASSWORD_FILE": str(password),
                    },
                    engine_factory=lambda *_args, **_kwargs: engine,
                    session_factory_builder=lambda **_kwargs: _RaisingSessionFactory(
                        RuntimeError("before commit")
                    ),
                )

            self.assertEqual(
                str(raised.exception),
                "seed failed; database unchanged",
            )
            self.assertEqual(engine.dispose_calls, 1)

    def test_cli_rejects_sensitive_and_unknown_arguments_without_echoing_values(
        self,
    ) -> None:
        sentinel = "synthetic-secret-must-not-leak"
        cases = (
            ["--password", sentinel],
            [f"--dsn={sentinel}"],
            ["--token", sentinel],
            ["--unknown", sentinel],
        )
        expected = (
            "usage: seed_test_data.py [-h] [--apply]\n"
            "seed_test_data.py: error: invalid arguments\n"
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                error_output = io.StringIO()
                with (
                    redirect_stderr(error_output),
                    self.assertRaises(SystemExit) as raised,
                ):
                    seed_runner._arguments(arguments)

                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(error_output.getvalue(), expected)
                self.assertNotIn(sentinel, error_output.getvalue())

    def test_main_preserves_cancellation_and_interrupt_during_cleanup(
        self,
    ) -> None:
        for failure_type in (asyncio.CancelledError, KeyboardInterrupt):
            with (
                self.subTest(failure_type=failure_type.__name__),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                dsn = directory / "bootstrap-dsn"
                password = directory / "initial-password"
                _write_secret(dsn, "postgresql://redacted")
                _write_secret(password, "redacted-password")
                engine = _FailingDisposeEngine("cleanup-secret-must-not-override")

                with self.assertRaises(failure_type) as raised:
                    seed_runner.main(
                        [],
                        environment={
                            **_config().as_environment(),
                            "HERMES_BOOTSTRAP_DSN_FILE": str(dsn),
                            "HERMES_INITIAL_USER_PASSWORD_FILE": str(password),
                        },
                        engine_factory=lambda *_args, _engine=engine, **_kwargs: (
                            _engine
                        ),
                        session_factory_builder=lambda _failure_type=(failure_type), **_kwargs: (
                            _RaisingSessionFactory(_failure_type("stop"))
                        ),
                    )

                self.assertEqual(str(raised.exception), "stop")
                self.assertEqual(engine.dispose_calls, 1)

    def test_main_plans_and_applies_same_orm_seed_to_sqlite_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database = directory / "hermes-cloud.sqlite3"
            database_url = f"sqlite+pysqlite:///{database}"
            migration_engine = build_sqlite_engine(
                database_url,
                allow_missing=True,
            )
            try:
                sqlite_migrations.upgrade_sqlite_schema(migration_engine)
            finally:
                migration_engine.dispose()
            database.chmod(0o660)
            dsn = directory / "bootstrap-dsn"
            password = directory / "initial-password"
            _write_secret(dsn, database_url)
            _write_secret(password, "correct-password")
            environment = {
                **_config().as_environment(),
                "HERMES_BOOTSTRAP_DSN_FILE": str(dsn),
                "HERMES_INITIAL_USER_PASSWORD_FILE": str(password),
            }

            plan_output = io.StringIO()
            with redirect_stdout(plan_output):
                seed_runner.main([], environment=environment)
            self.assertEqual(
                plan_output.getvalue(),
                "seed_mode=plan created=7 existing=0\n",
            )

            apply_output = io.StringIO()
            with redirect_stdout(apply_output):
                seed_runner.main(["--apply"], environment=environment)
            self.assertEqual(
                apply_output.getvalue(),
                "seed_mode=apply created=7 existing=0\n",
            )

            verify_output = io.StringIO()
            with redirect_stdout(verify_output):
                seed_runner.main([], environment=environment)
            self.assertEqual(
                verify_output.getvalue(),
                "seed_mode=plan created=0 existing=7\n",
            )

            engine = build_sqlite_engine(database_url)
            try:
                with Session(engine) as session:
                    self.assertEqual(
                        session.scalar(select(func.count()).select_from(TenantModel)),
                        1,
                    )
                self.assertEqual(
                    sqlite_migrations.plan_sqlite_schema(engine).source,
                    "current",
                )
            finally:
                engine.dispose()

    def test_owner_control_opt_in_plans_then_upgrades_existing_sqlite_seed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            database = directory / "hermes-cloud.sqlite3"
            database_url = f"sqlite+pysqlite:///{database}"
            migration_engine = build_sqlite_engine(
                database_url,
                allow_missing=True,
            )
            try:
                build_sqlite_metadata().create_all(migration_engine)
            finally:
                migration_engine.dispose()
            database.chmod(0o660)
            dsn = directory / "bootstrap-dsn"
            password = directory / "initial-password"
            _write_secret(dsn, database_url)
            _write_secret(password, "correct-password")
            base_environment = {
                **_config().as_environment(),
                "HERMES_BOOTSTRAP_DSN_FILE": str(dsn),
                "HERMES_INITIAL_USER_PASSWORD_FILE": str(password),
            }
            seed_runner.main(["--apply"], environment=base_environment)
            owner_environment = {
                **base_environment,
                "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
                "HERMES_SEED_DEVICE_KEY": "android-device",
            }

            plan_output = io.StringIO()
            with redirect_stdout(plan_output):
                seed_runner.main([], environment=owner_environment)
            self.assertEqual(
                plan_output.getvalue(),
                "seed_mode=plan created=1 existing=7\n",
            )
            engine = build_sqlite_engine(database_url)
            try:
                with Session(engine) as session:
                    self.assertIsNone(session.scalar(select(SessionProjectionModel)))
            finally:
                engine.dispose()

            apply_output = io.StringIO()
            with redirect_stdout(apply_output):
                seed_runner.main(["--apply"], environment=owner_environment)
            self.assertEqual(
                apply_output.getvalue(),
                "seed_mode=apply created=1 existing=7\n",
            )
            engine = build_sqlite_engine(database_url)
            try:
                with Session(engine) as session:
                    agent = session.scalar(select(AgentModel))
                    device = session.scalar(select(DeviceModel))
                    self.assertIsNotNone(agent)
                    self.assertIsNotNone(device)
                    self.assertEqual(device.agent_id, agent.agent_id)
                    self.assertIsNone(session.scalar(select(SessionProjectionModel)))
            finally:
                engine.dispose()

    def test_runner_uses_only_sqlalchemy_orm_and_sessionmaker_begin(self) -> None:
        source = RUNNER.read_text()
        tree = ast.parse(source)
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        sql_literals = {
            node.value.strip().split(" ", 1)[0].upper()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        self.assertIn("select", imported_names)
        self.assertIn("sessionmaker", imported_names)
        self.assertIn("begin", called_attributes)
        self.assertNotIn("text", imported_names)
        self.assertNotIn("exec_driver_sql", called_attributes)
        self.assertFalse(
            sql_literals.intersection({"SELECT", "INSERT", "UPDATE", "DELETE"})
        )


if __name__ == "__main__":
    unittest.main()

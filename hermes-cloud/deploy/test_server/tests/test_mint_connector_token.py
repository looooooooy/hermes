from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib.util
import io
import sqlite3
import stat
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from unittest import mock
from uuid import UUID

import jwt
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).parents[1]
CLOUD_ROOT = ROOT.parents[1]
RUNNER = ROOT / "scripts" / "mint_connector_token.py"
sys.path.insert(0, str(CLOUD_ROOT / "src"))

spec = importlib.util.spec_from_file_location("hermes_cloud_token_mint", RUNNER)
if spec is None or spec.loader is None:
    raise RuntimeError("token mint runner cannot be loaded")
token_mint = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = token_mint
spec.loader.exec_module(token_mint)

NOW = datetime.now(UTC).replace(microsecond=0)
SECRET = "s" * 32
TENANT_ID = UUID("33333333-3333-4333-8333-333333333333")
USER_ID = UUID("44444444-4444-4444-8444-444444444444")
WORKSPACE_ID = UUID("55555555-5555-4555-8555-555555555555")
AGENT_ID = UUID("66666666-6666-4666-8666-666666666666")
DEVICE_ID = UUID("77777777-7777-4777-8777-777777777777")
CREDENTIAL_ID = UUID("88888888-8888-4888-8888-888888888888")
PAIRING_OFFER_ID = UUID("99999999-9999-4999-8999-999999999999")
PAIRING_SESSION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PUBLIC_KEY = bytes(range(32))

from hermes_cloud.adapters.connector_auth import HmacJwtConnectorAuthenticator
from hermes_cloud.domain.persistence import PairingSessionState
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    DeviceCredentialModel,
    DeviceCredentialPublicKeyModel,
    DeviceLifecycleModel,
    DeviceModel,
    PairingEnrollmentProofModel,
    PairingOfferModel,
    PairingSessionModel,
    TenantModel,
    UserModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.runtime import (
    SQLiteOperationScopedPairingRepository,
)
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata


def _private_file(path: Path, value: str, *, mode: int = 0o600) -> str:
    path.write_text(value)
    path.chmod(mode)
    return str(path)


def _environment(secret_file: str) -> dict[str, str]:
    return {
        "HERMES_CONNECTOR_SIGNING_SECRET_FILE": secret_file,
        "HERMES_CONNECTOR_TOKEN_TENANT_ID": str(TENANT_ID),
        "HERMES_CONNECTOR_TOKEN_DEVICE_ID": str(DEVICE_ID),
        "HERMES_CONNECTOR_TOKEN_TTL_SECONDS": "300",
    }


def _owner_control_environment(secret_file: str, dsn_file: str) -> dict[str, str]:
    return {
        "HERMES_CONNECTOR_SIGNING_SECRET_FILE": secret_file,
        "HERMES_RUNTIME_DSN_FILE": dsn_file,
        "HERMES_CONNECTOR_TOKEN_TENANT_ID": str(TENANT_ID),
        "HERMES_CONNECTOR_TOKEN_DEVICE_ID": str(DEVICE_ID),
        "HERMES_CONNECTOR_TOKEN_TTL_SECONDS": "300",
        "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
        "HERMES_SEED_TENANT_SLUG": "android-test",
        "HERMES_SEED_AGENT_KEY": "android-agent",
        "HERMES_SEED_DEVICE_KEY": "android-device",
    }


def _active_owner_control_database(directory: Path) -> tuple[str, object]:
    database = directory / "runtime.sqlite3"
    database_url = f"sqlite+pysqlite:///{database}"
    engine = build_sqlite_engine(database_url, allow_missing=True)
    build_sqlite_metadata().create_all(engine)
    fingerprint = sha256(PUBLIC_KEY).hexdigest()
    with Session(engine) as session, session.begin():
        for model in (
            TenantModel(
                tenant_id=TENANT_ID,
                slug="android-test",
                display_name="Android Test",
                status="active",
                created_at=NOW - timedelta(days=1),
            ),
            UserModel(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                subject="android-user",
                display_name="Android User",
                email=None,
                status="active",
                created_at=NOW - timedelta(days=1),
            ),
            WorkspaceModel(
                tenant_id=TENANT_ID,
                workspace_id=WORKSPACE_ID,
                workspace_key="android",
                display_name="Android",
                status="active",
                created_by=USER_ID,
                created_at=NOW - timedelta(days=1),
            ),
            AgentModel(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                workspace_id=WORKSPACE_ID,
                agent_key="android-agent",
                status="active",
                last_seen_at=None,
                created_at=NOW - timedelta(days=1),
            ),
            DeviceModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                agent_id=AGENT_ID,
                workspace_id=WORKSPACE_ID,
                device_key="android-device",
                status="active",
                created_at=NOW - timedelta(days=1),
            ),
            PairingOfferModel(
                pairing_offer_id=PAIRING_OFFER_ID,
                pairing_code_digest="a" * 64,
                bootstrap_secret_digest="b" * 64,
                public_key_algorithm="ed25519",
                public_key=PUBLIC_KEY,
                credential_fingerprint=fingerprint,
                key_id=fingerprint,
                device_key="android-device",
                device_name="Android Connector",
                platform="macos",
                connector_version="1.0.0",
                state=PairingSessionState.CLAIMED.value,
                revision=1,
                expires_at=NOW + timedelta(minutes=5),
                claimed_at=NOW - timedelta(minutes=1),
                created_at=NOW - timedelta(minutes=2),
            ),
            PairingSessionModel(
                tenant_id=TENANT_ID,
                pairing_session_id=PAIRING_SESSION_ID,
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                device_id=DEVICE_ID,
                pairing_code_digest="a" * 64,
                state=PairingSessionState.CONFIRMED.value,
                failed_attempts=0,
                expires_at=NOW + timedelta(minutes=5),
                claimed_at=NOW - timedelta(minutes=1),
                confirmed_at=NOW - timedelta(seconds=30),
                created_at=NOW - timedelta(minutes=2),
            ),
            PairingEnrollmentProofModel(
                tenant_id=TENANT_ID,
                pairing_session_id=PAIRING_SESSION_ID,
                pairing_offer_id=PAIRING_OFFER_ID,
                owner_user_id=USER_ID,
                device_display_name="Android Connector",
                claim_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                scopes=["session.observe", "session.control.request"],
                challenge_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
                challenge_digest="c" * 64,
                challenge_expires_at=NOW + timedelta(seconds=30),
                owner_confirmed_at=NOW - timedelta(seconds=30),
                confirmation_digest="d" * 64,
                revision=3,
                created_at=NOW - timedelta(minutes=1),
                updated_at=NOW - timedelta(seconds=20),
            ),
            DeviceLifecycleModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                state="active",
                revision=2,
                updated_at=NOW - timedelta(seconds=20),
            ),
            DeviceCredentialModel(
                tenant_id=TENANT_ID,
                credential_id=CREDENTIAL_ID,
                device_id=DEVICE_ID,
                credential_type="public_key",
                key_id=fingerprint,
                credential_fingerprint=fingerprint,
                status="active",
                issued_at=NOW - timedelta(seconds=20),
                expires_at=NOW + timedelta(days=1),
                revoked_at=None,
            ),
            DeviceCredentialPublicKeyModel(
                tenant_id=TENANT_ID,
                credential_id=CREDENTIAL_ID,
                algorithm="ed25519",
                public_key=PUBLIC_KEY,
                credential_fingerprint=fingerprint,
                created_at=NOW - timedelta(seconds=20),
            ),
        ):
            session.add(model)
            session.flush()
    database.chmod(0o660)
    dsn_file = _private_file(directory / "runtime-dsn", database_url)
    return dsn_file, engine


class MintConnectorTokenTest(unittest.TestCase):
    def test_inspect_binding_reads_custom_seed_binding_without_token_or_secret(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            dsn_file, engine = _active_owner_control_database(directory)
            stdout = io.StringIO()
            environment = {
                "HERMES_RUNTIME_DSN_FILE": dsn_file,
                "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
                "HERMES_SEED_TENANT_SLUG": "android-test",
                "HERMES_SEED_AGENT_KEY": "android-agent",
                "HERMES_SEED_DEVICE_KEY": "android-device",
            }
            try:
                with (
                    mock.patch.object(
                        token_mint.jwt,
                        "encode",
                        side_effect=AssertionError("inspect must not mint"),
                    ),
                    contextlib.redirect_stdout(stdout),
                ):
                    token_mint.main(
                        ["--inspect-binding"],
                        environment=environment,
                        utc_now=lambda: NOW,
                    )

                rendered = stdout.getvalue()
                self.assertEqual(
                    rendered,
                    "binding "
                    f"tenant_id={TENANT_ID} "
                    f"device_id={DEVICE_ID} "
                    f"credential_id={CREDENTIAL_ID} "
                    f"agent_id={AGENT_ID} "
                    "scopes=session.observe,session.control.request\n",
                )
                self.assertNotIn(dsn_file, rendered)
                self.assertNotIn("eyJ", rendered)
            finally:
                engine.dispose()

    def test_legacy_mode_requires_canonical_uuid_claims(self) -> None:
        environment = {
            "HERMES_CONNECTOR_TOKEN_TENANT_ID": "tenant-test",
            "HERMES_CONNECTOR_TOKEN_DEVICE_ID": "device-test",
            "HERMES_CONNECTOR_TOKEN_TTL_SECONDS": "300",
        }

        with self.assertRaises(token_mint.ConnectorTokenMintError):
            token_mint.ConnectorTokenMintConfig.from_environment(environment)

    def test_owner_control_opt_in_requires_seed_exact_tenant_device_claims(
        self,
    ) -> None:
        environment = {
            "HERMES_CONNECTOR_TOKEN_TENANT_ID": str(TENANT_ID),
            "HERMES_CONNECTOR_TOKEN_DEVICE_ID": str(DEVICE_ID),
            "HERMES_CONNECTOR_TOKEN_TTL_SECONDS": "300",
            "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
            "HERMES_SEED_TENANT_SLUG": "android-test",
            "HERMES_SEED_AGENT_KEY": "android-agent",
            "HERMES_SEED_DEVICE_KEY": "android-device",
        }
        config = token_mint.ConnectorTokenMintConfig.from_environment(environment)
        self.assertEqual(config.tenant_id, str(TENANT_ID))
        self.assertEqual(config.device_id, str(DEVICE_ID))
        self.assertEqual(config.tenant_slug, "android-test")
        self.assertEqual(config.device_key, "android-device")

        for field, value in (
            ("HERMES_CONNECTOR_TOKEN_TENANT_ID", "android-test"),
            ("HERMES_CONNECTOR_TOKEN_DEVICE_ID", "android-device"),
            ("HERMES_SEED_OWNER_CONTROL_ENABLED", "yes"),
        ):
            with self.subTest(field=field):
                mismatched = {**environment, field: value}
                with self.assertRaises(token_mint.ConnectorTokenMintError):
                    token_mint.ConnectorTokenMintConfig.from_environment(mismatched)

    def test_owner_control_apply_mints_v1_token_accepted_by_production_orm_authenticator(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            secret = _private_file(directory / "signing", SECRET)
            dsn_file, engine = _active_owner_control_database(directory)
            output = directory / "connector.token"
            try:
                token_mint.main(
                    ["--apply", "--output", str(output)],
                    environment=_owner_control_environment(secret, dsn_file),
                    utc_now=lambda: NOW,
                )

                encoded = output.read_text().strip()
                claims = jwt.decode(
                    encoded,
                    SECRET,
                    algorithms=["HS256"],
                    options={
                        "verify_exp": False,
                        "verify_iat": False,
                        "verify_nbf": False,
                    },
                )
                self.assertEqual(
                    set(claims),
                    {
                        "tenant_id",
                        "device_id",
                        "credential_id",
                        "agent_id",
                        "scopes",
                        "jti",
                        "iat",
                        "nbf",
                        "exp",
                    },
                )
                self.assertEqual(claims["tenant_id"], str(TENANT_ID))
                self.assertEqual(claims["device_id"], str(DEVICE_ID))
                self.assertEqual(claims["credential_id"], str(CREDENTIAL_ID))
                self.assertEqual(claims["agent_id"], str(AGENT_ID))
                self.assertEqual(
                    claims["scopes"],
                    ["session.observe", "session.control.request"],
                )
                UUID(str(claims["jti"]))

                authority = SQLiteOperationScopedPairingRepository(
                    sessionmaker(bind=engine, expire_on_commit=False)
                )
                authenticator = HmacJwtConnectorAuthenticator(
                    SECRET.encode(),
                    utc_now=lambda: NOW,
                    device_authority=authority,
                )
                identity = asyncio.run(authenticator.authenticate(encoded))
                self.assertEqual(identity.tenant_id, str(TENANT_ID))
                self.assertEqual(identity.device_id, str(DEVICE_ID))
                self.assertEqual(identity.credential_id, str(CREDENTIAL_ID))
                self.assertEqual(identity.agent_id, str(AGENT_ID))
                self.assertFalse(identity.legacy_seed)
            finally:
                engine.dispose()

    def test_owner_control_dry_run_fails_closed_without_private_database_reference(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            secret = _private_file(directory / "signing", SECRET)
            environment = _owner_control_environment(secret, "")

            with self.assertRaises(SystemExit) as raised:
                token_mint.main([], environment=environment, utc_now=lambda: NOW)

            self.assertEqual(str(raised.exception), "mint failed; no token written")

    def test_owner_control_ttl_starts_after_a_slow_authority_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            secret = _private_file(directory / "signing", SECRET)
            dsn_file, engine = _active_owner_control_database(directory)
            output = directory / "connector.token"
            environment = _owner_control_environment(secret, dsn_file)
            environment["HERMES_CONNECTOR_TOKEN_TTL_SECONDS"] = "1"
            clock = [NOW]
            authority_method = (
                token_mint.SqlAlchemyPairingRepositoryBase.active_device_binding
            )

            def delayed_authority(repository, **arguments):
                snapshot = authority_method(repository, **arguments)
                clock[0] = NOW + timedelta(seconds=5)
                return snapshot

            try:
                with mock.patch.object(
                    token_mint.SqlAlchemyPairingRepositoryBase,
                    "active_device_binding",
                    autospec=True,
                    side_effect=delayed_authority,
                ):
                    token_mint.main(
                        ["--apply", "--output", str(output)],
                        environment=environment,
                        utc_now=lambda: clock[0],
                    )

                claims = jwt.decode(
                    output.read_text().strip(),
                    SECRET,
                    algorithms=["HS256"],
                    options={
                        "verify_exp": False,
                        "verify_iat": False,
                        "verify_nbf": False,
                    },
                )
                issued_at = int(clock[0].timestamp())
                self.assertEqual(claims["iat"], issued_at)
                self.assertEqual(claims["nbf"], issued_at)
                self.assertEqual(claims["exp"], issued_at + 1)
            finally:
                engine.dispose()

    def test_owner_control_revalidates_after_external_revocation_and_preserves_token(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            secret = _private_file(directory / "signing", SECRET)
            dsn_file, engine = _active_owner_control_database(directory)
            output = directory / "connector.token"
            output.write_text("previous-token\n")
            output.chmod(0o600)
            resolve_binding = token_mint._resolve_owner_control_binding
            calls = 0

            def revoke_between_resolves(**arguments):
                nonlocal calls
                calls += 1
                binding = resolve_binding(**arguments)
                if calls == 1:
                    with Session(engine) as external_session, external_session.begin():
                        credential = external_session.get(
                            DeviceCredentialModel,
                            (TENANT_ID, CREDENTIAL_ID),
                        )
                        self.assertIsNotNone(credential)
                        credential.status = "revoked"
                        credential.revoked_at = NOW
                return binding

            try:
                with (
                    mock.patch.object(
                        token_mint,
                        "_resolve_owner_control_binding",
                        autospec=True,
                        side_effect=revoke_between_resolves,
                    ),
                    self.assertRaises(SystemExit) as raised,
                ):
                    token_mint.main(
                        ["--apply", "--output", str(output)],
                        environment=_owner_control_environment(secret, dsn_file),
                        utc_now=lambda: NOW,
                    )

                self.assertEqual(
                    str(raised.exception),
                    "mint failed; no token written",
                )
                self.assertEqual(calls, 2)
                self.assertEqual(output.read_text(), "previous-token\n")
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            finally:
                engine.dispose()

    def test_owner_control_rejects_active_credential_with_revocation_timestamp(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            secret = _private_file(directory / "signing", SECRET)
            dsn_file, engine = _active_owner_control_database(directory)
            output = directory / "connector.token"
            try:
                with Session(engine) as session, session.begin():
                    credential = session.get(
                        DeviceCredentialModel,
                        (TENANT_ID, CREDENTIAL_ID),
                    )
                    self.assertIsNotNone(credential)
                    credential.revoked_at = NOW - timedelta(seconds=1)

                with self.assertRaises(SystemExit) as raised:
                    token_mint.main(
                        ["--apply", "--output", str(output)],
                        environment=_owner_control_environment(secret, dsn_file),
                        utc_now=lambda: NOW,
                    )

                self.assertEqual(
                    str(raised.exception),
                    "mint failed; no token written",
                )
                self.assertFalse(output.exists())
            finally:
                engine.dispose()

    def test_owner_control_rejects_ambiguous_suspended_revoked_or_expired_binding(
        self,
    ) -> None:
        cases = ("ambiguous", "suspended", "revoked", "expired")
        for state in cases:
            with (
                self.subTest(state=state),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                secret = _private_file(directory / "signing", SECRET)
                dsn_file, engine = _active_owner_control_database(directory)
                output = directory / "connector.token"
                try:
                    with Session(engine) as session, session.begin():
                        if state == "ambiguous":
                            session.add(
                                DeviceCredentialModel(
                                    tenant_id=TENANT_ID,
                                    credential_id=UUID(
                                        "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
                                    ),
                                    device_id=DEVICE_ID,
                                    credential_type="public_key",
                                    key_id="second-key",
                                    credential_fingerprint="e" * 64,
                                    status="active",
                                    issued_at=NOW - timedelta(seconds=10),
                                    expires_at=None,
                                    revoked_at=None,
                                )
                            )
                        elif state == "suspended":
                            lifecycle = session.get(
                                DeviceLifecycleModel,
                                (TENANT_ID, DEVICE_ID),
                            )
                            self.assertIsNotNone(lifecycle)
                            lifecycle.state = "suspended"
                        else:
                            credential = session.get(
                                DeviceCredentialModel,
                                (TENANT_ID, CREDENTIAL_ID),
                            )
                            self.assertIsNotNone(credential)
                            if state == "revoked":
                                credential.status = "revoked"
                                credential.revoked_at = NOW
                            else:
                                credential.expires_at = NOW

                    with self.assertRaises(SystemExit) as raised:
                        token_mint.main(
                            ["--apply", "--output", str(output)],
                            environment=_owner_control_environment(secret, dsn_file),
                            utc_now=lambda: NOW,
                        )

                    self.assertEqual(
                        str(raised.exception),
                        "mint failed; no token written",
                    )
                    self.assertFalse(output.exists())
                finally:
                    engine.dispose()

    def test_owner_control_dry_run_reads_binding_without_token_or_database_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            secret = _private_file(directory / "signing", SECRET)
            dsn_file, engine = _active_owner_control_database(directory)
            stdout = io.StringIO()
            try:
                with Session(engine) as session:
                    before = session.scalar(
                        select(func.count()).select_from(DeviceCredentialModel)
                    )
                with (
                    mock.patch.object(
                        token_mint.jwt,
                        "encode",
                        side_effect=AssertionError("dry run must not mint"),
                    ),
                    contextlib.redirect_stdout(stdout),
                ):
                    token_mint.main(
                        [],
                        environment=_owner_control_environment(secret, dsn_file),
                        utc_now=lambda: NOW,
                    )
                with Session(engine) as session:
                    after = session.scalar(
                        select(func.count()).select_from(DeviceCredentialModel)
                    )

                self.assertEqual(stdout.getvalue(), "mint_mode=plan ttl_seconds=300\n")
                self.assertEqual(before, after)
                self.assertEqual(
                    sorted(path.name for path in directory.iterdir()),
                    ["runtime-dsn", "runtime.sqlite3", "signing"],
                )
            finally:
                engine.dispose()

    def test_owner_control_sqlite_orm_read_does_not_register_raw_pragma_hook(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            secret = _private_file(directory / "signing", SECRET)
            dsn_file, setup_engine = _active_owner_control_database(directory)
            setup_engine.dispose()
            stdout = io.StringIO()
            statements: list[str] = []
            connect = sqlite3.dbapi2.connect

            def traced_connect(*arguments, **keywords):
                connection = connect(*arguments, **keywords)
                connection.set_trace_callback(statements.append)
                return connection

            with (
                mock.patch.object(
                    sqlite3.dbapi2,
                    "connect",
                    side_effect=traced_connect,
                ),
                mock.patch(
                    "hermes_cloud.platform.sqlite.engine."
                    "_configure_sqlite_pragma_policy",
                    side_effect=AssertionError("raw PRAGMA hook is forbidden"),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                token_mint.main(
                    [],
                    environment=_owner_control_environment(secret, dsn_file),
                    utc_now=lambda: NOW,
                )

            self.assertEqual(stdout.getvalue(), "mint_mode=plan ttl_seconds=300\n")
            self.assertFalse(
                any(
                    statement.lstrip().upper().startswith("PRAGMA")
                    for statement in statements
                ),
                statements,
            )

    def test_postgresql_owner_control_uses_psycopg3_without_psycopg2(self) -> None:
        with mock.patch.dict(sys.modules, {"psycopg2": None}):
            engine = token_mint._build_database_engine(
                "postgresql://owner:secret@database.example/hermes_cloud"
            )
        try:
            self.assertEqual(engine.url.drivername, "postgresql+psycopg")
            self.assertEqual(engine.dialect.driver, "psycopg")
        finally:
            engine.dispose()

    def test_postgresql_owner_control_forces_read_only_transactions(self) -> None:
        sentinel_engine = mock.Mock()
        with mock.patch.object(
            token_mint,
            "create_engine",
            return_value=sentinel_engine,
        ) as create:
            result = token_mint._build_database_engine(
                "postgresql://owner:secret@database.example/hermes_cloud"
            )

        self.assertIs(result, sentinel_engine)
        database_url = create.call_args.args[0]
        self.assertEqual(database_url.drivername, "postgresql+psycopg")
        self.assertEqual(
            create.call_args.kwargs["connect_args"],
            {"options": "-c default_transaction_read_only=on"},
        )
        self.assertEqual(
            create.call_args.kwargs["execution_options"],
            {"postgresql_readonly": True},
        )
        self.assertIs(create.call_args.kwargs["poolclass"], token_mint.NullPool)

    def test_default_dry_run_does_not_mint_write_or_emit_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            secret = _private_file(directory / "signing", SECRET)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    token_mint.jwt,
                    "encode",
                    side_effect=AssertionError("dry run must not mint"),
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                token_mint.main(
                    [],
                    environment=_environment(secret),
                    utc_now=lambda: NOW,
                )

            self.assertEqual(
                stdout.getvalue(),
                "mint_mode=plan ttl_seconds=300\n",
            )
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                tuple(path.name for path in directory.iterdir()),
                ("signing",),
            )
            self.assertNotIn("eyJ", stdout.getvalue())

    def test_apply_atomically_writes_private_exact_claim_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            secret = _private_file(directory / "signing", SECRET)
            output = directory / "connector.token"
            output.write_text("old-token")
            output.chmod(0o600)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                token_mint.main(
                    ["--apply", "--output", str(output)],
                    environment=_environment(secret),
                    utc_now=lambda: NOW,
                )

            encoded = output.read_text().strip()
            claims = jwt.decode(
                encoded,
                SECRET,
                algorithms=["HS256"],
                options={
                    "require": ["exp", "iat", "nbf"],
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            self.assertEqual(
                set(claims),
                {
                    "tenant_id",
                    "device_id",
                    "scope",
                    "iat",
                    "nbf",
                    "exp",
                },
            )
            self.assertEqual(claims["tenant_id"], str(TENANT_ID))
            self.assertEqual(claims["device_id"], str(DEVICE_ID))
            self.assertEqual(claims["scope"], "connector.connect")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(
                stdout.getvalue(),
                "mint_mode=apply token_written=true\n",
            )
            self.assertNotIn(encoded, stdout.getvalue())
            self.assertEqual(
                sorted(path.name for path in directory.iterdir()),
                ["connector.token", "signing"],
            )

    def test_apply_requires_absolute_output_and_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            secret = _private_file(directory / "signing", SECRET)
            target = directory / "target"
            target.write_text("existing")
            target.chmod(0o600)
            symlink = directory / "token-link"
            symlink.symlink_to(target)

            for arguments in (
                ["--apply"],
                ["--apply", "--output", "relative"],
                ["--apply", "--output", str(symlink)],
            ):
                with self.subTest(arguments=arguments):
                    with self.assertRaises(SystemExit) as raised:
                        token_mint.main(
                            arguments,
                            environment=_environment(secret),
                            utc_now=lambda: NOW,
                        )
                    self.assertEqual(
                        str(raised.exception),
                        "mint failed; no token written",
                    )
            self.assertEqual(target.read_text(), "existing")

    def test_invalid_configuration_is_redacted_and_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            secret_value = "secret-material-must-not-leak"
            secret = _private_file(directory / "signing", secret_value)
            output = directory / "connector.token"
            environment = _environment(secret)
            environment["HERMES_CONNECTOR_TOKEN_TTL_SECONDS"] = "3601"

            with self.assertRaises(SystemExit) as raised:
                token_mint.main(
                    ["--apply", "--output", str(output)],
                    environment=environment,
                    utc_now=lambda: NOW,
                )

            self.assertEqual(
                str(raised.exception),
                "mint failed; no token written",
            )
            self.assertNotIn(secret_value, str(raised.exception))
            self.assertFalse(output.exists())

    def test_cli_rejects_sensitive_and_unknown_arguments_without_echoing_values(
        self,
    ) -> None:
        sentinel = "synthetic-secret-must-not-leak"
        cases = (
            ["--password", sentinel],
            [f"--dsn={sentinel}"],
            ["--token", sentinel],
            ["--secret", sentinel],
            ["--credential", sentinel],
            ["--unknown", sentinel],
        )
        expected = (
            "usage: mint_connector_token.py [-h] [--inspect-binding] "
            "[--apply] [--output OUTPUT]\n"
            "mint_connector_token.py: error: invalid arguments\n"
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with (
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    token_mint._arguments(arguments)

                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(stderr.getvalue(), expected)
                self.assertNotIn(sentinel, stderr.getvalue())

    def test_cli_help_preserves_apply_and_output_surface(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            token_mint._arguments(["--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn(
            "usage: mint_connector_token.py [-h] [--inspect-binding] "
            "[--apply] [--output OUTPUT]",
            stdout.getvalue(),
        )
        self.assertIn("--inspect-binding", stdout.getvalue())
        self.assertIn("--apply", stdout.getvalue())
        self.assertIn("--output OUTPUT", stdout.getvalue())

    def test_runner_uses_only_orm_database_access_and_atomic_replace(
        self,
    ) -> None:
        source = RUNNER.read_text()
        tree = ast.parse(source)
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        imported_modules = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        sql_literals = {
            node.value.strip().split(" ", 1)[0].upper()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        self.assertNotIn("--token", source)
        self.assertNotIn("--secret", source)
        self.assertNotIn("build_sqlite_engine", source)
        self.assertNotIn("PRAGMA", source.upper())
        self.assertNotIn("cursor.execute", source)
        self.assertIn("sqlalchemy", imported_modules)
        self.assertIn("select", imported_names)
        self.assertIn("sessionmaker", imported_names)
        self.assertIn("begin", called_attributes)
        self.assertNotIn("text", imported_names)
        self.assertNotIn("exec_driver_sql", called_attributes)
        self.assertFalse(
            sql_literals.intersection({"SELECT", "INSERT", "UPDATE", "DELETE"})
        )
        self.assertIn("replace", called_attributes)
        self.assertIn("fchmod", called_attributes)


if __name__ == "__main__":
    unittest.main()

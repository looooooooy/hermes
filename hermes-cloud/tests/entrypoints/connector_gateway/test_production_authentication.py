from __future__ import annotations

import asyncio
import base64
import importlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

import jwt
import pytest
from sqlalchemy.engine import Engine

from hermes_cloud.adapters import connector_auth
from hermes_cloud.adapters.connector_auth import (
    ConnectorAuthenticationConfigurationError,
    HmacJwtConnectorAuthenticator,
    build_connector_authenticator,
)
from hermes_cloud.entrypoints.connector_gateway.bootstrap import (
    build_production_connector_gateway_application,
)
from hermes_cloud.platform.sqlalchemy.connector_command_router import (
    SqlAlchemyConnectorCommandRouter,
)
from hermes_cloud.platform.sqlalchemy.connector_transport_cursor import (
    SqlAlchemyConnectorTransportCursorAuthority,
)
from hermes_cloud.platform.sqlalchemy.observer_projection import (
    SqlAlchemyObserverIngress,
)
from hermes_cloud.platform.sqlalchemy.observer_subscription import (
    SqlAlchemyObserverSubscriptionRouter,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

NOW = datetime.now(UTC).replace(microsecond=0)
NOW_SECONDS = int(NOW.timestamp())
SIGNING_SECRET = b"s" * 32
EXACT_CLAIMS = {
    "tenant_id": "33333333-3333-4333-8333-333333333333",
    "device_id": "77777777-7777-4777-8777-777777777777",
    "scope": "connector.connect",
    "iat": NOW_SECONDS,
    "nbf": NOW_SECONDS,
    "exp": NOW_SECONDS + 300,
}
V1_CLAIMS = {
    "tenant_id": "33333333-3333-4333-8333-333333333333",
    "device_id": "77777777-7777-4777-8777-777777777777",
    "credential_id": "88888888-8888-4888-8888-888888888888",
    "agent_id": "66666666-6666-4666-8666-666666666666",
    "scopes": ["session.observe", "session.control.request"],
    "jti": "99999999-9999-4999-8999-999999999999",
    "iat": NOW_SECONDS,
    "nbf": NOW_SECONDS,
    "exp": NOW_SECONDS + 900,
}


def _token(
    claims: dict[str, object] | None = None,
    *,
    secret: bytes = SIGNING_SECRET,
    algorithm: str = "HS256",
    headers: dict[str, object] | None = None,
) -> str:
    return jwt.encode(
        claims or EXACT_CLAIMS,
        secret,
        algorithm=algorithm,
        headers=headers,
    )


def _private_file(path: Path, value: bytes, *, mode: int = 0o600) -> str:
    path.write_bytes(value)
    path.chmod(mode)
    return str(path)


def _runtime_dsn_file(tmp_path: Path) -> str:
    database = tmp_path / "runtime.sqlite3"
    database_url = f"sqlite+pysqlite:///{database}"
    engine = build_sqlite_engine(database_url, allow_missing=True)
    try:
        build_sqlite_metadata().create_all(engine)
    finally:
        engine.dispose()
    database.chmod(0o660)
    return _private_file(
        tmp_path / "runtime-dsn",
        database_url.encode("utf-8"),
    )


def _observer_keyring_file(tmp_path: Path) -> str:
    return _private_file(
        tmp_path / "observer-keyring.json",
        json.dumps(
            {
                "version": 1,
                "tenants": {
                    str(EXACT_CLAIMS["tenant_id"]): {
                        "current": "test-v1",
                        "keys": {
                            "test-v1": base64.b64encode(b"k" * 32).decode("ascii")
                        },
                    }
                },
            }
        ).encode("utf-8"),
    )


def _hello() -> str:
    return json.dumps(
        {
            "contract_version": 1,
            "message_id": "22222222-2222-4222-8222-222222222222",
            "message_type": "connector.hello",
            "tenant_id": EXACT_CLAIMS["tenant_id"],
            "device_id": EXACT_CLAIMS["device_id"],
            "sequence": 0,
            "sent_at": "2026-07-31T12:00:00Z",
            "payload": {
                "connector_instance_id": ("11111111-1111-4111-8111-111111111111"),
                "connector_version": "1.0.0",
                "runtime_generation": "runtime-test",
                "required_capabilities": ["session.observe"],
                "optional_capabilities": [],
                "resume": {
                    "mode": "fresh",
                    "next_outbound_sequence": 0,
                    "next_inbound_sequence": 0,
                },
            },
        },
        separators=(",", ":"),
    )


async def _websocket_exchange(
    app: Any,
    bearer_token: str,
) -> list[dict[str, Any]]:
    incoming = iter(
        (
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": _hello()},
            {"type": "websocket.disconnect", "code": 1000},
        )
    )
    outgoing: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(incoming)

    async def send(message: dict[str, Any]) -> None:
        outgoing.append(message)

    await app(
        {
            "type": "websocket",
            "path": "/api/ws",
            "headers": [(b"authorization", f"Bearer {bearer_token}".encode())],
            "subprotocols": ["hermes.connector.v1"],
        },
        receive,
        send,
    )
    return outgoing


def test_hmac_jwt_authenticator_accepts_only_exact_connector_claims() -> None:
    class Authority:
        def __init__(self) -> None:
            self.calls = 0

        def active_legacy_device_binding(self, **_arguments: object) -> object:
            self.calls += 1
            return type(
                "Snapshot",
                (),
                {
                    "binding": type(
                        "Binding",
                        (),
                        {
                            "tenant_id": UUID(EXACT_CLAIMS["tenant_id"]),
                            "device_id": UUID(EXACT_CLAIMS["device_id"]),
                            "credential_id": UUID(V1_CLAIMS["credential_id"]),
                            "agent_id": UUID(V1_CLAIMS["agent_id"]),
                            "scopes": (
                                "session.observe",
                                "session.control.request",
                            ),
                        },
                    )()
                },
            )()

    async def scenario() -> None:
        authority = Authority()
        authenticator = HmacJwtConnectorAuthenticator(
            SIGNING_SECRET,
            utc_now=lambda: NOW,
            device_authority=authority,
        )

        identity = await authenticator.authenticate(_token())
        await authenticator.revalidate(identity)
        await authenticator.check()

        assert identity.tenant_id == EXACT_CLAIMS["tenant_id"]
        assert identity.device_id == EXACT_CLAIMS["device_id"]
        assert identity.credential_id == V1_CLAIMS["credential_id"]
        assert identity.agent_id == V1_CLAIMS["agent_id"]
        assert identity.scopes == ("session.observe",)
        assert identity.legacy_seed is True
        assert authority.calls == 2
        assert authenticator.name == "connector-authentication"
        assert authenticator.critical is True
        assert authenticator.deadline_seconds > 0
        assert "ssss" not in repr(authenticator)

    asyncio.run(scenario())


def test_connector_token_expiry_is_enforced_by_jwt_runtime() -> None:
    expired_now = datetime(2020, 1, 1, tzinfo=UTC)
    expired_seconds = int(expired_now.timestamp())
    expired_token = _token(
        {
            **EXACT_CLAIMS,
            "iat": expired_seconds,
            "nbf": expired_seconds,
            "exp": expired_seconds + 300,
        }
    )

    async def scenario() -> None:
        authenticator = HmacJwtConnectorAuthenticator(
            SIGNING_SECRET,
            utc_now=lambda: expired_now,
        )

        with pytest.raises(PermissionError, match="connector token rejected"):
            await authenticator.authenticate(expired_token)

    asyncio.run(scenario())


def test_legacy_seed_token_requires_authoritative_orm_binding() -> None:
    async def scenario() -> None:
        authenticator = HmacJwtConnectorAuthenticator(
            SIGNING_SECRET,
            utc_now=lambda: NOW,
        )

        with pytest.raises(PermissionError, match="connector token rejected"):
            await authenticator.authenticate(_token())

    asyncio.run(scenario())


def test_v1_connector_token_requires_matching_authoritative_binding() -> None:
    class Authority:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def active_device_binding(self, **arguments: object) -> object:
            self.calls.append(arguments)
            return type(
                "Snapshot",
                (),
                {
                    "binding": type(
                        "Binding",
                        (),
                        {
                            "tenant_id": UUID(V1_CLAIMS["tenant_id"]),
                            "device_id": UUID(V1_CLAIMS["device_id"]),
                            "credential_id": UUID(V1_CLAIMS["credential_id"]),
                            "agent_id": UUID(V1_CLAIMS["agent_id"]),
                            "scopes": tuple(V1_CLAIMS["scopes"]),
                        },
                    )()
                },
            )()

    async def scenario() -> None:
        authority = Authority()
        authenticator = HmacJwtConnectorAuthenticator(
            SIGNING_SECRET,
            utc_now=lambda: NOW,
            device_authority=authority,
        )
        identity = await authenticator.authenticate(_token(V1_CLAIMS))

        assert identity.credential_id == V1_CLAIMS["credential_id"]
        assert identity.agent_id == V1_CLAIMS["agent_id"]
        assert identity.scopes == tuple(V1_CLAIMS["scopes"])
        assert identity.legacy_seed is False
        assert len(authority.calls) == 1

        await authenticator.revalidate(identity)
        assert len(authority.calls) == 2

    asyncio.run(scenario())


def test_connected_identity_expires_on_revalidation_with_injected_clock() -> None:
    class Authority:
        def __init__(self) -> None:
            self.calls = 0

        def active_device_binding(self, **_arguments: object) -> object:
            self.calls += 1
            return type(
                "Snapshot",
                (),
                {
                    "binding": type(
                        "Binding",
                        (),
                        {
                            "tenant_id": UUID(V1_CLAIMS["tenant_id"]),
                            "device_id": UUID(V1_CLAIMS["device_id"]),
                            "credential_id": UUID(V1_CLAIMS["credential_id"]),
                            "agent_id": UUID(V1_CLAIMS["agent_id"]),
                            "scopes": tuple(V1_CLAIMS["scopes"]),
                        },
                    )()
                },
            )()

    async def scenario() -> None:
        authority = Authority()
        clock = [NOW]
        authenticator = HmacJwtConnectorAuthenticator(
            SIGNING_SECRET,
            utc_now=lambda: clock[0],
            device_authority=authority,
        )
        identity = await authenticator.authenticate(_token(V1_CLAIMS))

        assert identity.token_issued_at == V1_CLAIMS["iat"]
        assert identity.token_not_before == V1_CLAIMS["nbf"]
        assert identity.token_expires_at == V1_CLAIMS["exp"]
        clock[0] = datetime.fromtimestamp(V1_CLAIMS["exp"], tz=UTC)
        with pytest.raises(PermissionError, match="expired"):
            await authenticator.revalidate(identity)
        assert authority.calls == 1

    asyncio.run(scenario())


def test_sync_orm_authority_does_not_block_event_loop() -> None:
    class BlockingAuthority:
        def active_device_binding(self, **_arguments: object) -> object:
            time.sleep(0.2)
            return type(
                "Snapshot",
                (),
                {
                    "binding": type(
                        "Binding",
                        (),
                        {
                            "tenant_id": UUID(V1_CLAIMS["tenant_id"]),
                            "device_id": UUID(V1_CLAIMS["device_id"]),
                            "credential_id": UUID(V1_CLAIMS["credential_id"]),
                            "agent_id": UUID(V1_CLAIMS["agent_id"]),
                            "scopes": tuple(V1_CLAIMS["scopes"]),
                        },
                    )()
                },
            )()

    async def scenario() -> None:
        authenticator = HmacJwtConnectorAuthenticator(
            SIGNING_SECRET,
            utc_now=lambda: NOW,
            device_authority=BlockingAuthority(),
        )
        started = time.monotonic()
        authentication = asyncio.create_task(
            authenticator.authenticate(_token(V1_CLAIMS))
        )
        await asyncio.sleep(0.01)
        assert time.monotonic() - started < 0.1
        await authentication

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "encoded",
    (
        _token({**EXACT_CLAIMS, "unknown": "rejected"}),
        _token({**EXACT_CLAIMS, "scope": "session.observe"}),
        _token({**EXACT_CLAIMS, "exp": NOW_SECONDS}),
        _token(
            {
                **EXACT_CLAIMS,
                "iat": NOW_SECONDS + 1,
                "nbf": NOW_SECONDS + 1,
            }
        ),
        _token({**EXACT_CLAIMS, "exp": NOW_SECONDS + 3_601}),
        _token({**EXACT_CLAIMS, "iat": True}),
        _token({**EXACT_CLAIMS, "tenant_id": ""}),
        _token({**EXACT_CLAIMS, "tenant_id": "tenant id"}),
        _token({**EXACT_CLAIMS, "device_id": "d" * 129}),
        _token(secret=b"s" * 48, algorithm="HS384"),
        _token(secret=b"w" * 32),
        _token(headers={"kid": "unknown-key"}),
    ),
)
def test_hmac_jwt_authenticator_fails_closed_for_invalid_tokens(
    encoded: str,
) -> None:
    async def scenario() -> None:
        authenticator = HmacJwtConnectorAuthenticator(
            SIGNING_SECRET,
            utc_now=lambda: NOW,
        )

        with pytest.raises(PermissionError, match="connector token rejected"):
            await authenticator.authenticate(encoded)

    asyncio.run(scenario())


def test_connector_signing_secret_file_is_strict_and_redacted(
    tmp_path: Path,
) -> None:
    valid = _private_file(tmp_path / "valid", SIGNING_SECRET)
    authenticator = build_connector_authenticator(
        {"HERMES_CONNECTOR_SIGNING_SECRET_FILE": valid},
        utc_now=lambda: NOW,
    )
    assert isinstance(authenticator, HmacJwtConnectorAuthenticator)

    sentinel = b"secret-material-must-not-leak"
    empty = _private_file(tmp_path / "empty", b"")
    short = _private_file(tmp_path / "short", b"x" * 31)
    long = _private_file(tmp_path / "long", b"x" * 4_097)
    wide = _private_file(tmp_path / "wide", sentinel, mode=0o640)
    symlink = tmp_path / "symlink"
    symlink.symlink_to(valid)
    cases = (
        {},
        {"HERMES_CONNECTOR_SIGNING_SECRET_FILE": "relative"},
        {"HERMES_CONNECTOR_SIGNING_SECRET_FILE": empty},
        {"HERMES_CONNECTOR_SIGNING_SECRET_FILE": short},
        {"HERMES_CONNECTOR_SIGNING_SECRET_FILE": long},
        {"HERMES_CONNECTOR_SIGNING_SECRET_FILE": wide},
        {"HERMES_CONNECTOR_SIGNING_SECRET_FILE": str(symlink)},
    )
    for environment in cases:
        with pytest.raises(ConnectorAuthenticationConfigurationError) as raised:
            build_connector_authenticator(
                environment,
                utc_now=lambda: NOW,
            )
        assert sentinel.decode() not in str(raised.value)


def test_production_composition_is_unready_without_valid_secret(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = build_production_connector_gateway_application(environment={})
        await app.startup()
        assert app.snapshot()["ready"] is False
        await app.shutdown()

        short = _private_file(tmp_path / "short", b"x" * 31)
        invalid_app = build_production_connector_gateway_application(
            environment={"HERMES_CONNECTOR_SIGNING_SECRET_FILE": short}
        )
        await invalid_app.startup()
        assert invalid_app.snapshot()["ready"] is False
        await invalid_app.shutdown()

    asyncio.run(scenario())


def test_production_composition_is_unready_without_runtime_dsn(
    tmp_path: Path,
) -> None:
    secret_file = _private_file(
        tmp_path / "connector-signing",
        SIGNING_SECRET,
    )

    async def scenario() -> None:
        app = build_production_connector_gateway_application(
            environment={
                "HERMES_CONNECTOR_SIGNING_SECRET_FILE": secret_file,
            },
        )
        await app.startup()
        assert app.snapshot()["ready"] is False
        await app.shutdown()

    asyncio.run(scenario())


def test_production_composition_is_unready_without_observer_keyring(
    tmp_path: Path,
) -> None:
    secret_file = _private_file(
        tmp_path / "connector-signing",
        SIGNING_SECRET,
    )
    runtime_dsn_file = _runtime_dsn_file(tmp_path)

    async def scenario() -> None:
        app = build_production_connector_gateway_application(
            environment={
                "HERMES_CONNECTOR_SIGNING_SECRET_FILE": secret_file,
                "HERMES_RUNTIME_DSN_FILE": runtime_dsn_file,
            },
        )
        await app.startup()
        assert app.snapshot()["ready"] is False
        await app.shutdown()

    asyncio.run(scenario())


def test_production_composition_is_unready_without_jwt_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_file = _private_file(
        tmp_path / "connector-signing",
        SIGNING_SECRET,
    )

    def unavailable_jwt_runtime() -> object:
        raise ConnectorAuthenticationConfigurationError(
            "connector JWT runtime is unavailable"
        )

    monkeypatch.setattr(
        connector_auth,
        "_load_jwt_runtime",
        unavailable_jwt_runtime,
    )

    async def scenario() -> None:
        app = build_production_connector_gateway_application(
            environment={
                "HERMES_CONNECTOR_SIGNING_SECRET_FILE": secret_file,
            },
        )
        await app.startup()
        assert app.snapshot()["ready"] is False
        await app.shutdown()

    asyncio.run(scenario())


def test_connector_authenticator_rejects_a_broken_jwt_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connector_auth, "import_module", lambda _name: object())

    with pytest.raises(
        ConnectorAuthenticationConfigurationError,
        match="JWT runtime is unavailable",
    ):
        HmacJwtConnectorAuthenticator(SIGNING_SECRET)


def test_production_composition_rejects_unbound_legacy_seed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        secret_file = _private_file(
            tmp_path / "connector-signing",
            SIGNING_SECRET,
        )
        runtime_dsn_file = _runtime_dsn_file(tmp_path)
        observer_keyring_file = _observer_keyring_file(tmp_path)
        app = build_production_connector_gateway_application(
            environment={
                "HERMES_CONNECTOR_SIGNING_SECRET_FILE": secret_file,
                "HERMES_RUNTIME_DSN_FILE": runtime_dsn_file,
                "HERMES_OBSERVER_KEYRING_FILE": observer_keyring_file,
            },
            utc_now=lambda: NOW,
        )
        await app.startup()

        outgoing = await _websocket_exchange(app, _token())

        assert app.snapshot()["ready"] is True
        assert outgoing == [
            {
                "type": "websocket.close",
                "code": 1008,
                "reason": "authentication_failed",
            }
        ]
        assert isinstance(
            app._gateway_service._command_router,
            SqlAlchemyConnectorCommandRouter,
        )
        assert isinstance(
            app._gateway_service._observer_ingress,
            SqlAlchemyObserverIngress,
        )
        assert isinstance(
            app._gateway_service._observer_subscription_router,
            SqlAlchemyObserverSubscriptionRouter,
        )
        assert isinstance(
            app._gateway_service._transport_cursor_authority,
            SqlAlchemyConnectorTransportCursorAuthority,
        )
        assert (
            app._gateway_service._resume_resolver
            is app._gateway_service._transport_cursor_authority
        )
        assert app._gateway_service._settings.available_capabilities == (
            "session.catalog.v1",
            "session.observe",
            "session.observe.output-parity.v1",
        )
        await app.shutdown()

    asyncio.run(scenario())


def test_sqlite_production_advertises_control_only_with_live_private_bridge(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        secret_file = _private_file(
            tmp_path / "connector-signing",
            SIGNING_SECRET,
        )
        runtime_dsn_file = _runtime_dsn_file(tmp_path)
        observer_keyring_file = _observer_keyring_file(tmp_path)
        with TemporaryDirectory(prefix="hc-", dir="/tmp") as temporary:
            runtime_directory = Path(temporary)
            runtime_directory.chmod(0o700)
            socket_path = runtime_directory / "owner-control.sock"
            app = build_production_connector_gateway_application(
                environment={
                    "HERMES_CONNECTOR_SIGNING_SECRET_FILE": secret_file,
                    "HERMES_RUNTIME_DSN_FILE": runtime_dsn_file,
                    "HERMES_OBSERVER_KEYRING_FILE": observer_keyring_file,
                    "HERMES_OWNER_CONTROL_SOCKET": str(socket_path),
                },
                utc_now=lambda: NOW,
            )

            await app.startup()
            try:
                assert app.snapshot()["ready"] is True
                assert app._gateway_service._settings.available_capabilities == (
                    "session.catalog.v1",
                    "session.observe",
                    "session.observe.output-parity.v1",
                    "session.control",
                )
                assert socket_path.stat().st_mode & 0o777 == 0o600
            finally:
                await app.shutdown()

            assert not socket_path.exists()

    asyncio.run(scenario())


def test_production_composition_disposes_runtime_engine_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_file = _private_file(
        tmp_path / "connector-signing",
        SIGNING_SECRET,
    )
    runtime_dsn_file = _runtime_dsn_file(tmp_path)
    observer_keyring_file = _observer_keyring_file(tmp_path)
    disposed: list[Engine] = []
    original_dispose = Engine.dispose

    def track_dispose(engine: Engine, *args: object, **kwargs: object) -> None:
        disposed.append(engine)
        original_dispose(engine, *args, **kwargs)

    monkeypatch.setattr(Engine, "dispose", track_dispose)

    async def scenario() -> None:
        app = build_production_connector_gateway_application(
            environment={
                "HERMES_CONNECTOR_SIGNING_SECRET_FILE": secret_file,
                "HERMES_RUNTIME_DSN_FILE": runtime_dsn_file,
                "HERMES_OBSERVER_KEYRING_FILE": observer_keyring_file,
            },
        )
        await app.startup()
        assert app.snapshot()["ready"] is True
        await app.shutdown()

    asyncio.run(scenario())
    assert len(disposed) == 1


def test_bootstrap_global_app_reads_connector_secret_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_file = _private_file(
        tmp_path / "connector-signing",
        SIGNING_SECRET,
    )
    runtime_dsn_file = _runtime_dsn_file(tmp_path)
    observer_keyring_file = _observer_keyring_file(tmp_path)
    monkeypatch.setenv(
        "HERMES_CONNECTOR_SIGNING_SECRET_FILE",
        secret_file,
    )
    monkeypatch.setenv(
        "HERMES_RUNTIME_DSN_FILE",
        runtime_dsn_file,
    )
    monkeypatch.setenv(
        "HERMES_OBSERVER_KEYRING_FILE",
        observer_keyring_file,
    )
    bootstrap = importlib.import_module(
        "hermes_cloud.entrypoints.connector_gateway.bootstrap"
    )
    reloaded = importlib.reload(bootstrap)

    async def scenario() -> None:
        await reloaded.app.startup()
        assert reloaded.app.snapshot()["ready"] is True
        await reloaded.app.shutdown()

    try:
        asyncio.run(scenario())
    finally:
        monkeypatch.delenv(
            "HERMES_CONNECTOR_SIGNING_SECRET_FILE",
            raising=False,
        )
        monkeypatch.delenv(
            "HERMES_RUNTIME_DSN_FILE",
            raising=False,
        )
        monkeypatch.delenv(
            "HERMES_OBSERVER_KEYRING_FILE",
            raising=False,
        )
        importlib.reload(reloaded)

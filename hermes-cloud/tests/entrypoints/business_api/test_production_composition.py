from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.selectable import Select

from hermes_cloud.adapters import business_api_runtime
from hermes_cloud.adapters.business_api_runtime import (
    build_production_business_api_application,
)
from hermes_cloud.entrypoints.business_api import create_app
from hermes_cloud.platform.sqlalchemy.observer_projection import (
    ObserverProjectionEventSource,
    SqlAlchemyObserverProjectionRepository,
)
from hermes_cloud.platform.sqlalchemy.observer_subscription import (
    SqlAlchemyObserverSubscriptionRouter,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata


def test_default_bootstrap_fails_closed_without_runtime_credentials() -> None:
    application = create_app(environment={})

    with TestClient(application) as client:
        readiness = client.get("/ready")
        liveness = client.get("/live")

    assert liveness.status_code == 200
    assert readiness.status_code == 503
    snapshot = readiness.json()
    assert snapshot["state"] == "READY"
    assert snapshot["diagnostic"] == "BLOCKED"
    assert snapshot["ready"] is False
    assert snapshot["diagnostic"] == "BLOCKED"
    assert snapshot["dependencies"] == [
        {
            "criticality": "CRITICAL",
            "error": {
                "category": "DEPENDENCY",
                "code": "DEPENDENCY_UNAVAILABLE",
                "retryable": True,
            },
            "name": "business-api-runtime-configuration",
            "status": "FAILED",
        }
    ]
    assert "dsn" not in readiness.text.lower()
    assert "secret" not in readiness.text.lower()


class _ProbeResult:
    def first(self) -> None:
        return None


class _ProbeSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    def execute(self, statement: object) -> _ProbeResult:
        self.statements.append(statement)
        return _ProbeResult()


class _SessionContext(AbstractContextManager[_ProbeSession]):
    def __init__(
        self,
        factory: _SessionFactory,
        session: _ProbeSession,
    ) -> None:
        self._factory = factory
        self._session = session

    def __enter__(self) -> _ProbeSession:
        return self._session

    def __exit__(self, *args: object) -> None:
        self._factory.closes += 1


class _SessionFactory:
    def __init__(self) -> None:
        self.sessions: list[_ProbeSession] = []
        self.closes = 0

    def begin(self) -> _SessionContext:
        session = _ProbeSession()
        self.sessions.append(session)
        return _SessionContext(self, session)


class _Engine:
    def __init__(self) -> None:
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1


def _private_file(path: Path, value: str) -> str:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def _observer_keyring_file(path: Path) -> str:
    return _private_file(
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


def test_complete_runtime_composition_probes_orm_and_disposes_engine(
    tmp_path: Path,
) -> None:
    engine = _Engine()
    session_factory = _SessionFactory()
    captured: dict[str, object] = {}

    def engine_factory(database_url: str, **options: object) -> _Engine:
        captured["database_url"] = database_url
        captured["engine_options"] = options
        return engine

    def session_factory_builder(
        *,
        bind: object,
        expire_on_commit: bool,
    ) -> _SessionFactory:
        captured["session_bind"] = bind
        captured["expire_on_commit"] = expire_on_commit
        return session_factory

    environment = {
        "HERMES_RUNTIME_DSN_FILE": _private_file(
            tmp_path / "database-dsn",
            "postgresql+psycopg://runtime.invalid/hermes",
        ),
        "HERMES_SIGNING_SECRET_FILE": _private_file(
            tmp_path / "signing-secret",
            "s" * 48,
        ),
    }

    application = build_production_business_api_application(
        environment=environment,
        engine_factory=engine_factory,
        session_factory_builder=session_factory_builder,
    )

    with TestClient(application) as client:
        readiness = client.get("/ready")
        status = client.get("/api/status")

    assert readiness.status_code == 200
    assert readiness.json()["dependencies"] == [
        {
            "criticality": "CRITICAL",
            "error": None,
            "name": "postgresql",
            "status": "HEALTHY",
        }
    ]
    assert status.json()["gateway_state"] == "ready"
    assert status.json()["auth_required"] is True
    assert status.json()["overall"] == "healthy"
    route_paths = {route.path for route in application.routes}
    assert "/auth/password-login" in route_paths
    assert "/auth/native/refresh" in route_paths
    assert isinstance(session_factory.sessions[0].statements[0], Select)
    assert session_factory.closes == 1
    assert captured["session_bind"] is engine
    assert captured["expire_on_commit"] is False
    assert captured["engine_options"] == {
        "pool_pre_ping": True,
    }
    assert engine.dispose_calls == 1
    assert "runtime.invalid" not in readiness.text
    assert "s" * 48 not in readiness.text


def test_short_signing_credential_fails_closed_without_creating_engine(
    tmp_path: Path,
) -> None:
    engine_calls = 0

    def engine_factory(*_args: object, **_kwargs: object) -> Any:
        nonlocal engine_calls
        engine_calls += 1
        raise AssertionError("engine must not be created")

    application = build_production_business_api_application(
        environment={
            "HERMES_RUNTIME_DSN_FILE": _private_file(
                tmp_path / "database-dsn",
                "postgresql+psycopg://runtime.invalid/hermes",
            ),
            "HERMES_SIGNING_SECRET_FILE": _private_file(
                tmp_path / "signing-secret",
                "too-short",
            ),
        },
        engine_factory=engine_factory,
    )

    with TestClient(application) as client:
        readiness = client.get("/ready")

    assert readiness.status_code == 503
    assert readiness.json()["dependencies"][0]["name"] == (
        "business-api-runtime-configuration"
    )
    assert engine_calls == 0
    assert "too-short" not in readiness.text


def test_invalid_database_url_fails_closed_instead_of_crashing_import(
    tmp_path: Path,
) -> None:
    application = build_production_business_api_application(
        environment={
            "HERMES_RUNTIME_DSN_FILE": _private_file(
                tmp_path / "database-dsn",
                "not-a-database-url",
            ),
            "HERMES_SIGNING_SECRET_FILE": _private_file(
                tmp_path / "signing-secret",
                "s" * 48,
            ),
        },
    )

    with TestClient(application) as client:
        readiness = client.get("/ready")

    assert readiness.status_code == 503
    assert readiness.json()["dependencies"][0]["name"] == (
        "business-api-runtime-configuration"
    )
    assert "not-a-database-url" not in readiness.text


def test_unknown_database_scheme_fails_closed_without_creating_engine(
    tmp_path: Path,
) -> None:
    engine_calls = 0

    def engine_factory(*_args: object, **_kwargs: object) -> Any:
        nonlocal engine_calls
        engine_calls += 1
        raise AssertionError("unknown providers must not create an engine")

    application = build_production_business_api_application(
        environment={
            "HERMES_RUNTIME_DSN_FILE": _private_file(
                tmp_path / "database-dsn",
                "mysql+pymysql://runtime.invalid/hermes",
            ),
            "HERMES_SIGNING_SECRET_FILE": _private_file(
                tmp_path / "signing-secret",
                "s" * 48,
            ),
        },
        engine_factory=engine_factory,
    )

    with TestClient(application) as client:
        readiness = client.get("/ready")

    assert readiness.status_code == 503
    assert readiness.json()["dependencies"][0]["name"] == (
        "business-api-runtime-configuration"
    )
    assert engine_calls == 0
    assert "mysql" not in readiness.text.lower()


@pytest.mark.parametrize("drivername", ("sqlite", "sqlite+pysqlite"))
def test_sqlite_runtime_composition_probes_real_file_and_disposes_engine(
    tmp_path: Path,
    drivername: str,
) -> None:
    database = tmp_path / f"{drivername.replace('+', '-')}.sqlite3"
    database_url = f"{drivername}:///{database}"
    migration_engine = build_sqlite_engine(database_url, allow_missing=True)
    try:
        build_sqlite_metadata().create_all(migration_engine)
    finally:
        migration_engine.dispose()
    database.chmod(0o660)
    application = build_production_business_api_application(
        environment={
            "HERMES_RUNTIME_DSN_FILE": _private_file(
                tmp_path / f"{drivername}-database-dsn",
                database_url,
            ),
            "HERMES_SIGNING_SECRET_FILE": _private_file(
                tmp_path / f"{drivername}-signing-secret",
                "s" * 48,
            ),
            "HERMES_OBSERVER_KEYRING_FILE": _observer_keyring_file(
                tmp_path / f"{drivername}-observer-keyring"
            ),
        },
    )

    with TestClient(application) as client:
        readiness = client.get("/ready")
        status = client.get("/api/status")

    assert readiness.status_code == 200
    assert readiness.json()["dependencies"] == [
        {
            "criticality": "CRITICAL",
            "error": None,
            "name": "sqlite",
            "status": "HEALTHY",
        }
    ]
    assert status.status_code == 200
    assert status.json()["gateway_state"] == "ready"

    reopened = build_sqlite_engine(database_url)
    try:
        with reopened.connect():
            pass
    finally:
        reopened.dispose()


def test_sqlite_runtime_fails_closed_without_observer_keyring(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing-keyring.sqlite3"
    database_url = f"sqlite+pysqlite:///{database}"
    migration_engine = build_sqlite_engine(database_url, allow_missing=True)
    try:
        build_sqlite_metadata().create_all(migration_engine)
    finally:
        migration_engine.dispose()
    database.chmod(0o660)

    application = build_production_business_api_application(
        environment={
            "HERMES_RUNTIME_DSN_FILE": _private_file(
                tmp_path / "missing-keyring-dsn",
                database_url,
            ),
            "HERMES_SIGNING_SECRET_FILE": _private_file(
                tmp_path / "missing-keyring-signing",
                "s" * 48,
            ),
        },
    )

    with TestClient(application) as client:
        readiness = client.get("/ready")

    assert readiness.status_code == 503
    assert "key" not in readiness.text.casefold()


def test_sqlite_runtime_composes_authoritative_observer_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "observer-runtime.sqlite3"
    database_url = f"sqlite+pysqlite:///{database}"
    migration_engine = build_sqlite_engine(database_url, allow_missing=True)
    try:
        build_sqlite_metadata().create_all(migration_engine)
    finally:
        migration_engine.dispose()
    database.chmod(0o660)
    captured: dict[str, object] = {}
    original_builder = business_api_runtime.build_business_api_application

    def capture(*args: object, **kwargs: object):
        captured.update(kwargs)
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(
        business_api_runtime,
        "build_business_api_application",
        capture,
    )
    application = build_production_business_api_application(
        environment={
            "HERMES_RUNTIME_DSN_FILE": _private_file(
                tmp_path / "observer-database-dsn",
                database_url,
            ),
            "HERMES_SIGNING_SECRET_FILE": _private_file(
                tmp_path / "observer-signing-secret",
                "s" * 48,
            ),
            "HERMES_OBSERVER_KEYRING_FILE": _observer_keyring_file(
                tmp_path / "observer-keyring"
            ),
        },
    )

    assert isinstance(
        captured.get("observer_projection_repository"),
        SqlAlchemyObserverProjectionRepository,
    )
    assert isinstance(
        captured.get("projection_event_source"),
        ObserverProjectionEventSource,
    )
    assert isinstance(
        captured.get("observer_subscription_manager"),
        SqlAlchemyObserverSubscriptionRouter,
    )

    with TestClient(application) as client:
        assert client.get("/ready").status_code == 200


def test_sqlite_runtime_composes_device_pairing_with_connector_signing_secret(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pairing-runtime.sqlite3"
    database_url = f"sqlite+pysqlite:///{database}"
    migration_engine = build_sqlite_engine(database_url, allow_missing=True)
    try:
        build_sqlite_metadata().create_all(migration_engine)
    finally:
        migration_engine.dispose()
    database.chmod(0o660)
    application = build_production_business_api_application(
        environment={
            "HERMES_RUNTIME_DSN_FILE": _private_file(
                tmp_path / "database-dsn",
                database_url,
            ),
            "HERMES_SIGNING_SECRET_FILE": _private_file(
                tmp_path / "business-signing-secret",
                "b" * 48,
            ),
            "HERMES_CONNECTOR_SIGNING_SECRET_FILE": _private_file(
                tmp_path / "connector-signing-secret",
                "c" * 48,
            ),
            "HERMES_OBSERVER_KEYRING_FILE": _observer_keyring_file(
                tmp_path / "pairing-observer-keyring"
            ),
        },
    )

    with TestClient(application) as client:
        assert client.get("/ready").status_code == 200

    paths = {route.path for route in application.routes}
    assert "/api/device-pairing/offers" in paths
    assert "/api/device-auth/tokens" in paths
    assert "/api/devices/{device_id}/revoke" in paths


def test_late_composition_failure_disposes_engine_and_returns_safe_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine()
    session_factory = _SessionFactory()
    sensitive_failure = "postgresql://runtime-secret@database.invalid/hermes"
    original_builder = business_api_runtime.build_business_api_application
    build_calls = 0

    def fail_once(*args: object, **kwargs: object) -> object:
        nonlocal build_calls
        build_calls += 1
        if build_calls == 1:
            raise RuntimeError(sensitive_failure)
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(
        business_api_runtime,
        "build_business_api_application",
        fail_once,
    )
    application = business_api_runtime.build_production_business_api_application(
        environment={
            "HERMES_RUNTIME_DSN_FILE": _private_file(
                tmp_path / "database-dsn",
                "postgresql+psycopg://runtime.invalid/hermes",
            ),
            "HERMES_SIGNING_SECRET_FILE": _private_file(
                tmp_path / "signing-secret",
                "s" * 48,
            ),
        },
        engine_factory=lambda *_args, **_kwargs: engine,
        session_factory_builder=lambda **_kwargs: session_factory,
    )

    with TestClient(application) as client:
        readiness = client.get("/ready")

    assert readiness.status_code == 503
    assert readiness.json()["dependencies"][0]["name"] == (
        "business-api-runtime-configuration"
    )
    assert engine.dispose_calls == 1
    assert sensitive_failure not in readiness.text


def test_late_composition_cancellation_disposes_engine_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine()
    session_factory = _SessionFactory()

    def cancel_composition(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        business_api_runtime,
        "build_business_api_application",
        cancel_composition,
    )

    with pytest.raises(asyncio.CancelledError):
        business_api_runtime.build_production_business_api_application(
            environment={
                "HERMES_RUNTIME_DSN_FILE": _private_file(
                    tmp_path / "database-dsn",
                    "postgresql+psycopg://runtime.invalid/hermes",
                ),
                "HERMES_SIGNING_SECRET_FILE": _private_file(
                    tmp_path / "signing-secret",
                    "s" * 48,
                ),
            },
            engine_factory=lambda *_args, **_kwargs: engine,
            session_factory_builder=lambda **_kwargs: session_factory,
        )

    assert engine.dispose_calls == 1


def test_bootstrap_global_app_reads_systemd_credentials_at_import(
    tmp_path: Path,
) -> None:
    database_file = _private_file(
        tmp_path / "database-dsn",
        "postgresql+psycopg://runtime.invalid/hermes",
    )
    signing_file = _private_file(
        tmp_path / "signing-secret",
        "s" * 48,
    )
    environment = {
        **os.environ,
        "HERMES_RUNTIME_DSN_FILE": database_file,
        "HERMES_SIGNING_SECRET_FILE": signing_file,
    }
    verification = """
from hermes_cloud.entrypoints.business_api.bootstrap import app

paths = {route.path for route in app.routes}
assert "/auth/password-login" in paths
assert "/auth/native/refresh" in paths
snapshot = app.snapshot()
assert snapshot["component"] == "business-api"
assert snapshot["state"] == "CREATED"
assert snapshot["dependencies"] == []
"""

    result = subprocess.run(
        (sys.executable, "-c", verification),
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "runtime.invalid" not in result.stdout
    assert "s" * 48 not in result.stdout

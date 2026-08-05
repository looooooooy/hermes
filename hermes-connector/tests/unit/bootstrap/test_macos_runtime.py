from __future__ import annotations

import asyncio
import hashlib
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn
from uuid import UUID

import pytest

from hermes_connector.adapters.platform.macos.foundation_projection import (
    FoundationNoOpLocalProjectionInvalidator,
)
from hermes_connector.adapters.platform.macos.pairing_projection import (
    MacOSPairedProjectionStore,
)
from hermes_connector.application.local_gateway_client import LocalRuntimeUnavailable
from hermes_connector.application.observer_intent_lane import ObserverIntentLane
from hermes_connector.application.observer_outbound_lane import ObserverOutboundLane
from hermes_connector.application.readiness_status import ReadinessStatusComponent
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.macos import (
    FileCredentialRuntimeForbidden,
    UnpairedConnector,
    build_macos_runtime,
    check_macos_runtime,
)
from hermes_connector.bootstrap.safe_logging import SafeStructuredLogger
from hermes_connector.bootstrap.settings import load_runtime_settings
from hermes_connector.domain.local_gateway import (
    AgentEndpoint,
    ProcessIdentityEvidence,
)
from hermes_connector.domain.pairing import PairedProjection


def _tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    for path in (root, *sorted(root.rglob("*"))):
        metadata = path.lstat()
        digest = None
        if stat.S_ISREG(metadata.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(
            (
                str(path.relative_to(root)) if path != root else ".",
                stat.S_IFMT(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
                metadata.st_uid,
                metadata.st_size,
                metadata.st_mtime_ns,
                digest,
            )
        )
    return tuple(entries)


def _valid_environment(tmp_path: Path) -> dict[str, str]:
    state_directory = tmp_path / "state"
    role_directories = {
        "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR": tmp_path / "local-registry",
        "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR": tmp_path / "local-sockets",
        "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR": tmp_path / "control-registry",
        "HERMES_CONNECTOR_CONTROL_SOCKET_DIR": tmp_path / "control-sockets",
        "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR": tmp_path / "observer-registry",
        "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR": tmp_path / "observer-sockets",
    }
    for directory in (state_directory, *role_directories.values()):
        directory.mkdir(mode=0o700)
    return {
        "HERMES_CONNECTOR_CLOUD_ENDPOINT": (
            "wss://cloud.example.test/hermes/internal/connector/ws"
        ),
        "HERMES_CONNECTOR_API_ENDPOINT": "https://cloud.example.test/hermes",
        "HERMES_CONNECTOR_PROFILE": "default",
        "HERMES_CONNECTOR_VERSION": "1.2.3",
        **{key: str(path) for key, path in role_directories.items()},
        "HERMES_CONNECTOR_STATE_DIR": str(state_directory),
        "HERMES_CONNECTOR_DATABASE_FILE": str(state_directory / "connector.sqlite3"),
        "HERMES_CONNECTOR_LOCK_FILE": str(state_directory / "connector.lock"),
    }


def _paired_projection() -> PairedProjection:
    return PairedProjection(
        tenant_id=UUID("66666666-6666-4666-8666-666666666666"),
        device_id=UUID("77777777-7777-4777-8777-777777777777"),
        credential_id=UUID("88888888-8888-4888-8888-888888888888"),
        agent_id=UUID("99999999-9999-4999-8999-999999999999"),
        scopes=("session.observe",),
        key_handle="hermes-device-key:v1:" + "B" * 43,
        credential_fingerprint="SHA256:" + "B" * 43,
        token_expires_at=datetime.now(UTC) + timedelta(seconds=300),
        lifecycle_state="active",
    )


class _NoIOFakeSecurityFrameworkAPI:
    def __init__(self) -> None:
        self.available_checks = 0
        self.commands = 0

    def check_available(self) -> None:
        self.available_checks += 1

    async def run(self, *args, **kwargs) -> NoReturn:
        self.commands += 1
        raise AssertionError(
            "composition/check must not touch the real or fake Keychain"
        )


class _VerifiedLocalRuntimePreflight:
    def __init__(self) -> None:
        self.endpoint = AgentEndpoint(
            pid=101,
            profile="default",
            socket_path=Path("/private/fixture/local.sock"),
            instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            runtime_generation="runtime-generation-1",
            host_bundle_id="com.nousresearch.hermes",
            process_identity=ProcessIdentityEvidence(
                start_time_ns=1_000,
                executable_path=Path("/private/fixture/hermes-python"),
                executable_device=41,
                executable_inode=73,
            ),
            socket_device=51,
            socket_inode=79,
            registry_path=Path("/private/fixture/local.json"),
        )

    def verify(self, profile: str) -> AgentEndpoint | None:
        return self.endpoint if profile == "default" else None


def test_macos_runtime_composes_frozen_observer_cloud_ingress_without_starting_io(
    tmp_path: Path,
) -> None:
    settings = load_runtime_settings(_valid_environment(tmp_path))
    projection = _paired_projection()
    asyncio.run(
        MacOSPairedProjectionStore(settings.paired_projection_file).save(projection)
    )
    security_api = _NoIOFakeSecurityFrameworkAPI()

    runtime = build_macos_runtime(
        settings,
        release_id="2026.08.03-b2a",
        config=ConnectorConfig(),
        logger=SafeStructuredLogger(lambda _: None),
        security_api=security_api,
        local_runtime_preflight=_VerifiedLocalRuntimePreflight(),
    )

    assert isinstance(runtime.observer_outbound_lane, ObserverOutboundLane)
    assert isinstance(runtime.observer_intent_lane, ObserverIntentLane)
    assert runtime.cloud_wss._observer_intent_lane is runtime.observer_intent_lane
    assert runtime.cloud_wss._observer_outbound_lane is runtime.observer_outbound_lane
    assert isinstance(runtime.components[-1], ReadinessStatusComponent)
    assert runtime.status_receipt is runtime.components[-1]
    assert runtime.status_receipt._store.path == settings.status_receipt_file
    assert not settings.status_receipt_file.exists()
    assert settings.instance_state_file.exists()
    assert not settings.database_file.exists()
    assert not settings.lock_file.exists()
    assert security_api.available_checks == 1
    assert security_api.commands == 0


def test_missing_local_runtime_fails_before_any_persistent_bootstrap_side_effect(
    tmp_path: Path,
) -> None:
    settings = load_runtime_settings(_valid_environment(tmp_path))
    asyncio.run(
        MacOSPairedProjectionStore(settings.paired_projection_file).save(
            _paired_projection()
        )
    )
    before = _tree_snapshot(tmp_path)

    with pytest.raises(LocalRuntimeUnavailable):
        build_macos_runtime(
            settings,
            release_id="2026.08.03-b2a",
            config=ConnectorConfig(),
            logger=SafeStructuredLogger(lambda _: None),
            security_api=_NoIOFakeSecurityFrameworkAPI(),
        )

    assert _tree_snapshot(tmp_path) == before
    assert not settings.instance_state_file.exists()
    assert not settings.database_file.exists()
    assert not settings.lock_file.exists()


def test_formal_runtime_refuses_unpaired_projection(tmp_path: Path) -> None:
    settings = load_runtime_settings(_valid_environment(tmp_path))

    with pytest.raises(UnpairedConnector):
        build_macos_runtime(
            settings,
            release_id="2026.08.03-b2a",
            config=ConnectorConfig(),
            logger=SafeStructuredLogger(lambda _: None),
            security_api=_NoIOFakeSecurityFrameworkAPI(),
        )


@pytest.mark.asyncio
async def test_foundation_projection_invalidator_is_explicit_zero_state_noop() -> None:
    invalidator = FoundationNoOpLocalProjectionInvalidator()

    await invalidator.invalidate_runtime("old-generation", "new-generation")

    assert invalidator.foundation_effect == "none"
    assert not hasattr(invalidator, "__dict__")


def test_check_validates_paths_and_credentials_without_creating_runtime_state(
    tmp_path: Path,
) -> None:
    settings = load_runtime_settings(_valid_environment(tmp_path))
    security_api = _NoIOFakeSecurityFrameworkAPI()

    check_macos_runtime(settings, security_api=security_api)

    assert not settings.instance_state_file.exists()
    assert not settings.database_file.exists()
    assert not settings.lock_file.exists()
    assert security_api.available_checks == 1
    assert security_api.commands == 0


@pytest.mark.parametrize(
    "directory_field",
    (
        "local_gateway_registry_directory",
        "local_gateway_socket_directory",
        "control_registry_directory",
        "control_socket_directory",
        "observer_registry_directory",
        "observer_socket_directory",
        "state_directory",
    ),
)
@pytest.mark.parametrize("mode", (0o500, 0o755))
def test_check_rejects_untrusted_runtime_directories(
    tmp_path: Path,
    directory_field: str,
    mode: int,
) -> None:
    settings = load_runtime_settings(_valid_environment(tmp_path))
    getattr(settings, directory_field).chmod(mode)

    with pytest.raises(ValueError):
        check_macos_runtime(
            settings,
            security_api=_NoIOFakeSecurityFrameworkAPI(),
        )


def test_check_rejects_read_only_managed_database_file(tmp_path: Path) -> None:
    settings = load_runtime_settings(_valid_environment(tmp_path))
    settings.database_file.write_text("", encoding="utf-8")
    settings.database_file.chmod(0o400)

    with pytest.raises(ValueError):
        check_macos_runtime(
            settings,
            security_api=_NoIOFakeSecurityFrameworkAPI(),
        )


def test_check_rejects_existing_unsafe_database_or_lock_file(
    tmp_path: Path,
) -> None:
    settings = load_runtime_settings(_valid_environment(tmp_path))
    settings.database_file.write_text("", encoding="utf-8")
    settings.database_file.chmod(0o644)

    with pytest.raises(ValueError):
        check_macos_runtime(
            settings,
            security_api=_NoIOFakeSecurityFrameworkAPI(),
        )

    settings.database_file.chmod(0o600)
    settings.lock_file.symlink_to(settings.database_file)
    with pytest.raises(ValueError):
        check_macos_runtime(
            settings,
            security_api=_NoIOFakeSecurityFrameworkAPI(),
        )


def test_check_revalidates_post_load_physical_role_isolation(tmp_path: Path) -> None:
    settings = load_runtime_settings(_valid_environment(tmp_path))
    settings = replace(
        settings,
        observer_socket_directory=settings.control_socket_directory,
    )

    with pytest.raises(ValueError, match="physically distinct"):
        check_macos_runtime(
            settings,
            security_api=_NoIOFakeSecurityFrameworkAPI(),
        )


def test_check_rejects_parent_symlink_created_after_settings_load(
    tmp_path: Path,
) -> None:
    settings = load_runtime_settings(_valid_environment(tmp_path))
    original_parent = tmp_path
    moved_parent = tmp_path.parent / f"{tmp_path.name}-moved"
    original_parent.rename(moved_parent)
    original_parent.symlink_to(moved_parent, target_is_directory=True)

    try:
        with pytest.raises(ValueError, match="symlink"):
            check_macos_runtime(
                settings,
                security_api=_NoIOFakeSecurityFrameworkAPI(),
            )
    finally:
        original_parent.unlink()
        moved_parent.rename(original_parent)


def test_formal_runtime_rejects_file_credential_migration_mode(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    token_file = tmp_path / "cloud-token"
    token_file.write_text("opaque-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    environment["HERMES_CONNECTOR_CREDENTIAL_STORE"] = "file"
    environment["HERMES_CONNECTOR_TOKEN_FILE"] = str(token_file)
    settings = load_runtime_settings(environment)
    security_api = _NoIOFakeSecurityFrameworkAPI()

    with pytest.raises(FileCredentialRuntimeForbidden):
        build_macos_runtime(
            settings,
            release_id="2026.08.03-b2a",
            config=ConnectorConfig(),
            logger=SafeStructuredLogger(lambda _: None),
            security_api=security_api,
        )
    assert security_api.commands == 0

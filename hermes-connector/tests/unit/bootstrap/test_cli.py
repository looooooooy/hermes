from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from hermes_connector import cli
from hermes_connector.adapters.platform.macos.process_identity import (
    current_process_identity,
)
from hermes_connector.adapters.platform.macos.status_receipt import (
    MacOSStatusReceiptStore,
)
from hermes_connector.application.file_credential_migration import (
    FileCredentialMigrationDisplay,
)
from hermes_connector.application.local_gateway_client import LocalRuntimeUnavailable
from hermes_connector.application.supervisor import SupervisorStartError
from hermes_connector.domain.cloud_session import CloudSessionState
from hermes_connector.domain.local_gateway import ProcessIdentityEvidence
from hermes_connector.domain.pairing import (
    PairingCancelDisplay,
    PairingStartDisplay,
    PairingStatusDisplay,
)
from hermes_connector.domain.readiness_status import (
    ConnectorStatusReceipt,
    LocalAuthorityIdentity,
)

CONNECTOR_ROOT = Path(__file__).resolve().parents[3]
RELEASE_ID = "2026.08.03-b2a"


def _status_receipt(*, ready: bool) -> ConnectorStatusReceipt:
    return ConnectorStatusReceipt(
        release_id=RELEASE_ID,
        pid=8123,
        process_identity=ProcessIdentityEvidence(
            start_time_ns=1_786_000_000_123_000_000,
            executable_path=Path(
                "/Applications/Hermes Connector.app/Contents/MacOS/python"
            ),
            executable_device=41,
            executable_inode=73,
        ),
        runtime_generation="runtime-generation-17",
        local_authority_identity=LocalAuthorityIdentity(
            profile="default",
            instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            host_bundle_id="com.nousresearch.hermes",
        ),
        cloud_state=(
            CloudSessionState.ACTIVE
            if ready
            else CloudSessionState.RECONCILING
        ),
        updated_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        ready=ready,
    )


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


def test_check_validates_without_network_or_runtime_file_creation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _valid_environment(tmp_path)
    lines: list[str] = []
    monkeypatch.setattr(
        cli,
        "build_macos_runtime",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("check must not compose or connect")
        ),
    )

    exit_code = cli.main(
        ["--check"],
        environment=environment,
        platform_name="darwin",
        output=lines.append,
    )

    assert exit_code == 0
    assert lines == ["hermes-connector: configuration_valid"]
    state_directory = Path(environment["HERMES_CONNECTOR_STATE_DIR"])
    assert not (state_directory / "instances.json").exists()
    assert not Path(environment["HERMES_CONNECTOR_DATABASE_FILE"]).exists()
    assert not Path(environment["HERMES_CONNECTOR_LOCK_FILE"]).exists()


def test_plaintext_cli_and_environment_tokens_fail_without_disclosure(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    lines: list[str] = []

    exit_code = cli.main(
        ["run", "--token", "must-never-appear"],
        environment=environment,
        platform_name="darwin",
        output=lines.append,
    )

    assert exit_code == 2
    assert "must-never-appear" not in repr(lines)

    environment["HERMES_CONNECTOR_ACCESS_TOKEN"] = "also-never-appear"
    lines.clear()
    exit_code = cli.main(
        ["--check"],
        environment=environment,
        platform_name="darwin",
        output=lines.append,
    )
    assert exit_code == 2
    assert "also-never-appear" not in repr(lines)


def test_help_is_safe_and_successful() -> None:
    lines: list[str] = []

    exit_code = cli.main(["--help"], output=lines.append)

    assert exit_code == 0
    assert lines == [
        "usage: hermes-connector "
        + (
            "{run --release-id ID|status --json|--check|pair start|"
            "pair status|pair cancel|credential migrate-file}"
        )
    ]


def test_unimplemented_platforms_fail_closed_before_composition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _valid_environment(tmp_path)
    monkeypatch.setattr(
        cli,
        "build_macos_runtime",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unsupported platform must not compose macOS")
        ),
    )

    for platform_name in ("linux", "win32"):
        lines: list[str] = []
        exit_code = cli.main(
            ["--check"],
            environment=environment,
            platform_name=platform_name,
            output=lines.append,
        )

        assert exit_code == 3
        assert lines == ["hermes-connector: platform_unavailable"]


def test_windows_can_import_help_and_fail_closed_without_fcntl() -> None:
    script = """
import importlib.abc
import sys

class MissingFcntl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "fcntl":
            raise ModuleNotFoundError("No module named 'fcntl'")
        return None

sys.modules.pop("fcntl", None)
sys.meta_path.insert(0, MissingFcntl())

from hermes_connector import cli

lines = []
assert cli.main(["--help"], output=lines.append) == 0
assert lines == [
    "usage: hermes-connector "
    "{run --release-id ID|status --json|--check|pair start|pair status|"
    "pair cancel|credential migrate-file}"
]

lines.clear()
environment = {
    "HERMES_CONNECTOR_CLOUD_ENDPOINT": "wss://cloud.example.test/ws",
    "HERMES_CONNECTOR_API_ENDPOINT": "https://cloud.example.test/hermes",
    "HERMES_CONNECTOR_PROFILE": "default",
    "HERMES_CONNECTOR_VERSION": "1.2.3",
    "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR": "/nonexistent/local-registry",
    "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR": "/nonexistent/local-sockets",
    "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR": "/nonexistent/control-registry",
    "HERMES_CONNECTOR_CONTROL_SOCKET_DIR": "/nonexistent/control-sockets",
    "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR": "/nonexistent/observer-registry",
    "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR": "/nonexistent/observer-sockets",
    "HERMES_CONNECTOR_STATE_DIR": "/nonexistent/state",
    "HERMES_CONNECTOR_DATABASE_FILE": "/nonexistent/state/connector.sqlite3",
    "HERMES_CONNECTOR_LOCK_FILE": "/nonexistent/state/connector.lock",
}
assert cli.main(
    ["--check"],
    environment=environment,
    platform_name="win32",
    output=lines.append,
) == 3
assert lines == ["hermes-connector: platform_unavailable"]
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(CONNECTOR_ROOT / "src")

    completed = subprocess.run(
        (sys.executable, "-c", script),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_run_uses_service_runner_until_bounded_stop_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _valid_environment(tmp_path)
    timeline: list[str] = []

    class Runner:
        async def run_until(self, stop_event: asyncio.Event) -> None:
            timeline.append("runner.start")
            asyncio.get_running_loop().call_soon(stop_event.set)
            await stop_event.wait()
            timeline.append("runner.stop")

    observed_release_ids: list[str] = []

    def build(*args, **kwargs):
        del args
        observed_release_ids.append(kwargs["release_id"])
        return SimpleNamespace(runner=Runner())

    monkeypatch.setattr(cli, "build_macos_runtime", build)

    exit_code = cli.main(
        ["run", "--release-id", RELEASE_ID],
        environment=environment,
        platform_name="darwin",
        output=lambda _: None,
    )

    assert exit_code == 0
    assert timeline == ["runner.start", "runner.stop"]
    assert observed_release_ids == [RELEASE_ID]


def test_run_requires_one_safe_explicit_release_id(tmp_path: Path) -> None:
    environment = _valid_environment(tmp_path)

    for arguments in (
        ["run"],
        ["run", "--release-id", "../escape"],
        ["run", "--release-id", "two/parts"],
        ["run", "--release-id", ""],
        ["run", "--release-id", RELEASE_ID, "extra"],
    ):
        lines: list[str] = []
        assert (
            cli.main(
                arguments,
                environment=environment,
                platform_name="darwin",
                output=lines.append,
            )
            == 2
        )
        assert lines == ["hermes-connector: invalid_arguments"]


def test_signal_handlers_request_stop_and_are_removed() -> None:
    class Loop:
        def __init__(self) -> None:
            self.handlers: dict[signal.Signals, object] = {}
            self.removed: list[signal.Signals] = []

        def add_signal_handler(self, selected, callback) -> None:
            self.handlers[selected] = callback

        def remove_signal_handler(self, selected) -> bool:
            self.removed.append(selected)
            return True

    loop = Loop()
    stop_event = asyncio.Event()

    installed = cli.install_stop_signal_handlers(loop, stop_event)
    assert installed == (signal.SIGINT, signal.SIGTERM)
    callback = loop.handlers[signal.SIGTERM]
    assert callable(callback)
    callback()
    assert stop_event.is_set()

    cli.remove_stop_signal_handlers(loop, installed)
    assert loop.removed == [signal.SIGINT, signal.SIGTERM]


def test_runtime_failure_has_safe_exit_code_and_message(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _valid_environment(tmp_path)
    lines: list[str] = []

    class Runner:
        async def run_until(self, stop_event: asyncio.Event) -> None:
            raise RuntimeError("must-never-appear")

    monkeypatch.setattr(
        cli,
        "build_macos_runtime",
        lambda *args, **kwargs: SimpleNamespace(runner=Runner()),
    )

    exit_code = cli.main(
        ["run", "--release-id", RELEASE_ID],
        environment=environment,
        platform_name="darwin",
        output=lines.append,
    )

    assert exit_code == 1
    assert lines[-1] == "hermes-connector: runtime_failed"
    assert "must-never-appear" not in repr(lines)


def test_local_runtime_unavailable_has_structured_retryable_message(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _valid_environment(tmp_path)
    lines: list[str] = []

    class Runner:
        async def run_until(self, stop_event: asyncio.Event) -> None:
            raise SupervisorStartError(
                "must-never-appear",
                category="local_runtime_unavailable",
                retryable=True,
            )

    monkeypatch.setattr(
        cli,
        "build_macos_runtime",
        lambda *args, **kwargs: SimpleNamespace(runner=Runner()),
    )

    exit_code = cli.main(
        ["run", "--release-id", RELEASE_ID],
        environment=environment,
        platform_name="darwin",
        output=lines.append,
    )

    assert exit_code == 1
    assert lines[-1] == (
        "hermes-connector: failure_category=local_runtime_unavailable retryable=true"
    )
    assert "must-never-appear" not in repr(lines)


def test_preflight_local_runtime_unavailable_has_structured_retryable_message(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _valid_environment(tmp_path)
    lines: list[str] = []
    monkeypatch.setattr(
        cli,
        "build_macos_runtime",
        lambda *args, **kwargs: (_ for _ in ()).throw(LocalRuntimeUnavailable()),
    )

    exit_code = cli.main(
        ["run", "--release-id", RELEASE_ID],
        environment=environment,
        platform_name="darwin",
        output=lines.append,
    )

    assert exit_code == 1
    assert lines[-1] == (
        "hermes-connector: failure_category=local_runtime_unavailable retryable=true"
    )


def test_run_rejects_file_credential_mode_before_runtime_composition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _valid_environment(tmp_path)
    token_file = tmp_path / "legacy-token"
    token_file.write_text("opaque-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    environment["HERMES_CONNECTOR_CREDENTIAL_STORE"] = "file"
    environment["HERMES_CONNECTOR_TOKEN_FILE"] = str(token_file)
    lines: list[str] = []
    monkeypatch.setattr(
        cli,
        "build_macos_runtime",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("formal run must reject file credentials before build")
        ),
    )

    exit_code = cli.main(
        ["run", "--release-id", RELEASE_ID],
        environment=environment,
        platform_name="darwin",
        output=lines.append,
    )

    assert exit_code == 2
    assert lines == ["hermes-connector: configuration_invalid"]


def test_status_json_has_stable_ready_not_ready_and_invalid_exit_codes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _valid_environment(tmp_path)

    for receipt, expected_exit in (
        (_status_receipt(ready=True), 0),
        (_status_receipt(ready=False), 2),
        (None, 3),
    ):
        monkeypatch.setattr(
            cli,
            "read_macos_status",
            lambda _settings, result=receipt: result,
        )
        lines: list[str] = []

        exit_code = cli.main(
            ["status", "--json"],
            environment=environment,
            platform_name="darwin",
            output=lines.append,
        )

        assert exit_code == expected_exit
        assert len(lines) == 1
        value = json.loads(lines[0])
        if receipt is None:
            assert value == {"ready": False}
        else:
            assert value["ready"] is receipt.ready
            assert value["release_id"] == RELEASE_ID
            assert set(value) == {
                "release_id",
                "pid",
                "process_start_time_ns",
                "process_executable",
                "process_executable_device",
                "process_executable_inode",
                "runtime_generation",
                "local_authority_identity",
                "cloud_state",
                "updated_at",
                "ready",
            }
        assert "token" not in lines[0]
        assert "wss://" not in lines[0]
        assert "session" not in lines[0]
        assert "tool" not in lines[0]


def test_status_json_configuration_failure_is_safe_invalid_json(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    environment["HERMES_CONNECTOR_STATE_DIR"] = "relative"
    lines: list[str] = []

    exit_code = cli.main(
        ["status", "--json"],
        environment=environment,
        platform_name="darwin",
        output=lines.append,
    )

    assert exit_code == 3
    assert lines == ['{"ready":false}']


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS process identity")
def test_status_json_real_reader_rejects_stale_crash_receipt(
    tmp_path: Path,
) -> None:
    environment = _valid_environment(tmp_path)
    identity = current_process_identity(os.getpid())
    assert identity is not None
    store = MacOSStatusReceiptStore(
        Path(environment["HERMES_CONNECTOR_STATE_DIR"]) / "status.json"
    )
    receipt = ConnectorStatusReceipt(
        release_id=RELEASE_ID,
        pid=os.getpid(),
        process_identity=identity,
        runtime_generation="runtime-generation-17",
        local_authority_identity=LocalAuthorityIdentity(
            profile="default",
            instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            host_bundle_id="com.nousresearch.hermes",
        ),
        cloud_state=CloudSessionState.ACTIVE,
        updated_at=datetime.now(UTC) - timedelta(seconds=31),
        ready=True,
    )
    store.publish(receipt)
    stale_lines: list[str] = []

    stale_exit = cli.main(
        ["status", "--json"],
        environment=environment,
        platform_name="darwin",
        output=stale_lines.append,
    )

    assert stale_exit == 3
    assert stale_lines == ['{"ready":false}']

    store.publish(
        ConnectorStatusReceipt(
            release_id=receipt.release_id,
            pid=receipt.pid,
            process_identity=receipt.process_identity,
            runtime_generation=receipt.runtime_generation,
            local_authority_identity=receipt.local_authority_identity,
            cloud_state=receipt.cloud_state,
            updated_at=datetime.now(UTC),
            ready=True,
        )
    )
    current_lines: list[str] = []
    current_exit = cli.main(
        ["status", "--json"],
        environment=environment,
        platform_name="darwin",
        output=current_lines.append,
    )

    assert current_exit == 0
    assert json.loads(current_lines[0])["release_id"] == RELEASE_ID


def test_pair_start_outputs_only_human_code_fingerprint_and_expiry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _valid_environment(tmp_path)
    for key in (
        "HERMES_CONNECTOR_TENANT_ID",
        "HERMES_CONNECTOR_DEVICE_ID",
        "HERMES_CONNECTOR_CREDENTIAL_STORE",
        "HERMES_CONNECTOR_TOKEN_FILE",
    ):
        environment.pop(key, None)
    lines: list[str] = []

    class Coordinator:
        async def start(self) -> PairingStartDisplay:
            return PairingStartDisplay(
                pairing_code="ABCD-EFGH",
                credential_fingerprint="SHA256:" + "B" * 43,
                expires_at=datetime(2026, 7, 31, 12, 5, tzinfo=UTC),
            )

    class PairingRuntime:
        coordinator = Coordinator()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(
        cli,
        "build_macos_pairing_runtime",
        lambda _settings: PairingRuntime(),
    )

    exit_code = cli.main(
        ["pair", "start"],
        environment=environment,
        platform_name="darwin",
        output=lines.append,
    )

    assert exit_code == 0
    assert lines == [
        "pairing_code=ABCD-EFGH",
        "credential_fingerprint=SHA256:" + "B" * 43,
        "expires_at=2026-07-31T12:05:00Z",
    ]
    assert "pairing_offer_secret" not in repr(lines)
    assert "access_token" not in repr(lines)


def test_pair_status_and_cancel_have_stable_safe_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _valid_environment(tmp_path)
    for key in (
        "HERMES_CONNECTOR_TENANT_ID",
        "HERMES_CONNECTOR_DEVICE_ID",
        "HERMES_CONNECTOR_CREDENTIAL_STORE",
        "HERMES_CONNECTOR_TOKEN_FILE",
    ):
        environment.pop(key, None)

    class Coordinator:
        async def status(self) -> PairingStatusDisplay:
            return PairingStatusDisplay(
                state="claimed",
                activation_state="waiting_owner_confirmation",
                credential_fingerprint="SHA256:" + "B" * 43,
                expires_at=datetime(2026, 7, 31, 12, 5, tzinfo=UTC),
                revision=2,
            )

        async def cancel(self) -> PairingCancelDisplay:
            return PairingCancelDisplay(state="cancelled_local")

    class PairingRuntime:
        coordinator = Coordinator()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(
        cli,
        "build_macos_pairing_runtime",
        lambda _settings: PairingRuntime(),
    )

    status_lines: list[str] = []
    cancel_lines: list[str] = []
    assert (
        cli.main(
            ["pair", "status"],
            environment=environment,
            platform_name="darwin",
            output=status_lines.append,
        )
        == 0
    )
    assert (
        cli.main(
            ["pair", "cancel"],
            environment=environment,
            platform_name="darwin",
            output=cancel_lines.append,
        )
        == 0
    )
    assert status_lines == [
        "pairing_state=claimed",
        "activation_state=waiting_owner_confirmation",
        "credential_fingerprint=SHA256:" + "B" * 43,
        "expires_at=2026-07-31T12:05:00Z",
    ]
    assert cancel_lines == ["pairing_state=cancelled_local"]


def test_pairing_failure_has_distinct_safe_exit_category(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _valid_environment(tmp_path)
    for key in (
        "HERMES_CONNECTOR_TENANT_ID",
        "HERMES_CONNECTOR_DEVICE_ID",
        "HERMES_CONNECTOR_CREDENTIAL_STORE",
        "HERMES_CONNECTOR_TOKEN_FILE",
    ):
        environment.pop(key, None)

    class Coordinator:
        async def status(self) -> PairingStatusDisplay:
            raise ValueError("pairing-offer-secret-must-never-appear")

    class PairingRuntime:
        coordinator = Coordinator()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(
        cli,
        "build_macos_pairing_runtime",
        lambda _settings: PairingRuntime(),
    )
    lines: list[str] = []

    exit_code = cli.main(
        ["pair", "status"],
        environment=environment,
        platform_name="darwin",
        output=lines.append,
    )

    assert exit_code == 1
    assert lines == ["hermes-connector: pairing_failed"]
    assert "pairing-offer-secret-must-never-appear" not in repr(lines)


def test_file_credential_is_available_only_through_one_shot_migration_command(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment = _valid_environment(tmp_path)
    token_file = tmp_path / "legacy-token"
    token_file.write_text("legacy-token-must-not-be-disclosed\n", encoding="utf-8")
    token_file.chmod(0o600)
    environment["HERMES_CONNECTOR_CREDENTIAL_STORE"] = "file"
    environment["HERMES_CONNECTOR_TOKEN_FILE"] = str(token_file)
    lines: list[str] = []

    class Migration:
        async def migrate(self) -> FileCredentialMigrationDisplay:
            return FileCredentialMigrationDisplay(
                device_id=UUID("77777777-7777-4777-8777-777777777777"),
                credential_fingerprint="SHA256:" + "B" * 43,
            )

    class MigrationRuntime:
        migration = Migration()

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(
        cli,
        "build_macos_file_credential_migration",
        lambda _settings: MigrationRuntime(),
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "build_macos_runtime",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("migration must not compose formal runtime")
        ),
    )

    exit_code = cli.main(
        ["credential", "migrate-file"],
        environment=environment,
        platform_name="darwin",
        output=lines.append,
    )

    assert exit_code == 0
    assert lines == [
        "credential_migration=complete",
        "device_id=77777777-7777-4777-8777-777777777777",
        "credential_fingerprint=SHA256:" + "B" * 43,
    ]
    assert "legacy-token-must-not-be-disclosed" not in repr(lines)

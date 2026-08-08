from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_connector import cli
from hermes_connector.adapters.platform.windows.private_state import (
    validate_private_directory,
)
from hermes_connector.domain.cloud_session import CloudSessionState
from hermes_connector.domain.local_gateway import ProcessIdentityEvidence
from hermes_connector.domain.pairing import PairingStartDisplay
from hermes_connector.domain.readiness_status import (
    ConnectorStatusReceipt,
    LocalAuthorityIdentity,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows CLI required")
RELEASE_ID = "2026.08.08-windows-cli"


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "HERMES_CONNECTOR_CLOUD_ENDPOINT": "wss://cloud.example.test/connector/ws",
        "HERMES_CONNECTOR_API_ENDPOINT": "https://cloud.example.test/hermes",
        "HERMES_CONNECTOR_PROFILE": "default",
        "HERMES_CONNECTOR_VERSION": "1.2.3",
        "HERMES_HOME": str(tmp_path / "hermes-home"),
    }


def _select_windows(_platform_name=None):
    return SimpleNamespace(platform_name="windows")


def _receipt(tmp_path: Path) -> ConnectorStatusReceipt:
    executable = Path(os.path.abspath(os.sys.executable))
    metadata = executable.stat()
    return ConnectorStatusReceipt(
        release_id=RELEASE_ID,
        pid=os.getpid(),
        process_identity=ProcessIdentityEvidence(
            start_time_ns=1_786_000_000_123_000_000,
            executable_path=executable,
            executable_device=metadata.st_dev,
            executable_inode=metadata.st_ino,
        ),
        runtime_generation="runtime-generation-windows-cli",
        local_authority_identity=LocalAuthorityIdentity(
            profile="default",
            instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            host_bundle_id="com.hermes.windows-cli",
        ),
        cloud_state=CloudSessionState.ACTIVE,
        updated_at=datetime.now(UTC),
        ready=True,
    )


def test_windows_check_uses_windows_settings_and_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines: list[str] = []
    observed = []
    monkeypatch.setattr(cli, "select_platform_adapters", _select_windows)
    monkeypatch.setattr(
        cli,
        "check_windows_runtime",
        lambda settings: observed.append(settings),
    )

    exit_code = cli.main(
        ["--check"],
        environment=_environment(tmp_path),
        platform_name="win32",
        output=lines.append,
    )

    assert exit_code == 0
    assert lines == ["hermes-connector: configuration_valid"]
    assert len(observed) == 1
    assert observed[0].credential_store == "dpapi"
    assert observed[0].state_directory == (
        tmp_path / "hermes-home" / "connector" / "profiles" / "default" / "state"
    )


def test_windows_pair_start_provisions_private_state_before_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines: list[str] = []
    monkeypatch.setattr(cli, "select_platform_adapters", _select_windows)

    class Coordinator:
        async def start(self) -> PairingStartDisplay:
            return PairingStartDisplay(
                pairing_code="ABCD-EFGH",
                credential_fingerprint="SHA256:" + "B" * 43,
                expires_at=datetime(2026, 8, 8, 12, 0, tzinfo=UTC),
            )

    class PairingRuntime:
        coordinator = Coordinator()

        async def aclose(self) -> None:
            return None

    def build(settings):
        validate_private_directory(settings.state_directory)
        return PairingRuntime()

    monkeypatch.setattr(cli, "build_windows_pairing_runtime", build)

    exit_code = cli.main(
        ["pair", "start"],
        environment=_environment(tmp_path),
        platform_name="win32",
        output=lines.append,
    )

    assert exit_code == 0
    assert lines[0] == "pairing_code=ABCD-EFGH"
    assert lines[1].startswith("credential_fingerprint=SHA256:")
    assert lines[2].startswith("expires_at=")


def test_windows_status_uses_shared_receipt_codec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "select_platform_adapters", _select_windows)
    expected = _receipt(tmp_path)
    monkeypatch.setattr(cli, "read_windows_status", lambda _settings: expected)
    lines: list[str] = []

    exit_code = cli.main(
        ["status", "--json"],
        environment=_environment(tmp_path),
        platform_name="win32",
        output=lines.append,
    )

    assert exit_code == 0
    payload = json.loads(lines[0])
    assert payload["ready"] is True
    assert payload["release_id"] == RELEASE_ID
    assert "token" not in lines[0]


def test_windows_run_dispatches_dpapi_runtime_and_bounded_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "select_platform_adapters", _select_windows)
    timeline: list[str] = []

    class Runner:
        async def run_until(self, stop_event: asyncio.Event) -> None:
            timeline.append("runner.start")
            stop_event.set()
            await stop_event.wait()
            timeline.append("runner.stop")

    def build(settings, **kwargs):
        assert settings.credential_store == "dpapi"
        assert kwargs["release_id"] == RELEASE_ID
        return SimpleNamespace(runner=Runner())

    monkeypatch.setattr(cli, "build_windows_runtime", build)

    exit_code = cli.main(
        ["run", "--release-id", RELEASE_ID],
        environment=_environment(tmp_path),
        platform_name="win32",
        output=lambda _line: None,
    )

    assert exit_code == 0
    assert timeline == ["runner.start", "runner.stop"]


def test_windows_signal_bridge_requests_async_stop_and_restores_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks: dict[signal.Signals, object] = {}
    restored: list[tuple[signal.Signals, object]] = []
    previous = object()

    class Loop:
        def call_soon_threadsafe(self, callback):
            callback()

    monkeypatch.setattr(signal, "getsignal", lambda _selected: previous)

    def set_handler(selected, callback):
        if callback is previous:
            restored.append((selected, callback))
        else:
            callbacks[selected] = callback

    monkeypatch.setattr(signal, "signal", set_handler)
    stop_event = asyncio.Event()
    installed = cli.install_windows_stop_signal_handlers(Loop(), stop_event)

    assert signal.SIGINT in callbacks
    callback = callbacks[signal.SIGINT]
    assert callable(callback)
    callback(signal.SIGINT, None)
    assert stop_event.is_set()

    cli.remove_windows_stop_signal_handlers(installed)
    assert (signal.SIGINT, previous) in restored

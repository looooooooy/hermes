"""Safe asyncio command-line entrypoint for the Hermes Connector service."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from hermes_connector.adapters.platform.availability import PlatformUnavailable
from hermes_connector.adapters.status_receipt_codec import encode_status_receipt
from hermes_connector.application.file_credential_migration import (
    FileCredentialMigrationDisplay,
)
from hermes_connector.application.local_gateway_client import LocalRuntimeUnavailable
from hermes_connector.application.supervisor import SupervisorStartError
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.platform import select_platform_adapters
from hermes_connector.bootstrap.safe_logging import SafeStructuredLogger
from hermes_connector.bootstrap.settings import (
    ConnectorRuntimeSettings,
    RuntimeConfigurationError,
    load_runtime_settings,
)
from hermes_connector.domain.pairing import (
    PairingCancelDisplay,
    PairingStartDisplay,
    PairingStatusDisplay,
)
from hermes_connector.domain.readiness_status import (
    ConnectorStatusReceipt,
    validate_release_id,
)

Output = Callable[[str], None]


class SignalLoop(Protocol):
    def add_signal_handler(
        self,
        selected: signal.Signals,
        callback: Callable[[], None],
    ) -> None: ...

    def remove_signal_handler(self, selected: signal.Signals) -> bool: ...

    def call_soon_threadsafe(
        self,
        callback: Callable[[], None],
    ) -> object: ...


class Runner(Protocol):
    async def run_until(self, stop_event: asyncio.Event) -> None: ...


class Runtime(Protocol):
    runner: Runner


class PairingCoordinatorPort(Protocol):
    async def start(self) -> PairingStartDisplay: ...

    async def status(self) -> PairingStatusDisplay: ...

    async def cancel(self) -> PairingCancelDisplay: ...


class PairingRuntime(Protocol):
    coordinator: PairingCoordinatorPort

    async def aclose(self) -> None: ...


class FileCredentialMigrationPort(Protocol):
    async def migrate(self) -> FileCredentialMigrationDisplay: ...


class FileCredentialMigrationRuntime(Protocol):
    migration: FileCredentialMigrationPort

    async def aclose(self) -> None: ...


def check_macos_runtime(settings: ConnectorRuntimeSettings) -> None:
    from hermes_connector.bootstrap.macos import check_macos_runtime as check

    check(settings)


def build_macos_runtime(
    settings: ConnectorRuntimeSettings,
    *,
    release_id: str,
    config: ConnectorConfig,
    logger: SafeStructuredLogger,
) -> Runtime:
    from hermes_connector.bootstrap.macos import build_macos_runtime as build

    return build(settings, release_id=release_id, config=config, logger=logger)


def read_macos_status(
    settings: ConnectorRuntimeSettings,
) -> ConnectorStatusReceipt | None:
    from hermes_connector.adapters.platform.macos.process_identity import (
        current_process_identity,
    )
    from hermes_connector.adapters.platform.macos.status_receipt import (
        MacOSStatusReceiptStore,
    )

    return MacOSStatusReceiptStore(settings.status_receipt_file).read(
        now=datetime.now(UTC),
        process_identity_provider=current_process_identity,
    )


def build_macos_pairing_runtime(
    settings: ConnectorRuntimeSettings,
) -> PairingRuntime:
    from hermes_connector.bootstrap.macos import (
        build_macos_pairing_runtime as build,
    )

    return build(settings)


def build_macos_file_credential_migration(
    settings: ConnectorRuntimeSettings,
) -> FileCredentialMigrationRuntime:
    from hermes_connector.bootstrap.macos import (
        build_macos_file_credential_migration as build,
    )

    return build(settings)


def check_windows_runtime(settings: ConnectorRuntimeSettings) -> None:
    from hermes_connector.bootstrap.windows import check_windows_runtime as check

    check(settings)


def build_windows_runtime(
    settings: ConnectorRuntimeSettings,
    *,
    release_id: str,
    config: ConnectorConfig,
    logger: SafeStructuredLogger,
) -> Runtime:
    from hermes_connector.bootstrap.windows import build_windows_runtime as build

    return build(settings, release_id=release_id, config=config, logger=logger)


def read_windows_status(
    settings: ConnectorRuntimeSettings,
) -> ConnectorStatusReceipt | None:
    from hermes_connector.bootstrap.windows import read_windows_status as read

    value = read(settings)
    return value if isinstance(value, ConnectorStatusReceipt) else None


def build_windows_pairing_runtime(
    settings: ConnectorRuntimeSettings,
) -> PairingRuntime:
    from hermes_connector.bootstrap.windows import (
        build_windows_pairing_runtime as build,
    )

    return build(settings)


def provision_windows_runtime_state(settings: ConnectorRuntimeSettings) -> None:
    from hermes_connector.bootstrap.windows_provision import (
        provision_windows_runtime_state as provision,
    )

    provision(settings)


def install_stop_signal_handlers(
    loop: SignalLoop,
    stop_event: asyncio.Event,
) -> tuple[signal.Signals, ...]:
    installed: list[signal.Signals] = []
    for selected in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(selected, stop_event.set)
        installed.append(selected)
    return tuple(installed)


def remove_stop_signal_handlers(
    loop: SignalLoop,
    installed: Sequence[signal.Signals],
) -> None:
    for selected in installed:
        loop.remove_signal_handler(selected)


def install_windows_stop_signal_handlers(
    loop: SignalLoop,
    stop_event: asyncio.Event,
) -> tuple[tuple[signal.Signals, object], ...]:
    installed: list[tuple[signal.Signals, object]] = []

    def request_stop(_signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    selected_signals = [signal.SIGINT]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        selected_signals.append(sigbreak)
    for selected in selected_signals:
        previous = signal.getsignal(selected)
        signal.signal(selected, request_stop)
        installed.append((selected, previous))
    return tuple(installed)


def remove_windows_stop_signal_handlers(
    installed: Sequence[tuple[signal.Signals, object]],
) -> None:
    for selected, previous in installed:
        signal.signal(selected, previous)


async def run_service(
    runner: Runner,
    *,
    platform_name: str = "macos",
) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    if platform_name == "windows":
        installed_windows = install_windows_stop_signal_handlers(loop, stop_event)
        try:
            await runner.run_until(stop_event)
        finally:
            remove_windows_stop_signal_handlers(installed_windows)
        return
    installed = install_stop_signal_handlers(loop, stop_event)
    try:
        await runner.run_until(stop_event)
    finally:
        remove_stop_signal_handlers(loop, installed)


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    platform_name: str | None = None,
    output: Output | None = None,
) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    diagnostic_sink = output or _stderr
    command = _parse_mode(arguments, diagnostic_sink)
    if command is None:
        return 2
    if command.action == "help":
        return 0
    status_sink = output or _stdout

    previous_umask = os.umask(0o077)
    try:
        try:
            selected = select_platform_adapters(platform_name)
            settings = load_runtime_settings(
                environment,
                platform_name=selected.platform_name,
            )
            if command.action == "status":
                receipt = _read_status(selected.platform_name, settings)
                if receipt is None:
                    status_sink('{"ready":false}')
                    return 3
                status_sink(_status_json(receipt))
                return 0 if receipt.ready else 2
            if command.action == "check":
                _check_runtime(selected.platform_name, settings)
                diagnostic_sink("hermes-connector: configuration_valid")
                return 0
            if command.action.startswith("pair:"):
                action = command.action.removeprefix("pair:")
                if selected.platform_name == "windows" and action == "start":
                    provision_windows_runtime_state(settings)
                pairing_runtime = _build_pairing_runtime(
                    selected.platform_name,
                    settings,
                )
                try:
                    asyncio.run(
                        run_pairing_action(
                            pairing_runtime,
                            action,
                            diagnostic_sink,
                        )
                    )
                except Exception:  # noqa: BLE001 - redact pairing boundary
                    diagnostic_sink("hermes-connector: pairing_failed")
                    return 1
                return 0
            if command.action == "credential:migrate-file":
                if selected.platform_name != "macos" or settings.credential_store != "file":
                    raise RuntimeConfigurationError(
                        "file credential migration mode is required"
                    )
                migration_runtime = build_macos_file_credential_migration(settings)
                try:
                    asyncio.run(
                        run_file_credential_migration(
                            migration_runtime,
                            diagnostic_sink,
                        )
                    )
                except Exception:  # noqa: BLE001 - redact migration boundary
                    diagnostic_sink("hermes-connector: credential_migration_failed")
                    return 1
                return 0
            runtime = _build_runtime(
                selected.platform_name,
                settings,
                release_id=command.require_release_id(),
                logger=SafeStructuredLogger(diagnostic_sink),
            )
        except PlatformUnavailable:
            if command.action == "status":
                status_sink('{"ready":false}')
                return 3
            diagnostic_sink("hermes-connector: platform_unavailable")
            return 3
        except LocalRuntimeUnavailable:
            diagnostic_sink(
                "hermes-connector: "
                "failure_category=local_runtime_unavailable retryable=true"
            )
            return 1
        except (OSError, RuntimeConfigurationError, ValueError):
            if command.action == "status":
                status_sink('{"ready":false}')
                return 3
            diagnostic_sink("hermes-connector: configuration_invalid")
            return 2

        try:
            asyncio.run(
                run_service(
                    runtime.runner,
                    platform_name=selected.platform_name,
                )
            )
        except KeyboardInterrupt:
            diagnostic_sink("hermes-connector: interrupted")
            return 130
        except SupervisorStartError as error:
            if error.category == "local_runtime_unavailable" and error.retryable:
                diagnostic_sink(
                    "hermes-connector: "
                    "failure_category=local_runtime_unavailable retryable=true"
                )
            else:
                diagnostic_sink("hermes-connector: runtime_failed")
            return 1
        except Exception:  # noqa: BLE001 - process boundary emits only a safe category
            diagnostic_sink("hermes-connector: runtime_failed")
            return 1
        return 0
    finally:
        os.umask(previous_umask)


def _read_status(
    platform_name: str,
    settings: ConnectorRuntimeSettings,
) -> ConnectorStatusReceipt | None:
    if platform_name == "macos":
        return read_macos_status(settings)
    if platform_name == "windows":
        return read_windows_status(settings)
    raise PlatformUnavailable("selected platform is unavailable")


def _check_runtime(
    platform_name: str,
    settings: ConnectorRuntimeSettings,
) -> None:
    if platform_name == "macos":
        check_macos_runtime(settings)
        return
    if platform_name == "windows":
        check_windows_runtime(settings)
        return
    raise PlatformUnavailable("selected platform is unavailable")


def _build_pairing_runtime(
    platform_name: str,
    settings: ConnectorRuntimeSettings,
) -> PairingRuntime:
    if platform_name == "macos":
        return build_macos_pairing_runtime(settings)
    if platform_name == "windows":
        return build_windows_pairing_runtime(settings)
    raise PlatformUnavailable("selected platform is unavailable")


def _build_runtime(
    platform_name: str,
    settings: ConnectorRuntimeSettings,
    *,
    release_id: str,
    logger: SafeStructuredLogger,
) -> Runtime:
    if platform_name == "macos":
        if settings.credential_store != "keychain":
            raise RuntimeConfigurationError(
                "formal runtime requires Keychain credentials"
            )
        return build_macos_runtime(
            settings,
            release_id=release_id,
            config=ConnectorConfig(),
            logger=logger,
        )
    if platform_name == "windows":
        if settings.credential_store != "dpapi":
            raise RuntimeConfigurationError(
                "formal Windows runtime requires DPAPI credentials"
            )
        return build_windows_runtime(
            settings,
            release_id=release_id,
            config=ConnectorConfig(),
            logger=logger,
        )
    raise PlatformUnavailable("selected platform is unavailable")


@dataclass(frozen=True, slots=True)
class _Command:
    action: str
    release_id: str | None = None

    def require_release_id(self) -> str:
        if self.release_id is None:
            raise RuntimeConfigurationError("release id is required")
        return self.release_id


def _parse_mode(arguments: tuple[str, ...], output: Output) -> _Command | None:
    if len(arguments) == 3 and arguments[:2] == ("run", "--release-id"):
        try:
            release_id = validate_release_id(arguments[2])
        except ValueError:
            output("hermes-connector: invalid_arguments")
            return None
        return _Command("run", release_id)
    if arguments == ("status", "--json"):
        return _Command("status")
    if arguments == ("--check",):
        return _Command("check")
    if (
        len(arguments) == 2
        and arguments[0] == "pair"
        and arguments[1] in {"start", "status", "cancel"}
    ):
        return _Command(f"pair:{arguments[1]}")
    if arguments == ("credential", "migrate-file"):
        return _Command("credential:migrate-file")
    if arguments in {("-h",), ("--help",)}:
        output(
            "usage: hermes-connector "
            "{run --release-id ID|status --json|--check|pair start|pair status|"
            "pair cancel|credential migrate-file}"
        )
        return _Command("help")
    output("hermes-connector: invalid_arguments")
    return None


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


def _stdout(message: str) -> None:
    print(message)


def _status_json(receipt: ConnectorStatusReceipt) -> str:
    return encode_status_receipt(receipt).decode("ascii")


async def run_pairing_action(
    runtime: PairingRuntime,
    action: str,
    output: Output,
) -> None:
    coordinator = runtime.coordinator
    try:
        if action == "start":
            result = await coordinator.start()
            output(f"pairing_code={result.pairing_code}")
            output(f"credential_fingerprint={result.credential_fingerprint}")
            output(f"expires_at={_format_datetime(result.expires_at)}")
            return
        if action == "status":
            result = await coordinator.status()
            output(f"pairing_state={result.state}")
            output(f"activation_state={result.activation_state}")
            output(f"credential_fingerprint={result.credential_fingerprint}")
            output(f"expires_at={_format_datetime(result.expires_at)}")
            return
        if action == "cancel":
            result = await coordinator.cancel()
            output(f"pairing_state={result.state}")
            return
        raise ValueError("pairing action is invalid")
    finally:
        await runtime.aclose()


async def run_file_credential_migration(
    runtime: FileCredentialMigrationRuntime,
    output: Output,
) -> None:
    try:
        result = await runtime.migration.migrate()
        output("credential_migration=complete")
        output(f"device_id={result.device_id}")
        output(f"credential_fingerprint={result.credential_fingerprint}")
    finally:
        await runtime.aclose()


def _format_datetime(value: datetime) -> str:
    utc = value.astimezone(UTC)
    if utc.microsecond:
        return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return utc.isoformat(timespec="seconds").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())

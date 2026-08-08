"""Explicit Windows ServiceRunner assembly used by formal-runtime validation."""

from __future__ import annotations

import os
from collections.abc import Iterable

from hermes_connector.adapters.platform.windows.instance_lock import WindowsInstanceLock
from hermes_connector.application.service_runner import ServiceRunner
from hermes_connector.application.supervisor import Supervisor
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.ports.component import ComponentPort
from hermes_connector.ports.logging import SafeLogPort


def build_windows_service_runner(
    *,
    lock_path: str | os.PathLike[str],
    components: Iterable[ComponentPort],
    config: ConnectorConfig,
    logger: SafeLogPort,
) -> ServiceRunner:
    """Assemble the Windows service runner without selecting platform adapters."""

    instance_lock = WindowsInstanceLock(lock_path)
    return ServiceRunner(instance_lock, Supervisor(components, config, logger))


__all__ = ["build_windows_service_runner"]

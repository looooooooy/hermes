from __future__ import annotations

from pathlib import Path

from hermes_connector.adapters.platform.windows.private_state import (
    ensure_private_directory,
)
from hermes_connector.bootstrap.settings import ConnectorRuntimeSettings


def provision_windows_runtime_state(settings: ConnectorRuntimeSettings) -> Path:
    """Create the missing current-user private state path for explicit setup."""

    if settings.credential_store != "dpapi":
        raise ValueError("Windows runtime provisioning requires DPAPI credentials")
    target = settings.state_directory
    missing: list[Path] = []
    current = target
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise ValueError("Windows runtime state has no existing ancestor")
        current = parent
    for directory in reversed(missing):
        ensure_private_directory(directory)
    if not missing:
        ensure_private_directory(target)
    return target


__all__ = ["provision_windows_runtime_state"]
